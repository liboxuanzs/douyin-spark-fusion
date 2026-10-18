"""账号级发送时间表（`core/schedule.py`）的行为契约。

这个模块最贵的两个 bug 都不是"算错时间"，而是**同一天里前后不一致**：

* **抖动每次重算** —— 门铃每 5 分钟响一次，如果每次 `due_at` 都换一个值，
  就会这一分钟判"没到点"、下一分钟还判"没到点"，永远追不上那个一直在往后飘的时刻。
  所以 `jitter_minutes()` 必须是 `sha256(账号|日期|跨度)` 的纯函数。
* **"今天发过了"没盖过"到没到点"** —— 面板的「立即运行」是强制的、并且同样记账；
  判定顺序一旦反过来，手动跑过之后定时那一轮会**再发一遍**，
  用户看到的就是"我明明手动发过了，怎么又发了"。

其余用例围着"读不出来不能挡住发送"展开：状态文件是账本，不是门禁。
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from core import schedule


def _at(text="2026-10-07 08:00:00"):
    """固定时刻（本地时区），避免用例依赖"现在几点"。"""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()


class ParseTimeTests(unittest.TestCase):
    def test_accepts_24h_hh_mm_and_normalizes(self):
        self.assertEqual(schedule.parse_time("08:00"), "08:00")
        self.assertEqual(schedule.parse_time("8:00"), "08:00")
        self.assertEqual(schedule.parse_time(" 20:30 "), "20:30")
        self.assertEqual(schedule.parse_time("00:00"), "00:00")
        self.assertEqual(schedule.parse_time("23:59"), "23:59")

    def test_rejects_anything_it_would_have_to_guess(self):
        """宁可让用户重填一次，也不要自作聪明 —— 发消息给真人的时刻猜错了代价不对称。"""
        for bad in ("", None, "  ", "24:00", "08:60", "0800", "8点", "08:00:00",
                    "20:30:00", "abc", "-1:00", "8:0a"):
            with self.subTest(bad=bad):
                self.assertIsNone(schedule.parse_time(bad), bad)

    def test_minutes_of(self):
        self.assertEqual(schedule.minutes_of("08:00"), 480)
        self.assertEqual(schedule.minutes_of("20:30"), 1230)
        self.assertIsNone(schedule.minutes_of("abc"))


class FallbackTimeTests(unittest.TestCase):
    """.env 的 TASK_SCHEDULE_TIMES 现在是"没单独配时间的账号用几点"。"""

    def test_takes_the_first_valid_entry(self):
        self.assertEqual(schedule.fallback_time("08:00,20:30"), "08:00")

    def test_falls_back_to_the_retired_deployment_time(self):
        # 20:30 = 旧部署 douyin-spark.service 的时间（2026-10-01 用户指定的口径）。
        # 改它等于替所有没写 time 的存量账号改了发送时间。
        self.assertEqual(schedule.DEFAULT_TIME, "20:30")
        for empty in ("", None, "   ", "abc", "25:00", ",,"):
            with self.subTest(empty=empty):
                self.assertEqual(schedule.fallback_time(empty), "20:30")

    def test_skips_garbage_and_uses_the_next_valid_one(self):
        """"08:00,垃圾" 里第一项认识就用它；"垃圾,20:30" 要跳过垃圾而不是整串放弃。"""
        self.assertEqual(schedule.fallback_time("垃圾,20:30"), "20:30")


class JitterDeterminismTests(unittest.TestCase):
    """★ 本文件最重要的一条：同一个账号、同一天，到点时刻必须恒定。"""

    def test_same_account_same_day_never_changes(self):
        first = schedule.jitter_minutes("11223344556", "2026-10-07", 15)
        for _ in range(50):
            self.assertEqual(schedule.jitter_minutes("11223344556", "2026-10-07", 15), first)

    def test_due_at_survives_being_recomputed_every_tick(self):
        """门铃每 5 分钟响一次、每次都要重新算 —— 算出来的 `due_at` 必须一模一样。

        这条如果挂了，症状是"设定 08:00，到 23:00 还没发"：
        每次重算都把 due_at 往后推一点，于是永远 `when < due_at`。
        """
        user = {"unique_id": "11223344556", "username": "示例号", "time": "08:00"}
        start = _at("2026-10-07 00:00:00")
        base = schedule.plan_for(user, when=start, fallback="20:30", jitter=15)["due_at"]
        for step in range(0, 24 * 60, 5):
            when = start + timedelta(minutes=step)
            plan = schedule.plan_for(user, when=when, fallback="20:30", jitter=15)
            self.assertEqual(plan["due_at"], base, f"第 {step} 分钟重算后 due_at 变了")

    def test_offset_is_within_span(self):
        for uid in ("11223344556", "12345678905", "28619454585"):
            for day in ("2026-10-07", "2026-10-08", "2026-11-01"):
                offset = schedule.jitter_minutes(uid, day, 15)
                self.assertGreaterEqual(offset, 0)
                self.assertLessEqual(offset, 15)

    def test_zero_span_disables_jitter(self):
        self.assertEqual(schedule.jitter_minutes("11223344556", "2026-10-07", 0), 0)

    def test_different_accounts_do_not_all_land_on_the_same_minute(self):
        """按账号算抖动的全部意义：岔开。

        如果所有人都在同一个偏移量发，那比不抖动还像机器人 ——
        这正是不能用 systemd `RandomizedDelaySec`（整个 unit 抖一次）的原因。
        """
        offsets = {
            schedule.jitter_minutes(uid, "2026-10-07", 15)
            for uid in ("11223344556", "12345678905", "28619454585", "a", "b", "c")
        }
        self.assertGreater(len(offsets), 1, "6 个账号全落在同一个抖动偏移上")


class PlanForTests(unittest.TestCase):
    def test_account_time_wins_over_the_global_default(self):
        plan = schedule.plan_for(
            {"unique_id": "u", "username": "甲", "time": "08:00"},
            when=_at(), fallback="20:30", jitter=0,
        )
        self.assertEqual(plan["time"], "08:00")
        self.assertTrue(plan["custom"])

    def test_empty_time_follows_the_global_default(self):
        plan = schedule.plan_for({"unique_id": "u", "time": ""}, when=_at(), fallback="20:30", jitter=0)
        self.assertEqual(plan["time"], "20:30")
        self.assertFalse(plan["custom"])

    def test_a_broken_account_time_falls_back_instead_of_exploding(self):
        """TASKS 是手写 JSON，`"time": "8点"` 迟早会出现 —— 不能炸整轮。"""
        for bad in ("8点", "25:00", "   ", None):
            with self.subTest(bad=bad):
                plan = schedule.plan_for(
                    {"unique_id": "u", "time": bad}, when=_at(), fallback="20:30", jitter=0
                )
                self.assertEqual(plan["time"], "20:30")
                self.assertFalse(plan["custom"])

    def test_due_at_is_the_time_plus_the_offset(self):
        when = _at("2026-10-07 00:00:00")
        plan = schedule.plan_for({"unique_id": "11223344556", "time": "08:00"},
                                 when=when, fallback="20:30", jitter=15)
        self.assertEqual(plan["due_at"].hour, 8)
        self.assertEqual(plan["due_at"].minute, plan["offset_minutes"])
        self.assertEqual(plan["due_text"], plan["due_at"].strftime("%H:%M"))
        self.assertEqual(plan["due_at"].date(), when.date())

    def test_username_defaults_to_the_unique_id(self):
        plan = schedule.plan_for({"unique_id": "11223344556"}, when=_at(), fallback="20:30", jitter=0)
        self.assertEqual(plan["username"], "11223344556")


class TickMinutesTests(unittest.TestCase):
    """敲门间隔由抖动反推 —— 刻意不做成配置项，少一个能调错但看不出来的旋钮。"""

    def test_default_jitter_matches_the_shipped_tick_constant(self):
        self.assertEqual(schedule.tick_minutes(schedule.DEFAULT_JITTER_MINUTES),
                         schedule.TICK_MINUTES)

    def test_no_jitter_ticks_every_minute(self):
        self.assertEqual(schedule.tick_minutes(0), schedule.TICK_MIN)

    def test_stays_inside_the_bounds(self):
        self.assertEqual(schedule.tick_minutes(1), schedule.TICK_MIN)
        self.assertEqual(schedule.tick_minutes(1000), schedule.TICK_MAX)
        for jitter in range(0, 61):
            with self.subTest(jitter=jitter):
                value = schedule.tick_minutes(jitter)
                self.assertGreaterEqual(value, schedule.TICK_MIN)
                self.assertLessEqual(value, schedule.TICK_MAX)


class JitterEnvTests(unittest.TestCase):
    def test_bad_values_fall_back_to_the_default(self):
        for bad in ("", "  ", "abc", None):
            with self.subTest(bad=bad):
                self.assertEqual(schedule.jitter_minutes_env(bad),
                                 schedule.DEFAULT_JITTER_MINUTES)

    def test_negative_is_clamped_to_zero(self):
        self.assertEqual(schedule.jitter_minutes_env("-5"), 0)

    def test_accepts_floats_and_spaces(self):
        self.assertEqual(schedule.jitter_minutes_env(" 20 "), 20)
        self.assertEqual(schedule.jitter_minutes_env("20.0"), 20)


class SkipRetryMaxEnvTests(unittest.TestCase):
    """`SCHEDULE_SKIP_RETRY_MAX` 的读取口径（跟 `jitter_minutes_env` 同一套写法）。"""

    def test_missing_values_fall_back_to_the_default(self):
        for missing in ("", "  ", None):
            with self.subTest(raw=missing):
                self.assertEqual(schedule.skip_retry_max_env(missing),
                                 schedule.DEFAULT_SKIP_RETRY_MAX)

    def test_bad_values_fall_back_to_the_default(self):
        # "1e999" 看着像个合法数字，但 int(float(...)) 抛 OverflowError：
        # 漏掉这一类异常 = select() 每 5 分钟炸一次，全部账号一起停发。
        for bad in ("abc", "1e999", "-1e999"):
            with self.subTest(raw=bad):
                self.assertEqual(schedule.skip_retry_max_env(bad),
                                 schedule.DEFAULT_SKIP_RETRY_MAX)

    def test_accepts_floats_and_spaces(self):
        self.assertEqual(schedule.skip_retry_max_env(" 3 "), 3)
        self.assertEqual(schedule.skip_retry_max_env("3.0"), 3)

    def test_negative_is_clamped_to_zero(self):
        self.assertEqual(schedule.skip_retry_max_env("-3"), 0)

    def test_zero_means_no_limit_at_all(self):
        """0 是"别限制"的显式写法，不是"一次都不许试" —— 写反了就是当天火花静默丢失。"""
        record = {"date": "2026-10-07", "status": schedule.STATUS_SKIPPED, "attempts": 999}
        self.assertFalse(schedule.skip_retry_exhausted(record, limit=0, day="2026-10-07"))


class _StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dsf-schedule-")
        self.path = os.path.join(self.dir, "schedule_state.json")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # 钉住"今天"：这些用例的数据全写在 2026-10-07，而 `save_marks()` 会拿
        # **真实现在**去 prune（只留最近 7 天）。不钉住的话，用例跑在几天之后的机器上
        # 就会被当成陈旧数据删掉 —— 失败原因跟被测行为毫无关系，而且成片地失败。
        clock = mock.patch.object(schedule, "now", lambda: _at())
        clock.start()
        self.addCleanup(clock.stop)


class StateFileTests(_StateCase):
    def test_missing_file_is_an_empty_state_not_an_error(self):
        state = schedule.load_state(self.path)
        self.assertEqual(state["accounts"], {})
        self.assertEqual(state["version"], schedule.STATE_VERSION)

    def test_corrupt_file_is_an_empty_state(self):
        """读不出来最坏是"今天多发一次"；抛异常最坏是"一个账号都发不出去"。

        这两者不对称 —— 所以损坏的账本必须被当成"没有账本"。
        """
        for junk in ("{", "[]", "null", "not json at all", '{"accounts": "???"}'):
            with self.subTest(junk=junk):
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(junk)
                state = schedule.load_state(self.path)
                self.assertEqual(state["accounts"], {})

    def test_save_then_load_round_trip(self):
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at(), plan={"time": "08:00", "due_at": _at()},
                           run_id="20261007-080000", status=schedule.STATUS_OK,
                           source="timer", note="发了 3 个好友")
        self.assertTrue(schedule.save_state(state, self.path))
        again = schedule.load_state(self.path)
        record = again["accounts"]["11223344556"]
        self.assertEqual(record["date"], "2026-10-07")
        self.assertEqual(record["time"], "08:00")
        self.assertEqual(record["run_id"], "20261007-080000")
        self.assertEqual(record["status"], schedule.STATUS_OK)
        self.assertEqual(record["source"], "timer")
        self.assertEqual(record["note"], "发了 3 个好友")
        self.assertTrue(again["updated_at"])

    def test_save_is_atomic_enough_to_leave_no_tmp_behind(self):
        state = schedule.load_state(self.path)
        schedule.save_state(state, self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_save_creates_the_directory(self):
        nested = os.path.join(self.dir, "logs", "schedule_state.json")
        self.assertTrue(schedule.save_state({"accounts": {}}, nested))
        self.assertTrue(os.path.exists(nested))

    def test_save_failure_returns_false_instead_of_raising(self):
        """记不上账不该让这一轮发送失败 —— 调用方只记一条 warning。"""
        blocked = os.path.join(self.dir, "not-a-dir")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("我是文件不是目录")
        self.assertFalse(schedule.save_state({"accounts": {}}, os.path.join(blocked, "x.json")))

    def test_notes_are_truncated(self):
        """账本是长期保留的 jsonl，一条异常不该把它撑爆。"""
        state = schedule.load_state(self.path)
        record = schedule.mark_sent(state, "u", when=_at(), note="啊" * 500)
        self.assertEqual(len(record["note"]), 200)

    def test_state_path_honours_the_env_var(self):
        # 绝对路径必须用"本机的绝对路径"来构造：写死的 "/tmp/x.json" 在 Windows 上
        # `ntpath.isabs()` 是 False（那边要盘符），会被当相对路径锚到仓库根 ——
        # 断言白红（issue #3 机制二：本文件 2 条失败之一就是它）。
        # Linux 上 `tempfile.gettempdir()` 就是 /tmp，被测行为一字不变。
        absolute = os.path.join(tempfile.gettempdir(), "x.json")
        self.assertEqual(schedule.state_path(absolute), absolute)
        old = os.environ.get("SCHEDULE_STATE_FILE")
        from_env = os.path.join(tempfile.gettempdir(), "from-env.json")
        os.environ["SCHEDULE_STATE_FILE"] = from_env
        try:
            self.assertEqual(schedule.state_path(), from_env)
        finally:
            if old is None:
                os.environ.pop("SCHEDULE_STATE_FILE", None)
            else:
                os.environ["SCHEDULE_STATE_FILE"] = old
        # 默认值按**仓库根**解析（不是 CWD）：从别的目录起进程时不能另开一份状态。
        self.assertEqual(
            schedule.state_path(),
            os.path.join(schedule.__file__.rsplit(os.sep + "core" + os.sep, 1)[0],
                         schedule.DEFAULT_STATE_FILE),
        )

    def test_state_path_is_anchored_to_the_repo_root(self):
        """★ 换 CWD 不能让状态文件跑到别处（否则"今天发过了"读不到 = 当天重发）。"""
        before = schedule.state_path()
        elsewhere = tempfile.mkdtemp(prefix="dsf-cwd-")
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        old_cwd = os.getcwd()
        try:
            os.chdir(elsewhere)
            self.assertEqual(schedule.state_path(), before)
        finally:
            os.chdir(old_cwd)
        # 相对路径（含 .env 里手写的相对值）同样落到仓库根下，绝对路径原样。
        # 相对那一侧用 `os.path.join` 拼：`.env` 里写的是 "logs/x.json"，但仓库根的
        # 拼接结果在 Windows 上是 "logs\x.json"，拿字面量比会假红（同上，机制二）。
        self.assertEqual(schedule.state_path(os.path.join("logs", "other.json")),
                         os.path.join(os.path.dirname(before), "other.json"))
        absolute = os.path.join(tempfile.gettempdir(), "abs.json")
        self.assertEqual(schedule.state_path(absolute), absolute)


class EntryAndPruneTests(_StateCase):
    def test_entry_for_only_knows_today(self):
        state = {"accounts": {"u": {"date": "2026-10-06", "status": "ok"}}}
        self.assertIsNone(schedule.entry_for(state, "u", _at("2026-10-07 08:00:00")))
        self.assertIsNotNone(schedule.entry_for(state, "u", _at("2026-10-06 08:00:00")))

    def test_entry_for_missing_account_is_none(self):
        self.assertIsNone(schedule.entry_for({"accounts": {}}, "u", _at()))
        self.assertIsNone(schedule.entry_for(None, "u", _at()))

    def test_prune_keeps_today_and_drops_the_ancients(self):
        state = {"accounts": {
            "today": {"date": "2026-10-07"},
            "recent": {"date": "2026-10-05"},
            "ancient": {"date": "2026-01-01"},
        }}
        self.assertEqual(schedule.prune(state, today="2026-10-07", keep_days=7), 1)
        self.assertEqual(set(state["accounts"]), {"today", "recent"})

    def test_prune_survives_a_broken_today(self):
        state = {"accounts": {"u": {"date": "2026-10-07"}}}
        self.assertEqual(schedule.prune(state, today="垃圾"), 0)
        self.assertEqual(set(state["accounts"]), {"u"})


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.when = _at("2026-10-07 08:30:00")
        self.users = [
            {"unique_id": "zao", "username": "早", "time": "08:00"},
            {"unique_id": "wan", "username": "晚", "time": "20:30"},
        ]

    def _select(self, state=None, jitter=0):
        return schedule.select(self.users, when=self.when, state=state or {"accounts": {}},
                               fallback="20:30", jitter=jitter)

    def test_splits_into_due_and_waiting(self):
        result = self._select()
        self.assertEqual([p["unique_id"] for _u, p in result["due"]], ["zao"])
        self.assertEqual([p["unique_id"] for _u, p in result["waiting"]], ["wan"])
        self.assertFalse(result["idle"])

    def test_nothing_due_means_idle(self):
        result = schedule.select(
            [self.users[1]], when=self.when, state={"accounts": {}}, fallback="20:30", jitter=0
        )
        self.assertTrue(result["idle"])
        self.assertEqual(result["due"], [])

    def test_already_sent_beats_not_due_yet(self):
        """★ 手动跑过一轮之后再定时跑，**不能**再发一次。

        面板的「立即运行」是强制的、并且同样记账；判定顺序一旦反过来，
        用户看到的就是"我明明手动发过了，怎么又发了"。
        """
        state = {"accounts": {"zao": {"date": "2026-10-07", "status": "ok"}}}
        result = self._select(state)
        self.assertEqual(result["due"], [])
        self.assertEqual([p["unique_id"] for _u, p, _r in result["sent"]], ["zao"])

    def test_yesterdays_record_does_not_block_today(self):
        """跨天不补发，但也**不能**被昨天的记录挡住今天的发送。"""
        state = {"accounts": {"zao": {"date": "2026-10-06", "status": "ok"}}}
        result = self._select(state)
        self.assertEqual([p["unique_id"] for _u, p in result["due"]], ["zao"])

    def test_past_due_within_the_day_is_still_due(self):
        """用户口径（2026-10-06）："只要还是当天就补发，多晚都补"。"""
        late = _at("2026-10-07 23:50:00")
        result = schedule.select(self.users, when=late, state={"accounts": {}},
                                 fallback="20:30", jitter=0)
        self.assertEqual(sorted(p["unique_id"] for _u, p in result["due"]), ["wan", "zao"])

    def test_plans_carry_every_account(self):
        result = self._select()
        self.assertEqual(set(result["plans"]), {"zao", "wan"})

    def test_empty_user_list_is_idle(self):
        result = schedule.select([], when=self.when, state={"accounts": {}},
                                 fallback="20:30", jitter=0)
        self.assertTrue(result["idle"])
        self.assertEqual(result["plans"], {})

    def test_a_skipped_account_is_still_retried_today(self):
        """skipped = 一条都没发出去，当天名额还在 —— 前几次必须照旧重试。

        这条钉的是既有语义：加了重试上限之后，"还没试够"的账号不能被顺手静音
        （静音的代价是当天火花断掉）。
        """
        state = {"accounts": {"zao": {"date": "2026-10-07", "status": "skipped",
                                      "attempts": 1}}}
        result = self._select(state)
        self.assertEqual([p["unique_id"] for _u, p in result["due"]], ["zao"])
        self.assertEqual(result["sent"], [])

    def test_a_skipped_account_stops_being_retried_after_the_cap(self):
        """★ 试够次数就当天不再挂门铃。

        不改的话：一个过不了门禁的账号每 5 分钟被踹起来开一次浏览器（一天 288 次），
        这本身就在喂风控 —— 而它今天根本不可能发出去。
        """
        with mock.patch.dict(os.environ, {"SCHEDULE_SKIP_RETRY_MAX": "6"}):
            state = {"accounts": {"zao": {"date": "2026-10-07", "status": "skipped",
                                          "attempts": 6,
                                          "last_attempt": "2026-10-07T08:25:00+08:00"}}}
            result = self._select(state)
            self.assertEqual(result["due"], [])
            self.assertEqual([p["unique_id"] for _u, p, _r in result["sent"]], ["zao"])
            # 记录必须留着、状态必须还是 skipped：面板据此显示"今天试过但没发出去"，
            # 而且用户重新扫码（retract_mark 把 attempts 归零）之后当天照样能补发。
            record = state["accounts"]["zao"]
            self.assertEqual(record["status"], "skipped")
            self.assertEqual(record["attempts"], 6)

    def test_the_cap_counts_only_todays_attempts(self):
        """昨天的 99 次不能把账号今天一上来就按死。"""
        state = {"accounts": {"zao": {"date": "2026-10-06", "status": "skipped",
                                      "attempts": 99}}}
        result = self._select(state)
        self.assertEqual([p["unique_id"] for _u, p in result["due"]], ["zao"])

    def test_a_broken_attempts_field_never_mutes_the_account(self):
        """字段写坏时宁可多踹一次，也不能静音 —— 多踹的代价是风控，静音的代价是断火花。"""
        for bad in (None, "", "abc", "1e999", [], {}):
            with self.subTest(attempts=bad):
                state = {"accounts": {"zao": {"date": "2026-10-07", "status": "skipped",
                                              "attempts": bad}}}
                result = self._select(state)
                self.assertEqual([p["unique_id"] for _u, p in result["due"]], ["zao"])

    def test_a_one_shot_limit_of_zero_never_mutes_anyone(self):
        """`SCHEDULE_SKIP_RETRY_MAX=0`（显式关掉上限）不能把当天全静音。"""
        with mock.patch.dict(os.environ, {"SCHEDULE_SKIP_RETRY_MAX": "0"}):
            state = {"accounts": {"zao": {"date": "2026-10-07", "status": "skipped",
                                          "attempts": 9999}}}
            result = self._select(state)
            self.assertEqual([p["unique_id"] for _u, p in result["due"]], ["zao"])

    def test_only_skipped_records_can_be_exhausted(self):
        day = "2026-10-07"
        self.assertFalse(schedule.skip_retry_exhausted(None, day=day))
        self.assertFalse(schedule.skip_retry_exhausted("垃圾", day=day))
        self.assertFalse(schedule.skip_retry_exhausted({"status": "ok", "attempts": 99}, day=day))
        self.assertFalse(schedule.skip_retry_exhausted({"status": "error", "attempts": 99}, day=day))
        self.assertTrue(schedule.skip_retry_exhausted(
            {"status": "skipped", "attempts": 6, "date": day}, day=day, limit=6))
        self.assertFalse(schedule.skip_retry_exhausted(
            {"status": "skipped", "attempts": 5, "date": day}, day=day, limit=6))


class TextTests(unittest.TestCase):
    def test_window_text_says_the_exact_minute_not_a_range(self):
        """抖动的落点算得出来，面板上能算出来的东西就不该让用户猜。"""
        plan = schedule.plan_for({"unique_id": "11223344556", "time": "08:00"},
                                 when=_at("2026-10-07 00:00:00"), fallback="20:30", jitter=15)
        text = schedule.window_text(plan)
        self.assertIn("每天 08:00", text)
        self.assertIn(plan["due_text"], text)
        self.assertIn("±15", text)

    def test_window_text_says_so_when_jitter_is_off(self):
        plan = schedule.plan_for({"unique_id": "u", "time": "08:00"},
                                 when=_at(), fallback="20:30", jitter=0)
        self.assertIn("不抖动", schedule.window_text(plan))

    def test_summary_text_for_a_finished_account(self):
        record = {"status": schedule.STATUS_OK, "sent_at": "2026-10-07T08:11:00+08:00",
                  "source": "timer"}
        self.assertIn("08:11", schedule.summary_text(None, None, record))
        self.assertIn("已发", schedule.summary_text(None, None, record))

    def test_summary_text_for_a_waiting_account(self):
        plan = {"due_text": "08:07"}
        self.assertEqual(schedule.summary_text(None, plan), "等 08:07 到点")

    def test_wait_seconds(self):
        plan = {"due_at": _at("2026-10-07 08:07:00")}
        self.assertEqual(schedule.wait_seconds(plan, _at("2026-10-07 08:00:00")), 420)
        # 已经过点了就是 0，不是负数 —— 面板倒计时不能出现 "-3 分钟"
        self.assertEqual(schedule.wait_seconds(plan, _at("2026-10-07 09:00:00")), 0)
        self.assertEqual(schedule.wait_seconds({}, _at()), 0)


class PersistedStateShapeTests(_StateCase):
    def test_state_file_is_human_readable_json(self):
        """出问题时用户会直接打开它看 —— 不能是紧凑的一行。"""
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at(), status=schedule.STATUS_OK)
        schedule.save_state(state, self.path)
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("\n", text.strip())
        self.assertEqual(json.loads(text)["accounts"]["11223344556"]["date"], "2026-10-07")
        # 中文备注不该被转义成 \uXXXX
        self.assertNotIn("\\u", text)



class ClearMarkTests(_StateCase):
    """`clear_mark` —— 重新登录成功后把"今天办过了"撤掉，让当天还能补发。

    没有它：掉登录那一轮被记成 error（失败也算办过，防的是坏 cookie 一天被门铃
    踹 288 次），人重新扫码登录之后还得手点「立即运行」，否则这一天就没了。
    有了它：登录成功的瞬间撤掉记账，下一个 5 分钟门铃自动补发。
    危险面只有一个 —— **已经成功发过的账号绝不能被撤**（撤了就一天发两遍），
    所以这里同时钉住"别人的记录不许动""别的日期的记录不许动"。
    """

    def test_removes_only_that_account_and_only_today(self):
        state = schedule.load_state(self.path)
        today = _at()
        yesterday = today - timedelta(days=1)
        schedule.mark_sent(state, "11223344556", when=yesterday, status=schedule.STATUS_OK)
        schedule.mark_sent(state, "11223344556", when=today, status=schedule.STATUS_ERROR)
        schedule.mark_sent(state, "12345678905", when=today, status=schedule.STATUS_OK)

        # 昨天的记录还在 → 今天的撤销请求不该被它挡住
        self.assertTrue(schedule.clear_mark(state, "11223344556", when=today))
        self.assertNotIn("11223344556", state["accounts"])
        self.assertIn("12345678905", state["accounts"])

    def test_is_a_noop_when_there_is_no_mark(self):
        state = schedule.load_state(self.path)
        self.assertFalse(schedule.clear_mark(state, "11223344556", when=_at()))
        self.assertEqual(state["accounts"], {})

    def test_does_not_touch_another_days_record(self):
        state = schedule.load_state(self.path)
        yesterday = _at() - timedelta(days=1)
        schedule.mark_sent(state, "11223344556", when=yesterday, status=schedule.STATUS_OK)
        self.assertFalse(schedule.clear_mark(state, "11223344556", when=_at()))
        self.assertIn("11223344556", state["accounts"])

    def test_survives_a_broken_state(self):
        """状态文件里塞了乱七八糟的东西也不能抛 —— 扫码登录不能被它拖垮。"""
        self.assertFalse(schedule.clear_mark({}, "11223344556"))
        self.assertFalse(schedule.clear_mark({"accounts": None}, "11223344556"))
        self.assertFalse(schedule.clear_mark({"accounts": {"11223344556": "坏了"}}, "11223344556"))

    def test_roundtrip_through_the_state_file(self):
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at(), status=schedule.STATUS_ERROR)
        schedule.save_state(state, self.path)
        again = schedule.load_state(self.path)
        self.assertTrue(schedule.clear_mark(again, "11223344556", when=_at()))
        schedule.save_state(again, self.path)
        final = schedule.load_state(self.path)
        self.assertNotIn("11223344556", final["accounts"])
        # 选举逻辑据此重新认为"今天还没发"
        self.assertIsNone(schedule.entry_for(final, "11223344556", _at()))


class MarkSentAttemptsTests(_StateCase):
    """`mark_sent()` 的 `attempts` 计数 —— `select()` 给 skipped 踩刹车就靠它。

    只有 skipped 计数：发成功(ok)之后再留着计数，会让"上一轮试够 6 次的账号"
    在用户手动跑成功之后仍然被当成"今天办过了"之外的东西，口径就乱了。
    """

    def test_skipped_marks_count_up(self):
        state = schedule.load_state(self.path)
        when = _at("2026-10-07 09:00:00")
        for expected in (1, 2, 3):
            record = schedule.mark_sent(state, "11223344556", when=when,
                                        status=schedule.STATUS_SKIPPED, source="timer")
            self.assertEqual(record["attempts"], expected)
            self.assertEqual(record["last_attempt"], schedule.iso(when))

    def test_success_carries_no_attempts(self):
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at(), status=schedule.STATUS_SKIPPED)
        record = schedule.mark_sent(state, "11223344556", when=_at("2026-10-07 09:00:00"),
                                    status=schedule.STATUS_OK)
        # 发过了，"今天还要不要再试"这个问题本身就不存在了 —— 别留个 6 在那儿误导人
        self.assertNotIn("attempts", record)
        self.assertFalse(schedule.skip_retry_exhausted(record, day="2026-10-07"))

    def test_a_new_day_starts_at_one(self):
        """昨天的计数不许滚进今天：滚进来 = 今天一上来就被判"试够了"，静默丢失。"""
        state = {"accounts": {"11223344556": {"date": "2026-10-06", "status": "skipped",
                                          "attempts": 5}}}
        record = schedule.mark_sent(state, "11223344556", when=_at(),
                                    status=schedule.STATUS_SKIPPED)
        self.assertEqual(record["attempts"], 1)

    def test_count_attempt_false_writes_no_counter(self):
        """没开浏览器的那种跳过（比如账号没配目标）不算"踹过风控"。"""
        state = schedule.load_state(self.path)
        record = schedule.mark_sent(state, "11223344556", when=_at(),
                                    status=schedule.STATUS_SKIPPED, count_attempt=False)
        self.assertNotIn("attempts", record)

    def test_the_counter_survives_the_merge_write(self):
        """计数要真的落到磁盘上：进程被杀之后下一个门铃得接着数，不能每次从 1 开始。"""
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at(), status=schedule.STATUS_SKIPPED)
        self.assertTrue(schedule.save_marks(state, ["11223344556"], self.path))
        again = schedule.load_state(self.path)
        self.assertEqual(again["accounts"]["11223344556"]["attempts"], 1)
        self.assertEqual(again["accounts"]["11223344556"]["last_attempt"], schedule.iso(_at()))

    def test_a_broken_counter_is_read_as_zero(self):
        self.assertEqual(schedule._attempts_of(None), 0)
        self.assertEqual(schedule._attempts_of("不是记录"), 0)
        self.assertEqual(schedule._attempts_of({"attempts": "abc"}), 0)
        self.assertEqual(schedule._attempts_of({"attempts": "1e999"}), 0)
        self.assertEqual(schedule._attempts_of({"attempts": -3}), 0)
        self.assertEqual(schedule._attempts_of({"attempts": 2}), 2)
        # 别的日期的计数一律不算
        self.assertEqual(schedule._attempts_of({"date": "2026-10-06", "attempts": 5},
                                               "2026-10-07"), 0)


class MergeSaveTests(_StateCase):
    """`save_marks` —— 只在点名账号上做"读磁盘 → 比时间戳 → 写回"的合并。

    这一组钉的是 2026-10-09/10-10 两次"掉登录后当天静默不发"的**共同根因**：
    sender 在轮次开始时读一份快照，轮次结束才写回；如果它整份覆盖，就会把面板
    在这中间撤掉的记账**复活**（当天再也不发），或者把面板刚写的状态**抹掉**。
    """

    def _disk(self):
        return schedule.load_state(self.path)

    def test_keeps_another_account_written_after_the_snapshot_was_taken(self):
        snapshot = self._disk()                     # sender 在轮次开始时读的快照
        disk = self._disk()
        schedule.mark_sent(disk, "12345678905", when=_at(), status=schedule.STATUS_OK)
        schedule.save_state(disk, self.path)        # 面板/另一个进程中途写了账

        schedule.mark_sent(snapshot, "11223344556", when=_at(), status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))

        final = self._disk()
        self.assertIn("12345678905", final["accounts"])   # 别人的账没被旧快照抹掉
        self.assertEqual(final["accounts"]["11223344556"]["status"], schedule.STATUS_OK)

    def test_disk_wins_for_the_same_account_when_it_is_newer(self):
        """面板在轮次中途撤了账（墓碑时间更晚）→ sender 的旧 error 记录必须认输。"""
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at(), status=schedule.STATUS_ERROR)
        schedule.save_state(disk, self.path)

        snapshot = self._disk()                     # 旧快照：只有 error 记录
        self.assertIn("11223344556", snapshot["accounts"])

        later = self._disk()
        self.assertTrue(schedule.retract_mark(later, "11223344556", when=_at("2026-10-07 09:00:00")))
        self.assertTrue(schedule.save_state(later, self.path))

        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        record = self._disk()["accounts"]["11223344556"]
        self.assertEqual(record["status"], schedule.STATUS_SKIPPED)
        self.assertEqual(record["source"], "relogin")

    def test_snapshot_wins_when_it_is_newer(self):
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at("2026-10-07 08:00:00"),
                           status=schedule.STATUS_ERROR)
        schedule.save_state(disk, self.path)

        snapshot = self._disk()
        schedule.mark_sent(snapshot, "11223344556", when=_at("2026-10-07 08:30:00"),
                           status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        self.assertEqual(self._disk()["accounts"]["11223344556"]["status"], schedule.STATUS_OK)

    def test_only_named_accounts_are_written(self):
        disk = self._disk()
        schedule.mark_sent(disk, "keep", when=_at("2026-10-07 07:00:00"), status=schedule.STATUS_ERROR)
        schedule.save_state(disk, self.path)

        snapshot = self._disk()
        schedule.mark_sent(snapshot, "keep", when=_at("2026-10-07 08:00:00"), status=schedule.STATUS_OK)
        schedule.mark_sent(snapshot, "mine", when=_at(), status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["mine"], self.path))

        final = self._disk()
        self.assertEqual(final["accounts"]["keep"]["status"], schedule.STATUS_ERROR)  # 未点名 → 磁盘为准
        self.assertEqual(final["accounts"]["mine"]["status"], schedule.STATUS_OK)

    def test_a_named_account_missing_from_the_snapshot_is_not_deleted(self):
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at(), status=schedule.STATUS_OK)
        schedule.save_state(disk, self.path)
        self.assertTrue(schedule.save_marks(self._disk(), ["never-seen"], self.path))
        self.assertIn("11223344556", self._disk()["accounts"])

    def test_overdue_records_are_pruned_but_todays_survive(self):
        disk = self._disk()
        disk["accounts"]["ancient"] = {"date": "2026-01-01", "status": schedule.STATUS_OK}
        schedule.mark_sent(disk, "11223344556", when=_at(), status=schedule.STATUS_OK)
        schedule.save_state(disk, self.path)

        snapshot = self._disk()
        schedule.mark_sent(snapshot, "11223344556", when=_at(), status=schedule.STATUS_ERROR)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))

        final = self._disk()["accounts"]
        self.assertNotIn("ancient", final)
        self.assertIn("11223344556", final)

    def test_prune_can_be_switched_off(self):
        disk = self._disk()
        disk["accounts"]["ancient"] = {"date": "2026-01-01"}
        schedule.save_state(disk, self.path)
        schedule.save_marks(self._disk(), [], self.path, prune_stale=False)
        self.assertIn("ancient", self._disk()["accounts"])

    def test_junk_input_never_raises(self):
        for state in ({}, None, {"accounts": None}, {"accounts": {"x": "坏了"}}):
            with self.subTest(state=state):
                self.assertTrue(schedule.save_marks(state, ["x", "", None], self.path))

    def test_leaves_no_tmp_file_behind(self):
        snapshot = self._disk()
        schedule.mark_sent(snapshot, "11223344556", when=_at(), status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_two_temporary_names_can_never_collide(self):
        """固定名 `target + '.tmp'` 会让两个进程互相截断 —— 现在是唯一名。"""
        names = set()
        for _ in range(5):
            state = self._disk()
            schedule.mark_sent(state, "11223344556", when=_at(), status=schedule.STATUS_OK)
            real_replace = os.replace
            seen = []

            def spy(src, dst, _seen=seen, _real=real_replace):
                _seen.append(src)
                return _real(src, dst)

            os.replace = spy
            try:
                self.assertTrue(schedule.save_state(state, self.path))
            finally:
                os.replace = real_replace
            names.add(seen[0])
        self.assertEqual(len(names), 5)              # 每次都换了新临时名

    def test_concurrent_merges_lose_no_update(self):
        """4 个线程各自记一个账号：flock 合并之后 4 条都要在（丢一条 = 多发一天）。"""
        import threading

        uids = ["a", "b", "c", "d"]
        errors = []

        def worker(uid):
            try:
                state = schedule.load_state(self.path)
                schedule.mark_sent(state, uid, when=_at(), status=schedule.STATUS_OK)
                if not schedule.save_marks(state, [uid], self.path):
                    errors.append(f"{uid}: save failed")
            except Exception as exc:                  # noqa: BLE001 - 线程里必须自己兜住
                errors.append(f"{uid}: {exc!r}")

        threads = [threading.Thread(target=worker, args=(uid,)) for uid in uids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        final = self._disk()["accounts"]
        for uid in uids:
            self.assertIn(uid, final)


    def test_a_real_success_beats_a_relogin_tombstone(self):
        """★ 墓碑输给"真发出去了"，哪怕它的时间戳更新。

        场景（2026-10-13 的真实数据）：面板在 09:00 写了撤账墓碑，sender 那一轮
        08:00 就开始了、用的也是 08:00 的**本轮开始时刻**记账，收尾合并时按纯时间戳
        比 → 已经发出去的 ok 被墓碑改回 skipped → 下一个门铃又发一遍（一天两条）。
        """
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at("2026-10-07 08:00:00"),
                           status=schedule.STATUS_ERROR, note="login_lost")
        self.assertTrue(schedule.save_state(disk, self.path))

        snapshot = self._disk()          # sender 在轮次开始时读到的快照（墓碑之前）
        panel = self._disk()
        self.assertTrue(schedule.retract_mark(panel, "11223344556", when=_at("2026-10-07 09:00:00")))
        self.assertTrue(schedule.save_state(panel, self.path))

        schedule.mark_sent(snapshot, "11223344556", when=_at("2026-10-07 08:00:00"),
                           run_id="20261007-080000", status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))

        record = self._disk()["accounts"]["11223344556"]
        self.assertEqual(record["status"], schedule.STATUS_OK)          # ok 赢，不是 skipped
        self.assertNotEqual(record["source"], "relogin")                # 墓碑被彻底盖掉
        self.assertEqual(record["run_id"], "20261007-080000")

        picked = schedule.select(
            [{"unique_id": "11223344556", "username": "示例号", "time": "08:00"}],
            when=_at("2026-10-07 09:30:00"), state=self._disk(), fallback="20:30", jitter=0,
        )
        self.assertEqual(picked["due"], [])                             # 门铃不会再发第二条
        self.assertEqual([p["unique_id"] for _u, p, _r in picked["sent"]], ["11223344556"])

    def test_a_failed_round_never_revives_over_a_relogin_tombstone(self):
        """★ 反过来同样要钉死：新规则只认"真成功"，error/skipped 一律没有特权。

        否则一个失败的轮次就能把面板的撤账盖掉 —— 当天名额再次被占住，
        用户重新扫码之后这一天还是发不出去（就是这次要修的那个静默丢失）。
        """
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at("2026-10-07 08:00:00"),
                           status=schedule.STATUS_ERROR, note="login_lost")
        self.assertTrue(schedule.retract_mark(disk, "11223344556", when=_at("2026-10-07 09:00:00")))
        self.assertTrue(schedule.save_state(disk, self.path))

        for status in (schedule.STATUS_ERROR, schedule.STATUS_SKIPPED):
            with self.subTest(status=status):
                snapshot = {"accounts": {"11223344556": {}}}
                schedule.mark_sent(snapshot, "11223344556", when=_at("2026-10-07 08:00:00"),
                                   status=status, note="本轮又没成")
                self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))

                record = self._disk()["accounts"]["11223344556"]
                self.assertEqual(record["status"], schedule.STATUS_SKIPPED)
                self.assertEqual(record["source"], "relogin")           # 还是那条墓碑
                self.assertIn("补发", record["note"])

                picked = schedule.select(
                    [{"unique_id": "11223344556", "username": "示例号", "time": "08:00"}],
                    when=_at("2026-10-07 09:30:00"), state=self._disk(),
                    fallback="20:30", jitter=0,
                )
                self.assertEqual([p["unique_id"] for _u, p in picked["due"]], ["11223344556"])

    def test_a_newer_failed_attempt_still_wins_over_the_tombstone(self):
        """墓碑不是"永久免死金牌"：比它更新的失败记录照旧盖过它。

        这条是**刻意**的：skipped 的 `attempts` 计数要能一轮一轮涨上去，
        否则重试上限永远到不了 6，"别每 5 分钟踹一次"就成了一句空话。
        """
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at("2026-10-07 08:00:00"),
                           status=schedule.STATUS_ERROR)
        self.assertTrue(schedule.retract_mark(disk, "11223344556", when=_at("2026-10-07 09:00:00")))
        self.assertTrue(schedule.save_state(disk, self.path))

        snapshot = {"accounts": {"11223344556": {}}}
        schedule.mark_sent(snapshot, "11223344556", when=_at("2026-10-07 09:30:00"),
                           status=schedule.STATUS_SKIPPED, note="login_lost")
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))

        record = self._disk()["accounts"]["11223344556"]
        self.assertEqual(record["status"], schedule.STATUS_SKIPPED)
        self.assertEqual(record["note"], "login_lost")
        self.assertEqual(record["attempts"], 1)      # 计数确实涨上去了

    def test_without_a_tombstone_timestamps_still_win(self):
        """没有墓碑时合并规则一个字没变：谁的时间戳新谁说了算。

        这条防止"真成功优先"被写成"ok 无条件优先" —— 那会让一个过期的 ok
        盖掉更新的失败记录，把当天的补发名额锁死。
        """
        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at("2026-10-07 09:00:00"),
                           status=schedule.STATUS_ERROR, note="磁盘较新")
        self.assertTrue(schedule.save_state(disk, self.path))
        snapshot = {"accounts": {"11223344556": {}}}
        schedule.mark_sent(snapshot, "11223344556", when=_at("2026-10-07 08:00:00"),
                           status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        self.assertEqual(self._disk()["accounts"]["11223344556"]["note"], "磁盘较新")

        disk = self._disk()
        schedule.mark_sent(disk, "11223344556", when=_at("2026-10-07 10:00:00"),
                           status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_state(disk, self.path))
        snapshot = {"accounts": {"11223344556": {}}}
        schedule.mark_sent(snapshot, "11223344556", when=_at("2026-10-07 09:00:00"),
                           status=schedule.STATUS_ERROR, note="快照较旧")
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        self.assertEqual(self._disk()["accounts"]["11223344556"]["status"], schedule.STATUS_OK)


class RetractMarkTests(_StateCase):
    """`retract_mark` —— 重新登录成功后写一条**墓碑**，而不是删掉那条记录。

    删掉就"没有时间戳可比"了：sender 手里攥着几分钟前的旧快照，收尾合并时会把
    error 记录原样写回，当天名额再次丢掉。墓碑让合并判得出"面板后来撤过"。
    """

    def test_tombstone_keeps_the_slot_open_for_today(self):
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at("2026-10-07 08:00:00"),
                           run_id="20261007-080000", status=schedule.STATUS_ERROR,
                           note="login_lost")
        self.assertTrue(schedule.retract_mark(state, "11223344556", when=_at("2026-10-07 09:00:00")))

        record = state["accounts"]["11223344556"]
        self.assertEqual(record["status"], schedule.STATUS_SKIPPED)
        self.assertEqual(record["source"], "relogin")
        self.assertEqual(record["sent_at"], schedule.iso(_at("2026-10-07 09:00:00")))
        self.assertEqual(record["run_id"], "20261007-080000")   # 保留原轮次号便于追溯
        self.assertIn("补发", record["note"])

        # 门铃据此认为"今天还没发出去" → 当天还能补
        picked = schedule.select(
            [{"unique_id": "11223344556", "username": "示例号", "time": "08:00"}],
            when=_at("2026-10-07 09:30:00"), state=state, fallback="20:30", jitter=0,
        )
        self.assertEqual([p[0]["unique_id"] for p in picked["due"]], ["11223344556"])
        self.assertEqual(picked["sent"], [])

    def test_only_that_account_and_only_today(self):
        state = schedule.load_state(self.path)
        today = _at()
        yesterday = today - timedelta(days=1)
        schedule.mark_sent(state, "11223344556", when=yesterday, status=schedule.STATUS_OK)
        schedule.mark_sent(state, "12345678905", when=today, status=schedule.STATUS_OK)

        self.assertFalse(schedule.retract_mark(state, "11223344556", when=today))   # 只有昨天的记录
        self.assertTrue(schedule.retract_mark(state, "12345678905", when=today))
        self.assertIn("11223344556", state["accounts"])
        self.assertEqual(state["accounts"]["11223344556"]["status"], schedule.STATUS_OK)
        self.assertEqual(state["accounts"]["12345678905"]["status"], schedule.STATUS_SKIPPED)

    def test_is_a_noop_without_a_mark_or_with_a_broken_state(self):
        self.assertFalse(schedule.retract_mark(schedule.load_state(self.path), "11223344556"))
        self.assertFalse(schedule.retract_mark({}, "11223344556"))
        self.assertFalse(schedule.retract_mark({"accounts": None}, "11223344556"))
        self.assertFalse(schedule.retract_mark({"accounts": {"11223344556": "坏了"}}, "11223344556"))

    def test_second_retract_moves_the_timestamp_forward(self):
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at("2026-10-07 08:00:00"),
                           status=schedule.STATUS_ERROR)
        self.assertTrue(schedule.retract_mark(state, "11223344556", when=_at("2026-10-07 09:00:00")))
        self.assertTrue(schedule.retract_mark(state, "11223344556", when=_at("2026-10-07 10:00:00")))
        self.assertEqual(state["accounts"]["11223344556"]["sent_at"],
                         schedule.iso(_at("2026-10-07 10:00:00")))

    def test_tombstone_survives_an_old_snapshot_merge(self):
        """端到端：面板撤账 → sender 用旧快照收尾合并 → 墓碑仍在、当天仍会补发。"""
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at("2026-10-07 08:00:00"),
                           run_id="20261007-080000", status=schedule.STATUS_ERROR)
        snapshot = json.loads(json.dumps(state))    # 另一进程攥着的旧快照
        self.assertTrue(schedule.save_state(state, self.path))

        panel = schedule.load_state(self.path)
        self.assertTrue(schedule.retract_mark(panel, "11223344556", when=_at("2026-10-07 09:00:00")))
        self.assertTrue(schedule.save_state(panel, self.path))

        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        final = schedule.load_state(self.path)
        self.assertEqual(final["accounts"]["11223344556"]["status"], schedule.STATUS_SKIPPED)
        picked = schedule.select(
            [{"unique_id": "11223344556", "username": "示例号", "time": "08:00"}],
            when=_at("2026-10-07 09:30:00"), state=final, fallback="20:30", jitter=0,
        )
        self.assertEqual([p[0]["unique_id"] for p in picked["due"]], ["11223344556"])

    def test_retract_mark_resets_the_attempts_so_today_can_still_send(self):
        """★ 回归：重扫之后当天必须还能补发，哪怕今天已经试满 6 次。

        这是"掉登录不要每 5 分钟踹一次"必须付出的代价 —— 一旦踩了刹车，
        就得有一条确定的路径把刹车松开。用户重新扫码成功 = `retract_mark`，
        它把 attempts 归零；不归零的话 `select()` 下一秒又判"今天试够了"，
        用户扫完码看着面板一切正常，火花却在当天悄悄断掉。
        """
        state = schedule.load_state(self.path)
        when = _at("2026-10-07 08:00:00")
        for _ in range(schedule.DEFAULT_SKIP_RETRY_MAX):
            schedule.mark_sent(state, "11223344556", when=when, status=schedule.STATUS_SKIPPED,
                               source="timer", note="login_lost")
        self.assertTrue(schedule.save_state(state, self.path))

        users = [{"unique_id": "11223344556", "username": "示例号", "time": "08:00"}]
        before = schedule.select(users, when=_at("2026-10-07 09:00:00"),
                                 state=schedule.load_state(self.path),
                                 fallback="20:30", jitter=0)
        self.assertEqual(before["due"], [])          # 踩住刹车：当天不再挂门铃

        rescan = schedule.load_state(self.path)
        self.assertEqual(rescan["accounts"]["11223344556"]["attempts"], 6)
        self.assertTrue(schedule.retract_mark(rescan, "11223344556", when=_at("2026-10-07 09:05:00")))
        self.assertEqual(rescan["accounts"]["11223344556"]["attempts"], 0)
        self.assertTrue(schedule.save_marks(rescan, ["11223344556"], self.path))

        after = schedule.select(users, when=_at("2026-10-07 09:10:00"),
                                state=schedule.load_state(self.path),
                                fallback="20:30", jitter=0)
        self.assertEqual([p["unique_id"] for _u, p in after["due"]], ["11223344556"])  # 刹车松开


class RequeueAfterReloginTests(_StateCase):
    """`requeue_after_relogin` —— 两条登录路径（面板扫码 + configTool）共用的同一份判定。

    issue #12：这份判定原来是面板里的**私有实现**，configTool 那条正规登录路径走不到它，
    于是同一份新 cookie 从两条路进来结果不同 —— 面板会当天补发、configTool 不会，
    而用户在两边看到的都是"登录成功了"。判定只有一份，才不会再次分叉。
    """

    UID = "11223344556"

    def setUp(self):
        super().setUp()
        self.saved_env = {
            key: os.environ.get(key)
            for key in ("SCHEDULE_STATE_FILE", "RUN_LEDGER_FILE")
        }
        os.environ["SCHEDULE_STATE_FILE"] = self.path
        self.ledger = os.path.join(self.dir, "runs.jsonl")
        os.environ["RUN_LEDGER_FILE"] = self.ledger

        def restore():
            for key, value in self.saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

    def _write_ledger(self, rows):
        lines = [
            row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)
            for row in rows
        ]
        with open(self.ledger, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + ("\n" if lines else ""))

    @staticmethod
    def _run(sent_ok, unique_id="11223344556"):
        return {
            "run_id": "20261007-080000",
            "started_at": schedule.iso(_at("2026-10-07 08:00:00")),
            "dry_run": "off",
            "accounts": [{
                "unique_id": unique_id,
                "username": "示例号",
                "status": "error" if sent_ok == 0 else "ok",
                "gate": "READY",
                "sent_ok": sent_ok,
                "sent_fail": 0,
            }],
        }

    def _mark_today(self, status):
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, self.UID, when=_at(), status=status, note="test")
        schedule.save_state(state, self.path)

    def _mark_of_today(self):
        state = schedule.load_state(self.path)
        return schedule.entry_for(state, self.UID, _at())

    def test_error_mark_today_is_retracted_and_saved(self):
        """今天跑过、但一条都没发出去 → 写成 `skipped` 墓碑并落盘（当天还能补发）。"""
        self._mark_today(schedule.STATUS_ERROR)
        self._write_ledger([])
        out = schedule.requeue_after_relogin(self.UID)
        self.assertTrue(out["requeued"], out)
        self.assertTrue(out["saved"], out)
        self.assertEqual(out["reason"], "")
        record = self._mark_of_today()
        self.assertEqual(record["status"], schedule.STATUS_SKIPPED)
        self.assertEqual(record["source"], "relogin")
        self.assertEqual(int(record.get("attempts") or 0), 0, "重试计数必须归零，否则当天不再补")

    def test_already_sent_today_keeps_the_mark(self):
        """今天发出去过（账本里 sent_ok>0）→ 绝不撤账：撤了朋友会收到两遍。"""
        self._mark_today(schedule.STATUS_ERROR)
        self._write_ledger([self._run(sent_ok=2)])
        out = schedule.requeue_after_relogin(self.UID)
        self.assertFalse(out["requeued"], out)
        self.assertEqual(out["reason"], "already-sent")
        self.assertEqual(self._mark_of_today()["status"], schedule.STATUS_ERROR)

    def test_ok_mark_blocks_requeue_without_a_ledger_row(self):
        """状态里今天已经是 `ok` → 账本读不到也不能补发（账本和状态是两次写）。"""
        self._mark_today(schedule.STATUS_OK)
        self._write_ledger([])
        out = schedule.requeue_after_relogin(self.UID)
        self.assertFalse(out["requeued"], out)
        self.assertEqual(out["reason"], "already-sent")

    def test_no_run_today_is_left_alone(self):
        """今天根本没轮到它 → 没有记账要撤，什么都不做（门铃本来就会发它）。"""
        self._write_ledger([])
        out = schedule.requeue_after_relogin(self.UID)
        self.assertFalse(out["requeued"], out)
        self.assertEqual(out["reason"], "no-run-today")
        self.assertIsNone(self._mark_of_today())

    def test_another_accounts_run_does_not_requeue(self):
        self._write_ledger([self._run(sent_ok=0, unique_id="98765432101")])
        out = schedule.requeue_after_relogin(self.UID)
        self.assertFalse(out["requeued"], out)
        self.assertEqual(out["reason"], "no-run-today")

    def test_empty_uid_is_a_noop(self):
        out = schedule.requeue_after_relogin("")
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "no-uid")


class StateLockTests(_StateCase):
    """锁坏了也不能把记账带崩 —— `save_state` 的"不抛"契约在锁这一层同样成立。

    原来 `os.makedirs` / `open(lock_path, "a+")` 是裸的：目录不可写、锁路径被
    别的东西占着（比如上一次崩溃留下的同名目录），OSError 会一路冒到
    `core/tasks.py` 的收尾 `finally`，把这一轮的账本行和记账一起丢掉 ——
    那一刻正好是"发成功了但没记上"的组合，下一个门铃就重发一遍。
    """

    def _disk(self):
        return schedule.load_state(self.path)

    def _block_the_lock(self):
        os.mkdir(self.path + ".lock")        # 同名目录 → open(..., "a+") 抛 OSError

    def test_a_broken_lock_path_does_not_raise(self):
        self._block_the_lock()
        with schedule._state_lock(self.path) as locked:
            self.assertFalse(locked)         # 拿不到锁，但调用方照常往下走

    def test_save_state_still_writes_without_the_lock(self):
        self._block_the_lock()
        state = schedule.load_state(self.path)
        schedule.mark_sent(state, "11223344556", when=_at(), status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_state(state, self.path))
        self.assertEqual(self._disk()["accounts"]["11223344556"]["status"], schedule.STATUS_OK)

    def test_save_marks_still_writes_without_the_lock(self):
        self._block_the_lock()
        snapshot = {"accounts": {"11223344556": {}}}
        schedule.mark_sent(snapshot, "11223344556", when=_at(), status=schedule.STATUS_OK)
        self.assertTrue(schedule.save_marks(snapshot, ["11223344556"], self.path))
        self.assertEqual(self._disk()["accounts"]["11223344556"]["status"], schedule.STATUS_OK)


if __name__ == "__main__":
    unittest.main()
