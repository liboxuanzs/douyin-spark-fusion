"""浏览器状态快照（P1）的存取、权限与降级行为。"""
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from core import browser_state


def _cookie(name="sessionid", value="v1", domain=".douyin.com", **over):
    item = {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": 1893456000, "httpOnly": True, "secure": True,
            "sameSite": "Lax"}
    item.update(over)
    return item


def _state(cookies, origins=None):
    return {
        "cookies": cookies,
        "origins": [{"origin": "https://www.douyin.com",
                     "localStorage": [{"name": "device_id", "value": "abc"}]}] if origins is None else origins,
    }


class BrowserStateTests(unittest.TestCase):
    """状态目录每次换一个临时位置：用例之间不能互相看到快照。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = os.path.join(self._dir.name, "browser-state")
        patcher = mock.patch.dict(os.environ, {browser_state.STATE_DIR_ENV: self.root})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_存下来再读回来是同一条会话(self):
        path = browser_state.write("11223344556", _state([_cookie()]))
        self.assertIsNotNone(path)
        self.assertEqual(str(path), os.path.join(self.root, "11223344556.json"))
        data = browser_state.read("11223344556")
        self.assertEqual([c["name"] for c in data["cookies"]], ["sessionid"])
        # 设备号这类信息就藏在 origins 里，必须原样保留
        self.assertEqual(data["origins"][0]["localStorage"][0]["value"], "abc")

    def test_只留抖音域的_cookie(self):
        state = _state([_cookie(), _cookie(name="ga", value="1", domain=".google.com")])
        browser_state.write("11223344556", state)
        names = [c["name"] for c in browser_state.read("11223344556")["cookies"]]
        self.assertEqual(names, ["sessionid"])

    def test_写出来的是_0600_目录是_0700(self):
        path = browser_state.write("11223344556", _state([_cookie()]))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)

    def test_没有快照就是全新上下文(self):
        self.assertIsNone(browser_state.read("11223344556"))
        self.assertIsNone(browser_state.storage_state_for("11223344556"))

    def test_坏_JSON_按下没有处理(self):
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, "11223344556.json"), "w", encoding="utf-8") as handle:
            handle.write("{这不是 JSON")
        with self.assertLogs("core.browser_state", level="WARNING") as logs:
            self.assertIsNone(browser_state.read("11223344556"))
        self.assertIn("11223344556.json", "\n".join(logs.output))

    def test_结构不对也按下没有处理(self):
        os.makedirs(self.root, exist_ok=True)
        for payload in ({"cookies": "不是列表"}, [1, 2, 3], {"origins": []}):
            with open(os.path.join(self.root, "11223344556.json"), "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertLogs("core.browser_state", level="WARNING"):
                self.assertIsNone(browser_state.read("11223344556"), payload)

    def test_一条目标域_cookie_都没有就不落盘(self):
        path = browser_state.write("11223344556", _state([_cookie(domain=".google.com")]))
        self.assertIsNone(path)
        self.assertFalse(os.path.exists(os.path.join(self.root, "11223344556.json")))

    def test_抖音号不合法就不碰磁盘(self):
        for bad in ("", "  ", "../etc/passwd", "a/b", "x" * 65):
            self.assertIsNone(browser_state.state_path(bad), bad)
            self.assertIsNone(browser_state.read(bad), bad)
            self.assertIsNone(browser_state.write(bad, _state([_cookie()])), bad)
        self.assertFalse(os.path.exists(self.root))

    def test_目录建不出来时只降级不抛异常(self):
        # 把「目录」换成一个普通文件：mkdir 会抛 FileExistsError（OSError 的一种）
        with open(self.root, "w", encoding="utf-8") as handle:
            handle.write("占位")
        with self.assertLogs("core.browser_state", level="WARNING"):
            self.assertIsNone(browser_state.write("11223344556", _state([_cookie()])))

    def test_同一份会话_是_True(self):
        state = _state([_cookie()])
        self.assertTrue(browser_state.same_session(state, [_cookie()]))

    def test_换了_sessionid_就不是同一份(self):
        state = _state([_cookie(value="旧值")])
        self.assertFalse(browser_state.same_session(state, [_cookie(value="新值")]))

    def test_快照里没有登录_cookie_不算同一份(self):
        state = _state([_cookie(name="ttwid", value="t1")])
        self.assertFalse(browser_state.same_session(state, [_cookie()]))
        self.assertFalse(browser_state.same_session(None, [_cookie()]))

    def test_环境变量能把目录挪走(self):
        other = os.path.join(self._dir.name, "elsewhere")
        with mock.patch.dict(os.environ, {browser_state.STATE_DIR_ENV: other}):
            self.assertEqual(str(browser_state.state_dir()), other)
            path = browser_state.write("11223344556", _state([_cookie()]))
            self.assertTrue(str(path).startswith(other))

    def test_没设环境变量时落在仓库的_logs_下面(self):
        with mock.patch.dict(os.environ, {browser_state.STATE_DIR_ENV: ""}):
            path = browser_state.state_path("11223344556")
        self.assertEqual(path.name, "11223344556.json")
        self.assertEqual(path.parent.name, "browser-state")
        self.assertEqual(path.parent.parent.name, "logs")


if __name__ == "__main__":
    unittest.main()
