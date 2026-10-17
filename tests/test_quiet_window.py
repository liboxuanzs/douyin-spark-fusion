"""禁跑时间窗（`core/quiet_window.py`）的离线单测。

这个窗口的用途是**两个程序别抢同一个抖音账号**：2026-10-01 之前 `本机` 上还跑着
另一套部署（`douyin-spark.service`，每天 20:30 起、抖动 0~20 分钟真的在发私信），
我们这套融合版同时开 Chromium 登同一个账号会互踢掉线、也更容易被风控盯上。

那套服务**已于 2026-10-01 停用禁用**，融合版成了唯一的发送方，所以默认值改成了
`off`（见 `test_default_window_is_off_now_that_we_are_the_only_sender`）；
窗口本身仍然要能用、且**必须可关**（紧急情况下不能被自己锁死）。
"""

import json
import unittest
from datetime import datetime, time

from core import quiet_window as qw


class ParseTests(unittest.TestCase):
    def test_parses_normal_window(self):
        self.assertEqual(qw.parse("20:20-21:00"), (time(20, 20), time(21, 0)))

    def test_parses_compact_form(self):
        """有人会写成 2020-2100（.env 里很常见的手滑），也认。"""
        self.assertEqual(qw.parse("2020-2100"), (time(20, 20), time(21, 0)))

    def test_parses_fullwidth_colon(self):
        """中文输入法下打出来的全角冒号不该让窗口静默失效。"""
        self.assertEqual(qw.parse("20：20-21：00"), (time(20, 20), time(21, 0)))

    def test_off_values_disable_window(self):
        for spec in ("", "off", "OFF", "none", "no", "0", "false", "disable", "disabled", "  off  "):
            with self.subTest(spec=spec):
                self.assertIsNone(qw.parse(spec))

    def test_invalid_specs_return_none(self):
        for spec in ("20:20", "-", "20:20-", "-21:00", "25:00-26:00", "20:70-21:00", "abc-def"):
            with self.subTest(spec=spec):
                self.assertIsNone(qw.parse(spec))

    def test_cross_midnight_window_is_valid(self):
        self.assertEqual(qw.parse("23:50-00:30"), (time(23, 50), time(0, 30)))


class InWindowTests(unittest.TestCase):
    window = (time(20, 20), time(21, 0))

    def test_inside(self):
        self.assertTrue(qw.in_window(datetime(2026, 10, 1, 20, 30), self.window))

    def test_start_is_inclusive_end_is_exclusive(self):
        """边界写清楚：20:20 算窗内（那一轮会被拦），21:00 已经放行。"""
        self.assertTrue(qw.in_window(datetime(2026, 10, 1, 20, 20), self.window))
        self.assertFalse(qw.in_window(datetime(2026, 10, 1, 21, 0), self.window))

    def test_outside(self):
        for hhmm in ((12, 30), (20, 19), (21, 1), (0, 0)):
            with self.subTest(hhmm=hhmm):
                self.assertFalse(qw.in_window(datetime(2026, 10, 1, *hhmm), self.window))

    def test_cross_midnight(self):
        late = (time(23, 50), time(0, 30))
        self.assertTrue(qw.in_window(datetime(2026, 10, 1, 23, 55), late))
        self.assertTrue(qw.in_window(datetime(2026, 10, 2, 0, 10), late))
        self.assertFalse(qw.in_window(datetime(2026, 10, 2, 12, 0), late))

    def test_zero_length_window_never_blocks(self):
        """起止相同 = 不限制；否则一个手滑的 "20:20-20:20" 会把整天锁死。"""
        same = (time(20, 20), time(20, 20))
        self.assertFalse(qw.in_window(datetime(2026, 10, 1, 20, 20), same))


class UntilTests(unittest.TestCase):
    def test_same_day(self):
        end = qw.until(datetime(2026, 10, 1, 20, 30), (time(20, 20), time(21, 0)))
        self.assertEqual(end, datetime(2026, 10, 1, 21, 0))

    def test_next_day_when_past_end(self):
        end = qw.until(datetime(2026, 10, 1, 23, 0), (time(23, 50), time(0, 30)))
        self.assertEqual(end, datetime(2026, 10, 2, 0, 30))


class CheckTests(unittest.TestCase):
    def test_default_window_is_off_now_that_we_are_the_only_sender(self):
        """旧部署退役后默认必须是 off。

        留着 20:20-21:00 会**静默**盖掉 20:30 那一轮定时任务：到点整轮跳过，
        账本里只有一行 `quiet_window`，面板上看不出任何异常。
        """
        self.assertEqual(qw.DEFAULT_WINDOW, "off")
        self.assertFalse(qw.check(datetime(2026, 10, 1, 20, 30), env={})["active"])
        self.assertFalse(qw.check(datetime(2026, 10, 1, 12, 30), env={})["active"])

    def test_default_window_is_still_customisable(self):
        """窗口还能用：想留（比如夜里不发）就写 HH:MM-HH:MM，边界照旧。"""
        window = qw.parse("20:20-21:00")
        self.assertIsNotNone(window)
        for minute in range(20, 60):
            with self.subTest(minute=minute):
                self.assertTrue(qw.in_window(datetime(2026, 10, 1, 20, minute), window))
        self.assertFalse(qw.in_window(datetime(2026, 10, 1, 21, 0), window))

    def test_active_inside_window(self):
        env = {qw.ENV_WINDOW: "20:20-21:00"}
        got = qw.check(datetime(2026, 10, 1, 20, 30), env=env)
        self.assertTrue(got["active"])
        self.assertEqual(got["spec"], "20:20-21:00")
        self.assertEqual(got["until"], "2026-10-01T21:00:00")
        self.assertIn("20:20-21:00", got["reason"])
        self.assertIn("21:00", got["reason"])

    def test_inactive_outside_window(self):
        got = qw.check(datetime(2026, 10, 1, 12, 30), env={qw.ENV_WINDOW: "20:20-21:00"})
        self.assertFalse(got["active"])
        self.assertEqual(got["until"], "")
        self.assertEqual(got["reason"], "")

    def test_timer_time_is_not_blocked_by_default(self):
        """我们自己的 timer 定在 20:30（沿用旧部署的时间）—— 默认（off）绝不能拦它。

        这条与 `viewmodel._quiet_clash()` 是一对：真给这一轮设了窗口，面板要报红。
        """
        self.assertFalse(qw.check(datetime(2026, 10, 1, 20, 30), env={})["active"])
        self.assertFalse(qw.check(datetime(2026, 10, 1, 20, 45), env={})["active"])

    def test_override_releases_window(self):
        env = {qw.ENV_WINDOW: "20:20-21:00", qw.ENV_OVERRIDE: "1"}
        got = qw.check(datetime(2026, 10, 1, 20, 30), env=env)
        self.assertFalse(got["active"])
        self.assertTrue(got["override"])

    def test_override_off_values_do_not_release(self):
        for raw in ("", "0", "false", "no", "off"):
            with self.subTest(raw=raw):
                env = {qw.ENV_WINDOW: "20:20-21:00", qw.ENV_OVERRIDE: raw}
                self.assertFalse(qw.override(env))
                self.assertTrue(qw.check(datetime(2026, 10, 1, 20, 30), env=env)["active"])

    def test_window_can_be_disabled_by_env(self):
        got = qw.check(datetime(2026, 10, 1, 20, 30), env={qw.ENV_WINDOW: "off"})
        self.assertFalse(got["active"])
        self.assertIsNone(got["window"])

    def test_custom_window_from_env(self):
        env = {qw.ENV_WINDOW: "07:00-08:00"}
        self.assertTrue(qw.check(datetime(2026, 10, 1, 7, 30), env=env)["active"])
        self.assertFalse(qw.check(datetime(2026, 10, 1, 20, 30), env=env)["active"])

    def test_invalid_spec_fails_open_but_reports_it(self):
        """写法不合法时**放行**（宁可发错时间也不能因为一个手滑的配置整天不发），
        但要把原始 spec 带出来，方便在面板/日志里看出问题。"""
        got = qw.check(datetime(2026, 10, 1, 20, 30), env={qw.ENV_WINDOW: "晚上八点"})
        self.assertFalse(got["active"])
        self.assertEqual(got["spec"], "晚上八点")
        self.assertIsNone(got["window"])

    def test_check_defaults_to_now_without_crashing(self):
        got = qw.check()
        self.assertIn("active", got)
        self.assertIsInstance(got["active"], bool)


class SummaryTests(unittest.TestCase):
    """`summary()` 存在的唯一理由：`check()` 的结果**不能**直接进 JSON。

    `window` 是两个 `datetime.time`，`json.dumps` 会抛
    `TypeError: Object of type time is not JSON serializable` ——
    2026-10-01 真机验证时正是它把整轮任务的收尾炸掉了。
    """

    def test_summary_is_json_safe(self):
        info = qw.check(datetime(2026, 10, 1, 20, 30), env={qw.ENV_WINDOW: "20:20-21:00"})
        with self.assertRaises(TypeError):
            json.dumps(info)                      # 原始结果确实不能直接序列化
        safe = qw.summary(info)
        self.assertEqual(json.loads(json.dumps(safe))["spec"], "20:20-21:00")
        self.assertNotIn("window", safe)

    def test_summary_keeps_the_fields_the_dashboard_needs(self):
        info = qw.check(datetime(2026, 10, 1, 20, 30), env={qw.ENV_WINDOW: "20:20-21:00"})
        safe = qw.summary(info)
        self.assertEqual(sorted(safe), sorted(qw.JSON_KEYS))
        self.assertTrue(safe["active"])
        self.assertEqual(safe["until"], "2026-10-01T21:00:00")
        self.assertIn("20:20-21:00", safe["reason"])

    def test_summary_tolerates_missing_or_empty_input(self):
        self.assertEqual(qw.summary(None), {key: None for key in qw.JSON_KEYS})
        self.assertEqual(qw.summary({})["active"], None)


if __name__ == "__main__":
    unittest.main()
