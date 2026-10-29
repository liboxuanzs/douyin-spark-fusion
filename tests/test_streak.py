# -*- coding: utf-8 -*-
"""tests/test_streak.py —— 火花连续天数（issue #15）的回归测试。

四个口径各有一组：dry_run 不能续天数、今天还没发不算断、窗口要有边界、
断签要给出"上次成功几天前"。
"""

import unittest
from datetime import date, datetime, timedelta, timezone

from core import streak

CST = timezone(timedelta(hours=8))
TODAY = datetime(2026, 10, 24, 12, 0, 0, tzinfo=CST)


def _run(day, *, uid="111", conv="conv-a", label="🌟", ok=True, dry_run="off", entry_dry_run=None):
    at = datetime.combine(day, datetime.min.time(), tzinfo=CST) + timedelta(hours=20)
    entry = {"target": label, "conv_id": conv, "ok": ok, "level": "strong",
             "via": "http", "at": at.isoformat()}
    if entry_dry_run is not None:
        entry["dry_run"] = entry_dry_run
    return {
        "run_id": "r-" + day.isoformat(),
        "started_at": at.isoformat(),
        "finished_at": (at + timedelta(minutes=5)).isoformat(),
        "dry_run": dry_run,
        "accounts": [{"unique_id": uid, "entries": [entry]}],
    }


def _days_ago(count):
    return TODAY.date() - timedelta(days=count)


class CurrentStreakTests(unittest.TestCase):
    def test_five_consecutive_days_including_today(self):
        rows = [_run(_days_ago(offset)) for offset in range(5)]
        result = streak.compute(rows, today=TODAY)
        item = result["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 5)
        self.assertEqual(item["longest"], 5)
        self.assertFalse(item["broken"])

    def test_today_not_sent_yet_does_not_break_the_streak(self):
        # 昨天的发送是最新的一条；今天还没到点
        rows = [_run(_days_ago(offset)) for offset in range(1, 4)]
        result = streak.compute(rows, today=TODAY)
        item = result["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 3)
        self.assertFalse(item["broken"])
        self.assertEqual(item["last_text"], "10-23")

    def test_two_days_without_a_send_is_a_broken_streak(self):
        rows = [_run(_days_ago(offset)) for offset in (2, 3, 4, 5)]
        result = streak.compute(rows, today=TODAY)
        item = result["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 0)
        self.assertTrue(item["broken"])
        self.assertEqual(item["idle_days"], 2)
        self.assertEqual(item["longest"], 4)
        self.assertIn("断签 2 天", streak.streak_text(item))

    def test_missing_yesterday_with_today_sent_restarts_at_one(self):
        rows = [_run(_days_ago(0)), _run(_days_ago(3))]
        result = streak.compute(rows, today=TODAY)
        item = result["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 1)
        self.assertEqual(item["longest"], 1)
        self.assertFalse(item["broken"])

    def test_longest_survives_a_break(self):
        rows = [_run(_days_ago(offset)) for offset in (0, 1, 5, 6, 7, 8)]
        item = streak.compute(rows, today=TODAY)["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 2)
        self.assertEqual(item["longest"], 4)


class DryRunTests(unittest.TestCase):
    def test_a_dry_run_row_never_extends_the_streak(self):
        rows = [_run(_days_ago(1)), _run(_days_ago(0), dry_run="full")]
        item = streak.compute(rows, today=TODAY)["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 1)
        self.assertEqual(item["last_text"], "10-23")

    def test_an_entry_flagged_as_dry_run_never_extends_the_streak(self):
        rows = [_run(_days_ago(1), entry_dry_run="select")]
        result = streak.compute(rows, today=TODAY)
        # 整条 entry 是空跑 → 不算任何目标成功过（账号还在，但目标是空的）
        account = result["accounts"]["111"]
        self.assertEqual(account["targets"], [])
        self.assertEqual(account["text"], "还没有成功记录")

    def test_an_entry_with_false_dry_run_counts(self):
        rows = [_run(_days_ago(1), entry_dry_run=False)]
        item = streak.compute(rows, today=TODAY)["accounts"]["111"]["targets"][0]
        self.assertTrue(item["known"])
        self.assertEqual(item["current"], 1)


class FailureTests(unittest.TestCase):
    def test_failed_entries_do_not_count(self):
        rows = [_run(_days_ago(1)), _run(_days_ago(0), ok=False)]
        item = streak.compute(rows, today=TODAY)["accounts"]["111"]["targets"][0]
        self.assertEqual(item["current"], 1)
        self.assertEqual(item["last_text"], "10-23")


class TargetResolutionTests(unittest.TestCase):
    def test_entries_are_keyed_by_conversation_not_by_display_name(self):
        rows = [
            _run(_days_ago(1), conv="conv-a", label="🌟"),
            _run(_days_ago(0), conv="conv-a", label="改过名"),
        ]
        result = streak.compute(rows, today=TODAY)
        items = result["accounts"]["111"]["targets"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["current"], 2)
        self.assertEqual(items[0]["label"], "改过名")

    def test_entries_without_a_conversation_fall_back_to_the_label(self):
        rows = [{"finished_at": TODAY.isoformat(), "dry_run": "off",
                 "accounts": [{"unique_id": "111", "entries": [
                     {"target": "扯？", "ok": True, "at": TODAY.isoformat()}]}]}]
        items = streak.compute(rows, today=TODAY)["accounts"]["111"]["targets"]
        self.assertEqual(items[0]["key"], "扯？")
        self.assertEqual(items[0]["current"], 1)

    def test_configured_targets_without_any_success_still_show_up(self):
        result = streak.compute([_run(_days_ago(1))], today=TODAY,
                                targets_by_account={"111": ["🌟", "wheat"]})
        items = {item["label"]: item for item in result["accounts"]["111"]["targets"]}
        self.assertTrue(items["🌟"]["known"])
        self.assertFalse(items["wheat"]["known"])
        self.assertEqual(streak.streak_text(items["wheat"]), "还没有成功记录")
        self.assertEqual(result["accounts"]["111"]["unknown"], 1)

    def test_friends_removed_from_the_config_stop_being_counted(self):
        # 账本是全部历史：早就被移掉的好友还躺在里面，它们的"上次成功"停在很久以前。
        # 不按当前配置过一遍，首屏会永远挂一条"有目标断签了"（线上真实数据就是这样）。
        rows = [
            _run(_days_ago(0), conv="conv-a", label="🌟"),
            _run(_days_ago(20), conv="conv-old", label="早就删掉的人"),
        ]
        result = streak.compute(rows, today=TODAY, targets_by_account={"111": ["🌟"]})
        summary = result["accounts"]["111"]
        self.assertEqual([item["label"] for item in summary["targets"]], ["🌟"])
        self.assertEqual(summary["broken"], 0, "删掉的好友不能算断签")

    def test_without_a_configured_list_everything_is_kept(self):
        rows = [_run(_days_ago(20), conv="conv-old", label="早就删掉的人")]
        result = streak.compute(rows, today=TODAY)
        self.assertEqual(len(result["accounts"]["111"]["targets"]), 1,
                         "不知道它有哪些好友时不能替它删")

    def test_two_accounts_are_kept_apart(self):
        rows = [_run(_days_ago(0), uid="111"), _run(_days_ago(3), uid="222")]
        result = streak.compute(rows, today=TODAY)
        self.assertEqual(result["accounts"]["111"]["current"], 1)
        self.assertEqual(result["accounts"]["222"]["current"], 0)
        self.assertTrue(result["accounts"]["222"]["broken"])


class WindowTests(unittest.TestCase):
    def test_window_starts_at_the_oldest_record(self):
        rows = [_run(_days_ago(offset)) for offset in range(29)]
        result = streak.compute(rows, today=TODAY)
        self.assertEqual(result["window_days"], 29)
        self.assertEqual(result["window_start"], _days_ago(28).isoformat())
        self.assertIn("只统计最近 29 天", result["window_text"])

    def test_an_empty_ledger_is_a_one_day_window(self):
        result = streak.compute([], today=TODAY)
        self.assertEqual(result["accounts"], {})
        self.assertEqual(result["window_days"], 1)

    def test_rows_without_timestamps_are_ignored(self):
        result = streak.compute([{"run_id": "x", "accounts": [{"unique_id": "111"}]}], today=TODAY)
        self.assertEqual(result["accounts"], {})

    def test_broken_targets_are_listed_for_the_banner(self):
        rows = [_run(_days_ago(4), conv="a", label="🌟"), _run(_days_ago(0), conv="b", label="wheat")]
        summary = streak.compute(rows, today=TODAY)["accounts"]["111"]
        self.assertEqual(summary["broken"], 1)
        self.assertEqual(summary["broken_labels"], ["🌟"])
        self.assertIn("连续", summary["text"])


if __name__ == "__main__":
    unittest.main()
