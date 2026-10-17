"""读好友（会话）列表 —— 面板"勾选目标"背后的那次滚动扫描。

为什么要有它：以前目标好友只能**手打名字**，主程序再去页面上一项一项比对。
名字会改、会重名，`DouyinIM._match()` 最后还有一条子串兜底（目标「小明」会命中
「小明明」）—— 打错一个字就可能发给另一个人。改成"先把列表读出来、由人来勾"
之后，勾选的那一刻我们就拿到了抖音自己算的 `uid` 与 `conv_id`，
`target_ref()` 会让这类目标**只按 id 匹配**，名字退化成纯粹的显示文本。

工作方式与 `core.qr_login` 完全一致：cloakbrowser 是 Playwright 的同步 API，
**有线程亲和性**，所有浏览器调用必须发生在同一个线程里；外部（面板）只读
`snapshot()` / `items()` 这两个加锁快照，不碰 page。

只读：本模块不写 `.env`、不改任何配置。写盘由 `webui/envfile.py` 负责。
"""
import threading
import time

from utils.logger import setup_logger

logger = setup_logger("friend_scan")

STATE_STARTING = "starting"
STATE_OPENING = "opening"
STATE_READY = "ready"
STATE_SCANNING = "scanning"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"

ACTIVE_STATES = (STATE_STARTING, STATE_OPENING, STATE_READY, STATE_SCANNING)

STATE_LABELS = {
    STATE_STARTING: "正在准备",
    STATE_OPENING: "正在打开浏览器",
    STATE_READY: "登录态正常，正在加载会话列表",
    STATE_SCANNING: "正在读取会话列表",
    STATE_DONE: "读取完成",
    STATE_ERROR: "读取失败",
    STATE_CANCELLED: "已取消",
}

# 单次扫描的预算：门禁 45 秒、整轮滚动 300 秒、资料静默窗 800 毫秒、滚动步数上限 400
# （与 configTool 的好友选择器同口径，那边实测 300~400 步足够扫完一个正常账号）
READY_TIMEOUT_SECONDS = 45.0
DEFAULT_TIMEOUT_SECONDS = 300.0
SETTLE_MS = 800
MAX_STEPS = 400
MAX_ITEMS = 2000        # 单账号最多带回这么多条，防止异常页面把内存和页面撑爆

TERMINAL_STATES = (STATE_DONE, STATE_ERROR, STATE_CANCELLED)


def public_item(raw):
    """会话项 → 面板/配置需要的那几个字段（多余的一律不带出去）。"""
    if not isinstance(raw, dict):
        return None
    conv_id = str(raw.get("conv_id") or "").strip()
    name = str(raw.get("display") or raw.get("remark") or raw.get("nickname")
               or raw.get("title") or "").strip()
    if not (conv_id or name):
        return None
    return {
        "name": name or conv_id,
        "title": str(raw.get("title") or "").strip(),
        "remark": str(raw.get("remark") or "").strip(),
        "nickname": str(raw.get("nickname") or "").strip(),
        "douyin_id": str(raw.get("douyin_id") or "").strip(),
        "uid": str(raw.get("uid") or "").strip(),
        "sec_uid": str(raw.get("sec_uid") or "").strip(),
        "conv_id": conv_id,
        "is_group": bool(raw.get("is_group")),
        "unread": bool(raw.get("unread")),
        "is_muted": bool(raw.get("is_muted")),
    }


class FriendScanSession(threading.Thread):
    """一次"读全部会话"的会话。面板每 2 秒读一次 `snapshot()`。"""

    def __init__(self, profile_dir, *, fingerprint="", proxy=None,
                 timeout=DEFAULT_TIMEOUT_SECONDS, headless=True, owner=""):
        super().__init__(daemon=True, name="friend-scan")
        self.profile_dir = str(profile_dir)
        self.fingerprint = str(fingerprint or "")
        self.proxy = proxy or ""
        self.timeout = float(timeout)
        self.headless = bool(headless)
        self.owner = str(owner or "")      # 这次读的是哪个账号（面板用来把列表贴到对应行）

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._state = STATE_STARTING
        self._message = "正在准备"
        self._error = ""
        self._items = []
        self._scan = {}
        # 注意：**不能**叫 self._started —— 那是 threading.Thread 自己的 Event，
        # 覆盖掉它会让 Thread.start() 在 self._started.is_set() 上抛 AttributeError。
        self._t0 = time.time()
        self._updated = self._t0
        self._finished = 0.0

    # ------------------------------------------------------------ 面板读这些

    def snapshot(self):
        """给面板的只读快照（**不含**会话明细，明细走 `items()`）。"""
        with self._lock:
            state = self._state
            items = self._items
            scan = dict(self._scan)
            data = {
                "state": state,
                "state_label": STATE_LABELS.get(state, state),
                "message": self._message,
                "error": self._error,
                "count": len(items),
                "active": state in ACTIVE_STATES,
                "started_at": self._t0,
                "updated_at": self._updated,
                "finished_at": self._finished,
                "age_seconds": int(max(0.0, (self._finished or time.time()) - self._t0)),
                "profile_dir": self.profile_dir,
                "fingerprint": self.fingerprint,
                "owner": self.owner,
                "scanned_all": bool(scan.get("scanned_all")),
                "stopped": scan.get("stopped") or "",
                "visited": scan.get("visited"),
                "steps": scan.get("steps"),
                "note": scan.get("note") or "",
                "truncated": bool(scan.get("truncated")),
                "groups": sum(1 for it in items if it.get("is_group")),
            }
            return data

    def items(self):
        """读到的会话明细（副本）。"""
        with self._lock:
            return [dict(it) for it in self._items]

    def cancel(self):
        """请求停止。线程会在下一个检查点退出，浏览器随之关闭。

        **这里故意不置终态**：面板用 ``active`` 判断"资料目录有没有被占着"，
        浏览器要等 ``finally: context.close()`` 才真的松开。提前改成 cancelled
        会让同一个账号立刻能再起一个 Chromium 去抢同一个 user-data-dir。
        终态由 ``run()`` 在浏览器关掉之后补上。
        """
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
            self._cancel.set()
            self._message = "正在取消…"
            self._updated = time.time()
        return True

    def cancelled(self):
        return self._cancel.is_set()

    @property
    def active(self):
        with self._lock:
            return self._state in ACTIVE_STATES

    # ------------------------------------------------------------ 内部

    def _set(self, state, message="", **extra):
        with self._lock:
            self._state = state
            if message:
                self._message = message
            for key, value in extra.items():
                if key == "error":
                    self._error = str(value or "")
                elif key == "scan":
                    self._scan = dict(value or {})
            self._updated = time.time()
            if state in TERMINAL_STATES:
                self._finished = self._updated

    def _ready_error(self, res):
        """门禁终态 → 人话（与 configTool 的 `_ready_error` 同口径）。"""
        res = res or {}
        status = str(res.get("status") or "")
        if status == "LOGGED_OUT":
            return "这个账号还没登录，先在「扫码登录」里扫一次码"
        if status == "EXPIRED":
            return "登录态已失效（本地还存着 sessionid，但服务端已经不认了），重新扫一次码"
        if status == "LOGIN_LOST":
            return "读取过程中掉登录了，重新扫一次码"
        if status == "TIMEOUT":
            return "等会话列表就绪超时了，稍后重试（抖音偶尔会很慢）"
        return f"登录态检查没通过：{status or '未知状态'}"

    def run(self):
        context = None
        page = None
        cancelled = False
        # 终态**推迟到浏览器关掉之后**才置（见 finally 里的注释）。这里先攒着。
        final = None
        # ★ `browser_mod` 必须在 try **外面**绑定：finally 里要用它关浏览器，而
        #   import 本身失败（顶层会拉 utils.config/logger，`.env` 解析异常即可触发）
        #   时 finally 会抛 `NameError` —— 原始异常被掩盖、**终态永不落地**，
        #   会话停在 active、账号一直"忙"到 1200 秒的 reaper 才放行。
        #   `core/qr_login.py::_close` 踩过同一个坑（2026-10-10 那次事故）。
        import core.browser as browser_mod
        try:
            import core.douyin_im as douyin_im

            self._set(STATE_OPENING, "正在打开浏览器（用这个账号自己的配置）")
            proxy = self.proxy or (browser_mod.get_config().get("proxyAddress") or "")
            context = browser_mod.launch_persistent_context(
                self.profile_dir, fingerprint=self.fingerprint or None,
                proxy=proxy, headless=self.headless)
            context.set_default_navigation_timeout(180_000)
            pages = getattr(context, "pages", None) or []
            page = pages[0] if pages else context.new_page()
            if self.cancelled():
                raise _Cancelled()

            self._set(STATE_READY, "正在检查这个账号的登录态")
            im = douyin_im.DouyinIM(
                page, timeout=self.timeout, max_steps=MAX_STEPS,
                settle_ms=SETTLE_MS, ready_timeout=READY_TIMEOUT_SECONDS)
            res = im.wait_ready() or {}
            if str(res.get("status") or "") != douyin_im.STATUS_READY:
                final = (STATE_ERROR, self._ready_error(res),
                         {"error": f"gate={res.get('status') or 'UNKNOWN'}"})
                return
            if self.cancelled():
                raise _Cancelled()

            self._set(STATE_SCANNING, "正在滚动读取会话列表…")
            items = []
            seen = set()
            for raw in im.iter_conversations():
                if self.cancelled():
                    raise _Cancelled()
                item = public_item(raw)
                if item is None:
                    continue
                key = item["conv_id"] or ("name:" + item["name"])
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
                if len(items) % 10 == 0:
                    self._set(STATE_SCANNING, f"已读到 {len(items)} 个会话…")
                if len(items) >= MAX_ITEMS:
                    self._scan = dict(im.last_scan or {})
                    self._scan["truncated"] = True
                    break

            scan = dict(im.last_scan or {})
            with self._lock:
                self._items = items
            summary = dict(scan)
            summary["truncated"] = bool(scan.get("truncated")) or len(items) >= MAX_ITEMS
            final = (STATE_DONE,
                     f"读到 {len(items)} 个会话"
                     + ("" if summary.get("scanned_all") else "（没扫到底，可能还有更多）"),
                     {"scan": summary})
        except _Cancelled:
            cancelled = True
        except Exception as exc:                              # noqa: BLE001 —— 线程里必须兜住
            final = (STATE_ERROR, f"读取失败：{type(exc).__name__}: {exc}",
                     {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            if context is not None:
                # 关浏览器的同时把 playwright 句柄还回去；close_context 自己吞异常
                # 并记日志，返回是否关干净（这里不关心：终态下面才置）。
                # 再包一层 try：收尾出错也**必须**走到下面置终态 —— 否则面板会
                # 一直以为这个账号在忙。
                try:
                    browser_mod.close_context(context)
                except Exception as exc:                     # noqa: BLE001
                    logger.warning(f"[SCAN] 关浏览器时出错（继续置终态）："
                                   f"{type(exc).__name__}: {exc}")
            # ★ 终态必须在浏览器真的关掉**之后**才置：面板用 `active` 判断资料目录
            # 有没有被占着（见 `cancel()` 的注释与 webui 的 `_profile_in_use`）。
            # 以前 STATE_DONE/STATE_ERROR 是在 try 体里置的、早于这句 close，
            # 于是"扫完了"和"目录已经空出来"之间有一个窗口 —— 用户在这个窗口里点
            # 扫码登录，第二个 Chromium 就会去抢同一个 user-data-dir（SingletonLock），
            # 谁也开不起来，而面板看起来一切正常。
            if cancelled:
                self._set(STATE_CANCELLED, "已取消")
            elif final is not None:
                self._set(final[0], final[1], **final[2])


class _Cancelled(Exception):
    """内部信号：用户点了取消。"""


def start_session(profile_dir, *, fingerprint="", proxy=None,
                  timeout=DEFAULT_TIMEOUT_SECONDS, headless=True, owner=""):
    """建好并启动一次扫描；调用方**必须**保存返回的引用（否则线程会被 GC 掉）。"""
    session = FriendScanSession(profile_dir, fingerprint=fingerprint, proxy=proxy,
                                timeout=timeout, headless=headless, owner=owner)
    session.start()
    return session
