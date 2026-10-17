"""`configTool.env_store` 的写盘单测：权限收紧 + 拒绝会被读坏的值。

背景（审计项）：
  * 图形工具保存 `.env` 时**没有 chmod** —— 文件里是全部 `COOKIES_<unique_id>`
    （= 抖音 sessionid，等于账号本身），以默认 umask 落盘就是 644，同机任何用户
    都能读走。`deploy/install.sh` 与 `docker/entrypoint-cron.sh` 都是 600，
    只有图形工具这条路径漏了。
  * `risky_values()` 早就写好却**没有任何调用方**（死代码）：值里含 ` #` 时
    `set_key(..., quote_mode="never")` 原样写进去，dotenv 再读出来就被截断 ——
    用户看到的是"保存成功、下次启动模板少了一半"。

⚠️ 本机（开发容器）**没有 python-dotenv**（它在 requirements.txt 里，只有生产
镜像/venv 装）。为了让这组用例在本地也能真跑，这里在 import `env_store` 之前垫了
一个最小实现：只覆盖 `env_store` 用到的三个函数 + `.env` 的四条解析规则
（`#` 注释、首尾空白、`export ` 前缀、`key=value`）。垫片只在真包缺失时生效，
生产/CI 上跑的仍是 python-dotenv 本体。
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# 最小 dotenv 垫片（只在真包缺失时注册）
# ---------------------------------------------------------------------------
def _install_dotenv_stub_if_missing() -> bool:
    try:
        import dotenv  # noqa: F401
        return False
    except Exception:
        pass

    module = type(sys)("dotenv")

    def _find_key(line: str, key: str) -> bool:
        body = line[7:] if line.startswith("export ") else line
        if "=" not in body:
            return False
        return body.split("=", 1)[0].strip() == key

    def dotenv_values(path, **_kwargs):
        values: dict = {}
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            return values
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, _, value = line.partition("=")
            # 「 #」之后当行内注释（与 python-dotenv 一致），值再裁首尾空白
            value = value.split(" #", 1)[0].strip()
            values[key.strip()] = value
        return values

    def set_key(path, key, value, quote_mode="always", **_kwargs):
        lines = Path(path).read_text(encoding="utf-8").splitlines() if Path(path).exists() else []
        rendered = str(value) if quote_mode == "never" else f'"{value}"'
        for index, line in enumerate(lines):
            if _find_key(line, key):
                lines[index] = f"{key}={rendered}"
                break
        else:
            lines.append(f"{key}={rendered}")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True, key, rendered

    def unset_key(path, key, **_kwargs):
        target = Path(path)
        if not target.exists():
            return None, key
        lines = target.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not _find_key(line, key)]
        if len(kept) == len(lines):
            return None, key
        target.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return True, key

    module.dotenv_values = dotenv_values
    module.set_key = set_key
    module.unset_key = unset_key
    sys.modules["dotenv"] = module
    return True


STUBBED = _install_dotenv_stub_if_missing()

from configTool import env_store  # noqa: E402
from configTool.models import Account, Config  # noqa: E402


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class _TempEnv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_path = os.path.join(self._tmp.name, ".env")
        # 防呆：任何用例都只能碰临时目录，绝不能写到仓库根的真 .env
        assert self.env_path.startswith(tempfile.gettempdir())

    def _config(self, **kwargs) -> Config:
        return Config(**kwargs)

    def _account(self, uid="12345678905") -> Account:
        return Account(
            username="小明",
            unique_id=uid,
            cookies='[{"name":"sessionid","value":"secret"}]',
            targets=["好友A"],
            fingerprint="12345",
        )


class PermissionsTests(_TempEnv):
    def test_saved_env_is_600(self):
        """首建与更新之后都必须是 600（文件里是全部 sessionid）。"""
        env_store.save_config(self._config(accounts=[self._account()]), self.env_path)
        if os.name == "posix":
            self.assertEqual(_mode(self.env_path), 0o600)
            self.assertTrue(os.path.isfile(self.env_path))

    def test_existing_loose_permissions_are_tightened(self):
        """已经是 644 的旧文件，保存一次也要收紧（升级路径）。"""
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("WEBUI_PORT=8787\n")
        os.chmod(self.env_path, 0o644)
        if os.name != "posix":
            self.skipTest("非 POSIX 不校验权限位")
        env_store.save_config(self._config(accounts=[self._account()]), self.env_path)
        self.assertEqual(_mode(self.env_path), 0o600)


@unittest.skipIf(STUBBED, "需要真 python-dotenv：垫片不写临时文件")
class TempFileModeTests(_TempEnv):
    """写盘中间态（`.tmp_xxxxxxxx`）不能带着 644 权限裸奔。

    python-dotenv 的 `rewrite()`：`lstat` 取 `.env` 原 mode → 写
    `NamedTemporaryFile(prefix=".tmp_", dir=<.env 所在目录>)` → `chmod(tmp, 原 mode)`
    → `os.replace`。中间态里是**整份 .env**（含全部 COOKIES_），所以 `.env` 只要还是
    644，这个中间态也是 644；进程在 replace 之前被杀掉就留下一个可被同机任意用户
    读走的完整配置。`save_config` 因此改成"写之前先收紧已存在的 .env"。
    """

    def test_intermediate_temp_file_is_not_world_readable(self):
        # 生产代码 `configTool/env_store.py::_lock_down` 在 Windows 上是空操作
        # （那边 os.chmod 只有只读位），"中间态继承到 0600"这个行为不存在于非 POSIX；
        # 硬跑只会得到 "438 != 384"。同文件里另外两条权限用例本来就有这道闸，这里补齐。
        if os.name != "posix":
            self.skipTest("非 POSIX 不校验权限位")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("WEBUI_PORT=8787\n")
        os.chmod(self.env_path, 0o644)

        seen: dict = {}
        real_replace = os.replace

        def spy(src, dst):
            try:
                seen["name"] = os.path.basename(src)
                seen["mode"] = _mode(src)
            except OSError:
                pass
            return real_replace(src, dst)

        with mock.patch("os.replace", spy):
            env_store.save_config(self._config(accounts=[self._account()]),
                                  self.env_path)

        self.assertTrue(seen, "没观察到 os.replace：python-dotenv 的写盘流程变了？")
        self.assertEqual(seen["mode"], 0o600,
                         f"中间态 {seen['name']} 权限是 {oct(seen['mode'])}，"
                         "它装着全部 COOKIES_")
        self.assertEqual(_mode(self.env_path), 0o600)


class RiskyValueTests(_TempEnv):
    def test_hash_in_value_is_refused(self):
        """值里含 ` #`（会被当行内注释截断）必须拒绝保存，而不是写坏。"""
        cfg = self._config(accounts=[self._account()], log_level="Info # 备注")
        with self.assertRaises(env_store.RiskyValueError) as ctx:
            env_store.save_config(cfg, self.env_path)
        self.assertIn("LOG_LEVEL", str(ctx.exception))

    def test_refusal_does_not_touch_the_file(self):
        """拒绝时文件必须原样不动 —— 别出现"报错了但已经写进去一半"。"""
        original = "WEBUI_PORT=8787\nLOG_LEVEL=Info\n"
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write(original)
        cfg = self._config(accounts=[self._account()], message_template="第一行 # 第二行")
        with self.assertRaises(env_store.RiskyValueError):
            env_store.save_config(cfg, self.env_path)
        with open(self.env_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), original)

    def test_leading_whitespace_is_refused(self):
        cfg = self._config(accounts=[self._account()], proxy_address="  http://127.0.0.1:1080")
        with self.assertRaises(env_store.RiskyValueError) as ctx:
            env_store.save_config(cfg, self.env_path)
        self.assertIn("PROXY_ADDRESS", str(ctx.exception))

    def test_risky_values_reports_key_and_reason(self):
        cfg = self._config(accounts=[self._account()], log_level="Info # x")
        risky = env_store.risky_values(cfg)
        self.assertEqual([key for key, _ in risky], ["LOG_LEVEL"])
        self.assertIn("注释", risky[0][1])

    def test_json_with_hash_after_space_is_reported(self):
        """TASKS 里的目标名带「空格 + `#`」同样会被截断（JSON 也救不了）。

        ★ 2026-10-17 实测修正：python-dotenv 只在 `\\s+#` 处把井号当行内注释截断，
        `好友#1` 这种**前面没有空白**的井号是合法值、原样保留。原来这条用例用的是
        `好友#1`，于是逼着 `risky_values` 用「含 `#` 就报」的过严规则 —— 误伤了
        `话题#打卡` 这类正常文案。现在夹具改成真会被截断的形状。
        """
        account = self._account()
        account.targets = ["好友 #1"]
        cfg = self._config(accounts=[account])
        keys = [key for key, _ in env_store.risky_values(cfg)]
        self.assertIn("TASKS", keys)

    def test_hash_without_leading_space_is_not_risky(self):
        """`话题#打卡`：井号前面没有空白，dotenv 不截断 ⇒ 不能拒存。

        与 `webui/envfile.py::dotenv_hazard()` 同一口径（`re.search(r"(?:^|\\s)#", value)`）。
        """
        cfg = self._config(accounts=[self._account()], message_template="话题#打卡")
        self.assertEqual(env_store.risky_values(cfg), [])
        env_store.save_config(cfg, self.env_path)
        self.assertIn("话题#打卡", Path(self.env_path).read_text(encoding="utf-8"))

    def test_shell_expansion_is_risky(self):
        """`${…}` 会被 dotenv 插值改写、回写后不可逆 ⇒ 必须拒存。"""
        cfg = self._config(accounts=[self._account()], message_template="你好${昵称}今天")
        keys = [key for key, _ in env_store.risky_values(cfg)]
        self.assertIn("MESSAGE_TEMPLATE", keys)

    def test_normal_json_and_template_are_not_risky(self):
        """正常值（含 `\\n` 字面换行、JSON 数组）不能被误判为风险而拒存。"""
        cfg = self._config(
            accounts=[self._account()],
            message_template="第一行\\n第二行",
            hitokoto_types=["文学", "影视"],
        )
        self.assertEqual(env_store.risky_values(cfg), [])
        notes, orphans = env_store.save_config(cfg, self.env_path)
        self.assertEqual(orphans, [])
        self.assertTrue(os.path.isfile(self.env_path))
        self.assertIn("TASKS=", Path(self.env_path).read_text(encoding="utf-8"))


class WriteRoundTripTests(_TempEnv):
    def test_save_then_load_keeps_values(self):
        cfg = self._config(accounts=[self._account()])
        env_store.save_config(cfg, self.env_path)
        loaded, notes = env_store.load_config(self.env_path)
        self.assertEqual(len(loaded.accounts), 1)
        self.assertEqual(loaded.accounts[0].unique_id, "12345678905")

    def test_unmanaged_lines_survive(self):
        """用户手写的注释与别人的变量必须原样留着（就地更新的承诺）。"""
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("# 我手写的注释\nWEBUI_PORT=8787\nMY_OWN_KEY=keep-me\n")
        env_store.save_config(self._config(accounts=[self._account()]), self.env_path)
        text = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn("# 我手写的注释", text)
        self.assertIn("MY_OWN_KEY=keep-me", text)

    def test_orphan_cookies_are_reported_not_deleted(self):
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("COOKIES_99999999999=[{\"name\":\"sessionid\",\"value\":\"old\"}]\n")
        notes, orphans = env_store.save_config(
            self._config(accounts=[self._account()]), self.env_path
        )
        self.assertEqual(orphans, ["COOKIES_99999999999"])
        self.assertTrue(any("未使用的旧 Cookie" in note for note in notes))
        self.assertIn("COOKIES_99999999999",
                      Path(self.env_path).read_text(encoding="utf-8"))

    def test_missing_file_gets_header(self):
        env_store.save_config(self._config(), self.env_path)
        text = Path(self.env_path).read_text(encoding="utf-8")
        self.assertIn("DouYinSparkFlow 配置文件", text)


@unittest.skipIf(not STUBBED, "本机有真 python-dotenv：垫片未启用")
class StubbedRunNote(unittest.TestCase):
    """垫片生效时做个显式记录（真环境里这个类直接 skip）。"""

    def test_stub_is_documented(self):
        self.assertIn("dotenv", sys.modules)


if __name__ == "__main__":
    unittest.main()
