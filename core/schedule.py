"""账号级发送时间表 —— 每个账号到点才发，一天只发一次。

## 为什么需要这个模块

改这里之前，"几点发"是 **systemd timer** 说了算：`OnCalendar=*-*-* 20:30:00`
一响，`python main.py task` 就把**所有**账号一口气跑完。账号数量一多，
想给它们岔开发送时间就只能改 timer —— 而 timer 只有一份，做不到"每个账号一个时间"。

现在的分工：

    systemd timer  每 5 分钟敲一次门（`OnCalendar=*:0/5`），只负责"醒过来"
    main.py task   醒过来之后问本模块：**现在有哪个账号到点了？**
    core/schedule  回答"该发谁"，并把"今天已经发过了"记在 state 文件里

于是每个账号的时间点可以各写各的（`TASKS[].time`），timer 不需要知道任何账号。

## 三个关键口径

1. **确定性的抖动**（`SCHEDULE_JITTER_MINUTES`，默认 15 分钟）
   不是准点发，而是在 `[设定时间 − 抖动, 设定时间 + 抖动]` 里挑一个时刻 ——
   前后**对称**：一半概率比设定时间早，一半比它晚。早期实现只往后退
   （`[设定时间, 设定时间 + 抖动]`），用户看到的是"设 20:30、实际每次 20:45"，
   2026-10-24 按用户要求改成对称（见 FUSION-DESIGN §11）。
   挑法是 `sha256(抖音号|日期)` 取模 —— **同一个账号同一天永远算出同一个时刻**，
   所以每 5 分钟敲一次门不会每次都得到不同答案，隔天又会换一个时刻。
   抖动设 0 就是严格按点。
   真正的发送时刻还受门铃粒度影响：门铃每 `tick_minutes()`（默认 5）分钟响一次，
   落在 `due_at` 之后、下一个门铃之间最多再晚一个间隔（这段在 `select()`）。

2. **当天补发**
   到点时机器关机/程序没跑，只要**还是当天**就补发（用户 2026-10-06 明确要求
   "只要还是当天就补发，多晚都补"）。跨天不补 —— 昨天的火花今天补没有意义，
   而且会在第二天一开机就把两轮挤在一起发出去。

3. **一天一次，发过就记**
   状态落在 `logs/schedule_state.json`（`SCHEDULE_STATE_FILE` 可改）。
   **只要这一轮真的走到了这个账号**（成功、失败、账号级报错都算）就记上，
   当天不再重试 —— 否则一个坏了 cookie 的账号会被 5 分钟一次的门铃
   反复踹起来开浏览器，一天 288 次。
   面板的"立即运行"是**强制**的（不受时间表约束），并且同样记账 ——
   这样手动跑过之后再也不会被定时那一轮重复发一遍。
   `skipped` 是唯一的例外：它表示"一条都没发出去、当天名额还在"，下一次门铃
   要接着试。但也不是无限试 —— 每条 skipped 记录带 `attempts` 计数，试满
   `SCHEDULE_SKIP_RETRY_MAX`（默认 6）次之后当天不再重试（见 `select()`）。

## 和 `TASK_SCHEDULE_TIMES` 的关系

`.env` 里的 `TASK_SCHEDULE_TIMES` 以前只影响面板显示，现在多了一个身份：
**没单独配时间的账号，就跟这个时间**。空则回落到 `DEFAULT_TIME`。
"""
import hashlib
import json
import os
import tempfile
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta

from core.util_time import iso, now, parse_iso
from utils.logger import setup_logger

logger = setup_logger("schedule")

try:                     # POSIX；Windows 上没有 fcntl，退化成"不加锁"（见 _state_lock）
    import fcntl
except ImportError:      # pragma: no cover - 只在 Windows 上走到
    fcntl = None

#: 没配 `TASK_SCHEDULE_TIMES`、账号也没写自己的时间时，用这个点。
#: 与旧部署 `douyin-spark.service` 的 20:30 保持一致（2026-10-01 用户指定的口径）。
DEFAULT_TIME = "20:30"

#: 默认抖动分钟数。0 = 严格按点。可由 `.env` 的 `SCHEDULE_JITTER_MINUTES` 覆盖。
DEFAULT_JITTER_MINUTES = 15

#: 抖动 15 分钟（默认）时对应的派发间隔。`tick_minutes()` 的输出之一，
#: 保留成常量是为了让部署期测试和面板文案有个不言自明的基准值。
TICK_MINUTES = 5

#: 派发间隔的上下限。上限 10 分钟：再稀就会让"设定 08:00"变成"08:10 之后才可能发"，
#: 用户填的时间点就没意义了。下限 1 分钟：`OnCalendar` 最细也就到分钟。
TICK_MIN = 1
TICK_MAX = 10

#: `skipped` 记录当天最多被门铃重试几次（`SCHEDULE_SKIP_RETRY_MAX` 可覆盖）。
#: 为什么要有上限：`skipped` 的含义是"当天名额还在、下一次门铃再试"，而门铃
#: 每 5 分钟响一次 —— 一个一直过不了门禁的账号会被反复踹起来开浏览器，一天
#: 最多 288 次，这本身就在喂风控。试满 6 次（默认约半小时）就当"今天不再试"，
#: 记录留着、状态仍是 skipped，用户重新扫码（`retract_mark` 会把计数归零）
#: 之后当天照样补发。0 = 不限制，退回从前的"一直重试"。
DEFAULT_SKIP_RETRY_MAX = 6


def tick_minutes(jitter=None):
    """由抖动幅度推出"timer 该多勤快地敲门"，单位分钟。

    **这个值不单独做配置项**，是刻意的：它的正确取值完全由抖动决定，
    多一个旋钮就多一种"调错了但看不出来"的坏配置 ——
    比如抖动 2 分钟却把敲门间隔设成 30 分钟，用户会看到消息晚半小时才发，
    而面板上一切正常。

    取 `抖动 / 3`：窗口现在是一前一后共 `2 × 抖动`，这个间隔是窗口的六分之一，
    窗口内至少被敲到 6 次；落在窗口里的任意时刻最多迟到 `间隔` 分钟 ——
    既不会错过，也不会把 `due_at` 拖过当天。
    """
    if jitter is None:
        jitter = jitter_minutes_env()
    span = max(0, int(jitter or 0))
    if span <= 0:
        return TICK_MIN  # 严格按点 → 每分钟敲一次，误差只有 systemd 的 AccuracySec
    return max(TICK_MIN, min(TICK_MAX, int(round(span / 3.0))))

#: 建议的 state 文件位置（可被 `SCHEDULE_STATE_FILE` 覆盖）。
DEFAULT_STATE_FILE = os.path.join("logs", "schedule_state.json")

STATE_VERSION = 1

#: 这一天这个账号的结局。三种都算"今天办过了"，不再重试。
STATUS_OK = "ok"            # 跑完了（个别好友失败也算）
STATUS_ERROR = "error"      # 账号级异常
STATUS_SKIPPED = "skipped"  # 没跑起来（缺 cookie 等）
STATUS_LABELS = {
    STATUS_OK: "已发",
    STATUS_ERROR: "出错",
    STATUS_SKIPPED: "跳过",
}


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------

def parse_time(value):
    """把 `"8:00"` / `"08:00"` / `" 08:00 "` 归一成 `"08:00"`；认不出来返回 None。

    刻意**只认 24 小时制的 `HH:MM`**：`"8点"`、`"08:00:00"`、`"800"` 一律 None。
    配置里的时间是"发消息给真人"的时刻，猜错了代价不对称 —— 宁可让用户
    在面板上重填一次，也不要自作聪明把 `"0800"` 读成 00:08。
    """
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    hour_text, _, minute_text = text.partition(":")
    if not hour_text.isdigit() or not minute_text.isdigit():
        return None
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def minutes_of(text):
    """`"08:00"` → 480。认不出来返回 None。"""
    parsed = parse_time(text)
    if parsed is None:
        return None
    hour, minute = parsed.split(":")
    return int(hour) * 60 + int(minute)


def fallback_time(times=None):
    """全局默认时间：`TASK_SCHEDULE_TIMES` 的第一项，认不出来就 `DEFAULT_TIME`。

    只取**第一项**：这个键允许写多个（历史遗留，面板"下次定时"会全列出来），
    但"没配时间的账号跟哪个点"必须只有一个答案，否则同一份配置有两种解释。
    """
    for item in _as_list(times):
        parsed = parse_time(item)
        if parsed:
            return parsed
    return DEFAULT_TIME


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    try:
        return [str(item).strip() for item in value]
    except TypeError:
        return [str(value).strip()]


# ---------------------------------------------------------------------------
# 抖动
# ---------------------------------------------------------------------------

def jitter_minutes(unique_id, day, span):
    """同一个账号、同一天，永远算出同一个抖动分钟数（`-span..+span`）。

    用哈希而不是 `random`：每 5 分钟门铃响一次，如果每次都是新的随机数，
    就会出现"这一分钟判它没到点、下一分钟又判它到点了"的抖动，
    而且 `due_at` 会一直往后飘、永远追不上。

    ★ 区间是**对称**的（2026-10-24 用户反馈"设 20:30、实际每次都 20:45"）。
    取模窗口是 `2 * span + 1` 再减 `span`：`span=15` 时可能提前 15 分钟，
    也可能推后 15 分钟。落地时刻还会被门铃粒度往后带一点（≤ 一个 tick）。
    """
    span = max(0, int(span or 0))
    if span <= 0:
        return 0
    seed = f"{unique_id or ''}|{day or ''}|{span}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return int(digest[:8], 16) % (span * 2 + 1) - span


def day_of(when):
    """`datetime` → `"YYYY-MM-DD"`（本地日期）。"""
    return when.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------

def user_enabled(user):
    """这个账号现在启用没有（`TASKS[].enabled`，缺键 = 启用）。

    解析规则只有一份，在 `utils.config.task_enabled`（它认得手写进 `.env` 的
    "false"/"0"/"off"/"停用" 这些形态）。派发器与面板拿到的账号字典里已经是布尔，
    只有**别人手搓的调用**（测试、探测脚本）会传字符串，所以这里只在那一种情况下
    才去借那一份规则，模块级不 import `utils.config`（见 `state_path` 的注释）。
    """
    if not isinstance(user, dict):
        return True
    value = user.get("enabled", True)
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    from utils.config import task_enabled       # noqa: PLC0415 函数内 import：避免 import 期耦合

    return task_enabled({"enabled": value})


def plan_for(user, *, when=None, fallback=None, jitter=None):
    """算出一个账号"今天几点该发"。

    返回的字典是**只读的展示/判定用快照**，不落盘：
        unique_id / username / time（最终生效的 HH:MM）/ custom（是不是账号自己写的）
        enabled（`TASKS[].enabled`）/ jitter_minutes / due_at（datetime）/ due_text / tick_minutes
    """
    when = when or now()
    uid = str((user or {}).get("unique_id") or "")
    own = parse_time((user or {}).get("time"))
    final = own or fallback_time(fallback)
    span = max(0, int(jitter if jitter is not None else DEFAULT_JITTER_MINUTES))
    offset = jitter_minutes(uid, day_of(when), span)
    hour, minute = (int(part) for part in final.split(":"))
    due = when.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=offset)
    # 抖动只允许把到点时刻在**当天**范围里挪，一头一尾都要夹：
    # 往后：systemd 门铃最后一次是 23:55（`*:0/5`），推过零点之后当天每一次门铃
    #   看到的都是「还没到点」，而下一次门铃已经是第二天、又按新的一天重算 ——
    #   这个账号当天一条都发不出去，日志里还一声不吭。挤回当天最后一次门铃：
    #   宁可早几分钟，也不能整天不发。
    # 往前：设定 00:05、抖动 -10 会落到昨天 23:55 —— 昨天的门铃早过去了，
    #   而 `select()` 拿"今天"的日期去比一个昨天的时刻，直接变成"永远没到点"。
    #   同理挤回当天 00:00，让当天第一个门铃就发。
    day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    last_tick = when.replace(hour=23, minute=60 - TICK_MINUTES, second=0, microsecond=0)
    if due < day_start:
        due = day_start
    elif due > last_tick:
        due = last_tick
    return {
        "unique_id": uid,
        "username": str((user or {}).get("username") or uid),
        "time": final,
        "custom": bool(own),
        # 停用的账号照样算得出"今天几点该发"（面板要显示它的时间），只是不会被派发。
        "enabled": user_enabled(user),
        "jitter_minutes": span,
        "offset_minutes": offset,
        "due_at": due,
        "due_text": due.strftime("%H:%M"),
        "tick_minutes": tick_minutes(span),
    }


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def state_path(path=None):
    """状态文件路径。相对路径按**仓库根**解析（与 `RUN_LOCK_FILE` 等同一套语义）。

    ★ 为什么不能按 CWD 解析：这个文件记的是"今天哪个账号发过了"。从别的目录起
    进程（`cd /tmp && python /opt/douyin-spark-fusion/main.py task`、docker 换了
    WORKDIR、systemd 少了 `WorkingDirectory=`）时，相对路径会**另开一份**空状态
    —— 每个账号当天再发一遍，而且两边都觉得自己是对的。
    `core/run_ledger.py` 的账本/锁已经这么锚了（见 `utils.config.resolve_path`），
    这里必须同口径，否则"账本说发过了、状态说没发过"会互相打架。

    `utils.config` 用**函数内** import：`webui` 也会 import 本模块，模块级 import
    会把 dotenv / 环境变量快照一起拉进面板进程（与 `core/run_ledger.py` 同款写法）。
    """
    from utils.config import resolve_path       # noqa: PLC0415  函数内 import：避免 import 期耦合

    return str(resolve_path(path or os.getenv("SCHEDULE_STATE_FILE") or DEFAULT_STATE_FILE))


def load_state(path=None):
    """读状态文件。**任何异常都当成"还没有记录"**，绝不让它挡住发送。

    读不出来最坏的后果是"今天多发一次"；抛异常最坏的后果是"一个账号都发不出去"。
    这两者不对称，所以这里一律吞掉。
    """
    target = state_path(path)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "updated_at": None, "accounts": {}}
    if not isinstance(data, dict):
        return {"version": STATE_VERSION, "updated_at": None, "accounts": {}}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    try:
        # `int()` 也在这个 try 里：`version` 被写成 `"3.5"` / `"x"` / `1e999` 时
        # 它会抛 ValueError/OverflowError —— 而在 try 外面抛，之后**每次**
        # load_state 都炸，等于全账号停发到人工修文件为止。
        version = int(data.get("version") or STATE_VERSION)
    except (TypeError, ValueError, OverflowError):
        version = STATE_VERSION
    return {
        "version": version,
        "updated_at": data.get("updated_at"),
        "accounts": {str(k): v for k, v in accounts.items() if isinstance(v, dict)},
    }


@contextmanager
def _state_lock(target, timeout=2.0):
    """给"读-改-写整个状态文件"加一把**跨进程**的小锁（`<状态文件>.lock`）。

    ★ 为什么需要它：`save_marks` 的合并要先读磁盘再写回，如果两个进程（sender
      收尾 + 面板扫码成功回调）同时做这件事，两边都读到同一份旧文件，各自把
      自己的改动写进去，最后一个 `os.replace` 赢 —— 另一个进程刚记的账**丢了**。
      光靠 `os.replace` 的原子性救不了这种情况（原子性只保证文件不会写坏，
      不保证不丢更新）。

    行为约定：拿不到锁（超时）**不阻塞**发送流程 —— 照样去合并写，只是退回
    "尽力而为"。宁可偶尔丢一次合并，也不能因为一个锁文件让整轮发送卡死。
    """
    if fcntl is None:                    # Windows：没有 flock，尽力而为
        yield False
        return
    lock_path = target + ".lock"
    handle = None
    acquired = False
    try:
        directory = os.path.dirname(os.path.abspath(lock_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = open(lock_path, "a+")
    except OSError as exc:
        # ★ 拿不到锁文件（目录只读 / 路径上是个普通文件 / 磁盘满 / fd 用尽）时
        #   **不能抛**：这个上下文管理器裹在 `save_marks` 外面，而 `save_marks`
        #   跑在 `core/tasks.py` 收尾的 finally 里 —— 从那里抛出去，同一段的
        #   记账、finished_at、账本追加会被一起带走：整轮发送在面板上**不留任何
        #   痕迹**（用户看到的是"什么都没发生"），下一轮还会把发过的账号重发一遍。
        #   契约本来就是"不抛、拿不到锁就尽力而为"（见上面 2 行），退化即可。
        logger.warning(
            f"⚠️ 发送时间表拿不到锁（{lock_path}）：{type(exc).__name__}: {exc} —— "
            f"本次合并写不加锁，可能与面板的改动互相覆盖"
        )
        yield False
        return
    try:
        deadline = _time.time() + max(0.0, float(timeout))
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if _time.time() >= deadline:
                    break
                _time.sleep(0.05)
        yield acquired
    finally:
        if handle is not None:
            try:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass


def _stamp(record):
    """取一条记账的时间戳（用来判新旧）。取不到返回 None = 当成最旧。"""
    if not isinstance(record, dict):
        return None
    for key in ("sent_at", "cleared_at", "updated_at"):
        moment = parse_iso(record.get(key))
        if moment is not None:
            return moment
    return None


def save_state(state, path=None):
    """原子写：先写同目录下的**唯一**临时文件再 `os.replace`。

    写失败只返回 False（调用方记警告），不抛 —— 记不上账不该让这一轮发送失败。

    ★ 临时文件名为什么必须唯一：以前是 `target + ".tmp"` 这个**固定**名字，
      sender 与面板各自的读-改-写窗口重叠时，两个进程用 "w" 打开的是同一个
      inode，谁先写完谁就被另一个人截断；`os.replace` 只搬得走其中一个，另一个
      进程可能把**半截 JSON** 搬成正式文件。下次 `load_state` 解析失败会按
      "还没有记录"处理 —— 于是所有账号"今天已发"的记账一次性清空、全量重发。
    """
    target = state_path(path)
    payload = dict(state or {})
    payload["version"] = STATE_VERSION
    payload["updated_at"] = iso(now())
    directory = os.path.dirname(os.path.abspath(target)) or "."
    tmp = ""
    try:
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False,
            prefix=os.path.basename(target) + ".", suffix=".tmp",
        )
        tmp = handle.name
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # NamedTemporaryFile 建出来是 600；这个文件历来是 644（同 logs 目录里的
        # 其它文件），保持一致，免得非 root 的读取方（旧部署、排障脚本）突然读不到。
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, target)
        return True
    except OSError:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def save_marks(state, unique_ids, path=None, *, prune_stale=True):
    """把 `state` 里**指定这几个账号**的记账合并进磁盘上的最新状态，再原子写。

    ★ 为什么不能直接 `save_state(state)`：`state` 是调用方在**这一轮开始时**读的
      快照，而 sender 的"读 → 写"窗口等于整轮浏览器操作（分钟级～1 小时）。这
      中间面板完全可能改过同一个文件（扫码成功回调撤掉当天的记账）。用旧快照
      整体覆盖 = 把面板刚做的改动抹掉（2026-10-09 示例号 掉登录后当天静默不发），
      或者把面板刚撤掉的记账复活（当天一条都发不出去）。

    合并规则：
      * 只动 `unique_ids` 里点名的账号，其余账号一律以磁盘为准；
      * 同一个账号，**时间戳新的赢**（面板的"撤账墓碑"用的是撤销时刻，见
        `retract_mark`；sender 用的是本轮开始时刻，所以谁后发生听谁的）；
      * 顺带把明显过期的记录清掉（`prune`，只减不加）。

    返回是否写成功（与 `save_state` 一样不抛）。
    """
    target = state_path(path)
    wanted = [str(uid or "") for uid in (unique_ids or []) if str(uid or "")]
    with _state_lock(target) as locked:
        if not locked:
            # 锁没拿到也照写：状态记不上的后果（重复发送）比丢一次合并更重。
            pass
        disk = load_state(target)
        if prune_stale:
            prune(disk)
        accounts = disk.setdefault("accounts", {})
        mine = (state or {}).get("accounts") or {}
        for key in wanted:
            if key not in mine:
                continue
            incoming = mine[key]
            current = accounts.get(key)
            if current is not None and _is_relogin_tombstone(current) and _is_real_success(incoming):
                # ★ 真正的成功比"撤账墓碑"更可信，时间戳不比了。
                # 场景：面板扫码成功 → 写墓碑（撤销时刻）→ 与此同时 sender 正跑着
                # 这一轮，收尾时用**本轮开始时刻**写 ok。开局时刻早于墓碑时刻，
                # 按时间戳合并就会把 ok 改回 skipped，下一次门铃再发一遍
                # = 同一天两条火花消息。墓碑的用途只是让"没发出去的"那天能补发，
                # 一旦这一轮真发出去了，墓碑就已经过期了。
                accounts[key] = incoming
                continue
            if current is not None and _newer(current, incoming):
                continue           # 磁盘上的更新 → 认输，别拿旧快照盖回去
            accounts[key] = incoming
        return save_state(disk, target)


def _is_relogin_tombstone(record):
    """这条记录是不是 `retract_mark` 写下的"扫码撤账墓碑"。"""
    if not isinstance(record, dict):
        return False
    return (str(record.get("status") or "") == STATUS_SKIPPED
            and str(record.get("source") or "") == "relogin")


def _is_real_success(record):
    """这条记录是不是"这一轮真发出去了"。"""
    if not isinstance(record, dict):
        return False
    return str(record.get("status") or "") == STATUS_OK


def _newer(left, right):
    """`left` 是否比 `right` 新。任一侧没有时间戳时判 False（= 不阻止写入）。"""
    a, b = _stamp(left), _stamp(right)
    if a is None or b is None:
        return False
    return a > b


def entry_for(state, unique_id, when=None):
    """取某个账号今天的记录；不是今天的返回 None。"""
    record = (state or {}).get("accounts", {}).get(str(unique_id or ""))
    if not isinstance(record, dict):
        return None
    if when is not None and str(record.get("date") or "") != day_of(when):
        return None
    return record


def mark_sent(state, unique_id, *, when=None, plan=None, run_id="", status=STATUS_OK,
              source="cli", note="", count_attempt=True):
    """记上"这个账号今天办过了"。就地改 `state` 并返回那条记录。

    `status=skipped`（一条都没发出去）时记录上会带 `attempts`＝"今天第几次没跑
    起来"和 `last_attempt`：`select()` 靠它给反复过不了门禁的账号踩刹车
    （见 `skip_retry_exhausted`）。**同一轮里对同一个账号 mark 两次会把计数多加
    一次**（6 次的上限实际只撑 3 轮），调用方负责一轮只 mark 一次。

    `count_attempt=False` 给"这次跳过根本没开浏览器"用（`TASKS` 里 `targets`
    是空的那种）：它没有风控成本，而用户补配目标的时刻说不准 —— 那之后**没有
    任何东西**会把计数归零（只有重新扫码会走 `retract_mark`），计进去等于把这个
    账号当天静音。这类记录干脆不带 `attempts` 字段。
    """
    when = when or now()
    plan = dict(plan or {})
    day = day_of(when)
    key = str(unique_id or "")
    accounts = state.setdefault("accounts", {})
    record = {
        "date": day,
        "time": plan.get("time") or "",
        "due_at": iso(plan["due_at"]) if plan.get("due_at") else "",
        "sent_at": iso(when),
        "run_id": str(run_id or ""),
        "status": status,
        "source": str(source or ""),
        "note": str(note or "")[:200],
    }
    if status == STATUS_SKIPPED and count_attempt:
        # 按**今天**的记录累加：昨天试过 6 次不该让账号今天一上来就静音。
        record["attempts"] = _attempts_of(accounts.get(key), day) + 1
        record["last_attempt"] = iso(when)
    accounts[key] = record
    return record


def clear_mark(state, unique_id, *, when=None):
    """撤销"这个账号今天办过了"的记录，返回是否真的撤掉了。

    用途只有一个：**当天一条都没发出去**（登录失效等）之后重新登录成功，
    要让门铃在当天剩下的时间里把它补发掉。已经成功发过的账号绝不走这条路 ——
    那会变成一天发两遍。判定与落盘都在 `requeue_after_relogin()` 里，
    真实调用方（面板扫码成功回调、configTool 登录成功回调）走的是那一个入口。
    """
    accounts = (state or {}).get("accounts")
    if not isinstance(accounts, dict):
        return False
    key = str(unique_id or "")
    record = accounts.get(key)
    if not isinstance(record, dict):
        return False
    if when is not None and str(record.get("date") or "") != day_of(when):
        return False
    accounts.pop(key, None)
    return True


def retract_mark(state, unique_id, *, when=None, note=""):
    """把"今天办过了"改写成一条 **`skipped` 墓碑**，返回是否真的改了。

    用途与 `clear_mark` 相同（当天一条都没发出去 → 重新登录成功后要让门铃补发），
    区别是**不删除、改写**：

    ★ 为什么不能删：删掉以后，"面板撤过账"这件事在文件里不留任何时间戳。
      而 sender 手里可能还攥着几分钟前读到的旧快照，收尾时用 `save_marks` 合并写
      —— 合并只能比时间戳，没有时间戳就比不出来，于是它会把 error 记录原样写回，
      当天火花再次静默丢失（2026-10-09 示例号 掉登录就丢在这条路径上）。
      写成墓碑之后，合并按时间戳一比就知道"面板后来撤过"，会认输。

    `skipped` 的口径与门禁/熔断一致：**一条都没发出去，当天名额还在**
    （见 `select()` 与 `STATUS_SKIPPED` 的注释），所以下一次门铃会重试它。
    """
    when = when or now()
    accounts = (state or {}).get("accounts")
    if not isinstance(accounts, dict):
        return False
    key = str(unique_id or "")
    record = accounts.get(key)
    if not isinstance(record, dict):
        return False
    if str(record.get("date") or "") != day_of(when):
        return False
    tomb = dict(record)
    tomb.update({
        "status": STATUS_SKIPPED,
        "sent_at": iso(when),
        "source": "relogin",
        "note": str(note or "重新登录成功，撤回今天的记账，等下一次门铃补发")[:200],
        # ★ 重试计数归零：墓碑的意义是"今天这条还没发出去，下一次门铃接着试"。
        #   重新扫码成功之后，用户等的是**今天**补发 —— 要是把撤账前的 6 次
        #   失败计数带过来，`select()` 会立刻判成"今天已经试够了"而不再重试，
        #   当天火花静默丢失（这正是这个模块最怕的形状）。留 0 而不是删字段：
        #   面板读的时候一眼看得见"这个账号今天从零开始试"。
        "attempts": 0,
    })
    accounts[key] = tomb
    return True


def requeue_after_relogin(unique_id, *, when=None, state=None, runs=None,
                           dry_run=False):
    """登录成功之后，把"今天已经办过"的记账撤掉 —— **前提是今天一条都没发出去**。

    ★ 为什么这件事只能有一份实现：当天掉登录之后重新登录的入口有**两条**，
      面板扫码（`webui/app.py`）与 configTool（`configTool/main.py`，README 里与面板
      并列的正规登录路径："用来生成/修改 .env、抓取登录态"）。判定写两份，迟早漏掉
      一个分支，而漏掉的后果是**当天火花静默丢失**：状态文件里记着"今天办过"、
      门铃当天不再叫，用户却以为"登录成功 = 会补发"（2026-10-09 的事故换个入口重演，
      见 2026-10-18 issue #12）。

    三条判定（与面板原来那份逐字一致）：
      1. 今天这个账号在账本里有 `sent_ok > 0`，或状态文件里今天这条是 `ok`
         → **绝不撤**（撤了就变成一天发两遍）；
      2. 今天账本与状态文件都没见过它 → 不撤（`no-run-today`：门铃本来就会发它）；
      3. 其余（今天跑过、但一条都没成功）→ 用 `retract_mark` 改写成 `skipped` 墓碑
         再按时间戳合并落盘（为什么不删记录见那里的注释）。

    `state` / `runs` 只是给测试注入用的；不传就现读（账本 + 状态文件**两个都看**：
    收尾时先写状态、后追加账本，中间被打断就成了"状态里有今天、账本里没有"，
    只看账本会误判成 `no-run-today` 而既不撤账也不补发）。

    `dry_run` 只回答"撤得动吗"、不碰任何文件（`saved` 恒为 False）：面板用它先把
    拒绝的话说完，再决定要不要花十几秒去探登录态 —— 否则用户点一下，先等十秒起
    浏览器，最后才被告知"今天已经成功发过"，纯属白等（2026-10-30）。

    返回 `{"requeued": bool, "saved": bool, "reason": str}`。任何异常都翻成 `reason`，
    绝不往外抛 —— 调用它的是"登录刚成功"的那一刻，补发判断失败不能让登录本身失败。
    """
    uid = str(unique_id or "").strip()
    when = when or now()
    if not uid:
        return {"requeued": False, "saved": False, "reason": "no-uid"}
    try:
        from core import run_ledger       # noqa: PLC0415 函数内 import：与 state_path 同款，避免 import 期耦合
    except Exception as exc:  # noqa: BLE001
        return {"requeued": False, "saved": False, "reason": f"import-failed: {type(exc).__name__}"}

    today = day_of(when)
    if runs is None:
        try:
            runs = run_ledger.read_runs(limit=200)
        except Exception as exc:  # noqa: BLE001
            return {"requeued": False, "saved": False, "reason": f"{type(exc).__name__}: {exc}"}

    seen_today = sent_today = False
    for record in runs or []:
        if not isinstance(record, dict):
            continue
        if not str(record.get("started_at") or "").startswith(today):
            continue
        for account in record.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            if str(account.get("unique_id") or "") != uid:
                continue
            seen_today = True
            try:
                if int(account.get("sent_ok") or 0) > 0:
                    sent_today = True
            except (TypeError, ValueError):
                sent_today = True      # 读不懂就当发过：宁可不补发，也不能补出双份

    if state is None:
        try:
            state = load_state()
        except Exception:  # noqa: BLE001 —— 状态文件读不了就只看账本
            state = None
    try:
        mark = entry_for(state, uid, when=when)
    except Exception:  # noqa: BLE001
        mark = None
    if isinstance(mark, dict):
        seen_today = True
        if str(mark.get("status") or "") == STATUS_OK:
            sent_today = True
    if sent_today:
        return {"requeued": False, "saved": False, "reason": "already-sent"}
    if not seen_today:
        return {"requeued": False, "saved": False, "reason": "no-run-today"}
    if dry_run:
        # 走到这里说明"今天跑过、但一条都没成功"：只要今天这条记录还在，retract_mark
        # 就撤得动；记录不见了（刚被别处撤过）才是 no-mark。不读也不写状态文件。
        ok = isinstance(mark, dict)
        return {"requeued": ok, "saved": False,
                "reason": "" if ok else "no-mark", "dry_run": True}
    try:
        # 这里是**跨进程**改状态文件：不能整体覆盖。sender 可能正跑着这一轮，
        # 它手里那份快照是几分钟前读的，收尾时会合并写一遍 —— 所以撤账要写成
        # `skipped` 墓碑（带撤销时刻）再按时间戳合并，sender 才会认输；
        # 直接删掉的话文件里没有时间戳可比，它会把 error 记录原样写回，
        # 当天火花再次静默丢失（2026-10-09 走的正是这条路径）。
        state = load_state()
        changed = retract_mark(state, uid, when=when)
        saved = save_marks(state, [uid]) if changed else False
    except Exception as exc:  # noqa: BLE001
        return {"requeued": False, "saved": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"requeued": bool(changed), "saved": bool(saved),
            "reason": "" if changed else "no-mark"}


def prune(state, *, today=None, keep_days=7):
    """丢掉明显过期的记录（按日期字符串比，不动今天的）。

    状态文件是"每天一行"的量级，不清也不会涨到哪去；这里只是别让它无限攒着。
    """
    today = today or day_of(now())
    if not isinstance(today, str) or len(today) != 10:
        return 0
    try:
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=max(1, int(keep_days)))).strftime("%Y-%m-%d")
    except ValueError:
        return 0
    accounts = (state or {}).get("accounts") or {}
    stale = [k for k, v in accounts.items() if str((v or {}).get("date") or "") < cutoff]
    for key in stale:
        accounts.pop(key, None)
    return len(stale)


# ---------------------------------------------------------------------------
# 筛选
# ---------------------------------------------------------------------------

def _attempts_of(record, day=None):
    """这条记录"今天已经试过几次"。

    字段缺失（旧版本写的记录）、写坏、写成 `"3"`/`3.0` 都按 0 算 —— 一律当成
    "还没试过"，也就是**继续按老规矩重试**。宁可多踹一次，也不能因为一个坏字段
    让账号当天彻底静音（那正是"火花静默断掉"的形状）。
    """
    if not isinstance(record, dict):
        return 0
    if day is not None and str(record.get("date") or "") != str(day):
        return 0                      # 昨天的计数不能算到今天头上
    try:
        # OverflowError 也要接：`float("1e999")` 是合法的 inf，`int(inf)` 抛的是
        # OverflowError，漏掉它 = 每次 `select()` 都炸（定时器每 5 分钟一次）。
        return max(0, int(float(record.get("attempts") or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def skip_retry_exhausted(record, *, limit=None, day=None):
    """这条 `skipped` 记录是不是"今天已经试够次数了"（= 今天不再试）。

    `day` 不传就按**当前时刻**算；`select()` 传的是它自己那个 `when` 的日期 ——
    它可能是个"假装的现在"（测试、回放），用真实当前时间比日期会永远对不上。

    面板/日志想解释"为什么这个账号今天不动了"时可以直接用这个判断。
    """
    if not isinstance(record, dict):
        return False
    if str(record.get("status") or "") != STATUS_SKIPPED:
        return False
    cap = skip_retry_max_env() if limit is None else max(0, int(limit))
    if cap <= 0:
        return False                  # 0 = 不限制，退回从前的"一直重试"
    return _attempts_of(record, day_of(now()) if day is None else day) >= cap


def select(users, *, when=None, state=None, fallback=None, jitter=None):
    """把账号分成四堆：到点了 / 还没到 / 今天发过了 / 已停用。

    判定顺序很重要 —— **"今天发过了"优先于"到没到点"**，否则手动跑过一轮之后，
    到点那一轮会把它再发一遍。

    `paused` 是 2026-10-24 新增的一堆（`TASKS[].enabled=false`）：停用的账号
    **不参与派发**，既不进 `due`（不能被发）也不进 `waiting`（面板拿 waiting
    算"下一个是谁"，停用账号会给出一个永远不会发生的时刻）。它也不进 `sent`：
    那是"今天办过了"的口径，停用账号今天根本没被尝试，混进去会让日志里的
    "今天已发 N 个"和真实回执对不上。
    """
    when = when or now()
    state = state if state is not None else load_state()
    fallback = fallback if fallback is not None else os.getenv("TASK_SCHEDULE_TIMES", "")
    if jitter is None:
        jitter = jitter_minutes_env()
    due, waiting, sent, paused = [], [], [], []
    plans = {}
    today = day_of(when)
    for user in users or []:
        plan = plan_for(user, when=when, fallback=fallback, jitter=jitter)
        uid = plan["unique_id"]
        plans[uid] = plan
        # ★ 停用排在最前：它的"今天到没到点""今天发没发过"都不影响派发决定，
        #   早一步分出去，后面的判定就不必逐个再想一遍"这个账号是不是停了"。
        if not plan.get("enabled", True):
            paused.append((user, plan))
            continue
        record = entry_for(state, uid, when)
        # 「今天发过了」只认**真发出去**的那一次。状态是 `skipped` 的记录意味着
        # 门禁没过 / 命中风控 —— 一条都没发出去，当天的名额还在，必须让下一次
        # 门铃继续试（用户可能在白天重新登录）。把它算成「已发」会让这个账号
        # 当天彻底静音，而面板上还写着「已发」。
        if record is not None and str(record.get("status") or "") != STATUS_SKIPPED:
            sent.append((user, plan, record))
        elif skip_retry_exhausted(record, day=today):
            # ★ 试够次数了（默认 6 次，约半小时）：当成"今天不再发"。
            #   记录**留着**、状态**仍是 skipped** —— 只是今天不再被门铃踹起来：
            #   一个过不了门禁（超时/掉登录没确认/命中风控）的账号本来每 5 分钟就
            #   会被重试一次，一天最多 288 次浏览器启动，这本身就在喂风控。
            #   用户重新扫码后 `retract_mark` 会把 attempts 归零（并把状态写成
            #   墓碑），当天照样补发 —— 既有功能没被破坏。
            sent.append((user, plan, record))
        elif when >= plan["due_at"]:
            due.append((user, plan))
        else:
            waiting.append((user, plan))
    return {
        "when": iso(when),
        "fallback": fallback_time(fallback),
        "jitter_minutes": max(0, int(jitter or 0)),
        "plans": plans,
        "due": due,
        "waiting": waiting,
        "sent": sent,
        "paused": paused,
        "idle": not due,
    }


def jitter_minutes_env(env=None):
    """读 `SCHEDULE_JITTER_MINUTES`。写错值回落到默认，且**下限为 0**。"""
    raw = (env if env is not None else os.getenv("SCHEDULE_JITTER_MINUTES"))
    if raw is None or not str(raw).strip():
        return DEFAULT_JITTER_MINUTES
    try:
        # OverflowError 也要接：`float("1e999")` 是合法的 inf，而 `int(inf)` 抛的是
        # OverflowError。这个函数跑在 `select()` 里，漏掉它 = 定时器每 5 分钟
        # 炸一次（报的还是 OverflowError，看不出是配置写错了）。
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_JITTER_MINUTES
    return max(0, value)


def skip_retry_max_env(env=None):
    """读 `SCHEDULE_SKIP_RETRY_MAX`：`skipped` 记录当天最多被重试几次。

    与 `jitter_minutes_env` 同一套口径：空/写坏 → 默认值；负数夹到 0（= 不限制，
    退回从前的"一直重试"）。**不设上限是危险的默认**，所以默认给
    `DEFAULT_SKIP_RETRY_MAX`，而不是 0。
    """
    raw = (env if env is not None else os.getenv("SCHEDULE_SKIP_RETRY_MAX"))
    if raw is None or not str(raw).strip():
        return DEFAULT_SKIP_RETRY_MAX
    try:
        # 与 jitter_minutes_env 一样要接 OverflowError（`float("1e999")` → inf）。
        # 这个函数跑在 `select()` 里，抛出去 = 门铃每 5 分钟炸一次，全部账号停发。
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_SKIP_RETRY_MAX
    return max(0, value)


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

def window_text(plan):
    """人话描述这个账号今天的发送窗口，给日志和面板共用。

    抖动的落点已经算死了（同一个账号同一天只有一个时刻），所以这里直接写
    **今天到底几点**，而不是含糊地说"0~15 分钟内随机"—— 面板上能算出来的
    东西就不该让用户猜。
    """
    plan = dict(plan or {})
    due = plan.get("due_text") or plan.get("time") or ""
    if not plan.get("jitter_minutes"):
        return f"每天 {plan.get('time') or ''}（不浮动）"
    return (f"每天 {plan.get('time') or ''}（今天落在 {due}，"
            f"浮动 ±{plan.get('jitter_minutes')} 分钟）")


def summary_text(result, plan=None, record=None):
    """给面板用的状态文案。传进来的都是本模块的 `select()`/`state` 产物。"""
    if record:
        label = STATUS_LABELS.get(str(record.get("status") or ""), "已处理")
        at = str(record.get("sent_at") or "")
        clock = at[11:16] if len(at) >= 16 else ""
        return f"今天 {clock} {label}（{record.get('source') or '手动'}）"
    if plan is not None:
        due = plan.get("due_text") or plan.get("time") or ""
        return f"等 {due} 到点"
    return str(result or "")


def wait_seconds(plan, when=None):
    """离到点还有多少秒（已经过了返回 0）。面板倒计时用。"""
    when = when or now()
    due = (plan or {}).get("due_at")
    if not isinstance(due, datetime):
        return 0
    return max(0, int((due - when).total_seconds()))


def sleep_until(_deadline=None):  # pragma: no cover - 保留给将来的"守着到点"模式
    """占位：本模块**不**负责睡觉。

    曾经想过让 timer 一天只响一次、然后在这里 `sleep` 到各个账号的时间点。
    放弃了：那样一个进程要挂十几个小时，中途重启/关机就全丢，
    而且日志里十几个小时只有一行。让 timer 勤敲、程序每次重新算，简单得多。
    """
    _time.sleep(0)
