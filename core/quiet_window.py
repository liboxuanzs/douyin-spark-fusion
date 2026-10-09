"""
core/quiet_window.py
"禁跑时间窗"—— 在指定的时间段里整轮不发，避免同一个抖音账号被两个程序同时操作。

背景：2026-10-01 之前 `.172` 上还跑着另一套部署 `douyin-spark.service`（每天 20:30 起、
抖动 0~20 分钟真的在发私信），两台 Chromium 用同一个抖音账号登录会互踢掉线，
所以当时默认窗口是 **20:20-21:00**，正好把那一轮包住。

那套服务**已于 2026-10-01 停用禁用**，融合版自己就是唯一的发送方，于是：
* 默认值改成 **off**。窗口存在的唯一理由（错开另一个发送方）没有了，留着 20:20-21:00
  反而会静默盖住 20:30 那一轮定时任务 —— 到点整轮跳过，一条都发不出去，而且只有
  翻账本才看得出来。
* 面板会在"禁跑窗盖住了定时器时间"时直接报红（`webui/viewmodel.py::_quiet_clash`），
  不用等到点才发现。真要用这个窗口（比如夜里不想发），就在面板「运行配置」里写。

这个模块只做**判断**，不做任何 I/O（除了读环境变量）：
`parse()` 解析 "20:20-21:00" / "20:20-01:30"（跨零点）/ "off"；
`check()` 回答"此刻是否在窗口内、什么时候结束"。

设计取舍：
* 窗口的用途是"两个程序别抢账号"，不是"每天固定几点不发"；只剩一个发送方时它就该关掉。
* 窗口**不阻止定时任务长期存在**，只是让撞窗的那一轮跳过并记进账本。
* 紧急情况下用 `QUIET_WINDOW=` 关掉它（或设成 `QUIET_WINDOW_OVERRIDE=1` 临时放行）。
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from typing import Any, Dict, Optional, Tuple

# 关掉窗口的写法（大小写不敏感）
_OFF_VALUES = ("", "off", "none", "no", "0", "false", "disable", "disabled")

# 默认关闭：窗口存在的唯一理由（与另一套部署错开）已经消失，
# 留着 20:20-21:00 会静默盖掉 20:30 那一轮定时任务。见模块 docstring。
DEFAULT_WINDOW = "off"

# 环境变量名
ENV_WINDOW = "QUIET_WINDOW"
ENV_OVERRIDE = "QUIET_WINDOW_OVERRIDE"


def _parse_hhmm(text: str) -> Optional[time]:
    """把 '20:20' / '2020' / '20：20'（全角冒号）解析成 time；不合法返回 None。"""
    raw = (text or "").strip().replace("：", ":")
    if not raw:
        return None
    if ":" in raw:
        hh, _, mm = raw.partition(":")
    elif len(raw) == 4 and raw.isdigit():
        hh, mm = raw[:2], raw[2:]
    else:
        return None
    try:
        hour, minute = int(hh), int(mm)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour=hour, minute=minute)


def parse(spec: Optional[str]) -> Optional[Tuple[time, time]]:
    """解析 "HH:MM-HH:MM" → (起, 止)；关掉的写法或不合法一律返回 None。

    跨零点的写法（如 "23:50-00:30"）是合法的，判断时按"跨天"处理。
    """
    raw = (spec or "").strip()
    if raw.lower() in _OFF_VALUES:
        return None
    left, sep, right = raw.partition("-")
    if not sep:
        return None
    start, end = _parse_hhmm(left), _parse_hhmm(right)
    if start is None or end is None:
        return None
    return (start, end)


def window_spec(env: Optional[Dict[str, str]] = None) -> str:
    """当前生效的窗口写法（未经解析），只读环境变量。"""
    if env is not None:
        return (env.get(ENV_WINDOW, DEFAULT_WINDOW) or "").strip()
    return (os.getenv(ENV_WINDOW, DEFAULT_WINDOW) or "").strip()


def override(env: Optional[Dict[str, str]] = None) -> bool:
    """是否被显式放行（`QUIET_WINDOW_OVERRIDE=1`）。"""
    raw = (env.get(ENV_OVERRIDE, "") if env is not None else os.getenv(ENV_OVERRIDE, "")) or ""
    return raw.strip().lower() not in _OFF_VALUES


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def in_window(at: datetime, window: Tuple[time, time]) -> bool:
    """`at` 是否落在窗口内。跨零点窗口（起 > 止）按"或"判断。"""
    start, end = window
    cur = _minutes(at.time())
    lo, hi = _minutes(start), _minutes(end)
    if lo == hi:
        return False          # 零长度窗口 = 不限制
    if lo < hi:
        return lo <= cur < hi
    return cur >= lo or cur < hi      # 跨零点


def until(at: datetime, window: Tuple[time, time]) -> datetime:
    """窗口结束的时刻（已过结束点则不返回过去时间，至少给 1 分钟后的整点）。"""
    _start, end = window
    end_dt = at.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_dt <= at:
        end_dt += timedelta(days=1)
    return end_dt


def check(at: Optional[datetime] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """当前是否禁跑。

    返回 ``{"active": bool, "spec": str, "window": (起, 止) | None,
    "until": ISO 字符串 or "", "override": bool, "reason": str}``。
    ``active`` 已经把 override 与"窗口写法不合法"都算进去了 —— 调用方只需要看它。
    """
    at = at or datetime.now()
    spec = window_spec(env)
    window = parse(spec)
    ovr = override(env)
    active = bool(window) and not ovr and in_window(at, window)
    reason = ""
    if active:
        reason = f"当前处于禁跑时间窗 {spec}，至 {until(at, window):%H:%M}"
    elif not window and spec.lower() not in _OFF_VALUES:
        reason = ""           # 写法不合法时静默放行，但下面会带出 spec 供排障
    return {
        "active": active,
        "spec": spec,
        "window": window,
        "until": until(at, window).isoformat(timespec="seconds") if active else "",
        "override": ovr,
        "reason": reason,
    }


#: `check()` 的结果里哪些键可以安全写进 JSON（账本、HTTP 响应）。
#: `window` 是 (datetime.time, datetime.time) 元组，**不能**直接喂给 `json.dumps`——
#: 2026-10-01 真机验证时正是它把整轮任务的收尾炸掉了（任务本身已经跑完，
#: 却因为写账本失败丢掉记录）。要序列化就用 `summary()`，别传原始 dict。
JSON_KEYS = ("active", "spec", "until", "override", "reason")


def summary(info) -> dict:
    """把 `check()` 的结果压成 JSON 安全的小字典（丢掉 `window` 元组）。"""
    info = info or {}
    return {key: info.get(key) for key in JSON_KEYS}
