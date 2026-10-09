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
        first = schedule.jitter_minutes("10050yi", "2026-10-07", 15)
        for _ in range(50):
            self.assertEqual(schedule.jitter_minutes("10050yi", "2026-10-07", 15), first)

    def test_due_at_survives_being_recomputed_every_tick(self):
        """门铃每 5 分钟响一次、每次都要重新算 —— 算出来的 `due_at` 必须一模一样。

        这条如果挂了，症状是"设定 08:00，到 23:00 还没发"：
        每次重算都把 due_at 往后推一点，于是永远 `when < due_at`。
        """
        user = {"unique_id": "10050yi", "username": "wheat", "time": "08:00"}
        start = _at("2026-10-07 00:00:00")
        base = schedule.plan_for(user, when=start, fallback="20:30", jitter=15)["due_at"]
        for step in range(0, 24 * 60, 5):
            when = start + timedelta(minutes=step)
            plan = schedule.plan_for(user, when=when, fallback="20:30", jitter=15)
            self.assertEqual(plan["due_at"], base, f"第 {step} 分钟重算后 due_at 变了")

    def test_offset_is_within_span(self):
        for uid in ("10050yi", "76998876121", "28619454585"):
            for day in ("2026-10-07", "2026-10-08", "2026-11-01"):
                offset = schedule.jitter_minutes(uid, day, 15)
                self.assertGreaterEqual(offset, 0)
                self.assertLessEqual(offset, 15)

    def test_zero_span_disables_jitter(self):
        self.assertEqual(schedule.jitter_minutes("10050yi", "2026-10-07", 0), 0)

    def test_different_accounts_do_not_all_land_on_the_same_minute(self):
        """按账号算抖动的全部意义：岔开。

        如果所有人都在同一个偏移量发，那比不抖动还像机器人 ——
        这正是不能用 systemd `RandomizedDelaySec`（整个 unit 抖一次）的原因。
        """
        offsets = {
            schedule.jitter_minutes(uid, "2026-10-07", 15)
            for uid in ("10050yi", "76998876121", "28619454585", "a", "b", "c")
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
        plan = schedule.plan_for({"unique_id": "10050yi", "time": "08:00"},
                                 when=when, fallback="20:30", jitter=15)
        self.assertEqual(plan["due_at"].hour, 8)
        self.assertEqual(plan["due_at"].minute, plan["offset_minutes"])
        self.assertEqual(plan["due_text"], plan["due_at"].strftime("%H:%M"))
        self.assertEqual(plan["due_at"].date(), when.date())

    def test_username_defaults_to_the_unique_id(self):
        plan = schedule.plan_for({"unique_id": "10050yi"}, when=_at(), fallback="20:30", jitter=0)
        self.assertEqual(plan["username"], "10050yi")


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


class _StateCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="dsf-schedule-")
        self.path = os.path.join(self.dir, "schedule_state.json")
        self.addCleanup(shutil.rmtree, self.dir, True)


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
        schedule.mark_sent(state, "10050yi", when=_at(), plan={"time": "08:00", "due_at": _at()},
                           run_id="20261007-080000", status=schedule.STATUS_OK,
                           source="timer", note="发了 3 个好友")
        self.assertTrue(schedule.save_state(state, self.path))
        again = schedule.load_state(self.path)
        record = again["accounts"]["10050yi"]
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
        self.assertEqual(schedule.state_path("/tmp/x.json"), "/tmp/x.json")
        old = os.environ.get("SCHEDULE_STATE_FILE")
        os.environ["SCHEDULE_STATE_FILE"] = "/tmp/from-env.json"
        try:
            self.assertEqual(schedule.state_path(), "/tmp/from-env.json")
        finally:
            if old is None:
                os.environ.pop("SCHEDULE_STATE_FILE", None)
            else:
                os.environ["SCHEDULE_STATE_FILE"] = old
        self.assertEqual(schedule.state_path(), schedule.DEFAULT_STATE_FILE)


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


class TextTests(unittest.TestCase):
    def test_window_text_says_the_exact_minute_not_a_range(self):
        """抖动的落点算得出来，面板上能算出来的东西就不该让用户猜。"""
        plan = schedule.plan_for({"unique_id": "10050yi", "time": "08:00"},
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
        schedule.mark_sent(state, "10050yi", when=_at(), status=schedule.STATUS_OK)
        schedule.save_state(state, self.path)
        with open(self.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("\n", text.strip())
        self.assertEqual(json.loads(text)["accounts"]["10050yi"]["date"], "2026-10-07")
        # 中文备注不该被转义成 \uXXXX
        self.assertNotIn("\\u", text)


if __name__ == "__main__":
    unittest.main()
