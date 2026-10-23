"""``python -m webui.reset_password`` —— 面板进不去时的应急改口令。

和 ``webui/hash_password`` 的分工：那个只把口令算成哈希串、你自己去改 ``.env`；
这个**直接写进 ``.env`**（走面板同一个写入口 ``webui.envfile.set_webui_password``：先备份、
拿同一把跨进程文件锁、明文与哈希两个键一起写），写完再重启面板并轮询 ``/healthz``，
所以它不需要"先能登录面板"，也不需要记得重启服务。

用法::

    cd /opt/douyin-spark-fusion
    .venv/bin/python -m webui.reset_password                # 交互输入（不回显，输两遍）
    .venv/bin/python -m webui.reset_password '我的新口令'    # 直接给
    .venv/bin/python -m webui.reset_password --random       # 生成随机口令并打印
    echo 我的口令 | .venv/bin/python -m webui.reset_password --stdin

    --hash        存哈希（默认存明文，与面板「访问口令」的默认一致）
    --no-restart  只写 .env，不动服务
    --path FILE   指定要写的 .env（默认仓库根的 .env；也认 ENV_FILE）
    --dry-run     只说会写哪个文件、什么存法，一个字节都不落盘

退出码：0 = 已写入（且服务已重启，或本来就无需重启）；1 = 写入失败 / 口令不合规 / 重启后没起来；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from webui import envfile
from webui.hash_password import _read_stdin
from webui.password import random_password

UNIT = "douyin-spark-fusion-web.service"
UNIT_DIR = Path("/etc/systemd/system")
DEFAULT_PORT = 8787
REPO_ROOT = Path(__file__).resolve().parent.parent


def _default_env_path() -> Path:
    """默认写仓库根的 ``.env`，**不依赖当前目录** —— 应急时人可能站在 ``/root`` 里。

    ``ENV_FILE`` 仍然优先：它是全项目认的"换一个 .env"开关（``webui/envfile.env_path`` 同口径）。
    """
    override = os.getenv("ENV_FILE")
    if override:
        return Path(override)
    return REPO_ROOT / ".env"


def _read_interactive() -> str:
    """输两遍、不回显；空口令或两次不一致都返回空串（调用方据此取消）。"""
    try:
        first = getpass.getpass("请输入新口令（不回显）：")
        if not first:
            print("口令为空，已取消。", file=sys.stderr)
            return ""
        again = getpass.getpass("请再输一次确认：")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return ""
    if first != again:
        print("两次输入不一致，已取消。", file=sys.stderr)
        return ""
    return first


def _resolve_password(args) -> str:
    if args.password:
        return args.password
    if args.random:
        return random_password()
    if args.stdin or not sys.stdin.isatty():
        return _read_stdin()
    return _read_interactive()


def _web_port(path: Path) -> int:
    """健康检查用哪个端口：``.env` 的 ``WEBUI_PORT``，读不到就用 8787。"""
    try:
        raw = str((envfile.read_map(path) or {}).get("WEBUI_PORT", "")).strip()
    except Exception:                                            # noqa: BLE001
        raw = ""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def _wait_healthz(port: int, *, timeout: float = 10.0) -> bool:
    """轮询 ``/healthz``。重启后的头几秒连不上是正常的（uvicorn 还在 import），所以要轮询。"""
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:   # noqa: S310 - 只连本机回环
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def _restart_panel() -> str:
    """重启面板，返回一句给人看的结果。**只报告、不抛** —— 口令已经写进去了，重启失败不该把结论翻掉。"""
    unit_path = UNIT_DIR / UNIT
    if not unit_path.exists():
        return (f"没找到 {unit_path}：面板不是 systemd 装的，跳过重启；"
                f"容器/开发机上用 ``python -m webui`` 重启")
    if shutil.which("systemctl") is None:
        return "没有 systemctl，跳过重启；请手动重启面板"
    if subprocess.run(["systemctl", "is-active", "--quiet", UNIT]).returncode != 0:
        return f"{UNIT} 当前没在运行：口令已写入，下次启动就用新口令（systemctl start {UNIT}）"
    done = subprocess.run(["systemctl", "restart", UNIT])
    if done.returncode != 0:
        return (f"重启失败（systemctl restart 退出码 {done.returncode}）：口令已写入，"
                f"但可能仍按旧口令校验，去看 systemctl status {UNIT}")
    return "服务已重启"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m webui.reset_password",
        description="把面板口令写进 .env 并重启面板（忘了口令、进不去时用）",
        epilog="退出码：0=已写入；1=写入失败/口令不合规/重启后没起来；2=用法错误",
    )
    parser.add_argument("password", nargs="?", help="新口令（8-128 字符；省略则交互输入）")
    parser.add_argument("--random", action="store_true", help="生成一个随机口令并打印出来")
    parser.add_argument("--stdin", action="store_true", help="从管道读一行作为新口令")
    parser.add_argument("--hash", action="store_true", help="存哈希（默认存明文，与面板默认一致）")
    parser.add_argument("--path", default=None, help="要写的 .env（默认仓库根的 .env）")
    parser.add_argument("--no-restart", action="store_true", help="只写 .env，不动服务")
    parser.add_argument("--dry-run", action="store_true", help="只说会写什么，不落盘、不重启")
    args = parser.parse_args(argv)

    if args.random and args.password:
        parser.error("--random 与显式口令不能一起给")
    if args.stdin and args.password:
        parser.error("--stdin 与显式口令不能一起给")

    target = Path(args.path) if args.path else _default_env_path()
    store = "hash" if args.hash else "plain"

    if args.dry_run:
        print(f"[dry-run] 目标文件：{target}")
        print(f"[dry-run] 存法：{'WEBUI_PASSWORD_HASH（pbkdf2，48 万轮）' if args.hash else 'WEBUI_PASSWORD（明文）'}")
        print(f"[dry-run] 写完{'不会' if args.no_restart else '会'}重启 {UNIT}")
        return 0

    password = _resolve_password(args)
    if not password:
        print("没有拿到口令，已取消。", file=sys.stderr)
        return 1

    result = envfile.set_webui_password(password, store=store, path=target)
    if not result.get("ok"):
        print(f"写入失败：{result.get('error') or '原因未知'}", file=sys.stderr)
        return 1

    if args.random:
        # 随机口令只在这一次输出里出现：写进 .env 之后就只剩哈希/明文，别再指望能捞回来
        print(f"新口令：{password}")
        print("（这行只打印这一次，现在就抄走；别贴进聊天记录或工单）")

    changed = list(result.get("changed") or [])
    written = result.get("path") or str(target)
    backup = result.get("backup") or ""
    if changed:
        print(f"已写入 {written}：{', '.join(changed)}")
        if backup:
            print(f"改动前的备份：{backup}")
        print("旧口令签出的会话已全部失效：请用新口令重新登录面板。")
    else:
        print(f"{written} 里的口令与这次要写的一致，没有改动任何字节。")

    if args.no_restart:
        print(f"没有重启面板（--no-restart）：新口令要等 {UNIT} 重启后才生效，"
              f"记得 systemctl restart {UNIT}")
        return 0

    print(_restart_panel())
    port = _web_port(target)
    if _wait_healthz(port):
        print(f"健康检查通过：http://127.0.0.1:{port}/healthz")
        return 0
    print(f"10 秒内 http://127.0.0.1:{port}/healthz 没响应 —— 口令已写入，"
          f"去看：systemctl status {UNIT}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
