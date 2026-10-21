"""cookie 回写 `.env`（P2）：什么时候写、什么时候一个字节都不动。"""
import glob
import json
import os
import tempfile
import unittest
from unittest import mock

from core import cookie_sync
from core import cookie_utils
from webui import envfile

UID = "11223344556"


def _cookie(name, value, domain=".douyin.com", **over):
    item = {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": 1893456000, "httpOnly": True, "secure": True}
    item.update(over)
    return item


def _session(sessionid="sid-1", secsdk="secsdk-1", ttwid="ttwid-1"):
    """一份"服务端会动"的 cookie：登录态 + 两个每轮都可能被刷新的值。"""
    return [
        _cookie("sessionid", sessionid),
        _cookie("ttwid", ttwid),
        _cookie("x-web-secsdk-uid", secsdk),
    ]


class CookieSyncTests(unittest.TestCase):
    """用真实的 `webui.envfile` 写临时 `.env`：这里要验证的就是这两层拼起来对不对。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.env_path = os.path.join(self._dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as handle:
            handle.write("OTHER=1\n")
        patcher = mock.patch.dict(os.environ, {"ENV_FILE": self.env_path})
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- 夹具 -----------------------------------------------------------
    def _seed(self, cookies):
        saved = envfile.save_cookie(UID, cookie_utils.cookies_to_json(cookies))
        self.assertTrue(saved.get("ok"), saved)

    def _text(self):
        with open(self.env_path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _backups(self):
        return sorted(glob.glob(self.env_path + ".bak-*"))

    # -- 用例 -----------------------------------------------------------
    def test_值没变就一个字节都不动(self):
        self._seed(_session())
        before = self._text()
        backups = self._backups()
        result = cookie_sync.sync(UID, _session())
        self.assertEqual(result["reason"], "unchanged")
        self.assertFalse(result["changed"])
        self.assertTrue(result["ok"])
        self.assertEqual(self._text(), before)
        self.assertEqual(self._backups(), backups)

    def test_服务端新签发的_cookie_会写回(self):
        self._seed(_session(secsdk="旧设备号"))
        backups = len(self._backups())
        result = cookie_sync.sync(UID, _session(secsdk="新设备号"))
        self.assertTrue(result["changed"], result)
        self.assertEqual(result["written"], 3)
        self.assertEqual(result["reason"], "")
        # 写进 .env 的是转义后的 JSON（中文会成为 \uXXXX），所以判据必须走
        # 读回来这条路 —— 那也正是下一轮真正会看到的形状。
        values = [c["value"] for c in cookie_sync._current_cookies(UID)]
        self.assertIn("新设备号", values)
        self.assertNotIn("旧设备号", values)
        # 其它键不能被顺手写坏
        self.assertIn("OTHER=1", self._text())
        self.assertEqual(len(self._backups()), backups + 1)   # 写之前先备份

    def test_回写之后读回来还是同一份(self):
        self._seed(_session())
        fresh = _session(secsdk="新设备号", ttwid="ttwid-2")
        cookie_sync.sync(UID, fresh)
        self.assertEqual(cookie_sync._canonical(cookie_sync._current_cookies(UID)),
                         cookie_sync._canonical(fresh))

    def test_新值里的百分号冒号不会被_unicode_escape_吃掉(self):
        # 真实 token 常见字符；读侧那次 unicode_escape 对它们没有影响，
        # 但一旦哪天改成"顺便反转义"，这里会立刻红。
        tricky = "tok%2F:a-1_b.c=d"
        self._seed(_session())
        result = cookie_sync.sync(UID, _session(secsdk=tricky))
        self.assertTrue(result["changed"], result)
        self.assertEqual(cookie_sync._current_cookies(UID)[2]["value"], tricky)

    def test_被踢的那一轮不写回(self):
        self._seed(_session(secsdk="旧设备号"))
        before = self._text()
        result = cookie_sync.sync(UID, _session(secsdk="死掉的设备号"), allow=False)
        self.assertEqual(result["reason"], "skipped")
        self.assertFalse(result["ok"])
        self.assertEqual(self._text(), before)

    def test_过期登录态不写回(self):
        self._seed(_session())
        before = self._text()
        dead = [_cookie("sessionid", "sid-dead", expires=1000), _cookie("ttwid", "t2")]
        result = cookie_sync.sync(UID, dead)
        self.assertEqual(result["reason"], "expired-login")
        self.assertEqual(self._text(), before)

    def test_没有登录_cookie_不写回(self):
        self._seed(_session())
        before = self._text()
        result = cookie_sync.sync(UID, [_cookie("ttwid", "t3")])
        self.assertEqual(result["reason"], "no-login")
        self.assertEqual(self._text(), before)

    def test_没有_env_文件就安静跳过(self):
        os.unlink(self.env_path)
        result = cookie_sync.sync(UID, _session())
        self.assertEqual(result["reason"], "no-env")
        self.assertFalse(result["ok"])
        self.assertFalse(os.path.exists(self.env_path))

    def test_写入器拒绝时把原因带回来(self):
        self._seed(_session())
        with mock.patch.object(envfile, "save_cookie", return_value={"ok": False, "error": "格式不对"}):
            with self.assertLogs("core.cookie_sync", level="WARNING"):
                result = cookie_sync.sync(UID, _session(secsdk="新设备号"))
        self.assertEqual(result["reason"], "rejected")
        self.assertEqual(result["error"], "格式不对")
        self.assertFalse(result["ok"])

    def test_拿不到写入器就安静跳过(self):
        with mock.patch.object(cookie_sync, "_envfile", return_value=None):
            result = cookie_sync.sync(UID, _session())
        self.assertEqual(result["reason"], "no-writer")
        self.assertFalse(result["ok"])

    def test_抖音号不合法直接拒掉(self):
        for bad in ("", "a/b", "../x"):
            result = cookie_sync.sync(bad, _session())
            self.assertEqual(result["reason"], "bad-uid", bad)

    def test_非法条目不会被写成半个_JSON(self):
        self._seed(_session())
        broken = _session() + [{"name": "", "value": "douyin.com", "domain": ".douyin.com", "path": "/"}]
        cookie_sync.sync(UID, broken)
        values = envfile.read_map()
        data = json.loads(values["COOKIES_" + UID])
        self.assertTrue(all(item["name"] for item in data))


if __name__ == "__main__":
    unittest.main()
