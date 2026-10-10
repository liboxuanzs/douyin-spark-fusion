# -*- coding: utf-8 -*-
"""webui.viewmodel 的单元测试。

viewmodel 是"面板要显示什么"的**唯一**来源（B 的模板与 app.js 都只认它产出的键），
所以这里钉住三件事：

1. 与 B 的模板/JS 约定的**键名**不能改（改了页面会静默不更新，而不是报错）；
2. 四个档位的数字必须自洽（每个目标恰好落在一档里，加起来等于 total）；
3. 它在任何脏数据下都不能抛异常（面板是只读展示层，脏账本不该把首页打成 500）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core import schedule  # noqa: E402
from webui import viewmodel  # noqa: E402


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class _FakeLock:
    def __init__(self, **kwargs):
        self.exists = kwargs.get("exists", True)
        self.running = kwargs.get("running", False)
        self.pid = kwargs.get("pid")
        self.started_at = kwargs.get("started_at")
        self.dry_run = kwargs.get("dry_run", "off")
        self.source = kwargs.get("source", "webui")
        self.run_id = kwargs.get("run_id", "")
        self.reason = kwargs.get("reason", "")


class _FakeRate:
    def __init__(self, **kwargs):
        self.data = {
            "exists": kwargs.get("exists", False),
            "tripped": kwargs.get("tripped", False),
            "active": kwargs.get("active", False),
            "expired": False,
            "keyword": kwargs.get("keyword", ""),
            "until": kwargs.get("until", ""),
            "until_text": kwargs.get("until_text", ""),
            "seconds_left": kwargs.get("seconds_left", 0),
        }

    def as_dict(self):
        return dict(self.data)


def _snapshot(**kwargs):
    base = {
        "username": "账号甲",
        "unique_id": "10001",
        "fingerprint": "fp-1",
        "target_count": 0,
        "targets": [],
        "cookie_configured": 1,
        "cookie_names": ["sessionid"],
        "cookie_message": "",
        "ready": True,
        "gate": "READY",
        "gate_text": "正常",
        "sent_ok": 0,
        "sent_fail": 0,
        "missing": [],
        "select_failed": [],
        "last_run_id": "",
        "last_run_text": "",
        "status": "ok",
        "status_text": "成功",
    }
    base.update(kwargs)
    return base


def _account(**kwargs):
    base = {
        "username": "账号甲",
        "unique_id": "10001",
        "status": "ok",
        "gate": "READY",
        "sent_ok": 0,
        "sent_fail": 0,
        "entries": [],
        "missing": [],
        "select_failed": [],
        "targets": [],
    }
    base.update(kwargs)
    return base


def _entry(target="好友甲", ok=True, level="strong", **kwargs):
    row = {
        "target": target,
        "ok": ok,
        "level": level,
        "reason": "",
        "via": "http+dom",
        "message_id": "mid-1",
        "dry_run": "off",
        "rate_limited": False,
        "at": _iso(datetime.now(timezone.utc)),
        "at_text": "2026-10-01 12:30:00",
    }
    row.update(kwargs)
    return row


def _record(accounts=None, **kwargs):
    base = {
        "run_id": "20261001-123000-abcd",
        "started_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=3)),
        "finished_at": _iso(datetime.now(timezone.utc)),
        "started_text": "2026-10-01 12:30:00",
        "finished_text": "2026-10-01 12:30:03",
        "duration_text": "3 秒",
        "dry_run": "off",
        "source": "webui",
        "source_text": "面板触发",
        "status": "ok",
        "status_text": "成功",
        "rate_limited": False,
        "skip_reason": "",
        "skip_until": "",
        "accounts": accounts if accounts is not None else [_account()],
    }
    base.update(kwargs)
    return base


class PercentTests(unittest.TestCase):
    def test_半向上取整(self) -> None:
        self.assertEqual(viewmodel.percent(1, 2), 50)
        self.assertEqual(viewmodel.percent(1, 3), 33)
        self.assertEqual(viewmodel.percent(2, 3), 67)
        self.assertEqual(viewmodel.percent(0, 0), 0, "除以零不能炸")

    def test_强弱两段加起来不超过_100(self) -> None:
        total = 9
        for confirmed in range(0, total + 1):
            for weak in range(0, total + 1 - confirmed):
                strong_pct, weak_pct = viewmodel.progress_pair(confirmed, confirmed + weak, total)
                with self.subTest(confirmed=confirmed, weak=weak):
                    self.assertLessEqual(strong_pct + weak_pct, 100, "整条进度条不能超过满格")
                    self.assertGreaterEqual(weak_pct, 0)
                    self.assertGreaterEqual(strong_pct, 0)

    def test_强弱两段之和等于已发送占比(self) -> None:
        # 弱段是"取余数"算出来的，所以两段加起来必须正好等于已发送的百分比，
        # 否则进度条会凭空多一截或少一截。
        for confirmed, weak in ((1, 1), (2, 3), (7, 1), (0, 4)):
            strong_pct, weak_pct = viewmodel.progress_pair(confirmed, confirmed + weak, 9)
            with self.subTest(confirmed=confirmed, weak=weak):
                self.assertEqual(strong_pct + weak_pct, viewmodel.percent(confirmed + weak, 9))


class EntryBucketTests(unittest.TestCase):
    def test_四档归类(self) -> None:
        self.assertEqual(viewmodel.entry_bucket({"ok": True, "level": "strong"}), "success")
        self.assertEqual(viewmodel.entry_bucket({"ok": True, "level": "medium"}), "weak")
        self.assertEqual(viewmodel.entry_bucket({"ok": False, "level": "conflict"}), "failed")
        self.assertEqual(viewmodel.entry_bucket({"ok": False, "level": ""}), "failed")


class AccountCardTests(unittest.TestCase):
    def test_每个目标恰好落在一档里(self) -> None:
        snapshot = _snapshot(targets=["甲", "乙", "丙", "丁"], target_count=4)
        record = _record(
            accounts=[
                _account(
                    targets=["甲", "乙", "丙", "丁"],
                    sent_ok=2,
                    sent_fail=1,
                    entries=[
                        _entry("甲"),
                        _entry("乙", level="medium"),
                        _entry("丙", ok=False, level="conflict", reason="conv_mismatch"),
                    ],
                )
            ]
        )
        card = viewmodel.build_account(snapshot, record)
        self.assertEqual(card["confirmed"], 1)
        self.assertEqual(card["weak"], 1)
        self.assertEqual(card["failed"], 1)
        self.assertEqual(card["pending"], 1)
        self.assertEqual(
            card["confirmed"] + card["weak"] + card["failed"] + card["pending"],
            card["total"],
            "四个档位加起来必须等于目标总数（否则页面上会凭空多出/漏掉目标）",
        )
        self.assertEqual(card["state"], "attention", "有失败目标时账号状态是需处理")

    def test_明细被截断时以账本汇总数为准(self) -> None:
        snapshot = _snapshot(targets=["甲"], target_count=30)
        record = _record(
            accounts=[
                _account(
                    targets=["甲"],
                    sent_ok=25,
                    sent_fail=3,
                    entries=[_entry("甲")],
                    entries_truncated=True,
                )
            ]
        )
        card = viewmodel.build_account(snapshot, record)
        self.assertGreaterEqual(card["confirmed"], 25, "截断时不能只报明细里的 1 条")
        self.assertGreaterEqual(card["failed"], 3)
        self.assertTrue(any("截断" in issue["text"] or "上限" in issue["text"] for issue in card["issues"]))

    def test_没出现和选中失败的目标都进需处理(self) -> None:
        snapshot = _snapshot(targets=["甲", "乙"], target_count=2)
        record = _record(
            accounts=[
                _account(
                    targets=["甲", "乙"],
                    missing=["乙"],
                    select_failed=["甲"],
                    entries=[_entry("丙")],
                )
            ]
        )
        card = viewmodel.build_account(snapshot, record)
        rows = {row["target"]: row for row in card["segments"]["failed"]}
        self.assertIn("乙", rows, "会话列表里没出现的目标要能在页面上点名")
        self.assertIn("甲", rows, "选中失败的目标也要点名")
        self.assertTrue(rows["乙"]["reason_text"])

    def test_门禁没过就是阻断(self) -> None:
        snapshot = _snapshot(gate="EXPIRED", gate_text="登录失效", ready=False, cookie_configured=0)
        card = viewmodel.build_account(snapshot, _record())
        self.assertEqual(card["state"], "blocked")
        self.assertEqual(card["state_css"], "paused", "blocked 复用 B 的 state-paused 配色")
        self.assertTrue(any(issue["level"] == "danger" for issue in card["issues"]))

    def test_没有记录时不炸(self) -> None:
        card = viewmodel.build_account(_snapshot(targets=["甲"], target_count=1), None)
        self.assertEqual(card["total"], 1)
        self.assertEqual(card["pending"], 1)
        self.assertEqual(card["state"], "idle")

    def test_脏数据不炸(self) -> None:
        snapshot = {"username": None, "unique_id": None, "targets": None, "gate": None}
        record = {"accounts": [{"unique_id": None, "entries": [None, 3, {"target": None}], "targets": None}]}
        card = viewmodel.build_account(snapshot, record)
        self.assertTrue(card["username"])
        self.assertIsInstance(card["total"], int)


class PerAccountRunTests(unittest.TestCase):
    """每个账号可以有自己的发送时间（`core/schedule.py` 的派发器每 5 分钟只为当时到点
    的账号跑一轮），所以"全局最近一轮"里通常只有一两个账号。

    2026-10-07 线上就是这么谎报的：wheat 17:35 已经 4/4 强确认，20:35 那一轮只跑了
    另一个账号，wheat 的卡立刻变成「未尝试 4 / 还没跑过」。
    """

    WHEAT_TARGETS = ["全都去", "^ω^, 我先鼠了…", "🌈LBX🌈", "·7"]

    def _snapshots(self):
        return [
            _snapshot(username="wheat", unique_id="10050yi",
                      targets=list(self.WHEAT_TARGETS), target_count=4),
            _snapshot(username="🌈LBX🌈", unique_id="76998876121",
                      targets=["甲", "乙", "丙"], target_count=3),
        ]

    def _wheat_run(self):
        """wheat 17:35 那一轮：4 个目标全部强确认。"""
        return _record(
            run_id="20261007-173500-tmr1",
            started_text="2026-10-07 17:35:00",
            accounts=[
                _account(
                    username="wheat", unique_id="10050yi",
                    targets=list(self.WHEAT_TARGETS), sent_ok=4,
                    entries=[_entry(name) for name in self.WHEAT_TARGETS],
                )
            ],
        )

    def _other_run(self):
        """20:35 那一轮：只有另一个账号（wheat 不在这里面）。"""
        return _record(
            run_id="20261007-203500-tmr2",
            started_text="2026-10-07 20:35:00",
            accounts=[
                _account(
                    username="🌈LBX🌈", unique_id="76998876121",
                    targets=["甲", "乙", "丙"], sent_ok=3,
                    entries=[_entry("甲"), _entry("乙"), _entry("丙")],
                )
            ],
        )

    def _pool(self):
        # 账本是新→旧的
        return [self._other_run(), self._wheat_run()]

    def test_总览每张卡取自己最近一轮(self) -> None:
        cards = viewmodel.account_cards(
            self._snapshots(), self._other_run(), records=self._pool(), per_account=True
        )
        by_id = {card["unique_id"]: card for card in cards}
        wheat = by_id["10050yi"]
        self.assertEqual(wheat["confirmed"], 4, "wheat 的 4 个目标上一轮已经强确认了")
        self.assertEqual(wheat["pending"], 0, "不能因为最近一轮没轮到它就报「未尝试 4」")
        self.assertEqual(wheat["run_id"], "20261007-173500-tmr1", "数字必须来自它自己那一轮")
        self.assertNotEqual(wheat["state"], "idle", "它跑过、而且全成功，不能显示成还没跑过")
        self.assertFalse(wheat["run_missing"])
        self.assertEqual(by_id["76998876121"]["confirmed"], 3)

    def test_运行记录页冻在选中那一轮并说清没轮到它(self) -> None:
        cards = viewmodel.account_cards(
            self._snapshots(), self._other_run(), records=self._pool(), per_account=False
        )
        wheat = {card["unique_id"]: card for card in cards}["10050yi"]
        self.assertTrue(wheat["run_missing"], "这一轮里没有它，模板要靠这个字段解释")
        self.assertEqual(wheat["run_id"], "20261007-203500-tmr2", "这一页必须冻在选中那一轮")
        self.assertEqual(wheat["pending"], 4)
        self.assertEqual(wheat["own_run_id"], "20261007-173500-tmr1", "它自己那一轮也要带出去")
        self.assertEqual(wheat["own_run_text"], "2026-10-07 17:35:00")

    def test_从没跑过的账号说的是没试过(self) -> None:
        cards = viewmodel.account_cards(
            [_snapshot(username="新账号", unique_id="999", targets=["甲"], target_count=1)],
            self._other_run(),
            records=self._pool(),
            per_account=True,
        )
        card = cards[0]
        self.assertTrue(card["run_missing"])
        self.assertEqual(card["own_run_text"], "", "账本里没有它 —— 模板要走另一句文案")
        self.assertEqual(card["pending"], 1)
        self.assertEqual(card["state"], "idle")

    def _build(self, *, per_account, selected=None):
        return viewmodel.build(
            records=self._pool(),
            accounts=self._snapshots(),
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "12:30"},
            schedules=["12:30"],
            quiet={"active": False},
            missing_browser=[],
            default_dry_run="off",
            selected=selected,
            per_account=per_account,
        )

    def test_总览汇总按各账号自己的那一轮算(self) -> None:
        summary = self._build(per_account=True)["summary"]
        self.assertEqual(summary["confirmed"], 7, "4 + 3，两个账号各自最近一轮加起来")
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["total"], 7)
        self.assertTrue(summary["mixed_runs"], "数字来自不止一轮，必须说出来")
        self.assertEqual(summary["run_id"], "", "各算各的时不能挑一个 run_id 顶上")
        self.assertEqual(summary["run_count"], 2)

    def test_运行记录页的汇总仍然只是那一轮(self) -> None:
        summary = self._build(per_account=False, selected=self._other_run())["summary"]
        self.assertEqual(summary["confirmed"], 3)
        self.assertEqual(summary["pending"], 4, "选中那一轮里 wheat 确实一个都没试过")
        self.assertFalse(summary["mixed_runs"])
        self.assertEqual(summary["run_id"], "20261007-203500-tmr2")

    def test_档位不一致时明说不一致(self) -> None:
        dry = self._other_run()
        dry["dry_run"] = "dry"
        view = viewmodel.build(
            records=[dry, self._wheat_run()],
            accounts=self._snapshots(),
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "12:30"},
            schedules=["12:30"],
            quiet={"active": False},
            missing_browser=[],
            default_dry_run="off",
            per_account=True,
        )
        summary = view["summary"]
        self.assertEqual(summary["dry_run"], "mixed", "有的真发有的空跑，不能只说一个档位")
        self.assertIn("不一致", summary["dry_run_text"])

    def test_档位一致时照旧写那一个(self) -> None:
        summary = self._build(per_account=True)["summary"]
        self.assertEqual(summary["dry_run"], "off")


class SummaryTests(unittest.TestCase):
    def test_汇总键与_B_的_JS_一致(self) -> None:
        snapshot = _snapshot(targets=["甲", "乙"], target_count=2)
        record = _record(accounts=[_account(targets=["甲", "乙"], entries=[_entry("甲")])])
        cards = [viewmodel.build_account(snapshot, record)]
        summary = viewmodel.summarize(cards, last_at="2026-10-01 12:30:03")
        for key in (
            "total",
            "confirmed",
            "pageEcho",
            "receiptOnly",
            "attention",
            "pending",
            "unprocessed",
            "remaining",
            "sentTotal",
            "lastConfirmedAt",
            "accounts",
            "blocked_targets",
        ):
            self.assertIn(key, summary, f"B 的 app.js 会读 summary.{key}，不能少")
        self.assertEqual(summary["accounts"], 1, "accounts 是**个数**不是列表")
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["receiptOnly"], 0, "C 没有「仅回执」这一档，恒定 0")


class ScheduleTimerTests(unittest.TestCase):
    """`.env` 里的 12:30 只是"打算几点跑"，真正会不会跑由 systemd 定时器说了算。"""

    def _view(self, timer):
        return viewmodel.build(
            records=[],
            accounts=[_snapshot(targets=["甲"], target_count=1)],
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "12:30"},
            next_run=datetime.now(timezone.utc) + timedelta(hours=1),
            schedules=["12:30"],
            quiet={"active": False},
            missing_browser=[],
            timer=timer,
            default_dry_run="off",
        )

    def test_定时器没启用时不许画下次触发时间(self) -> None:
        view = self._view({"name": "douyin-spark-fusion-daily.timer", "installed": True,
                           "enabled": False, "state": "disabled"})
        schedule = view["schedule"]
        self.assertFalse(schedule["armed"])
        self.assertEqual(schedule["nextTriggerText"], "", "disabled 的定时器不会触发，不能写个时间骗人")
        self.assertEqual(schedule["nextTriggerAt"], "")
        self.assertIn("定时器未启用", schedule["label"])
        self.assertFalse(schedule["hasWorkNextTrigger"], "没定时器就不该说'下次触发还有 N 个待发'")
        self.assertEqual(schedule["timerText"], "定时器未启用")

    def test_定时器启用时照常给时间(self) -> None:
        view = self._view({"name": "x.timer", "installed": True, "enabled": True, "state": "enabled"})
        schedule = view["schedule"]
        self.assertTrue(schedule["armed"])
        self.assertNotEqual(schedule["nextTriggerText"], "")
        self.assertEqual(schedule["timerText"], "定时器已启用")
        self.assertFalse(schedule["dispatcher"], "经典模式：一次定时跑全部账号")

    def test_派发器模式讲的是账号而不是心跳(self) -> None:
        """timer 每 5 分钟敲一次门、各账号按自己的时间发（`core/schedule.py`）。

        这时候 `remainingTargets`/`hasWorkNextTrigger`（= 上一轮的 pending）跟"下次
        触发会不会发"没关系：线上就出现过面板写着"当前没有待发送目标，下一次触发
        不会实际发送"，而 20:30 那个账号还没到点。
        """
        schedule = self._view({
            "name": "x.timer", "installed": True, "enabled": True,
            "state": "enabled", "dispatchMinutes": 5,
        })["schedule"]
        self.assertTrue(schedule["dispatcher"], "前端要靠它换一套说法")
        self.assertIn("各账号按自己的时间发", schedule["label"])
        self.assertIn("今天该发的都发完了", schedule["nextTriggerText"], "没有下一个 due 时要说这句话")

    def test_派发器模式下有下一个账号时说清是谁几点(self) -> None:
        view = viewmodel.build(
            records=[],
            accounts=[_snapshot(targets=["甲"], target_count=1, send_time="23:59")],
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "12:30"},
            next_run=None,
            schedules=["12:30"],
            quiet={"active": False},
            missing_browser=[],
            timer={"name": "x.timer", "installed": True, "enabled": True,
                   "state": "enabled", "dispatchMinutes": 5},
            next_due={"at": "2026-10-07T23:59:00+08:00", "text": "下一个 甲：今天 23:59"},
            default_dry_run="off",
        )
        schedule = view["schedule"]
        self.assertEqual(schedule["nextTriggerText"], "下一个 甲：今天 23:59")
        payload = viewmodel.overview_payload(view)
        self.assertTrue(payload["schedule"]["dispatcher"])
        self.assertEqual(payload["schedule"]["nextTriggerText"], "下一个 甲：今天 23:59",
                         "app.js 读的就是这个键，缺了会一律写成'今天该发的都发完了'")

    def test_没装定时器单元时说清楚(self) -> None:
        schedule = self._view({"name": "x.timer", "installed": False, "enabled": False,
                               "state": "missing"})["schedule"]
        self.assertEqual(schedule["timerText"], "没装定时器单元")
        self.assertEqual(schedule["nextTriggerText"], "")

    def test_拿不到定时器状态时不误报(self) -> None:
        """面板可能在 Windows/容器里跑，探不到 systemd 就按原来的样子显示，别乱报警。"""
        schedule = self._view(None)["schedule"]
        self.assertTrue(schedule["armed"])
        self.assertNotEqual(schedule["nextTriggerText"], "")


    def test_定时器时间与_env_对不上时要说出来(self) -> None:
        """真会跑的是单元文件里的时间；`.env` 只影响面板算的"下次触发"，两边对不上必须讲清楚。"""
        schedule = self._view({
            "name": "x.timer", "installed": True, "enabled": True, "state": "enabled",
            "onCalendar": ["08:00"], "jitterText": "最多 20 分钟",
        })["schedule"]
        self.assertEqual(schedule["timerTimes"], ["08:00"])
        self.assertTrue(schedule["timerMismatch"])
        self.assertIn("系统定时器", schedule["label"])
        self.assertIn("08:00", schedule["label"])
        self.assertIn("最多 20 分钟", schedule["label"], "抖动也要写出来，不然用户以为准点跑")

    def test_时间一致时不算不一致(self) -> None:
        schedule = self._view({
            "name": "x.timer", "installed": True, "enabled": True, "state": "enabled",
            "onCalendar": ["12:30"],
        })["schedule"]
        self.assertFalse(schedule["timerMismatch"])
        self.assertIn("系统定时器：12:30", schedule["label"])

    def test_拿到单元时间时以它为准(self) -> None:
        """`.env` 说 12:30、单元说 08:00 —— 标签必须写 08:00（那才是真的）。"""
        schedule = self._view({
            "name": "x.timer", "installed": True, "enabled": True, "state": "enabled",
            "onCalendar": ["08:00"],
        })["schedule"]
        self.assertIn("08:00", schedule["label"])
        self.assertNotIn("每日 12:30", schedule["label"])


    def test_运行配置那行定时时间也以定时器为准(self) -> None:
        rows = {row["label"]: row["value"] for row in viewmodel.config_rows(
            {"schedule_text": "12:30"}, None,
            {"armed": False, "timerTimes": [], "timerMismatch": False})}
        self.assertIn("不会自动跑", rows["定时时间"])

        rows = {row["label"]: row["value"] for row in viewmodel.config_rows(
            {"schedule_text": "12:30"}, None,
            {"armed": True, "timerTimes": ["08:00"], "timerMismatch": True})}
        self.assertIn("08:00", rows["定时时间"])
        self.assertIn("12:30", rows["定时时间"], "对不上时要把 .env 里的值也写出来")

        rows = {row["label"]: row["value"] for row in viewmodel.config_rows(
            {"schedule_text": "12:30"}, None,
            {"armed": True, "timerTimes": ["12:30"], "timerMismatch": False})}
        self.assertEqual(rows["定时时间"], "12:30")


class QuietClashTests(unittest.TestCase):
    """禁跑时间窗盖住定时器时间 = 到点整轮跳过、一条都不发。

    2026-10-01 改定时时间时差点踩上：`.env` 里没写 `QUIET_WINDOW`，而代码当时的默认窗
    是 20:20-21:00 —— 正好把新的 20:30 包住，跑起来会"什么都没发生"。面板必须提前说。
    """

    def _schedule(self, spec, on_calendar, state="enabled"):
        view = viewmodel.build(
            records=[],
            accounts=[_snapshot(targets=["甲"], target_count=1)],
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "20:30"},
            next_run=datetime.now(timezone.utc) + timedelta(hours=1),
            schedules=["20:30"],
            quiet={"active": False, "spec": spec},
            missing_browser=[],
            timer={"name": "x.timer", "installed": state != "missing",
                   "enabled": state == "enabled", "state": state,
                   "onCalendar": list(on_calendar)},
            default_dry_run="off",
        )
        return view["schedule"]

    def test_窗口盖住定时器时间时要说出来(self) -> None:
        schedule = self._schedule("20:20-21:00", ["20:30"])
        clash = schedule["quietClash"]
        self.assertTrue(clash, "20:20-21:00 包住了 20:30，必须报出来")
        self.assertEqual(clash["times"], ["20:30"])
        self.assertIn("20:20-21:00", clash["text"])
        self.assertIn("20:30", clash["text"])
        self.assertIn("整轮跳过", clash["text"])

    def test_窗口与定时器错开时不报(self) -> None:
        self.assertFalse(self._schedule("20:20-21:00", ["12:30"])["quietClash"])

    def test_窗口关掉时不报(self) -> None:
        for spec in ("off", "", "OFF", "none", "20:20"):
            with self.subTest(spec=spec):
                self.assertFalse(self._schedule(spec, ["20:30"])["quietClash"])

    def test_定时器没启用时不报(self) -> None:
        """没启用的定时器本来就不会跑，红框留给上面那条"不会自动跑"。"""
        self.assertFalse(self._schedule("20:20-21:00", ["20:30"], state="disabled")["quietClash"])

    def test_跨零点的窗口也认(self) -> None:
        clash = self._schedule("23:50-00:30", ["00:10"])["quietClash"]
        self.assertEqual(clash["times"], ["00:10"])

    def test_多个定时时间只报被盖住的那个(self) -> None:
        clash = self._schedule("20:20-21:00", ["08:00", "20:30"])["quietClash"]
        self.assertEqual(clash["times"], ["20:30"])


class OverviewPayloadTests(unittest.TestCase):
    def _view(self, **overrides):
        record = _record(accounts=[_account(targets=["甲"], entries=[_entry("甲")])])
        view = viewmodel.build(
            records=[record],
            accounts=[_snapshot(targets=["甲"], target_count=1)],
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "12:30"},
            next_run=datetime.now(timezone.utc) + timedelta(hours=1),
            schedules=["12:30"],
            quiet={"active": False},
            missing_browser=[],
            default_dry_run="off",
        )
        view.update(overrides)
        return view

    def test_契约键齐全且可序列化(self) -> None:
        payload = viewmodel.overview_payload(self._view(), now_iso=_iso(datetime.now(timezone.utc)), tz="Asia/Shanghai")
        json.dumps(payload, ensure_ascii=False)  # 必须能被 json 序列化（app.js 直接 JSON.parse）
        for key in ("ok", "now", "tz", "summary", "task", "schedule", "accounts"):
            self.assertIn(key, payload)
        self.assertIn("progressPercent", payload["summary"])
        account = payload["accounts"][0]
        for key in ("uniqueId", "state", "total", "confirmed", "pageEcho", "attention", "pending"):
            self.assertIn(key, account, f"app.js 的 updateAccounts 会读 accounts[].{key}")

    def test_账号字段用的是_JS_认的驼峰名(self) -> None:
        payload = viewmodel.overview_payload(self._view())
        self.assertEqual(payload["accounts"][0]["uniqueId"], "10001")


class TaskStateTests(unittest.TestCase):
    def test_运行中会算年龄(self) -> None:
        started = datetime.now(timezone.utc) - timedelta(seconds=95)
        task = viewmodel.build(
            records=[],
            accounts=[],
            lock=_FakeLock(running=True, started_at=started.isoformat(), run_id="r1"),
            rate_limit=_FakeRate(),
            cfg={},
            next_run=None,
            schedules=[],
            quiet={},
            missing_browser=[],
            default_dry_run="off",
        )["task"]
        self.assertTrue(task["running"])
        self.assertGreaterEqual(task["ageSeconds"], 90, "面板要显示「已运行约 N 秒」，不能恒为 0")
        self.assertLessEqual(task["ageSeconds"], 300)

    def test_锁文件不存在时不出横幅(self) -> None:
        view = viewmodel.build(
            records=[],
            accounts=[],
            lock=_FakeLock(exists=False, running=False, reason="锁文件不存在"),
            rate_limit=_FakeRate(),
            cfg={},
            next_run=None,
            schedules=[],
            quiet={},
            missing_browser=[],
            default_dry_run="off",
        )
        self.assertFalse(any(banner["kind"] == "lock" for banner in view["banners"]))

    def test_进程没了但锁还在才提示(self) -> None:
        view = viewmodel.build(
            records=[],
            accounts=[],
            lock=_FakeLock(exists=True, running=False, reason="进程已退出"),
            rate_limit=_FakeRate(),
            cfg={},
            next_run=None,
            schedules=[],
            quiet={},
            missing_browser=[],
            default_dry_run="off",
        )
        self.assertTrue(any(banner["kind"] == "lock" for banner in view["banners"]))


class BannerTests(unittest.TestCase):
    def _build(self, **kwargs):
        params = dict(
            records=[],
            accounts=[],
            lock=_FakeLock(running=False),
            rate_limit=_FakeRate(),
            cfg={},
            next_run=None,
            schedules=[],
            quiet={},
            missing_browser=[],
            default_dry_run="off",
        )
        params.update(kwargs)
        return viewmodel.build(**params)

    def test_冷却中与禁跑窗口各出一条(self) -> None:
        view = self._build(
            rate_limit=_FakeRate(tripped=True, active=True, keyword="操作频繁", until="2026-10-01T13:00:00"),
            quiet={"active": True, "reason": "当前处于禁跑时间窗 20:20-21:00", "until": "2026-10-01T21:00:00"},
        )
        kinds = [banner["kind"] for banner in view["banners"]]
        self.assertIn("cooldown", kinds)
        self.assertIn("quiet_window", kinds)
        cooldown = next(item for item in view["banners"] if item["kind"] == "cooldown")
        self.assertIn("操作频繁", cooldown["text"])
        self.assertTrue(cooldown["icon"], "每条横幅都要有图标名（模板用 data-lucide 渲染）")

    def test_阻断时给出不能运行的原因(self) -> None:
        view = self._build(missing_browser=["cloakbrowser"])
        self.assertIn("cloakbrowser", " ".join(view["blockers"]))
        self.assertFalse(view["can_run"])
        self.assertTrue(any(banner["kind"] == "browser" for banner in view["banners"]))

    def test_可运行时没有任何阻断(self) -> None:
        view = self._build()
        self.assertTrue(view["can_run"])
        self.assertEqual(view["blockers"], [])


class DryRunTests(unittest.TestCase):
    def test_三档与后端一致(self) -> None:
        from webui import app as app_mod

        self.assertEqual(tuple(viewmodel.DRY_RUN_LEVELS), tuple(app_mod.DRY_RUN_LEVELS))
        for level in viewmodel.DRY_RUN_LEVELS:
            self.assertIn(level, viewmodel.DRY_RUN_LABELS)
            self.assertTrue(viewmodel.DRY_RUN_HINTS[level], "每档都要有一句人话解释")
        self.assertEqual(viewmodel.DRY_RUN_LABELS["off"], "真实发送")

    def test_非法默认值回落到_off(self) -> None:
        view = viewmodel.build(
            records=[],
            accounts=[],
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={},
            next_run=None,
            schedules=[],
            quiet={},
            missing_browser=[],
            default_dry_run="不存在的档位",
        )
        self.assertEqual(view["dry_run"]["default"], "off")


class RunRowTests(unittest.TestCase):
    def test_表格行键齐全(self) -> None:
        row = viewmodel.run_row(_record())
        for key in ("run_id", "started_text", "status", "status_text", "dry_run_text", "source_text", "entries"):
            self.assertIn(key, row)
        self.assertEqual(row["source_text"], "面板触发")


class PurityTests(unittest.TestCase):
    def test_viewmodel_不引入浏览器依赖也不碰_core_tasks(self) -> None:
        """viewmodel 是纯函数层：面板能渲染，靠的只是账本文件。"""
        code = (
            "import sys, os;"
            f"sys.path.insert(0, r'{REPO_ROOT}');"
            "import webui.viewmodel;"
            "print('cloakbrowser' in sys.modules, 'playwright' in sys.modules, 'core.tasks' in sys.modules)"
        )
        import subprocess

        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=120,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "False False False", "viewmodel 必须能独立 import")


class ReasonLabelCoverageTests(unittest.TestCase):
    """判定原因码必须说人话。

    `core` 里每多一个 `reason=` 码，面板的"需处理"就会多显示一行英文——用户看不懂，
    而且看起来像面板坏了。这条测试从源码里把码抓出来，逼着 `REASON_LABELS` 跟上。
    """

    def _codes(self) -> set:
        import re

        codes = set()
        for name in ("core/douyin_im.py", "core/strict_verify.py", "core/quiet_window.py"):
            path = os.path.join(REPO_ROOT, name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            codes |= set(re.findall(r'R_[A-Z_]+\s*=\s*"([a-z][a-z_]*)"', src))
            codes |= set(re.findall(r'"reason":\s*"([a-z][a-z_]*)"', src))
            codes |= set(re.findall(r'reason\s*=\s*"([a-z][a-z_]*)"', src))
        return codes

    def test_每个原因码都有中文标签(self) -> None:
        codes = self._codes()
        self.assertTrue(codes, "没抓到任何原因码，正则大概失效了")
        missing = sorted(c for c in codes if c not in viewmodel.REASON_LABELS)
        self.assertEqual(missing, [], f"面板上会显示英文码：{missing}")

    def test_标签不重复也不为空(self) -> None:
        seen = {}
        for code, label in viewmodel.REASON_LABELS.items():
            self.assertTrue(label.strip(), f"{code} 的标签是空的")
            self.assertNotIn(
                label, seen, f"{code} 与 {seen.get(label)} 的标签一模一样，页面上分不出来"
            )
            seen[label] = code

    def test_输入框相关的码都点名了(self) -> None:
        for code in ("editor_missing", "editor_text_mismatch", "editor_not_cleared", "editor_residue"):
            self.assertIn(code, viewmodel.REASON_LABELS)
            self.assertIn("输入框", viewmodel.REASON_LABELS[code])


class ViaLabelCoverageTests(unittest.TestCase):
    """回执来源（`via`）也必须说人话，而且不能说错。

    面板「运行详情」每行会写一句证据来源（` · HTTP 回执…`）。`core` 里换一种来源，
    页面上就会冒出一个英文码；更坏的是措辞比事实更强 —— `via="http"` 只说明
    HTTP 回执先到了，`_wait_receipt` 拿到它就再等最多 1 秒收工，**不代表**会话回显里
    没有这条。写成「仅 HTTP 回执」会让用户以为另一侧看过且不认。
    """

    def _codes(self) -> set:
        import re

        with open(os.path.join(REPO_ROOT, "core/douyin_im.py"), encoding="utf-8") as fh:
            src = fh.read()
        # 只在 `type_and_send` 体内找：`via` 这个名字在别处另有含义
        # （`_wait_list_ready` 的 `"dom-stable"` 说的是"会话列表就绪"，跟回执无关，
        #  它到不了账本，页面上也永远看不到）。
        start = src.index("def type_and_send")
        end = src.index("\n    def ", start + 1)
        body = src[start:end]
        codes = set(re.findall(r'"via":\s*"([a-z][a-z+\-]*)"', body))
        # `"via": ("http+dom" if … else "http" if … else "dom" …)` —— 三元表达式跨行
        for expr in re.findall(r'"via":\s*\(([^)]*)\)', body):
            codes |= set(re.findall(r'"([a-z][a-z+\-]*)"', expr))
        return codes

    def test_每种回执来源都有中文标签(self) -> None:
        codes = self._codes()
        self.assertEqual(
            codes, {"http+dom", "http", "dom", "dry-run", "none"},
            "core 里的 via 取值变了：新值要在 VIA_LABELS 里补中文，删掉的值这里也要跟着改",
        )
        missing = sorted(c for c in codes if c not in viewmodel.VIA_LABELS)
        self.assertEqual(missing, [], f"面板上会显示英文码：{missing}")

    def test_标签不重复也不为空(self) -> None:
        seen = {}
        for code, label in viewmodel.VIA_LABELS.items():
            self.assertTrue(label.strip(), f"{code} 的标签是空的")
            self.assertNotIn(label, seen, f"{code} 与 {seen.get(label)} 的标签一模一样")
            seen[label] = code

    def test_只有_http_回执时不许说成另一侧不认(self) -> None:
        label = viewmodel.VIA_LABELS["http"]
        self.assertNotIn("仅", label, f"「{label}」暗示会话回显看过了且不认，事实是没等到")
        self.assertIn("会话回显", label, "要说清楚少的是哪一侧")



class LoginLostCardTests(unittest.TestCase):
    """运行期掉登录的账号卡：必须说清"登录失效、去重新扫码"，而不是"有几个失败"。

    2026-10-09 wheat：发送时才发现登录失效，账本里是 4 条发送失败，卡片当时只会
    显示「需处理 4」和一堆"发送失败"，用户根本不知道要做的是重新扫码。
    """

    def _card(self, **over):
        account = _account(sent_fail=4, status="error",
                           error="login_lost: 登录已失效（服务端不认这个 sessionid）"
                                 "，请到面板「扫码登录」重新登录该账号",
                           entries=[_entry(ok=False, level="none",
                                           reason="login_lost")], **over)
        return viewmodel.build_account(_snapshot(), _record(accounts=[account]))

    def test_state_is_blocked_not_just_attention(self):
        card = self._card()
        self.assertEqual(card["state"], "blocked")

    def test_issue_tells_the_user_to_scan_again(self):
        card = self._card()
        texts = [issue["text"] for issue in card["issues"]]
        self.assertTrue(any("登录已失效" in t for t in texts), texts)
        self.assertTrue(any("扫码" in t for t in texts), texts)
        self.assertTrue(any(issue["level"] == "danger" for issue in card["issues"]))

    def test_nothing_sent_is_blocked_and_explains_itself(self):
        """门禁通过、校验全 strong，却**一条都没送达** —— 卡片必须点名这件事。

        2026-10-10 🌈LBX🌈：账本 `sent_ok=0 / sent_fail=3`、`error="nothing_sent: …"`。
        修复前它记的是 `status=ok`，卡片显示"需处理 3"，用户根本看不出
        "不是三个好友失败，是这条会话被服务端作废了"。
        """
        account = _account(status="error", sent_fail=3,
                           error="nothing_sent: 本轮 3 个目标一条都没送达…"
                                 "请到面板「扫码登录」重新登录该账号",
                           entries=[_entry(ok=False, level="none", reason="")])
        card = viewmodel.build_account(_snapshot(), _record(accounts=[account]))
        self.assertEqual(card["state"], "blocked")
        texts = [issue["text"] for issue in card["issues"]]
        self.assertTrue(any("一条都没送达" in t for t in texts), texts)
        self.assertTrue(any("扫码" in t for t in texts), texts)

    def test_gate_expired_points_at_scanning_again(self):
        """门禁就判出登录失效（EXPIRED）时，卡片也要说清下一步是重新扫码。"""
        account = _account(status="skipped", sent_ok=0, sent_fail=0, entries=[],
                           gate="EXPIRED")
        snapshot = _snapshot(gate="EXPIRED", gate_text="登录已失效",
                             status="skipped", status_text="跳过")
        card = viewmodel.build_account(snapshot, _record(accounts=[account]))
        texts = [issue["text"] for issue in card["issues"]]
        self.assertTrue(any("上次门禁" in t and "扫码" in t for t in texts), texts)
        self.assertEqual(card["state"], "blocked")

    def test_plain_send_failure_stays_attention(self):
        """对照：普通发送失败（登录还在）仍然是 attention，不该被这条规则吞掉。"""
        account = _account(sent_fail=4, status="ok", error=None,
                           entries=[_entry(ok=False, level="none", reason="no_receipt")])
        card = viewmodel.build_account(_snapshot(), _record(accounts=[account]))
        self.assertEqual(card["state"], "attention")
        self.assertFalse(any("登录已失效" in issue["text"] for issue in card["issues"]))



class NextDueSkipTests(unittest.TestCase):
    """「下一个该发的是谁」必须和派发器同一口径：`skipped` 当天还会再试。

    2026-10-09 wheat 掉登录：上一轮被记成 `skipped`（门禁没过、一条都没发出去），
    派发器下一次门铃还会再试它，而面板顶上却写着「今天该发的都发完了」——
    两处说法相反，用户只能猜。根因是 `next_due()` 把"今天有记录"一律当成"发过了"。
    """

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="dsf-nextdue-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.state_file = os.path.join(self._dir, "schedule_state.json")
        self._old = os.environ.get("SCHEDULE_STATE_FILE")
        os.environ["SCHEDULE_STATE_FILE"] = self.state_file

        def restore():
            if self._old is None:
                os.environ.pop("SCHEDULE_STATE_FILE", None)
            else:
                os.environ["SCHEDULE_STATE_FILE"] = self._old
        self.addCleanup(restore)

    @staticmethod
    def _when(text="2026-10-09 22:30:00"):
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()

    def _mark(self, status, text="2026-10-09 22:30:00"):
        when = self._when(text)
        state = schedule.load_state()
        schedule.mark_sent(state, "10050yi", when=when, status=status,
                           plan={"time": "17:20", "due_at": when}, run_id="r1")
        schedule.save_state(state)

    def _account(self):
        return {"unique_id": "10050yi", "username": "wheat", "send_time": "17:20"}

    def test_skipped_keeps_it_as_the_next_one(self):
        self._mark(schedule.STATUS_SKIPPED)
        got = viewmodel.next_due([self._account()], when=self._when("2026-10-09 22:40:00"))
        self.assertIsNotNone(got, "跳过的那一轮当天还会再试，不能说'都发完了'")
        self.assertEqual(got["name"], "wheat")

    def test_ok_means_nothing_left_today(self):
        self._mark(schedule.STATUS_OK)
        self.assertIsNone(
            viewmodel.next_due([self._account()], when=self._when("2026-10-09 22:40:00"))
        )

    def test_error_also_counts_as_done_for_today(self):
        """`error`（跑挂了）**不**当天重试（否则坏 cookie 一天被踹 288 次）。"""
        self._mark(schedule.STATUS_ERROR)
        self.assertIsNone(
            viewmodel.next_due([self._account()], when=self._when("2026-10-09 22:40:00"))
        )

    def test_yesterdays_skip_does_not_resurrect_today(self):
        self._mark(schedule.STATUS_SKIPPED, text="2026-10-08 17:31:00")
        got = viewmodel.next_due([self._account()], when=self._when("2026-10-09 22:40:00"))
        # 今天的 due_at 是 17:31（已过），仍然应该报它是"下一个"
        self.assertIsNotNone(got)


if __name__ == "__main__":
    unittest.main()
