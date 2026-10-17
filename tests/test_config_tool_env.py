"""configTool/env_store.py 的写盘行为单测（缺陷 B 的回归）。

为什么单独一个文件：这里必须 import `configTool.env_store`，而它依赖
python-dotenv（本机可能没装）。`tests/test_config_tool.py` 是纯数据层单测，
保持零依赖；这边把 import 包在 try 里，缺依赖时整组 skip —— 顺便借用
`tests/test_env_store.py` 里那份最小垫片（它自己会判断要不要装）。

覆盖四件事（都是"以前没有、出事看不出来"的类型）：
  1. 只写与磁盘现值不同的键（值没变的键一个字节都不动）；
  2. 整批改动先落临时文件、**一次** os.replace 到真身（半途失败原文件不动）；
  3. 写前留备份（`.env.bak-<时间戳>`），只保留最新几份；
  4. `.env` 是软链时不许把它替换成普通文件。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

env_store = None
models = None
IMPORT_ERROR = None
STUBBED = False


def _load_dependencies() -> None:
    """装好测试用的 dotenv 垫片（只在缺真包时），再 import env_store。

    放在函数里而不是模块顶层：discover 会先把所有测试模块 import 一遍，而那份
    垫片会往 sys.modules 里塞一个没有 `__spec__` 的 `dotenv` —— 谁在它之后被
    import，谁就在 `importlib.util.find_spec("dotenv")` 上吃到
    `ValueError: dotenv.__spec__ is None`（tests/test_entrypoint_cron.py 就是）。
    测试真正开跑时 import 早就结束了，那会儿再装垫片没有这个副作用。
    """
    global env_store, models, IMPORT_ERROR, STUBBED
    if env_store is not None or IMPORT_ERROR is not None:
        return
    try:  # 借 tests/test_env_store.py 里那份最小垫片；它改名也不该让本文件崩
        from tests.test_env_store import _install_dotenv_stub_if_missing

        STUBBED = bool(_install_dotenv_stub_if_missing())
    except Exception:  # pragma: no cover - 只防御，正常不会走到
        STUBBED = False
    try:
        from configTool import env_store as loaded_env_store, models as loaded_models
    except Exception as exc:  # pragma: no cover - 缺依赖时整组跳过
        IMPORT_ERROR = exc
        return
    env_store = loaded_env_store
    models = loaded_models


def _normal(path) -> str:
    return os.path.realpath(str(path))


class TempEnvCase(unittest.TestCase):
    """给每个用例一个独立目录，`.env` 写在里面（绝不碰仓库根的真 .env）。"""

    @classmethod
    def setUpClass(cls):
        _load_dependencies()
        if env_store is None:
            raise unittest.SkipTest(f"缺依赖，整组跳过：{IMPORT_ERROR}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.env_path = self.root / ".env"

    def write_env(self, text: str) -> None:
        self.env_path.write_text(text, encoding="utf-8")

    def read_env(self) -> str:
        return self.env_path.read_text(encoding="utf-8")

    def backups(self) -> list:
        return sorted(p.name for p in self.root.glob(".env.bak-*"))

    def temp_leftovers(self) -> list:
        return sorted(p.name for p in self.root.iterdir() if ".configtool-" in p.name)


class OnlyChangedKeysAreWrittenTests(TempEnvCase):
    """值没变的键一个字节都不动 —— 别把"没变化的保存"变成"重写一遍 .env"。"""

    def test_first_save_writes_everything(self):
        cfg = models.Config.from_env_map({})
        notes, orphans = env_store.save_config(cfg, self.env_path)
        self.assertEqual(orphans, [])
        self.assertTrue(self.env_path.exists())
        self.assertIn("已创建", " ".join(notes))

    def test_second_save_without_changes_does_not_touch_the_file(self):
        cfg = models.Config.from_env_map({})
        env_store.save_config(cfg, self.env_path)
        before = self.read_env()
        stamp = 1_000_000_000  # 钉住 mtime：真写了的话它会变
        os.utime(self.env_path, (stamp, stamp))
        backups_before = self.backups()

        notes, _orphans = env_store.save_config(cfg, self.env_path)

        self.assertEqual(self.read_env(), before, "没有变化却把文件重写了")
        self.assertEqual(os.stat(self.env_path).st_mtime, stamp, "文件被碰过了")
        self.assertIn("没有需要写入的变化", " ".join(notes))
        self.assertEqual(self.backups(), backups_before, "没变化也备份会挤掉有用的旧备份")

    def test_only_the_changed_key_is_handed_to_set_key(self):
        cfg = models.Config.from_env_map({})
        env_store.save_config(cfg, self.env_path)
        cfg.proxy_address = "http://127.0.0.1:1080"

        seen: list = []
        real_set_key = env_store.set_key

        def spy(path, key, value, *args, **kwargs):
            seen.append(key)
            return real_set_key(path, key, value, *args, **kwargs)

        with mock.patch.object(env_store, "set_key", spy):
            notes, _orphans = env_store.save_config(cfg, self.env_path)

        self.assertEqual(seen, ["PROXY_ADDRESS"], "只该动真正变化的那个键")
        self.assertIn("PROXY_ADDRESS=http://127.0.0.1:1080", self.read_env())
        self.assertTrue(any("备份" in note for note in notes), notes)


class AtomicWriteTests(TempEnvCase):
    """整批改动一次 replace；失败时原文件分毫不动、临时文件清干净。"""

    def test_all_changes_land_in_a_single_replace(self):
        cfg = models.Config.from_env_map({})
        env_store.save_config(cfg, self.env_path)
        cfg.proxy_address = "http://127.0.0.1:1080"
        cfg.log_level = "DEBUG"
        cfg.schedule_jitter_minutes = 40

        real_replace = os.replace
        calls: list = []

        def spy(src, dst, *args, **kwargs):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("os.replace", spy):
            env_store.save_config(cfg, self.env_path)

        onto_env = [c for c in calls if _normal(c[1]) == _normal(self.env_path)]
        self.assertEqual(len(onto_env), 1, f"真身只该被替换一次，实际 {onto_env}")
        src, _dst = onto_env[0]
        self.assertEqual(_normal(Path(src).parent), _normal(self.env_path.parent),
                         "临时文件必须与 .env 同目录（跨盘 rename 不是原子的）")
        text = self.read_env()
        self.assertIn("LOG_LEVEL=DEBUG", text)
        self.assertIn("SCHEDULE_JITTER_MINUTES=40", text)

    def test_failure_leaves_the_original_intact(self):
        cfg = models.Config.from_env_map({})
        env_store.save_config(cfg, self.env_path)
        before = self.read_env()
        cfg.log_level = "DEBUG"

        def boom(*_args, **_kwargs):
            raise RuntimeError("写盘炸了")

        with mock.patch.object(env_store, "set_key", boom):
            with self.assertRaises(RuntimeError):
                env_store.save_config(cfg, self.env_path)

        self.assertEqual(self.read_env(), before, "失败的保存改动了原文件")
        self.assertEqual(self.temp_leftovers(), [], "临时文件没清干净")
        self.assertTrue(self.backups(), "写盘前应该已经留下备份")


class BackupTests(TempEnvCase):
    """写前备份：名字带时间戳、同秒不互相覆盖、只留最新几份。"""

    def test_backup_holds_the_previous_content(self):
        env_store.save_config(models.Config.from_env_map({}), self.env_path)
        old_text = self.read_env()
        cfg = models.Config.from_env_map({})
        cfg.log_level = "DEBUG"
        env_store.save_config(cfg, self.env_path)

        names = self.backups()
        self.assertEqual(len(names), 1, names)
        self.assertEqual((self.root / names[0]).read_text(encoding="utf-8"), old_text)

    def test_two_saves_in_the_same_second_do_not_overwrite_each_other(self):
        for level in ("INFO", "DEBUG", "WARNING"):
            cfg = models.Config.from_env_map({})
            cfg.log_level = level
            env_store.save_config(cfg, self.env_path)
        names = self.backups()
        self.assertEqual(len(names), len(set(names)), f"备份名撞车了：{names}")
        self.assertGreaterEqual(len(names), 2, f"第二次写盘的备份把第一次盖了：{names}")

    def test_only_the_newest_backups_are_kept(self):
        from configTool.env_store import BACKUP_KEEP

        env_store.save_config(models.Config.from_env_map({}), self.env_path)
        # 先摆七份"旧备份"，mtime 依次递减
        for index in range(7):
            old = self.root / f".env.bak-2020010{index}-000000"
            old.write_text(f"# old {index}\n", encoding="utf-8")
            os.utime(old, (1_000_000 + index, 1_000_000 + index))
        cfg = models.Config.from_env_map({})
        cfg.log_level = "DEBUG"
        env_store.save_config(cfg, self.env_path)

        names = self.backups()
        self.assertEqual(len(names), BACKUP_KEEP, names)
        # 留下的必须是最新的那几份：最旧的（20200100）已经被删掉
        self.assertNotIn(".env.bak-20200100-000000", names)
        self.assertIn(".env.bak-20200106-000000", names)


class RiskyValueRefusalTests(TempEnvCase):
    """拒绝会被 .env 解析规则改写的值（写进去是 A、读出来是 B）。"""

    def test_shell_expansion_is_refused(self):
        cfg = models.Config.from_env_map({})
        cfg.message_template = "你好${昵称}今天"
        reasons = dict(env_store.risky_values(cfg))
        self.assertIn("MESSAGE_TEMPLATE", reasons)
        self.assertIn("插值", reasons["MESSAGE_TEMPLATE"])
        with self.assertRaises(env_store.RiskyValueError):
            env_store.save_config(cfg, self.env_path)

    def test_hash_without_a_space_is_fine(self):
        """`话题#打卡` 不会被截断（python-dotenv 只认「空白 + #」）。"""
        cfg = models.Config.from_env_map({})
        cfg.message_template = "话题#打卡"
        self.assertEqual(env_store.risky_values(cfg), [])
        env_store.save_config(cfg, self.env_path)
        self.assertIn("话题#打卡", self.read_env())

    def test_hash_after_a_space_is_refused(self):
        cfg = models.Config.from_env_map({})
        cfg.message_template = "话题 #打卡"
        self.assertEqual([key for key, _why in env_store.risky_values(cfg)],
                         ["MESSAGE_TEMPLATE"])


class ReadPathTests(TempEnvCase):
    """读进来的值必须与文件里一致（不插值），空值不算"已配置"。"""

    def test_load_config_does_not_interpolate(self):
        self.write_env("MESSAGE_TEMPLATE=你好${昵称}今天\n")
        cfg, _notes = env_store.load_config(self.env_path)
        self.assertEqual(cfg.message_template, "你好${昵称}今天")

    def test_read_keys_ignores_empty_values(self):
        self.write_env("COOKIES_1=\nEMPTY=\nFOO=bar\n")
        self.assertEqual(env_store.read_keys(self.env_path), ["FOO"])

    def test_read_keys_on_missing_file(self):
        self.assertEqual(env_store.read_keys(self.env_path), [])


class UnsetKeysTests(TempEnvCase):
    """删除计数必须是真删掉的数量（不是入参个数）。"""

    def test_only_existing_keys_count(self):
        self.write_env("A=1\nB=2\nC=3\n")
        removed = env_store.unset_keys(["A", "NOPE", "C", "ALSO_MISSING"], self.env_path)
        self.assertEqual(removed, 2)
        self.assertEqual(env_store.read_keys(self.env_path), ["B"])

    def test_missing_file_is_zero(self):
        self.assertEqual(env_store.unset_keys(["A"], self.env_path), 0)


@unittest.skipIf(os.name != "posix", "软链只在 POSIX 上测")
class SymlinkEnvTests(TempEnvCase):
    """.env 是软链时：要么跟着真身写，要么明确拒绝 —— 绝不把软链换成普通文件。"""

    def setUp(self):
        super().setUp()
        self.real = self.root / "real.env"
        self.real.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
        self.env_path.symlink_to(self.real)

    def test_symlink_survives_or_the_save_is_refused(self):
        cfg = models.Config.from_env_map({"LOG_LEVEL": "DEBUG"})
        if env_store._dotenv_supports_follow_symlinks():
            env_store.save_config(cfg, self.env_path)
            self.assertTrue(self.env_path.is_symlink(), "软链被换成了普通文件")
            self.assertIn("LOG_LEVEL=DEBUG", self.real.read_text(encoding="utf-8"))
        else:
            with self.assertRaises(env_store.RiskyValueError) as ctx:
                env_store.save_config(cfg, self.env_path)
            self.assertIn("软链接", str(ctx.exception))
            self.assertTrue(self.env_path.is_symlink(), "拒绝保存却动了软链")
            self.assertEqual(self.real.read_text(encoding="utf-8"), "LOG_LEVEL=INFO\n")


if __name__ == "__main__":
    unittest.main()
