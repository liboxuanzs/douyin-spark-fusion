"""面板扫码登录：在无头 Chromium 里打开抖音的登录二维码，扫码后取回 Cookie。

为什么是「无头 + 转发 base64 图片」而不是 B 那套 noVNC 桌面：
  - B 的做法要在服务器上装 Xvfb + x11vnc + websockify + noVNC，把整个桌面流给浏览器；
    我们只要一张 178×178 的二维码，实测抖音的登录码就是个 `data:image/png;base64,…`
    的 `<img>`，直接把它转发给面板即可 —— 少四五个系统依赖，也少一个长连接通道。
  - 无头模式与真实发送用的是同一个浏览器二进制与同一套反检测启动参数，
    指纹还能按账号钉死（`--fingerprint=`），扫出来的 cookie 与跑任务时是同一个人。

线程模型与 `configTool/browser_login.py` 一样：cloakbrowser 底层是 Playwright 的
**同步** API，有线程亲和性，所有浏览器调用必须发生在同一个线程里。所以这里是一个
`threading.Thread`，外部只看 `snapshot()`（加锁的只读快照）与 `qr_png()`。

状态机（`state` 字段）：
    starting → waiting_qr → waiting_scan → verifying → done
    任意阶段出错 → error；被用户取消 → cancelled
"""
from __future__ import annotations

import os
import shutil
import threading
import time

from utils.config import get_config
from utils.logger import setup_logger
from core import cookie_utils
from core import douyin_im

# 本模块的日志器。名字固定 "qr_login"，与 core.tasks 的 "app"、core.douyin_im 的分开，
# 便于按 logger 名过滤；级别同样取自 .env 的 LOG_LEVEL。
#
# 这条链路以前一行日志都不打：扫码失败时除了面板上那句「300 秒超时」什么都没有，
# 事后分不清是用户没扫、页面停摆还是服务端不认。登录是整条链路的入口，值得留痕。
logger = setup_logger("qr_login", level=get_config().get("logLevel", "Info"))

# 登录页：抖音首页自带登录面板（`#login-panel-new`），点一下「登录」就出二维码
LOGIN_URL = "https://www.douyin.com/"

# 打开页面愿意等多久（毫秒）。机房链路抖动大，给宽一些。
GOTO_TIMEOUT_MS = 180_000

# 一次扫码会话的总预算（秒）。二维码本身也会过期，超时就让用户重开一次。
DEFAULT_TTL_SECONDS = 300.0

# 轮询间隔（毫秒）。**必须用 Playwright 自己的等待**（page.wait_for_timeout）：
# 用 time.sleep 会挡住事件泵，page.on("response") 的回调永远不执行。
POLL_INTERVAL_MS = 700

# 扫码之后等服务端确认登录态的上限（秒）
VERIFY_TIMEOUT_SECONDS = 60.0

# 账号资料接口的等待（秒）：先原地等，再考虑扫 localStorage 兜底
PROFILE_GRACE_SECONDS = 6.0

# 二维码多久没换新就判定「页面停摆」（秒）。
# 抖音的登录码实测 ~60 秒就过期换新（qr_seq 每 60 秒左右自增一次，换出来的
# 短链每次都不一样）。所以只要超过这个时间一格没动，就说明页面的取码轮询
# 已经不转了 —— 面板上那张码早就作废，用户扫它不可能有反应，
# 继续干等到 TTL 只会白等一场。
QR_STALL_SECONDS = 90.0

# 一次会话里最多自愈几次（重新载入登录页、重新取码）。自愈成功就重新计时。
QR_MAX_RECOVERIES = 3
# ---- 二次验证（风控账号：扫码后还要填短信验证码）----
#
#: 扫上码之后，至少隔多久才去瞄一眼有没有验证页（给页面留出跳转时间）。
CODE_DETECT_SECONDS = 8.0
#: 等用户把验证码填进面板的最长时间（秒）；超时按失败处理，会话不挂死。
CODE_WAIT_SECONDS = 300.0
#: 填完验证码之后，等登录 cookie 出现的最长时间（秒）。
CODE_VERIFY_SECONDS = 60.0
#: 验证码允许填错的次数（短信次数有限，别猛试）。
CODE_MAX_ATTEMPTS = 3
#: 面板截图 / 页面探测的最小间隔（秒）：验证码页要让人看得见，但别把 CPU 拍满。
SHOT_INTERVAL_SECONDS = 2.0

# 浏览器里已经存着登录态时，直接用它（不再要求扫码）
STATE_STARTING = "starting"
STATE_WAITING_QR = "waiting_qr"
STATE_WAITING_SCAN = "waiting_scan"
STATE_VERIFYING = "verifying"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"
# 扫码之后抖音还要过一道「身份验证」（风控账号）：两条路，①接收短信验证码
# ②发送指定短信到指定号码。本项目一律走①，于是"扫码"与"确认登录态"之间多了
# 这个状态 —— 会话停在这里，等用户在面板里把手机上收到的验证码填进来。
STATE_WAITING_CODE = "waiting_code"

ACTIVE_STATES = (STATE_STARTING, STATE_WAITING_QR, STATE_WAITING_SCAN,
                 STATE_VERIFYING, STATE_WAITING_CODE)

STATE_LABELS = {
    STATE_STARTING: "正在启动浏览器",
    STATE_WAITING_QR: "正在获取二维码",
    STATE_WAITING_SCAN: "等待扫码",
    STATE_VERIFYING: "已扫码，正在确认登录态",
    STATE_WAITING_CODE: "需要短信验证码",
    STATE_DONE: "登录成功",
    STATE_ERROR: "登录失败",
    STATE_CANCELLED: "已取消",
}

# 登录面板上的提示词 → 给用户的进度话术。顺序有意义：先匹配到的先用。
SCAN_MARKERS = (
    ("请在手机上确认", "已扫码，请在手机上点一下确认"),
    ("扫描成功", "已扫码，请在手机上点一下确认"),
    ("已扫码", "已扫码，请在手机上点一下确认"),
    ("确认登录", "已扫码，请在手机上点一下确认"),
    ("二维码已失效", "二维码已过期，正在刷新"),
    ("二维码失效", "二维码已过期，正在刷新"),
    ("点击刷新", "二维码已过期，正在刷新"),
)

# 「用户已经扫上了、正在手机上确认」这一类话术。停摆看门狗必须放过它们：
# 这时候重开页面会把用户正在做的确认打断。
#
# 反过来，**「二维码已过期，正在刷新」不在这个名单里** —— 页面挂着这句话却
# 又刷不出新码，恰恰就是页面停摆的现场，看门狗这时候最该出手。
# （所以判据不能简单写成「hint 非空」，那会把这个案子整个漏掉。）
CONFIRMING_HINTS = ("已扫码，请在手机上点一下确认",)

# ---- 二次验证页的判据与按钮文案 ----
# 「这是验证码页」最硬的信号：可见输入框自己的 placeholder / name / id 里带这些词。
CODE_INPUT_HINTS = ("验证码", "校验码", "动态码")
# 兜底信号：页面正文里出现这些词，且页面上至少有输入框。
CODE_PAGE_WORDS = ("身份验证", "安全验证", "接收短信验证码", "短信验证码",
                   "获取验证码", "请输入验证码", "验证码已发送")
# 「收验证码」这条路要点的按钮（顺序有意义：先精确、后宽松）。
CODE_RECEIVE_TEXTS = ("接收短信验证码", "接收验证码", "收验证码", "短信验证码",
                      "获取验证码", "点击获取", "获取短信")
# 「发指定短信到指定号码」那条路一律不点：本项目不走它。
CODE_SEND_TEXTS = ("发送短信", "发短信", "指定号码", "向指定", "语音通话", "打电话")
# 提交按钮那一轮要避开的词：这些是"再要一条码"，点了会把刚填的冲掉。
CODE_SUBMIT_AVOID = ("获取", "重新获取", "发送", "接收", "切换", "语音", "返回")
# 提交按钮的候选文案。
CODE_SUBMIT_TEXTS = ("验证", "确认", "确定", "提交", "下一步", "登录", "完成")
# 页面上出现这些词，说明刚才那条码没过。
CODE_ERROR_WORDS = ("验证码错误", "验证码不正确", "验证码有误", "验证码已失效",
                    "验证码过期", "验证码已过期", "请重新获取", "操作频繁",
                    "尝试次数过多", "验证失败")

# 真正"能点"的元素。提交那一步只认这些：页面上一个写着「身份验证」的段落也能被
# 子串匹配上「验证」，点它等于没点，还会把"找不到按钮就回车"那条兜底吃掉。
# （2026-10-07 真 Chromium 合成 DOM 探针实测：不限定标签时 _submit_code 会去点
#  <p>身份验证</p>，验证码根本没提交出去。）
CLICKABLE_TAGS = ("button", "a", "label", "input[type=submit]", "input[type=button]",
                  "[role=button]", "[role=link]")

# ---------------------------------------------------------------------------
# 页面侧脚本
# ---------------------------------------------------------------------------
# 取二维码：抖音的登录码是 <img src="data:image/png;base64,…">，实测 178×178。
# 只认「data: URI + 接近正方形 + 足够大」，避免把 22×22 的小图标当成二维码。
PICK_QR_JS = r"""
() => {
  const cands = [];
  document.querySelectorAll('img').forEach((im) => {
    const s = im.src || '';
    if (s.slice(0, 11) !== 'data:image/') return;
    const r = im.getBoundingClientRect();
    if (r.width < 120 || r.height < 120) return;
    if (Math.abs(r.width - r.height) > 16) return;
    cands.push({uri: s, w: Math.round(r.width), h: Math.round(r.height)});
  });
  cands.sort((a, b) => b.w - a.w);
  return cands[0] || null;
}
"""

# 点开登录面板：首页右上角那个「登录」。只点可见、尺寸合理、文案完全相等的元素。
CLICK_LOGIN_JS = r"""
() => {
  const want = ['登录', '立即登录', '登录/注册'];
  const nodes = Array.from(document.querySelectorAll('button, div, span, a'));
  for (const n of nodes) {
    const t = (n.textContent || '').trim();
    if (want.indexOf(t) < 0) continue;
    const r = n.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.width > 260 || r.height > 90) continue;
    n.click();
    return {clicked: t};
  }
  return {clicked: null};
}
"""

# 登录面板上的文字 → 判断「已扫码待确认」还是「二维码过期」
PANEL_HINT_JS = r"""
() => {
  const el = document.querySelector('#login-panel-new') || document.body;
  const t = ((el && el.innerText) || '').replace(/\s+/g, '');
  const marks = ['请在手机上确认', '扫描成功', '已扫码', '确认登录',
                 '二维码已失效', '二维码失效', '点击刷新'];
  return marks.filter((m) => t.indexOf(m) >= 0);
}
"""


# ---------------------------------------------------------------------------
# 二次验证页（风控账号）：探页面、点按钮、填验证码
# ---------------------------------------------------------------------------
CODE_PROBE_JS = r"""
() => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const inputs = [];
  document.querySelectorAll('input, textarea').forEach((el) => {
    if (!vis(el)) return;
    inputs.push({
      type: String(el.type || 'text').toLowerCase(),
      placeholder: String(el.placeholder || ''),
      name: String(el.name || ''),
      id: String(el.id || ''),
      label: String(el.getAttribute('aria-label') || ''),
      maxlength: Number(el.maxLength || 0),
      filled: String(el.value || '').length > 0,
    });
  });
  const buttons = [];
  document.querySelectorAll('button, a, div, span, p, label').forEach((el) => {
    if (!vis(el) || el.children.length > 1) return;
    const t = txt(el);
    if (t && t.length <= 24) buttons.push(t);
  });
  return {
    url: String(location.href || ''),
    text: (document.body ? txt(document.body) : '').slice(0, 1500),
    inputs: inputs.slice(0, 12),
    buttons: Array.from(new Set(buttons)).slice(0, 40),
    hasQr: !!document.querySelector('img[src^="data:image/png;base64,"]'),
  };
}
"""

CLICK_TEXT_JS = r"""
(payload) => {
  const want = (payload && payload.texts) || [];
  const avoid = (payload && payload.avoid) || [];
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const bad = (t) => avoid.some((w) => t.indexOf(w) >= 0);
  const rank = (el) => {
    const tag = String(el.tagName || '').toLowerCase();
    if (tag === 'button') return 0;
    if (tag === 'a') return 1;
    if (tag === 'label') return 2;
    return 3;
  };
  const tags = (payload && payload.tags) || [];
  const pool = tags.length ? tags.join(',') : 'button, a, div, span, p, label';
  const cands = [];
  document.querySelectorAll(pool).forEach((el) => {
    if (!vis(el) || el.children.length > 1) return;
    const t = txt(el);
    if (!t || t.length > 24 || bad(t)) return;
    cands.push([el, t, rank(el)]);
  });
  cands.sort((a, b) => a[2] - b[2]);
  for (const word of want) {
    for (const item of cands) {
      if (item[1].indexOf(word) >= 0) {
        try { item[0].scrollIntoView({block: 'center'}); } catch (e) { /* 老浏览器就算了 */ }
        item[0].click();
        return {ok: true, clicked: item[1]};
      }
    }
  }
  return {ok: false, clicked: '', seen: cands.slice(0, 20).map((c) => c[1])};
}
"""

FILL_CODE_JS = r"""
(code) => {
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const hint = (el) => [el.placeholder, el.name, el.id, el.getAttribute('aria-label')]
    .map((x) => String(x || '')).join(' ');
  const usable = (el) => {
    const t = String(el.type || 'text').toLowerCase();
    if (['text', 'tel', 'number', 'search', ''].indexOf(t) < 0) return false;
    return !el.disabled && !el.readOnly && vis(el);
  };
  const score = (el) => {
    const h = hint(el);
    let n = 0;
    if (h.indexOf('验证码') >= 0 || h.indexOf('校验码') >= 0 || h.indexOf('动态码') >= 0) n += 100;
    const max = Number(el.maxLength || 0);
    if (max >= 4 && max <= 8) n += 40;
    const t = String(el.type || '').toLowerCase();
    if (t === 'tel' || t === 'number') n += 20;
    if (h.indexOf('手机') >= 0 || h.indexOf('密码') >= 0 || h.indexOf('账号') >= 0) n -= 40;
    return n;
  };
  const all = Array.from(document.querySelectorAll('input')).filter(usable);
  if (!all.length) return {ok: false, error: '页面上找不到可以填验证码的输入框'};
  all.sort((a, b) => score(b) - score(a));
  const el = all[0];
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  el.focus();
  setter.call(el, String(code));
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, placeholder: String(el.placeholder || ''), score: score(el)};
}
"""


def _launch_context(profile_dir, *, args, proxy):
    """启动**持久化**上下文（实现在 `core.browser.launch_persistent_context`）。

    用持久化上下文而不是临时 profile：同一台服务器上反复扫码时，浏览器带着
    固定的指纹与设备信息，风控看到的是「同一个设备重新登录」，而不是「一台新设备」。
    """
    import core.browser as browser_mod
    return browser_mod.launch_persistent_context(
        profile_dir, args=args, proxy=proxy, headless=True)


class QrLoginSession(threading.Thread):
    """一次扫码登录会话。只读快照给面板用，cookie 交给 on_success 回调落盘。"""

    def __init__(self, profile_dir, *, fingerprint="", proxy=None,
                 ttl=DEFAULT_TTL_SECONDS, on_success=None, headless=True):
        super().__init__(name="qr-login", daemon=True)
        self.profile_dir = str(profile_dir)
        self.fingerprint = str(fingerprint or "")
        self.proxy = proxy or None
        self.ttl = float(ttl or DEFAULT_TTL_SECONDS)
        self.on_success = on_success
        self.headless = bool(headless)

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._state = STATE_STARTING
        self._message = "正在启动隐身浏览器…"
        self._hint = ""
        self._error = ""
        self._qr_png = b""
        self._qr_seq = 0
        self._qr_read_fails = 0     # 读二维码时页面 evaluate 抛错的次数（排障用）
        self._qr_read_error = ""
        self._started_at = time.time()
        self._updated_at = self._started_at
        self._finished_at = 0.0
        self.result = None          # 成功后：cookies / 账号资料 / 诊断信息
        self.persist = None         # on_success 的返回值（写了哪个键、备份在哪）
        self.browser = ""           # 实际用上的后端名（排障用）
        self._adopt = ""            # 登录成功后要把资料目录搬到的位置（浏览器关了才搬）
        # 正在收尾（终态已置位、浏览器还没关完）。`snapshot()` 用它把 active 一直
        # 顶到浏览器真的关掉为止 —— 见 `_run` 的 finally。
        self._closing = False
        self.moved_to = ""          # 真搬成了的话，这里是新目录
        # ---- 二次验证（风控账号：扫码后还要填短信验证码）----
        self._scanned = False       # 页面上出现过「已扫码」类提示
        self._scanned_at = 0.0
        self._code_value = ""       # 面板送进来的验证码（会话线程取走就清空）
        self._code_event = threading.Event()
        self._code_tries = 0        # 已经提交过几次
        self._code_error = ""       # 上一次为什么没成（面板显示）
        self._code_options = []     # 验证码页上认出来的按钮（展示 / 排障）
        self._last_probe_at = 0.0   # 页面探测节流
        self._shot_png = b""        # 验证码页截图（面板显示）
        self._shot_seq = 0
        self._shot_at = 0.0

    # ---- 只读快照 ----
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "state_label": STATE_LABELS.get(self._state, self._state),
                "message": self._message,
                "hint": self._hint,
                "error": self._error,
                "qr_seq": self._qr_seq,
                "has_qr": bool(self._qr_png),
                # 收尾期间也算 active：终态一置位门就开了的话，浏览器还占着
                # 资料目录，后起的会话会跟它抢同一个 user-data-dir。
                "active": self._state in ACTIVE_STATES or self._closing,
                "started_at": self._started_at,
                "updated_at": self._updated_at,
                "finished_at": self._finished_at,
                "age_seconds": int(max(0.0, (self._finished_at or time.time()) - self._started_at)),
                "account": (self.result or {}).get("account") if self.result else None,
                "profile_dir": self.profile_dir,
                # 登录成功后要搬去的目录（浏览器关了才搬）。面板靠它判断
                # "搬迁途中"这个资料目录也不能被别人抢走。
                "adopt_dir": self._adopt,
                "persist": self.persist,
                # ---- 二次验证（面板靠 need_code 把验证码输入框亮出来）----
                "need_code": self._state == STATE_WAITING_CODE,
                "code_tries": self._code_tries,
                "code_max_attempts": CODE_MAX_ATTEMPTS,
                "code_error": self._code_error,
                "code_options": list(self._code_options),
                "has_shot": bool(self._shot_png),
                "shot_seq": self._shot_seq,
            }

    def qr_png(self) -> bytes:
        with self._lock:
            return self._qr_png

    def cancel(self) -> None:
        self._cancel.set()
        self.message = "已请求取消，正在关闭浏览器…"

    def shot_png(self) -> bytes:
        """验证码页的截图（面板显示用；没有就返回 b""）。"""
        with self._lock:
            return self._shot_png

    def submit_code(self, code: str) -> dict:
        """面板把用户填的短信验证码送进来。

        这里只做「收下 + 叫醒等待中的线程」：真正往页面里填、点提交都在会话
        线程里做 —— Playwright 的 page 不能跨线程用。
        """
        text = "".join(str(code or "").split())
        if not text:
            return {"ok": False, "error": "验证码是空的"}
        if len(text) > 12 or not text.isdigit():
            return {"ok": False, "error": "验证码应该是一串数字，请照短信原文填"}
        with self._lock:
            if self._state != STATE_WAITING_CODE:
                return {"ok": False, "error": "现在不是在等验证码（这一页已经走过去了）"}
            self._code_value = text
            self._code_error = ""
            self._updated_at = time.time()
            self._code_event.set()
        return {"ok": True}

    # ---- 内部：状态推进 ----
    def _set(self, state: str, message: str = "", **extra) -> None:
        with self._lock:
            self._state = state
            if message:
                self._message = message
            for key, value in extra.items():
                setattr(self, f"_{key}", value)
            self._updated_at = time.time()
            if state not in ACTIVE_STATES:
                self._finished_at = self._updated_at

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    @message.setter
    def message(self, text: str) -> None:
        with self._lock:
            self._message = str(text)
            self._updated_at = time.time()

    def _set_qr(self, png: bytes) -> bool:
        """换上新二维码。返回「是否真的换了一张」—— 自愈看门狗靠它计时。"""
        with self._lock:
            if png and png != self._qr_png:
                self._qr_png = png
                self._qr_seq += 1
                self._updated_at = time.time()
                return True
            return False

    def _set_shot(self, png: bytes) -> bool:
        """换上新截图（验证码页要让人看得见）。返回「是否真的换了一张」。"""
        with self._lock:
            if png and png != self._shot_png:
                self._shot_png = png
                self._shot_seq += 1
                self._updated_at = time.time()
                return True
            return False

    # ---- 主循环 ----
    def run(self) -> None:                                       # noqa: C901 —— 线性流程，读起来更顺
        ctx = None
        try:
            import core.browser as browser_mod
            args = browser_mod.chrome_args(self.fingerprint or None)
            # 代理与任务链路取同一份配置：代理开着的时候，登录出口必须和发送出口一致，
            # 否则拿本机 IP 换来的 cookie 拿到云端去用，等于换了地区，风控一眼就看出来。
            proxy = self.proxy or (browser_mod.get_config().get("proxyAddress") or "")
            self._set(STATE_STARTING, "正在启动隐身浏览器…")
            os.makedirs(self.profile_dir, exist_ok=True)
            ctx = _launch_context(self.profile_dir, args=args, proxy=proxy)
            self.browser = "cloakbrowser" if not self._cancel.is_set() else ""
            try:
                ctx.set_default_navigation_timeout(GOTO_TIMEOUT_MS)
            except Exception:                                    # noqa: BLE001
                pass

            pages = [p for p in ctx.pages if not p.is_closed()]
            page = pages[0] if pages else ctx.new_page()
            self.page = page

            # 两个钩子都必须早于第一次 goto：
            #   ImMonitor            登录态写在聊天页首屏的 SSR 里，挂晚了就漏
            #   add_init_script      抖音自己会在页面加载时请求账号资料接口
            mon = douyin_im.ImMonitor(page)
            try:
                page.add_init_script(cookie_utils.INTERCEPT_JS)
            except Exception as exc:                             # noqa: BLE001
                self.message = f"账号资料钩子注入失败（不影响登录）：{type(exc).__name__}: {exc}"

            # ① 浏览器里可能已经存着登录态（同一个 profile 反复扫码时），先探一次
            if cookie_utils.has_login_cookie(self._cookies_raw(ctx)):
                self._set(STATE_VERIFYING, "浏览器里已有登录态，正在确认…")
                if self._verify(page, mon):
                    return self._finish(ctx, page, mon)
                self.message = "本地那份登录态服务端不认了，请重新扫码"

            # ② 打开登录页并等二维码
            self._set(STATE_STARTING, "正在打开抖音登录页…")
            try:
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            except Exception as exc:                             # noqa: BLE001
                # 超时 ≠ 导航失败：Playwright 只是不再等，请求仍在继续
                self.message = f"首屏加载等待超时，继续等页面就绪：{' '.join(str(exc).split())[:160]}"

            qr = self._wait_for_qr(page)
            if self._cancel.is_set():
                return self._cancel_now("已取消")
            if not qr:
                self._set(STATE_ERROR, "没能拿到登录二维码（页面结构可能变了）",
                          error="no_qrcode")
                return
            self._set_qr(qr)
            logger.info(f"[QR] 二维码已就绪 profile={self.profile_dir} bytes={len(qr)}")
            self._set(STATE_WAITING_SCAN, "请用抖音 App 扫描二维码，并在手机上确认登录")

            # ③ 等扫码：同时盯着二维码刷新与登录态
            #
            # 这里带一个「停摆看门狗」。抖音的码实测 ~60 秒换一张，健康的页面会自己
            # 把新码画出来，我们跟着读数就行；但页面卡住（JS 死了 / 反复跳转 /
            # evaluate 一直抛）时 qr_seq 会永远停在 1，面板端着一张死码不动 ——
            # 用户扫它自然没反应，然后干等到 TTL 报「你还没扫上」，完全误导。
            # 所以只要超过 QR_STALL_SECONDS 没读到新码，就整页重开、重新取码。
            deadline = time.time() + self.ttl
            last_qr_at = time.time()
            recoveries = 0
            need_code = False           # 探测到「身份验证」页就置位（见循环尾）
            while time.time() < deadline:
                if self._cancel.is_set():
                    return self._cancel_now("已取消")
                png = self._read_qr(page)
                if self._set_qr(png):
                    last_qr_at = time.time()
                hint = self._panel_hint(page)
                if hint:
                    with self._lock:
                        self._hint = hint
                        self._message = hint
                        self._updated_at = time.time()
                if hint in CONFIRMING_HINTS and not self._scanned:
                    # 手机上已经点了确认：从此二维码不再刷新是正常的（页面已经
                    # 走到下一步去了）。看门狗再"自愈"只会把验证码页整页刷掉。
                    self._scanned = True
                    self._scanned_at = time.time()
                    last_qr_at = time.time()
                    logger.info("[QR] 手机上已确认扫码，等页面进入下一步")
                if cookie_utils.has_login_cookie(self._cookies_raw(ctx)):
                    logger.info("[QR] 浏览器里出现了登录 cookie，去服务端确认")
                    break
                # hint 是「已扫码」那类话术时说明手机上正在确认，重开页面会打断用户。
                # 注意判据只看这一类，不看「二维码已过期」—— 那个恰恰是停摆现场。
                if (hint not in CONFIRMING_HINTS and not self._scanned
                        and time.time() - last_qr_at > QR_STALL_SECONDS):
                    if recoveries >= QR_MAX_RECOVERIES:
                        logger.error(
                            f"[QR] 二维码连续 {int(QR_STALL_SECONDS)} 秒没刷新，"
                            f"自愈 {recoveries} 次仍未恢复（qr_seq={self._qr_seq}，"
                            f"evaluate 失败 {self._qr_read_fails} 次"
                            + (f"，最后一次：{self._qr_read_error}" if self._qr_read_error else "")
                            + "）")
                        self._set(STATE_ERROR,
                                  "二维码卡住了：页面停止刷新，这张码已经作废。"
                                  "请点「开始扫码登录」重来一次",
                                  error="qr_stalled")
                        return
                    recoveries += 1
                    logger.warning(
                        f"[QR] 二维码 {int(QR_STALL_SECONDS)} 秒没刷新"
                        f"（自愈 {recoveries}/{QR_MAX_RECOVERIES}）：qr_seq={self._qr_seq} "
                        f"url={page.url[:100]} evaluate 失败 {self._qr_read_fails} 次，"
                        "重新载入登录页")
                    self._set(STATE_WAITING_QR, "二维码卡住了，正在自动重开登录页…")
                    if self._reopen_login_page(page):
                        last_qr_at = time.time()
                        self._set(STATE_WAITING_SCAN, "请用抖音 App 扫描二维码，并在手机上确认登录")
                    else:
                        logger.warning("[QR] 自愈后仍没拿到二维码，继续等下一轮")
                    continue
                # 扫完码之后抖音可能甩出一道「身份验证」（风控账号）。探测到就
                # 跳出扫码循环，交给 `_solve_code` 去收短信验证码。
                if (self._scanned or time.time() - last_qr_at > CODE_DETECT_SECONDS) \
                        and self._detect_code_page(page):
                    need_code = True
                    break
                page.wait_for_timeout(POLL_INTERVAL_MS)
            else:
                self._set(STATE_ERROR,
                          f"等了 {int(self.ttl)} 秒还没扫上，二维码已作废，请重开一次"
                          + (f"（期间自动重开过 {recoveries} 次）" if recoveries else ""),
                          error="timeout")
                return

            # ③.5 二次验证：风控账号扫完码还有一道「身份验证」。收码、填码、等放行，
            #      拿到登录 cookie 之后再落到 ④ 让服务端点头。
            if need_code and not self._solve_code(ctx, page):
                return

            # ④ 服务端确认 + 抓账号资料与 cookie
            self._set(STATE_VERIFYING, "已扫码，正在向服务端确认登录态…")
            if not self._verify(page, mon):
                logger.warning(f"[QR] 扫码后服务端仍不认这份登录态：{self._verdict}")
                self._set(STATE_ERROR, "扫码后服务端仍未认这份登录态，请重试",
                          error="verify_failed")
                return
            self._finish(ctx, page, mon)
        except Exception as exc:                                 # noqa: BLE001 —— 线程里不能让异常逃逸
            import traceback
            traceback.print_exc()
            logger.error(f"[QR] 扫码登录异常退出：{type(exc).__name__}: {exc}")
            self._set(STATE_ERROR, f"{type(exc).__name__}: {exc}", error=str(exc))
        finally:
            # ★ 先立「正在收尾」的牌子再关浏览器：`_finish` 早把状态推到终态了，
            #   而 `snapshot()` 的 active 只看状态 —— 牌子不立，`webui.app.
            #   _profile_in_use` 那道「资料目录被占用」的门在 Chromium 还开着的
            #   时候就开了，随后起飞的扫码会话 / 读好友列表会跟它抢同一个
            #   user-data-dir（两处都用 `login_profile_dir()`），报出来的却像是
            #   莫名其妙的单例锁错误。`core/friend_scan.py` 是同一个道理，那边
            #   的写法是"先关浏览器、再置终态"。
            self._closing = True
            try:
                if self._close(ctx):
                    self._adopt_profile()
                else:
                    # 关不干净就别搬目录：目录还被活着的那只 Chromium 占着，改名
                    # 只会留下僵尸进程和 SingletonLock，下一次登录更没救。
                    self._adopt_note({"ok": False,
                                      "error": "浏览器没关干净，资料目录这次先不搬（下次登录成功后重试）"})
            finally:
                self._closing = False

    # ---- 收尾：把资料目录搬到账号名下 ----
    def _adopt_profile(self) -> None:
        """浏览器关掉之后，把这次用的资料目录搬到账号名下（只搬一次，不动已有目录）。"""
        target = self._adopt
        if not target:
            return
        src = os.path.abspath(self.profile_dir)
        dst = os.path.abspath(target)
        if src == dst:
            return
        if not os.path.isdir(src):
            self._adopt_note({"ok": False, "error": f"资料目录不存在：{src}"})
            return
        if os.path.exists(dst):
            # 目标已经有了（老账号换设备重扫之类）：不覆盖，把已有那份留着更安全
            self._adopt_note({"ok": False, "error": f"目标目录已存在，未覆盖：{dst}"})
            return
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        except Exception as exc:                                 # noqa: BLE001
            self._adopt_note({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self.moved_to = dst
        self.profile_dir = dst
        self._adopt_note({"ok": True, "moved_to": dst})

    def _adopt_note(self, info: dict) -> None:
        """把搬动结果记进 persist（面板会显示），失败也不影响登录本身。"""
        with self._lock:
            base = dict(self.persist) if isinstance(self.persist, dict) else {}
            base["profile_moved"] = dict(info)
            self.persist = base

    # ---- 内部：页面操作 ----
    def _cookies_raw(self, ctx):
        try:
            return ctx.cookies()
        except Exception:                                        # noqa: BLE001
            return []

    def _read_qr(self, page):
        try:
            got = page.evaluate(PICK_QR_JS)
        except Exception as exc:                                 # noqa: BLE001
            # 页面正在跳转、执行上下文被销毁时 evaluate 会抛。以前这里静默吞掉，
            # 面板就会一直端着一张再也刷不新的死码 —— 看门狗正是为这种情况准备的。
            self._qr_read_fails += 1
            self._qr_read_error = f"{type(exc).__name__}: {' '.join(str(exc).split())[:120]}"
            return b""
        if not isinstance(got, dict):
            return b""
        return cookie_utils.data_uri_bytes(got.get("uri")) or b""

    def _wait_for_qr(self, page, timeout_seconds: float = 25.0):
        """先直接找；没有就点一次「登录」再等。返回二维码 PNG 字节。"""
        deadline = time.time() + timeout_seconds
        clicked = False
        while time.time() < deadline and not self._cancel.is_set():
            png = self._read_qr(page)
            if png:
                return png
            if not clicked and time.time() > deadline - timeout_seconds + 4:
                try:
                    page.evaluate(CLICK_LOGIN_JS)
                except Exception:                                # noqa: BLE001
                    pass
                clicked = True
            page.wait_for_timeout(POLL_INTERVAL_MS)
        return b""

    def _reopen_login_page(self, page) -> bool:
        """页面停摆时的自愈：整页重开一次，重新把二维码点出来。

        不做「点一下刷新」那种小动作：停摆多半意味着页面的取码轮询已经死了，
        只有重新载入才能拿回一个会自己换码的干净登录面板。
        """
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as exc:                                 # noqa: BLE001
            # 超时 ≠ 导航失败：Playwright 只是不再等，请求仍在继续
            logger.warning("[QR] 自愈时重新载入登录页超时，继续等页面就绪："
                           f"{' '.join(str(exc).split())[:160]}")
        fresh = self._wait_for_qr(page)
        if not fresh:
            return False
        return self._set_qr(fresh)

    def _panel_hint(self, page) -> str:
        try:
            marks = page.evaluate(PANEL_HINT_JS)
        except Exception:                                        # noqa: BLE001
            return ""
        for marker in marks or []:
            for key, text in SCAN_MARKERS:
                if key in marker:
                    return text
        return ""

    def _verify(self, page, mon) -> bool:
        """把聊天页打开，用主程序那套门禁判定登录态（服务端说了算）。"""
        try:
            page.goto(douyin_im.CHAT_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as exc:                                 # noqa: BLE001
            self.message = f"聊天页加载等待超时，继续判定：{' '.join(str(exc).split())[:120]}"
        deadline = time.time() + VERIFY_TIMEOUT_SECONDS
        verdict = {}
        while time.time() < deadline and not self._cancel.is_set():
            try:
                verdict = douyin_im.check_login(page, mon) or {}
            except Exception as exc:                             # noqa: BLE001
                verdict = {"state": f"ERROR: {type(exc).__name__}: {exc}"}
            if verdict.get("state") == "LOGGED_IN":
                self._verdict = verdict
                return True
            page.wait_for_timeout(POLL_INTERVAL_MS)
        self._verdict = verdict
        return False

    def _account_info(self, page) -> dict:
        """账号资料：页面钩子 → 原地再等一会儿 → localStorage 兜底。

        刷新页面的那条路（configTool 里有）在这里刻意不用：用户刚扫完码，
        刷新既打断视觉确认，也可能让还没落盘的登录态多绕一圈。
        """
        deadline = time.time() + PROFILE_GRACE_SECONDS
        while time.time() < deadline and not self._cancel.is_set():
            info = cookie_utils.detect_account_info(page)
            if info.get("unique_id") or info.get("nickname"):
                return info
            page.wait_for_timeout(500)
        info = cookie_utils.detect_account_info(page)
        if not (info.get("unique_id") or info.get("nickname")):
            try:
                got = page.evaluate(cookie_utils.LOCAL_STORAGE_JS)
            except Exception:                                    # noqa: BLE001
                got = None
            if isinstance(got, dict):
                info["nickname"] = info.get("nickname") or str(got.get("nickname") or "")
                info["unique_id"] = info.get("unique_id") or str(got.get("unique_id") or "")
                info["id_source"] = info.get("id_source") or str(got.get("source") or "localStorage")
        return info

    # ---- 二次验证：收短信验证码 ----
    def _detect_code_page(self, page) -> dict:
        """瞄一眼当前页面是不是抖音的「身份验证」页。是就返回页面探针，否则 {}。

        判据（按硬度排序）：①有可见输入框，且它的 placeholder / name / id /
        aria-label 里带「验证码」这类词 —— 这是最硬的信号；②页面正文里出现
        「身份验证」「安全验证」这类词，且页面上至少有输入框。
        还端着一张二维码、又没有任何"验证码"输入框时不算 —— 那是登录页本身。
        """
        now = time.time()
        if now - self._last_probe_at < SHOT_INTERVAL_SECONDS:
            return {}
        self._last_probe_at = now
        try:
            probe = page.evaluate(CODE_PROBE_JS)
        except Exception:                                        # noqa: BLE001
            return {}
        if not isinstance(probe, dict):
            return {}
        inputs = [item for item in (probe.get("inputs") or []) if isinstance(item, dict)]
        buttons = [str(x) for x in (probe.get("buttons") or [])]
        wants = [
            item for item in inputs
            if any(word in " ".join(str(item.get(field) or "")
                                    for field in ("placeholder", "name", "id", "label"))
                   for word in CODE_INPUT_HINTS)
        ]
        text = str(probe.get("text") or "")
        hit = bool(wants) or (bool(inputs) and any(word in text for word in CODE_PAGE_WORDS))
        if not hit or (probe.get("hasQr") and not wants):
            return {}
        with self._lock:
            self._code_options = buttons[:12]
        logger.info("[QR] 页面看起来是二次验证："
                    f"输入框={[i.get('placeholder') for i in inputs][:4]} 按钮={buttons[:8]}")
        return probe

    def _click_texts(self, page, texts, *, avoid=(), strict=False) -> str:
        """按文案点页面上的按钮。返回点中的文案；没点着返回 ""。

        `strict=True` 时只考虑真正能点的标签（`CLICKABLE_TAGS`）—— 提交那一步
        必须这样：页面上随便一段「身份验证」的正文都能被子串匹配上「验证」，
        点它没有任何作用，却会把"没找到按钮就回车"的兜底吃掉。
        """
        payload = {"texts": list(texts), "avoid": list(avoid)}
        if strict:
            payload["tags"] = list(CLICKABLE_TAGS)
        try:
            got = page.evaluate(CLICK_TEXT_JS, payload)
        except Exception as exc:                                 # noqa: BLE001
            logger.warning(f"[QR] 点击二次验证页元素失败：{type(exc).__name__}: {exc}")
            return ""
        if isinstance(got, dict) and got.get("ok"):
            clicked = str(got.get("clicked") or "")
            logger.info(f"[QR] 二次验证页点了「{clicked}」")
            return clicked
        logger.info(f"[QR] 二次验证页没找到要点的按钮（看到的是：{(got or {}).get('seen')}）")
        return ""

    def _shot(self, page) -> None:
        """给面板拍一张当前页面 —— 抖音在问什么，用户得能看见。"""
        now = time.time()
        if now - self._shot_at < SHOT_INTERVAL_SECONDS:
            return
        self._shot_at = now
        try:
            png = page.screenshot(type="png")
        except Exception:                                        # noqa: BLE001
            return
        if isinstance(png, (bytes, bytearray)) and png:
            self._set_shot(bytes(png))

    def _code_budget(self) -> float:
        """这次让用户填码的总预算（秒）。

        刻意**不**受会话 TTL 约束：TTL 管的是"二维码还有效多久"，用户扫上码
        之后拿手机、等短信、把码敲进面板要多久跟二维码没关系。次数上限
        （CODE_MAX_ATTEMPTS）才是防挂死的闸。
        """
        return float(CODE_WAIT_SECONDS)

    def _wait_for_code(self, ctx, page, timeout: float):
        """等面板把验证码送进来。

        返回验证码字符串；返回 "" 表示"还没填码就已经放行了"；None = 取消或超时。
        """
        # 先看看有没有"上一轮等的时候就送进来、还没来得及用"的码：用户完全可能
        # 在"正在等抖音放行"那几秒里又敲了一条，直接 clear() 会把它丢掉，
        # 然后这一轮又空等到超时。
        with self._lock:
            pending = self._code_value
            self._code_value = ""
        self._code_event.clear()
        if pending:
            return pending
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._cancel.is_set():
                return None
            if self._code_event.wait(POLL_INTERVAL_MS / 1000.0):
                self._code_event.clear()
                with self._lock:
                    code = self._code_value
                    self._code_value = ""
                if code:
                    return code
            self._shot(page)
            if cookie_utils.has_login_cookie(self._cookies_raw(ctx)):
                return ""
        return None

    def _fill_code(self, page, code: str) -> dict:
        """把验证码填进页面上的验证码输入框。"""
        try:
            got = page.evaluate(FILL_CODE_JS, str(code))
        except Exception as exc:                                 # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(got, dict) and got.get("ok"):
            logger.info(f"[QR] 验证码已填进页面（placeholder={got.get('placeholder')!r}）")
            return {"ok": True}
        return {"ok": False, "error": str((got or {}).get("error") or "页面里找不到验证码输入框")}

    def _submit_code(self, page) -> None:
        """点提交。找不到按钮就回车 —— 验证码输入框基本都在表单里。"""
        if self._click_texts(page, CODE_SUBMIT_TEXTS, avoid=CODE_SUBMIT_AVOID, strict=True):
            return
        try:
            page.keyboard.press("Enter")
            logger.info("[QR] 二次验证页没找到提交按钮，改用回车提交")
        except Exception as exc:                                 # noqa: BLE001
            logger.warning(f"[QR] 回车提交验证码失败：{type(exc).__name__}: {exc}")

    def _await_login(self, ctx, page, timeout: float = 0.0) -> bool:
        """填完码之后等登录 cookie 出现。"""
        wait = float(timeout or 0) or CODE_VERIFY_SECONDS
        deadline = time.time() + wait
        while time.time() < deadline and not self._cancel.is_set():
            if cookie_utils.has_login_cookie(self._cookies_raw(ctx)):
                return True
            self._shot(page)
            page.wait_for_timeout(POLL_INTERVAL_MS)
        return False

    def _page_complaint(self, page) -> str:
        """从页面上读一句"为什么没过"（验证码错误 / 操作频繁 …）。"""
        try:
            probe = page.evaluate(CODE_PROBE_JS)
        except Exception:                                        # noqa: BLE001
            return ""
        text = str(probe.get("text") or "") if isinstance(probe, dict) else ""
        for word in CODE_ERROR_WORDS:
            if word in text:
                return word
        return ""

    def _solve_code(self, ctx, page) -> bool:
        """二次验证总控：选「接收短信验证码」→ 等用户填码 → 填进去 → 等放行。

        返回 True = 浏览器里已经有登录 cookie（接着去 ④ 让服务端确认）；
        返回 False = 已经置好终态（超时 / 次数用完 / 取消），调用方直接 return。
        """
        logger.info("[QR] 进入二次验证：这个账号扫码后还要填短信验证码")
        self._set(STATE_WAITING_CODE,
                  "这个账号还要过一道身份验证：抖音给你发了一条短信验证码，"
                  "请填在下面的输入框里（本项目固定走「接收短信验证码」，"
                  "不会走「发指定短信到指定号码」那条路）")
        # 先只认真正能点的元素；抖音偶尔把这种选项做成 div，那就退回宽松模式。
        if not self._click_texts(page, CODE_RECEIVE_TEXTS, avoid=CODE_SEND_TEXTS, strict=True):
            self._click_texts(page, CODE_RECEIVE_TEXTS, avoid=CODE_SEND_TEXTS)
        self._shot(page)

        budget = self._code_budget()
        for attempt in range(1, CODE_MAX_ATTEMPTS + 1):
            with self._lock:
                self._code_tries = attempt - 1
                self._code_error = ""
            code = self._wait_for_code(ctx, page, budget)
            if code is None:
                if self._cancel.is_set():
                    return False
                logger.warning(f"[QR] 等了 {int(budget)} 秒没等到验证码，放弃这次登录")
                self._set(STATE_ERROR,
                          f"等了 {int(budget)} 秒没收到验证码，这次登录先作废。"
                          "重新点「开始扫码登录」可以再来一次",
                          error="code_timeout")
                return False
            if code == "":
                logger.info("[QR] 还没填验证码，浏览器里就已经有登录 cookie 了")
                return True
            with self._lock:
                self._code_tries = attempt
            filled = self._fill_code(page, code)
            if not filled.get("ok"):
                self._set(STATE_ERROR, f"没能把验证码填进页面：{filled.get('error')}",
                          error="code_fill")
                return False
            self._submit_code(page)
            self._set(STATE_WAITING_CODE, f"验证码已提交，正在等抖音放行（第 {attempt} 次）")
            if self._await_login(ctx, page):
                logger.info(f"[QR] 二次验证通过（第 {attempt} 次填码）")
                return True
            reason = self._page_complaint(page)
            with self._lock:
                self._code_error = (f"这次没通过：{reason}" if reason
                                    else "这次没通过（验证码可能不对或已过期），请再试一次")
            budget = min(budget, self._code_budget())
            if attempt < CODE_MAX_ATTEMPTS:
                self._set(STATE_WAITING_CODE, "刚才那条验证码没通过，请重新获取一条再填一次")
        logger.warning("[QR] 验证码连着几次没通过，放弃这次登录")
        self._set(STATE_ERROR,
                  f"验证码连续 {CODE_MAX_ATTEMPTS} 次没通过，这次登录先作废",
                  error="code_rejected")
        return False

    # ---- 收尾 ----
    def _cancel_now(self, message: str) -> None:
        self._set(STATE_CANCELLED, message)

    def _finish(self, ctx, page, mon) -> None:
        try:
            state = ctx.storage_state()
        except Exception as exc:                                 # noqa: BLE001
            logger.error(f"[QR] 读取浏览器 Cookie 失败：{type(exc).__name__}: {exc}")
            self._set(STATE_ERROR, f"读取浏览器 Cookie 失败：{type(exc).__name__}: {exc}",
                      error="storage_state")
            return

        raw = state.get("cookies", []) if isinstance(state, dict) else []
        cookies, stats = cookie_utils.filter_cookies(raw)
        if not cookies:
            logger.error("[QR] 登录态里没有可用的 Cookie（filter_cookies 全滤掉了）")
            self._set(STATE_ERROR, "登录态里没有可用的 Cookie", error="no_cookies")
            return

        account = self._account_info(page)
        verdict = getattr(self, "_verdict", {}) or {}
        if not account.get("nickname"):
            account["nickname"] = verdict.get("nickname") or ""
        try:
            user_agent = page.evaluate("() => navigator.userAgent")
        except Exception:                                        # noqa: BLE001
            user_agent = ""

        result = {
            "cookies": cookies,
            "cookies_json": cookie_utils.cookies_to_json(cookies),
            "cookie_names": cookie_utils.cookie_names(cookies),
            "logged_in": cookie_utils.has_login_cookie(cookies),
            "login_state": verdict.get("state") or "",
            "login_log": verdict.get("log") or "",
            "account": account,
            "total": stats["raw"],
            "matched": stats["matched"],
            "invalid": stats["invalid"],
            "fell_back": stats["fell_back"],
            "user_agent": user_agent,
            "fingerprint": self.fingerprint,
            "profile_dir": self.profile_dir,
        }
        self.result = result

        if self.on_success is not None:
            try:
                self.persist = self.on_success(result)
            except Exception as exc:                             # noqa: BLE001
                self.persist = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        # 回调可以要求"把这份资料目录搬到账号名下"（见 adopt_profile）：
        # 只是记下来，真正的搬动要等浏览器关掉（目录还开着的时候动不了）。
        if isinstance(self.persist, dict):
            target = str(self.persist.get("adopt_profile") or "")
            if target and os.path.abspath(target) != os.path.abspath(self.profile_dir):
                self._adopt = target
        self._set(STATE_DONE, self._done_message(result))
        logger.info(
            f"[QR] 登录成功：账号={account.get('nickname') or '-'}"
            f" 抖音号={account.get('unique_id') or '-'}"
            f" cookie={len(cookies)} 条"
            f" 落盘={'ok' if isinstance(self.persist, dict) and self.persist.get('ok') else '失败'}"
            f" profile={self.profile_dir}")

    def adopt_profile(self, target_dir: str) -> None:
        """把这次登录用的资料目录改挂到账号名下（浏览器关闭后执行）。

        为什么需要：**新建账号时还不知道抖音号**，扫码用的目录只能叫 `default`；
        等登录成功读到抖音号，如果不搬，"按账号读好友列表"用的是 `<抖音号>/`
        那个空目录 —— 看起来就是"这个账号还没登录"。搬一次之后两边就对上了。
        """
        target = str(target_dir or "")
        if target and os.path.abspath(target) != os.path.abspath(self.profile_dir):
            self._adopt = target

    @staticmethod
    def _done_message(result: dict) -> str:
        account = result.get("account") or {}
        who = account.get("nickname") or "该账号"
        uid = account.get("unique_id") or ""
        head = f"登录成功：{who}" + (f"（抖音号 {uid}）" if uid else "")
        if not result.get("cookie_names"):
            return head
        return f"{head}，已取到 {len(result['cookie_names'])} 项 Cookie"

    def _close(self, ctx) -> bool:
        """关浏览器。返回**是否关干净** —— 关不干净就不能去搬它的资料目录。

        用 `core.browser.close_context()` 而不是光 `ctx.close()`：它顺手把这个
        上下文自己的 playwright 句柄 `stop()` 掉。回落路径每开一次浏览器要起一个
        driver 子进程（node），以前那些句柄只往 `_KEEPALIVE` 里塞、从不回收。
        """
        if ctx is None:
            return True
        if browser_mod.close_context(ctx):
            return True
        # 关的时候报错，未必等于"浏览器还活着"：目标已经死了的话 Playwright 也会
        # 抛（Target closed / Browser closed）。真活着会在资料目录里留
        # SingletonLock —— 以它为准，别把好目录当脏目录。
        if not os.path.lexists(os.path.join(self.profile_dir, "SingletonLock")):
            logger.warning("[QR] 关浏览器时报了错，但资料目录没有 SingletonLock，按已关掉处理")
            return True
        return False


def start_session(profile_dir, *, fingerprint="", proxy=None, ttl=DEFAULT_TTL_SECONDS,
                  on_success=None, headless=True) -> QrLoginSession:
    """建好会话并起线程（调用方负责保存引用，别让它被 GC 掉）。"""
    session = QrLoginSession(
        profile_dir,
        fingerprint=fingerprint,
        proxy=proxy,
        ttl=ttl,
        on_success=on_success,
        headless=headless,
    )
    session.start()
    return session
