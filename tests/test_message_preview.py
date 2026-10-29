# -*- coding: utf-8 -*-
"""`core.msg_preview` 的测试：面板上的"这一轮会发出去什么"（issue #17）。

预览是**只读的镜子**，但镜子照歪了比没有镜子更糟：用户会照着预览去改模板，
却发现好友收到的还是另一副样子。所以这里钉死的不只是"能算出 3 行"，而是
每条口径都与真正发送的 `core/douyin_im.split_message_lines()` 对齐：

  * 字面 `\n` / 真换行 / 裸 `\r` 都算换行；
  * 只有空白的行整行丢掉，**行数按丢掉之后算**（否则"3 行"里有一行是假的）；
  * 零宽字符与两侧空格发送前会被删，预览也要说；
  * 只认大写方括号 `[API]`，写成 `[api]` 会原样发给好友 —— 这条必须警告；
  * 预览绝不联网：一言位置用示例文案顶着，并标出 sample=True。
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import msg_preview  # noqa: E402

GLOBAL = "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"


def codes(result):
    return [warn["code"] for warn in result["warnings"]]


class NormalizeTests(unittest.TestCase):
    def test_字面反斜杠n变成真换行(self) -> None:
        self.assertEqual(msg_preview.normalize_template("a\\nb"), "a\nb")

    def test_真换行保持不变(self) -> None:
        self.assertEqual(msg_preview.normalize_template("a\nb"), "a\nb")

    def test_裸回车也当换行(self) -> None:
        self.assertEqual(msg_preview.normalize_template("a\rb"), "a\nb")

    def test_两种换行混写不会变成两行(self) -> None:
        # 先收字面 \r\n 再收字面 \n，否则 "a\\r\\nb" 会拆成 "a" + 空 + "b"。
        self.assertEqual(msg_preview.normalize_template("a\\r\\nb"), "a\nb")

    def test_空模板(self) -> None:
        self.assertEqual(msg_preview.normalize_template(""), "")
        self.assertEqual(msg_preview.normalize_template(None), "")


class PreviewLineTests(unittest.TestCase):
    def test_字面换行模板拆成三行且替换一言(self) -> None:
        result = msg_preview.preview(GLOBAL)
        self.assertTrue(result["ok"])
        self.assertEqual(result["line_count"], 3)
        self.assertEqual(result["lines"][0]["text"], "[盖瑞]今日火花[加一]")
        self.assertEqual(result["lines"][2], {"text": msg_preview.SAMPLE_LINE, "sample": True})
        self.assertTrue(result["has_api"])

    def test_行数与字数都按丢掉空行之后算(self) -> None:
        result = msg_preview.preview("第一行\n\n\n第二行")
        self.assertEqual(result["line_count"], 2)
        self.assertEqual(result["blank_lines"], 2)
        self.assertIn("blank_line", codes(result))
        self.assertTrue(result["summary"].startswith("2 行"))

    def test_只有空白的行被丢掉且行数不算它(self) -> None:
        result = msg_preview.preview("甲\n   \n乙")
        self.assertEqual([line["text"] for line in result["lines"]], ["甲", "乙"])

    def test_零宽字符会被删掉并提示(self) -> None:
        result = msg_preview.preview("你好\u200b世界")
        self.assertEqual(result["lines"][0]["text"], "你好世界")
        self.assertIn("zero_width", codes(result))

    def test_首尾空格会被去掉并提示(self) -> None:
        result = msg_preview.preview("  你好  ")
        self.assertEqual(result["lines"][0]["text"], "你好")
        self.assertIn("padded", codes(result))

    def test_没有一言时sample为假(self) -> None:
        result = msg_preview.preview("你好")
        self.assertFalse(result["has_api"])
        self.assertFalse(result["lines"][0]["sample"])
        self.assertNotIn("含示例一言", result["summary"])

    def test_可以指定示例一言(self) -> None:
        result = msg_preview.preview("[API]", sample="示例一句话")
        self.assertEqual(result["lines"][0]["text"], "示例一句话")
        self.assertEqual(result["lines"][0]["sample"], True)


class PlaceholderTests(unittest.TestCase):
    def test_只填API会被警告一言取不到就整条不发(self) -> None:
        result = msg_preview.preview("[API]")
        self.assertIn("api_only", codes(result))
        self.assertTrue(result["has_api"])

    def test_小写api原样保留并警告会发给好友(self) -> None:
        result = msg_preview.preview("[api]")
        self.assertEqual(result["lines"][0]["text"], "[api]")
        self.assertFalse(result["has_api"])
        self.assertIn("api_case", codes(result))

    def test_全角括号的API也算写错了(self) -> None:
        result = msg_preview.preview("【API】")
        self.assertIn("api_case", codes(result))
        self.assertFalse(result["has_api"])

    def test_同时有大写和小写时大写生效且仍然警告(self) -> None:
        result = msg_preview.preview("[API]\n[api]")
        self.assertTrue(result["has_api"])
        self.assertIn("api_case", codes(result))
        self.assertEqual(result["lines"][1]["text"], "[api]")

    def test_行内一言也会被替换(self) -> None:
        result = msg_preview.preview("今天：[API] 好")
        self.assertEqual(result["lines"][0]["text"], "今天：" + msg_preview.SAMPLE_LINE + " 好")


class WarningTests(unittest.TestCase):
    def test_空模板明确不可发(self) -> None:
        result = msg_preview.preview("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["line_count"], 0)
        self.assertEqual(result["summary"], "0 行 · 没有内容")
        self.assertIn("empty", codes(result))

    def test_逐行超过两百字提示(self) -> None:
        result = msg_preview.preview("字" * (msg_preview.LINE_SOFT_LIMIT + 1))
        self.assertIn("line_long", codes(result))

    def test_整条超过五百字提示(self) -> None:
        line = "字" * 180
        result = msg_preview.preview("\n".join([line] * 3))
        self.assertIn("message_long", codes(result))

    def test_正常模板没有任何警告(self) -> None:
        result = msg_preview.preview(GLOBAL)
        self.assertEqual(codes(result), [])
        self.assertFalse(result["has_warnings"])

    def test_警告等级只有info和warn两种(self) -> None:
        result = msg_preview.preview("  [api]  \n\n" + "字" * 300)
        levels = {warn["level"] for warn in result["warnings"]}
        self.assertTrue(levels)
        self.assertLessEqual(levels, {"info", "warn"})

    def test_每条警告都带人话(self) -> None:
        for template in ("", "[API]", "[api]", "  甲  ", "甲\n\n乙", "字" * 300):
            for warn in msg_preview.preview(template)["warnings"]:
                self.assertGreaterEqual(len(warn["text"]), 8, warn)


class SummaryTests(unittest.TestCase):
    def test_带一言时同时给出两个字数(self) -> None:
        result = msg_preview.preview(GLOBAL)
        self.assertRegex(result["summary"], r"^3 行 · \d+ 字（含示例一言 \d+ 字）$")

    def test_含示例一定不小于不含示例(self) -> None:
        for text in (GLOBAL, "你好", "[API]", "甲\n乙\n[API]"):
            result = msg_preview.preview(text)
            self.assertLessEqual(result["chars"], result["chars_with_sample"])
            self.assertGreaterEqual(result["chars"], 0)

    def test_示例一言本身不计入不含示例的字数(self) -> None:
        plain = msg_preview.preview("你好")
        with_api = msg_preview.preview("你好\n[API]")
        self.assertEqual(plain["chars"], with_api["chars"])


class ContractWithSenderTests(unittest.TestCase):
    def test_零宽字符集合与发送端逐字一致(self) -> None:
        from core import douyin_im

        self.assertEqual(msg_preview.ZERO_WIDTH_CHARS, douyin_im.ZERO_WIDTH_CHARS)

    def test_占位符与发送端一致(self) -> None:
        from core import msg_builder

        source = open(
            os.path.join(ROOT, "core", "msg_builder.py"), encoding="utf-8"
        ).read()
        self.assertIn("[API]", source)
        self.assertEqual(msg_preview.API_TOKEN, "[API]")

    def test_模块本身不联网(self) -> None:
        source = open(
            os.path.join(ROOT, "core", "msg_preview.py"), encoding="utf-8"
        ).read()
        for forbidden in ("requests", "httpx", "urllib", "hitokoto.render"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
