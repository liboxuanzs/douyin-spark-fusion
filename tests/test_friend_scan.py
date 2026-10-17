"""`core.friend_scan` 的离线测试：只读列表、状态机、取消、纯 stdlib import。

浏览器与 `DouyinIM` 全部用替身（这个模块的价值在于"把列表读出来"，
真正的滚动逻辑在 `core/douyin_im.py` 里另有测试）。
"""
import subprocess
import sys
import unittest

from core import friend_scan

# 面板真的会读这些键（webui/app.py 的 friend_view() / dashboard.html）
SNAPSHOT_KEYS = (
    "state", "state_label", "message", "error", "count", "active",
    "started_at", "updated_at", "finished_at", "age_seconds",
    "profile_dir", "fingerprint", "owner",
    "scanned_all", "stopped", "visited", "steps", "note", "truncated", "groups",
)


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False
        self.timeout = None

    def set_default_navigation_timeout(self, ms):
        self.timeout = ms

    def new_page(self):
        raise AssertionError("已经有 page 了，不该再开一个")

    def close(self):
        self.closed = True


class _FakeIM:
    def __init__(self, page, *, status="READY", items=(), scan=None, boom=None, **kw):
        self.page = page
        self.kw = kw
        self.status = status
        self._items = list(items)
        self.last_scan = dict(scan or {"scanned_all": True, "stopped": "reach-bottom",
                                       "visited": len(items), "steps": 3, "note": ""})
        self._boom = boom
        self.ready_called = 0

    def wait_ready(self):
        self.ready_called += 1
        return {"status": self.status}

    def iter_conversations(self):
        for item in self._items:
            if self._boom:
                raise self._boom
            yield item


class _Sandbox:
    """把 core.browser / core.douyin_im 换成替身（两处都要塞，见 tests/test_webui.py 的教训）。"""

    def __init__(self, im_items=(), status="READY", scan=None, boom=None,
                 launch_error=None, fake_im_cls=None, on_close=None):
        self.ctx = None
        self.im_kwargs = {}
        self.launch_kwargs = {}
        self._items = im_items
        self._status = status
        self._scan = scan
        self._boom = boom
        self._launch_error = launch_error
        self._fake_im_cls = fake_im_cls
        # 关浏览器那一刻的回调：用来钉住"终态必须晚于 close_context"（见测试）
        self._on_close = on_close
        self._saved = {}

    def __enter__(self):
        import core
        import core.browser
        import core.douyin_im

        sandbox = self

        class _Browser:
            @staticmethod
            def get_config():
                return {"proxyAddress": ""}

            @staticmethod
            def launch_persistent_context(profile_dir, **kw):
                sandbox.launch_kwargs = dict(kw, profile_dir=profile_dir)
                if sandbox._launch_error:
                    raise sandbox._launch_error
                sandbox.ctx = _FakeContext(object())
                return sandbox.ctx

            @staticmethod
            def close_context(target):
                """core.browser.close_context 的替身：关对象 + 还 playwright 句柄。"""
                if sandbox._on_close is not None:
                    sandbox._on_close()
                if hasattr(target, "close"):
                    target.close()
                return True

        class _IM(_FakeIM if sandbox._fake_im_cls is None else sandbox._fake_im_cls):
            pass

        if sandbox._fake_im_cls is None:
            def _factory(page, **kw):
                sandbox.im_kwargs = kw
                return _FakeIM(page, status=sandbox._status, items=sandbox._items,
                               scan=sandbox._scan, boom=sandbox._boom, **kw)

            _IM = _factory
            _IM.STATUS_READY = "READY"
        else:
            _IM.STATUS_READY = "READY"

        fake_browser = type(sys)("core.browser")
        fake_browser.get_config = _Browser.get_config
        fake_browser.launch_persistent_context = _Browser.launch_persistent_context
        fake_browser.close_context = _Browser.close_context

        fake_im = type(sys)("core.douyin_im")
        fake_im.STATUS_READY = "READY"
        fake_im.DouyinIM = _IM

        for name, module in (("core.browser", fake_browser), ("core.douyin_im", fake_im)):
            self._saved[name] = (sys.modules.get(name), getattr(core, name.split(".")[1], None))
            sys.modules[name] = module
            setattr(core, name.split(".")[1], module)
        return self

    def __exit__(self, *exc):
        import core
        for name, (mod, attr) in self._saved.items():
            short = name.split(".")[1]
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
            if attr is None:
                if hasattr(core, short):
                    delattr(core, short)
            else:
                setattr(core, short, attr)
        return False


def _conv(i, **extra):
    item = {"conv_id": f"0:1:999:{2000 + i}", "display": f"U{i}", "title": f"U{i}",
            "remark": "", "nickname": "", "douyin_id": "", "uid": str(2000 + i),
            "sec_uid": "", "is_group": False, "unread": 0, "is_muted": False}
    item.update(extra)
    return item


class PublicItemTests(unittest.TestCase):
    def test_whitelist_only(self):
        raw = dict(_conv(1), extra_secret="不该带出去", peer_uid="999")
        item = friend_scan.public_item(raw)
        self.assertEqual(set(item), {
            "name", "title", "remark", "nickname", "douyin_id", "uid", "sec_uid",
            "conv_id", "is_group", "unread", "is_muted"})
        self.assertNotIn("extra_secret", item)

    def test_name_falls_back_through_the_chain(self):
        self.assertEqual(friend_scan.public_item({"conv_id": "c", "remark": "备注"})["name"], "备注")
        self.assertEqual(friend_scan.public_item({"conv_id": "c", "nickname": "昵称"})["name"], "昵称")
        self.assertEqual(friend_scan.public_item({"conv_id": "c", "title": "标题"})["name"], "标题")
        self.assertEqual(friend_scan.public_item({"conv_id": "c"})["name"], "c")

    def test_garbage_returns_none(self):
        for bad in (None, "字符串", 3, {}, {"conv_id": "  ", "display": "  "}):
            self.assertIsNone(friend_scan.public_item(bad), bad)

    def test_flags_are_bools(self):
        item = friend_scan.public_item(dict(_conv(1), is_group=1, unread="x", is_muted=None))
        self.assertIs(item["is_group"], True)
        self.assertIs(item["unread"], True)
        self.assertIs(item["is_muted"], False)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.s = friend_scan.FriendScanSession("/tmp/p", fingerprint="17022", owner="12345678905")

    def test_initial_state(self):
        snap = self.s.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_STARTING)
        self.assertTrue(snap["active"])
        self.assertEqual(snap["count"], 0)
        self.assertEqual(snap["owner"], "12345678905")
        self.assertEqual(snap["fingerprint"], "17022")

    def test_panel_reads_these_keys(self):
        self.assertEqual(set(self.s.snapshot()), set(SNAPSHOT_KEYS))

    def test_labels_are_chinese(self):
        for state, label in friend_scan.STATE_LABELS.items():
            self.assertTrue(label.strip(), state)
        self.assertIn("读取完成", friend_scan.STATE_LABELS[friend_scan.STATE_DONE])

    def test_cancel_waits_for_the_browser_to_close(self):
        """取消只发信号，**终态留给 run() 收尾**。

        提前置成 cancelled 会让面板以为资料目录已经空出来（`active` 是判断依据），
        同一个账号立刻能再起一个 Chromium 去抢同一个 user-data-dir。
        """
        self.assertTrue(self.s.cancel())
        snap = self.s.snapshot()
        self.assertTrue(self.s.cancelled())
        self.assertTrue(snap["active"])                     # 浏览器还没关，仍占着
        self.assertNotEqual(snap["state"], friend_scan.STATE_CANCELLED)
        self.assertIn("取消", snap["message"])
        # 收尾阶段（run() 的 finally 之后）才落到终态
        self.s._set(friend_scan.STATE_CANCELLED, "已取消")
        snap = self.s.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_CANCELLED)
        self.assertFalse(snap["active"])
        self.assertGreater(snap["finished_at"], 0)
        self.assertFalse(self.s.cancel())                  # 已经是终态 → 返回 False

    def test_cancel_is_idempotent_while_stopping(self):
        self.assertTrue(self.s.cancel())
        self.assertTrue(self.s.cancel())                   # 还没到终态，可以重复点
        self.assertTrue(self.s.cancelled())

    def test_set_error_and_scan(self):
        self.s._set(friend_scan.STATE_ERROR, "坏了", error="ValueError: x")
        snap = self.s.snapshot()
        self.assertEqual(snap["error"], "ValueError: x")
        self.assertFalse(snap["active"])

    def test_items_are_copies(self):
        self.s._items = [{"name": "A"}]
        got = self.s.items()
        got[0]["name"] = "改了"
        self.assertEqual(self.s.items()[0]["name"], "A")

    def test_groups_counted(self):
        self.s._items = [{"is_group": True}, {"is_group": False}, {"is_group": True}]
        self.assertEqual(self.s.snapshot()["groups"], 2)

    def test_ready_error_speaks_human(self):
        cases = {
            "LOGGED_OUT": "还没登录",
            "EXPIRED": "登录态已失效",
            "LOGIN_LOST": "掉登录",
            "TIMEOUT": "超时",
        }
        for status, token in cases.items():
            self.assertIn(token, self.s._ready_error({"status": status}), status)
        self.assertIn("未知状态", self.s._ready_error({}))


class RunTests(unittest.TestCase):
    def _run(self, sandbox, **kw):
        session = friend_scan.FriendScanSession("/tmp/p", fingerprint="17022", owner="acc1")
        with sandbox:
            session.run()                       # 直接跑，不起线程（结果确定）
        return session

    def test_happy_path_collects_items(self):
        items = [_conv(0), _conv(1), _conv(2, is_group=True)]
        with _Sandbox(im_items=items) as box:
            session = friend_scan.FriendScanSession("/tmp/p", fingerprint="17022", owner="acc1")
            session.run()
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_DONE)
        self.assertEqual(snap["count"], 3)
        self.assertEqual(snap["groups"], 1)
        self.assertTrue(snap["scanned_all"])
        self.assertIn("读到 3 个会话", snap["message"])
        self.assertEqual([it["name"] for it in session.items()], ["U0", "U1", "U2"])
        self.assertTrue(box.ctx.closed)          # 浏览器必须关掉

    def test_duplicate_conv_ids_are_merged(self):
        items = [_conv(0), dict(_conv(0))]
        with _Sandbox(im_items=items):
            session = friend_scan.FriendScanSession("/tmp/p")
            session.run()
        self.assertEqual(session.snapshot()["count"], 1)

    def test_gate_failure_reports_error(self):
        with _Sandbox(status="EXPIRED"):
            session = friend_scan.FriendScanSession("/tmp/p")
            session.run()
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_ERROR)
        self.assertIn("登录态已失效", snap["message"])
        self.assertEqual(snap["error"], "gate=EXPIRED")
        self.assertEqual(snap["count"], 0)

    def test_terminal_state_waits_for_the_browser_to_close(self):
        """终态必须在 `close_context()` **之后**才置。

        面板用 `active`（＝state 还在 ACTIVE_STATES 里）判断"资料目录有没有被占着"。
        以前 STATE_DONE / STATE_ERROR 是在 try 体里置的、早于 `close_context()` ——
        "扫完了"和"目录真的空出来"之间有一个窗口，用户在这段窗口里点扫码登录，
        第二个 Chromium 会去抢同一个 user-data-dir（SingletonLock），谁也开不起来。
        """
        holder = {}

        def _at_close():
            snap = session.snapshot()
            holder["state"] = snap["state"]
            holder["active"] = snap["active"]

        with _Sandbox(im_items=[_conv(0)], on_close=_at_close):
            session = friend_scan.FriendScanSession("/tmp/p")
            session.run()
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_DONE)
        self.assertFalse(snap["active"])
        # 关浏览器那一刻：还没到终态、仍算"占着目录"
        self.assertNotIn(holder.get("state"), friend_scan.TERMINAL_STATES)
        self.assertTrue(holder.get("active"), "面板会在这一瞬间以为目录空出来了")

    def test_launch_failure_reports_error(self):
        box = _Sandbox(launch_error=RuntimeError("浏览器启动失败 —— 没有这个可执行文件"))
        with box:
            session = friend_scan.FriendScanSession("/tmp/p")
            session.run()
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_ERROR)
        self.assertIn("浏览器启动失败", snap["message"])
        self.assertIn("RuntimeError", snap["error"])

    def test_scan_exception_reports_error(self):
        with _Sandbox(im_items=[_conv(0)], boom=ValueError("页面炸了")):
            session = friend_scan.FriendScanSession("/tmp/p")
            session.run()
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_ERROR)
        self.assertIn("ValueError", snap["message"])

    def test_not_scanned_all_is_flagged(self):
        scan = {"scanned_all": False, "stopped": "max-steps", "visited": 900,
                "steps": 400, "note": "未覆盖折叠组"}
        with _Sandbox(im_items=[_conv(0), _conv(1)], scan=scan):
            session = friend_scan.FriendScanSession("/tmp/p")
            session.run()
        snap = session.snapshot()
        self.assertFalse(snap["scanned_all"])
        self.assertIn("没扫到底", snap["message"])
        self.assertEqual(snap["note"], "未覆盖折叠组")

    def test_cancel_during_scan_stops_early(self):
        items = [_conv(i) for i in range(30)]
        session = friend_scan.FriendScanSession("/tmp/p")

        class _IM(_FakeIM):
            def __init__(inner, page, **kw):
                super().__init__(page, items=items, **kw)

            def iter_conversations(inner):
                for i, item in enumerate(inner._items):
                    if i == 2:
                        session.cancel()
                    yield item

        with _Sandbox(fake_im_cls=_IM):
            session.run()
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_CANCELLED)
        self.assertEqual(snap["count"], 0)       # 取消后不落地任何结果

    def test_profile_dir_and_proxy_reach_the_browser(self):
        box = _Sandbox(im_items=[_conv(0)])
        with box:
            session = friend_scan.FriendScanSession("/tmp/p", fingerprint="17022",
                                                    proxy="http://127.0.0.1:1080")
            session.run()
        self.assertEqual(box.launch_kwargs["profile_dir"], "/tmp/p")
        self.assertEqual(box.launch_kwargs["fingerprint"], "17022")
        self.assertEqual(box.launch_kwargs["proxy"], "http://127.0.0.1:1080")
        self.assertIs(box.launch_kwargs["headless"], True)

    def test_scan_budget_matches_configtool(self):
        """与 configTool 的好友选择器同口径 —— 两边扫的是同一个页面。"""
        self.assertEqual(friend_scan.READY_TIMEOUT_SECONDS, 45.0)
        self.assertEqual(friend_scan.SETTLE_MS, 800)
        self.assertEqual(friend_scan.MAX_STEPS, 400)
        self.assertGreaterEqual(friend_scan.DEFAULT_TIMEOUT_SECONDS, 180.0)
        self.assertGreater(friend_scan.MAX_ITEMS, 0)


class PurityTests(unittest.TestCase):
    def test_import_does_not_pull_browser_stack(self):
        code = (
            "import sys; import core.friend_scan;"
            "bad=[m for m in ('playwright','cloakbrowser','tkinter','fastapi') if m in sys.modules];"
            "print('|'.join(bad))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=".", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "", out.stdout)

    def test_start_session_returns_a_started_thread(self):
        """`start_session()` 必须真的能 start()。

        这条钉住一个真实 bug：`FriendScanSession.__init__` 里曾经把起始时间存进
        `self._started` —— 那是 `threading.Thread` 自己的 Event，被覆盖后
        `Thread.start()` 直接在 `self._started.is_set()` 抛 AttributeError，
        面板点「读取好友列表」就会 500。
        """
        with _Sandbox(im_items=[_conv(0)]):
            session = friend_scan.start_session("/tmp/p", owner="acc1")
            session.join(10)
        self.assertFalse(session.is_alive())          # 真的跑起来了（不是卡住）
        snap = session.snapshot()
        self.assertEqual(snap["state"], friend_scan.STATE_DONE)
        self.assertEqual(snap["owner"], "acc1")
        self.assertEqual(snap["count"], 1)


if __name__ == "__main__":
    unittest.main()
