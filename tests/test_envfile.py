"""`webui.envfile` 的测试：面板扫码登录要**就地**改 `.env`，这件事必须小心翼翼。

三条硬要求：
  * 只动自己管的键（`TASKS` / `COOKIES_*`），注释和别人手写的变量原样保留；
  * 每次写之前先备份，且备份不能无限堆积；
  * 写进去的 Cookie 必须是"真的有 sessionid"的那种，宁可不写也不能写坏。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from webui import envfile  # noqa: E402

UID = "12345678905"
UID2 = "98765432100"

SAMPLE_ENV = """\
# 续火花配置（这份注释必须活下来）
WEBUI_PORT=8787

# 账号清单
TASKS=[{"username":"账号1","unique_id":"12345678905","targets":["好友A"],"fingerprint":12345}]

MESSAGE_TEMPLATE=第一行\\n第二行
COOKIES_12345678905=[{"name":"sessionid","value":"old","domain":".douyin.com"}]

# 口令
WEBUI_PASSWORD_HASH=pbkdf2_sha256$1$aa$bb
"""

LOGIN_COOKIES = json.dumps(
    [
        {"name": "sessionid", "value": "new-secret", "domain": ".douyin.com", "path": "/"},
        {"name": "ttwid", "value": "t", "domain": ".douyin.com", "path": "/"},
    ],
    ensure_ascii=True,
    separators=(",", ":"),
)


class EnvFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dsf-envfile-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.env = self.tmp / ".env"
        self.env.write_text(SAMPLE_ENV, encoding="utf-8")
        self._saved = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._saved is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved

    def text(self) -> str:
        return self.env.read_text(encoding="utf-8")

    def value_of(self, key: str):
        for line in self.text().splitlines():
            if line.startswith(f"{key}="):
                return line[len(key) + 1 :]
        return None

    def backups(self) -> list:
        return sorted(self.tmp.glob(".env.bak-*"))


class PathTests(EnvFileTestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(envfile.env_path(self.tmp / "other.env"), self.tmp / "other.env")

    def test_env_file_variable_is_used(self):
        self.assertEqual(envfile.env_path(), self.env)

    def test_default_is_dot_env(self):
        os.environ.pop("ENV_FILE", None)
        self.assertEqual(envfile.env_path().name, ".env")


class ReadTests(EnvFileTestCase):
    def test_read_keys_lists_every_set_variable(self):
        keys = envfile.read_keys()
        self.assertIn("TASKS", keys)
        self.assertIn("COOKIES_12345678905", keys)
        self.assertIn("WEBUI_PASSWORD_HASH", keys, "读的是「文件里有哪些键」，用于展示与排障")

    def test_read_keys_of_missing_file(self):
        self.assertEqual(envfile.read_keys(self.tmp / "nope.env"), [])

    def test_read_tasks(self):
        tasks = envfile.read_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["unique_id"], UID)

    def test_read_tasks_rejects_broken_json(self):
        self.env.write_text("TASKS=[{oops\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            envfile.read_tasks()

    def test_task_for(self):
        self.assertEqual(envfile.task_for(UID)["username"], "账号1")
        self.assertIsNone(envfile.task_for(UID2))

    def test_cookie_key_and_configured(self):
        self.assertEqual(envfile.cookie_key(UID), "COOKIES_12345678905")
        self.assertTrue(envfile.cookie_configured(UID))
        self.assertFalse(envfile.cookie_configured(UID2))

    def test_cookie_configured_tolerates_missing_file(self):
        missing = self.tmp / "nope.env"
        self.assertFalse(envfile.cookie_configured(UID, path=missing))


class BackupTests(EnvFileTestCase):
    def test_backup_copies_content(self):
        dest = envfile.backup()
        self.assertTrue(Path(dest).exists())
        self.assertEqual(Path(dest).read_text(encoding="utf-8"), SAMPLE_ENV)

    def test_backup_of_missing_file_is_empty_string(self):
        self.assertEqual(envfile.backup(self.tmp / "nope.env"), "")

    def test_same_second_backups_do_not_overwrite(self):
        first = envfile.backup()
        second = envfile.backup()
        self.assertNotEqual(first, second)
        self.assertTrue(Path(first).exists() and Path(second).exists())

    def test_backups_are_pruned(self):
        for _ in range(envfile.BACKUP_KEEP + 4):
            envfile.backup()
            # 让 mtime 有区分度，_prune_backups 按 mtime 排序
            os.utime(self.env, None)
        self.assertLessEqual(len(self.backups()), envfile.BACKUP_KEEP)


class SaveCookieTests(EnvFileTestCase):
    def test_writes_the_key_and_keeps_everything_else(self):
        result = envfile.save_cookie(UID, LOGIN_COOKIES)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["key"], "COOKIES_12345678905")
        self.assertEqual(result["names"], ["sessionid", "ttwid"])
        self.assertTrue(result["backup"])
        text = self.text()
        self.assertIn("# 续火花配置（这份注释必须活下来）", text)
        self.assertIn("WEBUI_PASSWORD_HASH=pbkdf2_sha256$1$aa$bb", text)
        self.assertIn("MESSAGE_TEMPLATE=第一行\\n第二行", text)
        self.assertEqual(json.loads(self.value_of("COOKIES_12345678905")), json.loads(LOGIN_COOKIES))
        self.assertNotIn("old", self.value_of("COOKIES_12345678905"))

    def test_the_written_value_survives_the_unicode_escape_round_trip(self):
        # 复刻 utils/config.py 的读取：unicode_escape 解码后再 json.loads
        envfile.save_cookie(UID, LOGIN_COOKIES)
        raw = self.value_of("COOKIES_12345678905")
        decoded = json.loads(raw.encode("utf-8").decode("unicode_escape"))
        self.assertEqual(decoded[0]["name"], "sessionid")
        self.assertEqual(decoded[0]["value"], "new-secret")

    def test_non_ascii_values_are_escaped(self):
        payload = json.dumps([{"name": "sessionid", "value": "中文值", "domain": ".douyin.com"}])
        self.assertTrue(envfile.save_cookie(UID, payload)["ok"])
        self.assertNotIn("中文值", self.text())

    def test_refuses_without_a_login_cookie(self):
        payload = json.dumps([{"name": "ttwid", "value": "t", "domain": ".douyin.com"}])
        result = envfile.save_cookie(UID, payload)
        self.assertFalse(result["ok"])
        self.assertIn("sessionid", result["error"])
        self.assertEqual(
            self.value_of("COOKIES_12345678905"),
            '[{"name":"sessionid","value":"old","domain":".douyin.com"}]',
            "拒绝要发生在写之前：原来那份 Cookie 必须一个字都没动",
        )
        self.assertEqual(self.backups(), [], "没写成就不该留下备份")

    def test_refuses_an_expired_login_cookie(self):
        """名字对、`expires` 已经过去：存下去只会让面板显示"已配置"，发送时才 EXPIRED。

        掉登录之后手工粘一份旧 cookie 就是这个场景（2026-10-11 现场）。
        """
        payload = json.dumps([{"name": "sessionid", "value": "dead",
                               "domain": ".douyin.com", "expires": 1_700_000_000}])
        result = envfile.save_cookie(UID, payload)
        self.assertFalse(result["ok"])
        self.assertIn("过期", result["error"])
        self.assertEqual(
            self.value_of("COOKIES_12345678905"),
            '[{"name":"sessionid","value":"old","domain":".douyin.com"}]',
            "拒绝要发生在写之前",
        )

    def test_future_expiry_and_session_cookies_are_fine(self):
        far = int(time.time()) + 86400
        for extra in ({"expires": far}, {"expires": -1}, {}):
            with self.subTest(extra=extra):
                payload = json.dumps([{"name": "sessionid", "value": "live",
                                       "domain": ".douyin.com", **extra}])
                self.assertTrue(envfile.save_cookie(UID, payload)["ok"], extra)

    def test_expect_login_false_allows_it(self):
        payload = json.dumps([{"name": "ttwid", "value": "t", "domain": ".douyin.com"}])
        self.assertTrue(envfile.save_cookie(UID, payload, expect_login=False)["ok"])

    def test_refuses_broken_json_and_bad_ids(self):
        self.assertIn("JSON", envfile.save_cookie(UID, "sessionid=1")["error"])
        self.assertIn("JSON", envfile.save_cookie(UID, '{"name":"sessionid"}')["error"])
        self.assertFalse(envfile.save_cookie("", LOGIN_COOKIES)["ok"])
        self.assertIn("不合法", envfile.save_cookie("有中文", LOGIN_COOKIES)["error"])

    def test_missing_file_is_reported(self):
        self.assertIn("找不到", envfile.save_cookie(UID, LOGIN_COOKIES, path=self.tmp / "nope.env")["error"])


class UpsertAccountTests(EnvFileTestCase):
    def test_updates_an_existing_account_in_place(self):
        result = envfile.upsert_account(UID, username="新名字")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["added"])
        self.assertTrue(result["changed"])
        tasks = json.loads(self.value_of("TASKS"))
        self.assertEqual(len(tasks), 1, "就地更新，不该多出一条")
        self.assertEqual(tasks[0]["username"], "新名字")
        self.assertEqual(tasks[0]["targets"], ["好友A"], "没给 targets 就不该动它")
        self.assertEqual(tasks[0]["fingerprint"], 12345, "已有指纹必须留着")

    def test_adds_a_new_account_with_a_fingerprint(self):
        result = envfile.upsert_account(UID2, username="账号2", targets=["好友B", "好友C"])
        self.assertTrue(result["added"])
        tasks = json.loads(self.value_of("TASKS"))
        self.assertEqual(len(tasks), 2)
        entry = [t for t in tasks if t["unique_id"] == UID2][0]
        self.assertEqual(entry["targets"], ["好友B", "好友C"])
        self.assertTrue(envfile.FINGERPRINT_MIN <= int(entry["fingerprint"]) <= envfile.FINGERPRINT_MAX)

    def test_add_if_missing_false(self):
        result = envfile.upsert_account(UID2, add_if_missing=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["added"])
        self.assertEqual(len(json.loads(self.value_of("TASKS"))), 1)

    def test_new_account_uses_the_fingerprint_we_gave(self):
        """新账号：调用方给的种子必须真的写进去（以前这个参数是死的）。"""
        envfile.upsert_account(UID2, username="账号2", fingerprint="43210")
        entry = [t for t in json.loads(self.value_of("TASKS")) if t["unique_id"] == UID2][0]
        self.assertEqual(entry["fingerprint"], 43210)

    def test_new_account_falls_back_to_random_for_junk(self):
        for junk in ("abc", "", "999999999", "12.5", None):
            with self.subTest(junk=junk):
                self.setUp()
                envfile.upsert_account(UID2, username="账号2", fingerprint=junk)
                entry = [t for t in json.loads(self.value_of("TASKS")) if t["unique_id"] == UID2][0]
                self.assertTrue(
                    envfile.FINGERPRINT_MIN <= int(entry["fingerprint"]) <= envfile.FINGERPRINT_MAX,
                    f"{junk!r} 读不出来就该随机补一个，而不是写进文件",
                )

    def test_existing_fingerprint_is_never_swapped(self):
        """已有指纹的账号：顺手传一个别的值也不许把"设备"换掉。"""
        result = envfile.upsert_account(UID, username="账号1", fingerprint="88888")
        self.assertTrue(result["ok"], result)
        self.assertEqual(str(result["fingerprint"]), "12345")
        self.assertEqual(str(result["fingerprint_kept"]), "12345")
        self.assertIn("指纹保持 12345", result["message"])
        self.assertEqual(str(json.loads(self.value_of("TASKS"))[0]["fingerprint"]), "12345")

    def test_same_fingerprint_is_not_reported_as_kept(self):
        result = envfile.upsert_account(UID, username="别的名字", fingerprint="12345")
        self.assertNotIn("fingerprint_kept", result)
        self.assertNotIn("指纹保持", result["message"])
        self.assertEqual(str(json.loads(self.value_of("TASKS"))[0]["fingerprint"]), "12345")

    def test_no_change_means_no_write_and_no_backup(self):
        result = envfile.upsert_account(UID, username="账号1", targets=["好友A"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.backups(), [], "没改动就不该产生备份")

    def test_broken_tasks_is_reported_not_overwritten(self):
        self.env.write_text("TASKS=[{oops\n", encoding="utf-8")
        result = envfile.upsert_account(UID, username="x")
        self.assertFalse(result["ok"])
        self.assertIn("configTool", result["error"])
        self.assertEqual(self.text(), "TASKS=[{oops\n")

    def test_bad_id_and_missing_file(self):
        self.assertFalse(envfile.upsert_account("")["ok"])
        self.assertIn("找不到", envfile.upsert_account(UID, path=self.tmp / "nope.env")["error"])


class TargetsTests(EnvFileTestCase):
    def test_set_targets_cleans_and_dedupes(self):
        result = envfile.set_targets(UID, [" 好友A ", "好友B", "好友A", ""])
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(self.value_of("TASKS"))[0]["targets"], ["好友A", "好友B"])

    def test_set_targets_for_unknown_account(self):
        result = envfile.set_targets(UID2, ["好友B"])
        self.assertFalse(result["ok"])
        self.assertIn("扫码登录", result["error"])

    # ------------------------------------------------ 勾选来的目标带身份（uid/conv_id）

    def test_identity_targets_are_written_as_objects(self):
        picked = [
            {"name": "验收好友甲", "uid": "2001", "conv_id": "0:1:2"},
            {"name": "验收好友乙", "uid": "2002"},
        ]
        result = envfile.set_targets(UID, picked)
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(self.value_of("TASKS"))[0]["targets"], picked)

    def test_name_only_dict_becomes_a_plain_string(self):
        """老格式/手填：只有名字就写字符串 —— 别把 .env 变成一坨对象。"""
        result = envfile.set_targets(UID, [{"name": "好友A"}, "好友B"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(self.value_of("TASKS"))[0]["targets"], ["好友A", "好友B"])

    def test_identity_dedupes_by_conv_then_uid_then_name(self):
        picked = [
            {"name": "甲", "conv_id": "0:1:9"},
            {"name": "甲的另一个名字", "conv_id": "0:1:9"},     # 同 conv → 丢
            {"name": "乙", "uid": "77"},
            {"name": "乙（重复）", "uid": "77"},                 # 同 uid → 丢
            "丙",
            "丙",                                              # 同名 → 丢
        ]
        self.assertTrue(envfile.set_targets(UID, picked)["ok"])
        targets = json.loads(self.value_of("TASKS"))[0]["targets"]
        self.assertEqual(targets, [{"name": "甲", "conv_id": "0:1:9"},
                                   {"name": "乙", "uid": "77"}, "丙"])

    def test_same_name_with_identity_replaces_the_plain_one(self):
        """同一个人：手写过的名字 + 勾选来的身份版本 → 只留带身份的那条。"""
        targets = envfile.clean_targets([
            "验收好友甲",
            {"name": "验收好友甲", "uid": "2001", "conv_id": "0:1:2"},
        ])
        self.assertEqual(targets, [{"name": "验收好友甲", "uid": "2001", "conv_id": "0:1:2"}])

    def test_two_people_with_the_same_name_both_kept(self):
        """重名但 conv_id 不同 = 两个人，不能因为同名就吃掉一个。"""
        targets = envfile.clean_targets([
            {"name": "小明", "conv_id": "0:1:1"},
            {"name": "小明", "conv_id": "0:1:2"},
        ])
        self.assertEqual(len(targets), 2)

    def test_clean_targets_drops_junk(self):
        self.assertEqual(envfile.clean_targets([None, "", "   ", {}, {"name": "  "},
                                                {"uid": ""}, 7]), ["7"])
        self.assertEqual(envfile.clean_targets(None), [])

    def test_clean_targets_round_trips_through_env(self):
        picked = [{"name": "甲", "uid": "1", "conv_id": "0:1:2"}, "乙"]
        envfile.set_targets(UID, picked)
        again = envfile.read_tasks()[0]["targets"]
        self.assertEqual(envfile.clean_targets(again), picked)


class AccountMessageTests(EnvFileTestCase):
    """账号专属发送内容（`TASKS[].message`）：面板上"每个账号发不同的话"就靠它。

    三条不能破的规矩：
      * 只动 `TASKS` 里那一个账号的 `message` 键，别的键、注释、指纹都不许动；
      * 写空 = 恢复用全局模板 → **把键删掉**，而不是留一个空串；
      * 超长拒绝写盘（不静默截断），因为 `TASKS` 是 `.env` 里的一整行。
    """

    def message_of(self, uid: str = UID):
        for task in json.loads(self.value_of("TASKS")):
            if str(task.get("unique_id")) == uid:
                return task.get("message")
        return None

    def test_clean_message_normalizes_newlines_only(self):
        self.assertEqual(envfile.clean_message("  甲  "), "甲")
        self.assertEqual(envfile.clean_message("甲\r\n乙"), "甲\n乙")
        self.assertEqual(envfile.clean_message("甲\r乙"), "甲\n乙")
        # 字面 \n（反斜杠+n）是作者的本意，不能在这里被换成真换行 ——
        # 下游 core.douyin_im.line_list 两种都认，换掉只会让 .env 里出现真换行。
        self.assertEqual(envfile.clean_message("甲\\n乙"), "甲\\n乙")
        self.assertEqual(envfile.clean_message(None), "")

    def test_set_account_message_writes_the_key(self):
        result = envfile.set_account_message(UID, "只发给这个账号的一句话")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["message_set"], "只发给这个账号的一句话")
        self.assertEqual(self.message_of(), "只发给这个账号的一句话")
        # 别的什么都没动
        self.assertIn("# 续火花配置（这份注释必须活下来）", self.text())
        self.assertEqual(self.value_of("MESSAGE_TEMPLATE"), "第一行\\n第二行")
        self.assertEqual(json.loads(self.value_of("TASKS"))[0]["fingerprint"], 12345)
        self.assertEqual(json.loads(self.value_of("TASKS"))[0]["targets"], ["好友A"])

    def test_multiline_survives_the_env_round_trip(self):
        """真换行必须能存进 `.env` 的一行里，且读回来还是两行。"""
        envfile.set_account_message(UID, "第一行\n第二行")
        self.assertEqual(self.message_of(), "第一行\n第二行")
        self.assertEqual(len(self.value_of("TASKS").splitlines()), 1,
                         "TASKS 必须仍然是一行 —— 真换行得被 json 转义掉")
        self.assertIn("\\n", self.value_of("TASKS"))

    def test_empty_message_removes_the_key(self):
        envfile.set_account_message(UID, "先配一条")
        self.assertEqual(self.message_of(), "先配一条")
        result = envfile.set_account_message(UID, "   ")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["message_set"], "")
        self.assertNotIn("message", json.loads(self.value_of("TASKS"))[0])

    def test_upsert_with_message_none_keeps_the_old_value(self):
        """扫码登录那条路径会调 `upsert_account`，它**不能**顺手把内容清掉。"""
        envfile.set_account_message(UID, "别弄丢我")
        envfile.upsert_account(UID, username="账号1", fingerprint="12345")
        self.assertEqual(self.message_of(), "别弄丢我")

    def test_upsert_with_empty_message_also_clears_it(self):
        envfile.set_account_message(UID, "待清空")
        envfile.upsert_account(UID, message="")
        self.assertNotIn("message", json.loads(self.value_of("TASKS"))[0])

    def test_too_long_is_refused_and_the_file_is_untouched(self):
        before = self.text()
        result = envfile.set_account_message(UID, "长" * (envfile.MESSAGE_MAX_CHARS + 1))
        self.assertFalse(result["ok"])
        self.assertIn("太长", result["error"])
        self.assertIn(str(envfile.MESSAGE_MAX_CHARS), result["error"])
        self.assertEqual(self.text(), before, "被拒绝时一个字节都不该写")

    def test_exactly_at_the_limit_is_accepted(self):
        text = "长" * envfile.MESSAGE_MAX_CHARS
        self.assertTrue(envfile.set_account_message(UID, text)["ok"])
        self.assertEqual(self.message_of(), text)

    def test_unknown_account_is_refused(self):
        result = envfile.set_account_message(UID2, "给不存在的账号")
        self.assertFalse(result["ok"])
        self.assertIn("扫码登录", result["error"])

    def test_blank_uid_is_refused(self):
        self.assertFalse(envfile.set_account_message("  ", "x")["ok"])

    def test_it_makes_a_backup_before_writing(self):
        envfile.set_account_message(UID, "改之前先备份")
        self.assertTrue(self.backups(), "写 .env 之前必须先留一份备份")


class AccountTimeTests(EnvFileTestCase):
    """账号专属发送时间（`TASKS[].time`）：面板上"每个账号几点发"就靠它。

    和 `message` 同构，规矩也一样：
      * 只动那一个账号的 `time` 键；
      * 写空 = 跟全局 `TASK_SCHEDULE_TIMES` → **把键删掉**，不留空串；
      * 非法时间拒绝写盘（不静默丢弃），因为静默丢弃 = 用户以为设上了、其实还在用默认点。
    """

    def time_of(self, uid: str = UID):
        for task in json.loads(self.value_of("TASKS")):
            if str(task.get("unique_id")) == uid:
                return task.get("time")
        return None

    def test_clean_time_normalizes_and_rejects(self):
        self.assertEqual(envfile.clean_time("08:00"), "08:00")
        self.assertEqual(envfile.clean_time("8:00"), "08:00")
        self.assertEqual(envfile.clean_time(" 20:30 "), "20:30")
        for bad in (None, "", "   ", "8点", "25:00", "08:60", "800", "abc"):
            with self.subTest(bad=bad):
                self.assertEqual(envfile.clean_time(bad), "")

    def test_set_account_time_writes_the_key(self):
        result = envfile.set_account_time(UID, "08:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["time_set"], "08:00")
        self.assertEqual(self.time_of(), "08:00")
        # 别的什么都没动
        self.assertIn("# 续火花配置（这份注释必须活下来）", self.text())
        self.assertEqual(self.value_of("MESSAGE_TEMPLATE"), "第一行\\n第二行")
        task = json.loads(self.value_of("TASKS"))[0]
        self.assertEqual(task["fingerprint"], 12345)
        self.assertEqual(task["targets"], ["好友A"])

    def test_what_gets_stored_is_what_the_panel_shows(self):
        """`8:00` 存进去必须是 `08:00`。

        存原样的话，面板的 `<input type="time">` 拿到 `8:00` 会渲染成空白 ——
        用户看到的是"我设的时间没了"，而去翻 .env 才看得到它其实在。
        """
        self.assertTrue(envfile.set_account_time(UID, "8:00")["ok"])
        self.assertEqual(self.time_of(), "08:00")

    def test_empty_time_removes_the_key(self):
        envfile.set_account_time(UID, "08:00")
        self.assertEqual(self.time_of(), "08:00")
        result = envfile.set_account_time(UID, "   ")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["time_set"], "")
        self.assertNotIn("time", json.loads(self.value_of("TASKS"))[0])

    def test_invalid_time_is_refused_and_the_file_is_untouched(self):
        before = self.text()
        result = envfile.set_account_time(UID, "8点")
        self.assertFalse(result["ok"])
        self.assertIn("HH:MM", result["error"])
        self.assertEqual(self.text(), before, "被拒绝时一个字节都不该写")

    def test_upsert_with_send_time_none_keeps_the_old_value(self):
        """扫码登录那条路径会调 `upsert_account`，它**不能**顺手把时间清掉。"""
        envfile.set_account_time(UID, "08:00")
        envfile.upsert_account(UID, username="账号1", fingerprint="12345")
        self.assertEqual(self.time_of(), "08:00")

    def test_upsert_with_empty_send_time_also_clears_it(self):
        envfile.set_account_time(UID, "08:00")
        envfile.upsert_account(UID, send_time="")
        self.assertNotIn("time", json.loads(self.value_of("TASKS"))[0])

    def test_message_and_time_survive_each_other(self):
        """两个键走同一个 `upsert_account` —— 谁都不许把对方挤掉。"""
        envfile.set_account_message(UID, "只发给这个账号的话")
        envfile.set_account_time(UID, "08:00")
        envfile.set_account_message(UID, "改了内容")
        self.assertEqual(self.time_of(), "08:00", "改内容把时间弄丢了")
        envfile.set_account_time(UID, "09:30")
        self.assertEqual(
            [task.get("message") for task in json.loads(self.value_of("TASKS"))], ["改了内容"],
            "改时间把内容弄丢了",
        )
        self.assertEqual(self.time_of(), "09:30")

    def test_unknown_account_is_refused(self):
        result = envfile.set_account_time(UID2, "08:00")
        self.assertFalse(result["ok"])
        self.assertIn("扫码登录", result["error"])

    def test_blank_uid_is_refused(self):
        self.assertFalse(envfile.set_account_time("  ", "08:00")["ok"])

    def test_the_task_line_stays_one_line(self):
        envfile.set_account_message(UID, "第一行\n第二行")
        envfile.set_account_time(UID, "08:00")
        self.assertEqual(len(self.value_of("TASKS").splitlines()), 1)

    def test_it_makes_a_backup_before_writing(self):
        envfile.set_account_time(UID, "08:00")
        self.assertTrue(self.backups(), "写 .env 之前必须先留一份备份")


class RemoveAccountTests(EnvFileTestCase):
    def test_removes_account_and_cookie(self):
        result = envfile.remove_account(UID)
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(self.value_of("TASKS")), [])
        self.assertIsNone(self.value_of("COOKIES_12345678905"))

    def test_keep_cookie(self):
        self.assertTrue(envfile.remove_account(UID, keep_cookie=True)["ok"])
        self.assertIsNotNone(self.value_of("COOKIES_12345678905"))

    def test_unknown_account(self):
        self.assertFalse(envfile.remove_account(UID2)["ok"])


class ReloadTests(EnvFileTestCase):
    def test_reload_calls_utils_config(self):
        from unittest import mock

        with mock.patch("utils.config.reload_env", return_value=True) as reload_env:
            self.assertTrue(envfile.reload())
        reload_env.assert_called_once()
        self.assertEqual(Path(reload_env.call_args.args[0]), self.env)

    def test_reload_swallows_failures(self):
        from unittest import mock

        with mock.patch("utils.config.reload_env", side_effect=RuntimeError("boom")):
            self.assertFalse(envfile.reload())


class EnvTmpLeftoverTests(EnvFileTestCase):
    """python-dotenv 的写盘会在目录里留 `<env>.tmp_*`，而那个文件里是**整份 .env**。

    正常路径下它只活几毫秒；中途被杀（OOM / 断电 / 容器被停）就会留下一个权限可能
    是 0644、内容却包含全部 `COOKIES_*` 与 `WEBUI_PASSWORD_HASH` 的文件。所以面板
    每次写盘前后都扫一遍目录：够老的残留先 chmod 0600 再删，刚出现的绝不碰。
    """

    def _leftover(self, name: str, age_seconds: float, mode: int = 0o644) -> Path:
        path = self.tmp / name
        path.write_text(SAMPLE_ENV, encoding="utf-8")
        os.chmod(path, mode)
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_sweep_deletes_stale_and_keeps_fresh(self):
        old = self._leftover(".env.tmp_ab12cd", envfile.ENV_TMP_STALE_SECONDS + 60)
        also_old = self._leftover(".env.tmp", envfile.ENV_TMP_STALE_SECONDS + 1)
        fresh = self._leftover(".env.tmp_fresh0", 1.0)

        removed = envfile._sweep_env_tmp(self.env)

        self.assertIn(str(old), removed)
        self.assertIn(str(also_old), removed)
        self.assertFalse(old.exists(), "够老的残留必须删掉")
        self.assertFalse(also_old.exists())
        self.assertTrue(fresh.exists(), "刚出现的中间态是正在写的进程的，删了会打断它")
        self.assertTrue(self.env.exists(), "只清中间态，不动 .env 本身")

    def test_permissions_are_tightened_before_unlink(self):
        from unittest import mock

        # 被测的生产代码 `_lock_down()` 开头就是 `if os.name == "nt": return`
        # （Windows 的 chmod 只有只读位），那边根本没有"收紧成 0600"这个行为可断言；
        # 硬跑会得到 "438 != 384"（0o666 vs 0o600，issue #3 机制二）。
        if os.name != "posix":
            self.skipTest("非 POSIX 不校验权限位（Windows 的 chmod 只有只读位）")
        path = self._leftover(".env.tmp_lockme", envfile.ENV_TMP_STALE_SECONDS + 60, mode=0o644)
        seen = {}
        real_unlink = os.unlink

        def spy(target, *args, **kwargs):
            try:
                seen[str(target)] = os.stat(target).st_mode & 0o777
            except OSError:
                pass
            return real_unlink(target, *args, **kwargs)

        with mock.patch("os.unlink", spy):
            envfile._sweep_env_tmp(self.env)

        self.assertEqual(seen.get(str(path)), 0o600, "删之前必须先收紧成 0600（删失败也不能是 0644）")

    def test_a_normal_save_sweeps_stale_leftovers(self):
        old = self._leftover(".env.tmp_older1", envfile.ENV_TMP_STALE_SECONDS + 60)

        result = envfile.set_account_time(UID, "08:00")

        self.assertTrue(result["ok"], result)
        self.assertFalse(old.exists(), "每次写盘前后都要顺手清一遍残留")
        self.assertIn("TASKS=", self.text())

    def test_sweep_also_removes_dotenvs_real_temp_name(self):
        """★ python-dotenv 的真中间态叫 `.tmp_xxxxxxxx`（不是 `.env.tmp_*`）。

        `rewrite()` = `NamedTemporaryFile(prefix=".tmp_", dir=<.env 目录>)`：
        只认 `<env名>.tmp*` 的话，一个真残留都清不掉（它含全部 COOKIES）。
        """
        real = self._leftover(".tmp_ab12cd34", envfile.ENV_TMP_STALE_SECONDS + 60)
        fresh = self._leftover(".tmp_fresh012", 1.0)

        removed = envfile._sweep_env_tmp(self.env)

        self.assertIn(str(real), removed, "真中间态（.tmp_xxxxxxxx）必须清掉")
        self.assertFalse(real.exists())
        self.assertTrue(fresh.exists(), "刚出现的还是别人的中间态，不能删")

    def test_sweep_leaves_unrelated_dot_tmp_files_alone(self):
        """`.tmp_*` 前缀太泛：内容不像 .env 的（二进制 / 别的工具留的）一律不碰。"""
        binary = self.tmp / ".tmp_binary01"
        binary.write_bytes(b"\x00\x01\x02\x03" * 100)
        stamp = time.time() - (envfile.ENV_TMP_STALE_SECONDS + 60)
        os.utime(binary, (stamp, stamp))
        text_no_env = self.tmp / ".tmp_notes01"
        text_no_env.write_text("随手记的一行字\n", encoding="utf-8")
        os.utime(text_no_env, (stamp, stamp))
        folder = self.tmp / ".tmp_dir0001"
        folder.mkdir()

        removed = envfile._sweep_env_tmp(self.env)

        self.assertEqual(removed, [], f"不该删无关文件，实际删了 {removed}")
        self.assertTrue(binary.exists())
        self.assertTrue(text_no_env.exists())
        self.assertTrue(folder.exists())


class EnvSymlinkTests(EnvFileTestCase):
    """`.env` 被用户做成软链时（指向 /etc/... 或用 stash 管理），保存不能把它换成普通文件。

    默认的 python-dotenv 会把软链替换掉：真正被读取的那份配置没改（"改了不生效"），
    软链指向的文件被悄悄留在原地。所以要么带 `follow_symlinks` 改真身，要么明确拒绝。
    """

    def _symlinked_env(self):
        real = self.tmp / "real.env"
        real.write_text(SAMPLE_ENV, encoding="utf-8")
        link = self.tmp / "link.env"
        link.symlink_to(real)
        os.environ["ENV_FILE"] = str(link)
        return link, real

    def test_symlink_survives_a_save(self):
        link, real = self._symlinked_env()
        supports = envfile._dotenv_supports_follow_symlinks()
        problem = envfile.symlink_write_problem(link)

        result = envfile.set_account_time(UID, "08:00")

        if supports:
            self.assertEqual(problem, "", "支持 follow_symlinks 时不该报问题")
            self.assertTrue(result["ok"], result)
            self.assertTrue(link.is_symlink(), "软链被换成普通文件了 —— 用户看到的配置将不再生效")
            self.assertEqual(link.resolve(), real.resolve())
            self.assertIn("08:00", real.read_text(encoding="utf-8"), "改的必须是软链指向的真身")
        else:
            self.assertTrue(problem, "本机 python-dotenv 不支持 follow_symlinks 时必须给出中文原因")
            self.assertIn("软链", problem)
            self.assertFalse(result["ok"], "宁可不写，也不能静默换掉用户的软链")
            self.assertIn("软链", result["error"])
            self.assertTrue(link.is_symlink())
            self.assertEqual(real.read_text(encoding="utf-8"), SAMPLE_ENV, "真身一个字节都不该动")

    def test_plain_file_is_unaffected(self):
        self.assertEqual(envfile.symlink_write_problem(self.env), "")
        self.assertTrue(envfile.set_account_time(UID, "09:30")["ok"])


class EnvWriteLockTests(EnvFileTestCase):
    """跨进程写 .env 的排它锁。

    背景（2026-10-22 全量检查）：P2 之后**定时任务进程**也会写 `.env`（cookie 回写），
    与 web 面板、扫码回调是两个进程。只在进程内加锁时，两边会「读全文件 → 改一处 →
    整份写回」交错，把对方刚写的那一行盖掉 —— 丢的是 TASKS / COOKIES。
    """

    def _lock_path(self) -> Path:
        return Path(os.path.realpath(str(self.env)) + envfile.ENV_LOCK_SUFFIX)

    @unittest.skipIf(envfile.fcntl is None, "Windows 没有 fcntl，退化为进程内串行")
    def test_写盘会在_env_旁边留一把_600_的锁文件(self):
        self.assertTrue(envfile.set_account_time(UID, "08:00")["ok"])
        lock = self._lock_path()
        self.assertTrue(lock.exists(), "跨进程写锁应该落在 <env>.lock")
        self.assertEqual(lock.stat().st_size, 0, "锁文件里不该有内容（它只是根桩）")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(envfile.fcntl is None, "Windows 没有 fcntl")
    def test_另一个进程拿着锁时这边要排队(self):
        script = (
            "import fcntl, os, time\n"
            f"fd = os.open({str(self._lock_path())!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('READY', flush=True)\n"
            "time.sleep(1.2)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual((child.stdout.readline() or "").strip(), "READY")
            started = time.monotonic()
            result = envfile.set_account_time(UID, "09:30")
            waited = time.monotonic() - started
        finally:
            child.wait(timeout=10)
            if child.stdout:
                child.stdout.close()
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(waited, 0.5, "另一个进程拿着锁时应该等它放锁再写")
        self.assertLess(waited, 8.0, "排队不该排到超时：锁一定要在收尾时放掉")
        self.assertIn("09:30", self.text())

    @unittest.skipIf(envfile.fcntl is None, "Windows 没有 fcntl")
    def test_嵌套的写入口不会把自己锁死(self):
        # update_settings 内部会调 backup()，两者都带 @_serialized —— flock 不可重入，
        # 这里要钉住「同一线程按深度重入、只真锁一次」。
        result = envfile.update_settings({"WEBUI_PORT": "8788"})
        self.assertTrue(result["ok"], result)
        self.assertIn("WEBUI_PORT=8788", self.text())


if __name__ == "__main__":
    unittest.main()
