"""浏览器启动失败时的驱动句柄回收（`core/browser.py::_stop_silently`）。

背景：`sync_playwright().start()` 会拉起一个 node 子进程，只有 `stop()` 才回收。
成功路径把 `(pw, browser)` 登记进 `_KEEPALIVE`、由 `close_context()` 回收；
而**启动失败**时句柄根本还没登记，必须在 `except` 里就地 stop —— 漏掉的后果是
每失败一次漏一个 node 子进程，而"资料目录被另一只 Chromium 占着"正是最常见的
失败原因（扫码登录与好友扫描共用同一个 user-data-dir），会连着重试很多次。
"""

import sys
import types
import unittest
from unittest import mock

from core import browser as browser_mod


class _FakeSession:
    """假的 `sync_playwright()`：`start()` 返回的对象把 `stop()` 次数记下来。"""

    def __init__(self, *, launch=None, launch_error=None, ctx=None, ctx_error=None):
        self.stopped = 0
        pw = types.SimpleNamespace()
        pw.stop = self._stop
        chromium = types.SimpleNamespace()
        chromium.launch = self._make_launcher(launch_error, launch)
        chromium.launch_persistent_context = self._make_ctx(ctx_error, ctx)
        pw.chromium = chromium
        starter = types.SimpleNamespace(start=lambda: pw)
        mod = types.ModuleType("playwright.sync_api")
        mod.sync_playwright = lambda: starter
        self.pw = pw
        self.mod = mod

    def _stop(self):
        self.stopped += 1

    @staticmethod
    def _make_launcher(error, result):
        def launch(**_kw):
            if error is not None:
                raise error
            return result
        return launch

    @staticmethod
    def _make_ctx(error, result):
        def launch_persistent_context(_profile_dir, **_kw):
            if error is not None:
                raise error
            return result
        return launch_persistent_context

    def __enter__(self):
        self._saved = {name: sys.modules.get(name) for name in ("playwright", "playwright.sync_api")}
        pkg = types.ModuleType("playwright")
        pkg.__path__ = []                      # 让它看起来像个包，`from playwright.sync_api import …` 才走得通
        sys.modules["playwright"] = pkg
        sys.modules["playwright.sync_api"] = self.mod
        return self

    def __exit__(self, *_exc):
        for name, value in self._saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        return False


class LaunchFailureTests(unittest.TestCase):
    def test_driver_is_stopped_when_launch_fails(self):
        with _FakeSession(launch_error=RuntimeError("user-data-dir 被占")) as fake:
            keep = []
            with mock.patch.object(browser_mod, "_KEEPALIVE", keep):
                with self.assertRaises(RuntimeError):
                    browser_mod._launch_playwright(headless=True, args=[], proxy=None)
            self.assertEqual(fake.stopped, 1, "启动失败必须把 driver 还回去，否则漏一个 node 子进程")
            self.assertEqual(keep, [], "失败的句柄不该登记进 _KEEPALIVE（那会让 close_context 收到半个浏览器）")

    def test_driver_is_kept_when_launch_succeeds(self):
        sentinel = object()
        with _FakeSession(launch=sentinel) as fake:
            keep = []
            with mock.patch.object(browser_mod, "_KEEPALIVE", keep):
                got = browser_mod._launch_playwright(headless=True, args=[], proxy=None)
            self.assertIs(got, sentinel)
            self.assertEqual(fake.stopped, 0)
            self.assertEqual(len(keep), 1)
            self.assertIs(keep[0][1], sentinel)

    def test_persistent_context_failure_stops_the_driver(self):
        with _FakeSession(ctx_error=RuntimeError("SingletonLock 抢不到")) as fake:
            keep = []
            with mock.patch.dict("os.environ", {"BROWSER_BACKEND": "playwright"}):
                with mock.patch.object(browser_mod, "_KEEPALIVE", keep):
                    with self.assertRaises(RuntimeError) as cm:
                        browser_mod.launch_persistent_context("/tmp/definitely-not-a-profile")
            self.assertIn("浏览器启动失败", str(cm.exception))
            self.assertEqual(fake.stopped, 1)
            self.assertEqual(keep, [])

    def test_persistent_context_success_keeps_the_handle(self):
        sentinel = object()
        with _FakeSession(ctx=sentinel) as fake:
            keep = []
            with mock.patch.dict("os.environ", {"BROWSER_BACKEND": "playwright"}):
                with mock.patch.object(browser_mod, "_KEEPALIVE", keep):
                    got = browser_mod.launch_persistent_context("/tmp/definitely-not-a-profile")
            self.assertIs(got, sentinel)
            self.assertEqual(fake.stopped, 0)
            self.assertEqual(len(keep), 1)

    def test_stop_failure_is_only_warned(self):
        """回收失败不能把原始异常吃掉 —— 调用方要看到"为什么没启动起来"。"""
        with _FakeSession() as fake:
            def boom():
                raise RuntimeError("driver 已经死了")
            fake.pw.stop = boom
            browser_mod._stop_silently(fake.pw)          # 不抛


class DiskCacheLimitTests(unittest.TestCase):
    """Chrome 的磁盘缓存**默认没有上限**，必须显式给一个。

    实测（2026-10-14，生产机）：资料目录按 ~180MB/天/账号 增长，
    `logs/login-profile/11223344556/Default/Cache` 单独就 451MB，机器上 25G 可用
    按当时速度一两个月会被吃光；磁盘满的直接后果是发送失败。上限取 256MB：
    比"一天的量"大得多（不影响命中率），又远小于"能把盘写满"。
    """

    def test_base_args_carry_a_disk_cache_limit(self):
        limit = browser_mod.DISK_CACHE_SIZE_MB
        self.assertIsInstance(limit, int)
        self.assertGreater(limit, 0)
        self.assertLessEqual(limit, 1024, "上限别开太大，否则等于没设")
        expected = f"--disk-cache-size={limit * 1024 * 1024}"
        self.assertIn(expected, browser_mod.BASE_CHROME_ARGS)
        self.assertEqual(
            len([a for a in browser_mod.BASE_CHROME_ARGS if a.startswith("--disk-cache-size=")]),
            1,
            "只能有一个上限参数（重复的以最后一个为准，容易改出意外）",
        )

    def test_limit_survives_get_browser_args(self):
        """真正传给 Chromium 的是 get_browser 组装出来的 args，别只改了常量。"""
        captured = {}

        def fake_launch(headless, args, proxy):
            captured["args"] = list(args)
            return object()

        with mock.patch.object(browser_mod, "_launch_cloakbrowser", fake_launch):
            with mock.patch.object(browser_mod, "_KEEPALIVE", []):
                browser_mod.get_browser()
        self.assertIn(
            f"--disk-cache-size={browser_mod.DISK_CACHE_SIZE_MB * 1024 * 1024}",
            captured["args"],
        )


class BrowserGeoOptionTests(unittest.TestCase):
    """语言/时区必须跟出口 IP 所在的地方对得上（2026-10-22，P5）。

    实测这台机器的出口在香港 HGC 住宅宽带（223.19.25.170），而系统时区是
    Asia/Shanghai；UA 又自称 Windows —— 时区和 IP 对不上，是风控里最便宜的一条
    对照。这里钉住三件事：环境变量怎么读（空/写错都不许抛）、cloakbrowser 启动
    真的带上参数、Playwright 回落时上下文也带上（两边的参数名不一样：
    timezone vs timezone_id）。
    """

    def test_defaults_are_system_timezone_and_zh_cn(self):
        with mock.patch.dict("os.environ", {"BROWSER_TIMEZONE": "", "BROWSER_LOCALE": ""}):
            self.assertEqual(browser_mod.browser_locale(), "zh-CN")
            self.assertIsNone(browser_mod.browser_timezone(),
                              "留空 = 跟系统时区，不能瞎猜一个")
            self.assertEqual(browser_mod.launch_options(), {"locale": "zh-CN"},
                             "没配时区就别往浏览器传 timezone，让它自己用系统的")

    def test_values_come_from_env(self):
        env = {"BROWSER_TIMEZONE": "Asia/Hong_Kong", "BROWSER_LOCALE": "zh-HK"}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(browser_mod.launch_options(),
                             {"locale": "zh-HK", "timezone": "Asia/Hong_Kong"})
            self.assertEqual(browser_mod.context_options(),
                             {"locale": "zh-HK", "timezone_id": "Asia/Hong_Kong"},
                             "Playwright 的 new_context 管这个参数叫 timezone_id")

    def test_invalid_timezone_only_warns(self):
        """写错一个时区名，不该把整轮发送变成失败。"""
        with mock.patch.dict("os.environ", {"BROWSER_TIMEZONE": "Mars/Olympus"}):
            with self.assertLogs("browser", level="WARNING") as logs:
                self.assertIsNone(browser_mod.browser_timezone())
            self.assertIn("不是合法时区", "\n".join(logs.output))

    def test_cloakbrowser_launch_carries_them(self):
        captured = {}
        fake = types.ModuleType("cloakbrowser")
        fake.launch = lambda **kw: captured.update(kw) or object()
        env = {"BROWSER_TIMEZONE": "Asia/Hong_Kong", "BROWSER_LOCALE": "zh-CN"}
        with mock.patch.dict(sys.modules, {"cloakbrowser": fake}):
            with mock.patch.dict("os.environ", env):
                browser_mod._launch_cloakbrowser(headless=True, args=["--x"], proxy=None)
        self.assertEqual(captured["timezone"], "Asia/Hong_Kong")
        self.assertEqual(captured["locale"], "zh-CN")
        self.assertTrue(captured["humanize"], "别为了传时区把拟人化弄丢")

    def test_persistent_cloakbrowser_context_carries_them(self):
        captured = {}
        fake = types.ModuleType("cloakbrowser")
        fake.launch_persistent_context = lambda *a, **kw: captured.update(kw) or object()
        env = {"BROWSER_TIMEZONE": "Asia/Hong_Kong", "BROWSER_BACKEND": "cloakbrowser"}
        with mock.patch.dict(sys.modules, {"cloakbrowser": fake}):
            with mock.patch.dict("os.environ", env):
                browser_mod.launch_persistent_context("/tmp/whatever")
        self.assertEqual(captured["timezone"], "Asia/Hong_Kong")
        self.assertEqual(captured["locale"], "zh-CN")

    def test_persistent_playwright_fallback_carries_them(self):
        captured = {}
        pw = types.SimpleNamespace()
        pw.stop = lambda: None
        chromium = types.SimpleNamespace()
        chromium.launch_persistent_context = lambda _dir, **kw: captured.update(kw) or object()
        pw.chromium = chromium
        sync_mod = types.ModuleType("playwright.sync_api")
        sync_mod.sync_playwright = lambda: types.SimpleNamespace(start=lambda: pw)
        pkg = types.ModuleType("playwright")
        pkg.__path__ = []
        env = {"BROWSER_BACKEND": "playwright", "BROWSER_TIMEZONE": "Asia/Hong_Kong"}
        with mock.patch.dict(sys.modules, {"playwright": pkg, "playwright.sync_api": sync_mod}):
            with mock.patch.dict("os.environ", env):
                with mock.patch.object(browser_mod, "_KEEPALIVE", []):
                    browser_mod.launch_persistent_context("/tmp/whatever")
        self.assertEqual(captured.get("timezone_id"), "Asia/Hong_Kong")
        self.assertEqual(captured.get("locale"), "zh-CN")


if __name__ == "__main__":
    unittest.main()
