"""发送前必须清洗 cookie（`core/tasks.py:191-204`）。

背景（真事故的成因）：`playwright` 的 `context.add_cookies()` 是**整批**语义 ——
只要列表里有一条 `name` 或 `domain` 为空的条目，整批都会被拒绝，随后这个账号
在门禁阶段被判成"掉登录"，用户看到的是"这个账号需要重新扫码"。而抖音自己就会
产出这种空名 cookie（`value="douyin.com"`，见 `core/cookie_utils.py:51-77`），
`.env` 里的 `COOKIES_*` 是历史快照，完全可能带着它。

所以 `do_user_task` 在注入前必须先过 `cookie_utils.filter_cookies(..., domains_only=False)`：
丢掉的只是不可用条目，其它字段一律不动；**全部**不可用时宁可当场报错，也不要
把一批注定被拒的 cookie 塞给 Playwright（那会伪装成"登录态失效"）。
"""
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

from core import tasks as tasks_mod                # noqa: E402


def tearDownModule():
    """把自己塞进去的假 `cloakbrowser` 收干净（跨文件污染会打红 webui 的纯净性用例）。"""
    if getattr(sys.modules.get("cloakbrowser"), "_fusion_fake", False):
        sys.modules.pop("cloakbrowser", None)


class _Page:
    def wait_for_timeout(self, ms):
        pass


class _Context:
    def __init__(self):
        self.page = _Page()
        self.cookies = None

    def set_default_navigation_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    def new_page(self):
        return self.page

    def add_cookies(self, cookies):
        self.cookies = list(cookies)

    def close(self):
        pass


class _Browser:
    def __init__(self):
        self.contexts = []

    def new_context(self):
        c = _Context()
        self.contexts.append(c)
        return c

    def close(self):
        pass


class _StubIM:
    """门禁直接判死，让 `do_user_task` 在"注入 cookie 之后"立刻返回。

    本文件只关心 cookie 注入这一步；门禁判定本身的用例在 test_tasks_fusion.py。
    """

    def __init__(self, page, **kw):
        self.page = page

    def wait_ready(self):
        return {"status": "NOT_LOGGED_IN"}

    def recheck_login(self):
        return {"state": "EXPIRED"}


def _cookie(name="sessionid", value="v", domain=".douyin.com", **over):
    item = {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": 1893456000, "httpOnly": True, "secure": True}
    item.update(over)
    return item


class CookieInjectionTests(unittest.TestCase):
    def setUp(self):
        self.browser = _Browser()
        patcher = mock.patch.object(tasks_mod, "DouyinIM", _StubIM)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 浏览器状态快照（P1）用临时目录：假浏览器没有 storage_state，本来就不会
        # 读写，但不隔离的话，某台机器上真跑过一次任务就会留下 logs/browser-state，
        # 用例从此取决于那台机器的磁盘内容。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "BROWSER_STATE_DIR": os.path.join(tmp.name, "browser-state"),
            "ENV_FILE": os.path.join(tmp.name, ".env"),
        })
        env.start()
        self.addCleanup(env.stop)

    def _run(self, cookies):
        return tasks_mod.do_user_task(
            self.browser, "账号甲", cookies, [{"name": "好友", "conv_id": "0:1:2"}],
            unique_id="11223344556",
        )

    def test_空名_cookie_被丢掉_其余原样注入(self):
        """抖音自产的空名 cookie（value="douyin.com"）必须被挡在 add_cookies 之前。"""
        bad = {"name": "", "value": "douyin.com", "domain": ".douyin.com", "path": "/"}
        good = _cookie()
        self._run([bad, good])
        injected = self.browser.contexts[0].cookies
        self.assertEqual([c["name"] for c in injected], ["sessionid"])
        self.assertEqual(injected[0]["value"], "v")
        self.assertEqual(injected[0]["domain"], ".douyin.com")
        self.assertTrue(injected[0]["httpOnly"])

    def test_空域_cookie_被丢掉(self):
        self._run([_cookie(name="sid_tt", domain=""), _cookie(name="sessionid")])
        self.assertEqual([c["name"] for c in self.browser.contexts[0].cookies], ["sessionid"])

    def test_全部不可用时当场报错_不塞给_playwright(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run([{"name": "", "domain": ".douyin.com"},
                       {"name": "x", "domain": "  "}])
        self.assertIn("cookie 全部不可用", str(ctx.exception))
        self.assertIsNone(self.browser.contexts[0].cookies,
                          "一条都不可用时不该调用 add_cookies")

    def test_注入的是新列表_不是调用方传进来的那个对象(self):
        raw = [_cookie()]
        self._run(raw)
        injected = self.browser.contexts[0].cookies
        self.assertIsNot(injected, raw)
        self.assertEqual(len(injected), 1)

    def test_无关域的_cookie_照旧保留(self):
        """发送链路用 domains_only=False：域过滤会让登录态少一条，这里只清不可用条目。"""
        self._run([_cookie(name="其他站", domain=".example.com"), _cookie()])
        self.assertEqual(sorted(c["name"] for c in self.browser.contexts[0].cookies),
                         ["sessionid", "其他站"])


if __name__ == "__main__":
    unittest.main()
