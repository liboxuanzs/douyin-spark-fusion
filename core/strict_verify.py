"""严格确认（fail-closed）判定 —— 纯函数，不碰浏览器。

## 为什么要有这个模块

上游 C 的 `DouyinIM._select_and_verify()` 有三处在**证据不足时返回成功**：

    cur.get("convId") is None   → 算"确认了"
    want 为空                   → 算"确认了"
    只有网络信号（响应里出现过 conv_id）→ 算"确认了"

后果是**发错人**：选中态其实还停在上一个会话，调用方已经往那里打字发送。

这里把判定从浏览器代码里抽出来，做成可离线单测的纯函数，并且把口径统一成一句：

    **证据不足 = 失败**（fail-closed），而不是"没发现不对就算过"。

## 三值语义

每个信号只能给出 命中(True) / 不命中(False) / 未知(None) 三种结论：

* 只要**有一个信号不命中** → 整体失败（明确知道自己错了，最硬的否决）
* 否则**至少有一个信号命中** → 整体通过
* 全部未知 → 失败（证据不足）

"不命中"优先于"命中"：宁可判失败重试，也不能在自相矛盾的证据下发送。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.douyin_im import _is_group, _norm, peer_uid_of

# ---- 判定结果 ------------------------------------------------------------

LEVEL_STRONG = "strong"   # conv_id 级证据：确定性判断
LEVEL_MEDIUM = "medium"   # 显示名精确相等：确定性判断（弱于 conv_id）
LEVEL_NONE = "none"       # 没有任何命中证据
LEVEL_CONFLICT = "conflict"  # 有明确的**反证**

# 选中确认的失败原因（调用方按这个分流重试/告警）
R_NO_CONV_ID = "no_conv_id"            # DOM 明确没有选中会话
R_CONV_ID_UNREADABLE = "conv_id_unreadable"  # 有选中态但 fiber 读不到 id
R_IDENTITY_UNKNOWN = "identity_unknown"      # 目标身份不足以强校验
R_IDENTITY_MISMATCH = "identity_mismatch"    # conv_id / uid 指向另一个人
R_TITLE_MISMATCH = "title_mismatch"          # 选中项标题与目标不符（且没有强标识背书）
R_CONV_MISMATCH = "conv_mismatch"            # 网络信号指向目标，但选中的不是它
R_NO_EVIDENCE = "no_evidence"                # 什么都没观察到


@dataclass
class Verdict:
    """一次严格确认的结论。`ok` 是唯一给调用方做分支的字段。"""

    ok: bool
    level: str = LEVEL_NONE
    reason: str = ""
    detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:  # 让 `if verify(...)` 直接可用
        return self.ok

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "level": self.level,
            "reason": self.reason,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


# ---- 身份期望 ------------------------------------------------------------

@dataclass
class Expectation:
    """校验一个会话项要用到的全部身份信息。"""

    raw: str                 # 用户填的原始关键词
    norm: str                # 归一化后的关键词
    conv_id: Optional[str] = None   # 由 uid 复算出来的会话 id（最硬的判据）
    peer_uid: Optional[str] = None
    display: str = ""        # 归一化后的期望显示名
    matched_by: str = ""     # 命中所用的字段（remark/nickname/douyin_id/uid/title/fuzzy）
    ambiguous: bool = False  # 匹配到多个不同对象
    candidates: List[str] = field(default_factory=list)

    @property
    def strong(self) -> bool:
        """是否具备"不会认错人"的强标识。

        conv_id 或 peer_uid 二者之一是强标识；
        仅凭显示名（尤其 fuzzy 子串）**不算** —— 重名会把消息发错人。
        """
        return bool(self.conv_id or self.peer_uid)


def assemble_conv_id(biz: int, uid_a, uid_b) -> Optional[str]:
    """复刻官方 assembleConvId：`{biz}:1:{小uid}:{大uid}`（按**数值**升序）。

    与 `core/douyin_im.peer_uid_of()` 互逆，用来把「我知道对方 uid」变成
    「我能算出任何一个人的 conv_id」—— 这样校验就不依赖页面给不给 id。
    """
    a, b = str(uid_a or ""), str(uid_b or "")
    if not a.isdigit() or not b.isdigit():
        return None
    lo, hi = (a, b) if int(a) < int(b) else (b, a)
    if lo == hi:                      # 自己跟自己不是合法单聊（peer_uid_of 亦如此）
        return None
    return f"{biz}:1:{lo}:{hi}"


def build_expectation(raw, *, self_uid=None, peer_uid=None, biz=0,
                      conv_id=None, display="", matched_by="", ambiguous=False,
                      candidates=None) -> Expectation:
    """按已知信息构造期望；信息越全，判定越硬。

    `conv_id` 是**扫描时从会话列表 fiber 直接读到的目标会话 id**，比反算出来的硬：
    反算（`assemble_conv_id`）要 `self_uid`，而 `self_uid` 拿不到是常态
    （DOM 兜底判登录时返回 None），那种情况下 conv_id 与 peer_uid 两个信号会一起
    消失，判定只剩显示名 —— 重名就会放行。给了这个参数就不再依赖 self_uid。

    `ambiguous` / `candidates` 由扫描层给出：关键词命中多个**不同**对象时置位，
    判定层据此直接拒绝（宁可让用户补全关键词，也不能赌一个）。
    """
    conv_id = str(conv_id) if conv_id else None
    if not conv_id and self_uid and peer_uid:
        conv_id = assemble_conv_id(biz, self_uid, peer_uid)
    return Expectation(
        raw=raw,
        norm=_norm(raw),
        conv_id=conv_id,
        peer_uid=str(peer_uid) if peer_uid else None,
        display=_norm(display or raw),
        matched_by=matched_by,
        ambiguous=bool(ambiguous),
        candidates=list(candidates or []),
    )


# ---- 三个信号 ------------------------------------------------------------

def signal_conv_id(want, cur_conv) -> Optional[bool]:
    """会话 id 信号。

    `cur_conv` 是 `JS_CURRENT_CONV` 的返回值，可能是 None（没有选中态）。
    """
    if not want:
        return None                       # 自己没有期望值 → 这个信号无话可说
    if not cur_conv:
        return False                      # 明确没有选中会话
    got = cur_conv.get("convId")
    if not got:
        return None                       # 有选中态但 fiber 读不到 id → 未知
    return str(got) == str(want)


def signal_peer_uid(expect: Expectation, cur_conv, self_uid) -> Optional[bool]:
    """由选中会话反推对方的 uid，与期望比对。

    conv_id 缺失时退回 `peer_uid` 字段（同一条 fiber 上取到的）。
    """
    if not expect.peer_uid or not self_uid:
        return None
    got = None
    if cur_conv:
        got = peer_uid_of(cur_conv.get("convId") or "", self_uid)
        if not got:
            got = cur_conv.get("peerUid") or None
    if not got:
        return None
    return str(got) == str(expect.peer_uid)


def signal_title(expect: Expectation, cur_conv) -> Optional[bool]:
    """显示名信号：归一化后**精确相等**才算命中。

    刻意不做子串匹配 —— 上游 `_match()` 的 fuzzy 只是"扫列表时觉得可能是它"，
    绝不能当作"选中的就是它"。
    """
    if not expect.display:
        return None
    if not cur_conv:
        return None
    got = _norm(cur_conv.get("title") or "")
    if not got:
        return None
    return got == expect.display


def signal_network(want, net_hit: bool) -> Optional[bool]:
    """网络信号：只作**辅助**，单独永远不足以通过。

    它证明"点开了某个会话并拉到了消息"，但证明不了"拉的是目标那个会话"
    —— 上游正是把这条当成功依据才出的漏洞。
    """
    if net_hit:
        return True if want else None
    return None


# ---- 组合 ----------------------------------------------------------------

def _combine(signals: Dict[str, Optional[bool]], *, strong: bool = True,
             cur_conv=None) -> Verdict:
    """三值组合。

    `strong` 表示"目标身份是否足以做硬校验"（知道 conv_id 或对方 uid）。
    它为假时只能靠显示名，判定降级但不拒绝 —— 拒绝会让"只填昵称"的常规用法
    完全不可用，而显示名**精确相等**在归一化后仍是很强的信号（重名问题由
    `ambiguous` 拦截，见 `verify_selection`）。

    规则（按优先级）：

    1. `conv_id` 不命中时先分两种：**根本没有选中态** = 证据不足
       (`R_NO_CONV_ID`)；**选中了别人** = 反证 (`R_IDENTITY_MISMATCH`)
    2. 对方 uid 命中 → 通过；uid 不命中 → 反证
    3. 标题不命中，且没有硬证据背书 → 反证 (`R_TITLE_MISMATCH`)
       （有硬证据命中时标题对不上只是"改过名"，不算反证）
    4. 硬证据命中 → strong；只有标题命中 → medium
    5. 只有网络信号 → 失败（上游正是把这条当成功依据才出的漏洞）
    6. 全未知 → 失败（证据不足）
    """
    known = {k: v for k, v in signals.items() if v is not None}
    failed = [k for k, v in known.items() if v is False]
    passed = [k for k, v in known.items() if v is True]
    evidence = {"signals": dict(signals), "failed": failed, "passed": passed,
                "strong": strong}

    strong_ok = "conv_id" in passed or "peer_uid" in passed

    def fail(level, reason, detail):
        return Verdict(False, level, reason, detail, evidence)

    if "conv_id" in failed:
        if cur_conv is None:
            return fail(LEVEL_NONE, R_NO_CONV_ID, "页面上没有选中态")
        return fail(LEVEL_CONFLICT, R_IDENTITY_MISMATCH,
                    f"反证：选中了另一个会话（signals={signals}）")
    if "peer_uid" in failed:
        return fail(LEVEL_CONFLICT, R_IDENTITY_MISMATCH,
                    f"反证：选中会话的对方 uid 不是目标（signals={signals}）")
    if "title" in failed and not strong_ok:
        return fail(LEVEL_CONFLICT, R_TITLE_MISMATCH,
                    f"反证：标题与目标不符（signals={signals}）")

    if strong_ok:
        return Verdict(True, LEVEL_STRONG, "", "命中：" + ",".join(passed), evidence)
    if "title" in passed:
        return Verdict(True, LEVEL_MEDIUM, "",
                       "命中：title（无 conv_id / uid 背书，仅显示名）", evidence)
    if "network" in passed:
        return fail(LEVEL_NONE, R_CONV_MISMATCH,
                    "网络信号指向目标，但 DOM 没有确认选中的是它")
    if not strong:
        return fail(LEVEL_NONE, R_IDENTITY_UNKNOWN,
                    "目标身份不足以强校验，且显示名也没对上")
    return fail(LEVEL_NONE, R_NO_EVIDENCE, "没有任何命中信号")


def verify_selection(expect: Expectation, *, cur_conv, net_hit=False,
                     self_uid=None) -> Verdict:
    """判断"目标会话是否真的已被选中"。

    Args:
        expect:  期望身份（见 `build_expectation`）
        cur_conv: `JS_CURRENT_CONV` 的返回值；None 表示页面上没有选中态
        net_hit: 点击后是否观察到 `get_by_conversation` 响应里出现目标 conv_id
        self_uid: 自己的 uid（用来从 conv_id 反推对方）
    """
    if expect.ambiguous:
        return Verdict(False, LEVEL_NONE, R_IDENTITY_UNKNOWN,
                       "关键词命中多个不同对象，拒绝自动选择",
                       {"candidates": list(expect.candidates),
                        "signals": {}, "strong": expect.strong})

    signals = {
        "conv_id": signal_conv_id(expect.conv_id, cur_conv),
        # 对方 uid 反推：优先用 conv_id 算，读不到时退回页面直接给的 peerUid
        # （两者同源）。没有 self_uid 就别装作能算。
        "peer_uid": signal_peer_uid(expect, cur_conv, self_uid) if self_uid else None,
        "title": signal_title(expect, cur_conv),
        "network": signal_network(expect.conv_id, net_hit),
    }
    verdict = _combine(signals, strong=expect.strong, cur_conv=cur_conv)

    if verdict.ok:
        return verdict

    # 失败原因细化（只为了让日志和仪表盘好读，不影响 ok）
    if verdict.reason == R_NO_EVIDENCE:
        if not expect.strong:
            verdict.reason = R_IDENTITY_UNKNOWN
            verdict.detail = (f"关键词 {expect.raw!r} 只能靠显示名匹配"
                              f"（依据={expect.matched_by or '未知'}），无法强校验")
        elif cur_conv is None:
            verdict.reason = R_NO_CONV_ID
            verdict.detail = "页面上没有选中态（.curConversation 不存在）"
        elif not cur_conv.get("convId"):
            verdict.reason = R_CONV_ID_UNREADABLE
            verdict.detail = "有选中态但读不到 convId，标题也对不上"
    verdict.evidence["expect"] = {
        "raw": expect.raw, "conv_id": expect.conv_id, "peer_uid": expect.peer_uid,
        "display": expect.display, "matched_by": expect.matched_by,
    }
    return verdict


# ---- 发送回执 ------------------------------------------------------------

def _conv_holds_peer(conv_id, peer_uid) -> Optional[bool]:
    """这个单聊 conv_id 的参与者里有 peer_uid 吗？

    只看 conv id 自己（`0:1:<uidA>:<uidB>`，参与者升序）—— 不依赖 `self_uid`，
    因为 `self_uid` 也可能取不到（DOM 兜底判登录那种情况），而"收件人是谁"
    恰恰是回执判定的关键，不能因为少一个参数就跳过核验。
    不是单聊形状（群聊 / 没取到 id）返回 None：未知就是未知，不许当成"对上了"。
    """
    p = str(conv_id or "").split(":")
    if len(p) != 4 or p[1] != "1" or not peer_uid:
        return None
    return str(peer_uid) in (p[2], p[3])


def verify_receipt(expect: Expectation, *, before, after,
                   receipts=None, self_uid=None) -> Verdict:
    """判断"消息是否真的发到了目标会话"。

    两道证据，**都要**看：

    * DOM：同一会话内自己发的消息气泡数变多（`before`/`after` 来自
      `JS_MSG_STATE`，各自带 convId；convId 不同 = 换了会话，不可比）
    * HTTP：`v1/message/send` 的回执（`receipts` 是 `ImMonitor.sends` 的增量）
    """
    reasons = []
    evidence: Dict[str, Any] = {}

    # ---- DOM 侧 ----
    # dom_ok 三值；`dom_conv_off` 单独记录"现在选中的明显不是目标会话"，
    # 因为只有这一种才是**发错人**的强信号，气泡数没涨只是"没确认"。
    dom_ok: Optional[bool] = None
    dom_conv_off = False
    # 发送后 DOM 说"现在看的是哪个会话" —— 下面核验收件人时还要用它（HTTP 回执里
    # 没有收件人字段），所以声明提到分支外面，不能只在 else 里活着。
    a_conv: Optional[str] = None
    if before is None or after is None:
        reasons.append("DOM 状态缺失")
    else:
        b_conv = (before or {}).get("convId")
        a_conv = (after or {}).get("convId")
        evidence["dom_conv_before"] = b_conv
        evidence["dom_conv_after"] = a_conv
        if expect.conv_id and a_conv and a_conv != expect.conv_id:
            dom_ok = False
            dom_conv_off = True
            reasons.append(f"发送后当前会话变成了 {a_conv!r}（期望 {expect.conv_id!r}）")
        elif b_conv and a_conv and b_conv != a_conv:
            dom_ok = False
            dom_conv_off = True
            reasons.append("发送前后当前会话不同，计数不可比")
        else:
            b_n = int((before or {}).get("count") or 0)
            a_n = int((after or {}).get("count") or 0)
            evidence["dom_count_before"] = b_n
            evidence["dom_count_after"] = a_n
            if a_n > b_n and (after or {}).get("lastFromMe"):
                dom_ok = True
            else:
                # 注意：虚拟列表不重渲染时气泡数**不涨**很常见，所以这里只能算
                # "DOM 没确认"，不能算反证 —— 否则会把 HTTP 回执 ok 的发送误判成失败。
                dom_ok = False
                reasons.append(f"气泡数没有增加（{b_n} → {a_n}）或最后一条不是自己发的")

    # ---- 收件人能不能核验 ----
    # "服务端回 ok" 只说明有**一条**消息被收下了：回执里没有收件人字段
    # （decode_send_resp 只解 code/status/message_id），而 DOM 的气泡计数又是
    # 全局的（JS_MSG_STATE 数当前会话里自己发的气泡，会话一换就不可比）。
    # 所以能点名收件人的只有 DOM 报出的 convId；这里先把结论算清楚，
    # 下面据此决定"回执 ok"配不配叫强证据。
    # 期望的 convId 优先用扫描时读到的（expect.conv_id）；读不到时用 peer_uid
    # 反算 —— `self_uid` 这个参数就是为这条兜底路径传进来的。
    want_conv = expect.conv_id or assemble_conv_id(0, self_uid, expect.peer_uid)
    recipient_ok: Optional[bool] = None
    if a_conv:
        evidence["want_conv"] = want_conv
        if want_conv:
            recipient_ok = str(a_conv) == str(want_conv)
        else:
            recipient_ok = _conv_holds_peer(a_conv, expect.peer_uid)
        evidence["recipient_ok"] = recipient_ok

    # ---- HTTP 侧 ----
    http_ok: Optional[bool] = None
    good = [r for r in (receipts or []) if r.get("ok")]
    if receipts:
        evidence["http_receipts"] = len(receipts)
        evidence["http_message_id"] = (good[0].get("message_id") if good else None)
        http_ok = bool(good)
        if not good:
            reasons.append("HTTP 回执没有一条 ok")
    else:
        reasons.append("没有观察到 HTTP 回执（可能走 WS 通道）")

    if dom_conv_off:
        # DOM 明确说"现在选中的不是目标会话"。这是**发错人**的强信号，
        # 比"没发出去"严重得多，即使服务端回了 ok 也不能算成功
        # —— HTTP 回执里**没有**收件人字段（decode_send_resp 只解 code/status/message_id）。
        return Verdict(False, LEVEL_CONFLICT, "receipt_wrong_conv",
                       "会话不是目标：" + "；".join(reasons), evidence)
    if dom_ok is False and http_ok is False:
        return Verdict(False, LEVEL_CONFLICT, "receipt_failed", "；".join(reasons), evidence)
    if http_ok is True:
        # 回执 ok ≠ 强证据：它不带收件人，所以必须再拿 DOM 的"当前会话"去点名
        # 收件人。点得出（recipient_ok is True）才配叫强；点不出或点错了降级。
        # 注意这里**不动** ok —— 回答的是"证据够不够硬"，不是"发没发出去"。
        if recipient_ok is False:
            return Verdict(True, LEVEL_MEDIUM,
                           f"HTTP 回执 ok，但发送后当前会话是 {a_conv!r}"
                           f"（期望 {want_conv or expect.peer_uid!r}）"
                           f"—— 会话不匹配，回执本身不带收件人",
                           "HTTP 回执 ok", evidence)
        if recipient_ok is True:
            # ★ 判 strong 的**正向**条件只有这一条：回执 ok **且** 能点名收件人
            #   （发送后当前会话就是目标会话）。
            #   刻意**不要求**气泡数上涨：虚拟列表不重渲染时气泡数不涨是本项目的
            #   常态（生产 2026-10-01→10-13 的 81 条成功记录里，DOM 气泡证据一次
            #   都没出现过），拿它当必要条件等于把每一次发送都判成弱证据，
            #   面板「强确认」会永远是 0 —— 那只是把口径换个方向失真。
            #   气泡数仍然记进 evidence，供排查时看。
            return Verdict(True, LEVEL_STRONG, "", "HTTP 回执 ok", evidence)
        if dom_ok is True:
            return Verdict(True, LEVEL_MEDIUM,
                           "HTTP 回执 ok，DOM 也见到新气泡，但会话 id 取不到、"
                           "无法确认收件人是目标（回执本身不带收件人）",
                           "HTTP 回执 ok", evidence)
        if dom_ok is False:
            return Verdict(True, LEVEL_MEDIUM,
                           "HTTP 回执 ok，但 DOM 未见新气泡、会话 id 也取不到，"
                           "无法确认收件人是目标（回执本身不带收件人）",
                           "HTTP 回执 ok", evidence)
        return Verdict(True, LEVEL_MEDIUM,
                       "HTTP 回执 ok，但 DOM 状态取不到、没有气泡证据"
                       "（回执本身不带收件人）",
                       "HTTP 回执 ok", evidence)
    if dom_ok is True:
        return Verdict(True, LEVEL_MEDIUM, "", "DOM 气泡确认（同会话内增加）", evidence)
    return Verdict(False, LEVEL_NONE, "receipt_unconfirmed", "；".join(reasons), evidence)


# ---- 限流文案扫描用的公共辅助 --------------------------------------------

def looks_like_group(cur_conv, self_uid) -> Optional[bool]:
    """当前选中会话是不是群聊（避免往群里发单聊话术）。"""
    if not cur_conv:
        return None
    return _is_group(cur_conv.get("convId"), None, cur_conv.get("participantCount"))
