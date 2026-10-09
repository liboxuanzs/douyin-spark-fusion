import logging
import os
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"

# 日志落盘位置固定为**仓库根目录**下的 logs/app.log。
# 不能用相对路径 "logs/app.log"：那是相对 CWD 的，从别的目录导入本项目时
# 会在那边凭空建一个 logs/，日志就散了。这里以本文件位置回推仓库根。
LOG_FILE = str(Path(__file__).resolve().parent.parent / "logs" / "app.log")

#: 所有 logger 共用的那**一个**文件 handler（`_file_handler()` 里惰性创建）。
_FILE_HANDLER = None


def _file_handler():
    """拿到那一个共用的文件 handler。

    ★ 为什么必须共用：`logs/app.log` 只有一份，而 `setup_logger` 会被多个模块
    各调一次（`utils/config.py` 的 app、`core/douyin_im.py` 的 douyin_im、
    `core/qr_login.py` 的 qr_login…）。以前是**每个 logger 名各建一个
    `RotatingFileHandler`** 指向同一个文件，于是轮转时互相踩：A 把 app.log
    改名成 app.log.1 之后，B 还握着旧 inode 往里写；B 再轮转一次，又把
    app.log 改名成 app.log.1 —— 把 A 刚归档的那一段直接覆盖掉。日志轮转本该
    "丢最旧的"，结果变成"丢刚写的"。

    ★ 为什么是普通 `FileHandler`：轮转归 `deploy/logrotate.conf` 管
    （`deploy/install.sh` 会装到 `/etc/logrotate.d/`，`copytruncate` + `rotate 14`
    + `maxsize 32M`，那份配置里写明了"进程不会重开日志文件，所以必须
    copytruncate"）。两边都管只会互相覆盖归档文件：logrotate 写 `app.log.1`，
    进程内的 `RotatingFileHandler` 也写 `app.log.1`。

    handler 被别人 `close()` 掉（测试的 tearDown 就会这么干）之后要能重建，
    否则后续所有日志都会往一个关掉的流里写。
    """
    global _FILE_HANDLER
    stream = getattr(_FILE_HANDLER, "stream", None)
    if _FILE_HANDLER is None or stream is None or getattr(stream, "closed", False):
        _FILE_HANDLER = logging.FileHandler(LOG_FILE, encoding="utf-8")
        # NOTSET = 不过滤，实际档位由各 logger 自己那层管（见 setup_logger）
        _FILE_HANDLER.setLevel(logging.NOTSET)
    return _FILE_HANDLER


def resolve_log_level(level):
    if isinstance(level, int):
        return level

    if isinstance(level, str):
        mapping = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        return mapping.get(level.lower(), logging.INFO)

    return logging.INFO


def setup_logger(name="app", level="Info"):
    resolved_level = resolve_log_level(level)
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(resolved_level)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    if not logger.handlers:
        console_handler = logging.StreamHandler()
        logger.addHandler(console_handler)
        logger.addHandler(_file_handler())

    shared = _file_handler()
    for handler in logger.handlers:
        handler.setFormatter(formatter)
        # 共用的文件 handler 不跟着单个 logger 的档位走 —— 它是全局的，
        # 后建的 logger（level=Info）会把先建的（level=Debug）的写盘档位一起
        # 改掉。它固定 NOTSET，各 logger 按自己的 level 过滤。
        if handler is not shared:
            handler.setLevel(resolved_level)

    return logger


if __name__ == "__main__":
    logger = setup_logger(level="Debug")
    logger.debug("这是一个调试信息")
    logger.info("这是一个普通信息")
    logger.warning("这是一个警告信息")
    logger.error("这是一个错误信息")
    logger.critical("这是一个严重错误信息")
