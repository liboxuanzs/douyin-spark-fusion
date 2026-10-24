"""deploy/update.sh 的静态契约 —— 不跑 git、不连网、不碰 systemd。

这个脚本是线上唯一的日常升级入口，所以它只许做 fast-forward、只许在有代码改动时
重启面板、绝不写 .env 或 logs/。这些约定全靠文本钉住，改脚本时能立刻红。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "update.sh")


def _read():
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def _code_lines(text):
    """去掉注释与空行：注释里会正经讨论 reset --hard 这类"不要做的事"。"""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


class UpdateScriptTests(unittest.TestCase):
    def test_exists(self):
        self.assertTrue(os.path.isfile(SCRIPT))

    def test_only_fast_forward_never_resets(self):
        code = "\n".join(_code_lines(_read()))
        self.assertIn("git merge --ff-only", code)
        self.assertNotIn("git reset", code)
        self.assertNotIn("git pull", code)          # pull 会绕过"有没有分叉"的检查
        self.assertNotIn("--force-with-lease", code)

    def test_refuses_while_a_run_is_in_flight(self):
        text = _read()
        self.assertIn("kill -0", text)
        self.assertIn("RUN_LOCK_FILE", text)
        self.assertIn("--force", text)

    def test_restarts_the_web_unit_only(self):
        code = "\n".join(_code_lines(_read()))
        self.assertIn('systemctl restart "$WEB_UNIT"', code)
        self.assertNotIn("restart douyin-spark-fusion-daily", code)   # oneshot，不需要重启

    def test_never_writes_env_or_logs(self):
        code = "\n".join(_code_lines(_read()))
        self.assertNotIn('> "$env_file"', code)
        self.assertNotIn('>> "$env_file"', code)
        self.assertNotIn("rm -rf", code)
        self.assertIn("pip install", code)          # 依赖只在 requirements.txt 变时更新

    def test_needs_restart_is_gated_on_code_paths(self):
        text = _read()
        self.assertIn("needs_restart=0", text)
        self.assertIn("webui|core|utils|main", text)

    def test_no_backtick_in_code(self):
        """反引号落在双引号里就是命令替换 —— 一行提示会把 `.env.example` 当命令跑。

        2026-10-24 实测：升级后打印 "行 308: .env.example: 未找到命令"，提示文字整条
        消失（其余输出正常，所以很容易一直看不见）。要强调用「」，别用反引号。
        """
        offenders = [ln for ln in _code_lines(_read()) if "`" in ln]
        self.assertEqual(offenders, [])

    @unittest.skipUnless(shutil.which("bash"), "没有 bash，跳过语法检查")
    def test_syntax_is_valid(self):
        done = subprocess.run([shutil.which("bash"), "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
