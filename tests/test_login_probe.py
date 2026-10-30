"""`core/login_probe.py` —— 撤账之前那次登录态探测。

2026-10-30 wheat 事故：面板上的「补发」撤了记账，下一轮门铃开浏览器才发现登录早已
被踢 —— 白跑一轮、账本多一条 `login_dead`，人还在等补发。探测就是为了把这句话
提前问出来，所以这一组测试盯三件事：

① 结论分得清「登录着 / 登录已死 / 没结论」；
② **没探成 ≠ 登录已死**（`blocked=True`，调用方只能当没结论，不能拿去拒绝撤账）；
③ 只读：不写快照、不碰账本，上下文与浏览器都要收干净。
"""

import unittest
from unittest import mock

from core import login_probe


class _FakePage:
    def __init__(self, fail=False):
        self.urls = []
        self.fail = fail
        self.waited = 0

    def goto(self, url, wait_until="domcontentloaded"):
        self.urls.append(url)
        if self.fail:
            raise RuntimeError("net::ERR_CONNECTION_RESET")

    def wait_for_timeout(self, ms):
        self.waited += int(ms or 0)


class _FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.nav_timeout = None
        self.default_timeout = None

    def set_default_navigation_timeout(self, value):
        self.nav_timeout = value

    def set_default_timeout(self, value):
        self.default_timeout = value

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False
        self.context_kwargs = []

    def new_context(self, **kwargs):
        self.context_kwargs.append(kwargs)
        return self._context

    def close(self):
        self.closed = True


UID = "11223344556"
COOKIES = [{"name": "sessionid", "value": "abc", "domain": ".douyin.com", "path": "/"}]


class LoginProbeTests(unittest.TestCase):
    """一次探测 = 起上下文 → 打开 IM 页 → `check_login` 反复问一句。"""

    def setUp(self):
        self.page = _FakePage()
        self.context = _FakeContext(self.page)
        self.browser = _FakeBrowser(self.context)
        self.users = [{"unique_id": UID, "username": "示例号",
                       "fingerprint": "fp-1", "proxy": "", "cookies": list(COOKIES)}]

        for patcher in (
            mock.patch.object(login_probe, "attach", return_value="mon"),
            mock.patch.object(login_probe.browser_state, "read", return_value=None),
            mock.patch.object(login_probe.browser_state, "write") ,
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_state = login_probe.browser_state.write

    def _probe(self, verdict=None, **kwargs):
        kwargs.setdefault("users", self.users)
        kwargs.setdefault("browser", self.browser)
        if verdict is not None:
            patcher = mock.patch.object(login_probe, "check_login", return_value=verdict)
            patcher.start()
            self.addCleanup(patcher.stop)
        return login_probe.probe(UID, **kwargs)

    # ---------------------------------------------------------------- 三种结论

    def test_logged_in_is_reported_with_the_user(self):
        out = self._probe({"state": "LOGGED_IN", "user_id": "902043898218172",
                           "nickname": "🌈LBX🌈", "log": "[LOGIN] ✅ 已登录"})
        self.assertEqual(out["state"], "LOGGED_IN", out)
        self.assertFalse(out["blocked"], out)
        self.assertIn("902043898218172", out["detail"])
        self.assertEqual(out["user_id"], "902043898218172")
        self.assertEqual(self.page.urls, ["https://www.douyin.com/chat"],
                         "探的必须是发送时用的那个页面，否则登录态判定口径不一致")

    def test_expired_is_reported_as_expired(self):
        out = self._probe({"state": "EXPIRED", "log": "[LOGIN] ⚠️ 登录已失效"})
        self.assertEqual(out["state"], "EXPIRED", out)
        self.assertIn("失效", out["detail"])
        self.assertFalse(out["blocked"], out)

    def test_not_logged_in_is_reported(self):
        out = self._probe({"state": "NOT_LOGGED_IN"})
        self.assertEqual(out["state"], "NOT_LOGGED_IN", out)
        self.assertIn("没有登录", out["detail"])

    def test_no_conclusion_is_unknown_and_not_blocked(self):
        """页面一直不说话 → UNKNOWN（**不是** blocked：探过，只是没结论）。"""
        out = self._probe({"state": "UNKNOWN"}, timeout=0)
        self.assertEqual(out["state"], "UNKNOWN", out)
        self.assertFalse(out["blocked"], out)

    def test_unknown_is_retried_until_it_answers(self):
        """首屏慢的时候要接着问，不能一次 UNKNOWN 就下结论。"""
        seq = [{"state": "UNKNOWN"}, {"state": "UNKNOWN"}, {"state": "LOGGED_IN",
                                                            "user_id": "1"}]
        with mock.patch.object(login_probe, "check_login", side_effect=seq):
            out = login_probe.probe(UID, users=self.users, browser=self.browser, timeout=30)
        self.assertEqual(out["state"], "LOGGED_IN", out)
        self.assertGreaterEqual(self.page.waited, login_probe.PROBE_SETTLE_MS * 2)

    # ---------------------------------------------------------------- 没探成

    def test_unknown_account_is_blocked_without_touching_the_browser(self):
        out = login_probe.probe("999", users=self.users, browser=self.browser)
        self.assertEqual(out["state"], "UNKNOWN", out)
        self.assertTrue(out["blocked"], out)
        self.assertIn("TASKS", out["detail"])
        self.assertFalse(self.context.closed, "账号不存在时根本不该开上下文")

    def test_no_credential_at_all_is_not_logged_in(self):
        """既没有 .env cookie 也没有快照 → 不用起浏览器就知道结论。"""
        users = [dict(self.users[0], cookies=[])]
        with mock.patch.object(login_probe, "get_browser") as get_browser:
            out = login_probe.probe(UID, users=users)
        self.assertEqual(out["state"], "NOT_LOGGED_IN", out)
        self.assertFalse(out["blocked"], out)
        get_browser.assert_not_called()

    def test_browser_failure_is_blocked_not_dead(self):
        """浏览器起不来 = 探不了，**不是**登录死了 —— 否则用户连撤账都做不了。"""
        with mock.patch.object(login_probe, "get_browser",
                               side_effect=RuntimeError("浏览器启动失败 —— boom")):
            out = login_probe.probe(UID, users=self.users)
        self.assertEqual(out["state"], "UNKNOWN", out)
        self.assertTrue(out["blocked"], out)
        self.assertIn("探测没跑起来", out["detail"])

    def test_check_login_crash_is_unknown(self):
        with mock.patch.object(login_probe, "check_login", side_effect=RuntimeError("boom")):
            out = login_probe.probe(UID, users=self.users, browser=self.browser)
        self.assertEqual(out["state"], "UNKNOWN", out)
        self.assertIn("判定出错", out["detail"])

    def test_navigation_error_still_gives_a_verdict(self):
        """导航报错也继续判定（与发送路径同款）：抖音经常是半截加载就够看了。"""
        self.page.fail = True
        out = self._probe({"state": "LOGGED_IN", "user_id": "7"})
        self.assertEqual(out["state"], "LOGGED_IN", out)

    # ---------------------------------------------------------------- 只读与收尾

    def test_context_and_own_browser_are_closed(self):
        out = self._probe({"state": "LOGGED_IN", "user_id": "1"})
        self.assertEqual(out["state"], "LOGGED_IN", out)
        self.assertTrue(self.context.closed, "上下文必须关掉，否则每点一次漏一个 Chromium")
        self.assertFalse(self.browser.closed,
                         "浏览器是调用方传进来的，探测不该替别人关掉它")

    def test_probe_never_writes_anything(self):
        self._probe({"state": "EXPIRED"})
        self.write_state.assert_not_called()

    def test_fingerprint_and_proxy_go_to_the_browser(self):
        """出口与设备指纹口径必须与发送时一致，否则"探得通、发不出"。"""
        with mock.patch.object(login_probe, "get_browser", return_value=self.browser) as gb, \
                mock.patch.object(login_probe, "check_login",
                                  return_value={"state": "LOGGED_IN", "user_id": "1"}):
            out = login_probe.probe(UID, users=[dict(self.users[0],
                                                      proxy="http://127.0.0.1:1080")])
        self.assertEqual(out["state"], "LOGGED_IN", out)
        gb.assert_called_once_with("fp-1", proxy="http://127.0.0.1:1080")


if __name__ == "__main__":
    unittest.main()
