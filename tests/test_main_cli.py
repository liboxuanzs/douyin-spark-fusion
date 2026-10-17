"""`main.py` 的纯函数部分 —— CLI 入口的状态文案与来源解析。

**刻意不调用 `main()`**：那会真的去开浏览器。这里只测两件最容易说错话的事：

* "一条消息都没有真的发出去"这句提示，只能在**账本里最终生效的档位**不是
  `off` 时打印。`main.py task` 传的是 `None`（= 听 `.env` 的），
  拿入参去比就永远不相等，真发的那一轮也会在日志里留下"空跑"字样 ——
  而面板的日志页正好显示这行。
* 触发来源不能写死在 `.env` 里，否则面板就分不清谁跑的了。
"""
import os
import time
import unittest
from unittest import mock

import main


class DryRunNoticeTests(unittest.TestCase):
    def test_real_send_prints_nothing(self):
        self.assertEqual(main.dry_run_notice({"dry_run": "off"}), "")

    def test_none_level_is_treated_as_off(self):
        # 关键回归：runTasks 入参 None → 账本里是 off → 不许说"空跑"
        self.assertEqual(main.dry_run_notice({"dry_run": None}), "")
        self.assertEqual(main.dry_run_notice({}), "")
        self.assertEqual(main.dry_run_notice(None), "")

    def test_dry_run_levels_are_reported(self):
        for level in ("select", "full"):
            text = main.dry_run_notice({"dry_run": level})
            self.assertIn(level, text)
            self.assertIn("一条消息都没有真的发出去", text)


class SourceResolutionTests(unittest.TestCase):
    def test_cli_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            from core.run_ledger import run_source

            self.assertEqual(run_source(), "cli")

    def test_timer_unit_marks_its_runs(self):
        with mock.patch.dict(os.environ, {"RUN_SOURCE": "timer"}):
            from core.run_ledger import run_source

            self.assertEqual(run_source(), "timer")


class EnvFilePrecedenceTests(unittest.TestCase):
    """`.env` 是配置的唯一真相：进程环境里的同名旧值必须被它盖掉。

    真实故障（2026-10-02 20:38 那一轮）：unit 里写了 systemd 的
    `EnvironmentFile=/opt/douyin-spark-fusion/.env`，它按**转义序列**解析值，
    把消息模板里的字面 `\\n`（规则："用 \\n 表示换行"）吃成裸 `n`；
    而当年的 `main.py` 用 `load_dotenv(".env")`（`override=False`），
    被弄坏的进程环境反而赢了 —— 面板预览（读文件）显示三行，
    实际发出去是一整行、行尾还挂着 n。用户的原话就是"换行规则部分失效"。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from utils import config as utils_config

        self.config_mod = utils_config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        # 注意 `\\n`：文件里是**字面**两个字符（反斜杠 + n），不是真换行
        self.env.write_text(
            "MESSAGE_TEMPLATE=甲\\n乙\nLOG_LEVEL=Info\nDRY_RUN=off\n",
            encoding="utf-8",
        )

    def test_file_wins_over_a_mangled_process_value(self):
        """★ 核心回归：进程环境里那份"被解析坏的"值不许赢。"""
        with mock.patch.dict(os.environ, {"MESSAGE_TEMPLATE": "甲n乙"}, clear=False):
            drift = self.config_mod.load_env_file(str(self.env))
            self.assertEqual(os.environ["MESSAGE_TEMPLATE"], "甲\\n乙")
        self.assertEqual(drift, {"MESSAGE_TEMPLATE": ("甲n乙", "甲\\n乙")})

    def test_no_drift_when_values_agree(self):
        with mock.patch.dict(os.environ, {"MESSAGE_TEMPLATE": "甲\\n乙"}, clear=False):
            self.assertEqual(self.config_mod.load_env_file(str(self.env)), {})

    def test_run_controls_stay_with_the_launcher(self):
        """`DRY_RUN` / `RUN_MODE` / `RUN_SOURCE` 是"这一轮怎么跑"，启动方说了算。"""
        preset = {"DRY_RUN": "select", "RUN_MODE": "task", "RUN_SOURCE": "timer"}
        with mock.patch.dict(os.environ, preset, clear=False):
            drift = self.config_mod.load_env_file(str(self.env))
            for key, value in preset.items():
                self.assertEqual(os.environ[key], value, key)
        self.assertNotIn("DRY_RUN", drift)

    def test_missing_file_reports_not_read(self):
        self.assertIsNone(self.config_mod.load_env_file(str(self.env) + ".nope"))
        self.assertFalse(self.config_mod.reload_env(str(self.env) + ".nope"))

    # ---- drift 日志不许把凭据抄进日志文件 ----
    def test_drift_report_never_logs_credentials(self):
        """`.env` 里的 `COOKIES_<抖音号>` 是一整份登录态，不能进 logs/app.log。

        以前 main.py 直接把两侧原始值打进 WARNING —— 每启动一次就抄一份完整 cookie。
        """
        drift = {"COOKIES_11223344556": ("secret-old-value", "secret-new-value"),
                 "MESSAGE_TEMPLATE": ("甲n乙", "甲\\n乙")}
        text = self.config_mod.drift_report(drift)
        self.assertNotIn("secret-old-value", text)
        self.assertNotIn("secret-new-value", text)
        self.assertIn("COOKIES_11223344556", text, "是哪个键还是要报出来")
        self.assertIn("隐藏", text)
        self.assertIn("甲n乙", text, "非凭据键照旧报值 —— 模板被吃反斜杠就靠它")
        self.assertIn(repr("甲\\n乙")[1:-1], text)

    def test_drift_report_shows_lengths_and_fingerprints_for_secrets(self):
        text = self.config_mod.drift_report({"WEBUI_PASSWORD_HASH": ("x" * 10, "y" * 20)})
        self.assertIn("10 字符", text)
        self.assertIn("20 字符", text)
        self.assertIn("sha256:", text, "只报指纹，反推不出原文")

    def test_drift_report_handles_nothing(self):
        self.assertEqual(self.config_mod.drift_report({}), "")
        self.assertEqual(self.config_mod.drift_report(None), "")

    # `time.tzset` 是 POSIX 专有（Windows 上连属性都没有，生产代码在那条分支上
    # 也压根不调它，见 utils/config.py::_apply_tz）—— 没有 tzset 就没有可断言的时机，
    # 硬跑只会让 `mock.patch.object(..., "tzset")` 抛 AttributeError（issue #3 机制二）。
    @unittest.skipUnless(hasattr(time, "tzset"),
                         "本机 time 模块没有 tzset（Windows）：这条断言的是 POSIX 上的落地")
    def test_loading_the_env_applies_tz(self):
        """`.env` 里的 TZ 必须让 libc 立刻认账。

        只写进 os.environ 是不够的：时区是进程里的一份缓存，不调 tzset() 的话调度
        与日志仍按宿主时区算，而面板按 WEBUI_TZ 显示 —— 两边差几个小时还查不出来。
        """
        with mock.patch.dict(os.environ, {"TZ": "Asia/Shanghai"}, clear=False):
            with mock.patch.object(self.config_mod.time, "tzset") as fake:
                self.config_mod.load_env_file(str(self.env))
        self.assertTrue(fake.called, "读完 .env 必须调一次 tzset()")

    def test_reload_env_still_reports_true_on_success(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertTrue(self.config_mod.reload_env(str(self.env)))
            self.assertEqual(os.environ["MESSAGE_TEMPLATE"], "甲\\n乙")

    def test_webui_entry_uses_the_same_helper(self):
        """webui 入口也得按同一套口径读（否则又是"面板一套、实际一套"）。"""
        from webui import __main__ as webui_main

        with mock.patch.dict(os.environ, {"MESSAGE_TEMPLATE": "甲n乙"}, clear=False):
            webui_main.load_env(str(self.env))
            self.assertEqual(os.environ["MESSAGE_TEMPLATE"], "甲\\n乙")



class FlagParsingTests(unittest.TestCase):
    """`--only` / `--force` / 认不出来的开关。

    这里曾经有个"通吃型"分支（`startswith("-")` → 报错）排在 `--only` **前面**，
    于是 `--only`、`-o`、`--only=x` 全被判成"认不出来的开关"，而报错信息里给出的
    示例偏偏就是 `python main.py task --only 11223344556` —— 2026-10-09 想只跑一个账号
    时撞上的就是这个。位置参数的"静默忽略"更隐蔽：`--only x` 被吞掉后 `x` 会留在
    rest 里当子命令，`task x` 照样跑全部账号。
    """

    def test_only_with_a_value(self):
        force, only, rest, error = main._parse_flags(["task", "--only", "11223344556"])
        self.assertEqual((force, only, rest, error), (False, "11223344556", ["task"], ""))

    def test_only_alias_and_equals_form(self):
        self.assertEqual(main._parse_flags(["task", "-o", "示例号"])[1], "示例号")
        self.assertEqual(main._parse_flags(["task", "--only=示例号"])[1], "示例号")

    def test_only_without_a_value_is_an_error(self):
        _, _, _, error = main._parse_flags(["task", "--only"])
        self.assertIn("--only", error)

    def test_force_and_force_all_are_distinguishable(self):
        self.assertEqual(main._parse_flags(["task", "--force"])[0], True)
        self.assertEqual(main._parse_flags(["task", "--force-all"])[0], "all")

    def test_unknown_switch_is_still_rejected(self):
        """修 `--only` 不能把"认不出来的开关"这条防线一起修没。"""
        for bad in ("--froce", "--onlyy", "-x"):
            _, only, rest, error = main._parse_flags(["task", bad])
            self.assertTrue(error, bad)
            self.assertEqual(only, "")

    def test_positional_args_are_passed_through(self):
        self.assertEqual(main._parse_flags(["dry-run", "full"])[2], ["dry-run", "full"])
        self.assertEqual(main._parse_flags([])[2], [])


class PruneSwitchTests(unittest.TestCase):
    """`main.py prune` 的开关解析。

    ★ 这个类存在的头号理由不是"开关认不认得出"，而是**循环必须前进**：
      第一版实现漏了 `index += 1`，`main.py prune` 会在同一个元素上原地
      打转、占满一个核、永远不返回（真机上跑了 7 分钟 100% CPU、靠
      `ps -o time` 才认出是自旋）。下面每条测试都会在死循环下卡住 ——
      也就是说，这个类本身就是"别再把死循环发上线"的看门人。
    """

    def test_defaults_are_preview_only(self):
        info = main._parse_maintenance(["prune"])
        self.assertEqual(info["error"], "")
        self.assertFalse(info["apply"], "不传 --apply 绝不能真删")
        self.assertFalse(info["caches"])
        self.assertIsNone(info["grace_days"])

    def test_apply_and_caches_are_recognized(self):
        info = main._parse_maintenance(["prune", "--apply", "--caches", "--json"])
        self.assertEqual(info["error"], "")
        self.assertTrue(info["apply"])
        self.assertTrue(info["caches"])
        self.assertTrue(info["json"])

    def test_older_than_accepts_both_spellings(self):
        self.assertEqual(main._parse_maintenance(["prune", "--older-than", "3"])["grace_days"], 3.0)
        self.assertEqual(main._parse_maintenance(["prune", "--older-than=0"])["grace_days"], 0.0)

    def test_older_than_rejects_garbage_and_negatives(self):
        for bad in (["prune", "--older-than"], ["prune", "--older-than=abc"],
                    ["prune", "--older-than", "-1"]):
            info = main._parse_maintenance(bad)
            self.assertTrue(info["error"], bad)
            self.assertIsNone(info["grace_days"], bad)

    def test_unknown_switch_is_an_error_not_a_silent_default(self):
        # 清理是删文件的动作："--appli" 少半个字母绝不能变成"什么都没干但看起来成功了"，
        # 更不能变成"按默认宽限期删了"。
        info = main._parse_maintenance(["prune", "--appli"])
        self.assertIn("--appli", info["error"])
        self.assertFalse(info["apply"])

    def test_loop_advances_on_every_element(self):
        """每种开关各来一遍：函数必须返回（死循环会在这里挂住）。"""
        info = main._parse_maintenance(
            ["prune", "--apply", "--caches", "--json", "--older-than", "5", "--older-than=6", "clean"]
        )
        self.assertEqual(info["error"], "")
        self.assertTrue(info["apply"] and info["caches"] and info["json"])
        self.assertEqual(info["grace_days"], 6.0)

    def test_prune_switches_are_not_in_the_generic_rest(self):
        """`_parse_flags` 会丢掉 prune 自己的开关 —— 这正是 `_prune()` 必须
        读 `sys.argv` 的原因。这条测试把这个"设计缺陷"钉成事实：哪天有人把
        `pass` 改成 `rest.append(item)`，这里会红，提醒他去看 `_prune()`。"""
        _, _, rest, error = main._parse_flags(["prune", "--apply", "--caches", "--json"])
        self.assertEqual(error, "")
        self.assertEqual(rest, ["prune"])

    def test_apply_reaches_maintenance_via_argv(self):
        """★ 端到端回归：`main.py prune --apply --caches` 的 `--apply` 必须真的
        传到 `prune_caches(apply=True)` / `prune_orphans(apply=True)`。
        第一版读 `_ARGS` 时这里是 False（用户以为删了，实际只预演）。"""
        from core import maintenance

        seen = {}

        def fake_caches(apply=False):
            seen["caches"] = apply
            return {"removed": [], "freed_bytes": 0}

        def fake_orphans(apply=False, grace_days=None):
            seen["orphans"] = apply
            seen["grace_days"] = grace_days
            return {"removed": [], "skipped": [], "skipped_symlink": [], "freed_bytes": 0}

        with mock.patch.object(maintenance, "profile_report", lambda: {"exists": True, "profiles": [], "error": ""}), \
             mock.patch.object(maintenance, "report_lines", lambda report: []), \
             mock.patch.object(maintenance, "prune_lines", lambda result, what="": []), \
             mock.patch.object(maintenance, "prune_caches", fake_caches), \
             mock.patch.object(maintenance, "prune_orphans", fake_orphans), \
             mock.patch("sys.argv", ["main.py", "prune", "--apply", "--caches"]):
            code = main._prune()

        self.assertEqual(code, 0)
        self.assertIs(seen.get("caches"), True, "--apply 没传到 prune_caches")
        self.assertIs(seen.get("orphans"), True, "--apply 没传到 prune_orphans")

    def test_without_apply_nothing_is_deleted(self):
        """不传 `--apply`：孤儿段也必须是 apply=False，且不传 `--caches` 时
        连缓存清理都不该被调用（少做一件事 = 少一个删错的机会）。"""
        from core import maintenance

        with mock.patch.object(maintenance, "profile_report",
                               lambda: {"exists": True, "profiles": [], "error": ""}), \
             mock.patch.object(maintenance, "report_lines", lambda report: []), \
             mock.patch.object(maintenance, "prune_lines", lambda result, what="": []), \
             mock.patch.object(maintenance, "prune_caches") as fake_caches, \
             mock.patch.object(maintenance, "prune_orphans") as fake_orphans, \
             mock.patch("sys.argv", ["main.py", "prune"]):
            fake_caches.return_value = {"removed": [], "freed_bytes": 0}
            fake_orphans.return_value = {"removed": [], "skipped": [], "skipped_symlink": [], "freed_bytes": 0}
            main._prune()

        self.assertFalse(fake_caches.called, "没传 --caches 却动了缓存")
        self.assertEqual(fake_orphans.call_count, 1)
        self.assertIs(fake_orphans.call_args.kwargs.get("apply"), False)


if __name__ == "__main__":
    unittest.main()


class AutoPruneWiringTests(unittest.TestCase):
    """`main.py` 里那次"顺带维护"的接线。

    这是"磁盘别被缓存吃满"这条链路的最后一环，断了的后果是**静默**的：
    缓存继续长、盘满了发送才失败。所以三件事都要钉住 ——

    * 真轮次（`dry_run is None`）会调一次；
    * 空跑（dry-run）**不调**：那是人正盯着屏幕，不该动文件；
    * 维护自己炸了不许把轮次带崩，而且要把错误说出来（不许静默）。
    """

    def test_a_real_run_prunes_first(self):
        rec = {"nothing_due": True, "schedule": {"waiting": []}}
        with mock.patch.object(main, "_auto_prune_notice") as notice, \
                mock.patch("core.tasks.runTasks", return_value=rec):
            self.assertEqual(main._run(None), 0)
        self.assertEqual(notice.call_count, 1)

    def test_a_dry_run_never_touches_the_disk(self):
        rec = {"dry_run": "full", "accounts": []}
        with mock.patch.object(main, "_auto_prune_notice") as notice, \
                mock.patch("core.tasks.runTasks", return_value=rec):
            main._run("full")
        self.assertEqual(notice.call_count, 0)

    def test_the_notice_survives_a_crash(self):
        with mock.patch("core.maintenance.maybe_auto_prune",
                        side_effect=RuntimeError("boom")):
            main._auto_prune_notice()  # 不许抛

    def test_the_notice_reports_what_was_freed(self):
        import io
        from contextlib import redirect_stdout

        result = {"ran": True, "reason": "ok", "human_freed": "1.5 GB",
                  "caches": {"human_freed": "1.2 GB"},
                  "orphans": {"human_freed": "300.0 MB"}}
        buffer = io.StringIO()
        with mock.patch("core.maintenance.maybe_auto_prune", return_value=result), \
                redirect_stdout(buffer):
            main._auto_prune_notice()
        text = buffer.getvalue()
        self.assertIn("自动清理", text)
        self.assertIn("1.5 GB", text)
        self.assertIn("1.2 GB", text)

    def test_a_partial_cleanup_is_reported_as_a_warning(self):
        import io
        from contextlib import redirect_stderr

        result = {"ran": True, "reason": "error", "error": "读不到账号名单",
                  "human_freed": "0 B", "caches": {}, "orphans": {}}
        buffer = io.StringIO()
        with mock.patch("core.maintenance.maybe_auto_prune", return_value=result), \
                redirect_stderr(buffer):
            main._auto_prune_notice()
        self.assertIn("读不到账号名单", buffer.getvalue())

    def test_an_idle_tick_prints_nothing(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mock.patch("core.maintenance.maybe_auto_prune",
                        return_value={"ran": False, "reason": "not-due"}), \
                redirect_stdout(buffer):
            main._auto_prune_notice()
        self.assertEqual(buffer.getvalue(), "")
