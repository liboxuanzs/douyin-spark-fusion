"""``python -m webui`` —— 启动仪表盘。

设计要点
--------
* ``.env`` 只在**这里**加载（惰性、且是本包唯一的 dotenv 调用点），
  这样 import ``webui.app`` 永远不会因为缺少 ``.env`` 而失败。
* 绑定非回环地址且没有配置口令 → 打印中文原因并 **exit 2**（绝不裸奔）。
* ``--check`` 只做启动前校验，方便部署脚本 / CI 使用。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from webui import __version__, config

EXIT_CONFIG_ERROR = 2


def load_env(path: str = ".env") -> Optional[str]:
    """加载仓库根的 ``.env``（不存在就跳过，不报错）。返回实际加载的路径。

    ``.env`` 是配置的唯一真相（面板预览读的就是它），所以文件优先于进程环境里的
    同名值；``WEBUI_*`` 例外，它们有命令行开关（``--host`` / ``--port``），
    也允许用环境变量显式覆盖。这与 ``utils.config.load_env_file`` 是同一套口径 ——
    **不要**改回 systemd 的 ``EnvironmentFile=``：它会把 ``.env`` 里的字面 ``\\n``
    吃成裸 ``n``（消息模板的换行就是这么丢的），于是"面板显示两行、实际发一行"。
    """
    if not os.path.exists(path):
        return None
    try:
        import dotenv  # noqa: F401  函数内 import：import webui 时不强依赖 dotenv
    except ImportError:  # pragma: no cover - 部署机上一定有
        config.warn("未安装 python-dotenv，跳过 .env 加载（请用环境变量提供配置）")
        return None
    from utils.config import PROCESS_WINS_KEYS, load_env_file

    load_env_file(
        path,
        keep=PROCESS_WINS_KEYS + ("WEBUI_HOST", "WEBUI_PORT", "WEBUI_PASSWORD", "WEBUI_PASSWORD_HASH"),
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m webui",
        description="DouYinSparkFlow 只读仪表盘（FastAPI + 原生前端，零外链）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m webui                      # 用 .env / 环境变量里的配置启动\n"
            "  python -m webui --host 0.0.0.0 --port 8787\n"
            "  python -m webui --check              # 只校验配置，不启动服务\n"
            "  python -m webui.hash_password        # 生成 WEBUI_PASSWORD_HASH\n"
        ),
    )
    parser.add_argument("--host", default=None, help="绑定地址（默认取 WEBUI_HOST，回落到 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="绑定端口（默认取 WEBUI_PORT，回落到 8787）")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重启")
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="不加载 .env（完全依赖环境变量）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做启动前校验（配置不合法则退出码 2），不真正启动服务",
    )
    parser.add_argument("--version", action="version", version=f"webui {__version__}")
    return parser


def run(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.no_env_file:
        loaded = load_env()
        if loaded:
            print(f"[webui] 已加载 {loaded}", file=sys.stderr)

    host = args.host if args.host else config.webui_host()
    if args.port is not None:
        os.environ["WEBUI_PORT"] = str(args.port)

    try:
        port = config.port()
    except config.ConfigError as exc:
        print(f"[webui] 配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # ---------------------------------------------------------- 启动前校验
    try:
        notes = config.preflight(host=host)
    except config.ConfigError as exc:
        print(f"[webui] 拒绝启动：{exc}", file=sys.stderr)
        print(
            "[webui] 提示：绑定非回环地址时必须配置 WEBUI_PASSWORD 或 WEBUI_PASSWORD_HASH；"
            "若只想本机使用，请设 WEBUI_HOST=127.0.0.1。",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    for note in notes:
        config.warn(note)
    for extra in config.warnings():
        if extra not in notes:
            config.warn(extra)

    if args.check:
        print(f"[webui] 配置校验通过：host={host} port={port}")
        print(f"[webui] 账本={config.ledger_file()}")
        print(f"[webui] 运行锁={config.run_lock_file()}")
        print(f"[webui] 冷却状态={config.rate_limit_file()}")
        print(f"[webui] 日志={config.log_file()}")
        return 0

    # ---------------------------------------------------------- 启动服务
    if args.reload:
        os.environ["WEBUI_HOST"] = host

    print(f"[webui] 启动仪表盘：http://{host}:{port}/", file=sys.stderr)
    print(f"[webui] 日志文件：{config.log_file()}", file=sys.stderr)

    import uvicorn  # 函数内 import：--check 不需要 uvicorn

    if args.reload:
        uvicorn.run(
            "webui.app:app",
            host=host,
            port=port,
            log_level=args.log_level,
            reload=True,
            reload_dirs=[str(config.REPO_ROOT / "webui")],
        )
    else:
        from webui.app import app

        uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
