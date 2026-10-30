# -*- coding: utf-8 -*-
"""tests/test_doctor.py —— 体检（issue #16）的回归测试。

重点是那三种"最该报警、却不会有任何一轮跑完"的情形：定时器停了、账本被删了、
已经很久没有任何运行记录。以及两条**不许**报警的反面：账号全停用（那是用户自己
关的）、读不到 systemd 状态（不知道 ≠ 没启用）。
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from core import doctor

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=CST)

ACTIVE_TIMER = {
    "unit": doctor.TIMER_UNIT, "known": True, "load": "loaded",
    "active": "active", "enabled": "enabled",
    "last": "Fri 2026-10-24 07:00:00 CST", "next": "", "error": "",
}


def _row(minutes_ago, run_id="20261024-070000"):
    at = NOW - timedelta(minutes=minutes_ago)
    return {
        "run_id": run_id,
        "started_at": at.isoformat(),
        "finished_at": at.isoformat(),
        "accounts": [{"unique_id": "111", "status": "ok"}],
    }


def _users(*flags):
    return [{"unique_id": str(100 + index), "enabled": flag}
            for index, flag in enumerate(flags)]


class LedgerReadTests(unittest.TestCase):
    def test_missing_ledger_says_so_instead_of_pretending_it_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = doctor.read_ledger_rows(os.path.join(tmp, "runs.jsonl"))
        self.assertFalse(info["exists"])
        self.assertEqual(info["rows"], [])
        self.assertIn("FileNotFoundError", info["error"])

    def test_tail_read_drops_the_partial_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.jsonl")
            body = "".join(
                json.dumps({"run_id": "r%d" % index, "finished_at": "2026-10-24T0%d:00:00+08:00" % (index + 1)}) + "\n"
                for index in range(6)
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            # 只读最后一条记录 + 10 字节：切点落在上一条的中间，那半截必须被丢掉
            tail = len(body.rstrip("\n").split("\n")[-1]) + 10
            info = doctor.read_ledger_rows(path, tail_bytes=tail)
        self.assertTrue(info["exists"])
        self.assertEqual(info["bad_lines"], 0)
        self.assertTrue(info["rows"])
        self.assertEqual(info["rows"][-1]["run_id"], "r5")

    def test_bad_json_lines_are_counted_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"run_id": "ok"}\n')
                handle.write("{坏行\n")
                handle.write("[1, 2, 3]\n")
            info = doctor.read_ledger_rows(path)
        self.assertEqual(len(info["rows"]), 1)
        self.assertEqual(info["bad_lines"], 2)

    def test_limit_keeps_the_newest_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "runs.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for index in range(5):
                    handle.write(json.dumps({"run_id": "r%d" % index}) + "\n")
            info = doctor.read_ledger_rows(path, limit=2)
        self.assertEqual([row["run_id"] for row in info["rows"]], ["r3", "r4"])


class LastRunTests(unittest.TestCase):
    def test_picks_the_newest_by_timestamp_not_by_file_order(self):
        rows = [{"run_id": "new", "finished_at": "2026-10-24T09:00:00+08:00"},
                {"run_id": "old", "finished_at": "2026-10-23T09:00:00+08:00"}]
        self.assertEqual(doctor.last_run(rows)["row"]["run_id"], "new")

    def test_rows_without_any_timestamp_are_skipped(self):
        rows = [{"run_id": "x"}, {"run_id": "y", "started_at": "不是时间"}]
        self.assertIsNone(doctor.last_run(rows))

    def test_started_at_is_the_fallback_when_finished_at_is_missing(self):
        rows = [{"run_id": "x", "started_at": "2026-10-24T08:00:00+08:00"}]
        self.assertEqual(doctor.last_run(rows)["at"].hour, 8)


class TimerStateTests(unittest.TestCase):
    def test_parses_the_properties_it_asks_for(self):
        seen = {}

        def runner(unit, props):
            seen["unit"] = unit
            seen["props"] = list(props)
            return (
                "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                "LastTriggerUSec=Fri 2026-10-24 07:00:00 CST\n"
                "NextElapseUSecRealtime=Sat 2026-10-25 07:00:00 CST\n"
            )

        info = doctor.timer_state(runner=runner)
        self.assertTrue(info["known"])
        self.assertEqual(info["active"], "active")
        self.assertEqual(info["enabled"], "enabled")
        self.assertEqual(seen["unit"], doctor.TIMER_UNIT)
        self.assertIn("ActiveState", seen["props"])

    def test_not_found_unit_is_unknown_not_disabled(self):
        info = doctor.timer_state(runner=lambda unit, props: "LoadState=not-found\n")
        self.assertFalse(info["known"])
        self.assertIn("没有这个单元", info["error"])

    def test_missing_systemctl_is_unknown(self):
        info = doctor.timer_state(runner=lambda unit, props: None)
        self.assertFalse(info["known"])
        self.assertIn("systemctl", info["error"])


class AccountCountTests(unittest.TestCase):
    def test_counts_stopped_accounts(self):
        info = doctor.account_counts(_users(True, False, "0"))
        self.assertEqual(info, {"total": 3, "active": 1, "stopped": 2, "error": ""})

    def test_broken_config_is_reported_not_swallowed(self):
        with mock.patch("utils.config.get_userData", side_effect=RuntimeError("坏配置")):
            info = doctor.account_counts(None)
        self.assertEqual(info["total"], 0)
        self.assertIn("坏配置", info["error"])


ACTIVE_WEB = {
    "unit": doctor.WEB_UNIT, "known": True, "load": "loaded",
    "active": "active", "enabled": "enabled",
    "started": "Fri 2026-10-24 00:08:59 CST", "restarts": "0", "error": "",
}


def _acct_row(minutes_ago, uid="100", status="error", error="", run_id=""):
    """一条只关心结论的记录：某个账号这一轮是 ok 还是 error。"""
    row = _row(minutes_ago, run_id=run_id or "r%d" % minutes_ago)
    row["accounts"] = [{"unique_id": uid, "status": status, "error": error}]
    return row


class ServiceStateTests(unittest.TestCase):
    def test_parses_the_properties_it_asks_for(self):
        seen = {}

        def runner(unit, props):
            seen["unit"] = unit
            seen["props"] = list(props)
            return (
                "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                "ExecMainStartTimestamp=Fri 2026-10-24 00:08:59 CST\nNRestarts=0\n"
            )

        info = doctor.service_state(runner=runner)
        self.assertTrue(info["known"])
        self.assertEqual(info["active"], "active")
        self.assertEqual(info["enabled"], "enabled")
        self.assertEqual(info["restarts"], "0")
        self.assertEqual(seen["unit"], doctor.WEB_UNIT)
        self.assertIn("ActiveState", seen["props"])

    def test_not_found_unit_is_unknown_not_dead(self):
        info = doctor.service_state(runner=lambda unit, props: "LoadState=not-found\n")
        self.assertFalse(info["known"])
        self.assertIn("没有这个单元", info["error"])

    def test_missing_systemctl_is_unknown(self):
        info = doctor.service_state(runner=lambda unit, props: None)
        self.assertFalse(info["known"])
        self.assertIn("systemctl", info["error"])


class WebCheckTests(unittest.TestCase):
    def _report(self, **kwargs):
        base = dict(now_dt=NOW, rows=[_row(10)], users=_users(True),
                    timer=ACTIVE_TIMER, web=ACTIVE_WEB)
        base.update(kwargs)
        return doctor.diagnose(**base)

    def test_a_dead_panel_is_an_error_even_though_the_timer_is_fine(self):
        dead = dict(ACTIVE_WEB, active="failed", restarts="3")
        report = self._report(web=dead)
        self.assertEqual(report["level"], "error")
        self.assertIn("web_down", report["problems"])
        self.assertTrue(any("面板服务" in line and "用不了" in line
                            for line in report["lines"]))

    def test_an_unknown_panel_state_is_not_a_problem(self):
        unknown = {"unit": doctor.WEB_UNIT, "known": False, "error": "systemctl 不可用"}
        report = self._report(web=unknown)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["level"], "ok")
        self.assertTrue(any("读不到状态" in line for line in report["lines"]))

    def test_enabled_but_will_not_come_back_is_a_warning(self):
        off = dict(ACTIVE_WEB, enabled="disabled")
        report = self._report(web=off)
        self.assertIn("web_disabled", report["problems"])
        self.assertEqual(report["level"], "warning")

    def test_without_the_injection_the_check_is_simply_absent(self):
        report = self._report(web=None)
        self.assertFalse(any("面板服务" in line for line in report["lines"]))
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["web"], {})


class AccountOutcomeTests(unittest.TestCase):
    USERS = [{"unique_id": "100", "username": "wheat", "enabled": True}]

    def _report(self, rows, users=None):
        return doctor.diagnose(now_dt=NOW, rows=rows,
                               users=self.USERS if users is None else users,
                               timer=ACTIVE_TIMER, web=ACTIVE_WEB)

    def test_a_single_bad_run_is_a_warning_that_names_the_account(self):
        report = self._report([_acct_row(10, status="error", error="login_dead: 登录已失效")])
        self.assertIn("account_failing", report["problems"])
        self.assertEqual(report["level"], "warning")
        body = "\n".join(report["lines"])
        self.assertIn("wheat（100）", body)
        self.assertIn("login_dead", body)

    def test_three_bad_runs_in_a_row_is_an_error(self):
        rows = [_acct_row(10, status="error"), _acct_row(20, status="error"),
                _acct_row(30, status="error")]
        report = self._report(rows)
        self.assertIn("account_dead", report["problems"])
        self.assertEqual(report["level"], "error")
        self.assertIn("最近 3 轮一条都没发出去", "\n".join(report["lines"]))

    def test_an_ok_run_resets_the_streak(self):
        rows = [_acct_row(10, status="error"), _acct_row(20, status="ok"),
                _acct_row(30, status="error"), _acct_row(40, status="error")]
        report = self._report(rows)
        self.assertNotIn("account_dead", report["problems"])
        self.assertIn("account_failing", report["problems"])

    def test_a_healthy_account_says_nothing_at_all(self):
        report = self._report([_acct_row(10, status="ok")])
        self.assertEqual(report["problems"], [])
        self.assertFalse(any("账号 wheat" in line for line in report["lines"]))

    def test_a_stopped_account_is_never_reported(self):
        users = [{"unique_id": "100", "username": "wheat", "enabled": False}]
        rows = [_acct_row(10, status="error"), _acct_row(20, status="error"),
                _acct_row(30, status="error")]
        report = self._report(rows, users=users)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["level"], "ok")

    def test_an_account_outside_the_config_is_ignored(self):
        report = self._report([_acct_row(10, uid="999", status="error")])
        self.assertEqual(report["problems"], [])
        self.assertEqual([item["unique_id"] for item in report["outcomes"]["accounts"]],
                         ["100"])
        self.assertFalse(report["outcomes"]["accounts"][0]["seen"])


class DiagnoseTests(unittest.TestCase):
    def _report(self, **kwargs):
        base = dict(now_dt=NOW, rows=[_row(120)], users=_users(True), timer=ACTIVE_TIMER)
        base.update(kwargs)
        return doctor.diagnose(**base)

    def test_a_fresh_ledger_and_a_running_timer_is_all_green(self):
        report = self._report()
        self.assertTrue(report["ok"])
        self.assertEqual(report["level"], "ok")
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["last_run_text"], "10-24 10:00")
        self.assertAlmostEqual(report["silent_hours"], 2.0, places=2)

    def test_silence_past_the_threshold_is_an_error(self):
        report = self._report(rows=[_row(31 * 60)])
        self.assertIn("silent", report["problems"])
        self.assertEqual(report["level"], "error")
        self.assertFalse(report["ok"])
        self.assertTrue(any("没有任何运行记录" in line for line in report["lines"]))

    def test_all_accounts_stopped_suppresses_the_silence_alarm(self):
        report = self._report(rows=[_row(40 * 60)], users=_users(False, False))
        self.assertNotIn("silent", report["problems"])
        self.assertTrue(report["ok"])
        self.assertTrue(any("全部已停用" in line for line in report["lines"]))

    def test_threshold_zero_turns_the_judgement_off(self):
        report = self._report(rows=[_row(100 * 60)], threshold=0.0)
        self.assertNotIn("silent", report["problems"])
        self.assertTrue(any("静默判定已关闭" in line for line in report["lines"]))

    def test_missing_ledger_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._report(rows=None, ledger_path=os.path.join(tmp, "runs.jsonl"))
        self.assertIn("no_ledger", report["problems"])
        self.assertEqual(report["level"], "error")

    def test_ledger_without_any_timestamp_is_an_error(self):
        report = self._report(rows=[{"run_id": "x"}])
        self.assertIn("no_run", report["problems"])
        self.assertFalse(report["ok"])

    def test_inactive_timer_is_an_error(self):
        broken = dict(ACTIVE_TIMER, active="inactive")
        report = self._report(timer=broken)
        self.assertIn("timer_inactive", report["problems"])
        self.assertEqual(report["level"], "error")

    def test_a_timer_that_will_not_survive_a_reboot_is_only_a_warning(self):
        broken = dict(ACTIVE_TIMER, enabled="disabled")
        report = self._report(timer=broken)
        self.assertIn("timer_disabled", report["problems"])
        self.assertEqual(report["level"], "warning")
        self.assertFalse(report["ok"])

    def test_unknown_timer_state_does_not_raise_a_false_alarm(self):
        report = self._report(timer={"unit": doctor.TIMER_UNIT, "known": False,
                                     "error": "systemctl 不可用（不是 systemd 部署？）"})
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ok"])
        self.assertTrue(any("读不到状态" in line for line in report["lines"]))

    def test_report_carries_the_fields_the_panel_and_json_need(self):
        report = self._report()
        for key in ("ok", "level", "headline", "problems", "lines", "silent_hours",
                    "threshold_hours", "last_run_at", "last_run_text", "ledger",
                    "timer", "accounts", "checked_at"):
            self.assertIn(key, report)
        self.assertEqual(report["ledger"]["rows"], 1)
        self.assertEqual(report["accounts"]["active"], 1)

    def test_format_lines_never_returns_an_empty_list(self):
        self.assertEqual(doctor.format_lines({"lines": ["x"]}), ["x"])
        self.assertEqual(len(doctor.format_lines({"ok": True})), 1)


class MaybeNotifyTests(unittest.TestCase):
    def _bad_report(self):
        return {"ok": False, "level": "error", "headline": "体检没过：x",
                "problems": ["silent"], "lines": ["⛔ x"], "checked_at": "2026-10-24T12:00:00+08:00"}

    CONFIG = {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
              "level": "error", "timeout": 10.0}

    def test_a_clean_report_never_pushes(self):
        result = doctor.maybe_notify({"ok": True, "level": "ok"})
        self.assertEqual(result["reason"], "nothing-to-report")

    def test_without_a_webhook_it_says_so(self):
        result = doctor.maybe_notify(self._bad_report(), config_override={"url": "", "level": "error", "timeout": 10})
        self.assertEqual(result["reason"], "no-webhook")

    def test_notify_off_is_respected(self):
        cfg = dict(self.CONFIG, level="off")
        result = doctor.maybe_notify(self._bad_report(), config_override=cfg)
        self.assertEqual(result["reason"], "notify-off")

    def test_pushes_once_and_then_stays_quiet_for_the_rest_of_the_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "doctor_state.json")
            with mock.patch("core.notify.build_payload", return_value={}) as build:
                with mock.patch("core.notify.post", return_value=(True, "ok")) as post:
                    first = doctor.maybe_notify(self._bad_report(), now_dt=NOW,
                                                config_override=self.CONFIG, state_path=state)
                    second = doctor.maybe_notify(self._bad_report(), now_dt=NOW,
                                                 config_override=self.CONFIG, state_path=state)
            self.assertTrue(first["sent"])
            self.assertEqual(post.call_count, 1)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(second["reason"], "already-today")
            with open(state, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["date"], "2026-10-24")

    def test_a_failed_push_is_reported_and_not_marked_as_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "doctor_state.json")
            with mock.patch("core.notify.build_payload", return_value={}):
                with mock.patch("core.notify.post", return_value=(False, "HTTP 500")):
                    result = doctor.maybe_notify(self._bad_report(), now_dt=NOW,
                                                 config_override=self.CONFIG, state_path=state)
            self.assertFalse(result["sent"])
            self.assertEqual(result["reason"], "failed")
            self.assertFalse(os.path.exists(state))

    def test_a_raising_post_never_breaks_the_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("core.notify.build_payload", side_effect=RuntimeError("网络坏了")):
                result = doctor.maybe_notify(self._bad_report(), now_dt=NOW,
                                             config_override=self.CONFIG,
                                             state_path=os.path.join(tmp, "s.json"))
        self.assertFalse(result["sent"])
        self.assertIn("网络坏了", result["detail"])


class MainTests(unittest.TestCase):
    REPORT = {"ok": False, "level": "error", "headline": "体检没过：x", "problems": ["silent"],
              "lines": ["⛔ 已经 31 小时没有任何运行记录"], "checked_at": "2026-10-24T12:00:00+08:00"}

    def test_exit_code_is_one_when_something_is_wrong(self):
        out = io.StringIO()
        with mock.patch.object(doctor, "diagnose", return_value=self.REPORT):
            with mock.patch.object(doctor, "maybe_notify", return_value={"sent": True, "detail": "ok"}):
                with redirect_stdout(out):
                    code = doctor.main([])
        self.assertEqual(code, 1)
        self.assertIn("31 小时", out.getvalue())
        self.assertIn("已推送", out.getvalue())

    def test_exit_code_is_zero_when_everything_is_fine(self):
        out = io.StringIO()
        with mock.patch.object(doctor, "diagnose",
                               return_value={"ok": True, "level": "ok", "lines": ["✅ 一切正常"],
                                             "headline": "一切正常", "problems": []}):
            with redirect_stdout(out):
                code = doctor.main([])
        self.assertEqual(code, 0)
        self.assertIn("一切正常", out.getvalue())

    def test_json_mode_prints_machine_readable_output(self):
        out = io.StringIO()
        with mock.patch.object(doctor, "diagnose", return_value=self.REPORT):
            with mock.patch.object(doctor, "maybe_notify", return_value={"sent": False}):
                with redirect_stdout(out):
                    doctor.main(["--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["problems"], ["silent"])

    def test_no_notify_skips_the_push_entirely(self):
        with mock.patch.object(doctor, "diagnose", return_value=self.REPORT):
            with mock.patch.object(doctor, "maybe_notify", side_effect=AssertionError("不该推")):
                with redirect_stdout(io.StringIO()):
                    code = doctor.main(["--no-notify"])
        self.assertEqual(code, 1)

    def test_unknown_switch_exits_two_without_running_anything(self):
        with mock.patch.object(doctor, "diagnose", side_effect=AssertionError("不该体检")):
            with redirect_stdout(io.StringIO()):
                code = doctor.main(["--nope"])
        self.assertEqual(code, 2)

    def test_help_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = doctor.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("用法", out.getvalue())


class MainCliWiringTests(unittest.TestCase):
    """main.py 那边：doctor 要能进来，它自己的开关不能被当成"认不出来的开关"。"""

    def test_doctor_switches_survive_the_flag_parser(self):
        import main as cli

        force, only, rest, error = cli._parse_flags(["doctor", "--json", "--no-notify"])
        self.assertEqual(error, "")
        self.assertEqual(rest, ["doctor"])
        self.assertFalse(force)
        self.assertEqual(only, "")

    def test_doctor_helper_drops_the_mode_word_before_parsing(self):
        import main as cli

        seen = {}

        def fake_main(argv=None):
            seen["argv"] = list(argv or [])
            return 0

        with mock.patch("core.doctor.main", fake_main):
            code = cli._doctor(["--json", "doctor", "--no-notify"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"], ["--json", "--no-notify"])


class DeployArtifactTests(unittest.TestCase):
    """体检得有东西去喊它：unit、timer、安装脚本、示例配置四件都要在。"""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_doctor_unit_runs_the_doctor_subcommand(self):
        body = self._read("deploy", "douyin-spark-fusion-doctor.service")
        self.assertIn("main.py doctor", body)
        # 注释里可以提它（还要解释为什么不写），但**指令本身**一行都不能有：
        # systemd 会按转义序列解析值，把消息模板里的字面 \n 吃成裸 n。
        for line in body.splitlines():
            self.assertFalse(line.strip().startswith("EnvironmentFile="), line)

    def test_doctor_timer_fires_once_a_day(self):
        body = self._read("deploy", "douyin-spark-fusion-doctor.timer")
        self.assertIn("OnCalendar=*-*-* 09:00", body)
        self.assertIn("Unit=douyin-spark-fusion-doctor.service", body)

    def test_install_script_installs_the_doctor_units(self):
        body = self._read("deploy", "install.sh")
        self.assertIn("douyin-spark-fusion-doctor.service", body)
        self.assertIn("douyin-spark-fusion-doctor.timer", body)

    def test_env_example_documents_the_threshold(self):
        self.assertIn("NOTIFY_SILENCE_HOURS=30", self._read(".env.example"))


if __name__ == "__main__":
    unittest.main()
