"""限流 / 风控熔断 —— 纯 stdlib，可离线单测，不碰浏览器。

## 为什么需要它

连续给一堆好友发同一句模板话术，是**最容易被抖音判定为营销号**的行为。
一旦命中风控，继续重试只会把风险喂大（轻则本轮全废，重则账号被限）。

所以策略是"命中即熔断"：立刻停下本轮剩余目标，写一个冷却截止时间，
**冷却期内连新的一轮都不启动**（定时触发也跳过）。宁可少发一天火花，
也不能把账号打进去。

## 两个来源都要看

只查页面文案会漏掉"接口回了风控码但页面没弹提示"；
只查接口回执会漏掉"页面已经弹了滑块验证但请求还没回来"。
所以两个来源都扫，**回执（机器可读）优先**：

* HTTP：`v1/message/send` 回执里的 `status` 字段（`decode_send_resp` 的产物）
* DOM：提示条 / 弹窗 / toast 的**可见**文案（由 `douyin_im.JS_LIMIT_TEXT` 采集）

⚠️ 一个刻意的边界：**不把 `code == 8` 当限流**。那是抖音官方的「掉登录」码
（见 `douyin_im._watch_login_lost`），语义完全不同，混进来会让"登录失效"
被误报成"被限流"。登录问题由门禁负责，不归这里。
"""
import json
import os
import tempfile
from datetime import timedelta

from core.util_time import cooldown_human, iso, now, parse_iso  # noqa: F401  (re-export)

# 命中后默认冷却时长。30 分钟是保守值：足够跨过一个正常的发送节奏窗口，
# 又不至于把当天剩下的时间全废掉。
DEFAULT_COOLDOWN_SECONDS = 30 * 60

# 关键词按"后果"分类，方便日志/仪表盘给出人话解释。
CATEGORIES = {
    # 最典型：发送节奏过快
    "rate_limit": (
        "操作频繁", "操作太频繁", "操作过于频繁", "发送频繁", "发送过于频繁",
        "发送过快", "操作过快", "请求过于频繁", "请勿频繁", "频繁操作",
        "请稍后重试", "请稍后再试", "rate limit", "too many requests",
        "try again later",
    ),
    # 需要人机验证：已经是风控升级的信号
    "captcha": (
        "安全验证", "滑动验证", "滑块验证", "拖动滑块", "完成验证", "验证码",
        "人机验证", "captcha", "verify you are human",
    ),
    # 账号层面的风险提示
    "account_risk": (
        "账号异常", "账户异常", "操作异常", "账号存在风险", "存在风险",
        "功能受限", "账号受限", "已被限制", "risk control",
    ),
}

# 扁平化：`detect_texts` 用得上，测试也用它断言覆盖面
KEYWORDS = tuple(kw for kws in CATEGORIES.values() for kw in kws)


def _match(text):
    """在一段文本里找关键词 → (关键词, 分类) 或 None（按分类表顺序）。"""
    if not text:
        return None
    low = str(text).lower()
    for category, kws in CATEGORIES.items():
        for kw in kws:
            if kw.lower() in low:
                return kw, category
    return None


# ---- 两个来源 -------------------------------------------------------------

def detect_texts(texts):
    """扫 DOM 文案列表 → 命中信息或 None。"""
    for t in texts or ():
        m = _match(t)
        if m:
            return {"keyword": m[0], "category": m[1], "source": "dom",
                    "text": str(t)[:200]}
    return None


def detect_receipt(receipt):
    """扫一条发送回执 → 命中信息或 None。

    只看 `status` 文案：`code` 我们没有"限流专用码"的可靠情报，瞎猜一个
    数字会把正常失败误判成熔断（比漏报更糟 —— 那会白停一天）。
    """
    if not receipt:
        return None
    m = _match(receipt.get("status"))
    if m:
        return {"keyword": m[0], "category": m[1], "source": "http",
                "text": f"code={receipt.get('code')} status={receipt.get('status')!r}"}
    return None


def detect(*, receipts=(), dom_texts=()):
    """两个来源一起扫，**回执优先** → 命中信息或 None。"""
    for r in receipts or ():
        hit = detect_receipt(r)
        if hit:
            return hit
    return detect_texts(dom_texts)


# ---- 冷却状态 -------------------------------------------------------------

def default_state_path():
    """冷却状态文件。放在 `logs/` 下，与运行账本同目录（可被环境变量覆盖）。

    相对路径按**仓库根**解析（`utils.config.resolve_path`）：按 CWD 解析时，从别的
    目录起的进程会读到另一份（不存在的）冷却状态 —— 冷却静默失效，命中风控之后
    照旧按点发车。`utils.config` 用函数内 import，避免 import 期把 dotenv 拉进来。
    """
    from utils.config import resolve_path       # noqa: PLC0415  函数内 import：避免 import 期耦合

    return resolve_path(os.getenv("RATE_LIMIT_FILE") or os.path.join("logs", "rate_limit.json"))


class LimitGuard:
    """熔断状态机：读/写一个 JSON 文件，进程间用文件系统同步。

    刻意不做内存缓存 —— 定时任务与 web 手动触发是两个进程，
    缓存会让"冷却中"这个判断在另一个进程里失效。
    """

    def __init__(self, path=None, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS):
        self.path = str(path or default_state_path())
        self.cooldown_seconds = int(cooldown_seconds)

    # -- 读 --
    def read(self):
        """读冷却记录；文件不存在/坏掉都返回 None（= 没有冷却）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def in_cooldown(self, at=None):
        """还在冷却中 → 返回记录（附 `remaining_seconds`）；否则 None。

        `cooldown_until` 解析不了 → 返回 None 并**不**删除文件：
        宁可放过一轮，也不要因为文件坏掉就永久停机。
        """
        rec = self.read()
        if not rec:
            return None
        until = parse_iso(rec.get("cooldown_until"))
        if until is None:
            return None
        # `at` 归到整秒再算剩余：带着微秒算会让 1800 秒的冷却一上来就显示 1799
        # （仪表盘倒计时莫名少一秒），而写入侧本来就只有秒级精度。
        at = (at or now()).replace(microsecond=0)
        if until <= at:
            return None
        out = dict(rec)
        out["remaining_seconds"] = int((until - at).total_seconds())
        return out

    def status(self, at=None):
        """给仪表盘用的完整状态。"""
        rec = self.read()
        if not rec:
            return {"tripped": False, "record": None}
        until = parse_iso(rec.get("cooldown_until"))
        at = (at or now()).replace(microsecond=0)
        active = bool(until and until > at)
        return {
            "tripped": active,
            "record": rec,
            "cooldown_until": rec.get("cooldown_until"),
            "remaining_seconds": int((until - at).total_seconds()) if active else 0,
            "expired": bool(until and until <= at),
        }

    # -- 写 --
    def trip(self, hit, *, target=None, run_id=None, at=None,
             cooldown_seconds=None):
        """记录一次熔断，返回落盘的记录。

        `at` 先归到整秒：`util_time.iso()` 是秒级精度，带着微秒算出的截止时间
        换算回剩余秒数时会**少一秒**（1800.0 变成 int(1799.99)=1799），
        仪表盘上的倒计时就莫名跳一格。
        """
        at = (at or now()).replace(microsecond=0)
        secs = int(cooldown_seconds if cooldown_seconds else self.cooldown_seconds)
        rec = {
            "tripped_at": iso(at),
            "cooldown_until": iso(at + timedelta(seconds=secs)),
            "cooldown_seconds": secs,
            "keyword": (hit or {}).get("keyword"),
            "category": (hit or {}).get("category"),
            "source": (hit or {}).get("source"),
            "detail": (hit or {}).get("text"),
            "target": target,
            "run_id": run_id,
        }
        self._write(rec)
        return rec

    def clear(self):
        """清掉冷却（人工"我知道没事了，继续跑"）。返回是否有文件被删。"""
        try:
            os.remove(self.path)
            return True
        except OSError:
            return False

    def _write(self, rec):
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        # 先写临时文件再 replace：避免 web 进程正好读到写了一半的 JSON。
        # ★ 临时名必须**唯一**：定时器与面板是两个进程，固定的 .tmp 会让它们写
        #   同一个 inode，可能把对方的半截内容发布出去 —— read() 解析失败是静默
        #   返回 None，冷却于是被当成"没有"，熔断失效还接着发（喂风控）。
        #   照抄 core/schedule.py / core/browser_state.py 的做法。
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(self.path) + ".",
                                   suffix=".tmp", dir=d or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---- 发送节奏（借鉴 A 的限流熔断之外，给自己一个"别太快"的下限） ----------

def sleep_jitter_bounds(min_seconds, max_seconds):
    """把两个配置值理成 (lo, hi)：负值/倒置都纠正成安全值。

    单测覆盖这个纯函数，发送循环里就不必再写防御代码。
    """
    lo = float(min_seconds or 0)
    hi = float(max_seconds or 0)
    if lo < 0:
        lo = 0.0
    if hi < lo:
        hi = lo
    return lo, hi
