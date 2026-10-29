# -*- coding: utf-8 -*-
"""火花连续天数（issue #15）。

面板一直只报「今天发了几条」。可用户真正关心的是**这条火花连着多少天了、哪天断的** ——
抖音的连续天数一旦断掉不会自己恢复，而现在的面板要等"好像好几天没收到消息"才会发现。

数据全在运行账本里（logs/runs.jsonl）：每条 entry 有 target / conv_id / ok / level /
dry_run / at，按 (unique_id, conv_id) 归一天就能算。所以这里**不新增任何状态文件** ——
再加一个 streak.json 只会多一处能跟账本对不上的地方。

四条口径（每一条都对应一个真实的显示陷阱）：

1. **只认真实发送**：dry_run 不是 off 的那一轮、或者 entry 自己标了 dry_run 的，
   一律跳过 —— 空跑绝不能把连续天数续上。
2. **今天还没发不算断**：凌晨打开面板时今天的发送还没到点，此时应显示
   「昨天为止连续 N 天」，而不是红着脸说断签。
3. **窗口有限就说清楚**：账本只从尾部读 4 MB（webui/store.py 的 read_ledger），
   能算多长由账本自己决定，所以返回 window_days，界面必须说「只统计最近 N 天」。
4. **断签是"昨天没发"**：最后一次成功在前天或更早才算断，并给出「上次成功 N 天前」。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from core.util_time import DRY_RUN_OFF, norm_dry_run, parse_iso

__all__ = ["compute", "streak_text"]


def _entry_dry_run(entry: Dict[str, Any]) -> str:
    raw = entry.get("dry_run")
    if raw is None or raw is False:
        return DRY_RUN_OFF
    if raw is True:
        return "full"
    text = str(raw).strip().lower()
    if text in ("", "false", "0", "no", "off"):
        return DRY_RUN_OFF
    return norm_dry_run(text)


def _entry_day(entry: Dict[str, Any], fallback: Optional[date]) -> Optional[date]:
    stamp = parse_iso(entry.get("at"))
    if stamp is not None:
        return stamp.date()
    return fallback


def _row_day(row: Dict[str, Any]) -> Optional[date]:
    for key in ("finished_at", "started_at"):
        stamp = parse_iso(row.get(key))
        if stamp is not None:
            return stamp.date()
    return None


def _configured_labels(labels: Optional[Sequence[str]]) -> Optional[Set[str]]:
    """当前配置里这个账号的好友名单（去空白）。没配名单 / 名单为空时返回 None。

    None 与空集合是两件事：None = "不知道它有哪些好友"（保留账本里的一切），
    空集合 = "它现在一个好友都没有"（账本里的旧好友一个都不算）。
    """
    if labels is None:
        return None
    cleaned = {str(label or "").strip() for label in labels}
    cleaned.discard("")
    return cleaned or None


def _run_lengths(days: Sequence[date]) -> List[int]:
    """把有序日期列表切成一段段连续区间，返回每段长度。"""
    lengths: List[int] = []
    run = 0
    previous: Optional[date] = None
    for day in days:
        if previous is not None and (day - previous).days == 1:
            run += 1
        else:
            if run:
                lengths.append(run)
            run = 1
        previous = day
    if run:
        lengths.append(run)
    return lengths


def _current_streak(days: Sequence[date], today: date) -> int:
    """从最后一次成功往回数；最后一次必须落在今天或昨天，否则算断（返回 0）。"""
    if not days:
        return 0
    last = days[-1]
    if (today - last).days > 1:
        return 0
    streak = 1
    for index in range(len(days) - 1, 0, -1):
        if (days[index] - days[index - 1]).days == 1:
            streak += 1
        else:
            break
    return streak


def streak_text(item: Dict[str, Any]) -> str:
    """一行话：连续 N 天 / 断签（上次成功 N 天前）/ 还没有成功记录。"""
    if not item.get("known"):
        return "还没有成功记录"
    if item.get("broken"):
        return f"断签 {item.get('idle_days')} 天（上次 {item.get('last_text')}）"
    current = int(item.get("current") or 0)
    if current <= 0:
        return f"当前 0 天（上次 {item.get('last_text')}）"
    return f"连续 {current} 天"


def compute(
    rows: Iterable[Dict[str, Any]],
    *,
    today: Optional[datetime] = None,
    targets_by_account: Optional[Dict[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """算连续天数。返回 accounts / window_days / window_start / window_text。

    rows 是账本的**原始记录**（webui/store.py 归一化时会丢掉 conv_id，所以调用方
    要传 record.raw）。顺序无所谓：这里按日期自己排。
    """
    today_date = (today or datetime.now()).date()
    days_by_key: Dict[str, Dict[str, Any]] = {}
    earliest: Optional[date] = None

    for row in rows or ():
        if not isinstance(row, dict):
            continue
        if norm_dry_run(row.get("dry_run")) != DRY_RUN_OFF:
            continue
        day = _row_day(row)
        if day is None:
            continue
        if earliest is None or day < earliest:
            earliest = day
        for account in row.get("accounts") or ():
            if not isinstance(account, dict):
                continue
            uid = str(account.get("unique_id") or account.get("username") or "")
            if not uid:
                continue
            bucket = days_by_key.setdefault(uid, {})
            for entry in account.get("entries") or ():
                if not isinstance(entry, dict):
                    continue
                if _entry_dry_run(entry) != DRY_RUN_OFF:
                    continue
                if entry.get("ok") is not True:
                    continue
                entry_day = _entry_day(entry, day)
                if entry_day is None:
                    continue
                key = str(entry.get("conv_id") or entry.get("target") or "")
                if not key:
                    continue
                item = bucket.setdefault(key, {"label": "", "days": set()})
                label = str(entry.get("target") or "").strip()
                if label:
                    item["label"] = label
                item["days"].add(entry_day)
                if earliest is None or entry_day < earliest:
                    earliest = entry_day

    window_start = earliest or today_date
    window_days = max(1, (today_date - window_start).days + 1)

    accounts: Dict[str, Dict[str, Any]] = {}
    for uid, bucket in days_by_key.items():
        items: Dict[str, Dict[str, Any]] = {}
        # 只认配置里还列着的好友：账本就是全部历史，被移掉的好友还躺在里面，
        # 那些行的"上次成功"停在很久以前，把它们算成断签会让首屏天天报警（线上
        # 真实数据里就有 3 个早就删除的旧好友在报断签）。账号不在 targets 表里
        # （比如已经删掉的账号）时无从判断，按原样保留。
        alive = _configured_labels((targets_by_account or {}).get(uid))
        for key, raw_item in bucket.items():
            days = sorted(raw_item["days"])
            last = days[-1] if days else None
            current = _current_streak(days, today_date)
            longest = max(_run_lengths(days)) if days else 0
            idle_days = (today_date - last).days if last else None
            broken = bool(last and idle_days is not None and idle_days > 1)
            items[key] = {
                "key": key,
                "label": raw_item["label"] or key,
                "current": current,
                "longest": longest,
                "known": bool(last),
                "last": last.isoformat() if last else "",
                "last_text": last.strftime("%m-%d") if last else "",
                "idle_days": idle_days,
                "broken": broken,
                "days_in_window": len(days),
            }
        if alive is not None:
            items = {
                key: item for key, item in items.items()
                if str(item.get("label") or "").strip() in alive
            }
        accounts[uid] = items

    # 配置里列着、但账本里一次都没成功过的目标也要显示（否则"这个好友从来没发成"
    # 会彻底隐形 —— 那恰好是最该被发现的一类）。
    for uid, labels in (targets_by_account or {}).items():
        items = accounts.setdefault(str(uid), {})
        for label in labels or ():
            text = str(label or "").strip()
            if not text:
                continue
            if any(item["label"] == text for item in items.values()):
                continue
            key = "label:" + text
            items[key] = {
                "key": key, "label": text, "current": 0, "longest": 0,
                "known": False, "last": "", "last_text": "", "idle_days": None,
                "broken": False, "days_in_window": 0,
            }

    summary: Dict[str, Any] = {}
    for uid, items in accounts.items():
        ordered = sorted(items.values(), key=lambda item: (not item["known"], item["label"]))
        known = [item for item in ordered if item["known"]]
        current = min((item["current"] for item in known), default=0)
        longest = max((item["longest"] for item in ordered), default=0)
        broken = [item for item in ordered if item["broken"]]
        summary[uid] = {
            "targets": ordered,
            "current": current,
            "longest": longest,
            "broken": len(broken),
            "broken_labels": [item["label"] for item in broken],
            "unknown": len([item for item in ordered if not item["known"]]),
            "text": (
                f"连续 {current} 天 · 最长 {longest} 天" if known else "还没有成功记录"
            ),
        }

    return {
        "accounts": summary,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "window_text": f"只统计最近 {window_days} 天",
        "today": today_date.isoformat(),
    }
