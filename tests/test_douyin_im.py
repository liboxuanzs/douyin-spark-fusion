"""core.douyin_im 的纯逻辑单测（不依赖浏览器 / 不依赖 HAR / 不含任何真实身份）。

这一段覆盖的是「判定与匹配」逻辑：滚动到底判据、步长守卫、归一化、匹配优先级、
conv_id 拆 uid、群聊判定、资料 join、信封返回码。

真实报文抽样在 tests/test_douyin_im_har.py（需要本机抓的 HAR，找不到就 skip）。
另有 LoggingTests 钉住日志分级契约（改造后 `verbose` 开关已被 logger 级别取代）。
身份数据一律由 core.douyin_im.fake_uid / fake_sec_uid 生成，
固定种子 → 结果可复现，但不含任何真值，可以安全提交。
"""
import inspect
import json
import re
import unittest

from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent

from core import douyin_im
from core.douyin_im import (
    ROW_HEIGHT,
    DouyinIM,
    ImMonitor,
    _dom_text_matches,
    _envelope_code,
    _is_group,
    _norm,
    _readback_matches,
    _residue_echo,
    _scroll_top_for,
    check_login,
    decode_user_info,
    envelope,
    fake_sec_uid,
    line_list,
    peer_uid_of,
    scan,
    split_message_lines,
)

# 固定假身份（由固定种子派生的常量，见 douyin_im._FAKE_*）
ME = "10000000000000001"
PEER_A = "10000000000000002"
PEER_B = "10000000000000003"


class _Stub(DouyinIM):
    """绕过 __init__ 的重活，只保留匹配所需的 _state。"""

    def __init__(self):
        self._state = {"user_id": ME}


class ScrollCriterionTests(unittest.TestCase):
    """两个量语义相反，不能混用：
    atBottom = 用户滚到底（scrollTop + clientHeight >= scrollHeight）
    notFilled = 内容还没填满容器（scrollHeight <= clientHeight）
    前端拿后者判「要不要补页」，不能拿来判「到底」。
    """

    @staticmethod
    def probe(scroll_top, client_h, scroll_h):
        return {
            "atBottom": scroll_top + client_h >= scroll_h - 1,
            "notFilled": scroll_h <= client_h + 1,
        }

    def test_long_list_top_is_not_bottom_and_not_filled_is_false(self):
        top = self.probe(0, 600, 10000)
        self.assertIs(top["atBottom"], False)
        # notFilled 恒 false —— 正说明它≠「到底」
        self.assertIs(top["notFilled"], False)

    def test_long_list_bottom_is_bottom_but_still_not_filled_false(self):
        bottom = self.probe(9400, 600, 10000)
        self.assertIs(bottom["atBottom"], True)
        self.assertIs(bottom["notFilled"], False)

    def test_short_list_is_filled_false_but_content_all_visible(self):
        short = self.probe(0, 600, 300)
        self.assertIs(short["notFilled"], True)
        self.assertIs(short["atBottom"], True)

    def test_step_never_exceeds_40_percent_of_window(self):
        for ch in (300, 600, 900):
            step = max(120, int(ch * 0.4))
            self.assertLessEqual(step, ch * 0.4 + 1, f"容器高 {ch}px")


class ScrollTopTests(unittest.TestCase):
    """`data_index` 的安全消费（`_scroll_top_for`）：老代码会在这里抛 TypeError。

    现场：`JS_COLLECT` 里写的是 `dataIndex: holder ? Number(...) : null` —— **键在、
    值是 None**，所以 `item.get("data_index", 0)` 的默认值根本不生效：`None * 67`
    抛 TypeError。这行在 `iter_find_and_select` 里没有任何 try 兜着，异常一路穿到
    core/tasks.py 的账号级 except → 整轮被记成 error，排在后面的好友一个都不发。
    """

    def test_valid_index_is_multiplied_by_row_height(self):
        self.assertEqual(_scroll_top_for({"data_index": 3}), 3 * ROW_HEIGHT)
        self.assertEqual(_scroll_top_for({"data_index": 0}), 0)     # 0 是合法下标

    def test_none_falls_back_to_dom_index(self):
        # 同一窗口里的渲染序号也够用：滚到那个位置，目标所在窗口会被重新渲染
        self.assertEqual(_scroll_top_for({"data_index": None, "dom_index": 4}),
                         4 * ROW_HEIGHT)

    def test_none_without_any_index_falls_back_to_top(self):
        self.assertEqual(_scroll_top_for({"data_index": None}), 0)
        self.assertEqual(_scroll_top_for({"data_index": None, "dom_index": None}), 0)
        self.assertEqual(_scroll_top_for({}), 0)
        self.assertEqual(_scroll_top_for(None), 0)

    def test_negative_and_garbage_are_rejected(self):
        # 负数：浏览器会把负 scrollTop 夹到 0，但日志里留一个 -67 只会把排查带偏；
        # NaN 来自 `Number('非数字的 data-index')`，字符串乱值来自更下游的退化。
        for bad in (-1, -67, float("nan"), float("inf"), float("-inf"),
                    "abc", "", [], {}, True):
            self.assertEqual(_scroll_top_for({"data_index": bad}), 0,
                             f"data_index={bad!r} 不该被当成下标")

    def test_numeric_string_and_float_are_accepted(self):
        # JS 侧 `Number()` 给的就是数，这里是便宜的兜底（别把能用的值判死）
        self.assertEqual(_scroll_top_for({"data_index": "12"}), 12 * ROW_HEIGHT)
        self.assertEqual(_scroll_top_for({"data_index": 2.7}), 2 * ROW_HEIGHT)


class NormTests(unittest.TestCase):
    def test_strips_nbsp(self):
        self.assertEqual(_norm("甲\u00a0同\u00a0学"), _norm("甲同学"))

    def test_fullwidth_folds_to_ascii(self):
        self.assertEqual(_norm("ＡＢＣ"), _norm("abc"))

    def test_strips_zero_width(self):
        self.assertEqual(_norm("甲\u200b同学"), _norm("甲同学"))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(_norm("  甲同学  "), _norm("甲同学"))

    def test_none_is_safe(self):
        self.assertEqual(_norm(None), "")

    def test_mixed_nbsp_and_space(self):
        # 前端 TextKeepSpaces 会把空格换成 \xa0，两种混着出现也要能归一
        self.assertEqual(_norm("甲\u00a0同 学"), _norm("甲同学"))

    def test_public_alias_matches_internal(self):
        from core.douyin_im import norm
        self.assertIs(norm, _norm)


class PeerUidTests(unittest.TestCase):
    def setUp(self):
        self.s = _Stub()

    def test_extracts_peer_not_self(self):
        lo, hi = sorted((PEER_A, ME), key=int)
        cid = f"0:1:{lo}:{hi}"
        self.assertEqual(self.s._peer_uid_of(cid), PEER_A)

    def test_same_conv_id_from_other_side_yields_me(self):
        lo, hi = sorted((PEER_A, ME), key=int)
        cid = f"0:1:{lo}:{hi}"
        self.assertEqual(peer_uid_of(cid, PEER_A), ME)

    def test_group_conv_id_returns_none(self):
        self.assertIsNone(self.s._peer_uid_of("7000000000000000001"))

    def test_self_to_self_yields_self(self):
        self.assertEqual(self.s._peer_uid_of(f"0:1:{ME}:{ME}"), ME)


class AssembleConvIdTests(unittest.TestCase):
    """复刻官方 assembleConvId：uid 按数值升序排列。"""

    @staticmethod
    def assemble(biz, a, b):
        return f"{biz}:1:{a}:{b}" if int(b) > int(a) else f"{biz}:1:{b}:{a}"

    def test_ascending_regardless_of_argument_order(self):
        lo, hi = sorted((PEER_A, ME), key=int)
        self.assertEqual(self.assemble(0, PEER_A, ME), f"0:1:{lo}:{hi}")
        self.assertEqual(self.assemble(0, ME, PEER_A), f"0:1:{lo}:{hi}")


class IsGroupTests(unittest.TestCase):
    def test_numeric_conv_id_is_group(self):
        self.assertIs(_is_group("7000000000000000001", None, None), True)

    def test_0_1_prefix_is_single(self):
        self.assertIs(_is_group(f"0:1:{ME}:111", None, None), False)

    def test_0_2_prefix_is_group(self):
        self.assertIs(_is_group("0:2:1:2", None, None), True)

    def test_participant_count_fallback_when_conv_id_has_no_signal(self):
        self.assertIs(_is_group("7:9:1:2", None, 5), True)

    def test_0_1_beats_participant_count(self):
        # `0:1:` 定义上就是 1:1，不该被 participant_count 翻案
        self.assertIs(_is_group("0:1:a:b", None, 5), False)

    def test_returns_none_when_undecidable(self):
        self.assertIsNone(_is_group(None, None, None))

    def test_conv_type_is_not_used_as_enum(self):
        # conv.type 实测是长整型会话 id，不是类型枚举
        lo, hi = sorted((PEER_A, ME), key=int)
        self.assertIs(_is_group(f"0:1:{lo}:{hi}", 7000000000000000002, None), False)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.s = _Stub()
        self.sec = fake_sec_uid()
        self.item = {
            "remark": "甲同学", "nickname": "NickA", "douyin_id": "user_aaa",
            "uid": PEER_A, "sec_uid": self.sec,
            "title": "甲同学", "display": "甲同学",
        }

    def test_remark_has_highest_priority(self):
        _, how = self.s._match(self.item, {_norm("甲同学"): "甲同学"})
        self.assertEqual(how, "remark")

    def test_nickname_matched(self):
        _, how = self.s._match(self.item, {_norm("NickA"): "NickA"})
        self.assertEqual(how, "nickname")

    def test_douyin_id_matched(self):
        _, how = self.s._match(self.item, {_norm("user_aaa"): "user_aaa"})
        self.assertEqual(how, "douyin_id")

    def test_uid_matched(self):
        _, how = self.s._match(self.item, {_norm(PEER_A): "1"})
        self.assertEqual(how, "uid")

    def test_no_match_returns_none_pair(self):
        k, how = self.s._match(self.item, {"查无此人": "查无此人"})
        self.assertIsNone(k)
        self.assertIsNone(how)

    def test_remark_wins_over_nickname(self):
        both = {_norm("甲同学"): "甲同学", _norm("NickA"): "NickA"}
        _, how = self.s._match(self.item, both)
        self.assertEqual(how, "remark")

    def test_falls_back_to_dom_title_when_fiber_dead(self):
        dom_only = {"remark": None, "nickname": None, "douyin_id": "",
                    "uid": None, "sec_uid": None, "title": "甲同学", "display": "甲同学"}
        _, how = self.s._match(dom_only, {_norm("甲同学"): "甲同学"})
        self.assertEqual(how, "title")

    def test_dom_title_with_nbsp_still_matches(self):
        nb = "甲\u00a0同\u00a0学"
        dom = {"remark": None, "nickname": None, "douyin_id": "",
               "uid": "999", "sec_uid": "S9", "title": nb, "display": nb}
        k, how = self.s._match(dom, {_norm("甲同学"): "甲同学"})
        self.assertEqual(how, "title")
        self.assertIsNotNone(k)


class TargetRefTests(unittest.TestCase):
    """配置里的目标 → 统一结构。带身份的目标只按 id 匹配，名字只用来显示。"""

    def test_plain_string_keeps_name_matching(self):
        ref = douyin_im.target_ref(" Ｌｕ瞳 ")
        self.assertEqual(ref["name"], "Ｌｕ瞳")          # 显示保持用户写的样子
        self.assertEqual(ref["key"], "lu瞳")            # 匹配用归一后的 key
        self.assertEqual(ref["uid"], "")
        self.assertEqual(ref["conv_id"], "")

    def test_dict_with_conv_id_keys_by_conv(self):
        ref = douyin_im.target_ref({"name": "甲同学", "uid": "u-1", "conv_id": "0:1:2"})
        self.assertEqual(ref["key"], "conv:0:1:2")
        self.assertEqual(ref["name"], "甲同学")

    def test_dict_with_uid_only_keys_by_uid(self):
        ref = douyin_im.target_ref({"name": "甲同学", "uid": "u-1"})
        self.assertEqual(ref["key"], "uid:u-1")

    def test_dict_without_identity_falls_back_to_name(self):
        ref = douyin_im.target_ref({"name": "甲同学"})
        self.assertEqual(ref["key"], "甲同学")

    def test_dict_without_name_falls_back_to_id_for_display(self):
        ref = douyin_im.target_ref({"conv_id": "0:1:2"})
        self.assertEqual(ref["name"], "0:1:2")
        self.assertEqual(ref["key"], "conv:0:1:2")

    def test_empties_are_dropped(self):
        for bad in ("", "   ", None, {}, {"name": "  "}, 123 if False else ""):
            self.assertIsNone(douyin_im.target_ref(bad), bad)

    def test_refs_keeps_order_and_drops_empties(self):
        refs = douyin_im.target_refs(["甲同学", "", {"name": "乙同学", "uid": "u-2"}, None])
        self.assertEqual([r["name"] for r in refs], ["甲同学", "乙同学"])

    def test_name_helper_accepts_both_forms(self):
        self.assertEqual(douyin_im.target_name({"name": "甲同学"}), "甲同学")
        self.assertEqual(douyin_im.target_name({"conv_id": "0:1:2"}), "0:1:2")
        self.assertEqual(douyin_im.target_name("乙同学"), "乙同学")
        self.assertEqual(douyin_im.target_name(None), "")


class IdentityMatchTests(unittest.TestCase):
    """勾选写入的目标带 uid/conv_id —— 只按 id 匹配，禁止子串兜底。"""

    def setUp(self):
        self.s = _Stub()
        self.item = {
            "remark": "小明明", "nickname": "mingming", "douyin_id": "user_mm",
            "uid": "u-mm", "sec_uid": "sec-mm",
            "title": "小明明", "display": "小明明", "conv_id": "0:1:2",
        }

    def test_conv_id_wins_over_everything(self):
        key, how = self.s._match(self.item, {"甲同学": {"name": "甲同学"}},
                                 by_conv={"0:1:2": "conv:0:1:2"}, by_uid={})
        self.assertEqual(key, "conv:0:1:2")
        self.assertEqual(how, "conv_id")

    def test_uid_matched_before_names(self):
        key, how = self.s._match(self.item, {_norm("小明明"): "小明明"},
                                 by_conv={}, by_uid={"u-mm": "uid:u-mm"})
        self.assertEqual(key, "uid:u-mm")
        self.assertEqual(how, "uid")

    def test_sec_uid_also_counts(self):
        key, how = self.s._match(self.item, {}, by_conv={}, by_uid={"sec-mm": "uid:u-x"})
        self.assertEqual(key, "uid:u-x")
        self.assertEqual(how, "sec_uid")

    def test_identity_target_is_not_matched_by_substring(self):
        """「小明」是勾选来的（带 conv_id），页面上只有「小明明」——不许猜。"""
        pending = {"conv:0:9:9": {"name": "小明", "conv_id": "0:9:9", "uid": "",
                                  "key": "conv:0:9:9"}}
        key, how = self.s._match(self.item, pending, by_conv={}, by_uid={})
        self.assertIsNone(key)
        self.assertIsNone(how)

    def test_identity_target_is_not_matched_by_full_name_either(self):
        """名字一模一样但 id 不同 —— 不是同一个人（改名/重名），必须报没找到。"""
        pending = {"conv:0:9:9": {"name": "小明明", "conv_id": "0:9:9", "uid": "",
                                  "key": "conv:0:9:9"}}
        key, how = self.s._match(self.item, pending, by_conv={}, by_uid={})
        self.assertIsNone(key)

    def test_handwritten_name_target_is_not_matched_by_substring(self):
        """手写的名字目标只认「整段相等」——「小明」**不许**命中「小明明」。

        旧行为是任意子串兜底，等于把消息发给同名不同人的那个（「小明明」），
        而账本里记的是成功。发错了收不回来，所以宁可报「没找到」（面板和日志
        里看得见，用户补全关键词即可）。
        """
        key, how = self.s._match(self.item, {_norm("小明"): "小明"}, by_conv={}, by_uid={})
        self.assertIsNone(key)
        self.assertIsNone(how)

    def test_handwritten_name_target_matches_a_whole_segment(self):
        """带后缀的备注照样命中：「老王」是「老王（同事）」里的一段。"""
        item = dict(self.item, remark="老王（同事）", display="老王（同事）",
                    nickname="", title="", douyin_id="")
        key, how = self.s._match(item, {_norm("老王"): "老王"}, by_conv={}, by_uid={})
        self.assertEqual(key, "老王")
        self.assertEqual(how, "fuzzy")

    def test_emoji_decoration_is_trimmed_before_matching(self):
        """昵称两端的 emoji 不算数（本项目自己的号就叫 🌈示例号🌈）。"""
        item = dict(self.item, remark="🌈示例号🌈", display="🌈示例号🌈",
                    nickname="", title="", douyin_id="")
        key, how = self.s._match(item, {_norm("示例号"): "示例号"}, by_conv={}, by_uid={})
        self.assertEqual(key, _norm("示例号"))
        self.assertEqual(how, "fuzzy")

    def test_two_name_targets_on_one_conversation_mark_ambiguous(self):
        """一个会话同时命中多个名字目标 → 置歧义位，交给验层直接拒掉。

        在这之前全仓没有任何生产代码会写 `ambiguous`（只有测试手工构造过），
        `verify_selection` 的第一道闸一直是死代码。
        """
        item = dict(self.item, remark="老王,老李", display="老王,老李",
                    nickname="", title="", douyin_id="")
        pending = {_norm("老王"): "老王", _norm("老李"): "老李"}
        key, how = self.s._match(item, pending, by_conv={}, by_uid={})
        self.assertEqual(how, "fuzzy", "两个目标都该命中（子串兜底）")
        self.assertTrue(item.get("ambiguous"), "命中多个目标必须置歧义位")
        self.assertEqual(item.get("candidates"), [item.get("conv_id")])

    def test_mixed_targets_match_the_identity_one_first(self):
        by_conv = {"0:1:2": "conv:0:1:2"}
        pending = {"小明明": "小明明", "conv:0:1:2": "conv:0:1:2"}
        key, how = self.s._match(self.item, pending, by_conv=by_conv, by_uid={})
        self.assertEqual(key, "conv:0:1:2")
        self.assertEqual(how, "conv_id")


class JoinTests(unittest.TestCase):
    class _MonStub:
        """ImMonitor 的最小替身（errors 是 _finish_scan 会读的字段）。"""

        def __init__(self, sec):
            self.errors = []
            self.peers = {
                sec: {"sec_uid": sec, "uid": PEER_A, "nickname": "NickA",
                      "remark": "甲同学", "unique_id": "user_aaa"},
            }

        def by_sec_uid(self):
            return {p["sec_uid"]: p for p in self.peers.values()}

        def by_uid(self):
            return {p["uid"]: p for p in self.peers.values()}

    def setUp(self):
        self.s = _Stub()
        self.sec = fake_sec_uid()
        self.s.mon = self._MonStub(self.sec)

    def test_joins_nickname_remark_douyin_id(self):
        it = {"sec_uid": self.sec, "uid": None, "nickname": None,
              "remark": None, "douyin_id": "", "title": "NickA", "display": "NickA"}
        self.s._join(it)
        self.assertEqual(it["nickname"], "NickA")
        self.assertEqual(it["remark"], "甲同学")
        self.assertEqual(it["douyin_id"], "user_aaa")

    def test_display_upgraded_to_remark(self):
        it = {"sec_uid": self.sec, "uid": None, "nickname": None,
              "remark": None, "douyin_id": "", "title": "NickA", "display": "NickA"}
        self.s._join(it)
        self.assertEqual(it["display"], "甲同学")

    def test_unresolved_keeps_none_and_falls_back_to_title(self):
        it = {"sec_uid": fake_sec_uid(body_len=8), "uid": None, "nickname": None,
              "remark": None, "douyin_id": "", "title": "某群", "display": "某群"}
        self.s._join(it)
        self.assertIsNone(it["nickname"])
        self.assertEqual(it["display"], "某群")


class _NoWaitPage:
    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, arg=None):
        """折叠组/陌生人组容器数。默认 0 —— 实测这个账号就是 0 个容器，
        此时 last_scan["note"] 必须留空（不能被一句无条件的"未覆盖"吓到）。"""
        if "Stranger" in str(arg):
            return 0
        return getattr(self, "fold_containers", 0)


class _VListStub(_Stub):
    """模拟虚拟化会话列表：DOM 里恒只有「窗口内」那几条，滚动时窗口整体下移。

    尺寸刻意按真实量级搭（clientHeight 670 / 条目 100px / 步长守卫 0.4）
    → step = 268px ≈ 2.7 条 < 窗口 7 条，不会整屏跳过。
    若把尺寸改小（比如 total=7、clientHeight=3），step 会被抬到 120 的下限
    从而一步跳到底、中间的人永久漏掉 —— 那时测出来的"通过"是假的。
    """

    ITEM_H = 100
    CLIENT_H = 670

    def __init__(self, total=50):
        self._state = {"user_id": ME}
        self._scan_cache = {}
        self._walk_stat = {"stopped": "not-run", "steps": 0, "seen": {}}
        self._select_failed = []
        self.max_steps = 200
        self.timeout = 90.0
        self.settle_ms = 0
        self.total = total
        self.top = 0
        self.page = _NoWaitPage()
        # _finish_scan 的收尾日志会读 mon.errors；真身由 DouyinIM.__init__ 建立，
        # 这个桩绕过了 __init__，所以要自己补一个。资料一律对不上，
        # 让 _join 走「回退到 DOM 标题」那条分支（正是这里想覆盖的行为）。
        self.mon = JoinTests._MonStub("sec-that-matches-nothing")

    def _scroll_probe(self):
        sh = self.total * self.ITEM_H
        return {
            "found": True,
            "scrollTop": self.top,
            "clientHeight": self.CLIENT_H,
            "scrollHeight": sh,
            "atBottom": self.top + self.CLIENT_H >= sh - 1,
        }

    def _scroll_to(self, top):
        self.top = top

    def _read_window(self):
        first = self.top // self.ITEM_H
        n = self.CLIENT_H // self.ITEM_H + 1
        out = []
        for i in range(first, min(first + n, self.total)):
            out.append({
                "conv_id": f"0:1:{ME}:{2000 + i}", "title": f"U{i}",
                "nickname": None, "remark": None, "douyin_id": "",
                "uid": str(2000 + i), "sec_uid": None, "short_id": None,
                "is_group": False, "unread": 0, "is_muted": False,
                "is_current": False, "data_index": i, "dom_index": i - first,
                "from_fiber": True, "display": f"U{i}", "matched_by": None,
            })
        return out

    def _settle(self):
        self.settle_calls = getattr(self, "settle_calls", 0) + 1


class WalkTests(unittest.TestCase):
    """滚动骨架 _walk：去重、到底判定、提前中断不许谎报扫全。"""

    def setUp(self):
        self.s = _VListStub(total=50)

    def test_walks_all_and_dedupes_overlapping_windows(self):
        got = []
        for new_items in self.s._walk():
            got.extend(new_items)
        ids = [i["conv_id"] for i in got]
        self.assertEqual(len(ids), 50)          # 一条不多（重叠被吃掉）
        self.assertEqual(len(set(ids)), 50)     # 一条不少（没整屏跳过）

    def test_reaches_bottom(self):
        list(self.s._walk())
        self.s._finish_scan({}, [])
        self.assertEqual(self.s._walk_stat["stopped"], "reach-bottom")
        self.assertTrue(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["visited"], 50)

    def test_caller_break_is_not_reported_as_scanned_all(self):
        """外部提前 break 不能报 scanned_all —— 否则 missing 会被读成"确实不存在"。"""
        for i, _ in enumerate(self.s._walk()):
            if i >= 2:
                break
        self.s._finish_scan({}, [])
        self.assertEqual(self.s._walk_stat["stopped"], "caller-break")
        self.assertFalse(self.s.last_scan["scanned_all"])

    def test_all_found_by_the_caller_stays_all_found(self):
        """调用方找齐目标后写的 all-found 必须保留。

        线上每一轮都是"目标全找到就 break"，_walk 的 finally 却把它统一改写成
        caller-break → scanned_all 永远 False，账本和面板整天说"扫描未完成，
        missing 不代表不存在"（10-15/10-16 每天如此）。
        """
        for _ in self.s._walk():
            self.s._walk_stat["stopped"] = "all-found"
            break
        self.s._finish_scan({}, [])
        self.assertEqual(self.s._walk_stat["stopped"], "all-found")
        self.assertTrue(self.s.last_scan["scanned_all"])

    def test_no_container_reports_not_scanned(self):
        self.s._scroll_probe = lambda: {"found": False}
        self.assertEqual(list(self.s._walk()), [])
        self.s._finish_scan({}, [])
        self.assertEqual(self.s.last_scan["stopped"], "no-container")
        self.assertFalse(self.s.last_scan["scanned_all"])

    def test_scanned_all_without_fold_groups_says_nothing(self):
        """扫全了、又没有折叠组/陌生人组 → note 必须为空。

        实测这个账号就是 fold=0/stranger=0、API 报 23 条、扫描正好收齐 23 条，
        却因为 note 无条件写着"未覆盖折叠组/陌生人组"，让人以为读漏了。
        """
        list(self.s._walk())
        self.s._finish_scan({}, [])
        self.assertTrue(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["note"], "")

    def test_scanned_all_with_fold_groups_warns(self):
        self.s.page.fold_containers = 2
        list(self.s._walk())
        self.s._finish_scan({}, [])
        self.assertIn("折叠组/陌生人组未包含", self.s.last_scan["note"])
        self.assertIn("2", self.s.last_scan["note"])

    def test_probe_unavailable_stays_conservative(self):
        """探不到容器数（页面挂了 evaluate）时宁可保守提示，也不能说"没漏"。"""
        def boom(*a, **k):
            raise RuntimeError("页面没了")
        self.s.page.evaluate = boom
        list(self.s._walk())
        self.s._finish_scan({}, [])
        self.assertTrue(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["note"], "未覆盖折叠组/陌生人组")

    def test_unfinished_scan_note_talks_about_missing(self):
        for i, _ in enumerate(self.s._walk()):
            if i >= 2:
                break
        self.s._finish_scan({}, [])
        self.assertEqual(self.s.last_scan["note"], "扫描未完成，missing 不代表不存在")

    def test_exhausted_steps_is_not_scanned_all(self):
        """步数用尽 ≠ 扫全了：`exhausted` 是「我还没扫完就没步数了」。

        上游把它算进 scanned_all → 没找到的目标被汇报成"确实不在列表里"
        （面板不报警）→ 静默漏发。这里用 400 条的长列表 + 3 步上限模拟现场。
        """
        self.s.total = 400          # 200 步 × 268px 也扫不完
        self.s.max_steps = 3
        with self.assertLogs("douyin_im", level="WARNING"):
            list(self.s._walk())
            self.s._finish_scan({}, ["没找到的人"])
        self.assertEqual(self.s._walk_stat["stopped"], "exhausted")
        self.assertFalse(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["missing"], ["没找到的人"])
        self.assertEqual(self.s.last_scan["note"], "扫描未完成，missing 不代表不存在")

    def test_exhausted_warning_names_max_steps_and_remaining_rows(self):
        """光把 scanned_all 改成 False 不够：日志里得有人喊，还要带上 imMaxSteps 的值。"""
        self.s.total = 400
        self.s.max_steps = 3
        list(self.s._walk())
        with self.assertLogs("douyin_im", level="WARNING") as cm:
            self.s._finish_scan({}, ["甲同学"])
        msg = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("imMaxSteps=3", msg)
        self.assertIn("甲同学", msg)            # 没找到的人要出现在告警里
        self.assertIn("不能", msg)              # 不能当"不在列表里"
        self.assertTrue(any("还剩约" in r.getMessage() for r in cm.records), cm.output)

    def test_no_move_is_still_scanned_all(self):
        """no-move（连续 3 轮 scrollTop 不动）是刻意的"到底了"启发式：语义不变。

        它和 exhausted 的区别在于：那是页面自己不动了，不是"我没步数了"。
        """
        self.s._scroll_to = lambda top: None      # 页面滚不动了
        list(self.s._walk())
        self.s._finish_scan({}, [])
        self.assertEqual(self.s._walk_stat["stopped"], "no-move")
        self.assertTrue(self.s.last_scan["scanned_all"])

    def test_scan_stop_is_exposed_for_the_caller(self):
        """停止原因要能被调用方直接写进日志/账本 note（tasks.py 还没消费，字段先给）。"""
        list(self.s._walk())                       # 50 条短列表 → 到底
        self.s._finish_scan({}, [])
        self.assertEqual(self.s.last_scan["scan_stop"], "reach-bottom")   # 与 stopped 同值
        self.assertEqual(self.s.last_scan["scan_stop"], self.s.last_scan["stopped"])

        long_list = _VListStub(total=400)
        long_list.max_steps = 3
        list(long_list._walk())
        with self.assertLogs("douyin_im", level="WARNING"):
            long_list._finish_scan({}, [])
        self.assertEqual(long_list.last_scan["scan_stop"], "exhausted")


class IterConversationsTests(unittest.TestCase):
    """任务二：枚举全部会话（旧 collect() 删掉后的替代 API）。"""

    def setUp(self):
        self.s = _VListStub(total=50)
        # 故意给一个对不上的 sec_uid —— 资料缺失时必须回退到 DOM 标题
        self.s.mon = JoinTests._MonStub("sec-that-matches-nothing")

    def test_yields_every_conversation_exactly_once(self):
        convs = list(self.s.iter_conversations())
        self.assertEqual(len(convs), 50)
        self.assertEqual(len({c["conv_id"] for c in convs}), 50)

    def test_items_are_joined_before_yield(self):
        convs = list(self.s.iter_conversations())
        self.assertTrue(all(c.get("display") for c in convs))
        self.assertEqual(convs[0]["display"], "U0")

    def test_last_scan_filled_after_enumeration(self):
        list(self.s.iter_conversations())
        self.assertIsNotNone(self.s.last_scan)
        self.assertTrue(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["visited"], 50)
        self.assertEqual(self.s.last_scan["found"], {})
        self.assertEqual(self.s.last_scan["missing"], [])


class EnvelopeTests(unittest.TestCase):
    def test_reads_logout_code_8(self):
        buf = bytes([0x18, 0x08, 0x22, 0x02, 0x4F, 0x4B])
        self.assertEqual(_envelope_code(buf), 8)

    def test_reads_ok_code_0(self):
        buf = bytes([0x18, 0x00, 0x22, 0x02, 0x4F, 0x4B])
        self.assertEqual(_envelope_code(buf), 0)

    def test_empty_buffer_does_not_raise(self):
        self.assertIsNone(_envelope_code(b""))

    def test_scan_parses_varint_and_bytes_fields(self):
        fs = scan(bytes([0x18, 0x00, 0x22, 0x02, 0x4F, 0x4B]))
        self.assertEqual(len([f for f in fs if f[0] in (3, 4)]), 2)
        self.assertTrue(any(f[1] == 0 and f[2] == 0 for f in fs))
        self.assertTrue(any(f[1] == 2 and f[2] == b"OK" for f in fs))

    def test_scan_tolerates_truncated_and_garbage(self):
        self.assertIsInstance(scan(b"\x18"), list)
        self.assertIsInstance(scan(b"\xff\xff\xff\xff"), list)

    def test_envelope_reads_code_and_status(self):
        env = envelope(bytes([0x18, 0x00, 0x22, 0x02, 0x4F, 0x4B]))
        self.assertEqual(env["code"], 0)
        self.assertEqual(env["status"], "OK")


class DecodeUserInfoTests(unittest.TestCase):
    def setUp(self):
        self.sec_a = fake_sec_uid()
        self.sec_b = fake_sec_uid()
        self.raw = {
            "data": [
                {"uid": int(PEER_A), "sec_uid": self.sec_a, "nickname": "NickA",
                 "remark_name": "甲同学", "unique_id": "user_aaa"},
                {"uid": int(PEER_B), "sec_uid": self.sec_b, "nickname": "NickB",
                 "unique_id": "user_bbb"},
            ]
        }

    def test_decodes_two_entries(self):
        self.assertEqual(len(decode_user_info(self.raw)), 2)

    def test_uid_becomes_string_without_precision_loss(self):
        ui = decode_user_info(self.raw)
        self.assertEqual(ui[0]["uid"], PEER_A)

    def test_has_remark_true_when_present(self):
        ui = decode_user_info(self.raw)
        self.assertIs(ui[0]["has_remark"], True)

    def test_has_remark_false_when_absent(self):
        ui = decode_user_info(self.raw)
        self.assertIs(ui[1]["has_remark"], False)

    def test_missing_remark_name_yields_empty_string(self):
        ui = decode_user_info(self.raw)
        self.assertEqual(ui[1]["remark"], "")

    def test_non_dict_returns_empty(self):
        self.assertEqual(decode_user_info("nonsense"), [])

    def test_bad_json_bytes_return_empty(self):
        self.assertEqual(decode_user_info(b"{not json"), [])


class SplitMessageLinesTests(unittest.TestCase):
    """消息正文断行口径（`split_message_lines`）。

    链路上有两种 `\\n`，必须都认：
      - **字面** `\\`+`n`：`.env` 的 MESSAGE_TEMPLATE，dotenv 读出来不做 unescape
      - **真**换行 `U+000A`：一言正文、手写 .env 的引号多行值

    历史 bug：只认字面 `\\n` → 真换行被 `keyboard.type` 静默吞掉，三行挤成一行。
    反过来只认真换行 → `.env` 的默认模板整段变成一行。所以两个方向都要钉。
    """

    def test_literal_backslash_n_splits(self):
        """正常路径：.env 出来的字面 \\n 必须断行。"""
        self.assertEqual(split_message_lines("甲\\n乙"), ["甲", "乙"])

    def test_real_newline_splits(self):
        """第三方内容带真换行 —— 不能吞。"""
        self.assertEqual(split_message_lines("甲\n乙"), ["甲", "乙"])

    def test_literal_crlf_splits_without_leaking_cr(self):
        """字面 \\r\\n：收成一段分隔，**绝不能漏下一个裸露的 \\r**。

        注意这里用的是 Python 源码里的 `"\\r\\n"` —— 它是 **4 个字符**
        （反斜杠 r 反斜杠 n），正是 `dotenv` 从 .env 读出来的形态。
        别和真 CRLF 混了：那才 2 字符。
        """
        got = split_message_lines("甲\\r\\n乙")
        self.assertEqual(got, ["甲", "乙"])
        self.assertNotIn("\r", "".join(got), "漏下了裸露的 \\r，会打进草稿")

    def test_real_crlf_splits(self):
        self.assertEqual(split_message_lines("甲\r\n乙"), ["甲", "乙"])

    def test_default_template_shape(self):
        """回归：默认模板必须断成 3 段（[盖瑞] / —— / [API]）。"""
        src = "[盖瑞]今日火花[加一]\\n—— 右边 每日一言 左边 ——\\n[API]"
        self.assertEqual(len(split_message_lines(src)), 3)

    def test_no_newline_is_single_segment(self):
        self.assertEqual(split_message_lines("单行"), ["单行"])

    def test_trailing_literal_n_keeps_empty_tail(self):
        """尾部空段不会多键入内容 —— 调用方的 `if line:` 负责挡掉。

        但段数仍是 2，意味着段间会按一次 Shift+Enter。这是既有的收尾行为，
        测试把它钉住，免得日后被当成 bug"修"掉。
        """
        self.assertEqual(split_message_lines("甲\\n"), ["甲", ""])

    def test_empty_and_none_are_safe(self):
        self.assertEqual(split_message_lines(""), [""])
        self.assertEqual(split_message_lines(None), ["None"])  # str(None)，不炸

    def test_normalization_invariant(self):
        """不变量：断完之后不该再残留【字面 \\n】或【真 \\r】。

        这才是这个函数的**规格**——段数只是实现细节。
        """
        samples = ["甲\\n乙", "甲\n乙", "甲\\n\n乙", "甲\\r\\n乙", "甲\r\n乙",
                   "甲\\r\n乙", "甲\n\\n乙", "甲\\r\\r\\n乙", "单行"]
        for src in samples:
            with self.subTest(src=src):
                joined = "\n".join(split_message_lines(src))
                self.assertNotIn("\\n", joined, f"{src!r} 残留字面 \\n")
                self.assertNotIn("\r", joined, f"{src!r} 残留真 \\r")

    def test_mixed_forms_both_split(self):
        """混用（字面 + 真）两次都要断：段内容正确，段数=3。"""
        self.assertEqual(split_message_lines("甲\\n\n乙"), ["甲", "", "乙"])


class ReadbackMatchesTests(unittest.TestCase):
    """`dry-run=full` 的读回校验口径（`_readback_matches`）—— 必须**按行**比。

    历史 bug：两边都 `re.sub(r"\\s+", "", ...)` 之后比字符，于是 `"甲\\n乙"`
    与 `"甲乙"` 归一化后相等 —— **换行丢了也判"匹配"**，`dry-run=full`
    这一档就白跑了（它存在的全部意义就是证明"文本真的进了编辑器"，含行结构）。

    真实故障（2026-10-02）：unit 用 systemd `EnvironmentFile=` 解析 `.env`，
    模板里的字面 `\\n` 被吃成裸 `n`，整条消息挤成一行发出去，
    而 `dry-run=full` 因为这条归一化把它判成 ✅。
    """

    def test_same_lines_match(self):
        self.assertEqual(_readback_matches(["甲", "乙"], "甲\n乙")[0], True)

    def test_lost_newline_does_not_match(self):
        """★ 核心回归：换行没了必须判不匹配（老实现会误判成匹配）。"""
        self.assertIs(_readback_matches(["甲", "乙"], "甲乙")[0], False)
        self.assertIs(_readback_matches(["甲", "乙", "丙"], "甲乙丙")[0], False)

    def test_extra_or_missing_line_does_not_match(self):
        self.assertIs(_readback_matches(["甲"], "甲\n乙")[0], False)
        self.assertIs(_readback_matches(["甲", "乙"], "甲")[0], False)

    def test_order_matters(self):
        self.assertIs(_readback_matches(["甲", "乙"], "乙\n甲")[0], False)

    def test_whitespace_inside_and_between_lines_is_ignored(self):
        # 块边界可能多出空行、行首行尾可能多空白 —— 这些不算错
        for got in ("甲\n乙", "甲\n\n乙", "\n甲\n乙\n", " 甲 \n\t乙\t"):
            with self.subTest(got=got):
                self.assertIs(_readback_matches(["甲", "乙"], got)[0], True)

    def test_returns_the_raw_readback(self):
        # 第二个返回值是**原文**（调用方要用它打日志/切片），不是归一化结果
        match, got = _readback_matches(["甲"], " 甲 ")
        self.assertIs(match, True)
        self.assertEqual(got, " 甲 ")

    def test_zero_width_filler_is_ignored(self):
        """★ 真机首次 dry-run=full 的误报就在这里：Draft.js 在软换行后面塞 `\\u200b`。

        它是填充不是文字 —— 肉眼看不见，`str.strip()` 与 JS `trim()` 都删不掉
        （`"\\u200b".strip()` 仍是 `"\\u200b"`）。不删掉的话，一份**完全正确**的
        读回会被判成"不一致"，接着触发重试 → 文本被追加第二遍。
        """
        got = ("[盖瑞]今日火花[加一]\u200b\n"
               "—— [右边] 每日一言 [左边] ——\u200b\n"
               "[API]\u200b")
        want = ["[盖瑞]今日火花[加一]", "—— [右边] 每日一言 [左边] ——", "[API]"]
        self.assertIs(_readback_matches(want, got)[0], True)
        # 只由零宽字符组成的"空行"同样不算一行
        self.assertIs(_readback_matches(["甲"], "甲\n\u200b")[0], True)

    def test_line_list_drops_invisible_lines(self):
        self.assertEqual(line_list("甲\n\u200b\n乙"), ["甲", "乙"])
        self.assertEqual(line_list("甲\u200b"), ["甲"])

    def test_empty_inputs_are_safe(self):
        self.assertIs(_readback_matches([], "")[0], True)
        self.assertIs(_readback_matches(None, None)[0], True)
        self.assertIs(_readback_matches(["甲", ""], "甲")[0], True)  # 尾部空段不算一行

    def test_crlf_readback_matches(self):
        self.assertIs(_readback_matches(["甲", "乙"], "甲\r\n乙")[0], True)


class InputVerificationContractTests(unittest.TestCase):
    """输入原语的两条硬要求（改坏了就等于没有验证）。"""

    def test_readback_uses_innertext(self):
        """读回必须用 `innerText`：Draft.js 的软换行是 `<br>`、硬换行是兄弟块，
        `textContent` 两种都不补换行符 —— 换行丢没丢，只有 innerText 看得见。
        """
        self.assertIn("innerText", douyin_im.JS_EDITOR_TEXT)
        self.assertIn("fallback", douyin_im.JS_EDITOR_TEXT)
        self.assertIn("brs", douyin_im.JS_EDITOR_TEXT)

    def test_soft_break_is_self_verified_with_fallbacks(self):
        """换行插入要自校验 + 逐级退化，并把结果报出来（brk）。

        合成 keydown 不一定被 Draft 认；认不出来时行数不会变多 ——
        老实现派发完 keydown/keyup 就返回，插没插进去谁也不知道。
        """
        js = douyin_im.JS_TYPE_LINE
        self.assertIn("insertLineBreak", js)          # ② 退化
        self.assertIn("res.brk", js)                  # 结果必须回传
        self.assertIn("lineCount", js)                # 用行数自校验
        self.assertIn("'none'", js)                   # 三种都不行时明确报 none

    def test_zero_width_filler_never_counts_as_content(self):
        """三处 JS 都必须先删零宽字符（`\\u200b` 等 Draft 填充）。

        不删的后果：①读回文本与期望"看起来一样"却不相等 → 误报不一致；
        ②清空后残留一个 `\\u200b`，`JS_EDITOR_EMPTY` 说"还有内容" →
        下一轮判定成"草稿残留"；③`lineCount` 把多出来的零宽字符当成多了一行。
        """
        for name in ("JS_EDITOR_TEXT", "JS_EDITOR_EMPTY", "JS_TYPE_LINE"):
            with self.subTest(js=name):
                self.assertIn("\\u200b", getattr(douyin_im, name),
                              "%s 要删掉零宽字符" % name)
        self.assertIn("replace", douyin_im.JS_EDITOR_TEXT)
        self.assertIn("replace", douyin_im.JS_EDITOR_EMPTY)

    def test_clear_editor_tries_drafts_own_key_path_first(self):
        """清空编辑器：**先走 Draft 自己的键盘路径**（全选 + Backspace），再退到 execCommand。

        老实现只做"全选 + `execCommand('delete')`"。真机实测（2026-10-03 dry-run=full）：
        它只抹 DOM、不动 Draft 的 EditorState —— 当场判空说"空了"，一打字 React
        重渲染又把旧文本带回来，dry-run 的读回里上一条消息原封不动地又出现了一遍。
        """
        js = douyin_im.JS_EDITOR_CLEAR
        self.assertIn("Backspace", js)                  # ① 键盘路径
        self.assertIn("execCommand('delete')", js)      # ② 兜底
        self.assertIn("selectNodeContents", js)         # 两条路都先全选
        self.assertIn("tries", js)                      # 逐级结果要回传
        self.assertIn("how", js)                        # 哪条路奏效要回传
        self.assertIn("already-empty", js)              # 本来就空时别乱按

    def test_all_editor_js_share_the_same_selectors(self):
        """读到内容的、判空的、清空的、打字的 —— **必须是同一个节点、同一个顺序**。

        老实现里判空/清空只认 2 个选择器、读回/输入认 4 个：列表一旦分叉就会
        "读到的是 A、清的是 B"，判定全乱（且很难从日志上看出来）。现在共用
        `JS_EDITOR_SELECTORS`，这里把"顺序一致"钉死。
        """
        want = ['[data-e2e="msg-input"] .public-DraftEditor-content',
                '.DraftEditor-root [contenteditable="true"]',
                '.messageMsgInput [contenteditable="true"]',
                '[data-e2e="msg-input"] [contenteditable="true"]',
                '[contenteditable="true"]']
        for name in ("JS_EDITOR_TEXT", "JS_EDITOR_EMPTY", "JS_EDITOR_CLEAR",
                     "JS_TYPE_LINE"):
            with self.subTest(js=name):
                got = re.findall(r"document\.querySelector\('([^']+)'\)",
                                 getattr(douyin_im, name))
                self.assertEqual(got, want, f"{name} 的选择器（数量和顺序）与别人不一致")


class ResidueEchoTests(unittest.TestCase):
    """叠发检测（`_residue_echo`）：真跑前唯一的"两条消息叠着发"拦阻条件。

    现场（2026-10-03 真机）：清空没生效 → 第二次输入时旧文本在重渲染里回来 →
    读回变成"上一遍三行 + 这一遍三行"。发出去朋友就会看到两遍。
    这个判据要**只在有强证据时**成立 —— 拿"读回≠期望"当判据的话，排版差异
    会误拦，代价是整天不发（比叠发更亏）。
    """

    WANT = ["[盖瑞]今日火花[加一]", "—— [右边] 每日一言 [左边] ——", "今天的一言"]

    def test_single_copy_is_not_flagged(self):
        self.assertEqual(_residue_echo(self.WANT, "\n".join(self.WANT)), (False, 1))

    def test_two_copies_are_flagged(self):
        got = "\n".join(self.WANT + self.WANT)
        self.assertEqual(_residue_echo(self.WANT, got), (True, 2))

    def test_residue_from_the_previous_attempt_is_flagged(self):
        # 真实现场：上一遍的第三行是**上一遍的一言**（每次请求都不一样）
        got = "\n".join([self.WANT[0], self.WANT[1], "上一遍的一言"]
                        + self.WANT)
        self.assertEqual(_residue_echo(self.WANT, got), (True, 2))

    def test_layout_only_mismatch_is_not_flagged(self):
        # 换行丢了：读回≠期望，但不是叠发 —— 不能拦
        self.assertEqual(_residue_echo(self.WANT, "".join(self.WANT)), (False, 1))
        self.assertEqual(_residue_echo(self.WANT, "完全不同的内容"), (False, 0))

    def test_short_head_is_never_flagged(self):
        # "hi" 这种首行到处都会撞上：宁可不拦，也不误拦
        self.assertEqual(_residue_echo(["hi"], "hi hi"), (False, 0))

    def test_empty_inputs_are_safe(self):
        self.assertEqual(_residue_echo([], ""), (False, 0))
        self.assertEqual(_residue_echo(None, None), (False, 0))


class DomTextMatchTests(unittest.TestCase):
    """气泡文本判据（`_dom_text_matches`）：DOM 侧回执确认的"文本这一条证据"。

    ★ 两个真实会误判的输入（本组测试的存在理由）：
    ① 多行模板 `"早上好\\n晚安"`：head 取自模板、含 `\\n`，而气泡 textContent 里
       没有换行（Draft.js 的块边界在 textContent 里直接相接）；
    ② 模板以 emoji 开头：emoji 在气泡里是 `<img>` 渲染的，textContent 读不到它。
    误判的后果不是"少确认一次"——已发成功却等不到 DOM 确认，没有 HTTP 回执时判
    失败，任务层重发同一条 → 好友收到两条（本项目最忌讳的失败形态）。
    """

    def test_plain_single_line_still_matches(self):
        self.assertTrue(_dom_text_matches("早上好", "早上好"))
        # 前 8 字的对照口径不变：DOM 里是整条消息 + 后面的内容也算匹配
        self.assertTrue(_dom_text_matches("早上好", "早上好呀，今天也要加油"))

    def test_multiline_template_matches_textcontent_without_newline(self):
        self.assertTrue(_dom_text_matches("早上好\n晚安", "早上好晚安"))
        self.assertTrue(_dom_text_matches("早上好\n晚安", "早上好晚安，明天见"))

    def test_emoji_at_head_matches_bubble_without_the_emoji(self):
        # 气泡里 emoji 是 <img>，textContent 读不到它
        self.assertTrue(_dom_text_matches("🎉早上好", "早上好"))
        self.assertTrue(_dom_text_matches("🌈 示例号 🌈 今日火花+1", "示例号 今日火花+1"))
        # 另一端也可能把 emoji 画成了文字，同样得对得上
        self.assertTrue(_dom_text_matches("🎉早上好", "🎉早上好"))

    def test_zero_width_and_variation_selectors_are_ignored(self):
        self.assertTrue(_dom_text_matches("早上好\u200b", "早上好"))
        self.assertTrue(_dom_text_matches("早上好", "\ufeff早上好\ufe0f"))
        self.assertTrue(_dom_text_matches("早\u200d上好\ufe0e", "早上好"))

    def test_all_whitespace_differences_are_ignored(self):
        # 全角空格、NBSP、制表符、行首行尾的空行
        self.assertTrue(_dom_text_matches("早\u3000上\xa0好", "早上好"))
        self.assertTrue(_dom_text_matches("  早上好 ", "\t早上好\n"))

    def test_empty_head_defers_to_count_and_last_from_me(self):
        """整条都是 emoji/空白 → 文本给不出结论，返回 True 交回给调用方那两个条件。

        这里不是在放宽判据：`count > before` 与 `lastFromMe` 仍然在 `_wait_receipt`
        里独立成立才算 DOM 确认（见 ReceiptVerdictIntegrationTests）。
        """
        self.assertTrue(_dom_text_matches("🎉🎉🎉", "完全不同的东西"))
        self.assertTrue(_dom_text_matches("", "随便什么"))
        self.assertTrue(_dom_text_matches(" \n\t\u200b", "随便什么"))
        self.assertTrue(_dom_text_matches(None, None))

    def test_clearly_different_text_is_not_a_match(self):
        self.assertFalse(_dom_text_matches("早上好", "昨天的消息"))
        self.assertFalse(_dom_text_matches("早上好", ""))
        self.assertFalse(_dom_text_matches("早上好", None))
        # 只有开头的 emoji 不一样、文字部分完全对不上时，仍然判不匹配
        self.assertFalse(_dom_text_matches("🎉早上好", "晚上好"))


class _ClearPage:
    """按剧本演"清空编辑器"这件事的假页面（只实现 `_clear_editor` 用到的两个方法）。"""

    def __init__(self, script, text=""):
        self.script = list(script)      # 每次 JS_EDITOR_CLEAR 的剧本
        self.text = text
        self.probes = 0                 # 逼重渲染的探针字符插了几次
        self.exc = None

    def evaluate(self, expr, arg=None):
        if self.exc:
            raise self.exc
        if expr is douyin_im.JS_EDITOR_CLEAR:
            act = self.script.pop(0) if self.script else "lie"
            if act == "key":
                self.text = ""
                return {"ok": True, "how": "key", "left": ""}
            if act == "already":
                return {"ok": True, "how": "already-empty", "left": ""}
            if act == "none":
                return {"ok": False, "how": "none", "left": self.text[:20]}
            return {"ok": True, "how": "key", "left": ""}   # lie：说清好了，其实没清
        if expr is douyin_im.JS_EDITOR_EXISTS:
            return True
        if expr is douyin_im.JS_EDITOR_TEXT:
            return {"text": self.text, "len": len(self.text)}
        if expr is douyin_im.JS_EDITOR_EMPTY:
            return self.text.strip() == ""
        if expr is douyin_im.JS_TYPE_LINE:
            self.probes += 1
            if arg and arg.get("text"):
                self.text += arg["text"]        # 探针字符真的进了编辑器
            return {"ins": True, "grew": 1}
        return None

    def wait_for_timeout(self, ms):
        return None


class _ClearIM:
    """借用真方法的最小宿主：不构造 DouyinIM（它要 page + 导航 + 钩子）。"""

    _clear_editor = douyin_im.DouyinIM._clear_editor
    _editor_probe = douyin_im.DouyinIM._editor_probe

    def __init__(self, page):
        self.page = page
        self._editor_dirty = True


class ClearEditorTests(unittest.TestCase):
    """清空编辑器必须**验证**，不能"调用过就算清过了"。"""

    def test_verified_clear_reports_how_and_clears_the_dirty_flag(self):
        im = _ClearIM(_ClearPage(["key"], text="旧文本"))
        self.assertEqual(im._clear_editor(), (True, "key", ""))
        self.assertFalse(im._editor_dirty)

    def test_clear_that_only_lies_about_success_is_not_believed(self):
        """JS 说 ok、读回来还有文本（真机的 `execCommand('delete')` 就是这样）→ 不算清空。"""
        im = _ClearIM(_ClearPage(["lie", "lie"], text="旧文本"))
        cleared, how, _ = im._clear_editor()
        self.assertFalse(cleared)
        self.assertEqual(how, "key")            # how 只反映 JS 的自我报告
        self.assertTrue(im._editor_dirty)       # 没清干净就不能把账勾掉

    def test_distrust_empty_forces_a_rerender_probe(self):
        """上一次没清干净时，"看着是空的"不能信：先插一个字符逼 Draft 重渲染。"""
        page = _ClearPage(["already", "key"], text="")
        im = _ClearIM(page)
        cleared, _, _ = im._clear_editor(distrust_empty=True)
        self.assertTrue(cleared)
        self.assertEqual(page.probes, 1)        # 真的插了一次探针字符
        self.assertFalse(im._editor_dirty)

    def test_without_distrust_the_empty_read_is_trusted(self):
        page = _ClearPage(["already"], text="")
        im = _ClearIM(page)
        self.assertEqual(im._clear_editor(), (True, "already-empty", ""))
        self.assertEqual(page.probes, 0)        # 没被怀疑就别多插字符

    def test_gives_up_and_reports_the_leftover(self):
        im = _ClearIM(_ClearPage(["none", "none"], text="旧文本"))
        cleared, how, left = im._clear_editor()
        self.assertFalse(cleared)
        self.assertEqual(how, "none")
        self.assertEqual(left, "旧文本")

    def test_js_error_is_reported_not_raised(self):
        page = _ClearPage([], text="旧文本")
        page.exc = RuntimeError("页面没了")
        im = _ClearIM(page)
        # 第三项是 None（读不到），不是 ""（读到了、确实是空的）：把"读不到"说成
        # "已经空了"会让调用方以为清干净了，Draft 里的残留就留到下次输入叠两条。
        self.assertEqual(im._clear_editor(), (False, "error", None))
        self.assertTrue(im._editor_dirty)

    def test_probe_failure_does_not_mark_the_editor_clean(self):
        """逼重渲染的探针自己炸了 → 必须报失败，不能把"没读到"当成"已清空"。

        老代码在探针失败后 `pass` 掉、继续走下一轮；下一轮读到 `empty=True`
        就把 `_editor_dirty` 清掉，残留文本会跟着下一条消息一起发出去。
        """
        page = _ClearPage(["key"], text="残留")
        real = page.evaluate

        def evaluate(expr, arg=None):
            if expr is douyin_im.JS_TYPE_LINE:
                raise RuntimeError("探针字符插不进去")
            return real(expr, arg)

        page.evaluate = evaluate
        im = _ClearIM(page)
        cleared, how, left = im._clear_editor(distrust_empty=True)
        self.assertFalse(cleared)
        self.assertEqual(how, "probe_failed")
        self.assertEqual(left, "")          # 上一轮读到的是空串
        self.assertTrue(im._editor_dirty)    # 关键：账不能勾掉


def _envelope_bytes(code: int, filler: int = 0) -> bytes:
    """造一个最小 protobuf 信封：字段 3（返回码）= code，可选塞一段 filler 字节的业务体。"""
    buf = bytes([0x18, code & 0x7F])
    if filler:
        length = filler
        varint = b""
        while True:
            byte = length & 0x7F
            length >>= 7
            varint += bytes([byte | (0x80 if length else 0)])
            if not length:
                break
        buf += bytes([0x32]) + varint + b"x" * filler    # 字段 6 = 业务体
    return buf


class _LoginLostIM:
    """借用真的掉登录监控（`_watch_login_lost` / `_emit_login_lost`）的最小宿主。"""

    _watch_login_lost = douyin_im.DouyinIM._watch_login_lost
    _emit_login_lost = douyin_im.DouyinIM._emit_login_lost

    def __init__(self, page):
        self.page = page
        self._login_dead = False
        self._login_dead_reason = ""
        self._login_lost = []


class _Resp:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def body(self):
        return self._body


class LoginLostWatcherTests(unittest.TestCase):
    """运行期掉登录监控：`status_code=8` 必须**优先**认出来。"""

    def _host(self):
        page = _FakePage()
        im = _LoginLostIM(page)
        im._watch_login_lost()
        return im, page

    def test_status_code_8_marks_the_session_dead(self):
        im, page = self._host()
        for cb in page.listeners["response"]:
            cb(_Resp("https://imapi.douyin.com/v1/message/send", _envelope_bytes(8)))
        self.assertTrue(im._login_dead)

    def test_long_response_is_still_checked_for_code_8(self):
        """老代码把 `len(body) > 4096` 挡在解信封前面：长响应里的掉登录码直接漏过去。

        会话列表 / 长消息体本来就可能几 KB；漏一次就得等 20 秒超时后靠复检兜，
        期间发送循环会一条条把名额烧掉（2026-10-09 就是这样）。
        """
        im, page = self._host()
        body = _envelope_bytes(8, filler=6000)
        self.assertGreater(len(body), 4096)
        for cb in page.listeners["response"]:
            cb(_Resp("https://imapi.douyin.com/v1/message/list", body))
        self.assertTrue(im._login_dead)

    def test_other_codes_and_urls_leave_the_session_alone(self):
        im, page = self._host()
        for cb in page.listeners["response"]:
            cb(_Resp("https://imapi.douyin.com/v1/message/send", _envelope_bytes(0)))
            cb(_Resp("https://www.douyin.com/aweme/v1/web/foo", _envelope_bytes(8)))
        self.assertFalse(im._login_dead)

    def test_body_that_cannot_be_read_is_ignored(self):
        im, page = self._host()

        class Boom(_Resp):
            def body(self):
                raise RuntimeError("body 读不出来")

        for cb in page.listeners["response"]:
            cb(Boom("https://imapi.douyin.com/v1/message/send", b""))
        self.assertFalse(im._login_dead)


class _FakePage:
    """只实现 ImMonitor 用到的两个方法：on() 和（此处不需要的）其余一概不管。"""

    def __init__(self):
        self.listeners = {}

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        self.listeners.get(event, []).remove(cb)


class LoggingTests(unittest.TestCase):
    """沉默期改造后的日志契约。

    这次改造把 `verbose` 开关换成了真正的 logger 分级，所以要有测试钉住：
    【1】模块级 logger 存在且名字固定；
    【2】`verbose` / `log_fn` / `log()` / `ImMonitor._log` 已彻底不存在
         （否则就是改造没做干净，旧开关会和新 logger 双重过滤）；
    【3】关键事件按预期级别落日志 —— 用 assertLogs 真正捕获，
         而不是"看着代码觉得它该打 info"。
    """

    def test_module_logger_name_is_stable(self):
        self.assertEqual(douyin_im.logger.name, "douyin_im")

    def test_removed_log_plumbing(self):
        """旧的四个日志开关/通道必须全没了。"""
        self.assertFalse(hasattr(ImMonitor, "_log"))
        self.assertFalse(hasattr(DouyinIM, "log"))
        src = inspect.signature(ImMonitor.__init__)
        self.assertNotIn("verbose", src.parameters)
        self.assertNotIn("verbose", inspect.signature(DouyinIM.__init__).parameters)
        self.assertNotIn("log", inspect.signature(check_login).parameters)

    def test_im_monitor_login_logs_at_info(self):
        """收到登录 SSR → [LOGIN] 走 info。"""
        mon = ImMonitor(_FakePage())
        body = json.dumps({
            "odin": {"user_id": ME},
            "user": {"isLogin": True, "nickname": "NickA"},
        }).encode("utf-8")
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            mon._handle_login(body)
        self.assertTrue(any(r.levelname == "INFO" and "[LOGIN]" in r.getMessage()
                            for r in cm.records), cm.output)

    def test_im_monitor_init_round_logs_at_debug(self):
        """列表页轮次属于机械进度 → 必须是 debug，不然 Info 级会刷屏。"""
        mon = ImMonitor(_FakePage())
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            mon._handle_init(b"")
        self.assertTrue(any(r.levelname == "DEBUG" for r in cm.records), cm.output)

    def test_send_success_is_info_failure_is_warning(self):
        """发送回执按成败分流：成了 info，没成 warning。"""
        mon = ImMonitor(_FakePage())
        mon.sends = []

        def _fake_decode(ok):
            return lambda body: {"ok": ok, "code": 0 if ok else 8,
                                 "status": "OK" if ok else "ERR", "message_id": None}

        real = douyin_im.decode_send_resp
        try:
            douyin_im.decode_send_resp = _fake_decode(True)
            with self.assertLogs("douyin_im", level="DEBUG") as cm:
                mon._handle_send(b"")
            self.assertTrue(any(r.levelname == "INFO" for r in cm.records), cm.output)

            douyin_im.decode_send_resp = _fake_decode(False)
            with self.assertLogs("douyin_im", level="DEBUG") as cm:
                mon._handle_send(b"")
            self.assertTrue(any(r.levelname == "WARNING" for r in cm.records),
                            cm.output)
        finally:
            douyin_im.decode_send_resp = real

    def test_check_login_does_not_take_log_param(self):
        """结论行改由调用方记录 —— check_login 自己不再打日志。"""
        params = inspect.signature(check_login).parameters
        self.assertNotIn("log", params)

    def test_hook_already_installed_logs_at_debug_not_warning(self):
        """钩子第二次安装返回 undefined（JS 第一句就 `return`）—— 正常路径，归 debug。

        改造前每轮每个目标都会刷一条 `[SEL][HOOK] 安装=None` 的 warning（线上日志
        实测：每个目标一条），把真正的失败淹在噪音里。
        """
        im = _Stub()
        im.page = _HookPage(None)
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            with self.assertRaises(RuntimeError):    # 后面的定位由替身终止
                im._mouse_select(0, "mouse")
        hook = [r for r in cm.records if "[SEL][HOOK]" in r.getMessage()]
        self.assertEqual(len(hook), 1, cm.output)
        self.assertEqual(hook[0].levelname, "DEBUG", cm.output)
        self.assertIn("安装=None", hook[0].getMessage())

    def test_hook_install_state_is_still_warned(self):
        """真装上了（或装了但状态异常）→ 照旧 warning，别把信号一起调没了。"""
        im = _Stub()
        im.page = _HookPage("hooked:true")
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            with self.assertRaises(RuntimeError):
                im._mouse_select(0, "mouse")
        self.assertTrue(any(r.levelname == "WARNING"
                            and "安装=hooked:true" in r.getMessage()
                            for r in cm.records), cm.output)

    def test_hook_install_failure_is_still_warned(self):
        """`evaluate` 自己抛了（页面没了 / 脚本报错）—— 这才是真失败，保留 warning。"""
        im = _Stub()
        im.page = _HookPage(boom=RuntimeError("页面没了"))
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            with self.assertRaises(RuntimeError):
                im._mouse_select(0, "mouse")
        self.assertTrue(any(r.levelname == "WARNING" and "安装失败" in r.getMessage()
                            for r in cm.records), cm.output)


class _HookPage(_FakePage):
    """只够 `_mouse_select` 走到"装输入钩子"那一步的页面替身。

    `evaluate_handle` 直接抛错：钩子那行日志在它之前就打完了，后续定位元素的事
    本组测试不关心，异常由 `assertRaises(RuntimeError)` 接住。
    """

    def __init__(self, hook_state=None, boom=None):
        super().__init__()
        self.hook_state = hook_state
        self.boom = boom
        self.hook_calls = 0

    def evaluate(self, script, arg=None):
        if self.boom is not None:
            raise self.boom
        if script is DouyinIM.JS_INPUT_HOOK:
            self.hook_calls += 1
            return self.hook_state       # 第二次调用时 JS 直接 return → None
        return None

    def evaluate_handle(self, script, arg=None):
        raise RuntimeError("本组测试到此为止（钩子日志已经写完了）")


class _SelPage(_FakePage):
    """`_select_and_verify` / `_wait_receipt` 用到的最小页面替身。

    `wait_for_timeout` 是空实现：重试循环的次数与分支完全不变，只是不真的睡，
    否则每条失败路径都要等满 12 × 200ms × 3 种方式。
    """

    def __init__(self):
        super().__init__()
        self.removed = []

    def wait_for_timeout(self, ms):
        pass

    def remove_listener(self, event, cb):
        self.removed.append((event, cb))
        super().remove_listener(event, cb)


class SelectionFailClosedTests(unittest.TestCase):
    """把「乐观通过」三条路径钉在**真实** `_select_and_verify` 上。

    单测 `core.strict_verify` 只能证明判定函数本身对；这里证明判定函数
    **确实被接进了发送主链路**——否则改了判定、没改调用点，洞还在。
    """

    class _IM(_Stub):
        def __init__(self):
            super().__init__()
            self.page = _SelPage()
            self.mon = None
            self.last_select_verdict = None
            self.last_receipt_verdict = None
            self.cur = None
            self.idx = 1

        # —— 只替换真正碰浏览器的四个点，其余走真实实现 ——
        def _input_mode(self):
            return "real"

        def _type_real(self, editor, lines, dry):
            # 这些用例钉的是回执/熔断/登录态接线，打字机制由 ReplaceTypingTests 覆盖。
            self.typed_text = "\n".join(lines)
            return True

        def _dom_index_of(self, conv_id):
            return self.idx

        def _mouse_select(self, idx, mode):
            return {"x": 1, "y": 1, "mode": mode}

        def _current_conv(self):
            return self.cur

    def setUp(self):
        self.im = self._IM()
        # ME 与 PEER_A 的会话 id（uid 数值升序），与 core.strict_verify 的算法互逆
        self.conv_a = f"0:1:{ME}:{PEER_A}"
        self.conv_b = f"0:1:{ME}:{PEER_B}"
        self.item = {"conv_id": self.conv_a, "uid": PEER_A, "display": "甲同学",
                     "data_index": 0, "matched_by": "remark"}

    def _convs(self, conv_id=None, title="甲同学"):
        """设置「当前选中会话」的读数；conv_id=None 表示 fiber 读不到 id。"""
        self.im.cur = None if conv_id is None else {"convId": conv_id, "title": title}

    def test_conv_id_readable_but_it_is_someone_else_is_refused(self):
        """上游 C:1612 `cur["convId"] == want` 这一支本身是对的，但它只比较
        相等、不等就直接掉进「乐观通过」；现在不等 = 明确选错人 = 失败。"""
        self._convs(self.conv_b, "乙同学")
        self.assertFalse(self.im._select_and_verify(self.item))
        v = self.im.last_select_verdict
        self.assertFalse(v.ok)
        self.assertEqual(v.level, "conflict")
        self.assertEqual(v.reason, "identity_mismatch")

    def test_selection_without_readable_conv_id_is_refused(self):
        """C:1612 `not cur.get("convId")` 判成功 → 现在必须失败。"""
        self.im.cur = {"convId": None, "title": "另一个人"}
        self.assertFalse(self.im._select_and_verify(self.item))
        self.assertFalse(self.im.last_select_verdict.ok)

    def test_network_signal_without_dom_is_refused(self):
        """C:1623 `if net:` 判成功 → 现在必须失败（网络信号只作辅助）。

        `_on_resp` 是 `_select_and_verify` 内部注册的闭包，外部拿不到；所以这里
        从 `page.listeners` 取回**真实**的监听器手工喂响应，让 `probe["hit"]`
        真的变成 True —— 比伪造一个 `net` 参数更接近现场。

        关键差异：本轮点击**不抛异常**（抛了就 continue，走不到裁决那一步），
        让流程真正跑完「观察 → 裁决」，从而证明"网络命中 + DOM 无选中态"
        这条上游判成功的路径现在判失败。
        """
        conv = self.item["conv_id"]
        page = self.im.page
        fired = []

        class _Resp:
            url = "https://imapi.douyin.com/v1/message/get_by_conversation"

            @staticmethod
            def body():
                return conv.encode()

        def _mouse_select(idx, mode):
            # 每轮都喂一次响应，保证三种点击方式都是在 "net=True" 下裁决的
            fired.append(mode)
            for cb in list(page.listeners.get("response", [])):
                cb(_Resp())
            return {"x": 1, "y": 1, "mode": mode}

        original = self.im._mouse_select
        self.im._mouse_select = _mouse_select
        self.im.cur = None              # DOM 完全没有选中态
        try:
            self.assertFalse(self.im._select_and_verify(self.item))
        finally:
            self.im._mouse_select = original

        self.assertTrue(fired, "探针没被喂到响应，这条测试没测到东西")
        v = self.im.last_select_verdict
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, "no_conv_id")
        self.assertIs(v.evidence["signals"]["conv_id"], False)
        self.assertIs(v.evidence["signals"]["network"], True)

    def test_network_signal_for_target_while_wrong_conv_selected(self):
        """最危险的一种现场：网络确实拉到了目标会话，但选中态还停在别人身上。

        上游对这条判成功（`if net: return True`），正好会把消息发给上一个人。
        """
        conv = self.item["conv_id"]
        page = self.im.page
        fired = []

        class _Resp:
            url = "https://imapi.douyin.com/v1/message/get_by_conversation"

            @staticmethod
            def body():
                return conv.encode()

        def _mouse_select(idx, mode):
            if not fired:
                fired.append(True)
                for cb in list(page.listeners.get("response", [])):
                    cb(_Resp())
            return {"x": 1, "y": 1, "mode": mode}

        original = self.im._mouse_select
        self.im._mouse_select = _mouse_select
        self._convs(self.conv_b, "乙同学")
        try:
            self.assertFalse(self.im._select_and_verify(self.item))
        finally:
            self.im._mouse_select = original

        self.assertTrue(fired)
        self.assertEqual(self.im.last_select_verdict.reason, "identity_mismatch")

    def test_matching_conv_id_passes(self):
        self._convs(self.conv_a)
        self.assertTrue(self.im._select_and_verify(self.item))
        self.assertTrue(self.im.last_select_verdict.ok)
        self.assertEqual(self.im.last_select_verdict.level, "strong")

    def test_peer_uid_alone_is_still_strong(self):
        """conv_id 读不到，但页面上取到了对方 uid 且与目标一致 → 仍是硬证据。

        `JS_CURRENT_CONV` 从同一条 fiber 上同时取 `convId` 与 `peerUid`
        （`participant.toParticipantUserId`），所以"conv_id 读不到但 uid 读到了"
        是真实会发生的组合 —— 这时不该退化成"只能靠显示名"。
        """
        self.im.cur = {"convId": None, "title": "甲同学", "peerUid": PEER_A}
        self.assertTrue(self.im._select_and_verify(self.item))
        self.assertEqual(self.im.last_select_verdict.level, "strong")
        self.assertTrue(self.im.last_select_verdict.evidence["signals"]["peer_uid"])

    def test_peer_uid_of_someone_else_is_refused(self):
        """页面上取到的对方 uid 是别人 → 硬反证，比标题对得上更硬。"""
        self.im.cur = {"convId": None, "title": "甲同学", "peerUid": PEER_B}
        self.assertFalse(self.im._select_and_verify(self.item))
        self.assertEqual(self.im.last_select_verdict.reason, "identity_mismatch")
        self.assertEqual(self.im.last_select_verdict.level, "conflict")

    def test_title_only_without_any_hard_id_is_medium(self):
        """既没有 conv_id 也不知道 uid（纯显示名匹配）时，命中标题只给 medium。

        端到端这里必须能通过，否则"用户只填昵称"的常规用法会被 fail-closed
        误杀；但级别降到 medium，调用方据此可以决定要不要更保守。
        """
        item = {"conv_id": None, "uid": None, "display": "甲同学",
                "data_index": 0, "matched_by": "title"}
        self.im.cur = {"convId": None, "title": "甲同学"}
        self.assertTrue(self.im._select_and_verify(item))
        self.assertEqual(self.im.last_select_verdict.level, "medium")

    def test_ambiguous_keyword_is_refused(self):
        """关键词命中多个不同对象时，扫描层就标了 ambiguous —— 必须拒绝自动发送。"""
        item = dict(self.item)
        item["ambiguous"] = True
        self._convs(self.conv_a)
        self.assertFalse(self.im._select_and_verify(item))
        self.assertEqual(self.im.last_select_verdict.reason, "identity_unknown")

    def test_listener_removed_after_every_attempt(self):
        self._convs(self.conv_a)
        self.im._select_and_verify(self.item)
        self.assertEqual(self.im.page.removed[0][0], "response")
        self.assertEqual(self.im.page.listeners["response"], [])

    def test_none_data_index_scrolls_instead_of_raising_type_error(self):
        """`data_index=None`（JS_COLLECT 的 `holder ? Number(...) : null`）不许打断整轮。

        老代码 `item.get("data_index", 0) * ROW_HEIGHT` → 键在、值是 None，默认值
        不生效 → `None * 67` 抛 TypeError → 从这里一路穿到 tasks.py 的账号级 except：
        整轮记 error，排在后面的好友一个都不发。这里把「目标不在当前窗口 → 按
        data_index 滚过去」那条真实分支跑通，断言滚动落点而不是错误类型。
        """
        scrolled = []

        class _IM(self._IM):
            def __init__(self):
                super().__init__()
                self.lookups = 0

            def _dom_index_of(self, conv_id):
                # 第一次：不在当前窗口里（正是要走"滚过去再找"那一步）
                self.lookups += 1
                return None if self.lookups == 1 else self.idx

            def _scroll_to(self, top):
                scrolled.append(top)
                return True

        im = _IM()
        im.cur = {"convId": self.conv_a, "title": "甲同学"}
        self.assertTrue(im._select_and_verify(dict(self.item, data_index=None)))
        self.assertEqual(scrolled, [0])        # 退回顶部，而不是 None * 67

        # 同一窗口里的渲染序号也够用：能拿到就用它，别白滚一趟
        im2 = _IM()
        im2.cur = {"convId": self.conv_a, "title": "甲同学"}
        self.assertTrue(im2._select_and_verify(
            dict(self.item, data_index=None, dom_index=5)))
        self.assertEqual(scrolled, [0, 5 * ROW_HEIGHT])


class ReceiptVerdictIntegrationTests(unittest.TestCase):
    """把回执裁决钉在真实 `_wait_receipt` 上（含 convId 采集与「发错人」判定）。"""

    class _IM(_Stub):
        def __init__(self):
            super().__init__()
            self.page = _SelPage()
            self.mon = type("M", (), {"sends": []})()
            self.cur = None
            self.state = {"count": 10, "lastFromMe": False, "lastText": "上一条"}
            self.last_receipt_verdict = None

        def _current_conv(self):
            return self.cur

        def _msg_state(self):
            """直接给出「发送之后」的读数。

            真实实现在这里会 `page.evaluate(JS_MSG_STATE)` + `_current_conv()`，
            而 convId 的接线由 `test_msg_state_carries_conv_id` 单独钉住；
            本组测试要控的是气泡数与会话归属的组合，所以直接给快照。
            """
            st = dict(self.state)
            st["convId"] = (self.cur or {}).get("convId")
            return st

    def setUp(self):
        self.im = self._IM()
        self.conv_a = f"0:1:{ME}:{PEER_A}"
        self.conv_b = f"0:1:{ME}:{PEER_B}"
        self.item = {"conv_id": self.conv_a, "uid": PEER_A, "display": "甲同学"}
        self.before = {"convId": self.conv_a, "count": 10, "lastFromMe": False}

    def test_wrong_conv_after_send_is_conflict_even_with_ok_receipt(self):
        """发送后当前会话变成了别人 → 「发错人」，比「没发出去」严重。"""
        self.im.cur = {"convId": self.conv_b}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "你好"}
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertFalse(rc["ok"])
        self.assertEqual(rc["verdict"].reason, "receipt_wrong_conv")
        self.assertTrue(rc["dom_conv_off"])

    def test_scan_conv_id_reaches_verdict_when_self_uid_is_unknown(self):
        """扫描阶段读到的目标 conv_id 必须原样进判定层（GitHub issue #13）。

        `_wait_receipt` 从前构造期望时**漏传** `conv_id=want`，于是
        `verify_receipt` 里"发送后当前会话不是目标"的第一条闸恒不成立；
        这里把 `self_uid` 也抠掉（DOM 兜底判登录时它是 None，是常态），
        反算路径同时失效 —— 结果是**发错人**只剩显示名可看，被降级成
        `ok=True / medium`，任务层记成发送成功、当天名额被吃掉。
        """
        self.im._state = {}                      # self_uid 拿不到（常态）
        # 发送前后页面上的会话都是 conv_b：第二条闸（发送前后不同）不成立，
        # 所以只有"扫描读到的 conv_id 传下去了"才能认出这次发错了人
        self.im.cur = {"convId": self.conv_b}
        self.before = {"convId": self.conv_b, "count": 10, "lastFromMe": False}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "你好"}
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertFalse(rc["ok"])
        self.assertEqual(rc["verdict"].reason, "receipt_wrong_conv")
        self.assertTrue(rc["dom_conv_off"])
        self.assertEqual(rc["verdict"].evidence["want_conv"], self.conv_a)
        self.assertIn("会话不是目标", rc["verdict"].detail)

    def test_unknown_self_uid_and_no_scan_conv_id_still_catches_wrong_peer(self):
        """扫描没读到 conv_id、self_uid 也拿不到 → 退回"目标 uid 在不在会话里"。

        判据只剩 `_conv_holds_peer`：会话里没有 PEER_A → 一样判「发错人」。
        从前这一格返回 `ok=True / medium`，正是 issue #13 报的"稳定地看错人却算成功"。
        """
        self.im._state = {}
        self.item = {"uid": PEER_A, "display": "甲同学"}     # 扫描没读到 conv_id
        self.im.cur = {"convId": self.conv_b}
        self.before = {"convId": None, "count": 10, "lastFromMe": False}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "你好"}
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertFalse(rc["ok"])
        self.assertEqual(rc["verdict"].reason, "receipt_wrong_conv")
        self.assertEqual(rc["verdict"].level, "conflict")
        self.assertIsNone(rc["verdict"].evidence["want_conv"])
        self.assertIs(rc["verdict"].evidence["recipient_ok"], False)
        self.assertIn("的参与者里没有目标", rc["verdict"].detail)

    def test_unknown_self_uid_with_right_conv_is_still_strong(self):
        """同一格的正向对照：会话里**有** PEER_A → 仍然判成功且强证据。

        "认不出收件人"和"认出收件人不是目标"必须继续分开：把上面那条修成
        conflict 之后，这里不能跟着一起变失败（否则每一次发送都会判失败、
        重复发送）。
        """
        self.im._state = {}
        self.item = {"uid": PEER_A, "display": "甲同学"}     # 扫描没读到 conv_id
        self.im.cur = {"convId": self.conv_a}
        self.before = {"convId": None, "count": 10, "lastFromMe": False}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "你好"}
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertTrue(rc["ok"])
        self.assertEqual(rc["verdict"].level, "strong")
        self.assertIs(rc["verdict"].evidence["recipient_ok"], True)

    def test_failed_receipt_without_dom_growth_is_failure(self):
        """服务端明确拒绝、DOM 也没涨 → 失败（conflict 级，调用方应告警而不是重发）。"""
        self.im.cur = {"convId": self.conv_a}
        self.im.mon.sends = [{"ok": False, "message_id": None, "code": 8,
                              "status": "ERR"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertFalse(rc["ok"])
        self.assertEqual(rc["verdict"].reason, "receipt_failed")

    def test_ok_receipt_without_dom_growth_is_success_and_strong(self):
        """虚拟列表不重渲染时气泡数**不涨**很常见；会话没变、回执 ok 就算发出去了。

        强证据的**正向**条件是"回执 ok + 发送后当前会话就是目标会话"（能点名收件人），
        刻意不要求气泡数上涨 —— 生产 2026-10-01→10-13 的 81 条成功记录里 DOM 气泡
        证据一次都没出现过，拿它当必要条件等于把每一次发送都判成弱证据。
        上游在这里判失败（只看 DOM 增长）→ 重试一次 → 重复消息；融合版保留
        ok=True，气泡数只记进 evidence 不作反证。
        """
        self.im.cur = {"convId": self.conv_a}
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertTrue(rc["ok"])
        self.assertEqual(rc["verdict"].level, "strong")
        self.assertTrue(rc["verdict"].evidence["recipient_ok"])

    def test_ok_receipt_without_conv_id_is_success_but_weak(self):
        """回执 ok 但 DOM 连会话 id 都读不到 → 点不出收件人，只能算弱证据。

        回执里没有收件人字段（`decode_send_resp` 只解 code/status/message_id），
        所以"服务端收下了一条消息"证明不了"收件人是甲同学"。
        """
        self.im.cur = {}                      # 读不到当前会话
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertTrue(rc["ok"])
        self.assertEqual(rc["verdict"].level, "medium")
        self.assertIn("无法确认收件人是目标", rc["verdict"].reason)

    def test_ok_receipt_with_dom_growth_in_target_conv_is_strong(self):
        """回执 ok + 目标会话里多了一个自己发的气泡（文本也对得上）→ 强证据。"""
        self.im.cur = {"convId": self.conv_a}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "你好"}
        self.im.mon.sends = [{"ok": True, "message_id": "mid-1", "code": 0,
                              "status": "OK"}]
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertTrue(rc["ok"])
        self.assertEqual(rc["verdict"].level, "strong")

    def test_no_observation_at_all_is_not_success(self):
        self.im.cur = None
        self.im.mon.sends = []
        rc = self.im._wait_receipt(0, self.before, "你好", 0.2, hit=self.item)
        self.assertFalse(rc["ok"])

    def test_dom_only_confirmation_accepts_multiline_template(self):
        """多行模板：气泡 textContent 里没有换行，DOM 确认照样要建得起来。

        改造前 head 是 `"早上好\\n晚安"[:8]`（含 `\\n`）→ startswith 永远不成立 →
        dom 一直是 None → 没有 HTTP 回执时判失败 → 任务层重发同一条 → 好友收到两条。
        """
        self.im.cur = {"convId": self.conv_a}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "早上好晚安"}
        self.im.mon.sends = []
        rc = self.im._wait_receipt(0, self.before, "早上好\n晚安", 0.2, hit=self.item)
        self.assertTrue(rc["dom"], "DOM 确认没建起来：文本判据没接进 _wait_receipt")
        self.assertTrue(rc["ok"])
        self.assertIsNone(rc["http"])

    def test_dom_only_confirmation_accepts_emoji_head(self):
        """emoji 开头：气泡里那个 emoji 是 <img>，textContent 读不到它。"""
        self.im.cur = {"convId": self.conv_a}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "早上好"}
        self.im.mon.sends = []
        rc = self.im._wait_receipt(0, self.before, "🎉早上好", 0.2, hit=self.item)
        self.assertTrue(rc["dom"])
        self.assertTrue(rc["ok"])

    def test_dom_growth_with_unrelated_last_text_is_not_confirmed(self):
        """气泡涨了、最后一条也是自己发的，但文本明显对不上 → 不确认（不放宽判据）。"""
        self.im.cur = {"convId": self.conv_a}
        self.im.state = {"count": 11, "lastFromMe": True, "lastText": "昨天的消息"}
        self.im.mon.sends = []
        rc = self.im._wait_receipt(0, self.before, "早上好", 0.2, hit=self.item)
        self.assertIsNone(rc["dom"])
        self.assertFalse(rc["ok"])


class SendLimitWiringTests(unittest.TestCase):
    """发送侧熔断接线：`type_and_send` 必须把**回执里的**风控信号带出来。

    这条缝最容易断 —— `_wait_receipt` 收集的 receipts 若没被 `check_limit` 吃到，
    服务端回的「发送过于频繁」就永远触发不了熔断（页面上可能一个字都没有），
    而 tasks.py 只认 `rate_limited` 这个布尔量。故用假页面把整条缝钉住。
    """

    class _Kbd:
        def __init__(self):
            self.typed = []
            self.pressed = []

        def type(self, text):
            self.typed.append(text)

        def press(self, key):
            self.pressed.append(key)

    class _Editor:
        def __init__(self):
            self.clicked = 0

        def click(self):
            self.clicked += 1

    class _Page:
        def __init__(self, editor_text=None):
            self.keyboard = SendLimitWiringTests._Kbd()
            self.waited = 0
            self.editor_text = editor_text
            self.clears = 0

        def evaluate(self, expr, *a):
            if expr is douyin_im.JS_EDITOR_EMPTY:
                return True          # 没有草稿残留，别走清空分支
            if expr is douyin_im.JS_EDITOR_EXISTS:
                return True
            if expr is douyin_im.JS_EDITOR_TEXT:
                if self.editor_text is None:
                    return None
                return {"text": self.editor_text, "len": len(self.editor_text)}
            if expr is douyin_im.JS_EDITOR_CLEAR:
                self.clears += 1
                return {"ok": True, "how": "key", "left": ""}
            return None

        def wait_for_timeout(self, ms):
            self.waited += 1

    class _IM(_Stub):
        def __init__(self, receipts, dom_texts=(), editor_text=None):
            super().__init__()
            self.page = SendLimitWiringTests._Page(editor_text)
            self.mon = type("M", (), {"sends": []})()
            self.editor = SendLimitWiringTests._Editor()
            self._receipts = receipts
            self._dom_texts = list(dom_texts)
            self.limit_scans = 0
            self.send_clicks = 0

        def _input_mode(self):
            return "real"

        def _type_real(self, editor, lines, dry):
            # 这些用例钉的是回执/熔断/登录态接线，打字机制由 ReplaceTypingTests 覆盖。
            self.typed_text = "\n".join(lines)
            return True

        def _editor(self):
            return self.editor

        def _click_send(self):
            self.send_clicks += 1
            return "btn"

        # 这些用例把发送点击换成了自己的计数器（`_click_send`），
        # 所以显式声明走可信点击那条线（生产默认 False，见 PREFER_TRUSTED_CLICK）。
        PREFER_TRUSTED_CLICK = True

        def _msg_state(self):
            return {"count": 1, "lastFromMe": False, "convId": None}

        def _wait_receipt(self, *a, **kw):
            return {"ok": True, "http": {"ok": True, "code": 0, "status": "ok"},
                    "dom": None, "verdict": None, "dom_conv_off": False,
                    "receipts": self._receipts}

        def limit_texts(self):
            self.limit_scans += 1
            return list(self._dom_texts)

    def _send(self, receipts, dom_texts=(), dry_run="off", text="你好",
              editor_text=None):
        im = self._IM(receipts, dom_texts, editor_text=editor_text)
        out = im.type_and_send({"conv_id": f"0:1:{ME}:{PEER_A}", "uid": PEER_A,
                                "display": "甲同学"}, text, dry_run=dry_run)
        return im, out

    def test_receipt_rate_limit_status_aborts_with_keyword(self):
        """回执 status 里写着「发送过于频繁」→ rate_limited + 关键词一起带出。"""
        im, out = self._send([{"ok": True, "code": 0, "status": "发送过于频繁",
                               "message_id": "mid-1"}])
        self.assertTrue(out["rate_limited"])
        self.assertEqual(out["limit"]["keyword"], "发送过于频繁")
        self.assertEqual(out["limit"]["source"], "http")
        self.assertGreaterEqual(im.limit_scans, 1)      # 真的扫过，不是写死的 True
        # 即使 HTTP 回执本身是 ok 的，也要如实报告熔断（任务侧据此整轮停手）
        self.assertNotEqual(out["via"], None)

    def test_receipt_ok_and_clean_dom_is_not_rate_limited(self):
        im, out = self._send([{"ok": True, "code": 0, "status": "OK",
                               "message_id": "mid-1"}], dom_texts=["今日火花+1"])
        self.assertFalse(out["rate_limited"])
        self.assertIsNone(out["limit"])

    def test_dom_text_trips_when_receipt_is_clean(self):
        """回执干净但页面上挂着风控提示条 → 一样要熔断（两个来源都要看）。"""
        im, out = self._send([{"ok": True, "code": 0, "status": "OK"}],
                             dom_texts=["操作频繁，请稍后再试"])
        self.assertTrue(out["rate_limited"])
        self.assertEqual(out["limit"]["source"], "dom")

    def test_dry_run_never_scans_or_sends(self):
        """空跑不点发送、也不产生熔断判定（它根本没发出去）。"""
        im, out = self._send([{"ok": True, "code": 0, "status": "发送过于频繁"}],
                             dry_run="select")
        self.assertEqual(out["dry_run"], "select")
        self.assertFalse(out["rate_limited"])
        self.assertEqual(im.limit_scans, 0)
        self.assertEqual(im.page.keyboard.typed, [])          # select 连输入都不做

    def test_duplicated_editor_text_blocks_the_send(self):
        """真跑前读回发现"同一条消息两遍" → **不点发送**（发出去朋友会看到两遍）。

        现场：清空没生效 → 第二次输入时旧文本在重渲染里回来 → 编辑器里是
        "上一遍 + 这一遍"。这条防线只在有强证据时拦（见 `_residue_echo`）。
        """
        want = "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n今天的一言"
        doubled = want + "\n" + want
        im, out = self._send([{"ok": True, "code": 0, "status": "ok"}],
                             text=want, editor_text=doubled)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "editor_residue")
        self.assertEqual(out["echo"], 2)
        self.assertEqual(im.send_clicks, 0)                   # 一个字节都没发出去
        self.assertGreaterEqual(im.page.clears, 1)            # 拦下之后顺手清了残留

    def test_clean_editor_still_sends(self):
        im, out = self._send([{"ok": True, "code": 0, "status": "ok"}],
                             text="[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——",
                             editor_text="[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——")
        self.assertTrue(out["ok"])
        self.assertEqual(im.send_clicks, 1)


class ClickCredentialTests(unittest.TestCase):
    """发送凭据：`wait_receipt=False` 这条路不许把"根本没点下去"记成成功。

    审计现场（修复前 core/douyin_im.py:2711-2712）：`how = self._js_click_send()`
    在按钮找不到 / JS 抛异常时返回 None，调用方却无条件 `return dict(base, ok=True,
    via=how)` —— 一个字符都没出去也记成"已发送"，任务层当天不再重试（朋友那边火花
    照断），而账本和面板上写着成功。这里把"有凭据 / 没凭据"两条路都钉住。
    """

    class _Page:
        def __init__(self):
            self.keyboard = SendLimitWiringTests._Kbd()

        def evaluate(self, expr, *a):
            if expr is douyin_im.JS_EDITOR_EMPTY:
                return True                       # 没有草稿残留，别走清空分支
            if expr is douyin_im.JS_EDITOR_EXISTS:
                return True
            if expr is douyin_im.JS_EDITOR_TEXT:
                return {"text": "", "len": 0}
            if expr is douyin_im.JS_TYPE_LINE:
                # 合成输入要报"这一行进去了几个字"：返回 grew=0 时 `type_and_send`
                # 会以"合成输入一行都没进去"抛错，那就永远走不到点击那一步。
                return {"grew": len((a[0] or {}).get("text") or ""), "brk": "ok"}
            return None

        def wait_for_timeout(self, ms):
            pass

    class _IM(_Stub):
        def __init__(self, how):
            super().__init__()
            self.page = ClickCredentialTests._Page()
            self.mon = type("M", (), {"sends": []})()
            self.how = how

        def _input_mode(self):
            return "synth"                        # 合成输入这条路才会用 JS 点击

        def _js_click_send(self):
            return self.how

    def _send(self, how):
        im = self._IM(how)
        out = im.type_and_send({"conv_id": f"0:1:{ME}:{PEER_A}", "uid": PEER_A,
                                "display": "甲同学"}, "你好", wait_receipt=False)
        return im, out

    def test_missing_click_credential_is_not_reported_as_sent(self):
        """`_js_click_send()` 返回 None（按钮没找到 / JS 抛异常）→ ok 必须是 False。"""
        im, out = self._send(None)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["via"])
        self.assertTrue(out["reason"], "没发出去却连原因都不写，面板上只会看到一行空白")

    def test_click_credential_still_reports_success(self):
        """拿到 "js" 凭据 → 照旧算成功，via 带上凭据来源（别把好数据也一起压掉）。"""
        im, out = self._send("js")
        self.assertTrue(out["ok"])
        self.assertEqual(out["via"], "js")


class MsgStateConvIdTests(unittest.TestCase):
    """`_msg_state()` 必须带出 convId —— 回执裁决的会话归属全靠它。"""

    class _IM(_Stub):
        def __init__(self):
            super().__init__()
            self.cur = {"convId": "0:1:1:2", "title": "甲同学"}

        def _current_conv(self):
            return self.cur

    class _Page:
        def __init__(self, state):
            self.state = state

        def evaluate(self, expr, *a):
            return self.state

    def test_msg_state_carries_conv_id_and_title(self):
        im = self._IM()
        im.page = self._Page({"count": 3, "lastFromMe": True, "lastText": "嗨"})
        st = im._msg_state()
        self.assertEqual(st["count"], 3)
        self.assertEqual(st["convId"], "0:1:1:2")
        self.assertEqual(st["title"], "甲同学")

    def test_msg_state_survives_evaluate_failure(self):
        im = self._IM()

        class _Boom:
            def evaluate(self, expr, *a):
                raise RuntimeError("页面炸了")

        im.page = _Boom()
        self.assertEqual(im._msg_state(), {})


class LoginVerdictVetoTests(unittest.TestCase):
    """登录态判定不许"乐观通过"。

    线上实测到的 bug：本机 上一台**从未登录过**的机器跑 dry-run，页面明明是扫码登录框
    （body 里是"扫码登录 / 验证码登录 / 密码登录"），日志却打出
    `[LOGIN] ✅ 已登录  user_id=2117023095855936` —— 因为抖音给**匿名访客**也会发
    `odin.user_id`，而 `scrape_ssr` 只看"有没有数字 id"、从不看抖音自己写的 `isLogin`。
    结果是白等两分钟"列表就绪"再超时跳过账号。这里钉死两层否决：
      ① SSR 里 `isLogin:false` / `not_exist_login_cookie:true` 否决匿名 id；
      ② 页面上**真的画着**登录框时，DOM 否决任何"已登录"结论。
    """

    SSR_ANON = ('<script>window._ROUTER_DATA={"odin":{"user_id":"2117023095855936"},'
                '"user":{"isLogin":false,"info":{"uid":"2117023095855936"}}}</script>')
    SSR_LOGGED = ('<script>window._ROUTER_DATA={"user":{"isLogin":true,'
                  '"info":{"uid":"4245677605392163","secUid":"MS4wLjABAAAA"}}}</script>')

    class _Page:
        def __init__(self, html="", dom=None, cookie_names=()):
            self.html = html
            self.dom = dom or {}
            self.cookie_names = list(cookie_names)
            self.evaluated = []

        def content(self):
            return self.html

        def evaluate(self, expr, *a):
            self.evaluated.append(expr)
            if expr == douyin_im.JS_LOGIN_DOM:
                return self.dom
            return {}

        class _Ctx:
            def __init__(self, names):
                self.names = names

            def cookies(self):
                return [{"name": n, "value": "x"} for n in self.names]

        @property
        def context(self):
            return self._Ctx(self.cookie_names)

    # ---------------------------------------------------------------- SSR 层
    def test_anonymous_uid_with_is_login_false_is_not_logged_in(self):
        ss = douyin_im.scrape_ssr(self.SSR_ANON)
        self.assertEqual(ss["verdict"], "logged_out")
        # 匿名 id 仍然解出来了（诊断要用），但它不能把 verdict 顶成 logged_in
        self.assertEqual(ss["user_id"], "2117023095855936")

    def test_real_login_still_reports_logged_in(self):
        ss = douyin_im.scrape_ssr(self.SSR_LOGGED)
        self.assertEqual(ss["verdict"], "logged_in")
        self.assertEqual(ss["user_id"], "4245677605392163")

    def test_not_exist_login_cookie_vetoes_too(self):
        html = ('<script>window._ROUTER_DATA={"odin":{"user_id":"12345678901"},'
                '"not_exist_login_cookie":true}</script>')
        self.assertEqual(douyin_im.scrape_ssr(html)["verdict"], "logged_out")

    # ---------------------------------------------------------------- DOM 层
    def test_dom_login_box_vetoes_ssr_false_positive(self):
        page = self._Page(html=self.SSR_LOGGED,
                          dom={"loginVisible": True, "avatarCard": False,
                               "hasChatRoot": False, "itemCount": 0})
        lg = check_login(page, None)
        self.assertEqual(lg["state"], "NOT_LOGGED_IN")
        self.assertIsNone(lg["user_id"])
        self.assertTrue(lg["login_box"])

    def test_dom_login_box_with_sessionid_becomes_expired(self):
        """有 sessionid 却还看到登录框 → 是"失效"，不是"从未登录"（提示语不同）。"""
        page = self._Page(html=self.SSR_LOGGED,
                          dom={"loginVisible": True, "avatarCard": False},
                          cookie_names=["sessionid_ss"])
        self.assertEqual(check_login(page, None)["state"], "EXPIRED")

    def test_hidden_login_box_does_not_veto(self):
        """登录框还挂在 DOM 里但没渲染（display:none 等）→ 不能拿它否决已登录。"""
        page = self._Page(html=self.SSR_LOGGED,
                          dom={"loginVisible": False, "loginBox": True, "avatarCard": True})
        lg = check_login(page, None)
        self.assertEqual(lg["state"], "LOGGED_IN")
        self.assertEqual(lg["user_id"], "4245677605392163")

    def test_monitor_ssr_false_positive_is_also_vetoed(self):
        """监听器那条路径（①）同样要过 DOM 否决 —— 两条来源不能只堵一条。"""
        class _Mon:
            login = {"verdict": "logged_in", "user_id": "2117023095855936",
                     "nickname": None, "sec_uid": None}

        page = self._Page(html="", dom={"loginVisible": True, "avatarCard": False})
        self.assertEqual(check_login(page, _Mon())["state"], "NOT_LOGGED_IN")

    def test_login_dom_expression_checks_visibility_not_just_existence(self):
        """JS 里必须真的量过盒子（宽高 / display / visibility / opacity）。"""
        js = douyin_im.JS_LOGIN_DOM
        for token in ("getBoundingClientRect", "getComputedStyle", "loginBox", "avatarCard"):
            self.assertIn(token, js)


class IdentitySelectionTests(unittest.TestCase):
    """勾选来的目标（带 uid/conv_id）在真实滚动流程里只按 id 命中。

    这是「主动匹配」的关键一步：面板勾选时记下 uid/conv_id，之后名字改了、
    或者列表里出现同名的人，都不会把消息发给别人。
    """

    def setUp(self):
        self.s = _VListStub(total=50)
        self.s._select_and_verify = lambda item: True   # 选中判定本身另有测试

    def _conv(self, i):
        return f"0:1:{ME}:{2000 + i}"

    def test_conv_id_target_hits_the_right_person(self):
        got = list(self.s.iter_find_and_select(
            [{"name": "名字随便写", "conv_id": self._conv(5)}]))
        self.assertEqual([i["conv_id"] for i in got], [self._conv(5)])
        self.assertEqual(got[0]["matched_by"], "conv_id")

    def test_uid_target_hits_by_uid(self):
        got = list(self.s.iter_find_and_select([{"name": "随便", "uid": "2007"}]))
        self.assertEqual([i["conv_id"] for i in got], [self._conv(7)])
        self.assertEqual(got[0]["matched_by"], "uid")

    def test_name_target_still_matches_by_name(self):
        """旧配置（手写名字）必须继续能用。"""
        got = list(self.s.iter_find_and_select(["U9"]))
        self.assertEqual([i["display"] for i in got], ["U9"])

    def test_identity_target_that_is_gone_is_reported_missing(self):
        got = list(self.s.iter_find_and_select(
            [{"name": "早就不在了", "conv_id": f"0:1:{ME}:9999"}]))
        self.assertEqual(got, [])
        self.assertIn("早就不在了", self.s.last_scan["missing"])

    def test_identity_target_is_not_stolen_by_a_namesake(self):
        """列表里有人叫「U3」，但勾选的是 conv_id 指向的另一个人 → 不许发给他。"""
        got = list(self.s.iter_find_and_select(
            [{"name": "U3", "conv_id": self._conv(11)}]))
        self.assertEqual([i["conv_id"] for i in got], [self._conv(11)])
        self.assertEqual(got[0]["matched_by"], "conv_id")

    def test_two_identity_targets_both_found(self):
        got = list(self.s.iter_find_and_select([
            {"name": "甲", "uid": "2001"},
            {"name": "乙", "conv_id": self._conv(40)},
        ]))
        self.assertEqual(sorted(i["conv_id"] for i in got),
                         sorted([self._conv(1), self._conv(40)]))


class KickDecisionTests(unittest.TestCase):
    """`{"decision": "KICK"}` 是"这个会话作废了"，不是"这一条没发出去"。

    2026-10-20 的真实事故：两个账号到点时首条发送的响应就是一句
    `{"decision": "KICK"}`（不是 protobuf 信封）。旧代码只解出
    `code=None status=""` → 按普通发送失败重试 1/1、后续每条白等 20 秒超时，
    整天一条都没送出去，面板上还写着"已发"。这里钉住三层：
    解码层认得出、监视器置"登录已死"并回调、发起来不复检也不重试。
    """

    class _Page(object):
        def on(self, *a, **kw):
            pass

    def test_kick_body_is_decoded_as_kicked(self):
        r = douyin_im.decode_send_resp(b'{"decision": "KICK"}')
        self.assertFalse(r["ok"])
        self.assertTrue(r["kicked"])
        self.assertEqual(r["decision"], "KICK")

    def test_ordinary_envelope_is_not_a_kick(self):
        for body in (b"", b'{"decision": "OK"}'):
            r = douyin_im.decode_send_resp(body)
            self.assertFalse(r["kicked"], f"body={body!r} 被误判成被踢")

    def test_monitor_marks_login_dead_and_emits_login_lost(self):
        class _Mon(douyin_im.ImMonitor):
            # `_emit_login_lost` 在 DouyinIM 上（生产里 DouyinIM 继承 ImMonitor）。
            # 这里只借它的实现，不必为了一个回调把整台浏览器对象造出来。
            _emit_login_lost = douyin_im.DouyinIM._emit_login_lost

        mon = _Mon(self._Page())
        mon._login_dead = False
        mon._login_dead_reason = ""
        seen = []
        mon._login_lost = [lambda info: seen.append(info)]
        with self.assertLogs("douyin_im", level="ERROR") as cm:
            mon._handle_send(b'{"decision": "KICK"}')
        self.assertTrue(mon._login_dead)
        self.assertEqual(mon._login_dead_reason, "KICK:KICK")
        self.assertEqual(len(seen), 1)
        self.assertIn("KICK", seen[0]["reason"])
        self.assertTrue(any("踢下线" in line for line in cm.output))

    def test_monitor_alone_survives_a_kick(self):
        """生产里 DouyinIM 把 `_emit_login_lost` 绑到监视器上；但监视器自己单跑
        （或绑定还没发生）时也必须扛得住 —— 以前这里 AttributeError 被
        `_on_response` 的 except 吞掉，连紧随其后的 [SEND] 记账日志都不打。"""
        mon = douyin_im.ImMonitor(self._Page())
        mon._login_lost = []
        with self.assertLogs("douyin_im", level="ERROR") as cm:
            mon._handle_send(b'{"decision": "KICK"}')
        self.assertTrue(mon._login_dead)
        self.assertEqual(mon._login_dead_reason, "KICK:KICK")
        self.assertTrue(any("踢下线" in line for line in cm.output))

    def test_douyin_im_hands_its_emitter_to_the_monitor(self):
        im = douyin_im.DouyinIM.__new__(douyin_im.DouyinIM)
        im._login_dead = False
        im._login_dead_reason = ""
        im._login_lost = []
        with self.assertLogs("douyin_im", level="ERROR"):
            im._emit_login_lost("kicked", "KICK:KICK", "")
        self.assertTrue(im._login_dead, "DouyinIM 这边也要置死，后续发送才会短路")
        source = (REPO_ROOT / "core" / "douyin_im.py").read_text(encoding="utf-8")
        self.assertIn("self.mon._emit_login_lost = self._emit_login_lost", source)

    def test_kicked_send_is_reported_as_session_kicked(self):
        """复用 LoginRecheckTests 的替身：回执被换成"被踢"这一种。"""
        im = LoginRecheckTests._IM(receipt_ok=False, login_state="LOGGED_IN")
        im._wait_receipt = lambda *a, **kw: {
            "ok": False, "http": None, "dom": None, "verdict": None,
            "dom_conv_off": False, "receipts": [],
            "kicked": True, "kick_decision": "KICK",
        }
        out = im.type_and_send({"conv_id": f"0:1:{ME}:{PEER_A}", "uid": PEER_A,
                                "display": "甲同学"}, "你好", dry_run="off")
        self.assertFalse(out["ok"])
        self.assertTrue(out["kicked"])
        self.assertTrue(out["login_lost"])
        self.assertEqual(out["reason"], "session_kicked")
        self.assertEqual(out["kick_decision"], "KICK")
        self.assertTrue(im._login_dead)
        self.assertEqual(im.rechecks, 0, "会话都被踢了，再去复检登录态没有意义")
        self.assertEqual(im.send_clicks, 1, "被踢之后不该再点第二次发送")

class LoginRecheckTests(unittest.TestCase):
    """「发送拿不到回执」与「登录已失效」必须分开报。

    2026-10-09 示例号 的真实事故：网页登录态失效之后页面**仍然长着 IM 的样子**，
    点击发送照样"成功"、回执永远不来，4 个目标被记成「发送失败」而整轮按 ok 记账
    —— 面板上一切正常，人却在等消息，而且当天再也不会重试。这里把接线钉住：
    发送失败要复检登录态，复检出掉登录要（1）带 `login_lost` 出来，
    （2）本轮剩下的目标直接跳过，不再每个白等 20 秒。
    """

    class _IM(_Stub):
        def __init__(self, receipt_ok=False, login_state="EXPIRED"):
            super().__init__()
            self.page = SendLimitWiringTests._Page()
            self.mon = type("M", (), {"sends": []})()
            self.editor = SendLimitWiringTests._Editor()
            self._receipt_ok = receipt_ok
            self._login_state = login_state
            self.send_clicks = 0
            self.rechecks = 0
            self._login_dead = False
            self._login_dead_reason = ""
            self._login_lost = []

        def _input_mode(self):
            return "real"

        def _type_real(self, editor, lines, dry):
            # 这些用例钉的是回执/熔断/登录态接线，打字机制由 ReplaceTypingTests 覆盖。
            self.typed_text = "\n".join(lines)
            return True

        def _editor(self):
            return self.editor

        def _click_send(self):
            self.send_clicks += 1
            return "btn"

        # 这些用例把发送点击换成了自己的计数器（`_click_send`），
        # 所以显式声明走可信点击那条线（生产默认 False，见 PREFER_TRUSTED_CLICK）。
        PREFER_TRUSTED_CLICK = True

        def _msg_state(self):
            return {"count": 1, "lastFromMe": False, "convId": None}

        def _wait_receipt(self, *a, **kw):
            return {"ok": self._receipt_ok, "http": None, "dom": None,
                    "verdict": None, "dom_conv_off": False, "receipts": []}

        def limit_texts(self):
            return []

        def recheck_login(self):
            """替身：只决定"复检结论"，真正的 check_login 另有单测。"""
            self.rechecks += 1
            if self._login_state != "LOGGED_IN":
                self._login_dead = True
                self._login_dead_reason = self._login_state
            return {"state": self._login_state}

    def _send(self, im, text="你好"):
        return im.type_and_send({"conv_id": f"0:1:{ME}:{PEER_A}", "uid": PEER_A,
                                 "display": "甲同学"}, text, dry_run="off")

    def test_failed_receipt_with_dead_login_reports_login_lost(self):
        im = self._IM(receipt_ok=False, login_state="EXPIRED")
        out = self._send(im)
        self.assertFalse(out["ok"])
        self.assertTrue(out["login_lost"])
        self.assertEqual(out["reason"], "login_lost")
        self.assertEqual(out["login_state"], "EXPIRED")
        self.assertEqual(im.rechecks, 1)

    def test_failed_receipt_with_live_login_stays_a_plain_failure(self):
        im = self._IM(receipt_ok=False, login_state="LOGGED_IN")
        out = self._send(im)
        self.assertFalse(out["ok"])
        self.assertFalse(out.get("login_lost"))
        self.assertNotEqual(out["reason"], "login_lost")

    def test_next_target_is_skipped_once_login_is_known_dead(self):
        im = self._IM(receipt_ok=False, login_state="NOT_LOGGED_IN")
        first = self._send(im)
        self.assertTrue(first["login_lost"])
        clicks_after_first = im.send_clicks
        second = self._send(im, text="第二条")
        self.assertTrue(second["login_lost"])
        self.assertEqual(im.send_clicks, clicks_after_first)   # 第二次连点都没点
        self.assertEqual(im.rechecks, 1)                      # 也没再复检

    def test_recheck_login_sets_flag_and_emits_callback(self):
        """真 `recheck_login`：结论不是 LOGGED_IN 就置位 + 触发掉登录回调。"""
        im = self._IM(receipt_ok=False)
        im._login_dead = False
        seen = []
        im._login_lost = [lambda info: seen.append(info)]
        with mock.patch.object(douyin_im, "check_login",
                               return_value={"state": "EXPIRED", "log": "登录已失效"}):
            lg = douyin_im.DouyinIM.recheck_login(im)
        self.assertEqual(lg["state"], "EXPIRED")
        self.assertTrue(im._login_dead)
        self.assertEqual(im._login_dead_reason, "EXPIRED")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["reason"], "发送失败后复检登录态 = EXPIRED")

    def test_recheck_login_keeps_working_when_login_is_fine(self):
        im = self._IM(receipt_ok=False)
        im._login_dead = False
        with mock.patch.object(douyin_im, "check_login",
                               return_value={"state": "LOGGED_IN"}):
            douyin_im.DouyinIM.recheck_login(im)
        self.assertFalse(im._login_dead)
        self.assertEqual(im._login_dead_reason, "")


class ListTimeoutLoginRecheckTests(unittest.TestCase):
    """列表等不到时先复检登录态：登录失效要报「登录已失效」，不是「等待超时」。

    2026-10-09 示例号：网页登录态失效后，2026-10 新版登录弹窗（没有 `data-e2e`，
    只有 `.douyin_login_new_class` / `#douyin_login_comp_*`）画在页面上，
    旧选择器一个都没命中 → 门禁判成 TIMEOUT。而面板上"超时"没法告诉用户
    该去重新扫码，只有"登录已失效"才指向唯一的出路。
    """

    class _IM(_Stub):
        def __init__(self, list_result):
            super().__init__()
            self.page = object()
            self.mon = None
            self.ready_timeout = 0.01
            self._callbacks = []
            self._login_lost = []
            self._list_result = list_result

        def _wait_list_ready(self, lg):
            return dict(self._list_result)

    @staticmethod
    def _login(state):
        return {"state": state, "user_id": None, "nickname": None,
                "sec_uid": None, "log": f"登录态={state}"}

    def _run(self, list_result, first_state="LOGGED_IN"):
        im = self._IM(list_result)
        with mock.patch.object(douyin_im, "check_login",
                               return_value=self._login(first_state)):
            return im._run_preflight()

    def test_expired_wins_over_timeout(self):
        st = self._run({"ready": False,
                        "login_recheck": self._login("EXPIRED")})
        self.assertEqual(st["status"], "EXPIRED")
        self.assertEqual(st["login_state"], "EXPIRED")

    def test_not_logged_in_wins_over_timeout(self):
        st = self._run({"ready": False,
                        "login_recheck": self._login("NOT_LOGGED_IN")})
        self.assertEqual(st["status"], "LOGGED_OUT")

    def test_plain_timeout_is_still_timeout(self):
        """复检说还在登录 → 老老实实报超时，不许把超时说成掉登录。"""
        st = self._run({"ready": False,
                        "login_recheck": self._login("LOGGED_IN")})
        self.assertEqual(st["status"], "TIMEOUT")

    def test_missing_recheck_keeps_old_behaviour(self):
        st = self._run({"ready": False})
        self.assertEqual(st["status"], "TIMEOUT")


class NewLoginDialogDetectionTests(unittest.TestCase):
    """新版登录弹窗必须算登录框 —— 它的选择器不认识，门禁就会报"超时"。"""

    class _Page:
        def __init__(self, dom, cookie_names=("sessionid_ss",)):
            self.dom = dom
            self.cookie_names = list(cookie_names)

        def content(self):
            return ""

        def evaluate(self, expr, *a):
            return self.dom if expr == douyin_im.JS_LOGIN_DOM else {}

        class _Ctx:
            def __init__(self, names):
                self.names = names

            def cookies(self):
                return [{"name": n, "value": "x"} for n in self.names]

        @property
        def context(self):
            return self._Ctx(self.cookie_names)

    def test_new_dialog_is_expired_when_sessionid_present(self):
        lg = douyin_im.check_login(self._Page({"loginNew": True}), None)
        self.assertEqual(lg["state"], "EXPIRED")
        self.assertTrue(lg["login_box"])

    def test_new_dialog_without_cookie_is_not_logged_in(self):
        page = self._Page({"loginNew": True}, cookie_names=())
        self.assertEqual(douyin_im.check_login(page, None)["state"], "NOT_LOGGED_IN")

    def test_js_expression_knows_the_new_dialog(self):
        js = douyin_im.JS_LOGIN_DOM
        for token in ("douyin_login_new_class", "douyin_login_comp_mobile_code",
                      "loginNew", "loginWords"):
            self.assertIn(token, js)


class _ProbePage:
    """`_input_mode` / `_select_all` / `_type_real` / `_type_synth` 的最小页面替身。

    形状由 2026-10-15 的真机取证决定：
      * `insert_text`（CDP `Input.insertText`）是可信输入里**唯一可靠**的那条 ——
        `page.keyboard.type` 打 ASCII 一个字都进不去（页面收不到按键事件）；
      * 清空不可信，所以新设计根本不走"清空"这条路，**全选 + 插入**才是覆盖。

    `trusted_text` 模拟可信输入打完之后编辑器里剩下什么：
      * `"same"`   —— 精确落地（全选生效，插入即覆盖）；
      * `"append"` —— 选区没生效、插入变成追加（老毛病：叠字）；
      * 其它字符串 —— 直接指定残留内容（模拟掉字现场，如 `<>` 没进去）。
    `synth_lands` 决定 JS 覆盖输入（`JS_REPLACE_TEXT`）能不能打进去；
    `js_sel_in` 决定它内部那次原生全选有没有落在编辑器里（真机上只有落在编辑器里
    的**原生**选区才管用，元素级选区配 execCommand 是 no-op —— 见 `JsReplaceTests`）。
    """

    def __init__(self, events=(), *, editor=True, text="", trusted_text="same",
                 synth_lands=True, backspace_works=True, clear_script=("key", "key"),
                 js_sel_in=True, select_all_ok=True, boom=None):
        self.events = list(events)
        self.editor = editor
        self.text = text
        self.trusted_text = trusted_text
        self.synth_lands = synth_lands
        self.js_sel_in = js_sel_in       # 原生全选有没有落在编辑器里
        self.select_all_ok = select_all_ok   # JS_SELECT_ALL 这条通道本身通不通
        self.backspace_works = backspace_works
        self.script = list(clear_script)
        self.boom = boom
        self.evals = []
        self.moves = []
        self.typed = []            # ("cdp-insert"|"cdp-type"|"synth", 文本)
        self.js_replaces = []      # JS 覆盖输入打进去的文本
        self.presses = []
        self.selected_text = None
        self._has_selection = False   # 编辑器里有没有活动选区（分段输入的第一块靠它替换）
        self.clicked = 0
        self.keyboard = self       # page.keyboard.insert_text / type / press
        self.mouse = self          # page.mouse.move

    # —— CDP 鼠标 / 键盘 ——
    def move(self, x, y):
        self.moves.append((x, y))

    def insert_text(self, s):
        """CDP 可信输入：真机实测这条可靠（全选生效时是覆盖）。

        分段输入（`TYPING_CHUNK_CHARS`）会连着调好几次：**只有第一块**替换选区，后面的
        块接在光标后面 —— 这里按 `self._has_selection` 如实模拟，否则替身会比真机宽容。
        """
        self.typed.append(("cdp-insert", s))
        if self.trusted_text == "same":
            self.text = s if self._has_selection else self.text + s
            self._has_selection = False
        elif self.trusted_text == "append":
            self.text += s
        else:
            self.text = self.trusted_text

    def type(self, s):
        """老路径：真机上 ASCII 一个字都进不去，这里如实模拟（不落地）。"""
        self.typed.append(("cdp-type", s))

    def press(self, key):
        self.presses.append(key)
        if key == "Backspace" and self.backspace_works and self.text:
            self.text = self.text[:-1]
        elif key == "Shift+Enter":
            self.text += "\n"

    def click(self, **kw):
        self.clicked += 1
        self.click_kw = dict(kw)

    def wait_for_timeout(self, ms):
        return None

    def evaluate(self, expr, arg=None):
        self.evals.append(expr)
        if self.boom is not None:
            raise self.boom
        if expr is douyin_im.DouyinIM.JS_INPUT_PROBE:
            return "hooked"
        if isinstance(expr, str) and "__improbe" in expr:
            if "splice" in expr:
                out, self.events = list(self.events), []
                return out
            return None
        if expr is douyin_im.JS_EDITOR_EXISTS:
            return self.editor
        if expr is douyin_im.JS_EDITOR_TEXT:
            if not self.editor:
                return None
            return {"text": self.text, "len": len(self.text)}
        if expr is douyin_im.JS_EDITOR_EMPTY:
            return self.text.strip() == ""
        if expr is douyin_im.JS_EDITOR_CLEAR:
            act = self.script.pop(0) if self.script else "lie"
            if act == "key":
                self.text = ""
                return {"ok": True, "how": "key", "left": ""}
            if act == "already":
                return {"ok": True, "how": "already-empty", "left": ""}
            if act == "none":
                return {"ok": False, "how": "none", "left": self.text[:20]}
            return {"ok": True, "how": "key", "left": ""}   # lie：说清好了，其实没清
        if expr is douyin_im.JS_SELECT_ALL:
            if not self.editor or not self.select_all_ok:
                return None
            self.selected_text = self.text
            self._has_selection = True
            return self.text
        if expr is douyin_im.JS_REPLACE_TEXT:
            text = (arg or {}).get("text") or ""
            self.js_replaces.append(text)
            if self.synth_lands:
                self.text = text          # 选区被替换 = 覆盖，不是追加
            return {"ok": bool(self.synth_lands), "sel_in": self.js_sel_in,
                    "before": 0, "after": len(text)}
        if expr is douyin_im.JS_TYPE_LINE:
            if not self.editor:
                return {"ins": False, "grew": 0, "brk": "none"}
            self.typed.append(("synth", (arg or {}).get("text")))
            if arg and arg.get("text"):
                self.text += arg["text"]
            return {"ins": True, "grew": 1, "brk": "ins"}
        return None


class _TypingIM:
    """借用真方法的最小宿主（与 `_ClearIM` 同款：不构造 DouyinIM）。"""

    JS_INPUT_PROBE = douyin_im.DouyinIM.JS_INPUT_PROBE
    PREFER_TRUSTED_CLICK = douyin_im.DouyinIM.PREFER_TRUSTED_CLICK
    _input_mode = douyin_im.DouyinIM._input_mode
    _log_input_evidence = douyin_im.DouyinIM._log_input_evidence
    _select_all = douyin_im.DouyinIM._select_all
    _typing_landed = douyin_im.DouyinIM._typing_landed
    _type_synth = douyin_im.DouyinIM._type_synth
    _type_real = douyin_im.DouyinIM._type_real
    _try_trusted_typing = douyin_im.DouyinIM._try_trusted_typing
    _type_js_replace = douyin_im.DouyinIM._type_js_replace
    EDITOR_CLICK_TIMEOUT_MS = douyin_im.DouyinIM.EDITOR_CLICK_TIMEOUT_MS
    READBACK_ATTEMPTS = douyin_im.DouyinIM.READBACK_ATTEMPTS
    READBACK_SETTLE_MS = douyin_im.DouyinIM.READBACK_SETTLE_MS
    _editor_probe = douyin_im.DouyinIM._editor_probe
    _clear_editor = douyin_im.DouyinIM._clear_editor
    _insert_text_in_chunks = douyin_im.DouyinIM._insert_text_in_chunks
    _typing_chunk_settings = douyin_im.DouyinIM._typing_chunk_settings
    warmup_browse = douyin_im.DouyinIM.warmup_browse
    _snooze = douyin_im.DouyinIM._snooze
    _scroll_probe = douyin_im.DouyinIM._scroll_probe
    _scroll_to = douyin_im.DouyinIM._scroll_to
    TYPING_CHUNK_CHARS = douyin_im.DouyinIM.TYPING_CHUNK_CHARS
    TYPING_CHUNK_DELAY_MS = douyin_im.DouyinIM.TYPING_CHUNK_DELAY_MS
    WARMUP_BROWSE_MAX_SECONDS = douyin_im.DouyinIM.WARMUP_BROWSE_MAX_SECONDS

    def __init__(self, page, mode=None):
        self.page = page
        self._editor_dirty = True
        self._input_mode_cache = mode

    def _editor(self):
        """`_type_real` 重试前会**重新取一次**编辑器（上一轮的节点可能已被 React 换掉）。"""
        return self.page if getattr(self.page, "editor", True) else None


class OptimisticInputModeTests(unittest.TestCase):
    """输入方式不再"先探测再决定"（2026-10-15 真机推翻了事件探针这条路）。

    老版拿"页面收不收可信事件"当判据：真机上 CDP 的鼠标/按键事件根本到不了页面，
    于是永远判 synth —— 而生产一直在走 JS 合成输入（`isTrusted=false`）。现在改成
    **乐观先试可信输入**，由落地校验决定成败并缓存到本进程；事件探针只当日志证据。
    """

    def test_defaults_to_real_without_a_probe(self):
        self.assertEqual(_TypingIM(_ProbePage())._input_mode(), "real")

    def test_cached_mode_wins(self):
        self.assertEqual(_TypingIM(_ProbePage(), mode="synth")._input_mode(), "synth")
        self.assertEqual(_TypingIM(_ProbePage(), mode="real")._input_mode(), "real")

    def test_evidence_is_logged_once_through_cdp_moves(self):
        page = _ProbePage([{"t": "mousemove", "trusted": True}])
        im = _TypingIM(page)
        im._input_mode()
        self.assertTrue(im._input_evidence_logged)
        self.assertEqual(page.moves, [(2, 2), (6, 6)])
        page.moves = []
        im._input_mode()
        self.assertEqual(page.moves, [])          # 第二次不再探

    def test_evidence_failure_does_not_change_the_mode(self):
        im = _TypingIM(_ProbePage(boom=RuntimeError("页面没了")))
        self.assertEqual(im._input_mode(), "real")   # 探针炸了也照样先试可信输入

    def test_probe_result_never_decides_the_mode(self):
        """源码级守卫：`_input_mode` 的代码里不许再出现 synth 判定。"""
        src = Path(douyin_im.__file__).read_text(encoding="utf-8")
        body = src.split("def _input_mode(self):", 1)[1].split("def _log_input_evidence", 1)[0]
        code = body.split('"""')[2]              # 去掉 docstring，只看代码
        self.assertIn('return "real"', code)
        self.assertNotIn("synth", code)

    def test_hook_still_watches_move_events(self):
        js = douyin_im.DouyinIM.JS_INPUT_PROBE
        for token in ("pointermove", "mousemove", "isTrusted"):
            self.assertIn(token, js)
        self.assertNotIn("__imev", js)          # 不污染选中链路的点击诊断


class SelectAllTests(unittest.TestCase):
    """全选是"覆盖式重打"的地基：只动选区、不插字。"""

    def test_select_all_reports_the_selected_text(self):
        page = _ProbePage(text="旧文本")
        self.assertTrue(_TypingIM(page)._select_all())
        self.assertEqual(page.selected_text, "旧文本")
        self.assertEqual(page.text, "旧文本")     # 只选中，没动内容

    def test_select_all_without_editor_is_false(self):
        self.assertFalse(_TypingIM(_ProbePage(editor=False))._select_all())

    def test_select_all_exception_is_false(self):
        self.assertFalse(_TypingIM(_ProbePage(boom=RuntimeError("页面没了")))._select_all())


class TypingLandedTests(unittest.TestCase):
    """落地校验是**精确比较**：真机上可信输入会掉字（`<`/`>` 就是进不去）。"""

    LINES = ["续火花˵>ㅿ<˵"]

    def test_exact_match_lands(self):
        page = _ProbePage(text=self.LINES[0])
        self.assertTrue(_TypingIM(page)._typing_landed(self.LINES))

    def test_partial_text_is_not_landed(self):
        page = _ProbePage(text="续火花˵ㅿ˵")          # 真机实测：<> 掉了
        self.assertFalse(_TypingIM(page)._typing_landed(self.LINES))

    def test_extra_text_is_not_landed(self):
        """多出来也算不一致 —— 那是"上一条没清干净"的叠字现场。"""
        page = _ProbePage(text="续火花续火花˵ㅿ")
        self.assertFalse(_TypingIM(page)._typing_landed(self.LINES))

    def test_whitespace_differences_are_ignored(self):
        page = _ProbePage(text="续火花˵>\nㅿ<˵")
        self.assertTrue(_TypingIM(page)._typing_landed(self.LINES))

    def test_unreadable_editor_is_not_a_disproof(self):
        """编辑器在、只是读不到内容 —— 不算反证（与 `_residue_echo` 同一取舍）。"""
        page = _NoReadbackPage()
        self.assertTrue(_TypingIM(page)._typing_landed(self.LINES))

    def test_a_missing_editor_is_a_disproof(self):
        """编辑器**确定不存在**是反证：字不可能在里面，不能算"落地"。"""
        page = _ProbePage(editor=False)
        self.assertFalse(_TypingIM(page)._typing_landed(self.LINES))


class _FastClock:
    """把 `_type_synth` 的"等编辑器就绪 10 秒"压缩成瞬时（测试不睡真实时间）。"""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        self.t += 1.0
        return self.t


class _NoReadbackPage(_ProbePage):
    """编辑器存在、但内容读不回来的页面（`innerText` 取不到那种退化）。"""

    def evaluate(self, expr, arg=None):
        if expr is douyin_im.JS_EDITOR_TEXT:
            return None
        return super().evaluate(expr, arg)


class _StalePage(_ProbePage):
    """读回"迟到"的页面：前几次读到的还是旧内容，之后才是真内容。

    真机上 `insert_text` 之后编辑器的 state 未必立刻重渲染完 —— 一次读回就判死会把
    "其实打进去了"误判成没落地（白退一次、甚至白拒一条）。
    """

    def __init__(self, *a, stale=(), **kw):
        super().__init__(*a, **kw)
        self.stale = list(stale)
        self.text_reads = 0

    def evaluate(self, expr, arg=None):
        if expr is douyin_im.JS_EDITOR_TEXT:
            self.text_reads += 1
            if self.stale:
                return {"text": self.stale.pop(0), "len": 0}
        return super().evaluate(expr, arg)


class _TruncatingPage(_ProbePage):
    """`JS_TYPE_LINE` 只打进去一部分的页面（模拟"掉字"：读回里没有完整期望文本）。"""

    def evaluate(self, expr, arg=None):
        if expr is douyin_im.JS_TYPE_LINE:
            text = (arg or {}).get("text") or ""
            part = text[:2]
            self.text += part
            self.typed.append(("synth", text))
            return {"ins": True, "grew": len(part), "brk": "ins"}
        return super().evaluate(expr, arg)


class ReplaceTypingTests(unittest.TestCase):
    """可信输入 = 全选 + `insert_text` 覆盖；打不进去就用 JS 覆盖重打；都不成就拒发。

    ★ 为什么不是"清空 + 重打"：真机实测这个编辑器（字节 EditorKit）的清空不可信 ——
    `_clear_editor` 判"已清空"、读回也是空的，下一次输入旧文本又从编辑器自己的 state
    里冒出来，退回重打会叠成 `续火花续火花˵ㅿ`。全选 + 插入是**替换**，不会叠。
    """

    LINES = ["续火花˵>ㅿ<˵"]

    def test_trusted_insert_that_lands_is_kept(self):
        page = _ProbePage()
        im = _TypingIM(page, mode="real")
        # 关掉分段输入：这条断言的是"覆盖语义"，切块本身由 ChunkedTypingTests 负责
        with mock.patch.object(douyin_im, "get_config", lambda: {"typingChunkChars": 0}):
            self.assertTrue(im._type_real(page, self.LINES, "none"))
        self.assertEqual(page.typed, [("cdp-insert", self.LINES[0])])
        self.assertEqual(page.text, self.LINES[0])
        self.assertEqual(im._input_mode_cache, "real")     # 没有退回
        self.assertEqual(page.js_replaces, [])             # 没走兜底

    def test_trusted_insert_replaces_a_dirty_editor(self):
        """编辑器里有脏字时也必须只剩期望消息（覆盖，不是追加）。"""
        page = _ProbePage(text="ABC<>xyz")
        im = _TypingIM(page, mode="real")
        self.assertTrue(im._type_real(page, self.LINES, "none"))
        self.assertEqual(page.text, self.LINES[0])

    def test_partial_landing_falls_back_to_js_replace(self):
        page = _ProbePage(trusted_text="续火花˵ㅿ˵")        # <> 掉了
        im = _TypingIM(page, mode="real")
        self.assertTrue(im._type_real(page, self.LINES, "none"))
        self.assertEqual(im._input_mode_cache, "jsreplace")  # 后续目标不再试错
        self.assertEqual(page.js_replaces, [self.LINES[0]])
        self.assertEqual(page.text, self.LINES[0])         # 覆盖，不是叠字

    def test_appending_trusted_insert_is_corrected_by_the_js_path(self):
        """真机老毛病：选区没生效、可信插入变成追加 → JS 覆盖必须把它救回来。"""
        page = _ProbePage(text="ABC<>xyz", trusted_text="append")
        im = _TypingIM(page, mode="real")
        self.assertTrue(im._type_real(page, self.LINES, "none"))
        self.assertEqual(page.text, self.LINES[0])

    def test_both_paths_failing_refuses_to_send(self):
        page = _ProbePage(trusted_text="续", synth_lands=False)
        im = _TypingIM(page, mode="real")
        with self.assertRaises(RuntimeError) as ctx:
            im._type_real(page, self.LINES, "none")
        self.assertIn("拒绝发送", str(ctx.exception))
        self.assertNotEqual(page.text, self.LINES[0])

    def test_second_trusted_attempt_saves_the_message(self):
        """第一次没落地时**必须重试可信输入**，别一步退到不可靠的 JS 通道。"""
        page = _FlakyPage(fail_times=1)
        im = _TypingIM(page, mode="real")
        # 同上：这里数的是"重试了几次"，切块会让一次重试变成多次 insert。
        with mock.patch.object(douyin_im, "get_config", lambda: {"typingChunkChars": 0}):
            self.assertTrue(im._type_real(page, self.LINES, "none"))
        self.assertEqual(page.text, self.LINES[0])
        self.assertEqual(im._input_mode_cache, "real")     # 没退到 JS 通道
        self.assertEqual(page.js_replaces, [])
        self.assertEqual(page.insert_calls, 2)             # 真的重试过一次
        self.assertGreaterEqual(page.clicked, 2)           # 重试时重新点了编辑器

    def test_editor_click_timeout_is_actually_passed(self):
        """回归：类常量在方法体里必须写 `self.`。

        裸写 `EDITOR_CLICK_TIMEOUT_MS` 会变成全局名查找 → `NameError` → 被
        `except Exception` 吞掉 —— 现场表现是「点了编辑器、但超时根本没传下去」
        （真机上就是这么错过一次：点击一直用上下文默认的几十秒超时）。
        """
        page = _FlakyPage()
        im = _TypingIM(page, mode="real")
        im._try_trusted_typing(page, self.LINES)
        self.assertEqual(page.click_kw,
                         {"timeout": douyin_im.DouyinIM.EDITOR_CLICK_TIMEOUT_MS})

    def test_retry_falls_through_when_the_editor_is_gone(self):
        """重试前要重新取编辑器；取不到就跳过重试，直接走 JS 覆盖（不能崩）。"""
        page = _FlakyPage(fail_times=99)
        im = _TypingIM(page, mode="real")
        im._editor = lambda: None
        self.assertTrue(im._type_real(page, self.LINES, "none"))
        self.assertEqual(im._input_mode_cache, "jsreplace")
        self.assertEqual(page.text, self.LINES[0])

    def test_no_editor_refuses_to_send(self):
        page = _ProbePage(editor=False)
        im = _TypingIM(page, mode="real")
        with self.assertRaises(RuntimeError):
            im._type_real(page, self.LINES, "none")

    def test_synth_zero_typing_refuses_to_send(self):
        page = _ProbePage(editor=False)
        im = _TypingIM(page, mode="real")
        with mock.patch.object(douyin_im, "time", _FastClock()):
            with self.assertRaises(RuntimeError) as ctx:
                im._type_synth(self.LINES, "none")
        self.assertIn("一行都没进去", str(ctx.exception))

    def test_synth_zero_typing_in_dry_run_only_warns(self):
        page = _ProbePage(editor=False)
        im = _TypingIM(page, mode="real")
        with mock.patch.object(douyin_im, "time", _FastClock()):
            im._type_synth(self.LINES, douyin_im.DRY_RUN_FULL)  # 不抛


class _FlakyPage(_ProbePage):
    """可信输入**第一次不落地、第二次才落地**的页面。

    真机上"没落地"多半是焦点/选区还没生效（刚切过来的会话、上一轮的选区还挂在旧
    节点上）—— 重新点一次编辑器再来一遍就好了。这条替身守的就是那层重试：没有它，
    一次抖动就会白退到 JS 覆盖通道，而那条通道在本编辑器上**不可靠**（真机取证见
    `JS_REPLACE_TEXT` 的注释），最后只能拒发 = 漏发一条。
    """

    def __init__(self, *a, fail_times=1, **kw):
        super().__init__(*a, **kw)
        self.fail_times = fail_times
        self.insert_calls = 0

    def insert_text(self, s):
        self.insert_calls += 1
        if self.insert_calls <= self.fail_times:
            self.typed.append(("cdp-insert", s))      # 记一笔，但不落地
            return
        super().insert_text(s)


class SameTextTests(unittest.TestCase):
    """落地判据 `_same_text`：逐行比，退化时才整体折叠比。"""

    LINES = ["续火花˵>ㅿ<˵"]

    def test_exact_is_true(self):
        self.assertTrue(douyin_im._same_text(self.LINES[0], self.LINES))

    def test_line_break_shape_is_tolerated(self):
        self.assertTrue(douyin_im._same_text("续火花˵>\nㅿ<˵", self.LINES))

    def test_partial_is_false(self):
        self.assertFalse(douyin_im._same_text("续火花˵ㅿ˵", self.LINES))

    def test_duplicated_is_false(self):
        self.assertFalse(douyin_im._same_text("续火花续火花˵ㅿ", self.LINES))

    def test_multi_line_must_keep_every_line(self):
        lines = ["第一行", "第二行"]
        self.assertTrue(douyin_im._same_text("第一行\n第二行", lines))
        self.assertFalse(douyin_im._same_text("第一行", lines))


class ReadbackRetryTests(unittest.TestCase):
    """读回要重试几拍：state 还没重渲染完时一次读回就判死 = 白拒一条。"""

    LINES = ["续火花˵>ㅿ<˵"]

    def test_a_late_render_still_counts_as_landed(self):
        page = _StalePage(text=self.LINES[0], stale=["", ""])
        self.assertTrue(_TypingIM(page)._typing_landed(self.LINES))

    def test_text_that_never_matches_is_still_not_landed(self):
        page = _StalePage(text="别的字", stale=["", ""])
        self.assertFalse(_TypingIM(page)._typing_landed(self.LINES))

    def test_attempts_are_bounded(self):
        page = _StalePage(text=self.LINES[0], stale=[""] * 20)
        self.assertFalse(_TypingIM(page)._typing_landed(self.LINES))
        self.assertLessEqual(page.text_reads, _TypingIM.READBACK_ATTEMPTS + 2)


class JsReplaceTests(unittest.TestCase):
    """JS 覆盖通道 `_type_js_replace`：先**原生全选**，再 execCommand 替换。

    ★ 第一版兜底（"元素级选区 + execCommand"）在真机上**完全无效**（`/tmp/ml_probe2.py`：
      `ok=false`、内容一个字符都不变）⇒ 可信输入一旦掉字就必然走到"拒发" = 漏发。
      这里把新通道的两条安全线钉住：全选失败且编辑器非空 → 拒发；覆盖后读回不过 → 拒发。
    """

    LINES = ["续火花˵>ㅿ<˵"]

    def test_replaces_a_dirty_editor(self):
        page = _ProbePage(text="ABC<>xyz")
        im = _TypingIM(page, mode="jsreplace")
        self.assertTrue(im._type_js_replace(self.LINES, "none"))
        self.assertEqual(page.text, self.LINES[0])
        self.assertEqual(page.js_replaces, [self.LINES[0]])

    def test_select_all_failure_with_a_dirty_editor_refuses(self):
        page = _ProbePage(select_all_ok=False, text="残留")
        im = _TypingIM(page, mode="jsreplace")
        with self.assertRaises(RuntimeError) as ctx:
            im._type_js_replace(self.LINES, "none")
        self.assertIn("全选失败", str(ctx.exception))
        self.assertEqual(page.js_replaces, [])       # 一个字都没插进去

    def test_select_all_failure_with_an_empty_editor_is_allowed(self):
        page = _ProbePage(select_all_ok=False, text="")
        im = _TypingIM(page, mode="jsreplace")
        self.assertTrue(im._type_js_replace(self.LINES, "none"))
        self.assertEqual(page.js_replaces, [self.LINES[0]])

    def test_js_path_that_does_not_land_refuses_to_send(self):
        page = _ProbePage(text="ABC", synth_lands=False)
        im = _TypingIM(page, mode="jsreplace")
        with self.assertRaises(RuntimeError) as ctx:
            im._type_js_replace(self.LINES, "none")
        self.assertIn("拒绝发送", str(ctx.exception))


class SynthResidueGuardTests(unittest.TestCase):
    """老 JS 合成输入是**追加**语义：编辑器有残留时必须拒发，而不是叠成一条。"""

    LINES = ["续火花˵>ㅿ<˵"]

    def test_residue_plus_expected_text_refuses(self):
        page = _ProbePage(text="残留字")
        im = _TypingIM(page, mode="synth")
        with self.assertRaises(RuntimeError) as ctx:
            im._type_synth(self.LINES, "none")
        self.assertIn("叠发", str(ctx.exception))

    def test_short_typing_still_only_warns(self):
        """掉字（读回里没有完整期望文本）走老口径：只告警、照发 —— 产品取舍。"""
        page = _TruncatingPage(text="")
        im = _TypingIM(page, mode="synth")
        im._type_synth(self.LINES, "none")           # 不抛
        self.assertEqual(page.text, "续火")


class JsReplaceSourceGuardTests(unittest.TestCase):
    """源码级守卫：把真机取证钉在代码里，防止"顺手改回去"。"""

    def test_js_replace_does_its_own_native_select_all(self):
        js = douyin_im.JS_REPLACE_TEXT
        self.assertIn("execCommand('selectAll')", js)
        self.assertIn("sel_in", js)

    def test_editor_selectors_match_pick(self):
        """`_editor()` 的选择器必须与 `JS_EDITOR_SELECTORS.pick()` 完全一致。"""
        src = Path(douyin_im.__file__).read_text(encoding="utf-8")
        body = src.split("def _editor(self):", 1)[1].split("def _msg_state", 1)[0]
        body = body.split('"""')[-1]                 # 去掉 docstring，只看代码
        pick = src.split("JS_EDITOR_SELECTORS = ", 1)[1].split('"""', 2)[1]
        for sel in re.findall(r"document\.querySelector\('([^']+)'\)", pick):
            self.assertIn(sel, body, f"_editor 少了 pick() 里的选择器：{sel}")
        self.assertNotIn(".messageEditorimChatEditorContainer", body)


class ClickDecouplingTests(unittest.TestCase):
    """点击方式与输入方式解耦：可信鼠标点在虚拟化会话列表上会点错会话。"""

    def test_clicks_use_the_constant_not_the_input_mode(self):
        src = Path(douyin_im.__file__).read_text(encoding="utf-8")
        self.assertIn('modes = (("mouse", "elclick", "jsclick") if self.PREFER_TRUSTED_CLICK', src)
        self.assertIn("how = self._click_send() if self.PREFER_TRUSTED_CLICK else self._js_click_send()", src)
        self.assertNotIn("if self._input_mode() == 'synth' else self._click_send()", src)
        self.assertNotIn('if self._input_mode() == "synth"\n                 else ("mouse", "elclick", "jsclick")', src)

    def test_default_prefers_the_proven_js_clicks(self):
        self.assertFalse(douyin_im.DouyinIM.PREFER_TRUSTED_CLICK)


class _TimedPage(_ProbePage):
    """记下每次块间停顿的毫秒数（真机是 `page.wait_for_timeout`）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        return None


class ChunkedTypingTests(unittest.TestCase):
    """分段输入：最终内容一字不差，但打进页面的过程是"一段一段长出来"的。

    ★ 安全语义：调用方已全选编辑器 → 第一块**替换**、后续块**追加**，合起来必须等于
    要发的消息；落地校验只看最终内容（精确相等），不看"打了几块"。
    """

    TEXT = "续火花˵>ㅿ<˵"       # 8 个字

    def _run(self, chars, delay, page=None):
        page = page or _TimedPage()
        im = _TypingIM(page, mode="real")
        cfg = {"typingChunkChars": chars, "typingChunkDelayMs": delay}
        with mock.patch.object(douyin_im, "get_config", lambda: cfg):
            im._insert_text_in_chunks(self.TEXT)
        return page

    def test_splits_the_message_into_chunks(self):
        page = self._run(4, 100)
        self.assertEqual([t for _, t in page.typed], ["续火花˵", ">ㅿ<˵"])
        self.assertEqual(page.text, self.TEXT)

    def test_every_chunk_is_followed_by_a_short_pause(self):
        page = self._run(3, 100)
        self.assertEqual(len(page.typed), 3)
        self.assertEqual(len(page.waits), 2)
        for ms in page.waits:
            self.assertGreaterEqual(ms, 60)
            self.assertLessEqual(ms, 140)

    def test_zero_chunk_size_types_in_one_go(self):
        page = self._run(0, 100)
        self.assertEqual(page.typed, [("cdp-insert", self.TEXT)])
        self.assertEqual(page.waits, [])

    def test_zero_delay_types_in_one_go(self):
        page = self._run(4, 0)
        self.assertEqual(page.typed, [("cdp-insert", self.TEXT)])
        self.assertEqual(page.waits, [])

    def test_short_message_is_not_split(self):
        page = self._run(20, 100)
        self.assertEqual(page.typed, [("cdp-insert", self.TEXT)])
        self.assertEqual(page.waits, [])

    def test_chunking_keeps_the_landing_check_intact(self):
        """切块之后 `_try_trusted_typing` 仍要在"内容精确相等"时才返回 True。"""
        page = _TimedPage()
        im = _TypingIM(page, mode="real")
        cfg = {"typingChunkChars": 3, "typingChunkDelayMs": 100}
        with mock.patch.object(douyin_im, "get_config", lambda: cfg):
            self.assertTrue(im._try_trusted_typing(page, [self.TEXT]))
        self.assertEqual(page.text, self.TEXT)

    def test_a_dropped_chunk_is_detected(self):
        class _Dropping(_TimedPage):
            def insert_text(self, s):
                if s == ">ㅿ<˵":
                    self.typed.append(("cdp-insert", s))
                    return                      # 第二块丢了
                super().insert_text(s)

        page = _Dropping()
        im = _TypingIM(page, mode="real")
        cfg = {"typingChunkChars": 4, "typingChunkDelayMs": 100}
        with mock.patch.object(douyin_im, "get_config", lambda: cfg):
            self.assertFalse(im._try_trusted_typing(page, [self.TEXT]))
        self.assertEqual(page.text, "续火花˵")   # 缺字 → 落地校验必须说没满


class _WarmPage(_ProbePage):
    """能读/改滚动位置的页面（暖场浏览只吃这两条 JS）。"""

    def __init__(self, *a, height=800, scroll_height=4000, found=True,
                 scroll_ok=True, **kw):
        super().__init__(*a, **kw)
        self.height = height
        self.scroll_height = scroll_height
        self.found = found
        self.scroll_ok = scroll_ok
        self.scrolls = []
        self.top = 0

    def evaluate(self, expr, arg=None):
        if expr is douyin_im.JS_SCROLL_PROBE:
            if not self.found:
                return {"found": False}
            return {"found": True, "clientHeight": self.height,
                    "scrollHeight": self.scroll_height, "scrollTop": self.top}
        if expr is douyin_im.JS_SCROLL_TO:
            self.scrolls.append(arg)
            if not self.scroll_ok:
                return False
            self.top = arg
            return True
        return super().evaluate(expr, arg)


class _TickClock:
    """每取一次时间就前进一小步的假时钟。

    暖场浏览是"按预算循环、每轮取好几次时间"，`_FastClock` 每调用 +1.0 太粗 ——
    一轮循环就能吃掉整个预算（症状是只滚了一次、还在半截就收手）。
    """

    def __init__(self, step=0.1):
        self.t = 0.0
        self.step = step

    def monotonic(self):
        self.t += self.step
        return self.t


class WarmupBrowseTests(unittest.TestCase):
    """暖场浏览：只滚不点、来得及收手；任何异常都不能挡住随后的发送。"""

    def _run(self, page, seconds):
        im = _TypingIM(page, mode="real")
        with mock.patch.object(douyin_im, "time", _TickClock()):
            return im.warmup_browse(seconds)

    def test_zero_seconds_does_nothing(self):
        page = _WarmPage()
        self.assertEqual(self._run(page, 0), 0.0)
        self.assertEqual(page.scrolls, [])

    def test_negative_seconds_does_nothing(self):
        page = _WarmPage()
        self.assertEqual(self._run(page, -5), 0.0)
        self.assertEqual(page.scrolls, [])

    def test_browses_down_and_then_back_up(self):
        page = _WarmPage(scroll_height=3000, height=800)
        self._run(page, 8)
        self.assertGreaterEqual(len(page.scrolls), 3)
        self.assertGreater(max(page.scrolls), 0)
        self.assertLess(min(page.scrolls), max(page.scrolls))   # 到底之后回滚过
        self.assertEqual(page.clicked, 0)                      # 只滚，不点

    def test_seconds_are_capped_by_the_hard_limit(self):
        page = _WarmPage()
        spent = self._run(page, 999)
        cap = douyin_im.DouyinIM.WARMUP_BROWSE_MAX_SECONDS
        # 收手是在"下一轮检查"时才发生的，所以实际耗时可能比预算多出最后几轮的开销。
        self.assertLessEqual(spent, cap + 2)
        self.assertGreater(spent, 0)

    def test_missing_scroll_state_skips_quietly(self):
        page = _WarmPage(found=False)
        self.assertEqual(self._run(page, 10), 0.0)
        self.assertEqual(page.scrolls, [])

    def test_scroll_failure_stops_instead_of_raising(self):
        page = _WarmPage(scroll_ok=False)
        self._run(page, 10)                    # 不抛
        self.assertEqual(len(page.scrolls), 1)


if __name__ == "__main__":
    unittest.main()
