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
   到点后不是立刻发，而是在 `[设定时间, 设定时间 + 抖动]` 里挑一个时刻。
   挑法是 `sha256(抖音号|日期)` 取模 —— **同一个账号同一天永远算出同一个时刻**，
   所以每 5 分钟敲一次门不会每次都得到不同答案，隔天又会换一个时刻。
   抖动设 0 就是严格按点。

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

## 和 `TASK_SCHEDULE_TIMES` 的关系

`.env` 里的 `TASK_SCHEDULE_TIMES` 以前只影响面板显示，现在多了一个身份：
**没单独配时间的账号，就跟这个时间**。空则回落到 `DEFAULT_TIME`。
"""
import hashlib
import json
import os
import time as _time
from datetime import datetime, timedelta

from core.util_time import iso, now, parse_iso

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


def tick_minutes(jitter=None):
    """由抖动幅度推出"timer 该多勤快地敲门"，单位分钟。

    **这个值不单独做配置项**，是刻意的：它的正确取值完全由抖动决定，
    多一个旋钮就多一种"调错了但看不出来"的坏配置 ——
    比如抖动 2 分钟却把敲门间隔设成 30 分钟，用户会看到消息晚半小时才发，
    而面板上一切正常。

    取 `抖动 / 3`：派发间隔是抖动窗口的三分之一时，落在窗口里的任意时刻
    最多迟到 `间隔` 分钟，而窗口内至少被敲到 3 次 —— 既不会错过，
    也不会把 `due_at` 拖过当天。
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
    """同一个账号、同一天，永远算出同一个抖动分钟数（0..span）。

    用哈希而不是 `random`：每 5 分钟门铃响一次，如果每次都是新的随机数，
    就会出现"这一分钟判它没到点、下一分钟又判它到点了"的抖动，
    而且 `due_at` 会一直往后飘、永远追不上。
    """
    span = max(0, int(span or 0))
    if span <= 0:
        return 0
    seed = f"{unique_id or ''}|{day or ''}|{span}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    return int(digest[:8], 16) % (span + 1)


def day_of(when):
    """`datetime` → `"YYYY-MM-DD"`（本地日期）。"""
    return when.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------

def plan_for(user, *, when=None, fallback=None, jitter=None):
    """算出一个账号"今天几点该发"。

    返回的字典是**只读的展示/判定用快照**，不落盘：
        unique_id / username / time（最终生效的 HH:MM）/ custom（是不是账号自己写的）
        jitter_minutes / due_at（datetime）/ due_text / tick_minutes
    """
    when = when or now()
    uid = str((user or {}).get("unique_id") or "")
    own = parse_time((user or {}).get("time"))
    final = own or fallback_time(fallback)
    span = max(0, int(jitter if jitter is not None else DEFAULT_JITTER_MINUTES))
    offset = jitter_minutes(uid, day_of(when), span)
    hour, minute = (int(part) for part in final.split(":"))
    due = when.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=offset)
    # 抖动**不能**把到点时刻推到明天。systemd 门铃最后一次是 23:55（`*:0/5`），
    # 推过零点之后当天每一次门铃看到的都是「还没到点」，而下一次门铃已经是
    # 第二天、又按新的一天重算 —— 这个账号当天一条都发不出去，日志里还一声不吭。
    # 挤回当天最后一次门铃：宁可早几分钟，也不能整天不发。
    last_tick = when.replace(hour=23, minute=60 - TICK_MINUTES, second=0, microsecond=0)
    if due > last_tick:
        due = last_tick
    return {
        "unique_id": uid,
        "username": str((user or {}).get("username") or uid),
        "time": final,
        "custom": bool(own),
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
    """状态文件路径。相对路径按 CWD 解析（与 `RUN_LOCK_FILE` 等保持一致）。"""
    return str(path or os.getenv("SCHEDULE_STATE_FILE") or DEFAULT_STATE_FILE)


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
    return {
        "version": int(data.get("version") or STATE_VERSION),
        "updated_at": data.get("updated_at"),
        "accounts": {str(k): v for k, v in accounts.items() if isinstance(v, dict)},
    }


def save_state(state, path=None):
    """原子写：先写 `.tmp` 再 `os.replace`，避免门铃正好撞上写一半的文件。

    写失败只返回 False（调用方记警告），不抛 —— 记不上账不该让这一轮发送失败。
    """
    target = state_path(path)
    payload = dict(state or {})
    payload["version"] = STATE_VERSION
    payload["updated_at"] = iso(now())
    directory = os.path.dirname(os.path.abspath(target))
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def entry_for(state, unique_id, when=None):
    """取某个账号今天的记录；不是今天的返回 None。"""
    record = (state or {}).get("accounts", {}).get(str(unique_id or ""))
    if not isinstance(record, dict):
        return None
    if when is not None and str(record.get("date") or "") != day_of(when):
        return None
    return record


def mark_sent(state, unique_id, *, when=None, plan=None, run_id="", status=STATUS_OK,
              source="cli", note=""):
    """记上"这个账号今天办过了"。就地改 `state` 并返回那条记录。"""
    when = when or now()
    plan = dict(plan or {})
    accounts = state.setdefault("accounts", {})
    record = {
        "date": day_of(when),
        "time": plan.get("time") or "",
        "due_at": iso(plan["due_at"]) if plan.get("due_at") else "",
        "sent_at": iso(when),
        "run_id": str(run_id or ""),
        "status": status,
        "source": str(source or ""),
        "note": str(note or "")[:200],
    }
    accounts[str(unique_id or "")] = record
    return record


def clear_mark(state, unique_id, *, when=None):
    """撤销"这个账号今天办过了"的记录，返回是否真的撤掉了。

    用途只有一个：**当天一条都没发出去**（登录失效等）之后重新登录成功，
    要让门铃在当天剩下的时间里把它补发掉。已经成功发过的账号绝不走这条路 ——
    那会变成一天发两遍（调用方负责判断，见 webui/app.py 的扫码成功回调）。
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

def select(users, *, when=None, state=None, fallback=None, jitter=None):
    """把账号分成三堆：到点了 / 还没到 / 今天发过了。

    判定顺序很重要 —— **"今天发过了"优先于"到没到点"**，否则手动跑过一轮之后，
    到点那一轮会把它再发一遍。
    """
    when = when or now()
    state = state if state is not None else load_state()
    fallback = fallback if fallback is not None else os.getenv("TASK_SCHEDULE_TIMES", "")
    if jitter is None:
        jitter = jitter_minutes_env()
    due, waiting, sent = [], [], []
    plans = {}
    for user in users or []:
        plan = plan_for(user, when=when, fallback=fallback, jitter=jitter)
        uid = plan["unique_id"]
        plans[uid] = plan
        record = entry_for(state, uid, when)
        # 「今天发过了」只认**真发出去**的那一次。状态是 `skipped` 的记录意味着
        # 门禁没过 / 命中风控 —— 一条都没发出去，当天的名额还在，必须让下一次
        # 门铃继续试（用户可能在白天重新登录）。把它算成「已发」会让这个账号
        # 当天彻底静音，而面板上还写着「已发」。
        if record is not None and str(record.get("status") or "") != STATUS_SKIPPED:
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
        "idle": not due,
    }


def jitter_minutes_env(env=None):
    """读 `SCHEDULE_JITTER_MINUTES`。写错值回落到默认，且**下限为 0**。"""
    raw = (env if env is not None else os.getenv("SCHEDULE_JITTER_MINUTES"))
    if raw is None or not str(raw).strip():
        return DEFAULT_JITTER_MINUTES
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_JITTER_MINUTES
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
        return f"每天 {plan.get('time') or ''}（不抖动）"
    return (f"每天 {plan.get('time') or ''}（今天落在 {due}，"
            f"抖动 ±{plan.get('jitter_minutes')} 分钟）")


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
