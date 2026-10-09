"""`core.msg_builder` 的测试：账号专属内容与全局模板的取舍。

这个模块决定"这一轮到底发出去什么字"，改错的后果是**发错内容**（而且是在
真实账号上、真人可见）。所以这里把两条口径钉死：

  * `render_message()` 只做换行归一 + `[API]` 替换，**不碰**别的东西；
  * `build_message(template)`：给了非空内容就用它，空则回落全局 `MESSAGE_TEMPLATE`。

第二条是"存量账号零影响"的全部保证：老 `.env` 的 TASKS 里没有 `message` 这个键，
读出来是空串，必须走回全局模板 —— 一旦把空串当成"发空消息"，所有老账号
会在下一次定时任务里集体哑火。
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import msg_builder  # noqa: E402

GLOBAL = "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"


class _MsgBuilderCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            msg_builder, "get_config", lambda: {"messageTemplate": GLOBAL}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 一言每次调用都取新的一条；测试里固定成一句话，断言才有确定性。
        p = mock.patch.object(msg_builder, "request_hitokoto", lambda: "今天也要开心")
        p.start()
        self.addCleanup(p.stop)


class BuildMessageTests(_MsgBuilderCase):
    def test_没配过就用全局模板(self) -> None:
        self.assertEqual(msg_builder.build_message(), msg_builder.build_message(""))

    def test_只有空白也算没配过(self) -> None:
        """空白不是"发空白"，是"没配" —— 否则面板里误敲一个空格就静默失效。"""
        self.assertEqual(
            msg_builder.build_message("   \n  "),
            msg_builder.build_message(""),
        )

    def test_账号专属内容优先于全局模板(self) -> None:
        got = msg_builder.build_message("只发给这个账号的一句话")
        self.assertEqual(got, "只发给这个账号的一句话")
        self.assertNotIn("盖瑞", got)

    def test_账号专属内容里的一言占位符照样替换(self) -> None:
        self.assertEqual(msg_builder.build_message("今天：[API]"), "今天：今天也要开心")

    def test_账号专属内容只取一次一言(self) -> None:
        """每个好友各渲染一次，所以两次调用是两条不同的一言（这里断言调用次数）。"""
        with mock.patch.object(msg_builder, "request_hitokoto",
                               side_effect=["第一句", "第二句"]) as hit:
            self.assertEqual(msg_builder.build_message("[API]"), "第一句")
            self.assertEqual(msg_builder.build_message("[API]"), "第二句")
        self.assertEqual(hit.call_count, 2)

    def test_全局模板也走同一条渲染(self) -> None:
        """回落之后不能跳过 `[API]` 替换 —— 那是老功能。"""
        self.assertIn("今天也要开心", msg_builder.build_message(""))

    def test_取值失败时兜底成续火花(self) -> None:
        with mock.patch.object(msg_builder, "get_config", lambda: {}):
            self.assertEqual(msg_builder.build_message(""), "续火花")


class RenderMessageTests(_MsgBuilderCase):
    def test_字面换行归一(self) -> None:
        """`.env` 里手写的字面 `\\r\\n` 要收成字面 `\\n`，不能漏出裸露的 `\\r`。"""
        self.assertEqual(msg_builder.render_message("甲\\r\\n乙"), "甲\\n乙")

    def test_真换行归一(self) -> None:
        self.assertEqual(msg_builder.render_message("甲\r\n乙"), "甲\n乙")

    def test_首尾空白裁掉(self) -> None:
        self.assertEqual(msg_builder.render_message("\n  甲  \n"), "甲")

    def test_空输入就是空字符串(self) -> None:
        self.assertEqual(msg_builder.render_message(None), "")
        self.assertEqual(msg_builder.render_message("   "), "")

    def test_没有占位符就不调一言(self) -> None:
        with mock.patch.object(msg_builder, "request_hitokoto") as hit:
            msg_builder.render_message("平平无奇的一句话")
        hit.assert_not_called()

    def test_多行内容原样保留(self) -> None:
        text = "第一行\n第二行\n\n第四行"
        self.assertEqual(msg_builder.render_message(text), text)


if __name__ == "__main__":
    unittest.main()
