# -*- coding: utf-8 -*-
"""tests/test_proxy.py —— 每账号出口（issue #18）的回归测试。

这个模块的底线只有两条，测试也围着它们转：

1. **口令永不外露**：mask() 是界面/日志唯一允许的形态，异常文案里也要把原文换掉；
2. **写坏的地址不能让首页崩**：出口是手填的，`http://[::1` 这种能让 urlsplit 抛
   ValueError，而调用它的地方是渲染首页。
"""

import json
import os
import tempfile
import unittest

from core import proxy


class _Resp:
    """假 response：只实现 check() 用到的三样。"""

    def __init__(self, *, payload=None, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class NormalizeTests(unittest.TestCase):
    def test_裸地址补上_http(self):
        self.assertEqual(proxy.normalize("127.0.0.1:1080"), "http://127.0.0.1:1080")

    def test_已有的协议不重复补(self):
        self.assertEqual(proxy.normalize("socks5://h:1080"), "socks5://h:1080")

    def test_空与空白都是直连(self):
        for raw in ("", "   ", None, 0):
            self.assertEqual(proxy.normalize(raw), "")

    def test_两侧空白被去掉(self):
        self.assertEqual(proxy.normalize("  http://h:1  "), "http://h:1")


class ProblemTests(unittest.TestCase):
    def test_正常的_http_与_socks5_都通过(self):
        for raw in ("http://h:8080", "socks5://u:p@h:1080", "https://h:443"):
            self.assertEqual(proxy.problem(raw), "", raw)

    def test_空串等于直连不算问题(self):
        self.assertEqual(proxy.problem(""), "")

    def test_协议不支持要说出来(self):
        self.assertIn("ftp", proxy.problem("ftp://h:21"))

    def test_缺端口与端口不是数字都要拦(self):
        self.assertIn("端口", proxy.problem("http://h"))
        self.assertIn("端口", proxy.problem("http://h:abc"))

    def test_写坏的方括号不能抛异常(self):
        # urlsplit("http://[::1") 本身就会抛 ValueError —— 这一条钉住"不抛、说人话"
        text = proxy.problem("http://[::1")
        self.assertTrue(text)
        self.assertIn("看不懂", text)


class MaskTests(unittest.TestCase):
    def test_口令换成星号_用户名留着(self):
        """与面板「运行配置」里那一栏同一个形态（webui/store.py::mask_proxy）。

        用户名是账号、不是秘密；而两处规则必须一致 —— 否则同一串地址在两张页面上
        长得不一样，用户会以为其中一处被改过。
        """
        self.assertEqual(proxy.mask("http://u:p@h:1234"), "http://u:***@h:1234")

    def test_只带用户名要整段遮(self):
        # 出口口令经常就是"用户名"这一串，露一半等于没遮
        self.assertEqual(proxy.mask("http://u@h:1234"), "http://***@h:1234")

    def test_没有凭据时原样(self):
        self.assertEqual(proxy.mask("http://h:1234"), "http://h:1234")

    def test_空串还是空串(self):
        self.assertEqual(proxy.mask(""), "")

    def test_解析不了就只给星号(self):
        # 解析失败时绝不能"退回原文"：原文里可能就带着口令
        masked = proxy.mask("http://u:secret@[::1")
        self.assertNotIn("secret", masked)
        self.assertEqual(masked, "***")


class DescribeTests(unittest.TestCase):
    def test_只给_host_port(self):
        self.assertEqual(proxy.describe("http://u:p@h:1234"), "h:1234")

    def test_空串是直连(self):
        self.assertEqual(proxy.describe(""), "直连")

    def test_写坏了也要说得出话(self):
        self.assertIn("写坏", proxy.describe("http://[::1"))


class SourceTextTests(unittest.TestCase):
    def test_三种来源(self):
        self.assertEqual(proxy.source_text("http://a:1", "http://b:2"), "这个账号单独配置")
        self.assertEqual(proxy.source_text("", "http://b:2"), "全局 PROXY_ADDRESS")
        self.assertEqual(proxy.source_text("", ""), "没有配置代理（直连）")


class CheckTests(unittest.TestCase):
    def test_成功时返回_IP_与遮掩后的出口(self):
        seen = {}

        def fetcher(url, proxies, timeout):
            seen["url"] = url
            seen["proxies"] = proxies
            return _Resp(payload={"ip": "203.0.113.7"})

        result = proxy.check("http://u:secret@h:1", fetcher=fetcher)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "203.0.113.7")
        self.assertEqual(result["via"], "http://u:***@h:1")
        self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))
        self.assertEqual(seen["proxies"], {"http": "http://u:secret@h:1", "https": "http://u:secret@h:1"})

    def test_直连时不给_proxies(self):
        seen = {}

        def fetcher(url, proxies, timeout):
            seen["proxies"] = proxies
            return _Resp(payload={"ip": "198.51.100.9"})

        result = proxy.check("", fetcher=fetcher)
        self.assertTrue(result["ok"])
        self.assertEqual(result["via"], "直连")
        self.assertEqual(seen["proxies"], {})

    def test_回显服务报错算失败(self):
        result = proxy.check("http://h:1", fetcher=lambda *a: _Resp(status=502))
        self.assertFalse(result["ok"])
        self.assertIn("502", result["error"])

    def test_异常文案里的口令被替换掉(self):
        def fetcher(url, proxies, timeout):
            raise OSError("cannot connect to proxy http://u:secret@h:1")

        result = proxy.check("http://u:secret@h:1", fetcher=fetcher)
        self.assertFalse(result["ok"])
        self.assertNotIn("secret", result["error"])
        self.assertIn("***@h:1", result["error"])

    def test_不返回_JSON_时退回纯文本(self):
        result = proxy.check("", fetcher=lambda *a: _Resp(payload=None, text="203.0.113.7\n"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "203.0.113.7")

    def test_回显里没有_IP_算失败(self):
        result = proxy.check("", fetcher=lambda *a: _Resp(payload={}, text=""))
        self.assertFalse(result["ok"])
        self.assertIn("没有 IP", result["error"])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="proxy-state-")
        self.path = os.path.join(self.folder, "proxy_state.json")

    def tearDown(self):
        for name in os.listdir(self.folder):
            os.remove(os.path.join(self.folder, name))
        os.rmdir(self.folder)

    def test_写入再读回来(self):
        self.assertTrue(proxy.write_state({"accounts": {"111": {"ip": "1.2.3.4"}}}, self.path))
        self.assertEqual(proxy.read_state(self.path)["accounts"]["111"]["ip"], "1.2.3.4")

    def test_坏文件当没自检过(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ 不是 json")
        self.assertEqual(proxy.read_state(self.path), {})

    def test_不存在也是空(self):
        self.assertEqual(proxy.read_state(os.path.join(self.folder, "nope.json")), {})

    def test_state_for_取单个账号(self):
        state = {"accounts": {"111": {"ip": "1.2.3.4"}}}
        self.assertEqual(proxy.state_for("111", state)["ip"], "1.2.3.4")
        self.assertEqual(proxy.state_for("222", state), {})
        self.assertEqual(proxy.state_for("111", {}), {})
        self.assertEqual(proxy.state_for("111", {"accounts": []}), {})


if __name__ == "__main__":
    unittest.main()