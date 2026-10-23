"""webui.reset_password 的离线测试。

钉三件事：
  * --dry-run 一个字节都不写（应急工具最怕"我说了不写结果写了"）；
  * 真的写的时候走的是 set_webui_password（明文/哈希**两个键同写**，不用的那个清空）；
  * 口令不合规时退出码 1 且文件不动 —— 别让人以为改成功了。
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webui import envfile
from webui import reset_password


class DefaultsTests(unittest.TestCase):
    def test_env_file_env_var_wins(self):
        with mock.patch.dict(os.environ, {"ENV_FILE": "/tmp/elsewhere.env"}):
            self.assertEqual(reset_password._default_env_path(), Path("/tmp/elsewhere.env"))

    def test_default_is_repo_root_not_cwd(self):
        os.environ.pop("ENV_FILE", None)
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            try:
                os.chdir(tmp)                      # 站在别处跑，也必须写到仓库根
                self.assertEqual(
                    reset_password._default_env_path(),
                    reset_password.REPO_ROOT / ".env",
                )
            finally:
                os.chdir(old)

    def test_web_port_reads_env_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("WEBUI_PORT=9001\n", encoding="utf-8")
            self.assertEqual(reset_password._web_port(path), 9001)
            path.write_text("WEBUI_PORT=not-a-number\n", encoding="utf-8")
            self.assertEqual(reset_password._web_port(path), reset_password.DEFAULT_PORT)
            path.write_text("", encoding="utf-8")
            self.assertEqual(reset_password._web_port(path), reset_password.DEFAULT_PORT)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env = Path(self._tmp.name) / ".env"
        self.env.write_text(
            "WEBUI_PASSWORD='old-one'\nMESSAGE_TEMPLATE='甲\\n乙'\n", encoding="utf-8"
        )
        self.before = self.env.read_bytes()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reset_password.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_cli(["--dry-run", "--path", str(self.env), "NewPass123"])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertEqual(self.env.read_bytes(), self.before)

    def test_plain_write_clears_hash_and_keeps_other_keys(self):
        code, out, _ = self.run_cli(["--no-restart", "--path", str(self.env), "NewPass123"])
        self.assertEqual(code, 0)
        values = envfile.read_map(self.env)
        self.assertEqual(values.get("WEBUI_PASSWORD"), "NewPass123")
        self.assertEqual(values.get("WEBUI_PASSWORD_HASH") or "", "")
        self.assertEqual(values.get("MESSAGE_TEMPLATE"), "甲\\n乙")   # 别的键一个都不许丢
        self.assertIn("WEBUI_PASSWORD", out)

    def test_hash_write_clears_plain(self):
        code, _, _ = self.run_cli(["--no-restart", "--hash", "--path", str(self.env), "NewPass123"])
        self.assertEqual(code, 0)
        values = envfile.read_map(self.env)
        self.assertEqual(values.get("WEBUI_PASSWORD") or "", "")
        self.assertTrue(str(values.get("WEBUI_PASSWORD_HASH") or "").startswith("pbkdf2_sha256$"))

    def test_rejects_short_password_without_touching_file(self):
        code, _, err = self.run_cli(["--no-restart", "--path", str(self.env), "short"])
        self.assertEqual(code, 1)
        self.assertIn("写入失败", err)
        self.assertEqual(self.env.read_bytes(), self.before)

    def test_random_password_is_printed_and_stored(self):
        code, out, _ = self.run_cli(["--no-restart", "--random", "--path", str(self.env)])
        self.assertEqual(code, 0)
        line = [ln for ln in out.splitlines() if ln.startswith("新口令：")][0]
        printed = line.split("新口令：", 1)[1].strip()
        self.assertGreaterEqual(len(printed), 8)
        self.assertEqual(envfile.read_map(self.env).get("WEBUI_PASSWORD"), printed)


if __name__ == "__main__":
    unittest.main()
