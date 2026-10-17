"""`utils.config` 的时区落地（`_apply_tz`）单测。

背景（审计项）：`load_env_file()` 里那句 `time.tzset()` 原本写在两条早退**之后** ——

    if not os.path.exists(path):  return None      # ← .env 不存在
    except ImportError:           return None      # ← 本机没装 python-dotenv

于是"没有 .env / 没装 dotenv"的部署（宿主时区 UTC）永远调不到 tzset()，
而 `TZ` 默认是 `Asia/Shanghai` → 整张发送时间表平移 8 小时：面板显示 20:30 发，
真正发车在 12:30，且这一轮一条报错都没有。

这组用例只断言"tzset 被调到 / 不该被调时不调"，不去断言真实时区偏移 ——
后者依赖宿主装没装 tzdata，跑测试的机器上不可控。
"""

from __future__ import annotations

import builtins
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import config as config_mod  # noqa: E402


class _EnvFileTests(unittest.TestCase):
    """公共夹具：临时 .env、干净的 TZ/ENV_FILE、以及每个用例后复位模块状态。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_path = os.path.join(self._tmp.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("WEBUI_PORT=8787\n")

        # 不要用 mock.patch.dict(clear=True)：清空整个环境会顺手抹掉 PATH / HOME /
        # PYTHONPATH，出问题时表现成"毫无关系的 import 失败"。只动这两个键。
        self._env_saved = {k: os.environ[k] for k in ("TZ", "ENV_FILE", "LOG_LEVEL")
                           if k in os.environ}
        for key in ("TZ", "ENV_FILE", "LOG_LEVEL"):
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env)

        # 模块状态复位：`_TZ_APPLIED` 是"已落地过的 TZ"缓存，`config` 是 get_config()
        # 的整份缓存 —— 都必须在用例之间清零，否则断言取决于执行顺序。
        config_mod._TZ_APPLIED = None
        config_mod.config = None
        self.addCleanup(setattr, config_mod, "_TZ_APPLIED", None)
        self.addCleanup(setattr, config_mod, "config", None)

    def _restore_env(self):
        for key in ("TZ", "ENV_FILE", "LOG_LEVEL"):
            os.environ.pop(key, None)
        os.environ.update(self._env_saved)


class ApplyTzTests(_EnvFileTests):
    def test_tzset_runs_when_env_file_missing(self):
        """`.env` 不存在也必须调 tzset（原来第一条 return 就把它跳过了）。"""
        os.environ["TZ"] = "Asia/Shanghai"
        missing = os.path.join(self._tmp.name, "根本没有这个文件")
        with mock.patch.object(config_mod.time, "tzset") as fake:
            self.assertIsNone(config_mod.load_env_file(missing))
        self.assertEqual(fake.call_count, 1, "没有 .env 时 tzset 必须被调到")

    def test_tzset_runs_when_dotenv_missing(self):
        """本机没装 python-dotenv（精简镜像就是这样）也必须调 tzset。"""
        os.environ["TZ"] = "Asia/Shanghai"
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            # 只拦 dotenv，别的照常走真 import —— 覆盖的正是那条
            # `except Exception: return None`（真没装包时的表现）。
            if name == "dotenv" or name.startswith("dotenv."):
                raise ImportError("No module named 'dotenv'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", side_effect=fake_import):
            with mock.patch.object(config_mod.time, "tzset") as fake:
                self.assertIsNone(config_mod.load_env_file(self.env_path))
        self.assertEqual(fake.call_count, 1, "缺 python-dotenv 时 tzset 必须被调到")

    def test_tzset_only_on_tz_change(self):
        """同一个 TZ 连续读两次只调一次 —— webui 每次保存配置都会 reload_env()。"""
        os.environ["TZ"] = "Asia/Shanghai"
        missing = os.path.join(self._tmp.name, "根本没有这个文件")
        with mock.patch.object(config_mod.time, "tzset") as fake:
            config_mod.load_env_file(missing)
            config_mod.load_env_file(missing)
            self.assertEqual(fake.call_count, 1, "TZ 没变就不该重复 tzset")
            os.environ["TZ"] = "UTC"          # 值真的变了要能再落地一次
            config_mod.load_env_file(missing)
            self.assertEqual(fake.call_count, 2, "TZ 变了必须重新落地")

    def test_default_tz_used_when_unset(self):
        """TZ 没设时按 DEFAULT_TZ（Asia/Shanghai）落地，并且要**写回环境变量**。

        ⚠️ 回写不是顺手做的：`tzset()` 认的是 `os.environ["TZ"]` —— 它没设时 tzset()
        按宿主 `/etc/localtime` 落地（实测），"默认 Asia/Shanghai"这条就等于没生效，
        UTC 宿主上照旧平移 8 小时。所以这里断言 TZ 被补成默认值（`_restore_env`
        会在用例结束后还原）。
        """
        self.assertNotIn("TZ", os.environ)
        missing = os.path.join(self._tmp.name, "根本没有这个文件")
        with mock.patch.object(config_mod.time, "tzset") as fake:
            config_mod.load_env_file(missing)
        self.assertEqual(fake.call_count, 1, "TZ 未设也要按默认时区落地一次")
        self.assertEqual(os.environ.get("TZ"), config_mod.DEFAULT_TZ,
                         "不回写 TZ 的话 tzset() 落地的还是宿主时区")
        self.assertEqual(config_mod._TZ_APPLIED, config_mod.DEFAULT_TZ)
        self.assertEqual(config_mod.DEFAULT_TZ, "Asia/Shanghai")

    def test_force_reapplies_even_when_tz_unchanged(self):
        """`force=True` 要能绕过"值没变就跳过"（读到 .env 之后走的就是这条）。"""
        os.environ["TZ"] = "Asia/Shanghai"
        with mock.patch.object(config_mod.time, "tzset") as fake:
            config_mod._apply_tz()                    # 首次：落地
            config_mod._apply_tz()                    # 同值：跳过
            self.assertEqual(fake.call_count, 1)
            config_mod._apply_tz(force=True)          # 强制：再落地一次
            self.assertEqual(fake.call_count, 2, "force 必须绕过缓存")

    def test_reading_env_file_reapplies_tz(self):
        """★ 真的读到 `.env` 之后必须重新落地，哪怕缓存里的值看起来一样。

        `main.py` 在 import 期就先跑过一次 `load_env_file()`（缓存这时已经热了），
        所以"值比较"分不出"这个 TZ 来自宿主/默认值"与"它刚从 .env 里读出来"。
        这里用一个最小 dotenv 替身（`sys.modules` 注入，本机没装包也能测）把文件里
        的 TZ 设成**与缓存相同**的值 —— 只有 force 才能解释这一次调用。
        """
        os.environ["TZ"] = "Asia/Shanghai"
        config_mod._apply_tz()
        and_same = {"TZ": "Asia/Shanghai"}

        class _FakeDotenv:
            @staticmethod
            def dotenv_values(path):
                return dict(and_same)

            @staticmethod
            def load_dotenv(path, override=True):
                os.environ.update(and_same)

        import sys

        with mock.patch.dict(sys.modules, {"dotenv": _FakeDotenv}):
            with mock.patch.object(config_mod.time, "tzset") as fake:
                self.assertIsInstance(config_mod.load_env_file(self.env_path), dict)
        self.assertEqual(fake.call_count, 1, "读完 .env 必须调一次 tzset()")

    def test_apply_tz_skips_tzset_on_windows(self):
        """Windows 没有 tzset（os.name != posix）：跳过，不能 AttributeError。"""
        os.environ["TZ"] = "Asia/Shanghai"
        with mock.patch.object(os, "name", "nt"):
            with mock.patch.object(config_mod.time, "tzset",
                                   side_effect=AssertionError("Windows 上不该调 tzset")) as fake:
                config_mod._apply_tz()
        self.assertEqual(fake.call_count, 0)
        self.assertEqual(config_mod._TZ_APPLIED, "Asia/Shanghai")

    def test_apply_tz_survives_missing_tzdata(self):
        """极端环境 tzset() 抛异常也不能把启动拖垮（原来的 try/except 语义）。"""
        os.environ["TZ"] = "Asia/Shanghai"
        with mock.patch.object(config_mod.time, "tzset", side_effect=OSError("no tzdata")):
            config_mod._apply_tz()  # 不该抛
        self.assertEqual(config_mod._TZ_APPLIED, "Asia/Shanghai")


class ResolvePathTests(unittest.TestCase):
    """#7：配置里的相对路径必须锚到**仓库根**，不能跟着 CWD 跑。"""

    def test_relative_path_anchored_to_repo_root(self):
        self.assertEqual(
            config_mod.resolve_path("logs/runs.jsonl"),
            os.path.join(config_mod.REPO_ROOT, "logs", "runs.jsonl"),
        )

    def test_absolute_path_kept(self):
        self.assertEqual(config_mod.resolve_path("/var/log/x.log"), "/var/log/x.log")

    def test_empty_kept(self):
        self.assertIsNone(config_mod.resolve_path(None))
        self.assertEqual(config_mod.resolve_path(""), "")

    def test_repo_root_is_repo_root(self):
        """REPO_ROOT 必须真的是仓库根（有 main.py / core/ 的那一层）。"""
        self.assertTrue(os.path.isfile(os.path.join(config_mod.REPO_ROOT, "main.py")))
        self.assertTrue(os.path.isdir(os.path.join(config_mod.REPO_ROOT, "core")))

    def test_config_defaults_use_repo_root(self):
        """默认账本/锁文件不能还是相对 CWD 的 `logs/...`。"""
        config_mod.config = None
        self.addCleanup(setattr, config_mod, "config", None)
        cfg = config_mod.get_config()
        for key in ("runLedgerFile", "runLockFile", "rateLimitFile", "scheduleStateFile"):
            value = cfg[key]
            self.assertTrue(os.path.isabs(value), f"{key} 不是绝对路径：{value}")
            self.assertTrue(value.startswith(config_mod.REPO_ROOT), f"{key} 不在仓库根下：{value}")


class LogLevelDefaultTests(_EnvFileTests):
    """#9：LOG_LEVEL 的兜底默认值必须是 Info（与 main.py / .env.example / docs 一致）。"""

    def test_default_log_level_is_info(self):
        config_mod.config = None
        self.assertEqual(config_mod.get_config()["logLevel"], "Info")


if __name__ == "__main__":
    unittest.main()
