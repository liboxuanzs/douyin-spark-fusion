"""`main.py` 的纯函数部分 —— CLI 入口的状态文案与来源解析。

**刻意不调用 `main()`**：那会真的去开浏览器。这里只测两件最容易说错话的事：

* "一条消息都没有真的发出去"这句提示，只能在**账本里最终生效的档位**不是
  `off` 时打印。`main.py task` 传的是 `None`（= 听 `.env` 的），
  拿入参去比就永远不相等，真发的那一轮也会在日志里留下"空跑"字样 ——
  而面板的日志页正好显示这行。
* 触发来源不能写死在 `.env` 里，否则面板就分不清谁跑的了。
"""
import os
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
    示例偏偏就是 `python main.py task --only 10050yi` —— 2026-10-09 想只跑一个账号
    时撞上的就是这个。位置参数的"静默忽略"更隐蔽：`--only x` 被吞掉后 `x` 会留在
    rest 里当子命令，`task x` 照样跑全部账号。
    """

    def test_only_with_a_value(self):
        force, only, rest, error = main._parse_flags(["task", "--only", "10050yi"])
        self.assertEqual((force, only, rest, error), (False, "10050yi", ["task"], ""))

    def test_only_alias_and_equals_form(self):
        self.assertEqual(main._parse_flags(["task", "-o", "wheat"])[1], "wheat")
        self.assertEqual(main._parse_flags(["task", "--only=wheat"])[1], "wheat")

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


if __name__ == "__main__":
    unittest.main()
