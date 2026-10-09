"""core.tasks 融合部分的单测：dry-run 透传、限流熔断中止、冷却跳过、运行锁、账本落盘。

本机没有 cloakbrowser（真机上也只在部署环境里），所以先在 `sys.modules` 里塞一个
假模块再 import `core.tasks`；`DouyinIM` 与 `get_browser` 都用假对象替换，
只保留 `tasks` 真正用到的那些方法 —— 这些用例里**不会**启动任何浏览器。
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from datetime import time as dt_time
from unittest import mock

if "cloakbrowser" not in sys.modules:      # core.browser 是模块级 import cloakbrowser
    _fake_cb = types.ModuleType("cloakbrowser")
    _fake_cb.launch = lambda **kw: None
    _fake_cb._fusion_fake = True           # 只清理我们自己塞的那个
    sys.modules["cloakbrowser"] = _fake_cb

from core import limit_guard                       # noqa: E402
from core import msg_builder                       # noqa: E402
from core import run_ledger as ledger              # noqa: E402
from core import tasks as tasks_mod                # noqa: E402
from core.util_time import iso, now                # noqa: E402


def tearDownModule():
    """把自己塞进去的假 `cloakbrowser` 收干净。

    不收的后果很脏：`tests/test_webui.py` 有一条"webui 的导入不该拉起浏览器依赖"
    的用例，断言 `cloakbrowser` 不在 `sys.modules` 里 —— 会因为我这边的残留
    而**无缘无故变红**（跨文件污染）。`core.browser` 早已完成 import，
    这里删掉不影响本模块之后的用例。
    """
    if getattr(sys.modules.get("cloakbrowser"), "_fusion_fake", False):
        sys.modules.pop("cloakbrowser", None)


class _Page:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class _Context:
    def __init__(self):
        self.page = _Page()
        self.cookies = None
        self.closed = False

    def set_default_navigation_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    def new_page(self):
        return self.page

    def add_cookies(self, cookies):
        self.cookies = cookies

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self):
        self.contexts = []
        self.closed = False

    def new_context(self):
        c = _Context()
        self.contexts.append(c)
        return c

    def close(self):
        self.closed = True


def _ok_result(dry_run="off", **over):
    out = {"ok": True, "via": "http+dom", "message_id": "mid-1", "code": 0,
           "status": "ok", "conv_id": "0:1:2", "display": "甲同学",
           "reason": "", "level": "strong", "dry_run": dry_run,
           "rate_limited": False, "limit": None}
    out.update(over)
    return out


class _FakeIM:
    """只实现 core.tasks 用到的方法；判定逻辑本身的测试在 test_douyin_im.py。"""

    instances = []
    ready = {"status": "READY", "user_id": "100", "nickname": "测试号"}
    result = None            # 由各用例设置（None → 用 _ok_result）
    pre_limit = None         # 门禁后 check_limit 的返回值
    raise_on_ready = None

    def __init__(self, page, **kw):
        self.page = page
        self.kw = kw
        self.calls = []
        self.messages = []          # 每次 type_and_send 真正要发出去的字
        self.targets_seen = []
        self.detached = False
        self.last_scan = {"stopped": "bottom", "steps": 3, "visited": 5,
                          "missing": ["缺失的好友"], "select_failed": [],
                          "scanned_all": True, "note": "已到底"}
        _FakeIM.instances.append(self)

    def wait_ready(self):
        if _FakeIM.raise_on_ready:
            raise RuntimeError(_FakeIM.raise_on_ready)
        return dict(_FakeIM.ready)

    def iter_find_and_select(self, targets):
        self.targets_seen = list(targets)
        for t in targets:
            # tasks 现在传下来的是 target_ref() 归一过的条目（可能带 uid/conv_id）
            name = t.get("name") if isinstance(t, dict) else t
            yield {"display": name, "conv_id": "0:1:2", "uid": None,
                   "reselect": lambda: True}

    def type_and_send(self, friend, message, dry_run="off", **kw):
        self.calls.append((friend["display"], dry_run))
        self.messages.append(message)
        if _FakeIM.result is not None:
            r = dict(_FakeIM.result)
            r.setdefault("dry_run", dry_run)
            return r
        return _ok_result(dry_run)

    def check_limit(self, receipts=()):
        return _FakeIM.pre_limit

    def fold_groups(self):
        return {"fold": [], "stranger": []}

    def detach(self):
        self.detached = True


class _TasksCase(unittest.TestCase):
    """所有 `core.tasks` 用例的公共夹具。

    ★ 时间表（`core/schedule.py`）是 2026-10 加进来的，它让"跑一轮"变成了
    "跑**到点了的**账号"。这些用例关心的是"发没发出去"，不是"到没到点"，
    所以这里把时间表钉成"永远到点"：
      * 固定时钟 08:30；
      * 全局默认时间 00:00、抖动 0 → 到点时刻 00:00，早过了；
      * **状态文件每例一份** —— 共用一个的话，第一条用例记上"今天发过了"，
        后面所有用例就全都静静地不发了（症状是"消息数为 0"，
        离真正的原因隔了十万八千里）。
    想测调度本身的用例请继承 `ScheduleGateTests`，它会把这几个值改回去。
    """

    FIXED_NOW = datetime.strptime("2026-10-07 08:30:00", "%Y-%m-%d %H:%M:%S").astimezone()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.ledger_file = os.path.join(self._dir.name, "runs.jsonl")
        self.lock_file = os.path.join(self._dir.name, "run.lock")
        self.rate_file = os.path.join(self._dir.name, "rate_limit.json")
        self.schedule_file = os.path.join(self._dir.name, "schedule_state.json")
        env = {
            "RUN_LEDGER_FILE": self.ledger_file,
            "RUN_LOCK_FILE": self.lock_file,
            "RATE_LIMIT_FILE": self.rate_file,
            "TASK_SCHEDULE_TIMES": "00:00",
            "SCHEDULE_JITTER_MINUTES": "0",
            "SCHEDULE_STATE_FILE": self.schedule_file,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        clock = mock.patch.object(tasks_mod, "now", lambda: self.FIXED_NOW)
        clock.start()
        self.addCleanup(clock.stop)

        _FakeIM.instances = []
        _FakeIM.ready = {"status": "READY", "user_id": "100", "nickname": "测试号"}
        _FakeIM.result = None
        _FakeIM.pre_limit = None
        _FakeIM.raise_on_ready = None
        self.browsers = []

        def _get_browser(fingerprint=None):
            b = _Browser()
            self.browsers.append(b)
            return b

        cfg = mock.patch.dict(tasks_mod.config, {
            "dryRun": "off", "limitGuardEnabled": True,
            "sendIntervalMinSeconds": 0, "sendIntervalMaxSeconds": 0,
            # 抖动读的是 config 快照（不是 os.environ），得一起钉住
            "scheduleJitterMinutes": 0,
        })
        cfg.start()
        self.addCleanup(cfg.stop)

        for name, val in (("DouyinIM", _FakeIM), ("get_browser", _get_browser)):
            p = mock.patch.object(tasks_mod, name, val)
            p.start()
            self.addCleanup(p.stop)

        self.users = [
            {"username": "账号甲", "unique_id": "111", "fingerprint": None,
             "cookies": [{"name": "sessionid", "value": "x", "domain": ".douyin.com",
                          "path": "/"}],
             "targets": ["甲同学", "乙同学"]},
        ]

    def _set_users(self, users):
        p = mock.patch.object(tasks_mod, "userData", users)
        p.start()
        self.addCleanup(p.stop)

    def _runs(self):
        return ledger.read_runs(path=self.ledger_file)


class DryRunPlumbingTests(_TasksCase):
    def test_level_is_passed_down_and_recorded(self):
        self._set_users(self.users)
        rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertEqual(rec["dry_run"], "full")
        self.assertEqual(rec["source"], "test")
        im = _FakeIM.instances[0]
        self.assertEqual([c[1] for c in im.calls], ["full", "full"])
        # 账本里必须留下同一档位，否则仪表盘看到的和实际跑的不是一回事
        on_disk = self._runs()[0]
        self.assertEqual(on_disk["dry_run"], "full")
        self.assertEqual(on_disk["accounts"][0]["entries"][0]["dry_run"], "full")

    def test_env_is_the_fallback(self):
        self._set_users(self.users)
        with mock.patch.dict(os.environ, {"DRY_RUN": "select"}):
            with mock.patch.dict(tasks_mod.config, {"dryRun": "select"}):
                rec = tasks_mod.runTasks(source="test")
        self.assertEqual(rec["dry_run"], "select")
        self.assertEqual([c[1] for c in _FakeIM.instances[0].calls], ["select", "select"])

    def test_cli_value_beats_env(self):
        self._set_users(self.users)
        with mock.patch.dict(tasks_mod.config, {"dryRun": "full"}):
            rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(rec["dry_run"], "off")

    def test_no_extra_sleep_in_dry_run(self):
        """空跑不发送，就不该为"风控节奏"而等待 —— 否则一次空跑要白等几分钟。"""
        self._set_users(self.users)
        with mock.patch.dict(tasks_mod.config,
                             {"sendIntervalMinSeconds": 8, "sendIntervalMaxSeconds": 20}):
            with mock.patch("core.tasks.time.sleep") as sleeper:
                tasks_mod.runTasks(dry_run="full", source="test")
        sleeper.assert_not_called()

    def test_interval_applies_when_really_sending(self):
        self._set_users(self.users)
        with mock.patch.dict(tasks_mod.config,
                             {"sendIntervalMinSeconds": 8, "sendIntervalMaxSeconds": 20}):
            with mock.patch("core.tasks.time.sleep") as sleeper:
                tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(sleeper.call_count, 2)     # 每个目标各一次
        for call in sleeper.call_args_list:
            self.assertGreaterEqual(call.args[0], 8)
            self.assertLessEqual(call.args[0], 20)

    def test_entry_cap_marks_truncation(self):
        self._set_users([dict(self.users[0], targets=["a", "b", "c"])])
        with mock.patch.object(tasks_mod, "ENTRY_CAP", 1):
            rec = tasks_mod.runTasks(dry_run="full", source="test")
        acct = rec["accounts"][0]
        self.assertEqual(len(acct["entries"]), 1)
        self.assertTrue(acct["entries_truncated"])
        self.assertEqual(acct["sent_ok"], 3)        # 计数不受明细上限影响


class AccountMessageTests(_TasksCase):
    """每个账号发不同的话：`TASKS[].message` 必须一路走到 `type_and_send`。

    这条链路有三段（`runTasks` 从 userData 取 → `do_user_task` 收形参 → 逐个好友
    `build_message(message)`），任何一段漏传都**不会报错**，只会静默退回全局模板 ——
    表现是"面板上配了但没生效"，最难查。所以三段各钉一条。
    """

    def _sent(self) -> list:
        return [text for im in _FakeIM.instances for text in im.messages]

    def _patch_global_template(self, template: str) -> None:
        """把"没配内容时该发什么"固定下来，免得读到真实 `.env`。"""
        patcher = mock.patch.object(msg_builder, "get_config",
                                    lambda: {"messageTemplate": template})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_account_message_beats_the_global_template(self):
        self._patch_global_template("全局那一条")
        self._set_users([dict(self.users[0], message="甲账号专属")])
        tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(self._sent(), ["甲账号专属", "甲账号专属"])

    def test_no_message_falls_back_to_the_global_template(self):
        """存量账号（TASKS 里根本没有 message 键）必须照旧发全局模板。"""
        self._patch_global_template("全局那一条")
        self._set_users(self.users)
        tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(self._sent(), ["全局那一条", "全局那一条"])

    def test_blank_message_falls_back_too(self):
        self._patch_global_template("全局那一条")
        self._set_users([dict(self.users[0], message="   ")])
        tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(self._sent(), ["全局那一条", "全局那一条"])

    def test_two_accounts_get_their_own_text(self):
        self._patch_global_template("全局那一条")
        self._set_users([
            dict(self.users[0], message="给甲"),
            {"username": "账号乙", "unique_id": "222", "fingerprint": None,
             "cookies": [{"name": "sessionid", "value": "y", "domain": ".douyin.com",
                          "path": "/"}],
             "targets": ["丙同学"]},
        ])
        tasks_mod.runTasks(dry_run="off", source="test")
        # 甲有两个好友 → 两条"给甲"；乙没配 → 一条全局模板
        self.assertEqual(sorted(self._sent()), sorted(["给甲", "给甲", "全局那一条"]))

    def test_message_is_rendered_per_friend(self):
        """`[API]` 这类占位符是**每个好友各渲染一次** —— 不是渲染一次发给所有人。

        这里用真实的 `build_message`（`request_hitokoto` 换成计数器），
        断言两个好友各拿到一条不同的一言。
        """
        self._patch_global_template("没用到")
        self._set_users([dict(self.users[0], message="[API]")])
        seq = iter(["第一句", "第二句"])
        patcher = mock.patch.object(msg_builder, "request_hitokoto", lambda: next(seq))
        patcher.start()
        self.addCleanup(patcher.stop)
        tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(self._sent(), ["第一句", "第二句"])


class GateAndErrorTests(_TasksCase):
    def test_gate_failure_is_skipped_not_error(self):
        self._set_users(self.users)
        _FakeIM.ready = {"status": "EXPIRED", "user_id": None, "nickname": None}
        rec = tasks_mod.runTasks(dry_run="full", source="test")
        acct = rec["accounts"][0]
        self.assertEqual(acct["status"], "skipped")
        self.assertEqual(acct["gate"], "EXPIRED")
        self.assertEqual(acct["sent_ok"], 0)
        self.assertEqual(_FakeIM.instances[0].calls, [])
        self.assertTrue(_FakeIM.instances[0].detached)   # 跳过也要收尾

    def test_exception_becomes_error_entry_and_other_accounts_still_run(self):
        self._set_users([
            dict(self.users[0], username="坏账号"),
            dict(self.users[0], username="好账号", unique_id="222"),
        ])
        state = {"n": 0}

        class _FlakyIM(_FakeIM):
            def wait_ready(self):
                state["n"] += 1
                if state["n"] == 1:
                    raise RuntimeError("门禁阶段炸了")
                return dict(_FakeIM.ready)

        with mock.patch.object(tasks_mod, "DouyinIM", _FlakyIM):
            rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertEqual([a["status"] for a in rec["accounts"]], ["error", "ok"])
        self.assertIn("门禁阶段炸了", rec["accounts"][0]["error"])
        self.assertEqual(rec["accounts"][1]["sent_ok"], 2)

    def test_targets_are_normalized_and_empties_dropped(self):
        self._set_users([dict(self.users[0], targets=[" Ｌｕ瞳 ", "", "   "])])
        tasks_mod.runTasks(dry_run="full", source="test")
        # 归一化只在 tasks 里做（DouyinIM._match 用同一套 norm），空串必须被丢掉。
        # 名字保持用户写的样子（显示用），归一后的 key 才是匹配用的。
        seen = _FakeIM.instances[0].targets_seen
        self.assertEqual([t["name"] for t in seen], ["Ｌｕ瞳"])
        self.assertEqual(seen[0]["key"], "lu瞳")
        self.assertEqual(seen[0]["uid"], "")
        self.assertEqual(seen[0]["conv_id"], "")

    def test_targets_with_identity_keep_it(self):
        self._set_users([
            dict(self.users[0], targets=[
                {"name": "甲同学", "uid": "u-1", "conv_id": "0:1:2"},
                "乙同学",
            ])
        ])
        tasks_mod.runTasks(dry_run="full", source="test")
        seen = _FakeIM.instances[0].targets_seen
        self.assertEqual([t["name"] for t in seen], ["甲同学", "乙同学"])
        self.assertEqual(seen[0]["uid"], "u-1")
        self.assertEqual(seen[0]["conv_id"], "0:1:2")
        self.assertEqual(seen[0]["key"], "conv:0:1:2")   # 带身份 → 只按 id 匹配
        self.assertEqual(seen[1]["key"], "乙同学")


class LimitGuardIntegrationTests(_TasksCase):
    def test_receipt_limit_aborts_round_and_keeps_other_accounts_out(self):
        self._set_users([
            dict(self.users[0], username="账号甲"),
            dict(self.users[0], username="账号乙", unique_id="222"),
        ])
        _FakeIM.result = _ok_result(
            ok=False, via=None, message_id=None, level="none", reason="receipt_failed",
            rate_limited=True,
            limit={"keyword": "操作频繁", "category": "rate_limit", "source": "http",
                   "text": "code=1001 status='操作频繁'"})
        rec = tasks_mod.runTasks(dry_run="off", source="test")

        self.assertTrue(rec["rate_limited"])
        self.assertEqual(rec["limit"]["keyword"], "操作频繁")
        # 只处理了第一个账号：命中熔断后第二个账号的连接都不该开
        self.assertEqual(len(rec["accounts"]), 1)
        self.assertEqual(len(_FakeIM.instances), 1)

        g = limit_guard.LimitGuard(path=self.rate_file)
        active = g.in_cooldown()
        self.assertIsNotNone(active)
        self.assertEqual(active["keyword"], "操作频繁")
        self.assertGreater(active["remaining_seconds"], 0)
        self.assertFalse(os.path.exists(self.lock_file))   # 熔断也必须释放锁

    def test_pre_send_limit_stops_before_any_message(self):
        self._set_users(self.users)
        _FakeIM.pre_limit = {"keyword": "安全验证", "category": "captcha",
                             "source": "dom", "text": "请完成安全验证"}
        rec = tasks_mod.runTasks(dry_run="off", source="test")
        acct = rec["accounts"][0]
        self.assertEqual(acct["status"], "skipped")
        self.assertTrue(acct["abort_run"])
        self.assertEqual(_FakeIM.instances[0].calls, [])
        self.assertTrue(rec["rate_limited"])

    def test_guard_disabled_keeps_sending(self):
        self._set_users(self.users)
        _FakeIM.result = _ok_result(rate_limited=True,
                                    limit={"keyword": "操作频繁", "source": "dom"})
        with mock.patch.dict(tasks_mod.config, {"limitGuardEnabled": False}):
            rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertFalse(rec["rate_limited"])
        self.assertEqual(_FakeIM.instances[0].calls, [("甲同学", "off"), ("乙同学", "off")])
        self.assertFalse(os.path.exists(self.rate_file))

    def test_cooldown_skips_whole_round_without_opening_a_browser(self):
        self._set_users(self.users)
        limit_guard.LimitGuard(path=self.rate_file, cooldown_seconds=1800).trip(
            {"keyword": "操作频繁", "source": "dom"}, target="甲同学")
        rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(rec["skipped"]["reason"], "cooldown")
        self.assertEqual(rec["accounts"], [])
        self.assertEqual(_FakeIM.instances, [])
        self.assertEqual(self.browsers, [])
        on_disk = self._runs()[0]
        self.assertEqual(on_disk["skipped"]["reason"], "cooldown")

    def test_expired_cooldown_file_is_cleared_and_run_proceeds(self):
        self._set_users(self.users)
        g = limit_guard.LimitGuard(path=self.rate_file, cooldown_seconds=1)
        g.trip({"keyword": "操作频繁"}, at=now().replace(microsecond=0))
        # 手工把截止时间改到过去，模拟"冷却早就过了但文件还留着"
        import json
        with open(self.rate_file, encoding="utf-8") as f:
            rec_json = json.load(f)
        rec_json["cooldown_until"] = iso(now().replace(year=2020))
        with open(self.rate_file, "w", encoding="utf-8") as f:
            json.dump(rec_json, f)
        rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertIsNone(rec["skipped"])
        self.assertEqual(len(rec["accounts"]), 1)
        self.assertFalse(os.path.exists(self.rate_file))

    def test_dry_run_receipt_limit_does_not_persist_cooldown(self):
        """**F2 回归**：空跑一条都没发出去，凭什么写 30 分钟冷却？

        修复前：空跑时 `type_and_send` 的 dry-run 分支照样可能带出 `rate_limited`
        （比如页面上本来就挂着一条风控提示），`core/tasks.py` 直接 `guard.trip()`
        → 落盘冷却。而定时任务每天只跑一轮，撞上这个凭空来的冷却窗口就是**整天不发**。
        空跑只报告、不落盘、不中止本轮。
        """
        self._set_users(self.users)
        _FakeIM.result = _ok_result(
            ok=True, rate_limited=True,
            limit={"keyword": "操作频繁", "category": "rate_limit", "source": "dom",
                   "text": "操作频繁，请稍后再试"})
        rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertFalse(rec["rate_limited"])
        self.assertFalse(os.path.exists(self.rate_file))
        self.assertIsNone(rec["skipped"])
        # 不中止本轮：两个目标都还要走完
        self.assertEqual([c[1] for c in _FakeIM.instances[0].calls], ["full", "full"])
        # 账本里那一轮仍然标成空跑，且没有 limit 记录
        on_disk = self._runs()[0]
        self.assertFalse(on_disk["rate_limited"])
        self.assertEqual(on_disk["dry_run"], "full")

    def test_dry_run_pre_limit_does_not_persist_cooldown(self):
        """**F2 回归（门禁那一侧）**：门禁后扫到风控提示，空跑同样不许写冷却。"""
        self._set_users(self.users)
        _FakeIM.pre_limit = {"keyword": "安全验证", "category": "captcha",
                             "source": "dom", "text": "请完成安全验证"}
        rec = tasks_mod.runTasks(dry_run="select", source="test")
        acct = rec["accounts"][0]
        self.assertEqual(acct["status"], "ok")          # 不判 skipped、不中止
        self.assertFalse(acct.get("abort_run"))
        self.assertFalse(os.path.exists(self.rate_file))
        self.assertEqual([c[1] for c in _FakeIM.instances[0].calls], ["select", "select"])

    def test_real_run_still_trips_and_persists_cooldown(self):
        """反向钉子：真跑（off）时熔断必须照旧落盘 —— 别为了修 F2 把熔断修没了。"""
        self._set_users(self.users)
        _FakeIM.pre_limit = {"keyword": "安全验证", "category": "captcha",
                             "source": "dom", "text": "请完成安全验证"}
        rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(rec["accounts"][0]["status"], "skipped")
        self.assertTrue(os.path.exists(self.rate_file))
        self.assertTrue(limit_guard.LimitGuard(path=self.rate_file).in_cooldown())

    def test_cooldown_seconds_follow_config(self):
        """**F3 回归**：`LIMIT_COOLDOWN_MINUTES` 从前是死配置（永远 30 分钟）。"""
        self._set_users(self.users)
        _FakeIM.result = _ok_result(
            ok=True, rate_limited=True,
            limit={"keyword": "操作频繁", "category": "rate_limit", "source": "http"})
        with mock.patch.dict(tasks_mod.config, {"limitCooldownMinutes": 120}):
            tasks_mod.runTasks(dry_run="off", source="test")
        with open(self.rate_file, encoding="utf-8") as f:
            import json
            saved = json.load(f)
        self.assertEqual(saved["cooldown_seconds"], 7200)


class QuietWindowIntegrationTests(_TasksCase):
    """**F6**：禁跑时间窗内整轮跳过（窗口默认 off，这里是显式设了窗口的情形）。"""

    ACTIVE = {"active": True, "spec": "20:20-21:00", "until": "2026-10-01T21:00:00",
              "override": False, "reason": "当前处于禁跑时间窗 20:20-21:00，至 21:00"}
    IDLE = {"active": False, "spec": "20:20-21:00", "until": "", "override": False, "reason": ""}

    def _patch_window(self, value):
        p = mock.patch.object(tasks_mod.quiet_window, "check", lambda *a, **k: dict(value))
        p.start()
        self.addCleanup(p.stop)

    def test_window_skips_whole_round_without_opening_a_browser(self):
        self._set_users(self.users)
        self._patch_window(self.ACTIVE)
        rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(rec["skipped"]["reason"], "quiet_window")
        self.assertEqual(rec["skipped"]["until"], "2026-10-01T21:00:00")
        self.assertEqual(rec["accounts"], [])
        self.assertEqual(_FakeIM.instances, [])
        self.assertEqual(self.browsers, [])              # 连浏览器都不许开
        self.assertFalse(os.path.exists(self.lock_file))
        on_disk = self._runs()[0]
        self.assertEqual(on_disk["skipped"]["reason"], "quiet_window")

    def test_outside_window_runs_normally(self):
        self._set_users(self.users)
        self._patch_window(self.IDLE)
        rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertIsNone(rec["skipped"])
        self.assertEqual(len(rec["accounts"]), 1)

    def test_cooldown_wins_over_window(self):
        """两者同时命中时先报冷却：冷却是真的被风控盯上了，信息更重要。"""
        self._set_users(self.users)
        self._patch_window(self.ACTIVE)
        limit_guard.LimitGuard(path=self.rate_file, cooldown_seconds=1800).trip(
            {"keyword": "操作频繁", "source": "dom"})
        rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(rec["skipped"]["reason"], "cooldown")


class QuietWindowLedgerTests(_TasksCase):
    """**2026-10-01 真机踩过的坑**：`check()` 的结果里带 `window` 元组
    （两个 `datetime.time`），直接塞进账本会让 `json.dumps` 抛
    `TypeError: Object of type time is not JSON serializable` ——
    任务其实已经跑完了，却因为写账本这一步炸掉，整轮记录全丢。

    `QuietWindowIntegrationTests` 把 `check` 换成了假函数（假数据里没有 `window`），
    所以那几条一条都没拦住；这两条**不 mock**。
    """

    def test_real_check_result_survives_the_ledger(self):
        self._set_users(self.users)
        # 把窗口设成"整天"，保证不管几点跑都落在窗内
        env = mock.patch.dict(os.environ, {"QUIET_WINDOW": "00:00-23:59",
                                           "QUIET_WINDOW_OVERRIDE": ""})
        env.start()
        self.addCleanup(env.stop)

        rec = tasks_mod.runTasks(dry_run="off", source="test")
        self.assertEqual(rec["skipped"]["reason"], "quiet_window")
        self.assertNotIn("window", rec["skipped"]["record"],
                         "window 是 (time, time) 元组，不能进账本")
        json.dumps(rec, ensure_ascii=False)          # 整轮结果必须可序列化

        on_disk = self._runs()[0]
        self.assertEqual(on_disk["skipped"]["reason"], "quiet_window")
        self.assertEqual(on_disk["skipped"]["record"]["spec"], "00:00-23:59")

    def test_ledger_write_survives_an_unserializable_value(self):
        """兜底契约：账本写不进去绝不能反过来让整轮任务失败。"""
        path = os.path.join(self._dir.name, "ledger-with-time.jsonl")
        ok = ledger.append_run({"run_id": "x", "when": dt_time(20, 20)}, path=path)
        self.assertTrue(ok)
        with open(path, encoding="utf-8") as fh:
            line = json.loads(fh.readline())
        self.assertEqual(line["when"], "20:20:00")   # default=str 降级，而不是丢掉整行


class RunLockIntegrationTests(_TasksCase):
    def test_running_lock_skips_round(self):
        self._set_users(self.users)
        held = ledger.RunLock(source="timer", path=self.lock_file)
        held.acquire()
        self.addCleanup(held.release)
        rec = tasks_mod.runTasks(dry_run="off", source="webui")
        self.assertEqual(rec["skipped"]["reason"], "locked")
        self.assertEqual(rec["skipped"]["record"]["source"], "timer")
        self.assertEqual(_FakeIM.instances, [])

    def test_lock_is_released_and_ledger_closed_on_normal_path(self):
        self._set_users(self.users)
        rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertFalse(os.path.exists(self.lock_file))
        self.assertTrue(rec["finished_at"])
        on_disk = self._runs()[0]
        self.assertTrue(on_disk["finished_at"])
        self.assertEqual(on_disk["run_id"], rec["run_id"])

    def test_ledger_never_loses_the_round(self):
        """即使中途抛异常，账本也要留下这一轮（否则仪表盘会永远停在旧数据）。"""
        self._set_users(self.users)

        class _BoomIM(_FakeIM):
            def iter_find_and_select(self, targets):
                raise RuntimeError("扫描炸了")
                yield  # pragma: no cover

        with mock.patch.object(tasks_mod, "DouyinIM", _BoomIM):
            rec = tasks_mod.runTasks(dry_run="full", source="test")
        self.assertEqual(rec["accounts"][0]["status"], "error")
        self.assertTrue(self._runs())
        self.assertFalse(os.path.exists(self.lock_file))


class ScheduleGateTests(_TasksCase):
    """时间表闸门：`runTasks` 现在只发"到点了且今天还没发过"的账号。

    `started = now()` 被基类钉死在 08:30，所以这些用例不依赖"跑测试时几点"。
    时间表的判定逻辑本身在 `tests/test_schedule.py`；这里只测**接线**：
    谁进候选集、谁真被发出去、账本和状态文件各记了什么。
    """

    # 时钟 08:30（继承自基类）：够"08:00 已过、20:30 没到"。

    def setUp(self):
        super().setUp()
        # 基类把默认时间设成了 00:00（"永远到点"），这里换回真实的 20:30，
        # 好让"没到点"这件事真的发生。
        # 全局模板也要钉住：不钉的话 build_message 会用 utils.config 的默认模板，
        # 里面带 [API] —— 断言会依赖环境，还会真的去打一言接口。
        patcher = mock.patch.dict(os.environ, {"TASK_SCHEDULE_TIMES": "20:30"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # ⚠️ 这里**不能**用 `mock.patch.dict(tasks_mod.config, ...)`：
        # `tasks_mod.config` 是 import 那一刻的快照，而 `msg_builder` 每次调用
        # `utils.config.get_config()` 拿的是**当下**那份（`load_env_file` 会把它换掉），
        # 两者不一定是同一个 dict —— 补丁打在一个没人读的对象上，静默失效。
        # 所以照 `AccountMessageTests` 的做法，直接换掉 msg_builder 眼里的 get_config。
        cfg = mock.patch.object(msg_builder, "get_config",
                                lambda: {"messageTemplate": "续火花"})
        cfg.start()
        self.addCleanup(cfg.stop)
        self.early = [{"username": "账号甲", "unique_id": "111", "fingerprint": None,
                       "cookies": [{"name": "sessionid", "value": "x"}],
                       "targets": ["甲同学"], "time": "08:00"}]
        self.late = [{"username": "账号晚", "unique_id": "222", "fingerprint": None,
                      "cookies": [{"name": "sessionid", "value": "x"}],
                      "targets": ["甲同学"], "time": "20:30"}]

    def _state(self):
        with open(self.schedule_file, encoding="utf-8") as handle:
            return json.load(handle)

    def test_due_account_is_sent_and_recorded(self):
        self._set_users(self.early)
        rec = tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertEqual(len(_FakeIM.instances), 1, "到点的账号没被发出去")
        self.assertEqual(_FakeIM.instances[0].messages, ["续火花"])
        record = self._state()["accounts"]["111"]
        self.assertEqual(record["date"], "2026-10-07")
        self.assertEqual(record["time"], "08:00")
        self.assertEqual(record["source"], "timer")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(rec["schedule"]["ran"][0]["unique_id"], "111")

    def test_nothing_due_does_not_run_any_browser(self):
        self._set_users(self.late)
        rec = tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertEqual(_FakeIM.instances, [], "没到点的账号不该被跑")
        self.assertEqual(self.browsers, [], "没到点就不该起浏览器")
        self.assertTrue(rec["nothing_due"])
        self.assertFalse(os.path.exists(self.schedule_file))
        self.assertIsNone(rec["skipped"])

    def test_nothing_due_is_not_written_to_the_ledger(self):
        """★ timer 一天敲 288 次，早退全记进账本会把真正的运行记录淹掉。

        面板的"最近运行"直接读这个文件 —— 一天之后用户就只能看到
        "什么都没干"的记录了。
        """
        self._set_users(self.late)
        for _ in range(5):
            tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertEqual(self._runs(), [], "没到点的那几轮不该进账本")

    def test_already_sent_today_is_skipped(self):
        """★ 手动跑过一轮之后再定时跑，不能再发一次。

        面板的「立即运行」是强制的、并且同样记账；这条如果挂了，
        用户看到的就是"我明明手动发过了，怎么又发了"。
        """
        self._set_users(self.early)
        tasks_mod.runTasks(dry_run="off", source="webui", force=True)
        self.assertEqual(len(_FakeIM.instances), 1)
        _FakeIM.instances = []

        rec = tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertEqual(_FakeIM.instances, [], "今天已经发过的账号又被发了一次")
        self.assertTrue(rec["nothing_due"])
        self.assertEqual(rec["schedule"]["sent"][0]["unique_id"], "111")

    def test_force_ignores_the_schedule(self):
        """「立即运行」= 强制：不受时间表约束，但**照样记账**。"""
        self._set_users(self.late)
        rec = tasks_mod.runTasks(dry_run="off", source="webui", force=True)
        self.assertEqual(len(_FakeIM.instances), 1)
        self.assertFalse(rec.get("nothing_due"))
        self.assertTrue(rec["schedule"]["forced"])
        self.assertEqual(self._state()["accounts"]["222"]["source"], "webui")

    def test_only_narrows_the_candidate_set(self):
        self._set_users(self.early + self.late)
        tasks_mod.runTasks(dry_run="off", source="timer", only="222")
        self.assertEqual(_FakeIM.instances, [], "只点了没到点的那个，却发了别的")
        self.assertEqual(self._runs(), [])

    def test_only_matching_username_works_too(self):
        """`--only` 认抖音号也认昵称 —— 面板下拉里显示的是昵称，用户会照着敲。"""
        self._set_users(self.early + self.late)
        tasks_mod.runTasks(dry_run="off", source="cli", only="账号甲")
        self.assertEqual(len(_FakeIM.instances), 1, "按昵称没匹配上")

    def test_only_an_unknown_account_is_reported(self):
        self._set_users(self.early)
        rec = tasks_mod.runTasks(dry_run="off", source="cli", only="查无此人")
        self.assertEqual(rec["skipped"]["reason"], "no_such_account")
        self.assertEqual(_FakeIM.instances, [])
        # cli 是"人主动发起的"，这种跳过必须留痕
        self.assertEqual(self._runs()[0]["skipped"]["reason"], "no_such_account")

    def test_a_crashing_account_is_still_recorded_as_done_for_today(self):
        """★ 坏 cookie 的账号必须当天只被踹一次。

        否则每 5 分钟一次的门铃会让它一天开 288 次浏览器，把整台机器拖垮 ——
        "失败也要记账"是这条设计的全部意义。
        """
        self._set_users(self.early)
        _FakeIM.raise_on_ready = "登录态失效"

        rec = tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertEqual(rec["accounts"][0]["status"], "error")
        self.assertEqual(self._state()["accounts"]["111"]["status"], "error")

        _FakeIM.instances = []
        again = tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertEqual(_FakeIM.instances, [], "失败的账号被重复踹起来了")
        self.assertTrue(again["nothing_due"])

    def test_global_default_is_used_when_the_account_has_no_time(self):
        self._set_users([{"username": "没配时间", "unique_id": "333", "fingerprint": None,
                          "cookies": [{"name": "sessionid", "value": "x"}],
                          "targets": ["甲同学"]}])
        # TASK_SCHEDULE_TIMES=20:30，现在 08:30 → 没到点
        rec = tasks_mod.runTasks(dry_run="off", source="timer")
        self.assertTrue(rec["nothing_due"])
        self.assertEqual(rec["schedule"]["fallback"], "20:30")
        self.assertEqual(rec["schedule"]["waiting"][0]["time"], "20:30")

    def test_ledger_brief_carries_no_cookie_material(self):
        """账本是长期保留的 jsonl —— 里面塞进 cookies 就麻烦了。"""
        self._set_users(self.early)
        tasks_mod.runTasks(dry_run="off", source="timer")
        brief = json.dumps(self._runs()[0].get("schedule") or {}, ensure_ascii=False)
        self.assertNotIn("sessionid", brief)
        self.assertNotIn("cookies", brief)


if __name__ == "__main__":
    unittest.main()
