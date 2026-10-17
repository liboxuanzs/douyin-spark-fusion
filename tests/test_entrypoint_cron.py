"""`docker/entrypoint-cron.sh` 的调度决策单测（在临时沙箱里真跑那个脚本）。

审计项（高）：容器里以前是 `CRON_MINUTE CRON_HOUR * * *` —— **一天只敲一次门**。
而调度器要求按 `core/schedule.py::TICK_MINUTES`（5 分钟）反复敲门：每个账号的
发送时刻是 `TASKS[].time` 加一个 [0, SCHEDULE_JITTER_MINUTES] 内的**非负**抖动，
敲门那一刻只有"已经到点"的账号发得出去。默认抖动 15 分钟时，一天一次的门铃只能
覆盖恰好落在那一分钟的账号（约 1/16），其余账号当天一条都发不出去。

这些用例跑的是**真脚本**：把里面的容器绝对路径（/app/.env、/etc/cron.d/…）用 sed
改写到临时目录，`exec cron -f` 换成 `exit 0`，再检查生成的 crontab 行。脚本要
`python -c "from dotenv import …"` 读 .env，所以本机没装 python-dotenv 时整体跳过
（判据用 `find_spec`，不 import 它）。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "docker", "entrypoint-cron.sh")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import schedule as core_schedule  # noqa: E402

HAVE_DOTENV = importlib.util.find_spec("dotenv") is not None

#: 被测对象是**容器入口脚本**：`#!/bin/sh` + `sed` + `/etc/cron.d`，只在 POSIX 上存在。
#: Windows 上 `subprocess` 找不到 `bash` 会直接 FileNotFoundError（issue #3 机制二：
#: 本文件 8 条失败全是它）。这里同时要求 POSIX 与真 bash：Git-Bash 那种"有 bash 但路径
#: 语义是 Windows"的环境也不行 —— setUp 把脚本里的 `/app/.env` 之类改写成宿主的
#: `os.path.join(...)`（带盘符的 Windows 路径），喂给 bash 只会得到一堆错路径。
HAVE_POSIX_BASH = os.name == "posix" and shutil.which("bash") is not None


@unittest.skipIf(not HAVE_DOTENV, "本机没有 python-dotenv：脚本读不了 .env")
@unittest.skipUnless(HAVE_POSIX_BASH, "本机没有 bash/不是 POSIX：容器入口脚本跑不起来")
class EntrypointCronTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        for sub in ("app", "etc", "cron.d"):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
        self.cron_file = os.path.join(self.dir, "cron.d", "douyin-spark-flow")

        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        for old, new in (
            ("/app/.env", os.path.join(self.dir, "app", ".env")),
            ("/etc/douyin-spark-flow.env", os.path.join(self.dir, "etc", "flow.env")),
            ("/etc/cron.d/douyin-spark-flow", self.cron_file),
            ("/tmp/douyin-spark-flow.tz", os.path.join(self.dir, "tz")),
            ("\nexec cron -f", "\nexit 0"),          # 别真去起一个 cron 守护进程
        ):
            text = text.replace(old, new)
        self.script = os.path.join(self.dir, "entrypoint.sh")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(text)

    def _run(self, env_text: str, **extra):
        """跑一次脚本，返回 (输出, crontab 里那行任务)。"""
        with open(os.path.join(self.dir, "app", ".env"), "w", encoding="utf-8") as f:
            f.write(env_text)

        env = dict(os.environ)
        # 脚本里写的是 `python -`：让 PATH 里的 python 就是跑测试的这个解释器，
        # 它带了 python-dotenv（否则上面的 skipIf 已经跳过了）。
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        env["TZ"] = "Asia/Shanghai"
        env.pop("CRON_SCHEDULE", None)
        env.pop("CRON_TICK_MINUTES", None)
        env.update({k: str(v) for k, v in extra.items()})

        proc = subprocess.run(["bash", self.script], env=env, cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"脚本失败：{proc.stderr}")
        with open(self.cron_file, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        task = [ln for ln in lines if "run-task.sh" in ln]
        self.assertEqual(len(task), 1, f"没生成任务行：{lines}")
        return proc.stdout + proc.stderr, task[0]

    def test_default_is_the_same_tick_as_core_schedule(self):
        """★ 默认频率必须等于 `core/schedule.py::TICK_MINUTES`，不能写死一个数。"""
        out, task = self._run("TZ=Asia/Shanghai\n")
        self.assertTrue(task.startswith(f"*/{core_schedule.TICK_MINUTES} "), task)
        self.assertIn(f"*/{core_schedule.TICK_MINUTES} * * * *", out)

    def test_config_tool_style_9_0_still_gets_the_tick(self):
        """★ 核心回归：configTool 恒定写 CRON_HOUR=9 / CRON_MINUTE=0。

        把 9/0 当成"用户显式设置"就永远走老行为 = 抖动缺陷原样保留 ——
        这里断言它照样是 5 分钟一敲，且**不**打印老行为警告。
        """
        out, task = self._run("TZ=Asia/Shanghai\nCRON_HOUR=9\nCRON_MINUTE=0\n"
                              "CRON_SECOND=0\n")
        self.assertTrue(task.startswith(f"*/{core_schedule.TICK_MINUTES} "), task)
        self.assertNotIn("一天只敲一次", out)

    def test_explicit_other_time_falls_back_to_once_a_day_with_warning(self):
        """真的改成别的时间 → 按老行为一天一次，但必须说清抖动会失效。"""
        out, task = self._run("TZ=Asia/Shanghai\nCRON_HOUR=10\nCRON_MINUTE=30\n")
        self.assertTrue(task.startswith("30 10 * * * "), task)
        self.assertIn("一天只敲一次", out)
        self.assertIn("抖动", out)

    def test_tick_can_come_from_the_env_file(self):
        """`CRON_TICK_MINUTES` 写在 .env 里也要认（以前只有容器环境变量能设）。"""
        out, task = self._run("TZ=Asia/Shanghai\nCRON_TICK_MINUTES=10\n")
        self.assertTrue(task.startswith("*/10 * * * * "), task)

    def test_container_env_wins_over_env_file(self):
        out, task = self._run("TZ=Asia/Shanghai\nCRON_TICK_MINUTES=10\n",
                              CRON_TICK_MINUTES=3)
        self.assertTrue(task.startswith("*/3 * * * * "), task)

    def test_invalid_tick_falls_back_and_warns(self):
        out, task = self._run("TZ=Asia/Shanghai\nCRON_TICK_MINUTES=99\n")
        self.assertTrue(task.startswith(f"*/{core_schedule.TICK_MINUTES} "), task)
        self.assertIn("非法", out)

    def test_explicit_cron_schedule_is_used_verbatim_with_warning(self):
        out, task = self._run("TZ=Asia/Shanghai\n", CRON_SCHEDULE="0 9 * * *")
        self.assertTrue(task.startswith("0 9 * * * "), task)
        self.assertIn("CRON_SCHEDULE", out)
        self.assertIn("抖动", out)

    def test_no_leftover_flow_cron_file(self):
        """那个没人消费的 `/tmp/douyin-spark-flow.cron` 不该再被写出来。"""
        self._run("TZ=Asia/Shanghai\n")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "flow.cron")))


if __name__ == "__main__":
    unittest.main()
