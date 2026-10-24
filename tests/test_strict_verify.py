"""core.strict_verify 的纯逻辑单测 —— 不依赖浏览器 / 不依赖 HAR。

重点是**回归上游的三个"乐观通过"反例**：DOM 没有选中态、convId 读不到、
只有网络信号，这三种情况在 C 里都判成功，在融合版必须判失败。

身份数据一律用 core.douyin_im.fake_uid / fake_sec_uid 生成，不含任何真值。
"""
import random
import unittest

from core.douyin_im import fake_uid, peer_uid_of
from core.strict_verify import (
    LEVEL_CONFLICT,
    LEVEL_MEDIUM,
    LEVEL_STRONG,
    R_IDENTITY_MISMATCH,
    R_IDENTITY_UNKNOWN,
    R_NO_CONV_ID,
    R_TITLE_MISMATCH,
    Expectation,
    assemble_conv_id,
    build_expectation,
    looks_like_group,
    signal_conv_id,
    signal_title,
    verify_receipt,
    verify_selection,
)

ME = "10000000000000001"
PEER_A = "10000000000000002"
PEER_B = "10000000000000003"

# ME 与 PEER_A 的会话 id（uid 数值升序）
CONV_A = assemble_conv_id(0, ME, PEER_A)
CONV_B = assemble_conv_id(0, ME, PEER_B)


def expect_for(raw, peer_uid, display, matched_by="remark"):
    return build_expectation(raw, self_uid=ME, peer_uid=peer_uid,
                             display=display, matched_by=matched_by)


class AssembleConvIdTests(unittest.TestCase):
    """复刻官方 assembleConvId 并与 peer_uid_of 对拍（两者必须互逆）。"""

    def test_ascending_regardless_of_argument_order(self):
        self.assertEqual(assemble_conv_id(0, ME, PEER_A), assemble_conv_id(0, PEER_A, ME))
        self.assertEqual(assemble_conv_id(0, ME, PEER_A), CONV_A)

    def test_roundtrip_with_peer_uid_of_for_many_pairs(self):
        # 注意：`fake_uid()` 不带 rng 时**每次返回同一个值**（它内部按固定种子
        # 现建 Random 实例）。要连续取不同值必须自己传一个 rng，否则这里会死循环
        # —— 上游这个反直觉的签名本身就是个坑，本测试即为其回归守卫。
        rng = random.Random(20260919)
        seen = set()
        while len(seen) < 50:
            a, b = fake_uid(rng), fake_uid(rng)
            if a == b:                    # 自己跟自己不是合法单聊，跳过
                continue
            seen.add((a, b) if int(a) < int(b) else (b, a))
        for a, b in seen:
            cid = assemble_conv_id(0, a, b)
            self.assertIsNotNone(cid)
            self.assertEqual(peer_uid_of(cid, a), b)
            self.assertEqual(peer_uid_of(cid, b), a)

    def test_self_to_self_is_not_a_valid_conversation(self):
        self.assertIsNone(assemble_conv_id(0, ME, ME))

    def test_non_numeric_is_rejected(self):
        self.assertIsNone(assemble_conv_id(0, ME, "abc"))
        self.assertIsNone(assemble_conv_id(0, ME, None))

    def test_biz_prefix_is_preserved(self):
        self.assertTrue(assemble_conv_id(7, ME, PEER_A).startswith("7:1:"))

    def test_fake_uid_without_rng_is_constant(self):
        """钉住上游 `fake_uid()` 的反直觉行为：不传 rng 就恒定同值。

        任何"连续取多个不同假 uid"的代码都必须自备 rng；若上游哪天改成
        真随机，这条会失败并提醒我们把 `fake_uid(rng)` 调用点简化回去。
        """
        self.assertEqual(fake_uid(), fake_uid())
        rng = random.Random(1)
        self.assertNotEqual(fake_uid(rng), fake_uid(rng))


class SignalTests(unittest.TestCase):
    def test_conv_id_signal_unknown_when_fiber_unreadable(self):
        # 有选中态但读不到 id → 未知（不是命中，也不是反证）
        self.assertIsNone(signal_conv_id(CONV_A, {"convId": None, "title": "甲同学"}))

    def test_conv_id_signal_false_when_no_selection(self):
        self.assertIs(signal_conv_id(CONV_A, None), False)

    def test_conv_id_signal_matches(self):
        self.assertIs(signal_conv_id(CONV_A, {"convId": CONV_A}), True)

    def test_title_signal_is_exact_not_substring(self):
        e = expect_for("甲同学", PEER_A, "甲同学")
        # 归一化后精确相等 → 命中
        self.assertIs(signal_title(e, {"title": "甲 同 学"}), True)
        # 子串 → 不命中（这条挡住"重名/长名包含短名"的发错人）
        self.assertIs(signal_title(e, {"title": "甲同学的另一个号"}), False)

    def test_title_signal_unknown_when_blank(self):
        e = expect_for("甲同学", PEER_A, "甲同学")
        self.assertIsNone(signal_title(e, {"title": ""}))
        self.assertIsNone(signal_title(e, None))


class OptimisticPassRegressions(unittest.TestCase):
    """上游 C 的三条"乐观通过"路径，逐条钉死为失败。"""

    def setUp(self):
        self.e = expect_for("甲同学", PEER_A, "甲同学")

    def test_regression_conv_id_missing_is_not_success(self):
        """C:1612/1619 `not cur.get("convId")` 判成功 → 现在必须失败。"""
        v = verify_selection(self.e, cur_conv={"convId": None, "title": "另一个人"},
                             net_hit=False, self_uid=ME)
        self.assertFalse(v.ok)
        # 标题明确对不上 → 这是反证，不是"证据不足"
        self.assertEqual(v.level, LEVEL_CONFLICT)
        self.assertEqual(v.reason, "title_mismatch")

    def test_regression_network_signal_only_is_not_success(self):
        """C:1623 `if net:` 判成功 → 现在必须失败。

        没有选中态时原因就是 `no_conv_id`（比"只有网络信号"更准确：
        页面根本没选中任何会话）；有选中态但选错人则是 conflict 级。
        """
        v = verify_selection(self.e, cur_conv=None, net_hit=True, self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, R_NO_CONV_ID)

    def test_network_signal_with_wrong_selection_is_conflict(self):
        """网络信号指向目标，但 DOM 明确选中的是别人 → conflict 级失败。"""
        v = verify_selection(self.e, cur_conv={"convId": CONV_B, "title": "乙同学"},
                             net_hit=True, self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.level, LEVEL_CONFLICT)
        self.assertEqual(v.reason, R_IDENTITY_MISMATCH)

    def test_regression_no_selection_at_all(self):
        v = verify_selection(self.e, cur_conv=None, net_hit=False, self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, R_NO_CONV_ID)

    def test_weak_evidence_only_display_name_is_medium_not_strong(self):
        """只有显示名（无 uid）时不允许给 strong —— 降级为 medium。

        刻意**不**拒绝：拒绝会让"用户只填昵称"的常规用法不可用，而重名由
        扫描层的 `ambiguous` 拦截（见 `test_ambiguous_target_is_refused`）。
        但级别必须降下来，调用方据此才知道这条没有硬证据背书。
        """
        e = build_expectation("甲同学", self_uid=ME, peer_uid=None,
                              display="甲同学", matched_by="fuzzy")
        v = verify_selection(e, cur_conv={"convId": CONV_A, "title": "甲同学"},
                             net_hit=False, self_uid=ME)
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_MEDIUM)
        self.assertFalse(v.evidence["strong"])
        # 关键：即便标题对上了，也不能因为"DOM 里有个会话"就给 strong
        self.assertFalse(v.evidence["signals"]["conv_id"])

    def test_weak_evidence_with_wrong_title_is_refused(self):
        """没有硬证据时标题就是唯一判据，对不上 = 反证。"""
        e = build_expectation("甲同学", self_uid=ME, peer_uid=None,
                              display="甲同学", matched_by="fuzzy")
        v = verify_selection(e, cur_conv={"convId": CONV_A, "title": "另一个人"},
                             net_hit=False, self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.level, LEVEL_CONFLICT)
        self.assertEqual(v.reason, R_TITLE_MISMATCH)


class SelectionVerdictTests(unittest.TestCase):
    def setUp(self):
        self.e = expect_for("甲同学", PEER_A, "甲同学")

    def test_conv_id_match_is_strong(self):
        v = verify_selection(self.e, cur_conv={"convId": CONV_A, "title": "甲同学"},
                             self_uid=ME)
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_STRONG)

    def test_title_only_match_is_medium(self):
        # conv_id 读不到，但标题精确相等且 uid 反推一致 → 通过
        v = verify_selection(self.e, cur_conv={"convId": None, "title": "甲同学"},
                             self_uid=ME)
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_MEDIUM)

    def test_wrong_conv_id_is_rejected_even_with_network(self):
        """明确选了另一个人 → 网络信号再强也否决。"""
        v = verify_selection(self.e, cur_conv={"convId": CONV_B, "title": "乙同学"},
                             net_hit=True, self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.level, LEVEL_CONFLICT)

    def test_wrong_title_with_correct_conv_id(self):
        """标题对不上但 id 对得上：id 更硬 → 通过（并且不报 conflict）。"""
        v = verify_selection(self.e, cur_conv={"convId": CONV_A, "title": "改过名的他"},
                             self_uid=ME)
        self.assertTrue(v.ok)

    def test_ambiguous_target_is_refused(self):
        e = Expectation(raw="小明", norm="小明", conv_id=CONV_A, peer_uid=PEER_A,
                        display="小明", ambiguous=True,
                        candidates=[PEER_A, PEER_B])
        v = verify_selection(e, cur_conv={"convId": CONV_A}, self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, R_IDENTITY_UNKNOWN)

    def test_peer_uid_field_used_when_conv_id_unreadable(self):
        v = verify_selection(self.e, cur_conv={"convId": None, "peerUid": PEER_A},
                             self_uid=ME)
        self.assertTrue(v.ok)

    def test_verdict_is_truthy_and_serialisable(self):
        v = verify_selection(self.e, cur_conv={"convId": CONV_A}, self_uid=ME)
        self.assertTrue(bool(v))
        d = v.as_dict()
        self.assertTrue(d["ok"])
        self.assertIn("signals", d["evidence"])


class GroupGuardTests(unittest.TestCase):
    def test_private_conv_is_not_group(self):
        self.assertIs(looks_like_group({"convId": CONV_A}, ME), False)

    def test_numeric_conv_id_is_group(self):
        self.assertIs(looks_like_group({"convId": "7000000000000000001"}, ME), True)

    def test_missing_state_is_unknown(self):
        self.assertIsNone(looks_like_group(None, ME))


class ReceiptVerdictTests(unittest.TestCase):
    def setUp(self):
        self.e = expect_for("甲同学", PEER_A, "甲同学")
        self.before = {"convId": CONV_A, "count": 10, "lastFromMe": False}

    def test_dom_bubble_increase_confirms(self):
        after = {"convId": CONV_A, "count": 11, "lastFromMe": True}
        v = verify_receipt(self.e, before=self.before, after=after, receipts=[])
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_MEDIUM)

    def test_http_receipt_with_recipient_pinned_is_strong_without_bubble(self):
        """回执 ok + 发送后当前会话就是目标会话 → 强证据，**不要求**气泡数上涨。

        气泡数不涨在真实页面上是常态（虚拟列表不重渲染；生产 2026-10-01→10-13
        的 81 条成功记录里 DOM 气泡证据一次都没出现过），拿它当必要条件会把每一次
        发送都判成弱证据。能点名收件人（`recipient_ok is True`）+ 服务端回执 ok
        才是可用的硬证据；气泡数仍然留在 evidence 里供排查。
        """
        after = {"convId": CONV_A, "count": 10, "lastFromMe": False}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}])
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_STRONG)
        self.assertEqual(v.reason, "")
        # 气泡数照旧记下来（"没涨"是事实，只是不当作反证）
        self.assertEqual(v.evidence["dom_count_before"], 10)
        self.assertEqual(v.evidence["dom_count_after"], 10)
        self.assertTrue(v.evidence["recipient_ok"])

    def test_http_receipt_without_recipient_is_only_weak_evidence(self):
        """回执 ok 但连收件人都点不出来（会话 id 读不到）→ 只能算弱证据。

        回执里**没有**收件人字段（`decode_send_resp` 只解 code/status/message_id），
        所以"服务端收下了一条消息"证明不了"收件人是甲同学"。这里以前给 strong，
        面板就把一条"可能发给别人"的消息写成"强确认"，用户不会再多看一眼。
        ok 仍然为 True：这里判的是证据够不够硬，不是发没发出去。
        """
        after = {"convId": None, "count": 10, "lastFromMe": False}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}])
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_MEDIUM)
        self.assertIn("无法确认收件人是目标", v.reason)
        self.assertIn("回执本身不带收件人", v.reason)

    def test_http_receipt_with_new_bubble_in_target_conv_is_strong(self):
        """回执 ok + 目标会话里真的多了个自己发的气泡 → 也是强证据。"""
        after = {"convId": CONV_A, "count": 11, "lastFromMe": True}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}])
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_STRONG)
        self.assertEqual(v.reason, "")

    def test_http_receipt_with_other_conv_is_failure_not_strong(self):
        """发送后当前会话不是目标 → 判"发错人"（conflict），连 ok 都不许给。

        只有扫描阶段没读到 conv_id 才走得到这里（读到了就走上面 `receipt_wrong_conv`
        的 conflict 分支），期望的会话 id 靠 `self_uid + peer_uid` 反算，
        所以 `self_uid` 必须传进来 —— 不传就核验不了收件人。

        ★ 从前这里判 `ok=True / medium`（"反正服务端收下了一条"），于是真发错人
        被记成发送成功：`core/tasks.py` 记 `sent_ok += 1`、账本 ok=true、
        `core/schedule.select()` 认定当天已发、面板写"今天 HH:MM 已发" ——
        而消息送到了**别人**那里（GitHub issue #13）。回执里的 code/status
        证明不了收件人是谁（`decode_send_resp` 只解 code/status/message_id），
        所以"稳定地看错会话"必须与"两次读数不一致"（`dom_conv_off`）同级。
        """
        e = build_expectation("甲同学", self_uid=None, peer_uid=PEER_A,
                              display="甲同学", matched_by="remark")
        self.assertIsNone(e.conv_id)
        before = {"convId": CONV_B, "count": 10, "lastFromMe": False}
        after = {"convId": CONV_B, "count": 11, "lastFromMe": True}
        v = verify_receipt(e, before=before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}], self_uid=ME)
        self.assertFalse(v.ok)
        self.assertEqual(v.level, LEVEL_CONFLICT)
        # `reason` 是与 `dom_conv_off` 共用的那一个码（面板文案见
        # webui/viewmodel.py 的 REASON_LABELS），人话在 `detail` 里
        self.assertEqual(v.reason, "receipt_wrong_conv")
        self.assertIn("会话不是目标", v.detail)
        self.assertIn("回执本身不带收件人", v.detail)
        self.assertEqual(v.evidence["want_conv"], CONV_A)
        self.assertIs(v.evidence["recipient_ok"], False)

    def test_http_receipt_with_derived_conv_id_matching_is_strong(self):
        """同上但当前会话正是反算出来的那一个 → strong（证明上一条不是恒判 medium）。"""
        e = build_expectation("甲同学", self_uid=None, peer_uid=PEER_A,
                              display="甲同学", matched_by="remark")
        before = {"convId": CONV_A, "count": 10, "lastFromMe": False}
        after = {"convId": CONV_A, "count": 11, "lastFromMe": True}
        v = verify_receipt(e, before=before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}], self_uid=ME)
        self.assertEqual(v.level, LEVEL_STRONG)

    def test_http_receipt_with_self_uid_unknown_still_checks_recipient(self):
        """连 self_uid 都拿不到时退化成"目标 uid 在不在会话里"，仍能认出会话不匹配。

        这是 #13 里"`self_uid` 拿不到是常态"的那一格（core/strict_verify.py:117-120
        自己写明）：`want_conv` 反算不出来，判据只剩 `_conv_holds_peer`
        —— PEER_A 不在 CONV_B 的参与者里 → `recipient_ok is False` → 一样判失败。
        """
        e = build_expectation("甲同学", self_uid=None, peer_uid=PEER_A,
                              display="甲同学", matched_by="remark")
        before = {"convId": CONV_B, "count": 10, "lastFromMe": False}
        after = {"convId": CONV_B, "count": 11, "lastFromMe": True}
        v = verify_receipt(e, before=before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}], self_uid=None)
        self.assertFalse(v.ok)
        self.assertEqual(v.level, LEVEL_CONFLICT)
        self.assertEqual(v.reason, "receipt_wrong_conv")
        self.assertIn("的参与者里没有目标", v.detail)
        self.assertIsNone(v.evidence["want_conv"])
        self.assertIs(v.evidence["recipient_ok"], False)

    def test_group_conv_id_is_not_strong_by_default(self):
        """会话 id 不是单聊形状（群聊）→ 收件人核验不了，保守判弱证据。"""
        e = build_expectation("甲同学", self_uid=None, peer_uid=PEER_A,
                              display="甲同学", matched_by="remark")
        group = "7000000000000000001"
        before = {"convId": group, "count": 10, "lastFromMe": False}
        after = {"convId": group, "count": 11, "lastFromMe": True}
        v = verify_receipt(e, before=before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}])
        self.assertEqual(v.level, LEVEL_MEDIUM)
        self.assertIn("无法确认收件人是目标", v.reason)

    def test_conv_switched_after_send_is_failure_even_with_receipt(self):
        """发完当前会话变成了另一个人 → 判"发错人"，比发失败更严重。"""
        after = {"convId": CONV_B, "count": 3, "lastFromMe": True}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-1"}])
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "receipt_wrong_conv")

    def test_conv_switched_without_receipt_is_failure(self):
        after = {"convId": CONV_B, "count": 3, "lastFromMe": True}
        v = verify_receipt(self.e, before=self.before, after=after, receipts=[])
        self.assertFalse(v.ok)

    def test_failed_receipt_beats_a_dom_bubble(self):
        """回执明确失败 + 页面冒出一个气泡 → 不许判成功。

        气泡是**乐观渲染**：服务端返回 code!=0（限流 / 安全验证 / 消息被拒）时，
        本地照样可能先把这条画出来。以前这里先看 dom_ok，于是判 ok=True/medium，
        调用方 sent_ok += 1、调度认定"今天已发"，对方其实没收到也不会补发。
        """
        after = {"convId": CONV_A, "count": 11, "lastFromMe": True}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": False, "code": 5, "status": "fail"}])
        self.assertFalse(v.ok, "回执说失败，页面气泡不能把它救回来")
        self.assertEqual(v.reason, "receipt_failed")
        self.assertEqual(v.level, LEVEL_CONFLICT)

    def test_a_healthy_receipt_still_passes_with_a_bubble(self):
        """反向：正常回执 + 气泡照旧判成功（别把上面的修复做成一律失败）。"""
        after = {"convId": CONV_A, "count": 11, "lastFromMe": True}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": True, "message_id": "mid-9"}])
        self.assertTrue(v.ok)

    def test_http_failure_and_no_dom_growth_is_failure(self):
        after = {"convId": CONV_A, "count": 10, "lastFromMe": False}
        v = verify_receipt(self.e, before=self.before, after=after,
                           receipts=[{"ok": False, "code": 500}])
        self.assertFalse(v.ok)

    def test_missing_dom_state_with_receipt_still_passes(self):
        """DOM 状态一整块取不到时，回执只能算弱证据（ok 仍是 True，没发错人）。"""
        v = verify_receipt(self.e, before=None, after=None,
                           receipts=[{"ok": True, "message_id": "mid-2"}])
        self.assertTrue(v.ok)
        self.assertEqual(v.level, LEVEL_MEDIUM)
        self.assertIn("DOM 状态取不到", v.reason)


if __name__ == "__main__":
    unittest.main()
