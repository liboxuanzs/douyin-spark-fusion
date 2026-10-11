"""webui 的环境变量访问层 —— 全部**惰性读取**，绝不缓存在模块级。

为什么坚持惰性：``import webui.app`` 必须能成功（本机没装 playwright/cloakbrowser），
而且测试要能 monkeypatch 环境变量。任何形如 ``HOST = os.getenv(...)`` 的模块级常量
都会把这个能力毁掉，所以本文件里只有函数，没有常量。

环境变量与默认值一览（详见 docs/webui.md）：

===============================  ==========================  ==================================
变量                              默认值                       说明
===============================  ==========================  ==================================
WEBUI_HOST                       127.0.0.1                   监听地址
WEBUI_PORT                       8787                        监听端口
WEBUI_PASSWORD                   （空）                       明文口令
WEBUI_PASSWORD_HASH              （空）                       优先于明文，pbkdf2_sha256$salt$digest
WEBUI_AUTH                       pass                         off 时关闭认证（仅回环地址允许）
WEBUI_SESSION_SECRET             （随机）                     签名 cookie 的密钥
WEBUI_SESSION_MAX_AGE            28800                        会话有效期（秒）
WEBUI_COOKIE_NAME                dsf_webui_session            会话 cookie 名
WEBUI_TZ                         Asia/Shanghai                展示用时区
RUN_LEDGER_FILE                  logs/runs.jsonl              运行账本（每行一条 JSON）
RATE_LIMIT_FILE                  logs/rate_limit.json         熔断/冷却状态
RUN_LOCK_FILE                    logs/run.lock                运行锁
OPS_LOG_FILE                     logs/app.log                 日志文件（与 utils.logger 同一份）
TASK_SCHEDULE_TIMES              09:00 “下次定时”预览用，逗号分隔的 HH:MM，空则未知
===============================  ==========================  ==================================
"""

from __future__ import annotations

import ipaddress
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

__all__ = [
    "ConfigError",
    "DEFAULT_LOG_FILE",
    "DEFAULT_SCHEDULE_TIMES",
    "credential_fingerprint",
    "default_timezone",
    "is_loopback_host",
    "ledger_file",
    "load_timezone",
    "log_file",
    "normalize_bind_host",
    "password_hash",
    "password_plain",
    "port",
    "preflight",
    "rate_limit_file",
    "run_lock_file",
    "schedule_times",
    "session_cookie_name",
    "session_max_age",
    "session_secret",
    "webui_auth_mode",
    "webui_host",
    "warn",
    "warnings",
]

# 仓库根 = 本文件（webui/config.py）的上两级目录。
REPO_ROOT = Path(__file__).resolve().parent.parent

# 日志默认落在 utils/logger.py 写的那一份（logs/app.log），这样仪表盘的"日志尾部"
# 看到的就是任务真实输出。C 的 Docker 部署把这个目录挂到 /app/logs。
DEFAULT_LOG_FILE = "logs/app.log"
# 与 core/schedule.py 的 DEFAULT_TIME 对齐：派发器模式下账号没写 time 时按 20:30 发，
# 面板的"下次定时"预览若还按 09:00 算，用户会看到一个永远不会发生的时刻。
DEFAULT_SCHEDULE_TIMES = "20:30"


class ConfigError(RuntimeError):
    """启动前校验失败（中文原因，供 webui.__main__ 打印并以退出码 2 结束）。"""


# ---------------------------------------------------------------- 通用取值


def _getenv(name: str, default: str = "", aliases: Optional[List[str]] = None) -> str:
    """惰性读环境变量；别名按顺序兜底（先命中的赢）。"""
    names = [name] + list(aliases or [])
    for candidate in names:
        value = os.getenv(candidate)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def resolve_path(value: str) -> Path:
    """相对路径按仓库根解析，绝对路径原样返回。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


# ---------------------------------------------------------------- 启动期警告


def warnings() -> List[str]:
    """启动期提示列表（非致命），供 __main__ 打印。"""
    items: List[str] = []
    if webui_auth_mode() == "off":
        items.append(
            "WEBUI_AUTH=off：仪表盘**未启用认证**，任何能访问该端口的人都能看到账号与运行明细。"
            "仅可在回环地址（127.0.0.1）上这样跑。"
        )
    if not session_secret_configured():
        items.append(
            "未设置 WEBUI_SESSION_SECRET：已改为进程内随机生成，"
            "重启后所有已登录会话都会失效（想保持登录请把它写进 .env）。"
        )
    if not password_plain() and not password_hash():
        items.append(
            "未设置 WEBUI_PASSWORD / WEBUI_PASSWORD_HASH：当前没有任何口令，"
            "只能绑回环地址；要绑 0.0.0.0 必须先设口令。"
        )
    return items


def warn(message: str) -> None:
    """把一条警告打到 stderr（不引入 logging 配置，避免和 utils.logger 抢 handler）。"""
    import sys

    print(f"[webui][警告] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 监听地址


def webui_host() -> str:
    """监听地址，默认只绑回环。"""
    return _getenv("WEBUI_HOST", "127.0.0.1")


def port() -> int:
    """监听端口，默认 8787。"""
    raw = _getenv("WEBUI_PORT", "8787")
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - 取决于环境
        raise ConfigError(f"WEBUI_PORT 不是合法整数：{raw!r}") from exc
    if not 1 <= value <= 65535:
        raise ConfigError(f"WEBUI_PORT 超出 1-65535：{value}")
    return value


def normalize_bind_host(host: str) -> str:
    """把常见写法归一，便于判断是不是回环地址。"""
    value = (host or "").strip()
    if value in {"*", "0.0.0.0", "::", "[::]", ""}:
        return "0.0.0.0"
    return value


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------- 口令


def password_plain() -> str:
    """WEBUI_PASSWORD 明文口令（未设返回空串）。"""
    return os.getenv("WEBUI_PASSWORD", "") or ""


def password_hash() -> str:
    """WEBUI_PASSWORD_HASH 哈希口令（优先于明文，未设返回空串）。"""
    return os.getenv("WEBUI_PASSWORD_HASH", "") or ""


def credential_fingerprint() -> str:
    """当前口令的指纹（16 位十六进制；没设口令时返回空串）。

    用途只有一个：会话 cookie 里带上它，**口令一改，所有已发出的 cookie 立刻作废**
    （否则"改了口令"只对新登录生效，别人手里那 8 小时的会话还能继续用）。
    指纹是口令/哈希的 SHA256 前缀，反推不出口令；口令没变时它也不变，
    所以进程重启、`.env` 重读都不会把用户踢下线。
    """
    encoded = password_hash()
    plain = password_plain()
    if encoded:
        raw = f"hash:{encoded}"
    elif plain:
        raw = f"plain:{plain}"
    else:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def webui_auth_mode() -> str:
    """``off`` 表示关闭认证；其它值（默认 pass）表示要求口令。"""
    return "off" if os.getenv("WEBUI_AUTH", "").strip().lower() == "off" else "pass"


# ---------------------------------------------------------------- 会话


def session_cookie_name() -> str:
    return _getenv("WEBUI_COOKIE_NAME", "dsf_webui_session")


def session_max_age() -> int:
    raw = _getenv("WEBUI_SESSION_MAX_AGE", "28800")
    try:
        value = int(raw)
    except ValueError:
        return 28800
    return value if value > 0 else 28800


def session_secret_configured() -> bool:
    return bool(os.getenv("WEBUI_SESSION_SECRET", "").strip())


_SESSION_SECRET_CACHE: Optional[str] = None


def session_secret() -> str:
    """签名密钥：优先环境变量，缺省时随机生成并只警告一次。

    注意：缓存的是**随机生成**的那一份，环境变量每次仍然重新读取，
    这样测试里 monkeypatch WEBUI_SESSION_SECRET 立刻生效。
    """
    global _SESSION_SECRET_CACHE
    configured = os.getenv("WEBUI_SESSION_SECRET", "").strip()
    if configured:
        return configured
    if _SESSION_SECRET_CACHE is not None:
        return _SESSION_SECRET_CACHE
    _SESSION_SECRET_CACHE = secrets.token_urlsafe(48)
    warn(
        "未设置 WEBUI_SESSION_SECRET，本次运行使用随机密钥；"
        "进程重启后所有登录会话都会失效。"
    )
    return _SESSION_SECRET_CACHE


# ---------------------------------------------------------------- 启动前校验


def preflight(host: Optional[str] = None, auth_mode: Optional[str] = None) -> List[str]:
    """启动前校验：不允许裸奔，返回需要打印的警告列表；不合法时抛 ConfigError。

    规则（验收口径）：

    * 绑非回环地址（如 0.0.0.0）且没有配置任何口令 → 拒绝启动；
    * ``WEBUI_AUTH=off`` 只在回环地址上允许；
    * 回环 + 无口令 → 允许启动，但返回"未启用认证"的明确警告。
    """
    bind_host = normalize_bind_host(webui_host() if host is None else host)
    mode = webui_auth_mode() if auth_mode is None else auth_mode.strip().lower()
    loopback = is_loopback_host(bind_host)
    has_password = bool(password_plain()) or bool(password_hash())

    if mode == "off" and not loopback:
        raise ConfigError(
            f"拒绝启动：WEBUI_AUTH=off（关闭认证）只能绑回环地址，当前 WEBUI_HOST={bind_host}。\n"
            "  要么把 WEBUI_HOST 改回 127.0.0.1，要么去掉 WEBUI_AUTH=off 并设置 WEBUI_PASSWORD"
            "（或 WEBUI_PASSWORD_HASH）。"
        )

    if not loopback and not has_password:
        raise ConfigError(
            f"拒绝启动：WEBUI_HOST={bind_host} 是外部可访问地址，但没有配置访问口令，"
            "仪表盘不允许裸奔。\n"
            "  请在 .env 里设置 WEBUI_PASSWORD（明文，简单）或 WEBUI_PASSWORD_HASH（推荐，"
            "用 python -m webui.hash_password 生成）。"
        )

    items = warnings()

    if loopback and not has_password:
        items.append(
            f"未启用认证（WEBUI_HOST={bind_host} 为回环地址，无口令也能启动）："
            "同机能访问该端口的任何程序都能看到账号名、目标列表与运行明细。"
        )

    if password_hash() and not password_hash().startswith("pbkdf2_sha256$"):
        raise ConfigError(
            "WEBUI_PASSWORD_HASH 格式不正确：应为 pbkdf2_sha256$<salt_hex>$<digest_hex>。\n"
            "  用 python -m webui.hash_password 重新生成。"
        )

    return items


# ---------------------------------------------------------------- 运行数据文件


def ledger_file() -> Path:
    """运行账本（每行一条 JSON）。"""
    return resolve_path(_getenv("RUN_LEDGER_FILE", "logs/runs.jsonl"))


def rate_limit_file() -> Path:
    """熔断/冷却状态文件。"""
    return resolve_path(_getenv("RATE_LIMIT_FILE", "logs/rate_limit.json"))


def run_lock_file() -> Path:
    """运行锁文件。"""
    return resolve_path(_getenv("RUN_LOCK_FILE", "logs/run.lock"))


def log_file() -> Path:
    """日志文件路径（默认与 utils.logger 落盘位置一致）。"""
    return resolve_path(_getenv("OPS_LOG_FILE", DEFAULT_LOG_FILE))


def log_file_candidates() -> List[Path]:
    """日志候选路径 —— **只认配置的那一份**（``OPS_LOG_FILE``，默认 ``logs/app.log``）。

    这里以前会无条件追加默认路径当兜底候选，本意是"配置的读不到就退回默认那份"，
    实际后果是**配置错误被悄悄掩盖**：``OPS_LOG_FILE`` 指向的文件不存在时，页面会改读
    仓库里恰好存在的另一个日志文件并显示"已读取"，``/ops/logs/download`` 也返回 200，
    运维完全看不出路径写错了，还可能把无关日志的内容展示给看板用户。

    现在只返回配置的那一份：文件不存在就如实报"日志文件不存在"
    （页面明确提示 / 下载接口 404）。部署侧请把 ``OPS_LOG_FILE`` 指到真实文件
    （systemd 部署下是定时任务的标准输出，见 .env.example）。
    """
    return [log_file()]


# ---------------------------------------------------------------- 时区与定时


def default_timezone() -> str:
    return _getenv("WEBUI_TZ", "Asia/Shanghai")


def load_timezone(name: Optional[str] = None) -> tzinfo:
    """拿到 tzinfo；tzdata 缺失或名字非法时回落到固定 +08:00，绝不抛异常。"""
    tz_name = name if name is not None else default_timezone()
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone(timedelta(hours=8), "UTC+08:00")


def schedule_times() -> List[str]:
    """每日定时时间点（HH:MM，逗号分隔），仅用于"下次定时"的预览展示。

    真正的定时由 systemd timer / cron / Docker 的 CRON_* 负责，仪表盘不自己调度。
    留空表示未知 → 页面显示"未配置"。
    """
    raw = _getenv("TASK_SCHEDULE_TIMES", DEFAULT_SCHEDULE_TIMES, aliases=["WEBUI_TASK_SCHEDULE"])
    items = [part.strip() for part in raw.split(",") if part.strip()]
    valid: List[str] = []
    for item in items:
        try:
            hour_text, minute_text = item.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            valid.append(f"{hour:02d}:{minute:02d}")
    return valid


def next_run_at(now: Optional[datetime] = None) -> Optional[datetime]:
    """按 TASK_SCHEDULE_TIMES 算出下一次定时时间；未配置则 None。"""
    times = schedule_times()
    if not times:
        return None
    tz = load_timezone()
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    best: Optional[datetime] = None
    for item in times:
        hour, minute = (int(part) for part in item.split(":", 1))
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        if best is None or candidate < best:
            best = candidate
    return best
