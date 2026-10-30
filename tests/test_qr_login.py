"""`core.qr_login` 的可离线测试部分。

真跑一次扫码需要浏览器和一台手机，所以这里只钉三类东西：
  * 面板赖以显示的状态机（快照字段、二维码序号只在换图时递增、取消语义）；
  * `import` 这一层不许拖进浏览器依赖（面板进程 import 它是安全的）；
  * 页面脚本的挑选规则（纯字符串，确认选择器没被谁改跑偏）。
真正"能不能扫出码"由 本机 上的真机验证负责。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import browser_state, qr_login  # noqa: E402


class SessionStateTests(unittest.TestCase):
    def make(self, **kwargs):
        return qr_login.QrLoginSession("/tmp/does-not-matter", **kwargs)

    def test_initial_snapshot(self):
        session = self.make()
        snap = session.snapshot()
        self.assertEqual(snap["state"], qr_login.STATE_STARTING)
        self.assertEqual(snap["state_label"], qr_login.STATE_LABELS[qr_login.STATE_STARTING])
        self.assertTrue(snap["active"], "刚建好就算进行中")
        self.assertFalse(snap["has_qr"])
        self.assertEqual(snap["qr_seq"], 0)
        self.assertEqual(snap["error"], "")
        self.assertIsNone(snap["persist"])
        self.assertIsNone(snap["account"])
        self.assertGreaterEqual(snap["age_seconds"], 0)

    def test_snapshot_has_every_key_the_panel_reads(self):
        snap = self.make().snapshot()
        for key in (
            "state",
            "state_label",
            "message",
            "hint",
            "error",
            "qr_seq",
            "has_qr",
            "active",
            "started_at",
            "updated_at",
            "finished_at",
            "age_seconds",
            "account",
            "persist",
        ):
            with self.subTest(key=key):
                self.assertIn(key, snap)

    def test_qr_seq_only_moves_when_the_image_changes(self):
        session = self.make()
        session._set_qr(b"first")
        self.assertEqual(session.snapshot()["qr_seq"], 1)
        session._set_qr(b"first")
        self.assertEqual(session.snapshot()["qr_seq"], 1, "同一张图不该让浏览器重新下载")
        session._set_qr(b"second")
        self.assertEqual(session.snapshot()["qr_seq"], 2)
        session._set_qr(b"")
        self.assertEqual(session.snapshot()["qr_seq"], 2, "空图不推进序号")
        self.assertEqual(session.qr_png(), b"second")

    def test_a_finished_session_stops_handing_out_the_qr(self):
        """收尾之后那张码已经没有页面在对接了，不能再让面板画出来（2026-10-30）。"""
        session = self.make()
        session._set_qr(b"picture")
        session._set(qr_login.STATE_WAITING_SCAN, "等待扫码")
        self.assertTrue(session.snapshot()["has_qr"])
        session._set(qr_login.STATE_CANCELLED, "已取消")
        snap = session.snapshot()
        self.assertFalse(snap["active"])
        self.assertFalse(snap["has_qr"], "死码不能再展示：用户扫了只会得到「二维码已过期」")
        self.assertEqual(session.qr_png(), b"")

    def test_the_qr_is_still_served_while_the_session_is_alive(self):
        session = self.make()
        session._set_qr(b"picture")
        session._set(qr_login.STATE_WAITING_SCAN, "等待扫码")
        self.assertEqual(session.qr_png(), b"picture")

    def test_set_qr_tells_the_watchdog_whether_the_image_moved(self):
        """`_set_qr` 的返回值就是看门狗的计时依据，不能只当副作用函数用。"""
        session = self.make()
        self.assertTrue(session._set_qr(b"first"), "换了新图要返回 True，看门狗好重新计时")
        self.assertFalse(session._set_qr(b"first"), "同一张图不算刷新")
        self.assertFalse(session._set_qr(b""), "空图不算刷新")

    def test_read_qr_failures_are_counted_not_swallowed(self):
        """evaluate 抛错时以前是静默 return，排障时什么都看不到。"""
        session = self.make()

        class Boom:
            def evaluate(self, _script):
                raise RuntimeError("Execution context was destroyed")

        self.assertEqual(session._read_qr(Boom()), b"")
        self.assertEqual(session._qr_read_fails, 1)
        self.assertIn("Execution context was destroyed", session._qr_read_error)
        self.assertEqual(session._read_qr(Boom()), b"")
        self.assertEqual(session._qr_read_fails, 2)

    def test_finishing_state_is_not_active(self):
        session = self.make()
        session._set(qr_login.STATE_DONE, "好了")
        snap = session.snapshot()
        self.assertFalse(snap["active"])
        self.assertGreater(snap["finished_at"], 0, "收尾状态要记时间，否则面板一直显示『已进行 N 秒』")

    def test_cancel_sets_the_event_and_says_so(self):
        session = self.make()
        session.cancel()
        self.assertTrue(session._cancel.is_set())
        self.assertIn("取消", session.snapshot()["message"])

    def test_message_setter_is_visible_in_snapshot(self):
        session = self.make()
        session.message = "等待扫码"
        self.assertEqual(session.snapshot()["message"], "等待扫码")

    def test_extra_keys_land_on_the_snapshot(self):
        # _set(**extra) 是内部约定：state 推进时顺带写 hint/error
        session = self.make()
        session._set(qr_login.STATE_WAITING_SCAN, "已扫码", hint="请在手机上确认")
        snap = session.snapshot()
        self.assertEqual(snap["state"], qr_login.STATE_WAITING_SCAN)
        self.assertEqual(snap["hint"], "请在手机上确认")

    def test_done_message(self):
        text = qr_login.QrLoginSession._done_message(
            {
                "cookies": [{"name": "sessionid"}, {"name": "ttwid"}],
                "account": {"nickname": "示例昵称", "unique_id": "12345678905"},
            }
        )
        self.assertIn("示例昵称", text)
        self.assertIn("12345678905", text)
        self.assertIn("2", text)

    def test_done_message_without_an_account_id(self):
        text = qr_login.QrLoginSession._done_message({"cookies": [], "account": {}})
        self.assertIn("登录成功", text)


class ConstantsTests(unittest.TestCase):
    def test_active_states(self):
        self.assertIn(qr_login.STATE_WAITING_QR, qr_login.ACTIVE_STATES)
        for state in (qr_login.STATE_DONE, qr_login.STATE_ERROR, qr_login.STATE_CANCELLED):
            with self.subTest(state=state):
                self.assertNotIn(state, qr_login.ACTIVE_STATES)
                self.assertIn(state, qr_login.STATE_LABELS, "每个状态都要有中文标签给面板显示")

    def test_checking_has_its_own_label_and_counts_as_active(self):
        """步骤①不许再借「已扫码，正在确认登录态」那个状态。

        用户 2026-10-10 的反馈：点「重新登录」后扫码页直接显示"已扫码"——
        因为那一步用的就是 verifying。它现在有自己的标签。
        """
        self.assertIn(qr_login.STATE_CHECKING, qr_login.ACTIVE_STATES,
                      "确认已有登录态期间浏览器还开着，不能算结束")
        checking = qr_login.STATE_LABELS[qr_login.STATE_CHECKING]
        verifying = qr_login.STATE_LABELS[qr_login.STATE_VERIFYING]
        self.assertIn("已有登录态", checking)
        self.assertNotIn("已扫码", checking, "这一步还没扫任何码")
        self.assertIn("已扫码", verifying, "扫完码之后的确认才叫已扫码")
        self.assertNotEqual(checking, verifying)

    def test_login_page_markers_stay_populated(self):
        """排除登录页的那几个特征表一旦被清空，登录页又会被当成身份验证页。"""
        self.assertTrue(qr_login.LOGIN_PAGE_WORDS)
        self.assertTrue(qr_login.PHONE_INPUT_HINTS)
        self.assertTrue(qr_login.CODE_INPUT_FIELDS)
        self.assertGreaterEqual(qr_login.LOGIN_PROBE_INTERVAL, qr_login.SHOT_INTERVAL_SECONDS)
        self.assertGreater(qr_login.LOGIN_PROBE_SECONDS, 1)

    def test_scan_markers_are_mapped_to_human_text(self):
        self.assertTrue(qr_login.SCAN_MARKERS)
        for marker, text in qr_login.SCAN_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(marker.strip())
                self.assertTrue(text.strip())

    def test_the_expired_wording_is_not_taken_for_the_user_confirming(self):
        """页面挂着「二维码已过期」时，看门狗必须出手。

        这里钉的是一个真踩过的坑：自愈的判据一开始写成「hint 非空就不自愈」，
        可停摆的页面恰恰会显示「二维码已过期，正在刷新」—— 于是最该自愈的现场
        反被放过，用户继续端着一张死码干等到超时。
        """
        texts = {text for _marker, text in qr_login.SCAN_MARKERS}
        expired = {text for text in texts if "过期" in text}
        self.assertTrue(expired, "SCAN_MARKERS 里应当有一类『二维码已过期』话术")
        for text in expired:
            with self.subTest(text=text):
                self.assertNotIn(text, qr_login.CONFIRMING_HINTS,
                                 "『已过期』不是『用户在确认』，不能被看门狗放过")

    def test_confirming_hints_come_from_the_marker_table(self):
        texts = {text for _marker, text in qr_login.SCAN_MARKERS}
        self.assertTrue(qr_login.CONFIRMING_HINTS)
        for text in qr_login.CONFIRMING_HINTS:
            with self.subTest(text=text):
                self.assertIn(text, texts, "确认类话术必须仍由 SCAN_MARKERS 产出")

    def test_watchdog_thresholds_are_sane(self):
        # 抖音的登录码实测 ~60 秒换一张：阈值必须比它宽松，否则会误判成停摆
        self.assertGreater(qr_login.QR_STALL_SECONDS, 60)
        self.assertLess(qr_login.QR_STALL_SECONDS, qr_login.DEFAULT_TTL_SECONDS,
                        "阈值超过 TTL 就永远等不到自愈")
        self.assertGreaterEqual(qr_login.QR_MAX_RECOVERIES, 1)

    def test_page_scripts_target_the_real_dom(self):
        # 真机探针确认过：二维码是内联 data:image/png 的 <img>，登录入口是文案为「登录」的元素
        self.assertIn("data:image/", qr_login.PICK_QR_JS)
        self.assertIn("登录", qr_login.CLICK_LOGIN_JS)
        self.assertIn("#login-panel-new", qr_login.PANEL_HINT_JS)

    def test_ttl_and_intervals_are_sane(self):
        self.assertGreater(qr_login.DEFAULT_TTL_SECONDS, 60)
        self.assertLess(qr_login.POLL_INTERVAL_MS, 5000)
        self.assertGreater(qr_login.VERIFY_TIMEOUT_SECONDS, 10)

    def test_start_session_signature(self):
        import inspect

        params = inspect.signature(qr_login.start_session).parameters
        for name in ("profile_dir", "fingerprint", "proxy", "ttl", "on_success", "headless"):
            with self.subTest(name=name):
                self.assertIn(name, params)


class StallVerdictTests(unittest.TestCase):
    """看门狗判据：二维码没换新，到底该不该动页面。

    钉的是 2026-10-30 的真事故 —— 用户扫上码、手机上还没点确认时，码本来就不换；
    旧判据 90 秒就整页重载，把那张正在确认的码换掉，用户点的那一下落到一张没有
    页面在轮询的旧码上，面板从此一直「等待扫码」直到 300 秒超时。
    """

    def test_confirming_wording_stops_everything(self):
        confirming = qr_login.CONFIRMING_HINTS[0]
        self.assertEqual(
            qr_login._stall_verdict(1000.0, 0.0, has_qr=True, hint=confirming),
            "wait", "手机上正在确认，页面一动不动是正常的")
        self.assertEqual(
            qr_login._stall_verdict(1000.0, 0.0, has_qr=False, qr_gone_at=0.1,
                                    hint=confirming),
            "wait", "连码都没了、话术又在那儿，更不能动")

    def test_already_scanned_never_triggers_anything(self):
        self.assertEqual(
            qr_login._stall_verdict(1000.0, 0.0, has_qr=True, scanned=True),
            "wait")
        self.assertEqual(
            qr_login._stall_verdict(1000.0, 0.0, has_qr=False, qr_gone_at=0.0,
                                    scanned=True),
            "wait")

    def test_a_qr_that_just_left_the_page_is_not_touched(self):
        self.assertEqual(
            qr_login._stall_verdict(1000.0, 900.0, has_qr=False, qr_gone_at=997.0),
            "wait", "码刚消失，先当它在等手机确认")

    def test_a_qr_that_stays_gone_reopens_the_panel_instead_of_reloading(self):
        verdict = qr_login._stall_verdict(
            1000.0, 700.0, has_qr=False,
            qr_gone_at=1000.0 - qr_login.QR_GONE_REOPEN_SECONDS - 1)
        self.assertEqual(verdict, "reopen", "消失久了先点一次登录，不撕页面")

    def test_a_qr_that_never_repaints_waits_for_the_long_window(self):
        span = qr_login.QR_STALL_RELOAD_SECONDS
        self.assertEqual(qr_login._stall_verdict(1000.0, 1000.0 - span + 10,
                                                 has_qr=True), "wait")
        self.assertEqual(qr_login._stall_verdict(1000.0, 1000.0 - span - 1,
                                                 has_qr=True), "reload")

    def test_the_old_ninety_second_trigger_is_gone(self):
        """90 秒这个数只剩「开始怀疑」的意思，不许再触发重载。"""
        self.assertLess(qr_login.QR_STALL_SECONDS, qr_login.QR_STALL_RELOAD_SECONDS)
        self.assertEqual(
            qr_login._stall_verdict(1000.0, 1000.0 - qr_login.QR_STALL_SECONDS - 1,
                                    has_qr=True),
            "wait")

    def test_unknown_page_state_is_not_taken_for_a_missing_qr(self):
        """读不到页面（evaluate 抛了 / 正在跳转）时 has_qr 是 None，不能当没码。"""
        self.assertEqual(qr_login._stall_verdict(1000.0, 990.0, has_qr=None), "wait")
        self.assertEqual(qr_login._stall_verdict(1000.0, 1000.0 - 999,
                                                 has_qr=None), "reload")


class PanelProbeTests(unittest.TestCase):
    """把页面读数翻译成面板话术 / 结构化的探测结果。"""

    class _Page:
        def __init__(self, got):
            self.got = got

        def evaluate(self, _script):
            if isinstance(self.got, Exception):
                raise self.got
            return self.got

    class _Session:
        """只带这两个方法的壳：真 QrLoginSession 会去开浏览器，这里只想验翻译。"""

        _panel_probe = qr_login.QrLoginSession._panel_probe
        _panel_hint = qr_login.QrLoginSession._panel_hint

    def test_hint_comes_from_the_marker_table(self):
        hint = qr_login._hint_of(["请在手机上确认"])
        self.assertEqual(hint, "已扫码，请在手机上点一下确认")

    def test_the_first_matching_mark_wins(self):
        self.assertEqual(qr_login._hint_of(["二维码已失效", "已扫码"]),
                         "二维码已过期，正在刷新")

    def test_no_mark_means_no_hint(self):
        self.assertEqual(qr_login._hint_of([]), "")
        self.assertEqual(qr_login._hint_of(None), "")

    def test_probe_reads_the_rich_shape(self):
        page = self._Page({"marks": ["已扫码"], "hasQr": False,
                           "url": "https://www.douyin.com/jingxuan"})
        got = self._Session()._panel_probe(page)
        self.assertEqual(got["has_qr"], False)
        self.assertEqual(got["hint"], "已扫码，请在手机上点一下确认")
        self.assertIn("douyin.com", got["url"])

    def test_probe_still_accepts_the_old_array_shape(self):
        page = self._Page(["请在手机上确认"])
        got = self._Session()._panel_probe(page)
        self.assertIsNone(got["has_qr"], "老脚本不报 hasQr，只能算不知道")
        self.assertEqual(got["hint"], "已扫码，请在手机上点一下确认")

    def test_probe_failure_is_unknown_not_missing(self):
        page = self._Page(RuntimeError("page is navigating"))
        got = self._Session()._panel_probe(page)
        self.assertIsNone(got["has_qr"])
        self.assertEqual(got["hint"], "")

    def test_panel_hint_stays_a_thin_wrapper(self):
        page = self._Page({"marks": ["二维码已失效"], "hasQr": True, "url": ""})
        self.assertEqual(self._Session()._panel_hint(page), "二维码已过期，正在刷新")


class AdoptProfileTests(unittest.TestCase):
    """登录成功后把资料目录搬到账号名下（`default/` → `<抖音号>/`）。

    真机上踩到过：新建账号时还不知道抖音号，扫码只能用 `default/`；登录成功后
    面板按账号读好友列表，打开的是 `<抖音号>/` 那个空目录 —— 报"这个账号还没登录"。
    """

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def make(self, name="default", **kwargs):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.join(path, "Default"), exist_ok=True)
        with open(os.path.join(path, "Default", "Cookies"), "w", encoding="utf-8") as fh:
            fh.write("x")
        return qr_login.QrLoginSession(path, **kwargs)

    def test_moves_the_profile_to_the_account_dir(self):
        session = self.make()
        session.adopt_profile(os.path.join(self.root, "12345678905"))
        session._adopt_profile()
        self.assertTrue(os.path.isdir(os.path.join(self.root, "12345678905", "Default")))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "default")))
        self.assertEqual(session.profile_dir, os.path.join(self.root, "12345678905"))
        self.assertEqual(session.moved_to, os.path.join(self.root, "12345678905"))
        self.assertTrue(session.persist["profile_moved"]["ok"])

    def test_an_existing_account_dir_is_moved_aside_not_overwritten(self):
        """目标目录已存在时：旧的挪到 `<uid>.stale-<时间戳>`，新的搬进去。

        ★ 2026-10-17 审计 S4：以前这里是"直接放弃"，后果很具体 —— 账号继续用旧的、
        可能已经登出的那一份（用户看到"扫了码还是没登上"），而**新登录态留在源目录里
        没人用**。旧的那一份必须保留（不许删、不许改），只是改个名，让
        `core.maintenance.prune_orphans` 在 7 天宽限期之后收走。
        """
        session = self.make()
        target = os.path.join(self.root, "12345678905")
        os.makedirs(os.path.join(target, "Default"), exist_ok=True)
        with open(os.path.join(target, "Default", "Cookies"), "w", encoding="utf-8") as fh:
            fh.write("原来的登录态")
        session.adopt_profile(target)
        session._adopt_profile()
        with open(os.path.join(target, "Default", "Cookies"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x", "新登录态应该搬进账号目录")
        stale = sorted(n for n in os.listdir(self.root) if n.startswith("12345678905.stale-"))
        self.assertEqual(len(stale), 1, f"旧目录应该被挪成一个 .stale- 目录：{stale}")
        with open(os.path.join(self.root, stale[0], "Default", "Cookies"),
                  encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "原来的登录态", "旧资料目录只能被挪走，不能删也不能改")
        self.assertFalse(os.path.isdir(os.path.join(self.root, "default")), "源目录应该搬空了")
        self.assertTrue(session.persist["profile_moved"]["ok"])
        self.assertEqual(session.moved_to, target)

    def test_same_path_is_a_noop(self):
        session = self.make()
        session.adopt_profile(session.profile_dir)
        session._adopt_profile()
        self.assertEqual(session.moved_to, "")
        self.assertIsNone(session.persist)

    def test_missing_source_is_reported_not_raised(self):
        session = qr_login.QrLoginSession(
            os.path.join(self.root, "gone"), on_success=lambda result: {"ok": True})
        session.adopt_profile(os.path.join(self.root, "12345678905"))
        session._adopt_profile()          # 不该抛
        self.assertFalse(session.persist["profile_moved"]["ok"])
        self.assertIn("不存在", session.persist["profile_moved"]["error"])

    def test_persist_can_ask_for_the_move(self):
        """面板走的路径：on_success 返回 adopt_profile，_finish 记下来。"""
        session = self.make(
            on_success=lambda result: {
                "ok": True, "profile_dir": result.get("profile_dir"),
                "adopt_profile": os.path.join(self.root, "12345678905"),
            })
        session._finish(_Ctx(), _Page(), None)
        self.assertEqual(session._adopt, os.path.join(self.root, "12345678905"))

    def test_keep_existing_persist_keys(self):
        session = self.make(
            on_success=lambda result: {
                "ok": True, "key": "COOKIES_1",
                "adopt_profile": os.path.join(self.root, "12345678905"),
            })
        session._finish(_Ctx(), _Page(), None)
        session._adopt_profile()
        self.assertEqual(session.persist["key"], "COOKIES_1", "原来写的键不能丢")
        self.assertTrue(session.persist["profile_moved"]["ok"])


class EnsureProfileDirTests(unittest.TestCase):
    """资料目录里是明文的 sessionid，权限不能跟着 umask 走（默认 0755 = 全机可读）。

    `os.makedirs(..., exist_ok=True)` 不会去收紧**已经存在**的目录，所以这里两头都要钉：
    新建时就是 0700，老目录（线上现存的那几个是 0755）也要被收紧。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_new_dir_is_0700(self):
        # Windows 的 `os.chmod` 只有"只读"这一位、`stat` 也不报 POSIX 权限位
        # （实测 S_IMODE 恒为 0o777，于是断言变成 "511 != 448"）。生产代码里
        # `webui/envfile.py::_lock_down` 那种 `os.name == "nt"` 早退说明权限本身
        # 在那边不存在语义 —— 所以只校验"新建即 0700"，跳过非 POSIX。
        if os.name != "posix":
            self.skipTest("非 POSIX 不校验权限位（Windows 的 chmod 只有只读位）")
        path = os.path.join(self.root, "a", "b")
        self.assertEqual(qr_login._ensure_profile_dir(path), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)

    def test_existing_0755_dir_is_tightened(self):
        if os.name != "posix":
            self.skipTest("非 POSIX 不校验权限位（Windows 的 chmod 只有只读位）")
        path = os.path.join(self.root, "old")
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o755)
        qr_login._ensure_profile_dir(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700,
                         "已经存在的 0755 目录必须被收紧（makedirs 不会动它）")

    def test_chmod_failure_never_raises(self):
        """chmod 失败（比如目录在只读挂载上）只该记一条日志，不能把登录带走。"""
        path = os.path.join(self.root, "cant")
        os.makedirs(path, exist_ok=True)
        original = os.chmod
        os.chmod = lambda *a, **kw: (_ for _ in ()).throw(PermissionError("nope"))
        self.addCleanup(setattr, os, "chmod", original)
        self.assertEqual(qr_login._ensure_profile_dir(path), path, "不抛异常")
        self.assertTrue(os.path.isdir(path), "目录仍然要建出来")


class _Ctx:
    """假的持久化上下文：只够 `_finish` 用。"""

    def storage_state(self):
        return {"cookies": [
            {"name": "sessionid", "value": "x", "domain": ".douyin.com", "path": "/"},
            {"name": "ttwid", "value": "y", "domain": ".douyin.com", "path": "/"},
        ]}


class _Page:
    def evaluate(self, script):
        return "Mozilla/5.0 (test)"

    def wait_for_timeout(self, ms):     # 现场不真等
        return None


class PurityTests(unittest.TestCase):
    def test_import_does_not_pull_browser_dependencies(self):
        code = (
            "import sys; import core.qr_login; "
            "bad=[m for m in ('playwright','cloakbrowser') if m in sys.modules]; "
            "print(','.join(bad))"
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "", f"import 期不该拉浏览器依赖：{done.stdout.strip()}")

    def test_launch_context_reports_a_readable_error(self):
        # 要的是"启动失败"，但**不能依赖这台机器装没装 Chromium**：以前传一个不存在的目录，
        # 在装了浏览器的机器上会真的起一个空 profile（用例反而红不了）；现在给一个不存在的后端名，
        # core/browser.py 末尾会 raise ValueError("未知的 BROWSER_BACKEND：…")，被 _launch_context
        # 收成 RuntimeError 并带上人话（而不是把 traceback 甩给面板）。
        with mock.patch.dict(os.environ, {"BROWSER_BACKEND": "definitely-not-a-backend"}):
            with self.assertRaises(RuntimeError) as ctx:
                qr_login._launch_context("/tmp/dsf-test-profile-never-used", args=[], proxy=None)
        message = str(ctx.exception)
        self.assertIn("浏览器启动失败", message)
        self.assertIn("未知的 BROWSER_BACKEND", message)


class CodeVerifyTests(unittest.TestCase):
    """二次验证：风控账号扫码之后还要填一条短信验证码。

    真机那一步要一台手机，所以这里钉的是"这条码能不能收进来、填进去、
    状态机会不会卡死" —— 页面本身用假 Playwright 对象顶替。
    """

    def session(self, **kwargs):
        kwargs.setdefault("ttl", 60.0)
        return qr_login.QrLoginSession("/tmp/does-not-matter", **kwargs)

    def pin_login_cookie_state(self, state):
        """让「登录 cookie 换了一份新的没有」由 state[0] 驱动。

        桩必须打在**指纹**上，不能打在 `has_login_cookie`（只看名字）上：2026-10-11
        现场就是名字判据造成的假通过 —— 资料目录里躺着上一轮那份已被服务端作废的
        `sessionid`，名字一模一样。现在进二次验证时抓一次基线，只认"值换没换"。

        state[0] 为假时指纹是空的（"没有登录 cookie"），为真时是一份新指纹；这样
        基线为空、后来有了，也算"换了一份新的"。
        """
        original = qr_login.QrLoginSession._cookie_signature
        qr_login.QrLoginSession._cookie_signature = (
            lambda self_, ctx: (("sessionid", "new") if state[0] else ()))
        self.addCleanup(setattr, qr_login.QrLoginSession, "_cookie_signature", original)

    def pin_verify_seconds(self, seconds):
        original = qr_login.CODE_VERIFY_SECONDS
        qr_login.CODE_VERIFY_SECONDS = seconds
        self.addCleanup(setattr, qr_login, "CODE_VERIFY_SECONDS", original)

    def pin_check_login(self, verdict):
        original = qr_login.douyin_im.check_login
        qr_login.douyin_im.check_login = lambda page, mon: dict(verdict)
        self.addCleanup(setattr, qr_login.douyin_im, "check_login", original)

    def pin_probe_seconds(self, seconds):
        original = qr_login.LOGIN_PROBE_SECONDS
        qr_login.LOGIN_PROBE_SECONDS = seconds
        self.addCleanup(setattr, qr_login, "LOGIN_PROBE_SECONDS", original)

    def code_page(self):
        return _FakePage({
            "text": "身份验证 请选择验证方式 接收短信验证码 发送短信到指定号码",
            "hasQr": False,
            "buttons": ["接收短信验证码", "发送短信到指定号码"],
            "inputs": [{"placeholder": "请输入验证码", "type": "text", "maxlength": 6}],
        })

    # ---- 状态机 ----
    def test_waiting_code_counts_as_active(self):
        session = self.session()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        snap = session.snapshot()
        self.assertEqual(snap["state_label"], qr_login.STATE_LABELS[qr_login.STATE_WAITING_CODE])
        self.assertTrue(snap["active"], "等验证码期间资料目录还被占着，不能算结束")
        self.assertTrue(snap["need_code"])
        self.assertEqual(snap["code_max_attempts"], qr_login.CODE_MAX_ATTEMPTS)

    def test_snapshot_has_the_code_keys_even_before_any_code(self):
        snap = self.session().snapshot()
        for key in ("need_code", "code_tries", "code_max_attempts", "code_error",
                    "code_options", "has_shot", "shot_seq"):
            with self.subTest(key=key):
                self.assertIn(key, snap)
        self.assertFalse(snap["need_code"])

    # ---- 收码 ----
    def test_submit_code_rejects_junk(self):
        session = self.session()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        for bad in ("", "   ", "abcd", "12a4", "1234567890123"):
            with self.subTest(code=bad):
                self.assertFalse(session.submit_code(bad)["ok"])
        self.assertFalse(session._code_event.is_set(), "拒收的值不该叫醒会话线程")

    def test_submit_code_strips_spaces_and_wakes_the_waiter(self):
        session = self.session()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        self.assertTrue(session.submit_code("  1 2 3 4  ")["ok"])
        self.assertTrue(session._code_event.is_set())
        self.assertEqual(session._code_value, "1234")

    def test_submit_code_outside_waiting_code_is_refused(self):
        """没在等码的时候不能默默收下：用户点了提交却什么都没发生最难查。"""
        result = self.session().submit_code("1234")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    # ---- 认页 ----
    def test_detect_code_page_ignores_the_login_page_itself(self):
        session = self.session()
        page = _FakePage({"text": "扫码登录 请使用抖音App扫一扫", "hasQr": True,
                          "inputs": [], "buttons": ["登录", "验证码登录"]})
        self.assertEqual(session._detect_code_page(page), {})

    def test_detect_code_page_needs_a_code_input_or_a_strong_word(self):
        session = self.session()
        # 只有文案、一个输入框都没有：先别抢页面
        page = _FakePage({"text": "身份验证 接收短信验证码", "hasQr": False,
                          "inputs": [], "buttons": ["接收短信验证码"]})
        self.assertEqual(session._detect_code_page(page), {})
        # 有"验证码"输入框才算数
        session._last_probe_at = 0.0
        page.probe = {"text": "请输入验证码", "hasQr": False,
                      "inputs": [{"placeholder": "请输入验证码", "type": "text", "maxlength": 6}],
                      "buttons": ["验证", "重新获取"]}
        got = session._detect_code_page(page)
        self.assertTrue(got, "有验证码输入框就该认出来")
        self.assertEqual(session.snapshot()["code_options"], ["验证", "重新获取"])

    def test_detect_code_page_is_throttled(self):
        session = self.session()
        page = _FakePage({"text": "请输入验证码", "hasQr": False,
                          "inputs": [{"placeholder": "请输入验证码"}], "buttons": []})
        self.assertTrue(session._detect_code_page(page))
        page.probe = {}
        self.assertEqual(session._detect_code_page(page), {}, "同一秒内不重复探测页面")

    def test_detect_code_page_ignores_the_new_login_dialog(self):
        """真机探针原样搬来（2026-10-10）：新版登录弹窗把「扫码登录 / 验证码登录 /
        密码登录」三个 tab 渲染在同一页，于是登录页上也有一个可见的「请输入验证码」。

        以前这份探针会被认成身份验证页 —— 面板亮出「需要短信验证码」，还会去点登录页
        上的「获取验证码」（真给用户手机发短信）。这就是用户说的"无论如何都显示需要
        二次验证"。
        """
        session = self.session()
        page = _FakePage({
            "text": "登录后免费畅享高清视频 扫码登录 验证码登录 密码登录 获取验证码 登录",
            "hasQr": True,
            "inputs": [{"placeholder": "搜索你感兴趣的内容"}, {"placeholder": ""},
                       {"placeholder": "请输入手机号"}, {"placeholder": "请输入验证码"}],
            "buttons": ["精选", "推荐", "AI抖音", "关注", "朋友", "我的", "直播", "放映厅"],
        })
        self.assertEqual(session._detect_code_page(page), {})
        self.assertEqual(session.snapshot()["code_options"], [],
                         "认错页面就别把它的按钮当成「要点的按钮」")

    def test_detect_code_page_ignores_a_login_form_without_a_qr(self):
        """二维码那一栏被折叠掉、只剩手机号输入框时，仍然是登录表单，不是验证码页。"""
        session = self.session()
        page = _FakePage({
            "text": "验证码登录 请输入手机号 请输入验证码",
            "hasQr": False,
            "inputs": [{"placeholder": "请输入手机号"}, {"placeholder": "请输入验证码"}],
            "buttons": ["登录"],
        })
        self.assertEqual(session._detect_code_page(page), {})

    def test_detect_code_page_still_takes_the_real_identity_page(self):
        """收紧判据不能把真的身份验证页一起挡掉（2026-10-07 真机见过的那一页）。"""
        session = self.session()
        page = self.code_page()
        self.assertTrue(session._detect_code_page(page))
        self.assertIn("接收短信验证码", session.snapshot()["code_options"])

    # ---- 走完流程 ----
    def test_solve_code_picks_receive_sms_then_fills_and_waits(self):
        session = self.session()
        state = [False]
        self.pin_login_cookie_state(state)
        page = self.code_page()

        def feed():
            time.sleep(0.05)
            session.submit_code("4321")
            time.sleep(0.05)
            state[0] = True

        threading.Thread(target=feed, daemon=True).start()
        self.assertTrue(session._solve_code(_FakeCtx(), page))
        self.assertEqual(page.filled, ["4321"])
        self.assertIn("接收短信验证码", page.clicked, "固定走「收验证码」那条路")
        self.assertNotIn("发送短信到指定号码", page.clicked, "绝不走「发指定短信」那条路")
        self.assertEqual(session.snapshot()["code_tries"], 1)

    def test_submit_falls_back_to_enter_when_no_real_button(self):
        """页面上只有一段写着「验证」的正文时，不能去点它 —— 那等于没提交。

        真 Chromium 探针抓到的：不限定标签时 `_submit_code` 会点 <p>身份验证</p>，
        点完就 return，回车兜底永远轮不到，验证码根本没提交出去。
        """
        session = self.session()
        page = self.code_page()
        page.probe["buttons"] = ["身份验证"]
        page.clickables = []                      # 这一段是正文，不是按钮
        session._submit_code(page)
        self.assertEqual(page.clicked, [], "正文不该被点")
        self.assertEqual(page.keyboard.presses, ["Enter"], "找不到按钮就该回车")

    def test_submit_clicks_a_real_button_when_there_is_one(self):
        session = self.session()
        page = self.code_page()
        page.probe["buttons"] = ["身份验证", "验证"]
        page.clickables = ["验证"]
        session._submit_code(page)
        self.assertEqual(page.clicked, ["验证"])
        self.assertEqual(page.keyboard.presses, [], "点着了就别再回车（会提交两次）")

    def test_solve_code_gives_up_after_max_attempts(self):
        session = self.session()
        self.pin_login_cookie_state([False])              # 永远不放行
        self.pin_verify_seconds(0.05)            # 别真等 60 秒
        page = self.code_page()

        def feed():
            for code in ("1111", "2222", "3333"):
                time.sleep(0.05)
                session.submit_code(code)

        threading.Thread(target=feed, daemon=True).start()
        self.assertFalse(session._solve_code(_FakeCtx(), page))
        snap = session.snapshot()
        self.assertEqual(snap["state"], qr_login.STATE_ERROR)
        self.assertEqual(snap["error"], "code_rejected")
        self.assertEqual(page.filled, ["1111", "2222", "3333"])
        self.assertTrue(snap["code_error"], "面板要能看到这次为什么没过")

    def test_wait_for_code_uses_a_code_that_arrived_earlier(self):
        """用户在"正在等放行"那几秒里又敲了一条：不能 clear 掉当没看见。"""
        session = self.session()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        session.submit_code("9876")
        self.assertEqual(session._wait_for_code(_FakeCtx(), self.code_page(), 1.0), "9876")

    def test_wait_for_code_clears_the_event_under_the_lock(self):
        """`_code_event.clear()` 必须在锁里、且早于读值。

        以前 clear 排在"解锁"之后，用户提交验证码正好落在这个窗口（submit_code
        持锁写值 + set 事件）就会被 clear 抹掉：这一轮之后只等事件、不再读值，
        明明填了码却白等满 300 秒再报 code_timeout。
        """
        session = self.session()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        real = session._code_event
        probe = {}

        class _ProbeEvent:
            def clear(self):
                probe["locked"] = session._lock.locked()
                real.clear()

            def set(self):
                real.set()

            def is_set(self):
                return real.is_set()

            def wait(self, timeout=None):
                return real.wait(timeout)

        session._code_event = _ProbeEvent()
        session.submit_code("1234")
        self.assertEqual(session._wait_for_code(_FakeCtx(), self.code_page(), 1.0), "1234")
        self.assertTrue(probe.get("locked"), "clear() 必须在持锁时调用，否则会吞掉并发提交的码")

    def test_wait_for_code_times_out_and_reports_cancel(self):
        session = self.session()
        self.pin_login_cookie_state([False])
        self.assertIsNone(session._wait_for_code(_FakeCtx(), self.code_page(), 0.05))
        session.cancel()
        self.assertIsNone(session._wait_for_code(_FakeCtx(), self.code_page(), 1.0))

    # ---- 掉登录重扫：那份作废的 sessionid 不许冒充"已经放行" ----
    def test_wait_for_code_keeps_waiting_with_a_stale_login_cookie(self):
        """真现场（2026-10-11）：资料目录里躺着上一轮那份 sessionid，名字还在。

        旧的判据 `has_login_cookie`（只看名字）让 `_wait_for_code` 一进这个页面就
        返回 "" —— 面板上永远看不到输入框，可短信已经真发出去了，几十秒后以
        `verify_failed` 收场。名字在、值没换，就必须接着等用户填码。
        """
        session = self.session()
        stale = [{"name": "sessionid", "value": "old"}]
        ctx = _ProbeCtx(cookies=stale)
        session._code_base_sig = qr_login.cookie_utils.login_cookie_signature(stale)
        self.assertIsNone(session._wait_for_code(ctx, self.code_page(), 0.05),
                          "同一份旧 cookie 不算放行，要继续等验证码")

    def test_wait_for_code_notices_a_brand_new_login_cookie(self):
        session = self.session()
        stale = [{"name": "sessionid", "value": "old"}]
        ctx = _ProbeCtx(cookies=stale)
        session._code_base_sig = qr_login.cookie_utils.login_cookie_signature(stale)
        ctx.cookies_now = [{"name": "sessionid", "value": "new"}]
        self.assertEqual(session._wait_for_code(ctx, self.code_page(), 1.0), "",
                         "真换了一份新的 → 还没填码也算已经放行")

    def test_await_login_needs_a_new_cookie_or_the_server(self):
        session = self.session()
        stale = [{"name": "sessionid", "value": "old"}]
        ctx = _ProbeCtx(cookies=stale)
        session._code_base_sig = qr_login.cookie_utils.login_cookie_signature(stale)
        self.pin_check_login({"state": "EXPIRED"})
        self.pin_probe_seconds(0.05)
        self.assertFalse(session._await_login(ctx, self.code_page(), 0.05),
                         "服务端没认、cookie 也没换 → 不能当通过")
        ctx.cookies_now = [{"name": "sessionid", "value": "new"}]
        self.assertTrue(session._await_login(ctx, self.code_page(), 1.0))

    def test_await_login_trusts_the_server_when_the_value_stays_the_same(self):
        """服务端可能就把这一份 cookie 标成已验证、值一个字节都不变。

        那种情况下只有旁路问服务端才问得出来，不然用户明明过了验证码却要重来。
        """
        session = self.session()
        stale = [{"name": "sessionid", "value": "old"}]
        ctx = _ProbeCtx(cookies=stale)
        session._code_base_sig = qr_login.cookie_utils.login_cookie_signature(stale)
        self.pin_check_login({"state": "LOGGED_IN", "user_id": "9"})
        self.assertTrue(session._await_login(ctx, self.code_page(), 1.0))

    def test_solve_code_with_a_stale_cookie_still_asks_for_the_code(self):
        """端到端：带一份作废的 sessionid 进二次验证，也必须先让用户把码填进来。"""
        session = self.session()
        stale = [{"name": "sessionid", "value": "old"}]
        ctx = _ProbeCtx(cookies=stale)
        self.pin_check_login({"state": "LOGGED_IN", "user_id": "7"})
        page = self.code_page()

        def feed():
            time.sleep(0.05)
            session.submit_code("4321")

        threading.Thread(target=feed, daemon=True).start()
        self.assertTrue(session._solve_code(ctx, page))
        self.assertEqual(page.filled, ["4321"], "先把码填进去，而不是当场假通过")

    def test_solve_code_reports_a_timeout_instead_of_hanging(self):
        session = self.session()
        self.pin_login_cookie_state([False])
        original = qr_login.CODE_WAIT_SECONDS
        qr_login.CODE_WAIT_SECONDS = 0.05
        self.addCleanup(setattr, qr_login, "CODE_WAIT_SECONDS", original)
        self.assertFalse(session._solve_code(_FakeCtx(), self.code_page()))
        snap = session.snapshot()
        self.assertEqual(snap["state"], qr_login.STATE_ERROR)
        self.assertEqual(snap["error"], "code_timeout")

    # ---- 页面脚本与接线 ----
    def test_code_page_scripts_are_wired(self):
        for name in ("CODE_PROBE_JS", "CLICK_TEXT_JS", "FILL_CODE_JS"):
            with self.subTest(name=name):
                js = getattr(qr_login, name)
                self.assertIsInstance(js, str)
                self.assertGreater(len(js), 200, "脚本不该被谁删成空壳")
        self.assertIn("placeholder", qr_login.CODE_PROBE_JS, "探针要把输入框的线索带回来")
        self.assertIn("验证码", qr_login.CODE_INPUT_HINTS)
        self.assertIn("dispatchEvent", qr_login.FILL_CODE_JS, "React 需要原生 setter + input 事件")
        self.assertIn("avoid", qr_login.CLICK_TEXT_JS, "「不要点哪个」必须能传进去")

    def test_receive_texts_never_advertise_the_send_path(self):
        for word in qr_login.CODE_RECEIVE_TEXTS:
            with self.subTest(word=word):
                self.assertFalse(any(bad in word for bad in qr_login.CODE_SEND_TEXTS))

    def test_main_loop_hands_the_page_over_to_solve_code(self):
        source = pathlib.Path(qr_login.__file__).read_text(encoding="utf-8")
        self.assertIn("if need_code and not self._solve_code(ctx, page):", source)
        self.assertIn("and not self._scanned", source,
                      "扫上码之后别再让停摆看门狗把验证码页重开掉")

    def test_code_page_is_recognised_before_the_stall_watchdog(self):
        """身份验证页必须比停摆看门狗先被认出来。

        真现场（2026-10-30 复检二次验证那条路时发现的次序漏洞）：风控账号扫完码，
        抖音把二维码换成「身份验证」那一屏。看门狗此时看到的只是「码没了」——25 秒后
        去点「登录」、180 秒后整页重载，用户正填着的验证码页就被撕掉。
        把探测挪到判决之前：认出验证码页就立刻交棒，页面一个字节都不动。
        """
        source = pathlib.Path(qr_login.__file__).read_text(encoding="utf-8")
        loop = source.split("# ③ 等扫码")[1].split("# ③.5")[0]
        self.assertIn("self._detect_code_page(page)", loop)
        self.assertLess(loop.index("self._detect_code_page(page)"),
                        loop.index("_stall_verdict("),
                        "验证码页要在停摆看门狗之前认出来")

    def test_main_loop_never_confirms_login_on_the_qr_page(self):
        """扫码循环里只能用**旁路**确认登录态。

        真现场（2026-10-10）：码刚画出来 3 毫秒，就有人拿 `_verify()` 在扫码页上
        做确认 —— 它会把页面导航去聊天页，用户扫的那张码当场作废（扫了也登不上）。
        """
        source = pathlib.Path(qr_login.__file__).read_text(encoding="utf-8")
        loop = source.split("# ③ 等扫码")[1].split("# ③.5")[0]
        self.assertIn("_probe_logged_in(", loop, "扫码循环必须走旁路确认")
        self.assertNotIn("_verify(", loop,
                         "在扫码页上确认会把二维码整页刷掉，用户扫的码就废了")


class SideTabProbeTests(unittest.TestCase):
    """「这个上下文算登录了吗」必须去**旁边的标签页**问服务端。

    真现场（2026-10-10）：二维码刚画出来 3 毫秒，程序就判定"出现了登录 cookie"，
    把扫码页导航去聊天页做确认 —— 用户扫的那张码当场作废（扫了也登不上）；
    而且那份 cookie 是上一次登录留下的、服务端早就不认的旧货。
    """

    def session(self, **kwargs):
        kwargs.setdefault("ttl", 60.0)
        return qr_login.QrLoginSession("/tmp/does-not-matter", **kwargs)

    def pin_check_login(self, verdict):
        original = qr_login.douyin_im.check_login
        qr_login.douyin_im.check_login = lambda page, mon: dict(verdict)
        self.addCleanup(setattr, qr_login.douyin_im, "check_login", original)

    def pin_probe_seconds(self, seconds):
        original = qr_login.LOGIN_PROBE_SECONDS
        qr_login.LOGIN_PROBE_SECONDS = seconds
        self.addCleanup(setattr, qr_login, "LOGIN_PROBE_SECONDS", original)

    def test_probe_confirms_in_a_side_tab_and_never_touches_the_qr_page(self):
        session = self.session()
        self.pin_check_login({"state": "LOGGED_IN", "user_id": "1"})
        ctx = _ProbeCtx()
        self.assertTrue(session._probe_logged_in(ctx))
        self.assertEqual(len(ctx.opened), 1, "只开一个临时标签页")
        self.assertEqual(ctx.opened[0].goto_urls, [qr_login.douyin_im.CHAT_URL])
        self.assertTrue(ctx.opened[0].closed, "用完就关")
        self.assertEqual(session._verdict.get("user_id"), "1", "结论要留给 ④ 复用")

    def test_probe_says_no_when_the_server_still_rejects_the_session(self):
        session = self.session()
        self.pin_check_login({"state": "EXPIRED"})
        self.pin_probe_seconds(0.05)
        ctx = _ProbeCtx()
        self.assertFalse(session._probe_logged_in(ctx))
        self.assertTrue(ctx.opened[0].closed, "问不出结论也要把标签页关掉")

    def test_probe_swallows_a_broken_page(self):
        session = self.session()
        self.pin_probe_seconds(0.05)
        self.assertFalse(session._probe_logged_in(_ProbeCtx(fail=True)),
                         "旁路确认失败不能把整个扫码会话带走")

    def test_a_stale_login_cookie_is_not_taken_for_a_new_login(self):
        session = self.session()
        stale = [{"name": "sessionid", "value": "old"}]
        session._cookie_sig = qr_login.cookie_utils.login_cookie_signature(stale)
        ctx = _ProbeCtx(cookies=stale)
        self.assertFalse(session._cookie_moved(ctx), "同一份旧 cookie 不算「换了一份」")
        ctx.cookies_now = [{"name": "sessionid", "value": "new"}]
        self.assertTrue(session._cookie_moved(ctx), "真扫上之后 cookie 值会换")
        self.assertFalse(session._cookie_moved(ctx), "同一份指纹只报一次，别反复去开标签页")

    def test_no_login_cookie_at_all_is_not_a_move(self):
        session = self.session()
        self.assertFalse(session._cookie_moved(_ProbeCtx(cookies=[{"name": "ttwid", "value": "1"}])))


class VerifyExitTests(unittest.TestCase):
    """扫码之前那一步（① 资料目录里已有旧登录态）不许干等一整分钟。

    真机实测（2026-10-10，拿用户那份旧 profile 的副本）：新代码把状态从
    「已扫码，正在确认登录态」改成了「正在确认已有登录态」，但面板上要等满
    `VERIFY_TIMEOUT_SECONDS`（60 秒）才出现二维码 —— 用户对着那句话空等一分钟。
    """

    def session(self):
        return qr_login.QrLoginSession("/tmp/does-not-matter", ttl=60.0)

    def pin_check_login(self, states):
        seq = list(states)
        calls = {"n": 0}

        def fake(page, mon):                                     # noqa: ARG001
            index = min(calls["n"], len(seq) - 1)
            calls["n"] += 1
            item = seq[index]
            return dict(item) if isinstance(item, dict) else {"state": item}

        original = qr_login.douyin_im.check_login
        qr_login.douyin_im.check_login = fake
        self.addCleanup(setattr, qr_login.douyin_im, "check_login", original)
        return calls

    def test_a_dead_session_stops_the_pre_check_right_away(self):
        session = self.session()
        calls = self.pin_check_login(["EXPIRED"])                # 服务端明说不认
        started = time.time()
        self.assertFalse(session._verify(_FakePage({}), None, conclusive_exit=True))
        self.assertLess(time.time() - started, 5.0, "别等满 VERIFY_TIMEOUT_SECONDS 才给二维码")
        self.assertEqual(calls["n"], 1, "服务端已经给了结论，一次判定就够")
        self.assertEqual(session._verdict.get("state"), "EXPIRED", "判决要留给 ④ 复用")

    def test_no_cookies_at_all_is_also_conclusive(self):
        session = self.session()
        self.pin_check_login(["NOT_LOGGED_IN"])
        self.assertFalse(session._verify(_FakePage({}), None, conclusive_exit=True))

    def test_the_post_scan_check_still_gives_the_cookie_time_to_land(self):
        """扫码之后（④）不许早退：刚扫上那几秒服务端可能还在认 cookie。"""
        session = self.session()
        calls = self.pin_check_login(["NOT_LOGGED_IN", "LOGGED_IN"])
        self.assertTrue(session._verify(_FakePage({}), None), "默认口径必须等出登录态")
        self.assertEqual(calls["n"], 2)

    def test_only_the_pre_check_asks_for_the_conclusive_exit(self):
        source = pathlib.Path(qr_login.__file__).read_text(encoding="utf-8")
        pre = source.split("# ① 浏览器里可能已经存着登录态")[1].split("# ② 打开登录页")[0]
        post = source.split("# ④ 服务端确认")[1].split("# ---- 收尾")[0]
        self.assertIn("conclusive_exit=True", pre, "① 那一步要早退，不然旧 profile 上要空等 60 秒")
        self.assertNotIn("conclusive_exit=True", post, "④ 早退会把刚到手的登录态误判成失败")


class CloseTests(unittest.TestCase):
    def test_close_uses_the_browser_module_it_imports(self):
        """`_close` 里那个局部 import 漏掉的后果（2026-10-10 实测）：

        `NameError: name 'browser_mod' is not defined` —— 浏览器不关、资料目录不搬，
        会话线程带着 traceback 死掉。这里用桩把这条路径钉住。
        """
        stub = types.ModuleType("core.browser")
        stub.close_context = lambda ctx: False            # 假装关的时候报错
        had = sys.modules.get("core.browser")
        sys.modules["core.browser"] = stub
        self.addCleanup(_restore_module, "core.browser", had)
        import core.browser as real_browser
        real_attr = real_browser.close_context
        real_browser.close_context = stub.close_context
        self.addCleanup(setattr, real_browser, "close_context", real_attr)

        root = tempfile.mkdtemp(prefix="qr-close-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        lock = os.path.join(root, "SingletonLock")
        pathlib.Path(lock).write_text("x", encoding="utf-8")
        session = qr_login.QrLoginSession(root)
        self.assertFalse(session._close(object()),
                         "关不干净 + 资料目录里还有 SingletonLock → 不能当关掉了")
        os.remove(lock)
        self.assertTrue(session._close(object()),
                        "Playwright 常有的假报错：目录里没有锁就当关掉了")
        self.assertTrue(session._close(None), "没有上下文时直接算关干净")


def _restore_module(name, old):
    if old is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = old


class _ProbePage:
    """旁路确认用的假标签页。"""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.goto_urls = []
        self.closed = False
        self.waits = 0

    def goto(self, url, **_kwargs):
        if self.fail:
            raise RuntimeError("net down")
        self.goto_urls.append(url)

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def close(self):
        self.closed = True


class _ProbeCtx:
    """够 `_probe_logged_in` / `_cookie_moved` 用：记得住开过哪些标签页、cookie 现在是什么。"""

    def __init__(self, *, cookies=None, fail=False):
        self.cookies_now = list(cookies or [])
        self.fail = fail
        self.opened = []

    def cookies(self):
        return list(self.cookies_now)

    def new_page(self):
        page = _ProbePage(fail=self.fail)
        self.opened.append(page)
        return page


class _FakeKeyboard:
    def __init__(self):
        self.presses = []

    def press(self, key):
        self.presses.append(key)


class _FakePage:
    """够用的假页面：按传入的 JS 常量分派，记下填进去的码与点过的按钮。"""

    def __init__(self, probe):
        self.probe = probe
        self.filled = []
        self.clicked = []
        self.waits = 0
        self.keyboard = _FakeKeyboard()
        # 严格模式下"真正能点"的文案。默认页面上列出的按钮都算能点；
        # 要模拟"这只是一段正文"就把对应文案从这里去掉。
        self.clickables = list(probe.get("buttons") or [])

    def evaluate(self, js, arg=None):
        if js is qr_login.CODE_PROBE_JS:
            return dict(self.probe)
        if js is qr_login.CLICK_TEXT_JS:
            want = list((arg or {}).get("texts") or [])
            avoid = list((arg or {}).get("avoid") or [])
            tags = list((arg or {}).get("tags") or [])
            for word in want:
                for text in self.probe.get("buttons") or []:
                    if word in text and not any(bad in text for bad in avoid):
                        if tags and text not in self.clickables:
                            continue
                        self.clicked.append(text)
                        return {"ok": True, "clicked": text}
            return {"ok": False, "clicked": "", "seen": list(self.probe.get("buttons") or [])}
        if js is qr_login.FILL_CODE_JS:
            self.filled.append(str(arg))
            return {"ok": True, "placeholder": "请输入验证码", "score": 100}
        return None

    def screenshot(self, **_kwargs):
        return b"\x89PNG-fake-%d" % len(self.filled)

    def wait_for_timeout(self, _ms):
        self.waits += 1
        time.sleep(0.005)


class _FakeCtx:
    def cookies(self):
        return []


class WatchdogTests(unittest.TestCase):
    """守夜线程：卡死的会话必须有人收尾（2026-10-17 审计 S1）。

    卡死的典型形态：浏览器停止响应，`page.evaluate` / `ctx.cookies()` 永远不返回
    （这两个调用没有超时参数），`cancel()` 置的旗它收不到。以前面板只能把会话
    记录摘掉，于是下一次登录又去抢同一个 user-data-dir（SingletonLock 冲突）。
    """

    def make(self, **kwargs):
        kwargs.setdefault("ttl", 0.1)
        return qr_login.QrLoginSession("/tmp/watchdog-profile", **kwargs)

    def test_a_stuck_session_is_force_killed(self):
        from unittest import mock

        session = self.make()
        # 早就过了 TTL + 守夜宽限（宽限本身是分钟级，测试里直接把起点往前挪）
        session._started_at = time.time() - (qr_login.WATCHDOG_GRACE_SECONDS + 10)
        killed = []
        with mock.patch("core.browser.kill_profile_processes",
                        side_effect=lambda path, **kw: (killed.append(path), 1)[1]):
            session._start_watchdog()
            deadline = time.time() + 8
            while time.time() < deadline and not session._forced:
                time.sleep(0.2)
        self.assertTrue(session._forced, "守夜线程没有强制收尾")
        self.assertEqual(killed, ["/tmp/watchdog-profile"])
        snap = session.snapshot()
        self.assertEqual(snap["state"], qr_login.STATE_ERROR)
        self.assertFalse(snap["active"], "强制收尾之后不许再算进行中")
        self.assertIn("卡住", snap["message"])

    def test_a_finished_session_is_left_alone(self):
        from unittest import mock

        session = self.make()
        session._started_at = time.time() - (qr_login.WATCHDOG_GRACE_SECONDS + 10)
        session._finished_at = time.time()
        killed = []
        with mock.patch("core.browser.kill_profile_processes",
                        side_effect=lambda path, **kw: killed.append(path)):
            session._start_watchdog()
            time.sleep(2.6)
        self.assertEqual(killed, [], "已经结束的会话不该被强杀")
        self.assertEqual(session._forced, "")

    def test_cancel_shortens_the_budget(self):
        """取消之后只等 CANCEL_GRACE_SECONDS，不必等到 TTL + 宽限。"""
        from unittest import mock

        session = self.make(ttl=3600.0)
        session._started_at = time.time() - 1
        session.cancel()
        self.assertGreater(session._cancel_at, 0.0, "cancel() 要记下时刻")
        session._cancel_at = time.time() - (qr_login.CANCEL_GRACE_SECONDS + 5)
        killed = []
        with mock.patch("core.browser.kill_profile_processes",
                        side_effect=lambda path, **kw: (killed.append(path), 1)[1]):
            session._start_watchdog()
            deadline = time.time() + 8
            while time.time() < deadline and not session._forced:
                time.sleep(0.2)
        self.assertTrue(session._forced)
        self.assertIn("取消", session._forced)

    def test_the_human_code_stage_gets_its_own_budget(self):
        """人工填码阶段不该被二维码 TTL 掐断。

        旧判据是 `_started_at + ttl + 420s`：第 290 秒才扫码的人刚进验证码页就
        被判"会话卡住"强杀，CODE_MAX_ATTEMPTS 的三次机会一次都轮不上。
        """
        from unittest import mock

        session = self.make()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        session._started_at = time.time() - (qr_login.WATCHDOG_GRACE_SECONDS + 10)
        killed = []
        with mock.patch("core.browser.kill_profile_processes",
                        side_effect=lambda path, **kw: killed.append(path)):
            session._start_watchdog()
            time.sleep(2.6)
        self.assertEqual(killed, [], "等人填验证码的时候不许强杀")
        self.assertEqual(session._forced, "")
        self.assertGreater(session._human_since, 0.0)

        # 收尾：留一个活着的守夜线程会每 2 秒醒一次，后面有测试把 time.sleep
        # 打成 mock 在计数（test_tasks_fusion 的间隔测试）时，这个线程会把计数
        # 刷到几十万 —— 这里明确让守夜线程退出。
        session._finished_at = time.time()
        session._closing = True

    def test_the_human_stage_is_still_bounded(self):
        """独立预算是"更长"，不是"无限"：三轮回合耗光后照样收尾。"""
        from unittest import mock

        session = self.make()
        session._set(qr_login.STATE_WAITING_CODE, "等你填验证码")
        session._started_at = time.time() - (qr_login.WATCHDOG_GRACE_SECONDS + 10)
        session._human_since = time.time() - (
            qr_login.CODE_MAX_ATTEMPTS * (qr_login.CODE_WAIT_SECONDS + qr_login.CODE_VERIFY_SECONDS)
            + qr_login.WATCHDOG_GRACE_SECONDS + 10)
        killed = []
        with mock.patch("core.browser.kill_profile_processes",
                        side_effect=lambda path, **kw: (killed.append(path), 1)[1]):
            session._start_watchdog()
            deadline = time.time() + 8
            while time.time() < deadline and not session._forced:
                time.sleep(0.2)
        self.assertTrue(session._forced, "人工阶段也必须有个上限")
        self.assertIn("超过时限", session._forced)

    def test_kill_failure_does_not_escape(self):
        """守夜线程自己不许抛：杀进程失败也要把会话置成明确的错误态。"""
        from unittest import mock

        session = self.make()
        with mock.patch("core.browser.kill_profile_processes",
                        side_effect=RuntimeError("boom")):
            session._force_kill("测试原因")            # 不许抛
        self.assertEqual(session._forced, "测试原因")
        self.assertEqual(session.snapshot()["state"], qr_login.STATE_ERROR)


class KillProfileProcessesTests(unittest.TestCase):
    """`core.browser.kill_profile_processes`：按 `--user-data-dir=` 找进程再杀。"""

    def test_an_unknown_profile_is_a_noop(self):
        from core.browser import kill_profile_processes

        self.assertEqual(kill_profile_processes("/tmp/definitely-not-running-profile"), 0)

    def test_empty_path_is_a_noop(self):
        from core.browser import kill_profile_processes

        self.assertEqual(kill_profile_processes(""), 0)


class GotoTimeoutBudgetTests(unittest.TestCase):
    """自愈重载的超时预算：常量必须存在，且按剩余预算收缩、不越过下限。

    ★ 回归（GitHub issue #1）：`MIN_GOTO_TIMEOUT_MS` 曾经只被引用、没有定义，
    `_goto_timeout_ms(deadline)` 一调用就抛 NameError，而调用点把异常吞进
    「自愈时重新载入登录页超时」的 warning 里 ⇒ 自愈从来没真正跑过。
    """

    def test_下限常量有定义且是正数(self) -> None:
        self.assertIsInstance(qr_login.MIN_GOTO_TIMEOUT_MS, int)
        self.assertGreater(qr_login.MIN_GOTO_TIMEOUT_MS, 0)
        self.assertLessEqual(qr_login.MIN_GOTO_TIMEOUT_MS, qr_login.GOTO_TIMEOUT_MS)

    def test_没有截止时刻就用默认超时(self) -> None:
        self.assertEqual(qr_login._goto_timeout_ms(None), qr_login.GOTO_TIMEOUT_MS)

    def test_预算充足时用默认超时(self) -> None:
        self.assertEqual(qr_login._goto_timeout_ms(time.time() + 3600),
                         qr_login.GOTO_TIMEOUT_MS)

    def test_预算不足时收缩到剩余(self) -> None:
        got = qr_login._goto_timeout_ms(time.time() + 30)
        self.assertLess(got, qr_login.GOTO_TIMEOUT_MS)
        self.assertGreater(got, qr_login.MIN_GOTO_TIMEOUT_MS)

    def test_预算耗尽也不低于下限(self) -> None:
        self.assertEqual(qr_login._goto_timeout_ms(time.time() - 10),
                         qr_login.MIN_GOTO_TIMEOUT_MS)

    def test_源码里确实定义了常量(self) -> None:
        """源码级守卫：光有引用没有定义也能过前几条（别处可能碰巧有同名变量）。"""
        src = (pathlib.Path(ROOT) / "core" / "qr_login.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"(?m)^MIN_GOTO_TIMEOUT_MS\s*=\s*\d")


class _SnapshotCtx:
    """假上下文：cookie 里混一条别的域，origins 里带设备身份。"""

    def storage_state(self):
        return {
            "cookies": [
                {"name": "sessionid", "value": "s3", "domain": ".douyin.com", "path": "/"},
                {"name": "x-web-secsdk-uid", "value": "283d8355",
                 "domain": ".douyin.com", "path": "/"},
                {"name": "tracker", "value": "no", "domain": ".example.com", "path": "/"},
            ],
            "origins": [{
                "origin": "https://www.douyin.com",
                "localStorage": [
                    {"name": "web_runtime_security_uid",
                     "value": "283d8355-d3ed-418c-9d8c-30068c151a9f"},
                    {"name": "__msuuid__", "value": "89ab5ee2-0982-402e-ac3b-34a7937ee387"},
                ],
            }],
        }


class _AccountPage:
    """假页面：`window.__dySelfHook` 里已经有账号资料。"""

    def __init__(self, unique_id="10050yi", nickname="测试号"):
        self.unique_id = unique_id
        self.nickname = nickname

    def evaluate(self, script, *args):
        if "__dySelfHook" in str(script):
            user = {"nickname": self.nickname}
            if self.unique_id:
                user["unique_id"] = self.unique_id
            return {"profile": {"status_code": 0, "user": user}}
        return "Mozilla/5.0 (test)"

    def wait_for_timeout(self, ms):
        return None


class LoginDeviceSnapshotTests(unittest.TestCase):
    """P4：登录成功后顺带保存"登录这一台设备"的浏览器快照。

    真机实测（2026-10-22）：扫码登录的资料目录与每天发送的上下文是两套设备
    身份 —— `web_runtime_security_uid` 分别是 283d8355… 与 21d1e1d3…。
    登录时把整份 storage_state 存成发送快照，之后的任务才会沿用登录这一台。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.state_dir = os.path.join(self.root, "browser-state")
        old = os.environ.get("BROWSER_STATE_DIR")
        os.environ["BROWSER_STATE_DIR"] = self.state_dir
        self.addCleanup(self._restore_env, old)

    @staticmethod
    def _restore_env(old):
        if old is None:
            os.environ.pop("BROWSER_STATE_DIR", None)
        else:
            os.environ["BROWSER_STATE_DIR"] = old

    def make(self):
        profile = os.path.join(self.root, "default")
        os.makedirs(os.path.join(profile, "Default"), exist_ok=True)
        with open(os.path.join(profile, "Default", "Cookies"), "w", encoding="utf-8") as fh:
            fh.write("x")
        return qr_login.QrLoginSession(profile)

    def test_登录成功要留下发送快照(self):
        session = self.make()
        session._finish(_SnapshotCtx(), _AccountPage(), None)
        path = os.path.join(self.state_dir, "10050yi.json")
        self.assertTrue(os.path.isfile(path), "登录成功要留下发送快照")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        names = [cookie["name"] for cookie in saved["cookies"]]
        self.assertIn("sessionid", names)
        self.assertIn("x-web-secsdk-uid", names, "服务端签发的设备 cookie 要带过去")
        self.assertNotIn("tracker", names, "非抖音域的 cookie 不落盘")
        self.assertEqual(saved["origins"][0]["origin"], "https://www.douyin.com")
        self.assertTrue(
            any(item["name"] == "web_runtime_security_uid"
                for item in saved["origins"][0]["localStorage"]),
            "localStorage 里的设备身份才是这一层的重点")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(browser_state.same_session(saved, session.result["cookies"]),
                        "快照与这次登录的 cookie 必须被认成同一份会话")

    def test_没有抖音号就不写快照(self):
        session = self.make()
        session._finish(_SnapshotCtx(), _AccountPage(unique_id=""), None)
        self.assertEqual(session.snapshot()["state"], qr_login.STATE_DONE)
        self.assertEqual(os.listdir(self.state_dir) if os.path.isdir(self.state_dir) else [], [],
                         "不知道存给谁，就别留下一个会串号的快照")

    def test_抖音号不合法就不碰磁盘(self):
        session = self.make()
        session._finish(_SnapshotCtx(), _AccountPage(unique_id="../etc/passwd"), None)
        self.assertEqual(session.snapshot()["state"], qr_login.STATE_DONE)
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc", "passwd")))

    def test_写快照失败不影响登录结果(self):
        session = self.make()
        with mock.patch.object(qr_login.browser_state, "write",
                               side_effect=RuntimeError("boom")):
            with self.assertLogs("qr_login", level="WARNING") as logs:
                session._finish(_SnapshotCtx(), _AccountPage(), None)
        self.assertEqual(session.snapshot()["state"], qr_login.STATE_DONE)
        self.assertTrue(session.result["logged_in"])
        self.assertTrue(any("保存登录设备快照失败" in line for line in logs.output),
                        logs.output)


if __name__ == "__main__":
    unittest.main()
