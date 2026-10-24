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

    2026-10-07 线上就是这么谎报的：示例号 17:35 已经 4/4 强确认，20:35 那一轮只跑了
    另一个账号，示例号 的卡立刻变成「未尝试 4 / 还没跑过」。
    """

    WHEAT_TARGETS = ["好友A", "好友B", "🌈示例号🌈", "好友C"]

    def _snapshots(self):
        return [
            _snapshot(username="示例号", unique_id="11223344556",
                      targets=list(self.WHEAT_TARGETS), target_count=4),
            _snapshot(username="🌈示例号🌈", unique_id="12345678905",
                      targets=["甲", "乙", "丙"], target_count=3),
        ]

    def _示例号_run(self):
        """示例号 17:35 那一轮：4 个目标全部强确认。"""
        return _record(
            run_id="20261007-173500-tmr1",
            started_text="2026-10-07 17:35:00",
            accounts=[
                _account(
                    username="示例号", unique_id="11223344556",
                    targets=list(self.WHEAT_TARGETS), sent_ok=4,
                    entries=[_entry(name) for name in self.WHEAT_TARGETS],
                )
            ],
        )

    def _other_run(self):
        """20:35 那一轮：只有另一个账号（示例号 不在这里面）。"""
        return _record(
            run_id="20261007-203500-tmr2",
            started_text="2026-10-07 20:35:00",
            accounts=[
                _account(
                    username="🌈示例号🌈", unique_id="12345678905",
                    targets=["甲", "乙", "丙"], sent_ok=3,
                    entries=[_entry("甲"), _entry("乙"), _entry("丙")],
                )
            ],
        )

    def _pool(self):
        # 账本是新→旧的
        return [self._other_run(), self._示例号_run()]

    def test_总览每张卡取自己最近一轮(self) -> None:
        cards = viewmodel.account_cards(
            self._snapshots(), self._other_run(), records=self._pool(), per_account=True
        )
        by_id = {card["unique_id"]: card for card in cards}
        示例号 = by_id["11223344556"]
        self.assertEqual(示例号["confirmed"], 4, "示例号 的 4 个目标上一轮已经强确认了")
        self.assertEqual(示例号["pending"], 0, "不能因为最近一轮没轮到它就报「未尝试 4」")
        self.assertEqual(示例号["run_id"], "20261007-173500-tmr1", "数字必须来自它自己那一轮")
        self.assertNotEqual(示例号["state"], "idle", "它跑过、而且全成功，不能显示成还没跑过")
        self.assertFalse(示例号["run_missing"])
        self.assertEqual(by_id["12345678905"]["confirmed"], 3)

    def test_运行记录页冻在选中那一轮并说清没轮到它(self) -> None:
        cards = viewmodel.account_cards(
            self._snapshots(), self._other_run(), records=self._pool(), per_account=False
        )
        示例号 = {card["unique_id"]: card for card in cards}["11223344556"]
        self.assertTrue(示例号["run_missing"], "这一轮里没有它，模板要靠这个字段解释")
        self.assertEqual(示例号["run_id"], "20261007-203500-tmr2", "这一页必须冻在选中那一轮")
        self.assertEqual(示例号["pending"], 4)
        self.assertEqual(示例号["own_run_id"], "20261007-173500-tmr1", "它自己那一轮也要带出去")
        self.assertEqual(示例号["own_run_text"], "2026-10-07 17:35:00")

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
        self.assertEqual(summary["pending"], 4, "选中那一轮里 示例号 确实一个都没试过")
        self.assertFalse(summary["mixed_runs"])
        self.assertEqual(summary["run_id"], "20261007-203500-tmr2")

    def test_档位不一致时明说不一致(self) -> None:
        dry = self._other_run()
        dry["dry_run"] = "dry"
        view = viewmodel.build(
            records=[dry, self._示例号_run()],
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

    def test_轮询接口按同一口径说各账号各自最近一次(self) -> None:
        """issue #2：首屏写「各账号各自最近一次（共 2 轮）」，轮询得能重画同一句话。

        漏发 summary.mixedRuns 的后果不是报错：10 秒后同一位置变成「上一轮…」，
        数字一个没变、口径被悄悄改错，用户没有任何办法看出来。
        """
        view = self._build(per_account=True)
        payload = viewmodel.overview_payload(view)
        self.assertTrue(payload["summary"]["mixedRuns"])
        self.assertEqual(payload["summary"]["runCount"], view["summary"]["run_count"])
        self.assertEqual(payload["summary"]["runCount"], 2)

    def test_没混轮时轮询说的是上一轮(self) -> None:
        payload = viewmodel.overview_payload(
            self._build(per_account=False, selected=self._other_run())
        )
        self.assertFalse(payload["summary"]["mixedRuns"])


class OverviewPayloadWiringTests(unittest.TestCase):
    """issue #2/#4：轮询接口的字段必须真的来自服务端算好的那一份，别自己另算一套。"""

    def test_定时器挂了的诊断原样发出(self) -> None:
        """issue #2：首屏给的是「哪里坏了 + 怎么修」，轮询不能把它换成一句废话。"""
        view = viewmodel.build(
            records=[],
            accounts=[_snapshot(targets=["甲"], target_count=1)],
            lock=_FakeLock(),
            rate_limit=_FakeRate(),
            cfg={"message_template": "你好", "schedule_text": "12:30"},
            schedules=["12:30"],
            quiet={"active": False},
            missing_browser=[],
            # 「已启用」只说明开机自启：systemctl stop 过 / failed 的 timer 照样是
            # enabled，到点一样不会跑 —— 这正是首屏要解释的那种坏法。
            timer={"name": "x.timer", "installed": True, "enabled": True,
                   "state": "enabled", "activeState": "failed", "dispatchMinutes": 5},
            default_dry_run="off",
        )
        schedule = view["schedule"]
        self.assertIn("systemd 里当前是 failed", schedule["timerStallText"])
        self.assertFalse(schedule["armed"], "挂了就不能算 armed，否则前端会画一个不会发生的点")
        payload = viewmodel.overview_payload(view)
        self.assertEqual(payload["schedule"]["timerStallText"], schedule["timerStallText"])
        self.assertEqual(payload["schedule"]["timerActiveState"], "failed")

    def test_账号卡那几个会变的字段原样发出(self) -> None:
        cards = viewmodel.account_cards(
            [_snapshot(targets=["甲"], target_count=1, send_time="12:30")], None
        )
        card = viewmodel.overview_payload({"accounts": cards})["accounts"][0]
        for src, dst in (
            ("state_label", "stateLabel"),
            ("chip", "chip"),
            ("run_missing", "runMissing"),
            ("own_run_text", "ownRunText"),
            ("send_window", "sendWindow"),
            ("send_state", "sendState"),
            ("send_state_text", "sendStateText"),
            ("has_message", "hasMessage"),
            ("message_lines", "messageLines"),
        ):
            with self.subTest(key=dst):
                self.assertEqual(card[dst], cards[0][src], f"{dst} 必须与卡上的逐字一致")

    def test_账号卡的默认值不会漏键(self) -> None:
        """app.js 会直接读这些键：缺键等于 undefined，文案会变成 undefined 或空。"""
        card = viewmodel.overview_payload({"accounts": [{"unique_id": "1"}]})["accounts"][0]
        self.assertFalse(card["runMissing"])
        self.assertFalse(card["hasMessage"])
        self.assertEqual(card["stateLabel"], "")
        self.assertEqual(card["chip"], "")
        self.assertEqual(card["ownRunText"], "")
        self.assertEqual(card["sendWindow"], "")
        self.assertEqual(card["sendState"], "")
        self.assertEqual(card["sendStateText"], "")
        self.assertEqual(card["messageLines"], 0)


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


class StoppedAccountViewTests(unittest.TestCase):
    """停用账号在面板上的样子：说"已停用"，并且不占"今天要发多少"。"""

    def _stopped(self, **kwargs):
        base = {"enabled": False, "targets": ["甲"], "target_count": 1}
        base.update(kwargs)
        return _snapshot(**base)

    def test_the_card_says_stopped(self):
        card = viewmodel.build_account(self._stopped(), None)
        self.assertFalse(card["enabled"])
        self.assertEqual(card["state"], "stopped")
        self.assertEqual(card["state_label"], "已停用")
        self.assertEqual(card["chip"], "info")
        self.assertEqual(card["send_state"], "stopped")
        self.assertIn("已停用", card["send_state_text"])
        self.assertIn("不会自动发送", card["send_state_text"])

    def test_stopped_beats_blocked(self):
        """登录态掉了 + 已停用：显示"已停用"（它本来就不该发，别催人去扫码）。"""
        card = viewmodel.build_account(
            self._stopped(gate="EXPIRED", gate_text="登录失效", ready=False, cookie_configured=0),
            None,
        )
        self.assertEqual(card["state"], "stopped")

    def test_summary_does_not_count_stopped_targets(self):
        live = viewmodel.build_account(
            _snapshot(targets=["甲", "乙"], target_count=2),
            _record(accounts=[_account(targets=["甲", "乙"], entries=[_entry("甲")])]),
        )
        stopped = viewmodel.build_account(
            self._stopped(targets=["甲", "乙", "丙"], target_count=3), None
        )
        summary = viewmodel.summarize([live, stopped], last_at="2026-10-01 12:30:03")
        self.assertEqual(summary["accounts"], 2, "两个账号都还在配置里")
        self.assertEqual(summary["accounts_active"], 1)
        self.assertEqual(summary["accounts_stopped"], 1)
        self.assertEqual(summary["total"], 2, "停用账号的目标不该算进今天的进度")
        self.assertEqual(summary["pending"], 1)

    def test_header_account_count_explains_the_stopped_ones(self):
        """页头「N 个账号，共 M 个目标」：M 不数停用账号的目标，N 也要说清楚。

        2026-10-24 面板实测的缺陷：一个账号停用、一个启用时写「2 个账号，共 2 个目标」
        （启用那个号自己有 2 个目标），全停用时写「2 个账号，共 0 个目标」—— 两个
        数字互相打脸，也完全没提「有账号被停用了」，用户只能自己去翻卡片。
        """
        live = viewmodel.build_account(
            _snapshot(targets=["甲", "乙"], target_count=2),
            _record(accounts=[_account(targets=["甲", "乙"], entries=[_entry("甲")])]),
        )
        stopped = viewmodel.build_account(
            self._stopped(targets=["甲", "乙", "丙"], target_count=3), None
        )
        summary = viewmodel.summarize([live, stopped])
        self.assertEqual(summary["accounts_text"], "1 个账号（另有 1 个已停用）")
        self.assertEqual(
            viewmodel.summarize([stopped])["accounts_text"],
            "1 个账号全部已停用",
        )
        self.assertEqual(
            viewmodel.summarize([live])["accounts_text"],
            "1 个账号",
            "没有停用账号时，文案与原来一致",
        )

    def test_poll_payload_carries_that_text(self):
        """10 秒轮询也要能重画这一格（否则账号数停在首屏，目标数却在变）。"""
        view = {"summary": {"accounts_text": "1 个账号（另有 1 个已停用）", "total": 2}}
        payload = viewmodel.overview_payload(view)
        self.assertEqual(payload["summary"]["accountsText"], "1 个账号（另有 1 个已停用）")

    def test_all_stopped_is_said_out_loud(self):
        schedule_view = viewmodel._schedule_state(
            [], 0, None, [],
            timer={"dispatchMinutes": 5, "state": "enabled"},
            stopped=1, accounts_total=1,
        )
        self.assertEqual(schedule_view["nextTriggerText"], "所有账号都已停用，不会再自动发送")

    def test_partly_stopped_is_mentioned(self):
        schedule_view = viewmodel._schedule_state(
            [], 0, None, [],
            timer={"dispatchMinutes": 5, "state": "enabled"},
            stopped=1, accounts_total=3,
        )
        self.assertEqual(
            schedule_view["nextTriggerText"], "今天该发的都发完了（另有 1 个账号已停用）"
        )



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

    def test_值槽用谁几点的短句而不是整句话(self) -> None:
        """值槽是 26px 大字，塞整句话会撑成两三行、把整条概览拉高。

        派发器模式下要额外给一句短的（"甲 · 已过 23:59"）给值槽用，整句话留给
        提示行；app.js 轮询时读的就是这个键，缺了字号/句式会每 10 秒跳一次。
        """
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
            next_due={"at": "2026-10-07T23:59:00+08:00", "name": "甲",
                      "due_text": "23:59", "imminent": True,
                      "text": "下一个 甲：已过 23:59，等下一次敲门就发"},
            default_dry_run="off",
        )
        schedule = view["schedule"]
        self.assertEqual(schedule["nextTriggerShort"], "甲 · 已过 23:59")
        self.assertEqual(schedule["nextTriggerText"], "下一个 甲：已过 23:59，等下一次敲门就发",
                         "整句话要原样留着：提示行和没有短句时的回落都靠它")
        payload = viewmodel.overview_payload(view)
        self.assertEqual(payload["schedule"]["nextTriggerShort"], "甲 · 已过 23:59",
                         "app.js 读的就是这个键")

    def test_还没过点时短句不写已过(self) -> None:
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
            next_due={"at": "2026-10-07T20:00:00+08:00", "name": "甲",
                      "due_text": "23:59", "imminent": False,
                      "text": "下一个 甲：今天 23:59"},
            default_dry_run="off",
        )
        self.assertEqual(view["schedule"]["nextTriggerShort"], "甲 · 23:59")

    def test_拼不出短句时留空让前端回落(self) -> None:
        """单测/老调用方可能只喂 {at, text}（没有 name/due_text）：短句留空，
        nextTriggerText 照旧 —— 值槽回落到整句话，只是字小一号，不许报错。"""
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
        self.assertEqual(schedule["nextTriggerShort"], "")
        self.assertEqual(schedule["nextTriggerText"], "下一个 甲：今天 23:59")

    def test_定时器没启用时短句也要清空(self) -> None:
        """disabled 的定时器不会触发：值槽连短句都不许留，否则面板上会有个
        "甲 · 23:59" 挂在那里骗人。"""
        view = self._view({"name": "x.timer", "installed": True, "enabled": False,
                           "state": "disabled"})
        schedule = view["schedule"]
        self.assertFalse(schedule["armed"])
        self.assertEqual(schedule["nextTriggerShort"], "")

    def test_没装定时器单元时说清楚(self) -> None:
        schedule = self._view({"name": "x.timer", "installed": False, "enabled": False,
                               "state": "missing"})["schedule"]
        self.assertEqual(schedule["timerText"], "没装定时器单元")
        self.assertEqual(schedule["nextTriggerText"], "")
        self.assertEqual(schedule["nextTriggerShort"], "")

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

    def test_轮询也带着扫码会话状态(self) -> None:
        """「重新登录」按钮的禁用理由有两路：正在跑（task.running）与扫码会话开着。

        后者以前只写在首屏 HTML 的 `data-login-active` 上，轮询从不刷新 ——
        会话取消之后按钮一直灰着，只能手动刷新页面（2026-10-20 的真实投诉）。
        这里钉住服务端：payload 里必须有 `login.active`，前端才有的可同步。
        """
        payload = viewmodel.overview_payload(self._view())
        self.assertEqual(payload["login"], {"active": False}, "没有 login 时按未开会话算")
        busy = self._view(login={"session": {"active": True}})
        self.assertIs(viewmodel.overview_payload(busy)["login"]["active"], True)


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


class SkipReasonTextTests(unittest.TestCase):
    """整轮跳过也要说人话（2026-10-24 复查）。

    「运行记录」页与首页横幅原来直接把账本里的英文码印出来：
    `整轮跳过：already_sent`、`原因：account_stopped`。中文界面里突然冒一句英文，
    用户既看不懂，也不知道该不该管 —— 与发送明细的 reason 码是同一个毛病。
    """

    def test_run_row_carries_the_chinese_text(self):
        row = viewmodel.run_row(_record(skip_reason="account_stopped"))
        self.assertEqual(row["skip_reason"], "account_stopped", "原始码要留着排查")
        self.assertEqual(row["skip_reason_text"], "账号都已停用")

    def test_unknown_code_is_passed_through(self):
        """没见过的码原样带出去，不编故事。"""
        row = viewmodel.run_row(_record(skip_reason="brand_new_code"))
        self.assertEqual(row["skip_reason_text"], "brand_new_code")

    def test_the_banner_says_it_in_chinese_too(self):
        banners = viewmodel._banners(
            cooldown={}, quiet={}, task={}, summary={},
            missing_browser=[], blocked_reason="", last_skip="already_sent",
        )
        text = " ".join(banner["text"] for banner in banners)
        self.assertIn("今天已经发过了", text)
        self.assertNotIn("already_sent", text)


class SkipReasonCoverageTests(unittest.TestCase):
    """`core/tasks.py` 里新加的 `reason=` 码必须同时进 SKIP_LABELS。

    少一个，那一轮跳过的原因就会以英文码的形式出现在两个页面上。
    """

    def test_每个跳过码都有中文标签(self) -> None:
        import re

        with open(os.path.join(REPO_ROOT, "core/tasks.py"), encoding="utf-8") as fh:
            src = fh.read()
        codes = set(re.findall(r'reason="([a-z][a-z_]*)"', src))
        self.assertTrue(codes, "没抓到任何跳过原因码，正则大概失效了")
        missing = sorted(c for c in codes if c not in viewmodel.SKIP_LABELS)
        self.assertEqual(missing, [], f"页面上会显示英文码：{missing}")


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

    2026-10-09 示例号：发送时才发现登录失效，账本里是 4 条发送失败，卡片当时只会
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

        2026-10-10 🌈示例号🌈：账本 `sent_ok=0 / sent_fail=3`、`error="nothing_sent: …"`。
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

    def test_gate_dead_session_says_the_day_will_not_be_retried(self):
        """门禁判死（core/tasks.py 的 GATE_DEAD_STATUSES）时，卡片要说清两件事。

        2026-10-10 示例号 起的新记账口径：门禁 EXPIRED 记 `error`（`login_dead`），
        当天不再重试 —— 免得每 5 分钟重开一次浏览器去喂风控。但用户会看到
        "今天不试了"，所以必须同时说清「重新登录成功后当天照样补发」，
        否则会以为一整天就这么报废了。
        """
        account = _account(
            status="error", sent_ok=0, sent_fail=0, entries=[], gate="EXPIRED",
            error="login_dead: 登录已失效（登录已失效（有 sessionid 但服务端不认）），"
                  "本轮一条都没发出去，当天也不会再重试（拿同一份失效 cookie 反复开"
                  "浏览器对风控只有坏处）；请到面板「扫码登录」重新登录该账号，"
                  "登录成功后当天会自动补发")
        snapshot = _snapshot(gate="EXPIRED", gate_text="登录已失效",
                             status="error", status_text="出错")
        card = viewmodel.build_account(snapshot, _record(accounts=[account]))
        self.assertEqual(card["state"], "blocked")
        texts = [issue["text"] for issue in card["issues"]]
        self.assertTrue(any("今天不会再重试" in t for t in texts), texts)
        self.assertTrue(any("扫码" in t for t in texts), texts)
        self.assertTrue(any("自动补发" in t for t in texts), texts)

    def test_plain_send_failure_stays_attention(self):
        """对照：普通发送失败（登录还在）仍然是 attention，不该被这条规则吞掉。"""
        account = _account(sent_fail=4, status="ok", error=None,
                           entries=[_entry(ok=False, level="none", reason="no_receipt")])
        card = viewmodel.build_account(_snapshot(), _record(accounts=[account]))
        self.assertEqual(card["state"], "attention")
        self.assertFalse(any("登录已失效" in issue["text"] for issue in card["issues"]))



class NextDueSkipTests(unittest.TestCase):
    """「下一个该发的是谁」必须和派发器同一口径：`skipped` 当天还会再试。

    2026-10-09 示例号 掉登录：上一轮被记成 `skipped`（门禁没过、一条都没发出去），
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
        schedule.mark_sent(state, "11223344556", when=when, status=status,
                           plan={"time": "17:20", "due_at": when}, run_id="r1")
        schedule.save_state(state)

    def _account(self):
        return {"unique_id": "11223344556", "username": "示例号", "send_time": "17:20"}

    def test_skipped_keeps_it_as_the_next_one(self):
        self._mark(schedule.STATUS_SKIPPED)
        got = viewmodel.next_due([self._account()], when=self._when("2026-10-09 22:40:00"))
        self.assertIsNotNone(got, "跳过的那一轮当天还会再试，不能说'都发完了'")
        self.assertEqual(got["name"], "示例号")

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

    def test_a_stopped_account_is_not_the_next_one(self):
        """停用的账号不该被报成"下一个到点"—— 它今天不会自动发。"""
        got = viewmodel.next_due(
            [{**self._account(), "enabled": False}], when=self._when("2026-10-09 10:00:00")
        )
        self.assertIsNone(got)


    def test_yesterdays_skip_does_not_resurrect_today(self):
        self._mark(schedule.STATUS_SKIPPED, text="2026-10-08 17:31:00")
        got = viewmodel.next_due([self._account()], when=self._when("2026-10-09 22:40:00"))
        # 今天的 due_at 是 17:31（已过），仍然应该报它是"下一个"
        self.assertIsNotNone(got)


class SkipRetryExhaustedTests(unittest.TestCase):
    """试满上限的 `skipped` 当天不再重试 —— 面板文案与"下一个"都必须跟着改。

    `core/schedule.py` 给 `skipped` 记录加了 `attempts`：门铃每踹一次 +1，试满
    `SCHEDULE_SKIP_RETRY_MAX`（默认 6）之后 `select()` 当天不再踹它（记录留着、
    状态仍是 skipped）。面板旧文案一律写"下一次门铃会重试"，到上限后就是在骗人：
    今天不会再有门铃了，用户只能干等或者以为工具坏了。
    """

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="dsf-skipmax-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.state_file = os.path.join(self._dir, "schedule_state.json")
        self._old_state = os.environ.get("SCHEDULE_STATE_FILE")
        os.environ["SCHEDULE_STATE_FILE"] = self.state_file

        def restore_state():
            if self._old_state is None:
                os.environ.pop("SCHEDULE_STATE_FILE", None)
            else:
                os.environ["SCHEDULE_STATE_FILE"] = self._old_state
        self.addCleanup(restore_state)

        self._old_max = os.environ.get("SCHEDULE_SKIP_RETRY_MAX")
        os.environ.pop("SCHEDULE_SKIP_RETRY_MAX", None)   # 先按默认上限（6）测

        def restore_max():
            if self._old_max is None:
                os.environ.pop("SCHEDULE_SKIP_RETRY_MAX", None)
            else:
                os.environ["SCHEDULE_SKIP_RETRY_MAX"] = self._old_max
        self.addCleanup(restore_max)

    @staticmethod
    def _when(text="2026-10-12 23:00:00"):
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone()

    def _attempt(self, times=1):
        """模拟门铃踹了 N 次：`mark_sent(status=skipped)` 每调一次 attempts 就 +1。"""
        when = self._when("2026-10-12 21:30:00")
        state = schedule.load_state()
        for _ in range(times):
            schedule.mark_sent(state, "10060yi", when=when, status=schedule.STATUS_SKIPPED,
                               plan={"time": "21:30", "due_at": when}, run_id="r1")
        schedule.save_state(state)

    def _fields(self):
        return viewmodel.schedule_fields("10060yi", "21:30", when=self._when())

    def _account(self):
        return {"unique_id": "10060yi", "username": "示例号", "send_time": "21:30"}

    def test_没到上限时仍然说下一次门铃会重试(self):
        self._attempt(3)
        self.assertIn("下一次门铃会重试", self._fields()["send_state_text"])

    def test_试满上限后不再说会重试(self):
        self._attempt(6)
        text = self._fields()["send_state_text"]
        self.assertIn("试到上限", text)
        self.assertNotIn("下一次门铃会重试", text)
        self.assertIn("重新扫码登录", text, "到上限后唯一的补救办法必须写在面板上")

    def test_上限由_env_决定而不是写死_6(self):
        """`SCHEDULE_SKIP_RETRY_MAX=3` 时试 3 次就该到上限（面板不许自己比数字）。"""
        os.environ["SCHEDULE_SKIP_RETRY_MAX"] = "3"
        self._attempt(3)
        self.assertIn("试到上限", self._fields()["send_state_text"])

    def test_老记录没有_attempts_字段时文案不变(self):
        """旧版本写的 `skipped` 记录没有 `attempts` → 按"没到上限"，文案与从前一致。"""
        when = self._when("2026-10-12 21:30:00")
        state = schedule.load_state()
        state.setdefault("accounts", {})["10060yi"] = {
            "date": "2026-10-12", "time": "21:30", "due_at": schedule.iso(when),
            "sent_at": schedule.iso(when), "run_id": "old",
            "status": schedule.STATUS_SKIPPED, "source": "cli", "note": "",
        }
        schedule.save_state(state)
        text = self._fields()["send_state_text"]
        self.assertIn("下一次门铃会重试", text)
        self.assertNotIn("试到上限", text)

    def test_试满上限后下一个该发的是明天(self):
        self._attempt(6)
        got = viewmodel.next_due([self._account()], when=self._when())
        self.assertIsNotNone(got, "今天不动了，但「下一个」要落到明天，不能说都发完了")
        self.assertTrue(got.get("exhausted"))
        self.assertNotIn("等下一次敲门", got["text"])
        self.assertIn("明天", got["text"])
        due = datetime.fromisoformat(got["at"])
        self.assertEqual(schedule.day_of(due), "2026-10-13")
        # 与派发器同一份计划：明天那一刻就是 `plan_for` 算出来的（含抖动）
        plan = schedule.plan_for({"unique_id": "10060yi", "time": "21:30"},
                                 when=self._when("2026-10-13 23:00:00"),
                                 jitter=schedule.jitter_minutes_env())
        self.assertEqual(got["due_text"], plan["due_text"])

    def test_没到上限时下一个仍然是今天(self):
        self._attempt(3)
        got = viewmodel.next_due([self._account()], when=self._when())
        self.assertIsNotNone(got)
        self.assertFalse(got.get("exhausted"))
        self.assertIn("等下一次敲门", got["text"])


class ScheduleJitterTests(unittest.TestCase):
    """面板写的发送时刻必须和派发器同一口径 —— 抖动 span 是哈希的一部分。

    `core/schedule.py::plan_for` 把 jitter span 计进 `jitter_minutes(uid, day, span)`
    的哈希，派发器（`core/tasks.py`）一直传 `SCHEDULE_JITTER_MINUTES`，而面板这两处
    （`schedule_fields` / `next_due`）没传 —— 吃默认 15 分钟。默认值下两者恰好相等，
    所以这个 bug 平时隐身；用户把抖动调成 0 或 40 之后，面板写"今天 08:05 发"、
    实际却在别的分钟发，而且调过之后连"改回默认"都救不了（哈希本来就该跟着变）。
    """

    UID = "10001"  # 2026-10-11：span=0 → 08:00，span=15 → 08:05，span=40 → 07:39（负偏移）

    def setUp(self) -> None:
        self._old = os.environ.get("SCHEDULE_JITTER_MINUTES")
        self.addCleanup(self._restore_env)
        self._dir = tempfile.mkdtemp(prefix="dsf-jitter-")
        self.addCleanup(shutil.rmtree, self._dir, True)
        self._old_state = os.environ.get("SCHEDULE_STATE_FILE")
        os.environ["SCHEDULE_STATE_FILE"] = os.path.join(self._dir, "schedule_state.json")
        self.addCleanup(self._restore_state)

    def _restore_env(self) -> None:
        if self._old is None:
            os.environ.pop("SCHEDULE_JITTER_MINUTES", None)
        else:
            os.environ["SCHEDULE_JITTER_MINUTES"] = self._old

    def _restore_state(self) -> None:
        if self._old_state is None:
            os.environ.pop("SCHEDULE_STATE_FILE", None)
        else:
            os.environ["SCHEDULE_STATE_FILE"] = self._old_state

    @staticmethod
    def _when():
        return datetime(2026, 10, 11, 9, 0, 0).astimezone()

    def _dispatcher(self):
        """派发器口径（`core.schedule.select`）里这个账号今天的计划。"""
        picked = schedule.select(
            [{"unique_id": self.UID, "time": "08:00"}],
            when=self._when(),
            state={},
            jitter=schedule.jitter_minutes_env(),
        )
        return picked["plans"][self.UID]

    def _expected(self, span):
        """按 `core.schedule` 的定义式算出面板**应该**显示的时刻。

        故意不写死数字：2026-10-24 抖动改成前后对称之后，这个账号的偏移从
        +11 / +38 变成了 +5 / -21 —— 写死的断言只会替实现打掩护。
        """
        offset = schedule.jitter_minutes(self.UID, "2026-10-11", span)
        return (datetime(2026, 10, 11, 8, 0) + timedelta(minutes=offset)).strftime("%H:%M")

    def test_抖动为_0_时面板跟派发器一致(self):
        os.environ["SCHEDULE_JITTER_MINUTES"] = "0"
        fields = viewmodel.schedule_fields(self.UID, "08:00", when=self._when())
        self.assertEqual(fields["send_due"], self._dispatcher()["due_text"])
        # 0 分钟抖动 = 不抖：老代码（吃默认 15 分钟）会写 08:11。
        self.assertEqual(fields["send_due"], "08:00")

    def test_抖动为_40_时面板跟派发器一致(self):
        os.environ["SCHEDULE_JITTER_MINUTES"] = "40"
        fields = viewmodel.schedule_fields(self.UID, "08:00", when=self._when())
        self.assertEqual(fields["send_due"], self._dispatcher()["due_text"])
        self.assertEqual(fields["send_due"], self._expected(40))
        # span 参与哈希：40 分钟的落点必须和默认 15 分钟不同，否则这条测试白测。
        self.assertNotEqual(self._expected(40), self._expected(15))

    def test_下一个该发的也是同一口径(self):
        os.environ["SCHEDULE_JITTER_MINUTES"] = "40"
        got = viewmodel.next_due(
            [{"unique_id": self.UID, "username": "示例号", "send_time": "08:00"}],
            when=self._when(),
        )
        self.assertIsNotNone(got)
        self.assertEqual(got["due_text"], self._dispatcher()["due_text"])
        self.assertEqual(got["due_text"], self._expected(40))

    def test_没设_env_时仍然按默认_15_分钟(self):
        os.environ.pop("SCHEDULE_JITTER_MINUTES", None)
        fields = viewmodel.schedule_fields(self.UID, "08:00", when=self._when())
        self.assertEqual(fields["send_due"], self._expected(15))
        self.assertEqual(fields["send_due"], self._dispatcher()["due_text"])


class WholeRunErrorTests(unittest.TestCase):
    """「整轮 error」不许画成绿色正常。

    2026-10-11 线上：一轮里发出 1 条、0 条失败，然后浏览器崩了，账号结果记
    `status=error` —— 老判定只看 `failed`，于是卡片是绿色 healthy，跟"整轮跑完"
    长得一模一样；而 `core/schedule.py::select` 把 error 当成"今天办过了"，
    剩下的目标当天根本不会再试。看到绿色就不会有人去补，好友就那么断了。
    """

    def _card(self, **over):
        account = _account(
            status="error",
            sent_ok=1,
            sent_fail=0,
            error="browser_crashed: 浏览器异常退出（code=1）",
            entries=[_entry()],
            **over,
        )
        snapshot = _snapshot(status="error", status_text="出错", targets=["好友甲", "好友乙"])
        return viewmodel.build_account(
            snapshot, _record(status="error", status_text="出错", accounts=[account])
        )

    def test_整轮_error_是需处理而不是健康(self):
        card = self._card()
        self.assertNotEqual(card["state"], "healthy")
        self.assertEqual(card["state"], "attention")

    def test_整轮_error_给一条_danger_并说清还剩几条(self):
        card = self._card()
        dangers = [issue for issue in card["issues"] if issue["level"] == "danger"]
        self.assertTrue(dangers, card["issues"])
        text = " ".join(issue["text"] for issue in dangers)
        self.assertIn("本轮异常结束", text)
        self.assertIn("已发 1/2", text)
        self.assertIn("没有重试", text)

    def test_一条都没发出去的_error_仍然是阻断(self):
        """对照：`attempted<=0` 的 error（浏览器根本没起来）是 blocked，别被降级。"""
        account = _account(status="error", sent_ok=0, sent_fail=0, entries=[])
        card = viewmodel.build_account(
            _snapshot(status="error", status_text="出错", targets=["好友甲"]),
            _record(status="error", status_text="出错", accounts=[account]),
        )
        self.assertEqual(card["state"], "blocked")

    def test_登录失效的_error_不叠第二条整轮异常(self):
        """`login_lost` 已经有更具体的指引（去重新扫码），别再叠一条通用文案。"""
        account = _account(
            status="error",
            sent_ok=0,
            sent_fail=1,
            error="login_lost: 登录已失效（服务端不认这个 sessionid）",
            entries=[_entry(ok=False, level="none", reason="login_lost")],
        )
        card = viewmodel.build_account(
            _snapshot(status="error", status_text="出错"),
            _record(status="error", status_text="出错", accounts=[account]),
        )
        texts = " ".join(issue["text"] for issue in card["issues"])
        self.assertNotIn("本轮异常结束", texts)
        self.assertIn("登录已失效", texts)

    def test_被踢下线要说清是_KICK_而不是普通失效(self):
        """`{"decision": "KICK"}` 与"cookie 过期"是两件事：页面看着还登录着，

        是服务端把这个会话踢了。文案要给出唯一出路（重新扫码）并说明会自动补发，
        而且不能叠「本轮异常结束」—— 那会让用户以为已经发出去几条了。
        """
        account = _account(
            status="error",
            sent_ok=0,
            sent_fail=1,
            error="session_kicked: 服务端把会话踢下线（decision=KICK），本轮发送成功 0 条、失败 1 条",
            entries=[_entry(ok=False, level="none", reason="session_kicked")],
        )
        card = viewmodel.build_account(
            _snapshot(status="error", status_text="出错"),
            _record(status="error", status_text="出错", accounts=[account]),
        )
        texts = " ".join(issue["text"] for issue in card["issues"])
        self.assertIn("KICK", texts)
        self.assertIn("扫码登录", texts)
        self.assertIn("自动补发", texts)
        # 补发这句话只说一遍：`_REQUEUE_NOTE` 自己就带着"登录成功后会自动补发一次"，
        # 卡面再手写一句就是「自动补发一次（…登录成功后会自动补发一次）」（线上真踩到）。
        self.assertEqual(texts.count("自动补发"), 1, texts)
        self.assertNotIn("本轮异常结束", texts)
        self.assertEqual(card["state"], "blocked", "一条都没发出去，当天要人处理")

    def test_被踢之前已经发出去的条数要如实说(self):
        """KICK 可能落在第 2、3 条上：文案写死「一条都没发出去」会跟账本打架。

        core/tasks.py 的 error 写的是「本轮发送成功 N 条」，面板也必须按同一个
        口径说清楚 —— 用户要据此判断已经发给了谁、还要不要补。
        """
        account = _account(
            status="error",
            sent_ok=2,
            sent_fail=1,
            targets=["好友甲", "好友乙", "好友丙"],
            error="session_kicked: 服务端把会话踢下线（decision=KICK），本轮发送成功 2 条、失败 1 条",
            entries=[
                _entry(target="好友甲", ok=True, level="strong"),
                _entry(target="好友乙", ok=True, level="strong"),
                _entry(target="好友丙", ok=False, level="none", reason="session_kicked"),
            ],
        )
        card = viewmodel.build_account(
            _snapshot(status="error", status_text="出错", targets=["好友甲", "好友乙", "好友丙"]),
            _record(status="error", status_text="出错", accounts=[account]),
        )
        texts = " ".join(issue["text"] for issue in card["issues"])
        self.assertIn("已送达 2/3 条", texts)
        self.assertNotIn("一条都没发出去", texts)
        self.assertEqual(card["state"], "blocked")


class MaintenanceViewTests(unittest.TestCase):
    """「磁盘与资料目录」区块的数据层：字段名、安全线、以及"跳过了谁"。

    这一块是**会删文件**的（虽然默认只预演），所以钉住三件事：
    1. `core.maintenance.profile_report()` 的真实字段名（写错了页面只会静默空掉）；
    2. 名单读不到时 `ok=False` —— 面板据此把「执行」藏起来（core 侧一个都不删）；
    3. 每一行提前说清"能不能动"，别让用户拿「执行」当探针。
    """

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="dsf-maintenance-")
        self.addCleanup(shutil.rmtree, self._dir, True)

    @staticmethod
    def _disk():
        return {"ok": True, "text": "磁盘剩余 25.0 GB（共 100.0 GB，已用 75%）", "level": "warning"}

    def _report(self, **over):
        base = {
            "root": "/srv/spark/profiles",
            "exists": True,
            "known_ids": ["10001"],
            "error": "",
            "profiles": [
                {
                    "name": "10001",
                    "bytes": 1024 * 1024,
                    "cache_bytes": 512 * 1024,
                    "human_bytes": "1.0 MB",
                    "human_cache_bytes": "512.0 KB",
                    "idle_days": 0.0,
                    "is_orphan": False,
                    "in_use": True,
                    "is_scratch": False,
                },
                {
                    "name": "10002",
                    "bytes": 2 * 1024 * 1024,
                    "cache_bytes": 1024 * 1024,
                    "human_bytes": "2.0 MB",
                    "human_cache_bytes": "1.0 MB",
                    "idle_days": 9.5,
                    "is_orphan": True,
                    "in_use": False,
                    "is_scratch": False,
                },
                {
                    "name": "default",
                    "bytes": 3 * 1024 * 1024,
                    "cache_bytes": 2 * 1024 * 1024,
                    "human_bytes": "3.0 MB",
                    "human_cache_bytes": "2.0 MB",
                    "idle_days": 1.0,
                    "is_orphan": True,
                    "in_use": False,
                    "is_scratch": True,
                },
            ],
            "total_bytes": 6 * 1024 * 1024,
            "cache_bytes": 3584 * 1024,
            "orphan_bytes": 5 * 1024 * 1024,
            "human_total": "6.0 MB",
            "human_cache": "3.5 MB",
            "human_orphan": "5.0 MB",
        }
        base.update(over)
        return base

    def test_每个目录一行且字段名跟_core_一致(self):
        rows = viewmodel.profile_rows(self._report(), {"10001": "示例号"})
        self.assertEqual([row["name"] for row in rows], ["10001", "10002", "default"])
        running, orphan, scratch = rows
        self.assertEqual(running["label"], "示例号（10001）")
        self.assertEqual(running["human_cache_bytes"], "512.0 KB")
        self.assertTrue(running["in_use"])
        self.assertIn("正在跑", running["hint"])
        self.assertIn("在册", running["tags"])
        self.assertTrue(orphan["is_orphan"])
        self.assertIn("孤儿", orphan["tags"][0])
        self.assertIn("可以删", orphan["hint"])
        self.assertEqual(orphan["idle_text"], "9.5 天没动")
        # `default` 是扫码登录前的落脚点：永远算「临时」，不参与孤儿清理。
        self.assertTrue(scratch["is_scratch"])
        self.assertFalse(scratch["is_orphan"])
        self.assertEqual(scratch["tags"][0], "临时")
        self.assertIn("只清缓存", scratch["hint"])

    def test_宽限期没到就不说可以删(self):
        report = self._report(
            profiles=[
                {
                    "name": "10003",
                    "bytes": 1024,
                    "cache_bytes": 0,
                    "human_bytes": "1.0 KB",
                    "human_cache_bytes": "0 B",
                    "idle_days": 2.0,
                    "is_orphan": True,
                    "in_use": False,
                    "is_scratch": False,
                }
            ]
        )
        row = viewmodel.profile_rows(report, grace_days=7)[0]
        self.assertIn("还差 5.0 天", row["hint"])
        self.assertNotIn("可以删", row["hint"])

    def test_名单读不到时_ok_为假且带着错误(self):
        report = self._report(
            error="读不到账号名单：TASKS 里没有可用的 unique_id",
            known_ids=[],
            profiles=[],
            total_bytes=0,
            cache_bytes=0,
            orphan_bytes=0,
        )
        view = viewmodel.maintenance_view(report=report, disk=self._disk())
        self.assertFalse(view["ok"])
        self.assertIn("读不到账号名单", view["error"])
        self.assertEqual(view["rows"], [])

    def test_名单读不到时哪怕扫到了目录也不列出来(self):
        """真实形状：`profile_report()` 扫到了目录，只是判不出谁在册。

        `core.maintenance` 是按"不在名单里"判孤儿的 —— 名单为空时它会把**在册
        账号**也标成"孤儿（可删）"。安全线保证一个都不会真删，但把这些错的判定
        画出来只会诱导用户去点「执行」，所以行列表必须是空的。
        """
        report = self._report(
            error="读不到账号名单：TASKS 里没有可用的 unique_id",
            known_ids=[],
        )
        view = viewmodel.maintenance_view(report=report, disk=self._disk())
        self.assertFalse(view["ok"])
        self.assertEqual(view["rows"], [], "名单读不到时不许画逐目录判定")
        self.assertEqual(view["totals"]["count"], 0)
        self.assertEqual(view["totals"]["orphan_bytes"], 0, "孤儿字节数这时没有意义")
        self.assertEqual(view["totals"]["human_orphan"], "0 B")

    def test_名单读得到时_ok_为真且汇总说清构成(self):
        view = viewmodel.maintenance_view(
            report=self._report(), names={"10001": "示例号"}, disk=self._disk()
        )
        self.assertTrue(view["ok"])
        self.assertEqual(view["error"], "")
        self.assertEqual(view["notice"], "")
        self.assertIn("资料目录合计 6.0 MB", view["totals"]["text"])
        self.assertIn("可再生缓存 3.5 MB", view["totals"]["text"])
        self.assertEqual(view["grace_days"], viewmodel.ORPHAN_GRACE_DAYS)
        self.assertEqual(view["rows"][0]["label"], "示例号（10001）")
        self.assertEqual(view["root"], "/srv/spark/profiles")
        self.assertEqual(view["disk"]["level"], "warning")

    def test_资料目录不存在时说清还没登录过(self):
        report = self._report(
            exists=False, profiles=[], total_bytes=0, cache_bytes=0, orphan_bytes=0
        )
        view = viewmodel.maintenance_view(report=report, disk=self._disk())
        self.assertIn("还没有账号扫码登录过", view["notice"])
        self.assertEqual(view["rows"], [])

    def test_真目录走一遍字段名对得上(self):
        """不靠假数据：在临时目录里造一个资料目录，跑真的 `profile_report()`。"""
        root = os.path.join(self._dir, "profiles")
        profile = os.path.join(root, "10001", "Default")
        os.makedirs(os.path.join(profile, "Cache"))
        with open(os.path.join(profile, "Cache", "data_0"), "wb") as handle:
            handle.write(b"x" * 4096)
        with open(os.path.join(profile, "Cookies"), "wb") as handle:
            handle.write(b"c" * 10)

        view = viewmodel.maintenance_view(root=root, ids=["10001"])
        self.assertTrue(view["ok"])
        self.assertEqual(view["totals"]["count"], 1)
        row = view["rows"][0]
        self.assertEqual(row["name"], "10001")
        self.assertEqual(row["tags"], ["在册"])
        self.assertEqual(row["cache_bytes"], 4096)
        self.assertGreaterEqual(row["bytes"], 4106)
        self.assertTrue(view["disk"]["ok"])

    def test_磁盘剩余空间按分区算(self):
        disk = viewmodel.disk_usage_view(self._dir)
        self.assertTrue(disk["ok"])
        self.assertGreater(disk["free_bytes"], 0)
        self.assertTrue(disk["text"].startswith("磁盘剩余"))
        self.assertTrue(disk["tail"].startswith("剩余"), "折叠状态下也要看得见还剩多少")
        self.assertIn(disk["level"], ("ok", "warning", "danger"))
        self.assertLessEqual(disk["used_pct"], 100)

    def test_资料目录还没建也问得到磁盘(self):
        """目录不存在是第一次打开面板的常态：往上找存在的祖先，不许整块报错。"""
        disk = viewmodel.disk_usage_view(os.path.join(self._dir, "还没有这个目录"))
        self.assertTrue(disk["ok"])
        self.assertGreater(disk["free_bytes"], 0)

    def test_整块是_json_可序列化的(self):
        view = viewmodel.maintenance_view(
            report=self._report(), disk=viewmodel.disk_usage_view(self._dir)
        )
        self.assertIn('"rows"', json.dumps(view, ensure_ascii=False))

    def test_预演结果带上释放量与跳过名单(self):
        result = {
            "ok": True,
            "error": "",
            "applied": False,
            "removed": [
                {
                    "profile": "10001",
                    "path": "/srv/spark/profiles/10001/Default/Cache",
                    "bytes": 2048,
                    "human_bytes": "2.0 KB",
                }
            ],
            "freed_bytes": 2048,
            "human_freed": "2.0 KB",
            "skipped_in_use": ["10002"],
            "root": "/srv/spark/profiles",
        }
        view = viewmodel.prune_view(result, what="缓存")
        self.assertTrue(view["ok"])
        self.assertFalse(view["applied"])
        self.assertTrue(view["dry_run"])
        self.assertEqual(view["removed"][0]["label"], "10001/Cache")
        self.assertEqual(view["human_freed"], "2.0 KB")
        self.assertEqual(view["skipped_in_use"], ["10002"])
        self.assertIn("预演", view["headline"])
        # 清缓存的结果里没有 grace_days，前端只认一套键名 → 补默认值。
        self.assertEqual(view["grace_days"], viewmodel.ORPHAN_GRACE_DAYS)
        json.dumps(view, ensure_ascii=False)

    def test_孤儿预演把三类跳过都带出来(self):
        result = {
            "ok": True,
            "error": "",
            "applied": False,
            "removed": [
                {"name": "10009", "path": "/srv/spark/profiles/10009", "bytes": 10, "human_bytes": "10 B"}
            ],
            "freed_bytes": 10,
            "human_freed": "10 B",
            "kept": ["10001"],
            "skipped_recent": ["10008"],
            "skipped_in_use": ["10007"],
            "skipped_symlink": ["link1"],
            "grace_days": 7.0,
            "root": "/srv/spark/profiles",
        }
        view = viewmodel.prune_view(result, what="孤儿目录")
        self.assertEqual(view["removed"][0]["label"], "10009")
        self.assertEqual(view["kept"], ["10001"])
        self.assertEqual(view["skipped_recent"], ["10008"])
        self.assertEqual(view["skipped_in_use"], ["10007"])
        self.assertEqual(view["skipped_symlink"], ["link1"])
        self.assertEqual(view["grace_days"], 7.0)
        self.assertIn("孤儿目录", view["headline"])

    def test_名单读不到时清理结果是失败而不是零(self):
        view = viewmodel.prune_view(
            {"ok": False, "error": "读不到账号名单：TASKS 没配", "applied": False}
        )
        self.assertFalse(view["ok"])
        self.assertIn("没动任何文件", view["headline"])
        self.assertEqual(view["removed"], [])


if __name__ == "__main__":
    unittest.main()
