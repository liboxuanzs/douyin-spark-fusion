"""`do_user_task` 收尾把浏览器状态存下来、下一轮装回去（P1 的接线层）。"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

if "cloakbrowser" not in sys.modules:      # core.browser 是模块级 import cloakbrowser
    _fake_cb = types.ModuleType("cloakbrowser")
    _fake_cb.launch = lambda **kw: None
    _fake_cb._fusion_fake = True
    sys.modules["cloakbrowser"] = _fake_cb

from core import browser_state                 # noqa: E402
from core import tasks as tasks_mod            # noqa: E402

UID = "11223344556"


def tearDownModule():
    if getattr(sys.modules.get("cloakbrowser"), "_fusion_fake", False):
        sys.modules.pop("cloakbrowser", None)


def _cookie(name="sessionid", value="v1", domain=".douyin.com", **over):
    item = {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": 1893456000, "httpOnly": True, "secure": True}
    item.update(over)
    return item


def _state(cookies, origins=None):
    return {
        "cookies": cookies,
        "origins": [{"origin": "https://www.douyin.com",
                     "localStorage": [{"name": "device_id", "value": "dev-1"}]}] if origins is None else origins,
    }


def _ok_result(dry_run="off"):
    return {"ok": True, "status": "ok", "conv_id": "0:1:2", "display": "甲同学",
            "reason": "", "level": "strong", "dry_run": dry_run,
            "rate_limited": False, "limit": None, "via": "http",
            "message_id": "1", "login_lost": False, "kicked": False}


class _Page:
    def wait_for_timeout(self, ms):
        pass


class _Context:
    def __init__(self, storage_state=None, snapshot=None, raise_snapshot=False, raise_add=False):
        self.storage_state_in = storage_state
        self.raise_add = raise_add
        self.cookies = None
        self.closed = False
        self._snapshot = snapshot
        self._raise_snapshot = raise_snapshot
        self.page = _Page()

    def set_default_navigation_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    def new_page(self):
        return self.page

    def add_cookies(self, cookies):
        if self.raise_add:
            raise RuntimeError("注入 cookie 失败")
        self.cookies = list(cookies)

    def storage_state(self):
        if self._raise_snapshot:
            raise RuntimeError("浏览器已经关掉了")
        return json.loads(json.dumps(self._snapshot or {"cookies": [], "origins": []}))

    def close(self):
        self.closed = True


class _Browser:
    """支持 `new_context(storage_state=...)` 的假浏览器。"""

    def __init__(self, *, accepts_state=True, snapshot=None, raise_snapshot=False,
                 broken_state=False, raise_add=False):
        self.contexts = []
        self.accepts_state = accepts_state
        self.snapshot = snapshot
        self.raise_snapshot = raise_snapshot
        self.broken_state = broken_state
        self.raise_add = raise_add

    def new_context(self, **kwargs):
        if kwargs and not self.accepts_state:
            raise TypeError("new_context() got an unexpected keyword argument")
        if self.broken_state and kwargs.get("storage_state") is not None:
            # Playwright 对坏 state 抛的是 Error，不是 TypeError
            raise RuntimeError("storage_state 里 origins 结构不对")
        ctx = _Context(storage_state=kwargs.get("storage_state"),
                       snapshot=self.snapshot, raise_snapshot=self.raise_snapshot,
                       raise_add=self.raise_add)
        self.contexts.append(ctx)
        return ctx

    def close(self):
        pass


class _FakeIM:
    """只实现 `core.tasks` 用到的那几个方法。"""

    result = None

    def __init__(self, page, **kw):
        self.page = page
        self.last_scan = {"stopped": "bottom", "steps": 1, "visited": 1,
                          "missing": [], "select_failed": [], "scanned_all": True,
                          "note": "已到底"}

    def wait_ready(self):
        return {"status": "READY", "user_id": "100", "nickname": "测试号"}

    def recheck_login(self):
        return {"state": "EXPIRED"}

    def iter_find_and_select(self, targets):
        for t in targets:
            name = t.get("name") if isinstance(t, dict) else t
            yield {"display": name, "conv_id": "0:1:2", "uid": None,
                   "reselect": lambda: True}

    def type_and_send(self, friend, message, dry_run="off", **kw):
        if _FakeIM.result is not None:
            out = dict(_FakeIM.result)
            out.setdefault("dry_run", dry_run)
            return out
        return _ok_result(dry_run)

    def check_limit(self, receipts=()):
        return None

    def fold_groups(self):
        return {"fold": [], "stranger": []}

    def detach(self):
        pass


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = os.path.join(self._dir.name, "browser-state")
        env = {
            "BROWSER_STATE_DIR": self.state_dir,
            "ENV_FILE": os.path.join(self._dir.name, ".env"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        im = mock.patch.object(tasks_mod, "DouyinIM", _FakeIM)
        im.start()
        self.addCleanup(im.stop)
        _FakeIM.result = None

        sleep = mock.patch.object(tasks_mod.time, "sleep", lambda *_: None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def _run(self, browser, *, cookies=None, unique_id=UID):
        return tasks_mod.do_user_task(
            browser, "账号甲",
            cookies if cookies is not None else [_cookie()],
            [{"name": "甲同学", "conv_id": "0:1:2"}],
            unique_id=unique_id,
        )

    def _stored(self):
        return browser_state.read(UID)

    # -- 同一份会话：以快照为准 ------------------------------------------
    def test_同一份会话就只装快照不再注入_env_的旧值(self):
        browser_state.write(UID, _state([_cookie(value="v1")]))
        browser = _Browser(snapshot=_state([_cookie(value="v1")]))
        out = self._run(browser)
        self.assertEqual(out["status"], "ok")
        ctx = browser.contexts[0]
        self.assertIsNotNone(ctx.storage_state_in)
        self.assertIsNone(ctx.cookies, "同一份会话不该再 add_cookies")

    def test_跑完把这一轮的快照存下来(self):
        snapshot = _state([_cookie(value="v1"),
                           _cookie(name="ttwid", value="t2"),
                           _cookie(name="ga", value="1", domain=".google.com")])
        out = self._run(_Browser(snapshot=snapshot))
        self.assertEqual(out["status"], "ok")
        stored = self._stored()
        self.assertEqual(sorted(c["name"] for c in stored["cookies"]),
                         ["sessionid", "ttwid"])
        self.assertEqual(stored["origins"][0]["localStorage"][0]["value"], "dev-1")

    # -- 换了会话：以 .env 为准，但设备状态继续用 ------------------------
    def test_env_换了会话就重新注入但设备状态照用(self):
        browser_state.write(UID, _state([_cookie(value="旧值")]))
        browser = _Browser(snapshot=_state([_cookie(value="新值")]))
        out = self._run(browser, cookies=[_cookie(value="新值")])
        self.assertEqual(out["status"], "ok")
        ctx = browser.contexts[0]
        self.assertIsNotNone(ctx.storage_state_in, "设备状态（localStorage）要继续用")
        self.assertEqual([c["value"] for c in ctx.cookies], ["新值"])

    # -- 没有快照：与改动前完全一致 --------------------------------------
    def test_没有快照就是全新上下文(self):
        browser = _Browser(snapshot=_state([_cookie()]))
        out = self._run(browser)
        self.assertEqual(out["status"], "ok")
        ctx = browser.contexts[0]
        self.assertIsNone(ctx.storage_state_in)
        self.assertEqual([c["name"] for c in ctx.cookies], ["sessionid"])
        self.assertIsNotNone(self._stored(), "第一轮跑完就该有快照了")

    # -- 被踢：不拿死 cookie 盖掉还能用的快照 ----------------------------
    def test_被踢下线的一轮不回写_env(self):
        dead = {"ok": False, "login_lost": True, "kicked": True,
                "kick_decision": "KICK", "level": "none", "reason": "KICK",
                "via": None, "message_id": None, "rate_limited": False,
                "limit": None, "login_state": "EXPIRED"}
        _FakeIM.result = dead
        browser = _Browser(snapshot=_state([_cookie(value="v1")]))
        with mock.patch.object(tasks_mod.cookie_sync, "sync") as sync:
            out = self._run(browser)
        self.assertIn(out["status"], ("ok", "error"))
        self.assertTrue(sync.called)
        self.assertFalse(sync.call_args.kwargs["allow"], "被踢的会话不能回写 .env")
        self.assertIsNotNone(self._stored(), "设备状态本身仍然值得留下")

    def test_门禁就判死登录态的一轮不回写_env(self):
        """门禁 `LOGGED_OUT` / `EXPIRED` 时一条都没发，收尾也不许回写 .env。

        死会话在页面上可能已经被换成一份**匿名** sessionid（未来过期、名字还叫
        sessionid），has_login_cookie / has_usable_login_cookie 都会放行 ——
        真写回去就把 .env 里的凭据盖掉了，用户重新扫码之前先坏一份。
        设备状态（localStorage）仍然值得留下。
        """
        browser = _Browser(snapshot=_state([_cookie(value="死值")]))
        dead_gate = {"status": "LOGGED_OUT", "user_id": "", "nickname": ""}
        with mock.patch.object(_FakeIM, "wait_ready", return_value=dead_gate):
            with mock.patch.object(tasks_mod.cookie_sync, "sync") as sync:
                out = self._run(browser)
        self.assertEqual(out["status"], "error")
        self.assertTrue(out["error"].startswith("login_dead:"), out["error"])
        self.assertTrue(sync.called)
        self.assertFalse(sync.call_args.kwargs["allow"], "门禁判死的会话不能回写 .env")
        self.assertIsNotNone(self._stored(), "设备状态本身仍然值得留下")

    # -- 降级路径 --------------------------------------------------------
    def test_假浏览器不吃_storage_state_参数也能跑完(self):
        browser = _Browser(accepts_state=False, snapshot=_state([_cookie()]))
        out = self._run(browser)
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(browser.contexts[0].storage_state_in)
        self.assertIsNotNone(browser.contexts[0].cookies)

    def test_取不到浏览器状态时只降级(self):
        browser = _Browser(raise_snapshot=True)
        # core/tasks 的 logger 叫 app（utils.logger.setup_logger 的默认名）
        with self.assertLogs("app", level="DEBUG") as logs:
            out = self._run(browser)
        self.assertTrue(any("取浏览器状态失败" in line for line in logs.output), logs.output)
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(self._stored())
        self.assertTrue(browser.contexts[0].closed)

    def test_坏快照不会让账号当天报错(self):
        """快照文件是上一轮自己写的，坏了也只能丢 —— 不能每轮都炸同一个账号。"""
        browser_state.write(UID, _state([_cookie(value="v1")]))
        browser = _Browser(broken_state=True, snapshot=_state([_cookie(value="v2")]))
        with self.assertLogs("app", level="WARNING") as logs:
            out = self._run(browser)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(any("浏览器状态快照用不了" in line for line in logs.output), logs.output)
        ctx = browser.contexts[0]
        self.assertIsNone(ctx.storage_state_in, "坏快照要丢掉重开，而不是原样再喂一次")
        self.assertIsNotNone(ctx.cookies, "退回全新上下文后仍要注入 .env 的 cookie")

    def test_建上下文中途失败也要关掉上下文(self):
        """新开的上下文如果没交给 finally，失败路径上会一直挂着一个浏览器上下文。"""
        browser = _Browser(snapshot=_state([_cookie()]), raise_add=True)
        with self.assertRaises(RuntimeError):
            self._run(browser)
        self.assertTrue(browser.contexts[0].closed, "失败路径上上下文也必须关闭")

    def test_存快照出错也不改变本轮结果(self):
        browser = _Browser(snapshot=_state([_cookie()]))
        with mock.patch.object(tasks_mod.browser_state, "write",
                               side_effect=RuntimeError("磁盘满了")):
            with self.assertLogs("app", level="DEBUG") as logs:
                out = self._run(browser)
        self.assertTrue(any("保存浏览器状态失败" in line for line in logs.output), logs.output)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(browser.contexts[0].closed, "上下文必须照常关闭")

    def test_没有抖音号就不存快照(self):
        browser = _Browser(snapshot=_state([_cookie()]))
        out = self._run(browser, unique_id=None)
        self.assertEqual(out["status"], "ok")
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "None.json")))

    def test_新上下文带上语言与时区(self):
        """发送上下文必须和登录那一台说同一个语言、同一个时区（P5）。

        `new_context` 是**每个任务现开**的，光把参数传给浏览器启动还不够：
        这里漏了，页面里的 Intl/navigator.language 就回到服务器默认值。
        """
        got = {}

        class _CapturingBrowser:
            def new_context(self, **kwargs):
                got.update(kwargs)
                return object()

        with mock.patch.dict(os.environ, {"BROWSER_TIMEZONE": "Asia/Hong_Kong"}):
            tasks_mod._new_context(_CapturingBrowser(), None)
        self.assertEqual(got.get("locale"), "zh-CN")
        self.assertEqual(got.get("timezone_id"), "Asia/Hong_Kong")
        self.assertNotIn("storage_state", got)

    def test_新上下文同时带快照与语言时区(self):
        got = {}

        class _CapturingBrowser:
            def new_context(self, **kwargs):
                got.update(kwargs)
                return object()

        state = _state([_cookie()])
        with mock.patch.dict(os.environ, {"BROWSER_TIMEZONE": "Asia/Hong_Kong"}):
            tasks_mod._new_context(_CapturingBrowser(), state)
        self.assertEqual(got.get("storage_state"), state)
        self.assertEqual(got.get("timezone_id"), "Asia/Hong_Kong")


if __name__ == "__main__":
    unittest.main()
