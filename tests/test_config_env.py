"""`utils/config.py` 的容错读取：环境变量与 TASKS 解析。

这里的每一条都对应一类**真实事故形状**：手改 `.env` 少一个引号、写个中文逗号、
把 `LIMIT_COOLDOWN_MINUTES` 手滑写成大数、或者把 `targets` 写成 `null`。
以前这些错误会从读配置一路冒到进程顶层 —— 定时器每 5 分钟 exit 1、`runs.jsonl`
零记录、面板「立即运行」503，而日志里只有一行 traceback，看不出是 `.env` 的错。
"""

import json
import os
import unittest
from unittest import mock

from utils import config as config_mod


class _EnvSandbox(unittest.TestCase):
    """改环境变量的用例统一在这里收尾（`os.environ` 是进程级的）。"""

    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._saved)
        config_mod.userData = None


class EnvNumberTests(_EnvSandbox):
    def test_int_garbage_falls_back_and_warns(self):
        os.environ["X_INT"] = "30 分钟"
        with mock.patch.object(config_mod, "logger") as log:
            self.assertEqual(config_mod._env_int("X_INT", 7), 7)
        self.assertTrue(log.warning.called)

    def test_int_infinity_does_not_raise(self):
        """`float("1e999")` 是合法的 inf —— `int(inf)` 抛的是 OverflowError，不是 ValueError。"""
        os.environ["X_INT"] = "1e999"
        with mock.patch.object(config_mod, "logger"):
            self.assertEqual(config_mod._env_int("X_INT", 3), 3)

    def test_int_is_clamped_on_both_sides(self):
        os.environ["X_INT"] = "99999"
        with mock.patch.object(config_mod, "logger") as log:
            self.assertEqual(config_mod._env_int("X_INT", 30, minimum=1, maximum=1440), 1440)
        self.assertTrue(log.warning.called)
        os.environ["X_INT"] = "0"
        with mock.patch.object(config_mod, "logger"):
            self.assertEqual(config_mod._env_int("X_INT", 30, minimum=1, maximum=1440), 1)

    def test_number_garbage_and_infinity(self):
        os.environ["X_NUM"] = "8s"
        with mock.patch.object(config_mod, "logger"):
            self.assertEqual(config_mod._env_number("X_NUM", 8.0), 8.0)
        # inf / nan 能过 float()，但拿去比大小、算间隔、算冷却全是错的
        # （nan 的比较恒为 False，会静默失效）→ 一律回落默认值，而不是钳到上限。
        os.environ["X_NUM"] = "inf"
        with mock.patch.object(config_mod, "logger") as log:
            self.assertEqual(config_mod._env_number("X_NUM", 8.0, maximum=120), 8.0)
        self.assertTrue(log.warning.called)
        os.environ["X_NUM"] = "nan"
        with mock.patch.object(config_mod, "logger"):
            self.assertEqual(config_mod._env_number("X_NUM", 8.0, minimum=1, maximum=120), 8.0)

    def test_ms_is_clamped(self):
        os.environ["X_MS"] = "-5"
        with mock.patch.object(config_mod, "logger"):
            self.assertEqual(config_mod._env_ms("X_MS", 1000, minimum=0), 0)

    def test_empty_value_means_default_without_warning(self):
        os.environ["X_INT"] = "   "
        with mock.patch.object(config_mod, "logger") as log:
            self.assertEqual(config_mod._env_int("X_INT", 5), 5)
        self.assertFalse(log.warning.called, "空值＝没配，不该刷告警")


class UserDataToleranceTests(_EnvSandbox):
    """TASKS 是整轮任务的输入，写坏了要"当空名单 + 留痕"，绝不能把进程掀翻。"""

    COOKIES = json.dumps([{"name": "sessionid", "value": "x"}])

    def _parse(self, tasks_raw, cookies=COOKIES, key="COOKIES_1"):
        os.environ["TASKS"] = tasks_raw
        if cookies is not None:
            os.environ[key] = cookies
        config_mod.userData = None
        with mock.patch.object(config_mod, "logger"):
            return config_mod.get_userData()

    def test_broken_json_is_an_empty_roster_not_a_crash(self):
        self.assertEqual(self._parse('[{"username": "x",'), [])
        self.assertEqual(self._parse("[1, 2,"), [])
        self.assertEqual(self._parse(""), [])

    def test_single_object_is_wrapped(self):
        got = self._parse(json.dumps({"username": "甲", "unique_id": "1", "targets": ["乙"]}))
        self.assertEqual([u["unique_id"] for u in got], ["1"])
        self.assertEqual(got[0]["targets"], ["乙"])

    def test_non_list_top_level_is_empty(self):
        self.assertEqual(self._parse('"nope"'), [])
        self.assertEqual(self._parse("42"), [])

    def test_items_that_are_not_objects_are_skipped(self):
        raw = json.dumps(["nope", {"username": "甲", "unique_id": "1"}])
        got = self._parse(raw)
        self.assertEqual([u["unique_id"] for u in got], ["1"])

    def test_item_without_unique_id_is_skipped(self):
        raw = json.dumps([{"username": "甲", "targets": ["乙"]}])
        self.assertEqual(self._parse(raw), [])

    def test_null_targets_become_an_empty_list(self):
        raw = json.dumps([{"username": "甲", "unique_id": "1", "targets": None}])
        got = self._parse(raw)
        self.assertEqual(got[0]["targets"], [], "`list(None)` 会抛 TypeError，必须在这里收口")

    def test_non_list_targets_become_an_empty_list(self):
        raw = json.dumps([{"username": "甲", "unique_id": "1", "targets": "乙同学"}])
        self.assertEqual(self._parse(raw)[0]["targets"], [])

    def test_missing_or_broken_cookies_skip_the_account(self):
        raw = json.dumps([{"username": "甲", "unique_id": "1", "targets": ["乙"]}])
        self.assertEqual(self._parse(raw, cookies=None), [])
        self.assertEqual(self._parse(raw, cookies="{不是 json"), [])

    def test_null_message_and_time_become_empty_strings(self):
        raw = json.dumps([{"username": "甲", "unique_id": "1", "targets": [],
                           "message": None, "time": None}])
        got = self._parse(raw)[0]
        self.assertEqual(got["message"], "")
        self.assertEqual(got["time"], "")

    def test_accounts_are_built_locally_then_published_once(self):
        """半成品绝不能挂在全局名上：面板请求与运行线程会同时调用本函数。"""
        raw = json.dumps([{"username": "甲", "unique_id": "1"}, {"username": "乙", "unique_id": "2"}])
        config_mod.userData = None
        with mock.patch.object(config_mod, "logger"):
            os.environ["TASKS"] = raw
            os.environ["COOKIES_1"] = self.COOKIES
            os.environ["COOKIES_2"] = self.COOKIES
            got = config_mod.get_userData()
        self.assertEqual([u["unique_id"] for u in got], ["1", "2"])
        self.assertIs(config_mod.userData, got)


if __name__ == "__main__":
    unittest.main()
