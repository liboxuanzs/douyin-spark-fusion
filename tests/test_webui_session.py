"""登录保持时长（``WEBUI_SESSION_MAX_AGE``）的测试：默认值 / 夹取 / 人话文案 / 四处口径一致。

为什么值得单独一个文件：`webui/config.py` 是纯 stdlib 模块，本机没有 fastapi、dotenv
也能跑；而"改了默认值却忘了改文档和 `.env.example`"这类漂移，只有把四处口径钉在一起
才拦得住（面板自己的端到端用例在 ``tests/test_webui.py``，需要 fastapi）。

背景：默认值原来写死 8 小时，一天里干两趟活就要重输一次口令 —— 现在默认 30 天，
上限 400 天（浏览器对 cookie 有效期的硬上限，写更大也会被截断）。
"""

from __future__ import annotations

import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from webui import config  # noqa: E402


class SessionMaxAgeTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("WEBUI_SESSION_MAX_AGE", None)

    def tearDown(self) -> None:
        os.environ.pop("WEBUI_SESSION_MAX_AGE", None)

    def test_默认是三十天(self) -> None:
        self.assertEqual(config.DEFAULT_SESSION_MAX_AGE, 30 * 24 * 3600)
        self.assertEqual(config.session_max_age(), config.DEFAULT_SESSION_MAX_AGE)

    def test_读环境变量(self) -> None:
        os.environ["WEBUI_SESSION_MAX_AGE"] = "604800"
        self.assertEqual(config.session_max_age(), 604800)
        os.environ["WEBUI_SESSION_MAX_AGE"] = "  3600  "
        self.assertEqual(config.session_max_age(), 3600, "首尾空白要容忍")

    def test_非法值回落默认(self) -> None:
        for raw in ("", "   ", "abc", "0", "-1", "3.5", "1e999"):
            with self.subTest(raw=raw):
                os.environ["WEBUI_SESSION_MAX_AGE"] = raw
                self.assertEqual(config.session_max_age(), config.DEFAULT_SESSION_MAX_AGE)

    def test_超过四百天夹到四百天(self) -> None:
        """Chrome 对 cookie 有效期有 400 天硬上限 —— 界面上别显示"114 年"。"""
        os.environ["WEBUI_SESSION_MAX_AGE"] = str(config.MAX_SESSION_MAX_AGE + 1)
        self.assertEqual(config.session_max_age(), config.MAX_SESSION_MAX_AGE)
        os.environ["WEBUI_SESSION_MAX_AGE"] = "999999999999"
        self.assertEqual(config.session_max_age(), config.MAX_SESSION_MAX_AGE)
        self.assertEqual(config.MAX_SESSION_MAX_AGE, 400 * 24 * 3600)

    def test_人话文案(self) -> None:
        cases = {
            "2592000": "30 天",
            "86400": "1 天",
            "129600": "1.5 天",
            "28800": "8 小时",
            "5400": "1.5 小时",
            "1800": "30 分钟",
            "45": "45 秒",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                os.environ["WEBUI_SESSION_MAX_AGE"] = raw
                self.assertEqual(config.session_max_age_text(), want)

    def test_人话文案的默认值是三十天(self) -> None:
        self.assertEqual(config.session_max_age_text(), "30 天")

    def test_导出清单包含新名字(self) -> None:
        for name in ("DEFAULT_SESSION_MAX_AGE", "MAX_SESSION_MAX_AGE", "session_max_age_text"):
            self.assertIn(name, config.__all__)


class SessionMaxAgeAlignmentTests(unittest.TestCase):
    """四处口径必须一致：代码默认值 / 面板白名单默认值 / `.env.example` / `docs/webui.md`。"""

    def test_面板白名单里有这个键且默认值一致(self) -> None:
        src = (REPO_ROOT / "webui" / "settings.py").read_text(encoding="utf-8")
        self.assertIn('"WEBUI_SESSION_MAX_AGE"', src)
        block = src.split('"WEBUI_SESSION_MAX_AGE"', 1)[1].split("),", 1)[0]
        self.assertIn(f'default="{config.DEFAULT_SESSION_MAX_AGE}"', block)
        self.assertIn("KIND_INT", block)
        self.assertIn(f"maximum={config.MAX_SESSION_MAX_AGE // 86400} * 24 * 3600", block)

    def test_env_example_与代码一致(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn(f"WEBUI_SESSION_MAX_AGE={config.DEFAULT_SESSION_MAX_AGE}", text)

    def test_docs_与代码一致(self) -> None:
        text = (REPO_ROOT / "docs" / "webui.md").read_text(encoding="utf-8")
        self.assertIn(f"| `WEBUI_SESSION_MAX_AGE` | `{config.DEFAULT_SESSION_MAX_AGE}`", text)

    def test_注释里不再写八小时(self) -> None:
        """文档漂移是最容易发生的一种：默认值改了，注释还写着老数字。"""
        for rel in ("webui/config.py", "webui/settings.py", ".env.example", "docs/webui.md"):
            with self.subTest(rel=rel):
                text = (REPO_ROOT / rel).read_text(encoding="utf-8")
                for line in text.splitlines():
                    if "WEBUI_SESSION_MAX_AGE" in line or "登录保持" in line:
                        self.assertNotIn("默认 8 小时", line, f"{rel}: {line}")
                        self.assertNotIn("28800", line, f"{rel}: {line}")


class SessionMaxAgeReadPathTests(unittest.TestCase):
    """`read_token` 与 `set_cookie` 都得走这个函数 —— 否则"延长了"只是界面上的说法。"""

    def test_读回与下发都引用同一个函数(self) -> None:
        auth_src = (REPO_ROOT / "webui" / "auth.py").read_text(encoding="utf-8")
        self.assertIn("max_age=config.session_max_age()", auth_src)
        app_src = (REPO_ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("max_age=config.session_max_age()", app_src)
        self.assertIn("config.session_max_age_text()", app_src)

    def test_两天前签发的会话在新默认下仍然有效(self) -> None:
        """这次改动的用户可见证据：两天前登录的浏览器，在老口径（8 小时）下必须重输口令，
        在新默认（30 天）下不该被踢。做法是把 itsdangerous 取时间的那个模块属性换成假的。"""
        try:
            import itsdangerous.timed
            from webui import auth
        except Exception:  # noqa: BLE001 —— 本机没有 itsdangerous 时跳过
            self.skipTest("本机没有 itsdangerous")
        now = time.time()
        fake_time = types.SimpleNamespace(time=lambda: now - 2 * 86400)
        with mock.patch.object(itsdangerous.timed, "time", fake_time):
            token = auth.issue_token("admin")
        self.assertEqual(auth.read_token(token), "admin", "默认 30 天：两天前的会话不该过期")
        with mock.patch.object(config, "session_max_age", return_value=8 * 3600):
            self.assertIsNone(auth.read_token(token), "老的 8 小时口径下它必须过期")

    def test_token_的寿命跟着配置走(self) -> None:
        """时长被读的时候生效：负数 → 立刻过期；改回来 → 同一枚 token 又有效。"""
        try:
            from webui import auth
        except Exception:  # noqa: BLE001
            self.skipTest("本机没有 itsdangerous")
        token = auth.issue_token("admin")
        self.assertEqual(auth.read_token(token), "admin")
        with mock.patch.object(config, "session_max_age", return_value=-1):
            self.assertIsNone(auth.read_token(token), "负数 = 立即过期")
        self.assertEqual(auth.read_token(token), "admin", "恢复配置后旧 token 仍然有效")
