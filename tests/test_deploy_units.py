"""部署件之间的一致性 —— 纯文本断言，不需要 systemd、不需要网络。

这里钉的都是"两边各自都对、合起来错"的融合缝：

* 面板的"下次定时"是读 `.env` 的 `TASK_SCHEDULE_TIMES` 算出来的，
  真正的调度却是 `deploy/douyin-spark-fusion-daily.timer` 的 `OnCalendar`
  说了算 —— 两边不一致时，面板会**信誓旦旦地显示一个错的时间**。
* 账本里的"触发来源"靠 `RUN_SOURCE=timer` 注入；而 `.env` **不许**交给
  systemd 的 `EnvironmentFile=` 解析 —— 它按转义序列处理值，会把消息模板里的
  字面 `\n` 吃成裸 `n`（换行丢失）。`.env` 由程序自己用 python-dotenv 读。
* 两个 unit 的日志必须追加到**同一个**文件，否则面板"日志"页只看得见一半。
* `.github/workflows/` 只允许留手动触发的一份（融合版的验收项之一：
  不上 GitHub 跑任何自动化）。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def _directives(text, key):
    """取出 unit 文件里**生效的** `Key=...` 行（跳过注释）。

    必须跳过注释：这两个 unit 的注释里大段讨论着"为什么不再用
    `RandomizedDelaySec`"，拿整段文本去 assertNotIn 会被自己的说明文字绊倒。
    """
    key = key.strip().lower()
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().lower() == key:
            found.append(value.strip())
    return found


def _env_value(key):
    """从 .env.example 里取一个键的值（跳过注释行）。"""
    for line in _read(ROOT, ".env.example").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


class ScheduleConsistencyTests(unittest.TestCase):
    """"几点发"由应用说了算，但"多久敲一次门"还是 timer 说了算 —— 钉的就是这条缝。

    2026-10-07 起每个账号可以有自己的发送时间（`TASKS[].time`，见 `core/schedule.py`），
    timer 从"每天 20:30 那一发"降级成**派发器**：只负责按时敲门，
    到了几个账号的点由应用判定。面板上"今天落在 08:07"、"等 08:07 到点"、
    "该发了"全都是按本文件的间隔承诺的，所以两边的数字必须一致。
    """

    def _tick(self):
        """从 timer 里读出敲门间隔（分钟）；写法不对就直接失败。"""
        timer = _read(DEPLOY, "douyin-spark-fusion-daily.timer")
        m = re.search(r"^OnCalendar=\*:0/(\d+)\s*$", timer, re.M)
        self.assertIsNotNone(
            m,
            "timer 必须写成 `OnCalendar=*:0/N` 这种派发器形式："
            "每个账号各有各的发送时间，一个固定的 `*-*-* HH:MM:SS` 表达不了；"
            "写成 `*-*-* HH:MM:SS` 还会让面板显示的时间表与实际发送时间对不上",
        )
        return int(m.group(1))

    def test_timer_interval_matches_core_schedule_tick(self):
        """门铃的间隔必须等于 `core.schedule.TICK_MINUTES`。

        敲得比它慢：面板写着"该发了"，用户却要一直等；
        敲得比它快：白起一堆进程、账本里多一堆早退。
        `core/schedule.py` 是纯 stdlib，这里 import 它不需要 systemd 也不需要网络。
        """
        from core import schedule

        self.assertEqual(
            self._tick(), schedule.TICK_MINUTES,
            "timer 的敲门间隔与 core/schedule.py 的 TICK_MINUTES 不一致："
            "面板承诺的「等 08:07 到点」就是按后者算的",
        )

    def test_timer_does_not_jitter_the_whole_unit(self):
        """抖动必须按账号算，**不能**交给 systemd。

        `RandomizedDelaySec` 是"整个 unit 抖一次" —— 所有账号会一起挪到同一个
        随机点，那恰好是最像机器人的行为：真实的人不会每天以同一个偏移量发。
        现在偏移按 `sha256(抖音号|日期)` 算，同一个账号同一天恒定一个时刻。
        """
        timer = _read(DEPLOY, "douyin-spark-fusion-daily.timer")
        self.assertEqual(
            _directives(timer, "RandomizedDelaySec"), [],
            "抖动已改为 core/schedule 按账号计算，timer 里不该再有 RandomizedDelaySec："
            "它抖的是整个 unit，所有账号会一起挪到同一个随机点",
        )

    def test_retired_deployment_time_is_still_the_fallback(self):
        """口径：旧部署 `douyin-spark.service` 是 20:30 + 0~20 分钟抖动。

        2026-10-01 用户要求"时间改成和上个程序设定的一样"。现在 20:30 从
        "唯一的发送时间"变成"**没单独配时间的账号**的默认时间"，
        但默认值本身不该漂 —— 改它等于替所有存量账号改了发送时间。
        """
        from core import schedule

        self.assertEqual(schedule.DEFAULT_TIME, "20:30")
        self.assertEqual(
            schedule.parse_time(_env_value("TASK_SCHEDULE_TIMES")),
            "20:30",
            ".env.example 的 TASK_SCHEDULE_TIMES 是没写 time 的账号的默认时间，"
            "不跟着 core/schedule.DEFAULT_TIME 走，会让「新装一份」和「这份默认」不一致",
        )
        self.assertEqual(
            _env_value("QUIET_WINDOW"),
            "off",
            "禁跑窗默认必须是 off：默认的 20:20-21:00 会把晚间的账号整轮吃掉，"
            "而现在门铃每 5 分钟一次，撞上禁跑窗就整天不发",
        )

    def test_jitter_default_is_a_non_negative_int(self):
        from core import schedule

        raw = _env_value("SCHEDULE_JITTER_MINUTES")
        self.assertIsNotNone(raw, ".env.example 里应当带上 SCHEDULE_JITTER_MINUTES")
        self.assertTrue(raw.lstrip("-").isdigit(), "SCHEDULE_JITTER_MINUTES 必须是整数：%r" % raw)
        self.assertEqual(int(raw), schedule.DEFAULT_JITTER_MINUTES)
        self.assertGreaterEqual(int(raw), 0)

    def test_state_file_points_into_logs(self):
        from core import schedule

        raw = _env_value("SCHEDULE_STATE_FILE")
        self.assertEqual(raw, schedule.DEFAULT_STATE_FILE)
        # 写成绝对路径或跑出 logs/ 都会让 logrotate 与备份脚本漏掉它
        self.assertTrue(raw.startswith("logs/"), "账本必须落在 logs/ 下：%r" % raw)

    def test_timer_points_at_our_service_and_is_persistent(self):
        timer = _read(DEPLOY, "douyin-spark-fusion-daily.timer")
        self.assertIn("Unit=douyin-spark-fusion-daily.service", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)


class RunSourceInjectionTests(unittest.TestCase):
    """定时跑的那一轮必须被标成 timer，而不是"命令行"。"""

    def setUp(self):
        self.service = _read(DEPLOY, "douyin-spark-fusion-daily.service")

    def test_service_injects_timer_source(self):
        self.assertIn("Environment=RUN_SOURCE=timer", self.service)

    def test_env_is_not_parsed_by_systemd(self):
        """★ 两个 unit 都**不许**写 `EnvironmentFile=`。

        systemd 解析 EnvironmentFile 时按"转义序列"处理值：`.env` 里消息模板写的
        字面 `\\n`（规则："用 \\n 表示换行"）会被吃掉反斜杠、只剩一个裸 `n` ——
        发出去的消息变成一整行、行尾挂着 n（2026-10-02 20:38 那一轮的真实故障）。
        同一份文件交给 python-dotenv 读则原样保留。

        所以 `.env` 只由程序自己读（`main.py` / `python -m webui`），
        它同时是"面板预览看到的"和"实际发出去的"唯一真相。
        """
        for name in ("douyin-spark-fusion-daily.service", "douyin-spark-fusion-web.service"):
            text = _read(DEPLOY, name)
            active = [ln for ln in text.splitlines()
                      if ln.strip().startswith("EnvironmentFile=")]
            self.assertEqual(
                active, [],
                "%s 里出现了 EnvironmentFile= —— 它会把 .env 里的字面 \\n 吃成裸 n，"
                "换行就没了；.env 交给程序自己读" % name,
            )
            self.assertIn("EnvironmentFile", text, "%s 该留一条注释说明为什么不写它" % name)

    def test_service_runs_task_mode(self):
        self.assertIn("main.py task", self.service)
        self.assertIn("Type=oneshot", self.service)
        self.assertIn("WorkingDirectory=/opt/douyin-spark-fusion", self.service)

    def test_env_example_does_not_hardcode_run_source(self):
        # .env 是三个入口共用的：写死来源会让面板再也分不清谁触发的
        self.assertIsNone(_env_value("RUN_SOURCE"))


class LogAndPortTests(unittest.TestCase):
    def test_both_units_append_to_the_same_log(self):
        want = "/opt/douyin-spark-fusion/logs/" + os.path.basename(
            _env_value("OPS_LOG_FILE") or ""
        )
        for name in ("douyin-spark-fusion-web.service", "douyin-spark-fusion-daily.service"):
            text = _read(DEPLOY, name)
            self.assertIn("StandardOutput=append:%s" % want, text, name)
            self.assertIn("StandardError=append:%s" % want, text, name)

    def test_web_unit_starts_the_dashboard_module(self):
        web = _read(DEPLOY, "douyin-spark-fusion-web.service")
        self.assertIn("ExecStart=/opt/douyin-spark-fusion/.venv/bin/python -m webui", web)
        self.assertIn("Restart=always", web)

    def test_web_port_is_the_one_documented_and_free(self):
        # 8787：既有 douyin-spark.service 占 8000、sub2api 占 8080
        self.assertEqual(_env_value("WEBUI_PORT"), "8787")
        self.assertEqual(_env_value("WEBUI_HOST"), "0.0.0.0")

    def test_install_script_installs_all_three_units(self):
        sh = _read(DEPLOY, "install.sh")
        for name in (
            "douyin-spark-fusion-web.service",
            "douyin-spark-fusion-daily.service",
            "douyin-spark-fusion-daily.timer",
        ):
            self.assertIn(name, sh)
        self.assertIn("daemon-reload", sh)


class LogRotateTests(unittest.TestCase):
    """F8：日志必须轮转，否则 Debug 档一天就能把磁盘写满。"""

    def test_rule_covers_the_log_the_units_write(self):
        text = _read(DEPLOY, "logrotate.conf")
        self.assertIn("/opt/douyin-spark-fusion/logs/*.log", text)
        # 两个单元写的那个文件名必须能被通配符盖住
        self.assertTrue(os.path.basename(_env_value("OPS_LOG_FILE") or "").endswith(".log"))

    def test_rule_uses_copytruncate(self):
        """**关键**：进程由 systemd 拉起、不会重开日志文件。

        用默认的 create 而不是 copytruncate，轮转后进程继续往改名走的旧 inode 写，
        新文件永远是空的 —— 面板"日志"页会突然不再更新。
        """
        text = _read(DEPLOY, "logrotate.conf")
        self.assertIn("copytruncate", text)
        self.assertNotIn("\n    create", text)

    def test_install_script_installs_the_rule(self):
        sh = _read(DEPLOY, "install.sh")
        self.assertIn("/etc/logrotate.d/douyin-spark-fusion", sh)
        self.assertIn("logrotate.conf", sh)


class WorkflowTriggerTests(unittest.TestCase):
    """验收项：不再依赖 GitHub Actions 自动运行。"""

    def _workflow_files(self):
        if not os.path.isdir(WORKFLOWS):
            return []
        return [f for f in os.listdir(WORKFLOWS) if f.endswith((".yml", ".yaml"))]

    def test_only_the_local_tool_build_remains(self):
        self.assertEqual(self._workflow_files(), ["build-configtool.yml"])

    def test_no_scheduled_or_push_trigger(self):
        for name in self._workflow_files():
            text = _read(WORKFLOWS, name)
            head = text.split("jobs:", 1)[0]
            self.assertNotIn("schedule:", head, "%s 里还有定时触发" % name)
            self.assertNotIn("push:", head, "%s 里还有 push 触发" % name)
            self.assertIn("workflow_dispatch:", head, "%s 应当只保留手动触发" % name)

    def test_readme_does_not_claim_actions_automation(self):
        readme = _read(ROOT, "README.md")
        self.assertNotIn("支持 GitHub Actions 自动运行", readme)


if __name__ == "__main__":
    unittest.main()
