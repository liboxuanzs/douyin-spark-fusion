"""面板的视图模型 —— 把只读数据装成 B 的模板与前端轮询契约需要的形状。

为什么单独一层
--------------
面板的 HTML/JS/CSS 是从 B（``halfwaystudent/douyin-sparkflow``）**原样搬过来**的：
``static/app.js`` 每 10 秒拉一次 ``/api/ops/overview``，然后按**固定的键名**把数字写进
``[data-overview-value]`` / ``[data-account-overview]`` 那些节点。键名对不上，页面不会报错，
只会静静地停在初始值 —— 这种 bug 非常难查。

所以把「C 的账本/配置 → B 的键」这一步收进本模块：纯函数、可离线单测，
模板和 JSON 接口共用同一份视图模型，两边永远一致。

键名契约（改动前先看 ``static/app.js`` 的 ``render()``）
--------------------------------------------------------
``summary``   ``total, confirmed, pageEcho, receiptOnly, attention, pending,
              unprocessed, remaining, sentTotal, progressPercent, lastConfirmedAt``
``accounts[]`` ``uniqueId, state, total, confirmed, pageEcho, attention, pending``
``task``      ``running, stale, ageSeconds``
``schedule``  ``nextTriggerAt, nextTriggerText, label, remainingTargets,
              hasWorkNextTrigger, dispatcher``

C 与 B 的口径差异（务必保持诚实，不许凑数）
------------------------------------------
* B 有「仅回执未确认」（``receiptOnly``）这一档：靠 HTTP 回执就认为发出去的。
  C **没有**这种成功 —— 回执在 C 里只是证据之一，归属不明时判失败。
  故 ``receiptOnly`` 恒为 0，页面上也不给它渲染卡片。
* B 有「已处理但未确认」（``unprocessed``）：账号级阻断导致本轮根本没试。
  C 每轮对每个目标都是「试了并给出裁决」，没试的目标直接算「未尝试」，
  故 ``unprocessed`` 恒为 0，未尝试的目标全部计入 ``pending``（不重复计数）。
* 因此每个目标恰好落在 ``confirmed`` / ``pageEcho`` / ``attention`` / ``pending`` 之一。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from webui import store
from core import schedule

__all__ = [
    "DRY_RUN_LEVELS",
    "DRY_RUN_LABELS",
    "DRY_RUN_HINTS",
    "SEGMENTS",
    "STATE_LABELS",
    "STATE_CSS",
    "account_cards",
    "age_seconds",
    "build",
    "config_rows",
    "entry_bucket",
    "human_left",
    "message_lines",
    "overview_payload",
    "percent",
    "progress_pair",
    "next_due",
    "run_row",
    "schedule_fields",
    "summarize",
    "until_text",
]

# ---------------------------------------------------------------- 常量

#: 空跑三档 —— 取值必须与 ``core.douyin_im`` 的 ``DRY_RUN_OFF/SELECT/FULL`` 一致。
DRY_RUN_LEVELS: Tuple[str, ...] = ("off", "select", "full")

DRY_RUN_LABELS: Dict[str, str] = {
    "off": "真实发送",
    "select": "只选中不发送",
    "full": "输入后清空",
}

DRY_RUN_HINTS: Dict[str, str] = {
    "off": "真把消息发出去 —— 唯一会改变对方会话状态的档位。",
    "select": "只走到「选中好友」为止，验证会话能不能定位，不发消息。",
    "full": "把消息真的敲进编辑器、读回校验，然后清空草稿，唯独不点发送。",
}

#: 每种横幅配一个 lucide 图标名（必须存在于 ``static/lucide.min.js``）。
BANNER_ICONS: Dict[str, str] = {
    "cooldown": "hourglass",
    "quiet_window": "moon",
    "browser": "puzzle",
    "running": "loader-circle",
    "lock": "lock",
    "skipped": "circle-slash",
    "blocked": "octagon-alert",
}

#: 判定原因码（``core.strict_verify`` 的 ``R_*`` 与回执判定的码）说人话。
REASON_LABELS: Dict[str, str] = {
    "no_conv_id": "DOM 里没有选中任何会话",
    "conv_id_unreadable": "有选中态，但读不到会话 id",
    "identity_unknown": "身份信息不足以强校验",
    "identity_mismatch": "选中的是另一个人（身份对不上）",
    "title_mismatch": "选中项标题与目标不符",
    "conv_mismatch": "网络信号指向目标，但选中的不是它",
    "no_evidence": "两边都没观察到发送证据",
    # core/strict_verify.py::verify_receipt() 的三个回执级码
    "receipt_wrong_conv": "回执对上了，但选中的会话不是目标（可能发错人）",
    "receipt_failed": "页面与回执两边都说没发出去",
    "receipt_unconfirmed": "没有拿到能确认发送的回执",
    # core/douyin_im.py 输入框（Draft.js）相关的码
    "editor_missing": "页面上找不到可用的输入框（结构可能变了）",
    "editor_text_mismatch": "输入框里的字与要发的内容对不上",
    "editor_not_cleared": "输入框里上一次的字没清干净，这条先不发（免得叠着发两遍）",
    "editor_residue": "输入框里发现上一条消息的残留，这条先不发",
    # core/douyin_im.py 的登录态复检（2026-10-09 wheat 掉登录那次加的）：
    # 页面还长着 IM 的样子，回执却永远不来 —— 这不是"没发出去"，是"没登录"。
    "login_lost": "登录已失效（要重新扫码登录）",
}

#: 回执来源（``core.douyin_im`` 的 ``via`` 字段）。
#: 注意 `http` 这一档的措辞：`_wait_receipt` 拿到 HTTP 回执后**最多再等 1 秒**
#: 就收工（`core/douyin_im.py` 里 `if http and time.time() - t0 > 1.0: break`），
#: 所以"只有 HTTP"不等于"会话回显里没有这条" —— 它也可能是"没等到/没采到"。
#: 写成「仅 HTTP 回执」会让用户以为另一侧看过且不认，面板不能这么暗示。
VIA_LABELS: Dict[str, str] = {
    "http+dom": "HTTP 回执 + 会话回显",
    "http": "HTTP 回执（会话回显没等到）",
    "dom": "仅会话回显（没拿到 HTTP 回执）",
    "dry-run": "空跑：输入后清空",
    # 被拦下的那两条（输入框清不干净 / 发现上一条的残留）从来没点过发送，
    # 所以既没有回执也没有回显 —— 别让页面上只显示一个 "none"。
    "none": "没发送（先拦下了）",
}

#: 证据级别（``core.strict_verify`` 的 ``LEVEL_*``）。
LEVEL_LABELS: Dict[str, str] = {
    "strong": "强确认",
    "medium": "弱证据",
    "conflict": "反证",
    "none": "无证据",
}

SEGMENTS: Tuple[Tuple[str, str], ...] = (
    ("success", "成功"),
    ("weak", "弱证据"),
    ("failed", "需处理"),
    ("pending", "未尝试"),
)

#: C 的四种账号状态。B 的 CSS 只给 ``.account-console`` 写了三档配色
#: （``state-attention``/``state-paused`` 红、``state-warning``/``state-pending`` 黄、
#: ``state-healthy`` 绿），这里映射过去，不新增 B 没有的类名。
STATE_LABELS: Dict[str, str] = {
    "healthy": "正常",
    "attention": "需处理",
    "blocked": "账号阻断",
    "idle": "尚未运行",
    "orphan": "已不在配置里",
}

STATE_CSS: Dict[str, str] = {
    "healthy": "healthy",
    "attention": "attention",
    "blocked": "paused",
    "idle": "pending",
    "orphan": "pending",
}

#: ``status-chip`` 的配色（B 的三档）。
STATE_CHIP: Dict[str, str] = {
    "healthy": "success",
    "attention": "danger",
    "blocked": "danger",
    "idle": "warning",
    "orphan": "info",
}

_LEVEL_STRONG = "strong"
_LEVEL_MEDIUM = "medium"

#: 门禁通过时的状态码（见 ``core.douyin_im`` 的 ``STATUS_*``）。
_GATE_OK = "READY"


# ---------------------------------------------------------------- 小工具


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """同时支持 dataclass（``store.RunRecord`` 等）与普通 dict —— 单测可以只喂字典。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _jsonable(info: Any) -> Dict[str, Any]:
    """``quiet_window.check()`` 的结果里带 ``datetime.time`` 元组，不能直接进 JSON/账本。

    ``core.quiet_window.summary()`` 已经是白名单版本；这里再兜一层，
    免得将来 ``check()`` 多出别的不可序列化字段时又在写账本那一步炸掉。
    """
    if not isinstance(info, dict):
        return {}
    return {
        key: ("" if value is None else value)
        for key, value in info.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


def percent(part: Any, total: Any) -> int:
    """半向上取整的百分比 —— 与 B 的模板/JS 用**同一个**算法，避免两处数字打架。"""
    try:
        part_value = float(part or 0)
        total_value = float(total or 0)
    except (TypeError, ValueError):
        return 0
    if total_value <= 0:
        return 0
    return int((part_value * 100.0 / total_value) + 0.5)


def progress_pair(confirmed: Any, sent: Any, total: Any) -> Tuple[int, int]:
    """返回 ``(强确认百分比, 弱证据百分比)``；两段加起来不超过 100。"""
    strong = percent(confirmed, total)
    weak = max(0, percent(sent, total) - strong)
    return strong, weak


def entry_bucket(entry: Dict[str, Any]) -> str:
    """一条发送明细归到哪个档位：``success`` / ``weak`` / ``failed``。"""
    if not bool(entry.get("ok")):
        return "failed"
    return "success" if _text(entry.get("level")) == _LEVEL_STRONG else "weak"


def _reason_text(reason: str) -> str:
    """判定原因说人话；不认识的码原样带出去（不编故事）。"""
    if not reason:
        return ""
    if reason in REASON_LABELS:
        return REASON_LABELS[reason]
    return reason


def _segment_row(target: str, bucket: str, **extra: Any) -> Dict[str, Any]:
    """明细行的统一形状：模板直接读，不用为缺失键做兼容。"""
    row: Dict[str, Any] = {
        "target": _text(target, "（未命名目标）"),
        "ok": False,
        "level": "",
        "level_text": "",
        "reason": "",
        "reason_text": "",
        "via": "",
        "via_text": "",
        "at_text": "",
        "message_id": "",
        "dry_run": "",
        "rate_limited": False,
        "bucket": bucket,
    }
    row.update(extra)
    return row


def _target_rows(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for entry in entries:
        reason = _text(entry.get("reason"))
        via = _text(entry.get("via"))
        level = _text(entry.get("level"))
        ok = bool(entry.get("ok"))
        rows.append(
            {
                "target": _text(entry.get("target"), "（未命名目标）"),
                "ok": ok,
                "level": level,
                "level_text": LEVEL_LABELS.get(level, level or "—"),
                "reason": reason,
                "reason_text": _reason_text(reason),
                "via": via,
                "via_text": VIA_LABELS.get(via, via or "—"),
                "at_text": _text(entry.get("at_text")),
                "message_id": _text(entry.get("message_id")),
                "dry_run": _text(entry.get("dry_run")),
                "rate_limited": bool(entry.get("rate_limited")),
                "bucket": "success" if ok and level == _LEVEL_STRONG else ("weak" if ok else "failed"),
            }
        )
    return rows


# ---------------------------------------------------------------- 账号卡


def _record_account(record: Any, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """在某一轮记录里找这个账号的结果（先按 unique_id、再按用户名）。"""
    unique_id = _text(snapshot.get("unique_id"))
    username = _text(snapshot.get("username"))
    for account in _get(record, "accounts") or []:
        if not isinstance(account, dict):
            continue
        if unique_id and _text(account.get("unique_id")) == unique_id:
            return account
        if username and _text(account.get("username")) == username:
            return account
    return None


def _account_state(
    *,
    ready: bool,
    gate: str,
    status: str,
    attempted: int,
    failed: int,
) -> str:
    """账号整体状态。判定顺序就是「先看能不能跑，再看跑得怎么样」。

    * ``blocked`` —— 根本没法干活：没 cookie、门禁没过（登录失效等）、
      上一轮被整账号跳过，或者失败了但**一条都没发出去**（浏览器没起来之类）。
    * ``attention`` —— 干了活但有失败的目标。
    * ``healthy`` —— 有成功（强确认或弱证据）。
    * ``idle`` —— 还没跑过。
    """
    if not ready:
        return "blocked"
    if gate and gate != _GATE_OK:
        return "blocked"
    if status == "skipped":
        return "blocked"
    if status == "error" and attempted <= 0:
        return "blocked"
    if failed > 0:
        return "attention"
    if attempted > 0:
        return "healthy"
    return "idle"


def _account_state_for(data: Dict[str, Any], *, ready: bool, gate: str, status: str,
                       attempted: int, failed: int, login_lost: bool = False) -> str:
    """和 ``_account_state`` 一样，只是先认「只在账本里、配置里已经没了」这一档。"""
    if data.get("orphan"):
        return "orphan"
    # 运行期掉登录：页面上一切正常、发送却永远拿不到回执，最后一条都没发出去。
    # 这既不是"干了活有几个失败"（attention），也不是门禁没过（gate 仍是 READY），
    # 而是**根本上没法干活** —— 按 blocked 处理，卡片给出重新扫码的指引。
    if login_lost:
        return "blocked"
    return _account_state(
        ready=ready, gate=gate, status=status, attempted=attempted, failed=failed
    )


def schedule_fields(unique_id: str, send_time: str = "", *, when=None) -> Dict[str, Any]:
    """一个账号「什么时候发」的展示行 —— 面板上的时间输入框与今日状态都用它。

    ★ 判定逻辑**一律走 `core.schedule`**，这里只把结果翻成人话。面板写着
    "今天 08:07 发"、调度就真的在 08:07 发，必须是同一份计算 ——
    否则用户看到的和实际发生的对不上，是最伤信任的一类 bug。

    读状态文件是每张卡一次小文件读（≤10 个账号、只在渲染首页时发生），
    刻意不做缓存：缓存要么跨请求读到旧状态，要么得引入失效逻辑，
    都比这一次 `open()` 贵。
    """
    uid = _text(unique_id)
    when = when or schedule.now()
    if not uid:
        # 没有抖音号就没有 key —— 无从谈起"今天发过没有"，别假装知道。
        return {
            "send_time": "", "send_time_custom": False, "send_window": "",
            "send_due": "", "send_state": "unknown", "send_state_text": "没有抖音号，无法排期",
            "send_sent_at": "",
        }
    plan = schedule.plan_for({"unique_id": uid, "time": _text(send_time)}, when=when)
    state = schedule.load_state()
    record = schedule.entry_for(state, uid, when)
    status = str((record or {}).get("status") or "")
    if record and status == schedule.STATUS_SKIPPED:
        # 「跳过」= 门禁没过 / 命中风控，一条都没发出去，当天名额还在，
        # 下一次门铃会再试（见 core.schedule.select）。文案必须和「已发」区分开，
        # 否则用户以为今天已经发过了，其实一条都没有。
        clock = str(record.get("sent_at") or "")[11:16]
        state_key = "due"
        state_text = f"今天 {clock} 试过但没发出去（跳过），下一次门铃会重试"
    elif record:
        clock = str(record.get("sent_at") or "")[11:16]
        label = schedule.STATUS_LABELS.get(status, "已处理")
        state_key = "sent"
        state_text = f"今天 {clock} {label}（{record.get('source') or '手动'}）"
    elif when >= plan["due_at"]:
        # 到点了但还没发：只有"今天一个门铃都没敲到"或"刚跨过时间点"才会看到，
        # 文案要说明"不用管，下一轮门铃就发"，否则用户会以为卡住了。
        state_key = "due"
        state_text = f"该发了（{plan['due_text']}），等下一次触发"
    else:
        state_key = "waiting"
        state_text = f"等 {plan['due_text']} 到点"
    return {
        "send_time": plan["time"],
        "send_time_custom": bool(plan["custom"]),
        "send_window": schedule.window_text(plan),
        "send_due": plan["due_text"],
        "send_state": state_key,
        "send_state_text": state_text,
        "send_status": status,
        "send_sent_at": str((record or {}).get("sent_at") or ""),
    }


def next_due(accounts: Sequence[Any] = (), *, when=None) -> Optional[Dict[str, Any]]:
    """全部账号里最近的一个"今天还没发"的到点时刻 —— 面板的"下次自动触发"显示它。

    timer 现在每 5 分钟敲一次门（`OnCalendar=*:0/5`），照抄 timer 的心跳等于什么
    都没说（永远是"5 分钟内"）。真正有意义的是**下一个发出去的会是谁、几点**。

    返回 ``None`` 表示今天没有账号还会发了（都发过了）—— 面板要能说出这句话，
    而不是继续画一个明天的时间让人以为今天还有。
    """
    when = when or schedule.now()
    state = schedule.load_state()
    best: Optional[Dict[str, Any]] = None
    for item in accounts or []:
        uid = _text(_get(item, "unique_id"))
        if not uid:
            continue
        # 今天已经记过账的账号不会再发 —— 不排除掉的话"下次"会一直指向一个
        # 已经发生过、且今天不会再发生的时间。
        #
        # ★ 口径必须与派发器一致（`core.schedule.select`）：`skipped` 是
        #   "门禁没过 / 命中风控，一条都没发出去"，当天名额还在、下一次门铃还会再试。
        #   把它算成"今天发过了"，面板就会一边显示该账号「下一次门铃会重试」、
        #   一边在顶上写「今天该发的都发完了」（2026-10-09 wheat 掉登录时就是这样）。
        record = schedule.entry_for(state, uid, when)
        if record is not None and _text(record.get("status")) != schedule.STATUS_SKIPPED:
            continue
        plan = schedule.plan_for(
            {"unique_id": uid, "username": _text(_get(item, "username")),
             "time": _text(_get(item, "send_time"))}, when=when
        )
        if best is None or plan["due_at"] < best["due_at"]:
            best = plan
    if best is None:
        return None
    name = _text(best.get("username")) or _text(best.get("unique_id"))
    due = best["due_at"]
    imminent = when >= due
    return {
        "name": name,
        "at": due.isoformat(timespec="seconds"),
        "due_text": best["due_text"],
        "imminent": imminent,
        # 过点了还没发是常态（门铃每 5 分钟才响一次），文案要把这件事说清楚，
        # 否则用户会以为卡住了、去点"立即运行"。
        "text": (
            f"下一个 {name}：已过 {best['due_text']}，等下一次敲门就发"
            if imminent else
            f"下一个 {name}：今天 {best['due_text']}"
        ),
    }


def build_account(snapshot: Any, record: Any = None) -> Dict[str, Any]:
    """把「配置里的账号」与「最近一轮的运行结果」合成一张账号卡。"""
    data = snapshot.as_dict() if hasattr(snapshot, "as_dict") else dict(snapshot or {})
    account = _record_account(record, data)

    entries_raw = list((account or {}).get("entries") or [])
    entries = _target_rows(entries_raw)
    buckets: Dict[str, List[Dict[str, Any]]] = {"success": [], "weak": [], "failed": [], "pending": []}
    for row in entries:
        buckets[entry_bucket(row)].append(row)

    confirmed = len(buckets["success"])
    weak = len(buckets["weak"])
    failed = len(buckets["failed"])

    sent_ok = _get(account, "sent_ok")
    sent_fail = _get(account, "sent_fail")
    truncated = bool(_get(account, "entries_truncated"))
    if truncated:
        # 明细被 ENTRY_CAP 截断时以账本的汇总数为准（否则数字会显得比实际好）
        confirmed = max(confirmed, _int(sent_ok))
        failed = max(failed, _int(sent_fail))

    # 会话列表里没出现的目标、以及选中失败的目标，都没有 entry —— 单独补进「需处理」
    for name, reason in (
        (
            _get(account, "missing") or [],
            "没在会话列表里出现（可能已不是好友，或备注名变了）",
        ),
        (_get(account, "select_failed") or [], "选中失败（DOM 里定位不到这个会话）"),
    ):
        for item in name:
            label = _text(item)
            if not label:
                continue
            failed += 1
            buckets["failed"].append(
                _segment_row(label, "failed", reason=reason, reason_text=reason)
            )

    targets = [_text(item) for item in (data.get("targets") or []) if _text(item)]
    run_targets = [_text(item) for item in (_get(account, "targets") or []) if _text(item)]
    total = max(len(targets), len(run_targets), _int(data.get("target_count")))
    # 配置里的目标数可能比账本里的少（上一轮发完，回头把 TASKS 改小了），
    # 那时分母会出现「已发送 380/2」这种超过 100% 的怪数。分母至少得容得下这一轮
    # 真正处理过的目标数——四个档位加起来仍然恰好等于 total，进度条也不会超过 100%。
    total = max(total, confirmed + weak + failed)

    # 「未尝试」以减法为准（每个目标恰好落在一档里，四个数加起来必然等于 total）；
    # 能点名到具体目标的就写进 pending 档位，点不出名字的只计数、不编名字。
    attempted = confirmed + weak + failed
    pending = max(0, total - attempted)
    covered = {row["target"] for row in entries}
    for name in list(targets) + [item for item in run_targets if item not in targets]:
        if len(buckets["pending"]) >= pending:
            break
        if name not in covered:
            buckets["pending"].append(_segment_row(name, "pending", level_text="未尝试"))
            covered.add(name)

    sent = confirmed + weak
    strong_pct, weak_pct = progress_pair(confirmed, sent, total)

    ready = bool(data.get("ready"))
    gate = _text(data.get("gate")).upper()
    status = _text(data.get("status")).lower()
    orphan = bool(data.get("orphan"))
    # 「运行期掉登录」只写在账本的账号结果里（`core/tasks.py` 的 `out["error"]`），
    # 配置快照 `data` 上没有这个字段 —— 必须从 `account` 上取。
    login_lost = str(_get(account, "error") or "").startswith("login_lost")
    state = _account_state_for(
        data, ready=ready, gate=gate, status=status, attempted=attempted, failed=failed,
        login_lost=login_lost,
    )

    issues: List[Dict[str, str]] = []
    # orphan 卡不往 issues 里塞东西：模板在卡头下面已经有一段说明，
    # 这里只保留"上一轮到底怎么了"的事实（门禁、被跳过），标题也换成"它上一轮的情况"。
    if not ready and not orphan:
        issues.append(
            {
                "level": "danger",
                "text": _text(data.get("cookie_message")) or "没有可用的 cookie，该账号会被跳过",
            }
        )
    if gate and gate != _GATE_OK:
        text = f"上次门禁：{_text(data.get('gate_text'), gate)}"
        if gate in ("EXPIRED", "LOGGED_OUT"):
            # 登录失效是**唯一**能一眼看出该怎么办的门禁失败：重新扫码。
            # 只说"上次门禁：登录已失效"用户还得自己猜下一步做什么。
            text += " —— 到下面「扫码登录」用这个账号重新扫一次即可（登录成功后当天会自动补发）"
        issues.append({"level": "danger", "text": text})
    if login_lost:
        # 与"门禁没过"分开说：门禁是**操作前**就看出来的，这条是**发送时**才暴露的
        # ——页面还长着 IM 的样子（列表、输入框、发送按钮都在），点击也"成功"，
        # 但服务端不认这个登录态，于是每条都拿不到回执，一条也没发出去。
        issues.append({
            "level": "danger",
            "text": "上一轮登录已失效（服务端不认这个 sessionid），一条都没发出去 —— "
                    "到下面「扫码登录」用这个账号重新扫一次即可；"
                    "重新登录成功后，当天会自动补发（不用手点「立即运行」）",
        })
    if status == "skipped":
        issues.append({"level": "warning", "text": "上一轮该账号被跳过"})
    if truncated:
        issues.append(
            {
                "level": "warning",
                "text": f"该轮明细只记录了前 {len(entries_raw)} 条（上限），计数以账本汇总为准",
            }
        )

    return {
        # ---- 配置与凭据（不含任何 cookie 值）
        "unique_id": _text(data.get("unique_id")),
        "username": _text(data.get("username"), "未命名账号"),
        "fingerprint": _text(data.get("fingerprint")),
        "targets": targets or run_targets,
        "target_count": total,
        "cookie_configured": _int(data.get("cookie_configured")),
        "cookie_names": [_text(item) for item in (data.get("cookie_names") or [])],
        "cookie_message": _text(data.get("cookie_message")),
        # 账号专属发送内容（空串 = 没配过，走全局模板）。面板把它填进输入框；
        # 顺手给一个布尔，模板里就不用再写一次判断。
        "message": _text(data.get("message")),
        "has_message": bool(_text(data.get("message"))),
        "message_lines": message_lines(_text(data.get("message"))),
        # 发送时间（每账号可自定义，见 core/schedule.py）。
        **schedule_fields(data.get("unique_id"), _text(data.get("send_time"))),
        "ready": ready,
        "orphan": orphan,
        "gate": gate,
        "gate_text": _text(data.get("gate_text"), store.gate_label(gate)),
        "status": status,
        "status_text": _text(data.get("status_text"), store.status_label(status)),
        "last_run_id": _text(data.get("last_run_id")),
        "last_run_text": _text(data.get("last_run_text")),
        # ---- 本轮（数字来自哪一轮也一并带出去，避免"这是哪次的数据"说不清）
        "run_id": _text(_get(record, "run_id")),
        "run_text": _text(_get(record, "started_text")) or _text(_get(record, "run_id")),
        # 这一轮里没有它：每个账号可以有自己的发送时间，所以"最近一轮里没有这个账号"
        # 是常态。模板据此说明"不是它没发，而是这一轮没轮到它"，别让 0 装作事实。
        "run_missing": record is not None and account is None,
        "own_run_id": "",
        "own_run_text": "",
        "dry_run": _text(_get(record, "dry_run"), "off"),
        "dry_run_text": DRY_RUN_LABELS.get(_text(_get(record, "dry_run"), "off"), ""),
        # ---- 计数（每个目标恰好落在一档里）
        "total": total,
        "confirmed": confirmed,
        "weak": weak,
        "pageEcho": weak,
        "failed": failed,
        "attention": failed,
        "pending": pending,
        "sent": sent,
        "strong_pct": strong_pct,
        "weak_pct": weak_pct,
        "sent_pct": percent(sent, total),
        # ---- 状态与配色
        "state": state,
        "state_label": STATE_LABELS[state],
        "state_css": STATE_CSS[state],
        "chip": STATE_CHIP[state],
        # ---- 明细
        "segments": buckets,
        "segment_counts": {key: len(buckets[key]) for key, _label in SEGMENTS},
        "issues": issues,
    }


def _snapshot_dict(snapshot: Any) -> Dict[str, Any]:
    if hasattr(snapshot, "as_dict"):
        return dict(snapshot.as_dict())
    return dict(snapshot or {})


def _own_record(pool: Sequence[Any], data: Dict[str, Any]) -> Any:
    """池子里（新→旧）**第一条出现过这个账号**的轮次 —— 也就是它自己的最近一次。"""
    for item in pool:
        if _record_account(item, data) is not None:
            return item
    return None


def account_cards(
    snapshots: Sequence[Any],
    record: Any = None,
    *,
    records: Sequence[Any] = (),
    per_account: bool = False,
) -> List[Dict[str, Any]]:
    """把账号列表变成卡片。

    ``per_account=True``（总览页）：每张卡取**它自己最近参与过的那一轮**。每个账号
    可以有自己的发送时间（`core/schedule.py` 的派发器每 5 分钟只为当时到点的账号跑
    一轮），于是"全局最近一轮"里通常只有一两个账号。2026-10-07 线上就这么骗了用户
    一回：wheat 17:35 已经 4/4 强确认，20:35 那一轮只跑了别的账号，它的卡立刻变成
    「未尝试 4 / 还没跑过」。

    ``per_account=False``（运行记录页）：所有卡冻在 ``record`` 那一轮 —— 那一页讲的
    就是"选中的那一轮"。两种情况都会去 ``records`` 里找出账号**自己**的那一轮，写进
    ``own_run_id``/``own_run_text``，好让模板解释"这一轮没有轮到它"。
    """
    pool = [item for item in (records or ()) if item is not None]
    cards: List[Dict[str, Any]] = []
    for snapshot in snapshots or []:
        data = _snapshot_dict(snapshot)
        own = _own_record(pool, data)
        chosen = own if (per_account and own is not None) else record
        if chosen is None and pool:
            chosen = pool[0]
        card = build_account(snapshot, chosen)
        if own is not None and own is not chosen:
            card["own_run_id"] = _text(_get(own, "run_id"))
            card["own_run_text"] = _text(_get(own, "started_text")) or _text(_get(own, "run_id"))
        cards.append(card)
    return cards


# ---------------------------------------------------------------- 汇总


def summarize(cards: Sequence[Dict[str, Any]], last_at: str = "") -> Dict[str, Any]:
    """把账号卡汇总成 B 的 ``summary``（每个目标恰好落在一档里）。

    ``orphan`` 卡（只活在上一轮账本里、`TASKS` 里已经没有的账号）**不计入**：
    下一轮根本不会碰它们，计进去只会让"共 N 个目标"和"账号被阻断"对不上实际。
    """
    live = [card for card in cards if not card.get("orphan")]
    total = sum(_int(card.get("total")) for card in live)
    confirmed = sum(_int(card.get("confirmed")) for card in live)
    weak = sum(_int(card.get("weak")) for card in live)
    failed = sum(_int(card.get("failed")) for card in live)
    pending = sum(_int(card.get("pending")) for card in live)
    blocked_targets = sum(_int(card.get("total")) for card in live if card.get("state") == "blocked")
    sent = confirmed + weak
    strong_pct, weak_pct = progress_pair(confirmed, sent, total)

    return {
        "total": total,
        "confirmed": confirmed,
        "weak": weak,
        "pageEcho": weak,
        # C 里不存在"只看回执就记账"的成功，恒为 0（不渲染这张卡片）
        "receiptOnly": 0,
        "failed": failed,
        "attention": failed,
        "pending": pending,
        # 见模块 docstring：C 没有"已处理但未确认"的中间态，未尝试的目标全在 pending 里
        "unprocessed": 0,
        "remaining": pending,
        "sentTotal": sent,
        "sent": sent,
        "sent_pct": percent(sent, total),
        "strong_pct": strong_pct,
        "weak_pct": weak_pct,
        "blocked_targets": blocked_targets,
        "accounts": len(live),
        "accounts_blocked": sum(1 for card in live if card.get("state") == "blocked"),
        "accounts_attention": sum(1 for card in live if card.get("state") == "attention"),
        "accounts_orphan": len(cards) - len(live),
        # 这一格叫「最近强确认」，说明文字是"通过会话选择 + 回执双确认"。
        # 只有弱证据（只拿到页面回显）时不能点亮它，否则数字和说明互相打脸；
        # 那时由 lastWeakAt 单独承载，模板用一句"只有弱证据"来讲。
        "lastConfirmedAt": last_at if confirmed else "",
        "lastWeakAt": last_at if (not confirmed and weak) else "",
    }


# ---------------------------------------------------------------- 运行状态


def run_row(record: Any) -> Dict[str, Any]:
    """账本里的一轮 → 「运行记录」页表格的一行。"""
    status = _text(_get(record, "status"), "ok").lower()
    counts = _get(record, "counts") or {}
    return {
        "run_id": _text(_get(record, "run_id")),
        "started_text": _text(_get(record, "started_text")) or _text(_get(record, "started_at")),
        "finished_text": _text(_get(record, "finished_text")),
        "duration_text": _text(_get(record, "duration_text")),
        "status": status,
        "status_text": _text(_get(record, "status_text"), store.status_label(status)),
        "chip": {
            "ok": "success",
            "error": "danger",
            "limited": "danger",
            "skipped": "warning",
            # 没干活的两档：整轮跳过、账号全被拦下、没有任何账号结果，都不算成功
            "empty": "warning",
            "blocked": "warning",
        }.get(status, "warning"),
        "dry_run": _text(_get(record, "dry_run"), "off"),
        "dry_run_text": DRY_RUN_LABELS.get(_text(_get(record, "dry_run"), "off"), ""),
        "source": _text(_get(record, "source")),
        "source_text": _text(_get(record, "source_text"), store.source_text(_text(_get(record, "source")))),
        "rate_limited": bool(_get(record, "rate_limited")),
        "skip_reason": _text(_get(record, "skip_reason")),
        "skip_until": _text(_get(record, "skip_until")),
        "note": _text(_get(record, "note")),
        "accounts": _int(_get(counts, "accounts")),
        "sent_ok": _int(_get(counts, "sent_ok")),
        "sent_fail": _int(_get(counts, "sent_fail")),
        "entries": _int(_get(counts, "entries")),
        "is_latest": False,
    }


def until_text(until: Any, seconds_left: Any = 0) -> str:
    """把"截止时间"渲染成胶囊里的短文案（``还剩 27 分`` / ``至 21:00``）。"""
    text = _text(until)
    if not text:
        return ""
    seconds = _int(seconds_left)
    if seconds > 0:
        return "还剩 " + human_left(seconds)
    try:
        deadline = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return "至 " + deadline.strftime("%H:%M")


def human_left(seconds: int) -> str:
    """秒数转人话：``27 分 12 秒`` / ``2 小时 3 分``。"""
    seconds = max(0, _int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def render_message(template: Any) -> str:
    """把 ``.env`` 里的模板渲染成"实际会发出去的样子"。

    规则照抄 ``core/douyin_im.split_message_lines()``（那一侧的单元测试是权威）：
    字面 ``\\r\\n`` → 字面 ``\\n`` → 真换行，再收掉裸露的 ``\\r``。
    面板只用它来预览；真正切分永远由 core 做。
    """
    text = _text(template)
    if not text:
        return ""
    return (
        text.replace("\\r\\n", "\\n")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def message_lines(template: Any) -> int:
    """模板实际会发几行（空行不算）。"""
    text = render_message(template)
    if not text:
        return 0
    return len([line for line in text.splitlines() if line.strip()])


def age_seconds(started_at: Any, now: Optional[datetime] = None) -> int:
    """锁已经持有了多少秒；算不出来就返回 0。

    B 的轮询器会把 ``task.ageSeconds`` 写进横幅（"已运行约 N 秒"），
    所以这个值必须是真的 —— 恒 0 会让人以为任务刚起来。
    """
    text = _text(started_at)
    if not text:
        return 0
    try:
        started = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = int((current - started).total_seconds())
    return seconds if seconds > 0 else 0


def _task_state(lock: Any) -> Dict[str, Any]:
    running = bool(_get(lock, "running"))
    dry_run = _text(_get(lock, "dry_run"), "off")
    started_at = _get(lock, "started_at")
    return {
        "running": running,
        # 锁文件在不在：只有"文件在、进程不在"才值得提示一句；
        # 文件本来就没有时 store 也会给个 reason（"锁文件不存在"），别拿它当横幅。
        "exists": bool(_get(lock, "exists")),
        "pid": _get(lock, "pid"),
        "run_id": _text(_get(lock, "run_id")),
        "dry_run": dry_run,
        "dry_run_text": DRY_RUN_LABELS.get(dry_run, dry_run),
        "source": _text(_get(lock, "source")),
        "source_text": store.source_text(_text(_get(lock, "source"))),
        "started_text": store.fmt_stamp(started_at) if started_at else "",
        "reason": _text(_get(lock, "reason")),
        # C 的锁只要进程活着就不算陈旧（见 core.run_ledger.lock_info）：
        # 「跑了很久」不等于「卡死」，页面上不吓唬人。
        "stale": False,
        "ageSeconds": age_seconds(started_at) if running else 0,
    }


def _quiet_clash(spec: str, timer_times: Sequence[str]) -> Dict[str, Any]:
    """禁跑时间窗会不会把定时器的时间整个盖掉。

    盖掉的后果不是"晚一点发"，而是**到点整轮跳过、一条都不发**，而且只有翻账本
    才看得出来（2026-10-01 改定时时间时就差点踩上：`.env` 没写 QUIET_WINDOW、
    代码默认窗 20:20-21:00，正好把新的 20:30 包住）。面板必须提前说。
    """
    spec = _text(spec)
    times = [_text(item) for item in (timer_times or []) if _text(item)]
    if not spec or not times:
        return {}
    from core import quiet_window  # 函数内 import：core 不在 webui 的 import 期出现

    # 关掉的写法（off/none/…）与不合法的写法都会被 parse 判成 None —— 后者由
    # `check()` 负责放行并报出 spec，这里不必再判一遍。
    window = quiet_window.parse(spec)
    if window is None:
        return {}
    clash: List[str] = []
    for item in times:
        hour, _, minute = item.partition(":")
        try:
            at = datetime(2000, 1, 1, int(hour), int(minute))
        except (TypeError, ValueError):
            continue
        if quiet_window.in_window(at, window):
            clash.append(item)
    if not clash:
        return {}
    return {
        "spec": spec,
        "times": clash,
        "text": f"禁跑时间窗 {spec} 盖住了定时器的 {'、'.join(clash)} —— 到点会整轮跳过，一条都不会发。",
    }


def _schedule_state(records: Sequence[Any], pending: int, next_run: Any, schedules: Sequence[str],
                    timer: Optional[Dict[str, Any]] = None,
                    quiet: Optional[Dict[str, Any]] = None,
                    next_due: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    labels = [_text(item) for item in (schedules or []) if _text(item)]
    timer = dict(timer or {})
    # 新架构：timer 是每 N 分钟敲一次门的**派发器**，`OnCalendar` 里没有"几点发"。
    # 这时候不能再按 `.env` 的 TASK_SCHEDULE_TIMES 画一个"下次自动触发" ——
    # 那个值现在的含义只是"没单独配时间的账号用几点"。
    dispatch = _int(timer.get("dispatchMinutes"))
    # `.env` 里的 TASK_SCHEDULE_TIMES 只描述"打算几点跑"；真正会不会跑、几点跑
    # 由 systemd timer 说了算。timer 是 disabled 的时候，面板绝不能画一个不会
    # 发生的"下次自动触发"。
    armed = _text(timer.get("state")) in ("enabled", "")
    timer_times = [_text(item) for item in (timer.get("onCalendar") or []) if _text(item)]
    # 单元文件里的时间才是真的；`.env` 和它对不上就明说，别让两个地方各说各话。
    mismatch = bool(armed and timer_times and labels and timer_times != labels)
    if dispatch:
        label = f"每 {dispatch} 分钟敲一次门，各账号按自己的时间发"
        if next_due:
            next_iso = _text(next_due.get("at"))
            next_text = _text(next_due.get("text"))
        else:
            next_iso = ""
            next_text = "今天该发的都发完了"
    elif next_run is not None:
        label = "每日 " + "、".join(labels) if labels else "已配置定时"
        next_iso = next_run.isoformat()
        next_text = store.fmt_stamp(next_iso)
    else:
        label = "未配置定时任务"
        next_iso = ""
        next_text = ""
    if not armed:
        label = "定时器未启用"
        next_text = ""
        next_iso = ""
    elif timer_times:
        label = "系统定时器：" + "、".join(timer_times)
        if timer.get("jitterText"):
            label += f"（{timer['jitterText']}）"
    return {
        "times": labels,
        "label": label,
        "nextTriggerAt": next_iso,
        "nextTriggerText": next_text,
        "remainingTargets": pending,
        "hasWorkNextTrigger": pending > 0 and armed,
        # 派发器模式（timer 每 N 分钟敲一次门、各账号按自己的时间发）：上面那两个
        # 键讲的是"上一轮还剩几个目标没走到"，跟"下一次触发会不会发"没关系 ——
        # 前端要靠这个标志改用 nextTriggerText 说话。
        "dispatcher": bool(dispatch),
        "lastRunId": _text(_get(records[0], "run_id")) if records else "",
        "timer": timer,
        "armed": armed,
        "timerTimes": timer_times,
        "timerMismatch": mismatch,
        "quietClash": _quiet_clash(
            _text(_get(quiet or {}, "spec")), timer_times if armed else []
        ),
        "timerText": {
            "enabled": "定时器已启用",
            "disabled": "定时器未启用",
            "missing": "没装定时器单元",
        }.get(_text(timer.get("state")), "定时器状态未知"),
    }


def _banners(
    *,
    cooldown: Dict[str, Any],
    quiet: Dict[str, Any],
    task: Dict[str, Any],
    summary: Dict[str, Any],
    missing_browser: Sequence[str],
    blocked_reason: str,
    last_skip: str = "",
    last_skip_until: str = "",
) -> List[Dict[str, Any]]:
    """页面顶部的状态横幅（B 的 ``status-banner``）。顺序 = 紧急程度。"""
    banners: List[Dict[str, Any]] = []
    if cooldown.get("active"):
        keyword = cooldown.get("keyword") or "（未记录关键词）"
        banners.append(
            {
                "kind": "cooldown",
                "level": "warning",
                "title": "风控冷却中",
                "text": f"命中关键词「{keyword}」，冷却至 {cooldown.get('until_text') or cooldown.get('until')}；"
                "冷却期间任何触发（含定时任务）都会整轮跳过。",
                "until": cooldown.get("until") or "",
                "secondsLeft": _int(cooldown.get("seconds_left")),
            }
        )
    if quiet.get("active"):
        banners.append(
            {
                "kind": "quiet_window",
                "level": "warning",
                "title": "禁跑时间窗内",
                "text": f"{quiet.get('reason') or '当前处于禁跑时间窗'}；"
                "这段时间整轮不发。到「运行配置」里改 QUIET_WINDOW 可调整窗口，"
                "设 QUIET_WINDOW_OVERRIDE=1 可强行放行。",
                "until": quiet.get("until") or "",
                "secondsLeft": 0,
            }
        )
    if missing_browser:
        banners.append(
            {
                "kind": "browser",
                "level": "warning",
                "title": "缺少浏览器依赖",
                "text": f"本机找不到 {' / '.join(missing_browser)}，无法执行任务；查看类功能不受影响。",
                "until": "",
                "secondsLeft": 0,
            }
        )
    if task.get("running"):
        banners.append(
            {
                "kind": "running",
                "level": "info",
                "title": "正在运行",
                "text": f"run_id={task.get('run_id')} · {task.get('dry_run_text')}"
                f" · 来源 {task.get('source_text')} · 起始 {task.get('started_text') or '—'}",
                "until": "",
                "secondsLeft": 0,
            }
        )
    if task.get("exists") and task.get("reason") and not task.get("running"):
        banners.append(
            {
                "kind": "lock",
                "level": "info",
                "title": "运行锁未释放",
                "text": _text(task.get("reason")),
                "until": "",
                "secondsLeft": 0,
            }
        )
    if summary.get("accounts_blocked"):
        banners.append(
            {
                "kind": "blocked",
                "level": "warning",
                "title": f"{summary['accounts_blocked']} 个账号被阻断",
                "text": f"共 {summary.get('blocked_targets')} 个目标本轮发不出去，"
                "通常是登录失效或缺 cookie；下面「账号与目标好友」里每个账号都写了原因。",
                "until": "",
                "secondsLeft": 0,
            }
        )
    if last_skip:
        detail = f"原因：{last_skip}"
        if last_skip_until:
            detail += f"（跳过至 {last_skip_until}）"
        banners.append(
            {
                "kind": "skipped",
                "level": "warning",
                "title": "上一轮整轮跳过",
                "text": f"{detail}。这一轮没有对任何账号做操作。",
                "until": "",
                "secondsLeft": 0,
            }
        )
    if blocked_reason and not banners:
        banners.append(
            {
                "kind": "blocked",
                "level": "info",
                "title": "当前不能立即运行",
                "text": blocked_reason,
                "until": "",
                "secondsLeft": 0,
            }
        )
    for banner in banners:
        banner["icon"] = BANNER_ICONS.get(banner["kind"], "info")
        banner["untilText"] = until_text(banner.get("until"), banner.get("secondsLeft", 0))
    return banners


# ---------------------------------------------------------------- 配置项

# 可改的那些行不在这里：它们在 `webui/settings.py` 的白名单里，值取自 `.env` 原文，
# 由首页的「运行配置」表单渲染与保存。这里只剩**只读**的行 —— 它们不是"配置项"，
# 而是"事实"：定时时间由 systemd 决定，另外几个是文件位置。
def config_rows(cfg: Any, paths: Optional[Dict[str, str]] = None,
                schedule: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """只读配置行（定时时间 + 文件位置 + 账号配置错误）。"""
    schedule = dict(schedule or {})
    timer_times = [_text(item) for item in (schedule.get("timerTimes") or []) if _text(item)]
    # 这一行的值必须写"真正会跑的时间"：`.env` 里的 TASK_SCHEDULE_TIMES 只是意图，
    # 定时器没启用时它一秒都不会触发（真机上就这么误导过用户）。
    if timer_times:
        when = "、".join(timer_times)
    else:
        when = _text(_get(cfg, "schedule_text"), "未配置")
    schedule_hint = "由 systemd 定时器决定"
    if not schedule.get("armed", True):
        when += "（系统定时器未启用，实际不会自动跑）"
    elif schedule.get("timerMismatch"):
        when = f"{when}（.env 里写的是 {_text(_get(cfg, 'schedule_text'), '未配置')}）"
    rows: List[Dict[str, str]] = [
        {"label": "定时时间", "value": when, "hint": schedule_hint},
    ]
    for label, value in (paths or {}).items():
        if _text(value):
            rows.append({"label": label, "value": _text(value), "hint": "文件位置"})
    error = _text(_get(cfg, "tasks_error"))
    if error:
        rows.append({"label": "账号配置", "value": error, "hint": "读配置时就失败了"})
    return rows


# ---------------------------------------------------------------- 总装


def build(
    *,
    records: Sequence[Any] = (),
    accounts: Sequence[Any] = (),
    lock: Any = None,
    rate_limit: Any = None,
    cfg: Any = None,
    next_run: Any = None,
    schedules: Sequence[str] = (),
    quiet: Optional[Dict[str, Any]] = None,
    missing_browser: Sequence[str] = (),
    timer: Optional[Dict[str, Any]] = None,
    next_due: Optional[Dict[str, Any]] = None,
    user: str = "",
    default_dry_run: str = "off",
    cooldown_minutes: str = "",
    paths: Optional[Dict[str, str]] = None,
    selected: Any = None,
    per_account: bool = False,
) -> Dict[str, Any]:
    """一次请求的完整视图模型（模板与 ``/api/ops/overview`` 共用）。

    ``per_account=True``：账号卡各取各的最近一轮（总览页 —— 每个账号有自己的发送
    时间）。运行记录页传 False，那边讲的是"选中的那一轮"。
    """
    records = list(records or [])
    if selected is None:
        selected = next((r for r in records if _get(r, "accounts")), None)
    if selected is None and records:
        selected = records[0]

    cards = account_cards(accounts, selected, records=records, per_account=per_account)
    finished = _text(_get(selected, "finished_at"))
    summary = summarize(cards, last_at=store.fmt_stamp(finished) if finished else "")
    # 这一版的数字来自哪一轮、那一轮是真发还是空跑 —— 直接写在 KPI 旁边，
    # 免得"确认成功 5"看上去像真发出去了 5 条（空跑的确认只是排练）。
    summary["run_id"] = _text(_get(selected, "run_id"))
    summary["dry_run"] = _text(_get(selected, "dry_run"), "off")
    summary["dry_run_text"] = DRY_RUN_LABELS.get(summary["dry_run"], summary["dry_run"])
    # 各账号各算各的时候，"数字来自哪一轮"可能不止一轮：这时不能挑一个 run_id 顶上，
    # 否则接口读到的那个 ID 只对其中一张卡是真的。同理，档位（真发/空跑）也必须
    # 所有卡一致才敢在 KPI 旁边写一句全局的话 —— 不一致就明说不一致。
    card_runs = {_text(card.get("run_id")) for card in cards if _text(card.get("run_id"))}
    card_dry = {
        _text(card.get("dry_run"), "off") for card in cards if not card.get("orphan")
    }
    summary["run_count"] = len(card_runs)
    summary["mixed_runs"] = bool(per_account and len(card_runs) > 1)
    if summary["mixed_runs"]:
        summary["run_id"] = ""
    if per_account:
        if len(card_dry) > 1:
            summary["dry_run"] = "mixed"
            summary["dry_run_text"] = "各账号档位不一致"
        elif card_dry:
            only = _text(next(iter(card_dry)), "off")
            summary["dry_run"] = only
            summary["dry_run_text"] = DRY_RUN_LABELS.get(only, only)

    rate = rate_limit
    cooldown = rate.as_dict() if hasattr(rate, "as_dict") else dict(rate or {})
    quiet_info = _jsonable(quiet)
    task = _task_state(lock)
    schedule = _schedule_state(
        records, summary["pending"], next_run, schedules, timer=timer, quiet=quiet_info,
        next_due=next_due,
    )

    level = _text(default_dry_run, "off").lower()
    if level not in DRY_RUN_LEVELS:
        level = "off"

    browsers_missing = [_text(name) for name in (missing_browser or []) if _text(name)]
    blockers: List[str] = []
    if task["running"]:
        blockers.append(f"已有一轮任务在运行（run_id={task['run_id']}）")
    if cooldown.get("active"):
        blockers.append(f"风控冷却中，至 {cooldown.get('until_text') or cooldown.get('until')}")
    if quiet_info.get("active"):
        blockers.append(quiet_info.get("reason") or "当前处于禁跑时间窗")
    if browsers_missing:
        blockers.append(f"缺少浏览器依赖：{' / '.join(browsers_missing)}")

    last_run = run_row(selected) if selected is not None else None
    if last_run is not None:
        last_run["is_latest"] = bool(records) and selected is records[0]

    runs = [run_row(record) for record in records[:50]]
    if runs:
        runs[0]["is_latest"] = True

    template = _text(_get(cfg, "message_template"))

    return {
        "accounts": cards,
        "summary": summary,
        "task": task,
        "schedule": schedule,
        "cooldown": cooldown,
        "quiet_window": quiet_info,
        "banners": _banners(
            cooldown=cooldown,
            quiet=quiet_info,
            task=task,
            summary=summary,
            missing_browser=browsers_missing,
            blocked_reason=blockers[0] if blockers else "",
            last_skip=_text(_get(selected, "skip_reason")),
            last_skip_until=_text(_get(selected, "skip_until")),
        ),
        "blockers": blockers,
        "can_run": not blockers,
        "config_rows": config_rows(cfg, paths, schedule),
        "message_template": template,
        "message_template_rendered": render_message(template),
        "message_template_lines": message_lines(template),
        "dry_run": {
            "levels": list(DRY_RUN_LEVELS),
            "labels": dict(DRY_RUN_LABELS),
            "hints": dict(DRY_RUN_HINTS),
            "default": level,
        },
        "segments": [{"key": key, "label": label} for key, label in SEGMENTS],
        "last_run": last_run,
        "runs": runs,
        "selected_run_id": _text(_get(selected, "run_id")),
        "user": user,
        "missing_browser": browsers_missing,
        "has_records": bool(records),
        "has_entries": any(card["segment_counts"]["success"] for card in cards)
        or any(card["segment_counts"]["weak"] for card in cards),
    }


def overview_payload(view: Dict[str, Any], *, now_iso: str = "", tz: str = "") -> Dict[str, Any]:
    """``/api/ops/overview`` —— 键名必须与 B 的 ``static/app.js`` 完全一致。"""
    summary = view.get("summary") or {}
    task = view.get("task") or {}
    schedule = view.get("schedule") or {}
    accounts = view.get("accounts") or []
    return {
        "ok": True,
        "now": now_iso,
        "tz": tz,
        "summary": {
            "total": summary.get("total", 0),
            "confirmed": summary.get("confirmed", 0),
            "pageEcho": summary.get("pageEcho", 0),
            "receiptOnly": summary.get("receiptOnly", 0),
            "attention": summary.get("attention", 0),
            "pending": summary.get("pending", 0),
            "unprocessed": summary.get("unprocessed", 0),
            "remaining": summary.get("remaining", 0),
            "sentTotal": summary.get("sentTotal", 0),
            "progressPercent": percent(summary.get("sentTotal"), summary.get("total")),
            "lastConfirmedAt": summary.get("lastConfirmedAt", ""),
            "lastWeakAt": summary.get("lastWeakAt", ""),
        },
        "task": {
            "running": bool(task.get("running")),
            "stale": bool(task.get("stale")),
            "ageSeconds": _int(task.get("ageSeconds")),
        },
        "schedule": {
            "nextTriggerAt": schedule.get("nextTriggerAt", ""),
            "label": schedule.get("label", ""),
            # 派发器模式下这句才是"下一次触发会干什么"的真话（"下一个 wheat：今天
            # 17:20" / "今天该发的都发完了"），前端要拿它写 KPI 下面那行小字。
            "nextTriggerText": schedule.get("nextTriggerText", ""),
            "remainingTargets": schedule.get("remainingTargets", 0),
            "hasWorkNextTrigger": bool(schedule.get("hasWorkNextTrigger")),
            # 派发器模式：前端不许拿"上一轮剩几个目标"当"下次触发还有活干"。
            "dispatcher": bool(schedule.get("dispatcher")),
            # 定时器是 disabled 的时候，前端**不能**把"下次触发"画成一个时间：
            # 到点什么都不会发生（真机上就是这么骗了用户一回）。
            "armed": bool(schedule.get("armed", True)),
            "timerText": schedule.get("timerText", ""),
            "timerMismatch": bool(schedule.get("timerMismatch")),
            "timerTimes": list(schedule.get("timerTimes") or []),
        },
        "accounts": [
            {
                "uniqueId": card.get("unique_id", ""),
                "state": card.get("state", "idle"),
                # 前端要按 state_css 上色（"orphan" 这种状态没有对应的配色规则），
                # 也要知道上一轮是不是空跑 —— 否则轮询一刷新就把服务端写的标记抹掉。
                "stateCss": card.get("state_css", ""),
                "total": card.get("total", 0),
                "confirmed": card.get("confirmed", 0),
                "pageEcho": card.get("pageEcho", 0),
                "attention": card.get("attention", 0),
                "pending": card.get("pending", 0),
                "dryRun": card.get("dry_run", "off"),
                "dryRunText": card.get("dry_run_text", ""),
            }
            for card in accounts
        ],
    }
