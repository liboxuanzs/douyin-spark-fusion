"""仪表盘的**只读**数据层：账本 / 冷却状态 / 运行锁 / 账号与配置快照。

硬规矩（会被验收）：

* 本模块**只读**，一个字节都不写。运行锁由 `core.run_ledger.RunLock` 自己用
  `O_CREAT|O_EXCL` 原子创建 —— 面板**不许**替它先写一把：那样 `runTasks`
  会把面板自己当成"另一个正在跑的任务"而跳过整轮（2026-09-30 真机踩过）。
* 账本可能不存在、可能是半行、可能混着坏 JSON —— 一律容忍：坏行跳过并计数，绝不抛异常。
* ``utils.config.get_userData()`` / ``get_config()`` 只在**函数内部**惰性调用
  （``core.tasks`` 是模块级快照，import 即求值，这里必须绕开）。
* **cookie 的值永远不出现在任何返回值里** —— 只返回"配置了几项 / 缺哪几项"。
"""

from __future__ import annotations

import errno
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from webui import config

__all__ = [
    "LOCK_STALE_AFTER",
    "AccountSnapshot",
    "ConfigSnapshot",
    "LedgerRead",
    "RateLimitState",
    "RunLockState",
    "RunRecord",
    "account_snapshots",
    "config_snapshot",
    "display_name",
    "fmt_duration",
    "fmt_stamp",
    "gate_label",
    "is_truthy_skip",
    "read_ledger",
    "read_lock",
    "read_rate_limit",
    "read_tasks_env",
    "status_label",
    "target_label",
    "timer_state",
]

# 锁文件里 pid 判断不出来时的兜底：started_at 距今超过这个时长就算过期。
LOCK_STALE_AFTER = timedelta(hours=2)

# 一次 overview 最多带出多少条运行记录 / 单条记录最多多少条明细。
MAX_RECORDS = 20
MAX_ENTRIES = 300

GATE_LABELS = {
    "READY": "正常",
    "LOGGED_OUT": "未登录",
    "EXPIRED": "登录失效",
    "LOGIN_LOST": "掉登录",
    "TIMEOUT": "等待超时",
    "ERROR": "内部错误",
    "UNKNOWN": "未知",
}

STATUS_LABELS = {
    "ok": "成功",
    "skipped": "跳过",
    "error": "失败",
    "pending": "进行中",
}

STATUS_ORDER = {"ok": 0, "pending": 1, "skipped": 2, "error": 3}

# 日志里唯一会被优先红掉的东西：URL 形式的密码。
_URL_CRED_RE = re.compile(r"://([^:/@\s]+):([^@/\s]+)@")


# ---------------------------------------------------------------- 基础工具


def _is_blank(value: Any) -> bool:
    """``None`` / 空串 / 空容器都算"没有"。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _text(value: Any) -> str:
    """把任意值安全地变成一行文本（不抛异常）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _num(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _load_json(path: Path) -> Any:
    """读一个 JSON 文件；不存在 / 坏 JSON / 权限问题都返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def parse_iso(value: Any) -> Optional[datetime]:
    """宽松解析 ISO 时间串；解析不了返回 None（绝不抛异常）。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.load_timezone())
    return parsed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_stamp(value: Any, tz=None) -> str:
    """转成展示用的本地时间串（``2026-10-01 09:12:33``）；解析不了就原样返回。

    ``tz`` 只接受真正的 ``tzinfo``：模板漏传、或传进 Jinja2 的 ``Undefined``
    （宏作用域里取不到上下文变量时就是这样）都回落到默认时区。展示层的小错不该
    让整页 500 —— 以前这里只判 ``tz is not None``，``Undefined`` 会一路传进
    ``astimezone`` 并炸出 ``TypeError: tzinfo argument must be None or of a
    tzinfo subclass, not type 'Undefined'``。
    """
    parsed = parse_iso(value)
    if parsed is None:
        return _text(value)
    zone = tz if isinstance(tz, tzinfo) else config.load_timezone()
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "-"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def target_label(value: Any) -> str:
    """配置里的一项发送目标 → 显示用的一行文本。

    `TASKS[].targets` 有两种形态：手写的字符串，以及面板勾选好友列表写入的
    `{"name", "uid", "conv_id"}`。后者要显示名字，不能把整个 dict 塞进页面
    （`_text()` 会把它变成一段 JSON，界面上就没法看了）。
    """
    if isinstance(value, dict):
        for key in ("name", "display", "title", "conv_id", "uid"):
            text = _text(value.get(key)).strip()
            if text:
                return text
        return ""
    return _text(value).strip()


def display_name(value: Any, fallback: str = "未知") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def gate_label(value: Any) -> str:
    key = str(value or "").strip().upper()
    if not key:
        return "未记录"
    return GATE_LABELS.get(key, key)


def status_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return "未记录"
    return STATUS_LABELS.get(key, key)


def is_truthy_skip(skipped: Any) -> bool:
    """``skipped`` 非空即表示整轮被跳过（账本里它可能是字符串，也可能是对象）。"""
    if skipped is None or skipped is False:
        return False
    if isinstance(skipped, str):
        return skipped.strip() != ""
    if isinstance(skipped, (list, tuple, dict)):
        return len(skipped) > 0
    return True


# ---------------------------------------------------------------- 脱敏


def mask_proxy(value: Any) -> str:
    """代理地址脱敏：保留 scheme/主机/端口，账号密码换成 ``***``。"""
    text = _text(value).strip()
    if not text:
        return ""
    return _URL_CRED_RE.sub(r"://\1:***@", text)


# ---------------------------------------------------------------- 运行锁


@dataclass
class RunLockState:
    """运行锁的解读结果。"""

    exists: bool = False
    running: bool = False
    pid: Optional[int] = None
    pid_alive: Optional[bool] = None
    started_at: Optional[str] = None
    dry_run: str = "off"
    source: str = ""
    run_id: str = ""
    reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "exists": self.exists,
            "running": self.running,
            "pid": self.pid,
            "pid_alive": self.pid_alive,
            "started_at": self.started_at,
            "started_at_text": fmt_stamp(self.started_at) if self.started_at else "",
            "dry_run": self.dry_run,
            "source": self.source,
            "run_id": self.run_id,
            "reason": self.reason,
        }


def _pid_alive_windows(pid: int) -> Optional[bool]:
    """Windows 的进程存活判断：``OpenProcess`` + ``GetExitCodeProcess``。

    为什么不用 ``os.kill(pid, 0)``：它在 Windows 上没有 POSIX 语义。实测（Python 3.12
    + Win11）对**活着的自己**抛 ``OSError errno=22 winerror=87 参数错误。``，
    对**不存在的 pid** 抛 ``OSError errno=8 winerror=11 试图加载格式不正确的程序。``
    —— 既不表示"活着"也不表示"死了"，错误码还和进程是否存在对不上（同样一个死 pid，
    两次调用的 winerror 都能不一样）。以前把它当 POSIX 用（只认 ``ProcessLookupError``），
    残留锁会被判成"判断不了"，再被"不足 2 小时就算在跑"的保守规则兜成"正在运行"，
    于是一个早已退出的进程能永远挡住手动触发（HTTP 409）。

    这里直接问内核，三个结果都能确定：

    * 拿不到句柄且 ``GetLastError == ERROR_INVALID_PARAMETER(87)`` → 没有这个进程；
    * 拿不到句柄且 ``ERROR_ACCESS_DENIED(5)``（例如系统进程）→ 存在，保守当成活着；
    * 拿到句柄 → 看退出码是不是 ``STILL_ACTIVE(259)``。

    ``ctypes`` 是标准库，不引入新依赖；任何意外都返回 ``None``（交给调用方兜底）。
    """
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        return None
    handle = kernel32.OpenProcess(
        process_query_limited_information | synchronize, False, int(pid)
    )
    if not handle:
        last_error = ctypes.get_last_error()
        if last_error == error_invalid_parameter:
            return False
        if last_error == error_access_denied:
            return True
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == still_active
    except Exception:
        return None
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def pid_alive(pid: Any) -> Optional[bool]:
    """判断 pid 是否还活着。

    返回 ``None`` 表示"判断不了"（pid 非法 / 平台不支持）—— 调用方按 started_at 兜底。

    POSIX 用 ``os.kill(pid, 0)``（WinError 87 那个坑见 ``_pid_alive_windows`` 的说明）；
    Windows 走 ``_pid_alive_windows``，因为 ``os.kill(pid, 0)`` 在那边完全不可用。
    """
    value = _num(pid)
    if value is None or value <= 0:
        return None
    if os.name == "nt":
        return _pid_alive_windows(value)
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不属于当前用户 —— 也算活着。
        return True
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ESRCH:
            return False
        if getattr(exc, "errno", None) == errno.EPERM:
            return True
        return None
    except (ValueError, AttributeError):
        return None
    except Exception:
        return None
    return True


def read_lock(path: Optional[Path] = None) -> RunLockState:
    """读运行锁。

    判定"正在跑"：文件存在且（pid 仍存活 **或** 无法判断 pid 时 started_at 距今 < 2 小时）。
    """
    target = Path(path) if path is not None else config.run_lock_file()
    state = RunLockState()
    if not target.exists():
        state.reason = "锁文件不存在"
        return state

    state.exists = True
    payload = _load_json(target)
    if not isinstance(payload, dict):
        state.reason = "锁文件内容不是合法 JSON"
        return state

    state.raw = payload
    state.pid = _num(payload.get("pid"))
    state.started_at = _text(payload.get("started_at")) or None
    state.dry_run = (_text(payload.get("dry_run")) or "off").lower()
    state.source = _text(payload.get("source"))
    state.run_id = _text(payload.get("run_id"))
    state.pid_alive = pid_alive(state.pid)
    if state.pid_alive is False:
        state.pid = None

    if state.pid_alive is True:
        state.running = True
        state.reason = f"pid {state.pid} 仍在运行"
        return state

    if state.pid_alive is None:
        started = parse_iso(state.started_at)
        if started is None:
            state.reason = "无法判断 pid 存活且没有可用时间戳，保守视为未在运行"
            return state
        age = _now() - started.astimezone(timezone.utc)
        if age < LOCK_STALE_AFTER:
            state.running = True
            state.reason = f"pid 无法判断，锁文件距今 {fmt_duration(age.total_seconds())}（< 2 小时），视为正在运行"
        else:
            state.reason = f"锁文件已过期（距今 {fmt_duration(age.total_seconds())}）"
        return state

    state.reason = "持有锁的进程已退出，锁文件是残留"
    return state


# 说明（**F9**，2026-10-01 对抗式审查）：这里原本有个 `write_lock()`，面板"立即运行"时
# 先自己写一把运行锁，随后 `core.tasks.runTasks` 用 `O_CREAT|O_EXCL` 建锁时发现
# "已有活人持锁" → 整轮被跳过，日志里是"已有一轮任务在运行（来源=webui）"。
# 两半各自都"对"，合起来就是"面板永远跑不起来"。现在锁只有一个持有者（core），
# 所以这个函数被删掉了 —— **别再把它加回来**：面板要判断"能不能跑"只读 `read_lock()`。


# ---------------------------------------------------------------- 熔断 / 冷却


@dataclass
class RateLimitState:
    """熔断状态。``exists`` 表示文件在，``tripped`` 表示仍在冷却期内。"""

    exists: bool = False
    tripped: bool = False
    active: bool = False
    expired: bool = False
    keyword: str = ""
    source: str = ""
    target: str = ""
    at: str = ""
    until: str = ""
    until_text: str = ""
    seconds_left: Optional[int] = None
    run_id: str = ""
    note: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "exists": self.exists,
            "tripped": self.tripped,
            "active": self.active,
            "expired": self.expired,
            "keyword": self.keyword,
            "source": self.source,
            "target": self.target,
            "at": self.at,
            "at_text": fmt_stamp(self.at) if self.at else "",
            "until": self.until,
            "until_text": self.until_text,
            "seconds_left": self.seconds_left,
            "run_id": self.run_id,
            "note": self.note or "",
        }


def read_rate_limit(path: Optional[Path] = None) -> RateLimitState:
    """读冷却状态。文件不存在 = 从未熔断；``cooldown_until`` 已过期 = 冷却已结束。"""
    target = Path(path) if path is not None else config.rate_limit_file()
    state = RateLimitState()
    if not target.exists():
        return state

    payload = _load_json(target)
    if not isinstance(payload, dict):
        state.exists = True
        state.note = "冷却状态文件内容不是合法 JSON，已忽略"
        return state

    state.exists = True
    state.raw = payload
    state.tripped = True
    state.keyword = _text(payload.get("keyword"))
    state.source = _text(payload.get("source"))
    state.target = _text(payload.get("target"))
    state.at = _text(payload.get("tripped_at"))
    state.until = _text(payload.get("cooldown_until")) or _text(payload.get("until"))
    state.run_id = _text(payload.get("run_id"))
    state.note = _text(payload.get("note"))
    state.until_text = fmt_stamp(state.until) if state.until else ""

    deadline = parse_iso(state.until)
    if deadline is None:
        # 没有截止时间就没法说"还在冷却"，保守按活动处理。
        state.active = True
        state.seconds_left = None
        return state

    seconds_left = int((deadline.astimezone(timezone.utc) - _now()).total_seconds())
    if seconds_left > 0:
        state.active = True
        state.seconds_left = seconds_left
    else:
        state.expired = True
        state.seconds_left = 0
    return state


# ---------------------------------------------------------------- 运行账本


@dataclass
class LedgerRead:
    """账本读取结果：``records`` 倒序（最新在前），``file`` 为 None 表示没找到文件。"""

    records: List[Dict[str, Any]] = field(default_factory=list)
    bad_lines: int = 0
    truncated: bool = False
    file: Optional[str] = None

    @property
    def count(self) -> int:
        return len(self.records)


def _ledger_candidates(path: Optional[Path] = None) -> List[Path]:
    if path is not None:
        return [Path(path)]
    primary = config.ledger_file()
    candidates = [primary]
    # 另一个 agent 可能在写别的路径；多试一个常见名，纯读、零副作用。
    alternate = primary.with_name("runs.json")
    if alternate.exists():
        candidates.append(alternate)
    return candidates


def read_ledger(
    path: Optional[Path] = None, *, limit: int = MAX_RECORDS, tail_bytes: int = 4 * 1024 * 1024
) -> LedgerRead:
    """从**尾部**读账本，最多 ``limit`` 条；坏行跳过并计数。

    只读文件末尾若干 MB：账本可能长到几百 MB，全读进来毫无必要。
    """
    result = LedgerRead()
    target = None
    for candidate in _ledger_candidates(path):
        if candidate.exists():
            target = candidate
            break
    if target is None:
        return result
    result.file = str(target)

    try:
        size = target.stat().st_size
        with open(target, "rb") as handle:
            start = max(0, size - max(64 * 1024, int(tail_bytes)))
            if start:
                result.truncated = True
            handle.seek(start)
            blob = handle.read()
    except OSError:
        return result

    text = blob.decode("utf-8", "replace")
    lines = text.splitlines()
    if result.truncated and lines:
        lines = lines[1:]  # 首行可能被截断，丢掉

    records: List[Dict[str, Any]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            result.bad_lines += 1
            continue
        if isinstance(payload, dict):
            records.append(payload)
        else:
            result.bad_lines += 1

    records.reverse()  # 最新的排前面
    result.records = records[:limit] if limit and limit > 0 else records
    return result


# ---------------------------------------------------------------- 运行记录视图


@dataclass
class RunRecord:
    """账本一条记录归一化后的视图（UI 直接渲染这个）。"""

    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    started_text: str = ""
    finished_text: str = ""
    dry_run: str = "off"
    source: str = ""
    source_text: str = ""
    rate_limited: bool = False
    status: str = "ok"
    status_text: str = "成功"
    skipped: bool = False
    skip_reason: str = ""
    skip_until: str = ""
    limit: Dict[str, Any] = field(default_factory=dict)
    note: str = ""
    accounts: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    duration_seconds: Optional[float] = None
    duration_text: str = "-"
    raw: Dict[str, Any] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        """给前端轮询用的精简版（明细按需再取）。"""
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "started_text": self.started_text,
            "finished_at": self.finished_at,
            "finished_text": self.finished_text,
            "dry_run": self.dry_run,
            "source": self.source,
            "source_text": self.source_text,
            "rate_limited": self.rate_limited,
            "status": self.status,
            "status_text": self.status_text,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "skip_until": self.skip_until,
            "limit": self.limit,
            "note": self.note,
            "accounts": self.accounts,
            "counts": self.counts,
            "duration_seconds": self.duration_seconds,
            "duration_text": self.duration_text,
        }


# 触发来源的中文标签：命令行 / 面板 / 定时器（外加人随手写的常见称呼）
SOURCE_LABELS = {
    "cli": "命令行",
    "webui": "面板触发",
    "timer": "定时任务",
    # `RUN_SOURCE=manual` 是"人手动跑一轮验收"时最顺手的写法；认不出来的值会
    # 原样显示成英文，页面上很扎眼，所以常见称呼在这里顺手登记一份。
    "manual": "手动运行",
}


def source_text(source: str) -> str:
    s = (source or "").strip().lower()
    return SOURCE_LABELS.get(s, s or "—")


def _skip_info(skipped: Any) -> Tuple[str, str]:
    if isinstance(skipped, dict):
        reason = _text(skipped.get("reason")) or "skipped"
        until = _text(skipped.get("until")) or _text(skipped.get("cooldown_until"))
        return reason, until
    if isinstance(skipped, str):
        return skipped.strip(), ""
    return _text(skipped) or "skipped", ""


def normalize_run(raw: Dict[str, Any], tz=None) -> RunRecord:
    """把账本里的一行归一化成 UI 视图。所有字段都做类型防御。"""
    record = RunRecord(raw=raw if isinstance(raw, dict) else {})
    raw = record.raw
    record.run_id = _text(raw.get("run_id"))
    record.started_at = _text(raw.get("started_at"))
    record.finished_at = _text(raw.get("finished_at"))
    record.started_text = fmt_stamp(record.started_at, tz) if record.started_at else ""
    record.finished_text = fmt_stamp(record.finished_at, tz) if record.finished_at else ""
    record.dry_run = (_text(raw.get("dry_run")) or "off").lower()
    # 触发来源（cli / webui / timer）：区分"定时自己跑的"和"我在面板上点的"
    record.source = _text(raw.get("source")).lower()
    record.source_text = source_text(record.source)
    record.rate_limited = bool(raw.get("rate_limited"))

    limit_payload = raw.get("limit")
    if isinstance(limit_payload, dict) and any(not _is_blank(v) for v in limit_payload.values()):
        record.limit = {
            "keyword": _text(limit_payload.get("keyword")),
            "source": _text(limit_payload.get("source")),
            "target": _text(limit_payload.get("target")),
            "cooldown_until": _text(limit_payload.get("cooldown_until")),
            "cooldown_until_text": fmt_stamp(limit_payload.get("cooldown_until"), tz)
            if limit_payload.get("cooldown_until")
            else "",
        }

    raw_skipped = raw.get("skipped")
    record.skipped = is_truthy_skip(raw_skipped)
    if record.skipped:
        reason, until = _skip_info(raw_skipped)
        record.skip_reason = reason
        record.skip_until = fmt_stamp(until, tz) if until else ""

    accounts = raw.get("accounts")
    account_views: List[Dict[str, Any]] = []
    totals = {"sent_ok": 0, "sent_fail": 0, "missing": 0, "select_failed": 0, "entries": 0}
    # 账本自己写的 status=error 必须算失败：面板替 core 补记"这一轮没跑起来"
    # 的记录里 accounts 是空的，只看账号就永远推不出"有失败"，会被显示成成功。
    raw_status = (_text(raw.get("status")) or "").lower()
    failed = raw_status == "error"
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, dict):
                continue
            entries_raw = item.get("entries")
            entries: List[Dict[str, Any]] = []
            if isinstance(entries_raw, list):
                for entry in entries_raw[:MAX_ENTRIES]:
                    if not isinstance(entry, dict):
                        continue
                    entries.append(
                        {
                            "target": _text(entry.get("target")),
                            "ok": bool(entry.get("ok")),
                            "level": _text(entry.get("level")),
                            "reason": _text(entry.get("reason")),
                            "via": _text(entry.get("via")),
                            "message_id": _text(entry.get("message_id")),
                            "dry_run": _text(entry.get("dry_run")) or record.dry_run,
                            "rate_limited": bool(entry.get("rate_limited")),
                            "at": _text(entry.get("at")),
                            "at_text": fmt_stamp(entry.get("at"), tz) if entry.get("at") else "",
                        }
                    )
            total_entries = len(entries_raw) if isinstance(entries_raw, list) else len(entries)
            # 不认识的 status 不许默认成"成功"——那是最容易骗人的兜底值。
            # 原样留着，页面上按原码显示 + 黄色 chip（run_row 的 chip 表就是这么兜的）。
            status = (_text(item.get("status")) or "ok").lower()
            sent_ok = _num(item.get("sent_ok"))
            sent_fail = _num(item.get("sent_fail"))
            missing = item.get("missing") if isinstance(item.get("missing"), list) else []
            select_failed = (
                item.get("select_failed") if isinstance(item.get("select_failed"), list) else []
            )
            targets = item.get("targets") if isinstance(item.get("targets"), list) else []
            view = {
                "username": display_name(item.get("username"), "未命名账号"),
                "unique_id": _text(item.get("unique_id")),
                "status": status,
                "status_text": status_label(status),
                "gate": _text(item.get("gate")).upper(),
                "gate_text": gate_label(item.get("gate")),
                "sent_ok": sent_ok,
                "sent_fail": sent_fail,
                "scanned_all": item.get("scanned_all"),
                "steps": _num(item.get("steps")),
                "visited": _num(item.get("visited")),
                "missing": [_text(x) for x in missing],
                "select_failed": [_text(x) for x in select_failed],
                "targets": [_text(x) for x in targets],
                "entries": entries,
                "entries_total": total_entries,
                "entries_truncated": total_entries > len(entries),
            }
            account_views.append(view)
            totals["sent_ok"] += sent_ok or 0
            totals["sent_fail"] += sent_fail or 0
            totals["missing"] += len(missing)
            totals["select_failed"] += len(select_failed)
            totals["entries"] += total_entries
            if status == "error" or (sent_fail or 0) > 0:
                failed = True
    record.accounts = account_views

    if record.skipped:
        record.status = "skipped"
    elif record.rate_limited:
        record.status = "limited"
    elif failed:
        record.status = "error"
    elif not account_views:
        # 跑完一轮却没有任何账号结果：这不是"成功"。清掉已删账号的历史条目之后
        # 也会落到这里 —— 页面直说"没有账号结果"，不去猜原因。
        record.status = "empty"
    elif all(item["status"] == "skipped" for item in account_views) and totals["entries"] == 0:
        # 每个账号都被门禁拦下（登录失效 / 掉登录 / 超时），一条也没尝试过：
        # 以前这里显示绿色的「成功」，属于同一类"默认值太乐观"。
        record.status = "blocked"
    else:
        record.status = "ok"
    record.status_text = {
        "skipped": "整轮跳过",
        "limited": "命中熔断",
        "error": "有失败",
        "empty": "没有账号结果",
        "blocked": "账号全被跳过",
        "ok": "成功",
    }[record.status]

    # 账本自己写的 note 优先（"面板触发的一轮没能跑起来"、清理历史条目时补的说明…）。
    # 不再给"没有账号结果"的轮次猜原因：那句"账号凭据可能未配置"会把线程崩溃、
    # 被清掉的历史条目统统说成"你没配 cookie"，而状态本身已经把话说清楚了。
    record.note = _text(raw.get("note"))
    error_text = _text(raw.get("error"))
    if error_text and error_text not in record.note:
        record.note = f"{record.note} {error_text}".strip() if record.note else error_text

    started = parse_iso(record.started_at)
    finished = parse_iso(record.finished_at)
    if started is not None and finished is not None:
        seconds = (finished.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        if seconds >= 0:
            record.duration_seconds = round(seconds, 1)
            record.duration_text = fmt_duration(seconds)

    record.counts = {
        "accounts": len(record.accounts),
        "sent_ok": totals["sent_ok"],
        "sent_fail": totals["sent_fail"],
        "missing": totals["missing"],
        "select_failed": totals["select_failed"],
        "entries": totals["entries"],
    }
    return record


# ---------------------------------------------------------------- 账号与配置快照


def read_tasks_env() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """只读 ``TASKS`` 环境变量（不碰 COOKIES_*，避免读到凭据）。

    返回 ``(任务列表, 错误说明)``。解析失败不抛异常，而是把原因带出去给页面显示。
    """
    raw = os.getenv("TASKS", "")
    if not raw.strip():
        return [], None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return [], f"TASKS 不是合法 JSON：{exc}"
    if not isinstance(payload, list):
        return [], "TASKS 不是 JSON 数组"
    return [item for item in payload if isinstance(item, dict)], None


def cookie_status(unique_id: str) -> Dict[str, Any]:
    """cookie 配置状态 —— **只回报名字，绝不回报值**。

    复刻 ``utils.config.get_userData()`` 的取键与解码口径：
    ``COOKIES_<unique_id>`` 大写，值先 ``unicode_escape`` 再 ``json.loads``。
    """
    result: Dict[str, Any] = {
        "configured": 0,
        "names": [],
        "missing_key": False,
        "broken": False,
        "message": "",
    }
    key = f"cookies_{unique_id}".upper()
    raw = os.getenv(key, "")
    if not raw:
        result["missing_key"] = True
        result["message"] = f"缺少环境变量 {key}"
        return result
    try:
        decoded = raw.encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        decoded = raw
    try:
        payload = json.loads(decoded)
    except ValueError:
        result["broken"] = True
        result["message"] = f"{key} 不是合法 JSON 数组"
        return result
    if not isinstance(payload, list):
        result["broken"] = True
        result["message"] = f"{key} 不是 JSON 数组"
        return result
    names = []
    for item in payload:
        if isinstance(item, dict):
            name = _text(item.get("name"))
            if name:
                names.append(name)
    result["configured"] = len(payload)
    result["names"] = names
    return result


def _load_userdata() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """惰性调用 ``utils.config.get_userData()``（会读 TASKS + COOKIES_*）。"""
    refresh_env()
    try:
        from utils.config import get_userData  # 函数内 import：import 期不产生副作用
    except Exception as exc:  # pragma: no cover - 只在 utils 被破坏时发生
        return [], f"无法导入 utils.config：{exc}"
    try:
        data = get_userData()
    except Exception as exc:
        return [], f"读取账号配置失败：{type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return [], "账号配置格式异常"
    return [item for item in data if isinstance(item, dict)], None


@dataclass
class AccountSnapshot:
    """一个账号的配置态 + 上次运行态（不含任何 cookie 值）。"""

    username: str
    unique_id: str
    fingerprint: str = ""
    target_count: int = 0
    targets: List[str] = field(default_factory=list)
    cookie_configured: int = 0
    cookie_names: List[str] = field(default_factory=list)
    cookie_message: str = ""
    # 这个账号专属的发送内容（`TASKS[].message`）。空串 = 没配过，走全局模板。
    message: str = ""
    # 这个账号专属的发送时间（`TASKS[].time`，"HH:MM"）。空串 = 没配过，
    # 跟 `.env` 的 `TASK_SCHEDULE_TIMES`。**存原始值**，解析/展示交给
    # `core.schedule`（解析规则只有那一份）。
    send_time: str = ""
    ready: bool = False
    gate: str = ""
    gate_text: str = "未记录"
    sent_ok: Optional[int] = None
    sent_fail: Optional[int] = None
    missing: List[str] = field(default_factory=list)
    select_failed: List[str] = field(default_factory=list)
    last_run_id: str = ""
    last_run_text: str = ""
    status: str = ""
    status_text: str = "未运行"
    # 只出现在上一轮账本里、`TASKS` 里已经没有它了（用户删了账号，或临时改过 .env）。
    # 面板要把它和"配置里的账号"区分开：它不会被下一轮处理，也没有可移除的配置。
    orphan: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "username": self.username,
            "unique_id": self.unique_id,
            "fingerprint": self.fingerprint,
            "target_count": self.target_count,
            "targets": self.targets,
            "cookie_configured": self.cookie_configured,
            "cookie_names": self.cookie_names,
            "cookie_message": self.cookie_message,
            "message": self.message,
            "send_time": self.send_time,
            "ready": self.ready,
            "gate": self.gate,
            "gate_text": self.gate_text,
            "sent_ok": self.sent_ok,
            "sent_fail": self.sent_fail,
            "missing": self.missing,
            "select_failed": self.select_failed,
            "last_run_id": self.last_run_id,
            "last_run_text": self.last_run_text,
            "status": self.status,
            "status_text": self.status_text,
            "orphan": self.orphan,
        }


def account_snapshots(records: Optional[Iterable[RunRecord]] = None) -> List[AccountSnapshot]:
    """合并"配置里的账号"与"最近一次有结果的运行"，生成账号卡数据。

    以最近一条**带账号结果**的运行为准（整轮被跳过的记录不含有效门禁信息，跳过它往前找）。
    """
    # 先按 .env 的现状刷新一次环境再读 TASKS：`read_tasks_env` 读的是
    # `os.getenv`，而 `_load_userdata()` 内部（`webui/store.py:885`）才调
    # `refresh_env()`。顺序反过来就会"面板外刚改完 .env，第一帧还按旧账号库建卡"。
    refresh_env()
    tasks, tasks_error = read_tasks_env()
    userdata, userdata_error = _load_userdata()
    configured = {str(item.get("unique_id") or ""): item for item in userdata}

    by_unique: Dict[str, Dict[str, Any]] = {}
    own_run: Dict[str, Dict[str, Any]] = {}
    last_run: Dict[str, Any] = {}
    if records:
        for record in records:
            if not record.accounts:
                continue
            stamp = {
                "run_id": record.run_id,
                "text": record.started_text or record.run_id,
            }
            if not last_run:
                last_run = dict(stamp)
            for account in record.accounts:
                key = account.get("unique_id") or account.get("username")
                key = str(key) if key else ""
                if not key or key in by_unique:
                    continue
                # 每个账号记**自己**的最近一次：一个账号可能只出现在一部分轮次里
                # （每个账号可以有自己的发送时间，见 core/schedule.py，派发器每 5
                # 分钟只为当时到点的账号跑一轮）。原来这里是"第一条带账号的记录"
                # 然后 break，于是没赶上最近那一轮的账号，门禁/状态/上次运行全空 ——
                # 2026-10-07 线上 wheat 那张卡就这么成了"未运行"。
                by_unique[key] = account
                own_run[key] = dict(stamp)

    seen: set = set()
    snapshots: List[AccountSnapshot] = []

    def build(username: str, unique_id: str, fingerprint: str, targets: List[str],
              message: str = "", send_time: str = "") -> AccountSnapshot:
        if unique_id:
            cookies: Dict[str, Any] = cookie_status(unique_id)
        else:
            cookies = {
                "configured": 0,
                "names": [],
                "message": "配置里缺少 unique_id，无法定位 COOKIES_<unique_id>",
            }
        snapshot = AccountSnapshot(
            username=display_name(username, "未命名账号"),
            unique_id=unique_id,
            fingerprint=_text(fingerprint),
            target_count=len(targets),
            targets=[target_label(item) for item in targets],
            cookie_configured=int(cookies.get("configured") or 0),
            cookie_names=list(cookies.get("names") or []),
            cookie_message=_text(cookies.get("message")),
            message=_text(message),
            send_time=_text(send_time),
        )
        snapshot.ready = snapshot.cookie_configured > 0
        if not snapshot.cookie_message and not snapshot.ready:
            snapshot.cookie_message = "未配置 cookie，该账号本轮会被跳过"
        return snapshot

    # 1) 配置里的账号（TASKS 是权威列表）
    for item in tasks:
        unique_id = _text(item.get("unique_id"))
        username = display_name(item.get("username"), "未命名账号")
        targets = item.get("targets") if isinstance(item.get("targets"), list) else []
        snapshot = build(username, unique_id, _text(item.get("fingerprint")), targets,
                         _text(item.get("message")), _text(item.get("time")))
        snapshots.append(snapshot)
        if unique_id:
            seen.add(unique_id)
        seen.add(snapshot.username)

    if not tasks:
        for unique_id, item in configured.items():
            snapshot = build(
                display_name(item.get("username"), "未命名账号"),
                unique_id,
                _text(item.get("fingerprint")),
                list(item.get("targets") or []),
                _text(item.get("message")),
                _text(item.get("time")),
            )
            if not snapshot.cookie_message and tasks_error:
                snapshot.cookie_message = tasks_error
            snapshots.append(snapshot)
            if unique_id:
                seen.add(unique_id)
            seen.add(snapshot.username)

    # 2) 上次运行里出现、但配置里没有的账号（例如临时改了 .env）
    for key, account in by_unique.items():
        username = display_name(account.get("username"), "未命名账号")
        unique_id = _text(account.get("unique_id"))
        if key in seen or unique_id in seen or username in seen:
            continue
        # 账本里这一条也带 targets，但它是**上一轮跑过的**目标，不是现在要发的；
        # 面板上不再给它勾选入口，所以这里只带名字、不带目标。
        snapshot = build(username, unique_id, "", [])
        # 它不在 TASKS 里 —— 下一轮根本不会处理它，面板必须说清楚，
        # 否则用户会对着这张卡点「移除」，拿到"TASKS 里没有 X"却看不出为什么。
        snapshot.orphan = True
        if not snapshot.cookie_message:
            snapshot.cookie_message = "只在上一轮的运行记录里，TASKS 里已经没有它了"
        snapshots.append(snapshot)

    # 3) 回填上次运行的门禁与计数
    for snapshot in snapshots:
        key = snapshot.unique_id or snapshot.username
        account = by_unique.get(key) or by_unique.get(snapshot.username)
        if account is None:
            if tasks_error:
                snapshot.status_text = tasks_error
            continue
        snapshot.gate = _text(account.get("gate")).upper()
        snapshot.gate_text = _text(account.get("gate_text")) or gate_label(snapshot.gate)
        snapshot.sent_ok = account.get("sent_ok")
        snapshot.sent_fail = account.get("sent_fail")
        snapshot.missing = list(account.get("missing") or [])
        snapshot.select_failed = list(account.get("select_failed") or [])
        if not snapshot.targets and account.get("targets"):
            snapshot.targets = list(account["targets"])
            snapshot.target_count = len(snapshot.targets)
        snapshot.status = _text(account.get("status"))
        snapshot.status_text = _text(account.get("status_text")) or status_label(snapshot.status)
        # 「上次运行」也按账号自己的那一轮算，不然每个账号都挂着全局最近一轮的 ID。
        stamp = own_run.get(key) or own_run.get(snapshot.username) or last_run
        snapshot.last_run_id = _text(stamp.get("run_id"))
        snapshot.last_run_text = _text(stamp.get("text"))

    if userdata_error and not snapshots:
        snapshots.append(
            AccountSnapshot(
                username="（配置读取失败）",
                unique_id="",
                status_text=userdata_error,
                cookie_message=userdata_error,
            )
        )
    return snapshots


# ---------------------------------------------------------------- 配置快照（只读）


@dataclass
class ConfigSnapshot:
    """给页面看的只读配置。**没有**任何写入口，也不回显 cookie 值。"""

    message_template: str = ""
    hitokoto_types: List[str] = field(default_factory=list)
    proxy_masked: str = ""
    proxy_configured: bool = False
    browser_action_timeout: str = ""
    im_scan_timeout: str = ""
    im_ready_timeout: str = ""
    friend_list_settle: str = ""
    im_max_steps: str = ""
    task_retry_times: str = ""
    log_level: str = ""
    debug: bool = False
    tz: str = ""
    schedule_text: str = ""
    tasks_error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "message_template": self.message_template,
            "hitokoto_types": self.hitokoto_types,
            "proxy_masked": self.proxy_masked,
            "proxy_configured": self.proxy_configured,
            "browser_action_timeout": self.browser_action_timeout,
            "im_scan_timeout": self.im_scan_timeout,
            "im_ready_timeout": self.im_ready_timeout,
            "friend_list_settle": self.friend_list_settle,
            "im_max_steps": self.im_max_steps,
            "task_retry_times": self.task_retry_times,
            "log_level": self.log_level,
            "debug": self.debug,
            "tz": self.tz,
            "schedule_text": self.schedule_text,
            "tasks_error": self.tasks_error,
        }


def refresh_env() -> bool:
    """让"改了 `.env` 就生效"在常驻进程里成立（见 `utils.config.reload_env`）。

    没有这一步就会出现这样一幕：用户在 `.env` 里填好真实 `TASKS` /
    `COOKIES_<uid>`，回到面板点"立即运行" —— 跑的还是进程启动那一刻的
    旧值（`get_config()` / `get_userData()` 是首次调用即冻结的模块级快照），
    面板上也还列着旧账号，看起来就是"配置改了没用"。

    每请求调一次的成本 = 读一个小文件 + 清两个缓存，可以忽略。
    任何异常都吞掉：读不到配置只该让页面显示旧值，不该整页 500。
    """
    try:
        from utils.config import reload_env  # 函数内 import：import 期不产生副作用
    except Exception:
        return False
    try:
        return bool(reload_env())
    except Exception:
        return False


def _parse_on_calendar(text: str) -> List[str]:
    """从 timer 单元里抠出 ``HH:MM``。``OnCalendar=*-*-* 12:30:00`` → ``["12:30"]``。

    只认这种"每天几点"的写法；``Mon..Fri``／``*-*-01`` 之类认不出来就返回空列表，
    面板宁可不显示，也不猜一个错的时间给用户看。
    """
    found: List[str] = []
    for raw in _text(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            # 允许传进来的是完整的 unit 文本，也允许只传值
            pass
        value = line.split("=", 1)[1].strip() if "=" in line else line
        if "=" in line and not line.split("=", 1)[0].strip().lower().startswith("oncalendar"):
            continue
        parts = value.split()
        if len(parts) != 2 or parts[0] != "*-*-*":
            continue
        clock = parts[1].split(":")
        if len(clock) < 2 or not all(part.isdigit() for part in clock[:2]):
            continue
        stamp = f"{int(clock[0]):02d}:{int(clock[1]):02d}"
        if stamp not in found:
            found.append(stamp)
    return found


def _parse_dispatch(text: str) -> int:
    """``OnCalendar=*:0/5`` → ``5``（分钟）；不是派发器写法就返回 0。

    新架构里 timer 只负责敲门，**几点发由应用按每个账号的时间判定**
    （`core/schedule.py`）。面板必须分得清这两种 timer，否则会把
    "每 5 分钟一次的心跳"当成"每天的发车时间"画给用户看。
    """
    for raw in _text(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().lower() != "oncalendar":
            continue
        m = re.match(r"^\*:0/(\d+)$", value.strip())
        if m:
            step = int(m.group(1))
            if step > 0:
                return step
    return 0


def _parse_delay(text: str) -> str:
    """``RandomizedDelaySec=20m`` → ``"最多 20 分钟"``（认不出来就返回原始写法）。"""
    value = _text(text).strip().lower()
    if not value or value in ("0", "0s"):
        return ""
    unit_names = {"s": "秒", "m": "分钟", "h": "小时", "min": "分钟", "sec": "秒"}
    for suffix in ("min", "sec", "h", "m", "s"):
        if value.endswith(suffix) and value[: -len(suffix)].strip().isdigit():
            number = int(value[: -len(suffix)].strip())
            return f"最多 {number} {unit_names[suffix]}"
    return value


def timer_state(
    name: str = "douyin-spark-fusion-daily.timer",
    *,
    unit_root: str = "/etc/systemd/system",
) -> Dict[str, Any]:
    """面板上写的"下次自动触发"到底会不会发生？——看 systemd 定时器的真实配置。

    这是真机上抓到的一处"面板一套、实际一套"：`.env` 里明明记着 `12:30`，
    面板也画着"下次自动触发 10/02 12:30"，可 `douyin-spark-fusion-daily.timer`
    **是 disabled 的** —— 到点什么都不会发生。

    只读文件系统、不调 systemctl（面板进程不一定是 root，也不该依赖 dbus）：
    systemd 的"启用"就是在 `timers.target.wants/` 下建一个软链接；真正几点跑
    写在单元文件的 ``OnCalendar`` 里，所以这里把它也读出来，好和 `.env` 对账。
    """
    unit = _text(name) or "douyin-spark-fusion-daily.timer"
    info: Dict[str, Any] = {
        "name": unit,
        "installed": False,
        "enabled": False,
        "state": "unknown",
        "onCalendar": [],
        "jitterText": "",
        # `OnCalendar=*:0/5`（每 5 分钟敲一次门的**派发器**）→ 5；认不出来是 0。
        # 与 onCalendar 是两种互斥写法：前者代表"几点发由应用决定"，
        # 后者代表"timer 自己就是那个时间"（旧架构）。
        "dispatchMinutes": 0,
    }
    try:
        root = Path(unit_root)
        unit_file = root / unit
        wanted = root / "timers.target.wants" / unit
        info["installed"] = unit_file.exists()
        if wanted.exists() or wanted.is_symlink():
            info["enabled"] = True
        if info["installed"]:
            info["state"] = "enabled" if info["enabled"] else "disabled"
            try:
                text = unit_file.read_text(encoding="utf-8", errors="replace")
            except OSError:                                      # pragma: no cover - 权限
                text = ""
            info["onCalendar"] = _parse_on_calendar(text)
            info["dispatchMinutes"] = _parse_dispatch(text)
            for line in text.splitlines():
                key, _, value = line.partition("=")
                if key.strip().lower() == "randomizeddelaysec":
                    info["jitterText"] = _parse_delay(value)
                    break
        # 只有"真的跑在 systemd 主机上"才下结论说"没装"；开发机（Windows、容器）
        # 上没有 systemd 目录，保持 unknown —— 面板不该在那种地方报"你没装定时器"。
        elif Path("/run/systemd/system").exists():
            info["state"] = "missing"
    except OSError:                                              # pragma: no cover - 平台差异
        return info
    return info


def config_snapshot() -> ConfigSnapshot:
    """惰性读 ``utils.config.get_config()``，转成**只读**展示视图。"""
    refresh_env()
    snapshot = ConfigSnapshot()
    snapshot.tz = config.default_timezone()
    times = config.schedule_times()
    snapshot.schedule_text = "、".join(times) if times else "未配置"

    try:
        from utils.config import DEBUG, get_config  # 函数内 import
    except Exception as exc:  # pragma: no cover
        snapshot.tasks_error = f"无法导入 utils.config：{exc}"
        return snapshot

    try:
        payload = get_config() or {}
    except Exception as exc:
        snapshot.tasks_error = f"读取配置失败：{type(exc).__name__}: {exc}"
        return snapshot

    def value(key: str, default: str = "") -> str:
        return _text(payload.get(key, default))

    snapshot.message_template = value("messageTemplate")
    raw_types = payload.get("hitokotoTypes")
    snapshot.hitokoto_types = [_text(item) for item in raw_types] if isinstance(raw_types, list) else []
    proxy = _text(payload.get("proxyAddress"))
    snapshot.proxy_configured = bool(proxy.strip())
    snapshot.proxy_masked = mask_proxy(proxy) if proxy else "未配置"
    snapshot.browser_action_timeout = f"{value('browserActionTimeout')} 毫秒"
    snapshot.im_scan_timeout = f"{value('imScanTimeout')} 秒"
    snapshot.im_ready_timeout = f"{value('imReadyTimeout')} 秒"
    snapshot.friend_list_settle = f"{value('friendListSettleMs')} 毫秒"
    snapshot.im_max_steps = value("imMaxSteps")
    snapshot.task_retry_times = value("taskRetryTimes")
    snapshot.log_level = value("logLevel")
    snapshot.debug = bool(DEBUG)

    _tasks, tasks_error = read_tasks_env()
    snapshot.tasks_error = tasks_error or ""
    return snapshot
