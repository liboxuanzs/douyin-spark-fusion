# -*- coding: utf-8 -*-
"""静默体检：定时器还活着吗、账本还新吗、账号是不是全停用了。

issue #16：通知一直挂在「跑完一轮」上（core.notify.notify_run），而最该报警的那几种
情形恰好不会产生一轮 ——

* 定时器被停了 / 被 disable 了：面板上一片安静，谁也不会发现；
* 派发器天天每 5 分钟敲门，但「没有账号到点」的敲门按设计不进账本；
* TASKS 里唯一那个账号掉了登录 / 被停用，于是永远没有一轮真的跑完。

python main.py doctor 把这三件事读一遍，用退出码回答「有没有事」，用几行人话说明
「事在哪」。定时器（deploy/douyin-spark-fusion-doctor.timer）每天喊一次；有事才推
通知，并且每天最多推一条（记在 logs/doctor_state.json），不会把手机淹掉。

三条边界：

1. 不碰浏览器、不占运行锁、不写时间表 —— 它只读账本、读 systemd 状态、读 TASKS。
2. 读不到就说读不到：账本文件不存在是「⛔ 没有可读的账本」，不是「0 小时前刚跑过」。
3. 账号全停用不算静默：那是用户自己关掉的，天天报警只会让人把通知关掉。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from core.util_time import human_duration, iso, now, parse_iso

__all__ = [
    "DEFAULT_SILENCE_HOURS",
    "SILENCE_KEY",
    "TIMER_UNIT",
    "account_counts",
    "diagnose",
    "format_lines",
    "last_run",
    "main",
    "maybe_notify",
    "read_ledger_rows",
    "silence_hours",
    "timer_state",
]

#: 多久没有任何运行记录算「静默」。默认 30 小时：最短的账号间隔约 21 小时
#: （17:20 到次日 17:20，抖动负 15 分钟），留出一次失败重试的余量，又不至于把
#: 「今天晚发了十分钟」当成故障。
DEFAULT_SILENCE_HOURS = 30.0
#: 把这个设成 0 就关掉静默判定（但定时器与账本检查照做）。
SILENCE_KEY = "NOTIFY_SILENCE_HOURS"

#: 与 deploy/ 里的 unit 同名；改名要同步改 install.sh、docs/deploy.md。
TIMER_UNIT = "douyin-spark-fusion-daily.timer"

#: 体检状态（每天最多推一条通知）的位置。和账本一样按仓库根解析。
STATE_FILE = os.path.join("logs", "doctor_state.json")

#: 账本只读尾部这么多字节：runs.jsonl 一轮一条，180 轮也就 180 KB。
TAIL_BYTES = 256 * 1024

TIMER_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "UnitFileState",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
)


# ---------------------------------------------------------------------------
# 读配置
# ---------------------------------------------------------------------------


def silence_hours() -> float:
    """读 NOTIFY_SILENCE_HOURS。0 = 关掉静默判定；写错回落默认并告警。"""
    raw = (os.getenv(SILENCE_KEY) or "").strip()
    if not raw:
        return DEFAULT_SILENCE_HOURS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn(f"{SILENCE_KEY}={raw!r} 不是数字，按 {DEFAULT_SILENCE_HOURS:g} 小时算")
        return DEFAULT_SILENCE_HOURS
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        _warn(f"{SILENCE_KEY}={raw!r} 不是有限数字，按 {DEFAULT_SILENCE_HOURS:g} 小时算")
        return DEFAULT_SILENCE_HOURS
    return max(0.0, value)


def _warn(message: str) -> None:
    """体检本身不许因为日志系统没配好而失败：能 import 就用项目 logger，不能就 stderr。"""
    try:
        from utils.logger import setup_logger

        setup_logger("doctor").warning(message)
    except Exception:  # noqa: BLE001
        print(f"[体检] ⚠️ {message}", file=sys.stderr)


def _env_path(name: str, default: str) -> str:
    """按 .env 的既有约定解析路径：相对路径一律锚到仓库根。"""
    try:
        from utils.config import resolve_path

        return str(resolve_path(os.getenv(name, default)))
    except Exception:  # noqa: BLE001 —— 体检不该因为路径工具挂了而失败
        return os.getenv(name, default)


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------


def read_ledger_rows(path: Optional[str] = None, *, limit: int = 0,
                     tail_bytes: int = TAIL_BYTES) -> Dict[str, Any]:
    """从账本尾部读记录（最新在后）。返回 rows/path/exists/error/bad_lines。

    **不做归一化**：体检只关心时间戳，归一化是 webui 的事。
    坏行只计数，不让一行坏 JSON 把整次体检变成异常。
    """
    target = path or _env_path("RUN_LEDGER_FILE", os.path.join("logs", "runs.jsonl"))
    info: Dict[str, Any] = {
        "rows": [], "path": str(target), "exists": False,
        "error": "", "bad_lines": 0,
    }
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    info["exists"] = True
    try:
        with open(target, "rb") as handle:
            offset = max(0, int(size) - max(0, int(tail_bytes)))
            handle.seek(offset)
            blob = handle.read()
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    text = blob.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if offset > 0 and lines:
        # 从中间切进来时第一行一定是半截，直接丢 —— 它不是「坏行」。
        lines = lines[1:]
    rows: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            info["bad_lines"] += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            info["bad_lines"] += 1
    if limit > 0:
        rows = rows[-int(limit):]
    info["rows"] = rows
    return info


def _row_time(row: Dict[str, Any]) -> Optional[datetime]:
    for key in ("finished_at", "started_at"):
        stamp = parse_iso(row.get(key))
        if stamp is not None:
            return stamp
    return None


def last_run(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """最新一条记录（按时间戳，不按文件顺序 —— 手改过的账本也认）。"""
    best: Optional[Dict[str, Any]] = None
    best_time: Optional[datetime] = None
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        stamp = _row_time(row)
        if stamp is None:
            continue
        if best_time is None or stamp > best_time:
            best, best_time = row, stamp
    if best is None:
        return None
    return {"row": best, "at": best_time}


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------


def _systemctl_show(unit: str, properties: Sequence[str]) -> Optional[str]:
    """systemctl show 的原始输出；不可用（没有 systemd / 超时）返回 None。"""
    args = ["systemctl", "show", unit]
    for name in properties:
        args += ["-p", name]
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout or ""


def timer_state(unit: str = TIMER_UNIT, *,
                runner: Optional[Callable[[str, Sequence[str]], Optional[str]]] = None
                ) -> Dict[str, Any]:
    """定时器状态。读不到就明说不知道，别猜成「没启用」（那是假报警）。"""
    run = runner or (lambda u, p: _systemctl_show(u, p))
    out = run(unit, TIMER_PROPERTIES)
    empty: Dict[str, Any] = {
        "unit": unit, "known": False, "load": "", "active": "", "enabled": "",
        "last": "", "next": "", "error": "",
    }
    if not out:
        empty["error"] = "systemctl 不可用（不是 systemd 部署？）"
        return empty
    props: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    load = props.get("LoadState", "")
    if load in ("", "not-found"):
        empty["load"] = load
        empty["error"] = f"没有这个单元（LoadState={load or '未知'}）"
        return empty
    return {
        "unit": unit,
        "known": True,
        "load": load,
        "active": props.get("ActiveState", ""),
        "enabled": props.get("UnitFileState", ""),
        # 这两个时间戳是 systemd 自己的格式（Fri 2026-10-24 17:20:00 CST），
        # 只原样展示给人看：解析它要跟时区缩写打交道，而精确的「多久没跑」账本里就有。
        "last": props.get("LastTriggerUSec", ""),
        "next": props.get("NextElapseUSecRealtime", ""),
        "error": "",
    }


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


def account_counts(users: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """TASKS 里有多少账号、几个在用、几个停用。读配置失败就返回 error。"""
    if users is None:
        try:
            from utils.config import get_userData

            users = list(get_userData() or ())
        except Exception as exc:  # noqa: BLE001
            return {"total": 0, "active": 0, "stopped": 0,
                    "error": f"{type(exc).__name__}: {exc}"}
    try:
        from core.schedule import user_enabled
    except Exception:  # noqa: BLE001
        def user_enabled(user):  # type: ignore[misc]
            return bool((user or {}).get("enabled", True))

    items = list(users or ())
    active = sum(1 for item in items if user_enabled(item))
    return {"total": len(items), "active": active,
            "stopped": len(items) - active, "error": ""}


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------


def _hours_between(later: datetime, earlier: Optional[datetime]) -> Optional[float]:
    if earlier is None:
        return None
    return max(0.0, (later - earlier).total_seconds() / 3600.0)


def diagnose(
    *,
    now_dt: Optional[datetime] = None,
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    users: Optional[Sequence[Dict[str, Any]]] = None,
    timer: Optional[Dict[str, Any]] = None,
    threshold: Optional[float] = None,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """把三件事读一遍，返回结构化结论（模板 / 接口 / 命令行共用）。

    所有入参都可注入，单测因此不需要 systemd、不需要真账本。
    """
    at = now_dt or now()
    limit = silence_hours() if threshold is None else float(threshold)
    if rows is None:
        ledger = read_ledger_rows(ledger_path)
    else:
        ledger = {
            "rows": [row for row in rows if isinstance(row, dict)],
            "path": str(ledger_path or _env_path("RUN_LEDGER_FILE", "logs/runs.jsonl")),
            "exists": True, "error": "", "bad_lines": 0,
        }
    latest = last_run(ledger.get("rows") or ())
    last_at = latest["at"] if latest else None
    silent = _hours_between(at, last_at)
    accounts = account_counts(users)
    timer_info = dict(timer) if timer is not None else timer_state()

    problems: List[str] = []
    lines: List[str] = []

    # ① 账本
    if not ledger.get("exists"):
        problems.append("no_ledger")
        lines.append(
            f"⛔ 没有可读的账本：{ledger.get('path')}"
            + (f"（{ledger.get('error')}）" if ledger.get("error") else "")
            + " —— 要么还没跑过，要么 RUN_LEDGER_FILE 指错了地方"
        )
    elif latest is None:
        problems.append("no_run")
        lines.append(
            f"⛔ 账本里一条带时间戳的记录都没有：{ledger.get('path')}"
            + (f"（坏行 {ledger['bad_lines']} 条）" if ledger.get("bad_lines") else "")
        )
    else:
        row = latest["row"]
        sent = len([a for a in (row.get("accounts") or []) if isinstance(a, dict)])
        lines.append(
            f"✅ 账本：最后一条 {last_at.strftime('%m-%d %H:%M')}"
            + (f"（{human_duration(int((silent or 0) * 3600))}前）" if last_at else "")
            + f"，run_id={row.get('run_id') or '?'}、{sent} 个账号"
        )

    # ② 静默（全停用不算：那是用户自己关的）
    if silent is not None and limit > 0:
        if accounts.get("total") and accounts.get("active") == 0:
            lines.append(
                f"⏸️ 静默判定跳过：{accounts['total']} 个账号全部已停用，"
                f"本来就不会有运行记录"
            )
        elif silent > limit:
            problems.append("silent")
            lines.append(
                f"⛔ 已经 {human_duration(int(silent * 3600))}没有任何运行记录"
                f"（最后一次 {last_at.strftime('%m-%d %H:%M') if last_at else '—'}；"
                f"阈值 {limit:g} 小时）"
            )
        else:
            lines.append(
                f"✅ 距最近一条运行记录 {human_duration(int(silent * 3600))}，"
                f"在阈值 {limit:g} 小时内"
            )
    elif limit <= 0:
        lines.append("⏸️ 静默判定已关闭（NOTIFY_SILENCE_HOURS=0）")

    # ③ 定时器
    if not timer_info.get("known"):
        lines.append(
            f"⚠️ 定时器 {timer_info.get('unit') or TIMER_UNIT}：读不到状态"
            f"（{timer_info.get('error') or '未知原因'}）—— 手动触发不受影响"
        )
    else:
        active = str(timer_info.get("active") or "")
        enabled = str(timer_info.get("enabled") or "")
        detail = f"enabled={enabled or '?'} active={active or '?'}"
        if timer_info.get("last"):
            detail += f" 上次触发 {timer_info['last']}"
        if active != "active":
            problems.append("timer_inactive")
            lines.append(
                f"⛔ 定时器 {timer_info['unit']}：{detail} —— 它不在跑，不会自动发"
            )
        elif enabled not in ("enabled", "static", "enabled-runtime"):
            problems.append("timer_disabled")
            lines.append(
                f"⚠️ 定时器 {timer_info['unit']}：{detail} —— 现在能跑，"
                f"但重启后不会自己起来（systemctl enable {timer_info['unit']}）"
            )
        else:
            lines.append(f"✅ 定时器 {timer_info['unit']}：{detail}")

    # ④ 账号
    if accounts.get("error"):
        lines.append(f"⚠️ 账号名单读不出来：{accounts['error']}")
    else:
        lines.append(
            f"✅ 账号：{accounts['total']} 个，{accounts['active']} 个在用"
            f"、{accounts['stopped']} 个已停用"
        )

    severe = bool({"no_ledger", "no_run", "silent", "timer_inactive"} & set(problems))
    level = "error" if severe else ("warning" if problems else "ok")
    if level == "ok":
        headline = "一切正常"
    elif level == "error":
        headline = "体检没过：有问题需要处理（见上面几行）"
    else:
        headline = "体检有提醒：不致命，但建议看一眼"
    lines.append(("✅ " if level == "ok" else "⛔ " if level == "error" else "⚠️ ") + headline)

    return {
        "ok": level == "ok",
        "level": level,
        "headline": headline,
        "problems": problems,
        "lines": lines,
        "silent_hours": None if silent is None else round(silent, 2),
        "threshold_hours": limit,
        "last_run_at": iso(last_at) if last_at else "",
        "last_run_text": last_at.strftime("%m-%d %H:%M") if last_at else "",
        "ledger": {
            "path": ledger.get("path", ""),
            "exists": bool(ledger.get("exists")),
            "error": ledger.get("error", ""),
            "rows": len(ledger.get("rows") or ()),
            "bad_lines": int(ledger.get("bad_lines") or 0),
        },
        "timer": timer_info,
        "accounts": accounts,
        "checked_at": iso(at),
    }


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def format_lines(report: Dict[str, Any]) -> List[str]:
    """给人看的几行。lines 已经在 diagnose 里排好序，这里只兜底。"""
    lines = [str(item) for item in (report.get("lines") or []) if str(item)]
    if lines:
        return lines
    return [("✅ " if report.get("ok") else "⛔ ") + _str(report.get("headline"), "体检完成")]


# ---------------------------------------------------------------------------
# 通知（每天最多一条）
# ---------------------------------------------------------------------------


def _state_path() -> str:
    return _env_path("DOCTOR_STATE_FILE", STATE_FILE)


def _read_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: str, state: Dict[str, Any]) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
    except OSError as exc:  # 通知去重失败不能影响体检结论
        _warn(f"体检状态写不进去（{path}）：{type(exc).__name__}: {exc}")


def maybe_notify(
    report: Dict[str, Any],
    *,
    logger: Any = None,
    now_dt: Optional[datetime] = None,
    config_override: Optional[Dict[str, Any]] = None,
    state_path: Optional[str] = None,
) -> Dict[str, Any]:
    """有事就推一条；同一天只推一次（记 logs/doctor_state.json）。"""
    if report.get("ok") or report.get("level") == "ok":
        return {"sent": False, "reason": "nothing-to-report", "detail": ""}

    from core import notify

    cfg = notify.config(config_override)
    if cfg.get("level") == notify.LEVEL_OFF:
        return {"sent": False, "reason": "notify-off", "detail": ""}
    if not cfg.get("url"):
        return {"sent": False, "reason": "no-webhook", "detail": ""}

    at = now_dt or now()
    path = state_path or _state_path()
    today = at.strftime("%Y-%m-%d")
    state = _read_state(path)
    if _str(state.get("date")) == today:
        return {"sent": False, "reason": "already-today", "detail": _str(state.get("at"))}

    title = "火花体检没过：" + _str(report.get("headline"), "有问题")
    text = "\n".join(format_lines(report))
    kind = notify.kind_of(cfg["url"])
    try:
        payload = notify.build_payload(
            cfg["url"], title, text, kind=kind,
            status="error" if report.get("level") == "error" else "warning",
            run_id="doctor", at=_str(report.get("checked_at")), accounts=[],
        )
        ok, detail = notify.post(cfg["url"], payload, cfg.get("timeout"), kind=kind)
    except Exception as exc:  # noqa: BLE001 —— 通知永远不许把体检结论带偏
        ok, detail = False, f"{type(exc).__name__}: {exc}"

    if ok:
        _write_state(path, {
            "date": today, "at": iso(at), "level": report.get("level"),
            "problems": list(report.get("problems") or []),
        })
        return {"sent": True, "reason": "ok", "detail": str(detail)}
    if logger is not None:
        logger.warning(f"体检通知没发出去：{detail}")
    return {"sent": False, "reason": "failed", "detail": str(detail)}


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """python main.py doctor [--json] [--no-notify] 的实现。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(item in ("-h", "--help") for item in args):
        print(
            "用法：python main.py doctor [--json] [--no-notify]\n"
            "  体检：账本还新吗、定时器还在跑吗、账号是不是全停用了。\n"
            "  退出码 0=一切正常，1=有事（同时按 NOTIFY_* 推一条，每天最多一条）。\n"
            "  --json       机器可读\n"
            "  --no-notify  只打印，不推通知"
        )
        return 0
    unknown = [item for item in args if item not in ("--json", "--no-notify")]
    if unknown:
        print(f"认不出来的开关：{' '.join(unknown)}（可用：--json / --no-notify）",
              file=sys.stderr)
        return 2

    report = diagnose()
    if "--json" in args:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for line in format_lines(report):
            print(line)

    if "--no-notify" not in args and not report.get("ok"):
        result = maybe_notify(report)
        if result.get("sent"):
            print(f"📨 已推送一条体检通知：{result.get('detail')}")
        elif result.get("reason") == "already-today":
            print("📨 今天已经推过一条体检通知，不重复推")
        elif result.get("reason") == "failed":
            print(f"📨 体检通知没发出去：{result.get('detail')}", file=sys.stderr)

    return 0 if report.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
