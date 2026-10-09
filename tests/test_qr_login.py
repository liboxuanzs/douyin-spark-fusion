"""`core.qr_login` 的可离线测试部分。

真跑一次扫码需要浏览器和一台手机，所以这里只钉三类东西：
  * 面板赖以显示的状态机（快照字段、二维码序号只在换图时递增、取消语义）；
  * `import` 这一层不许拖进浏览器依赖（面板进程 import 它是安全的）；
  * 页面脚本的挑选规则（纯字符串，确认选择器没被谁改跑偏）。
真正"能不能扫出码"由 .172 上的真机验证负责。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import qr_login  # noqa: E402


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
        session.adopt_profile(os.path.join(self.root, "76998876121"))
        session._adopt_profile()
        self.assertTrue(os.path.isdir(os.path.join(self.root, "76998876121", "Default")))
        self.assertFalse(os.path.isdir(os.path.join(self.root, "default")))
        self.assertEqual(session.profile_dir, os.path.join(self.root, "76998876121"))
        self.assertEqual(session.moved_to, os.path.join(self.root, "76998876121"))
        self.assertTrue(session.persist["profile_moved"]["ok"])

    def test_never_overwrites_an_existing_account_dir(self):
        session = self.make()
        target = os.path.join(self.root, "76998876121")
        os.makedirs(os.path.join(target, "Default"), exist_ok=True)
        with open(os.path.join(target, "Default", "Cookies"), "w", encoding="utf-8") as fh:
            fh.write("原来的登录态")
        session.adopt_profile(target)
        session._adopt_profile()
        with open(os.path.join(target, "Default", "Cookies"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "原来的登录态", "已存在的目录一个字节都不许动")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "default")), "源目录留着")
        self.assertFalse(session.persist["profile_moved"]["ok"])
        self.assertIn("已存在", session.persist["profile_moved"]["error"])

    def test_same_path_is_a_noop(self):
        session = self.make()
        session.adopt_profile(session.profile_dir)
        session._adopt_profile()
        self.assertEqual(session.moved_to, "")
        self.assertIsNone(session.persist)

    def test_missing_source_is_reported_not_raised(self):
        session = qr_login.QrLoginSession(
            os.path.join(self.root, "gone"), on_success=lambda result: {"ok": True})
        session.adopt_profile(os.path.join(self.root, "76998876121"))
        session._adopt_profile()          # 不该抛
        self.assertFalse(session.persist["profile_moved"]["ok"])
        self.assertIn("不存在", session.persist["profile_moved"]["error"])

    def test_persist_can_ask_for_the_move(self):
        """面板走的路径：on_success 返回 adopt_profile，_finish 记下来。"""
        session = self.make(
            on_success=lambda result: {
                "ok": True, "profile_dir": result.get("profile_dir"),
                "adopt_profile": os.path.join(self.root, "76998876121"),
            })
        session._finish(_Ctx(), _Page(), None)
        self.assertEqual(session._adopt, os.path.join(self.root, "76998876121"))

    def test_keep_existing_persist_keys(self):
        session = self.make(
            on_success=lambda result: {
                "ok": True, "key": "COOKIES_1",
                "adopt_profile": os.path.join(self.root, "76998876121"),
            })
        session._finish(_Ctx(), _Page(), None)
        session._adopt_profile()
        self.assertEqual(session.persist["key"], "COOKIES_1", "原来写的键不能丢")
        self.assertTrue(session.persist["profile_moved"]["ok"])


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
        # 后端给个不存在的路径：必须抛 RuntimeError 且带人话，而不是把 traceback 甩给面板
        with self.assertRaises(RuntimeError) as ctx:
            qr_login._launch_context("/tmp/definitely-not-a-browser-dir", args=["--nope"], proxy=None)
        self.assertIn("浏览器启动失败", str(ctx.exception))


class CodeVerifyTests(unittest.TestCase):
    """二次验证：风控账号扫码之后还要填一条短信验证码。

    真机那一步要一台手机，所以这里钉的是"这条码能不能收进来、填进去、
    状态机会不会卡死" —— 页面本身用假 Playwright 对象顶替。
    """

    def session(self, **kwargs):
        kwargs.setdefault("ttl", 60.0)
        return qr_login.QrLoginSession("/tmp/does-not-matter", **kwargs)

    def pin_logged_in(self, state):
        """把 has_login_cookie 换成一个由 state[0] 驱动的开关。"""
        original = qr_login.cookie_utils.has_login_cookie
        qr_login.cookie_utils.has_login_cookie = lambda cookies: bool(state[0])
        self.addCleanup(setattr, qr_login.cookie_utils, "has_login_cookie", original)

    def pin_verify_seconds(self, seconds):
        original = qr_login.CODE_VERIFY_SECONDS
        qr_login.CODE_VERIFY_SECONDS = seconds
        self.addCleanup(setattr, qr_login, "CODE_VERIFY_SECONDS", original)

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

    # ---- 走完流程 ----
    def test_solve_code_picks_receive_sms_then_fills_and_waits(self):
        session = self.session()
        state = [False]
        self.pin_logged_in(state)
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
        self.pin_logged_in([False])              # 永远不放行
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

    def test_wait_for_code_times_out_and_reports_cancel(self):
        session = self.session()
        self.pin_logged_in([False])
        self.assertIsNone(session._wait_for_code(_FakeCtx(), self.code_page(), 0.05))
        session.cancel()
        self.assertIsNone(session._wait_for_code(_FakeCtx(), self.code_page(), 1.0))

    def test_solve_code_reports_a_timeout_instead_of_hanging(self):
        session = self.session()
        self.pin_logged_in([False])
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


if __name__ == "__main__":
    unittest.main()
