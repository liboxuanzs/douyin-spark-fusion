import os, sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True if os.environ.get("DEBUG", "").lower() == "true" else False
config = None
userData = None


def _env_int(name, default, minimum=None):
    """读一个整数环境变量：写错值不炸配置，回落到默认值并告警一次。

    裸 `int(os.getenv(...))` 遇上 `.env` 里手滑写的 `30 分钟` 会抛 ValueError，
    而那是在 `get_config()` 里 —— 整个进程连日志都打不出来就死了。
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = int(default)
    else:
        try:
            value = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            logger.warning(f"环境变量 {name}={raw!r} 不是整数，回落到默认值 {default}")
            value = int(default)
    if minimum is not None and value < minimum:
        logger.warning(f"环境变量 {name}={value} 小于下限 {minimum}，按 {minimum} 处理")
        value = int(minimum)
    return value


def _env_number(name, default, minimum=None):
    """读一个**可以是小数**的数字环境变量（秒数这类）：写错回落默认值并告警一次。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning(f"环境变量 {name}={raw!r} 不是数字，回落到默认值 {default}")
            value = float(default)
    if minimum is not None and value < minimum:
        logger.warning(f"环境变量 {name}={value} 小于下限 {minimum}，按 {minimum} 处理")
        value = float(minimum)
    return value


def _env_ms(name, default, minimum=None):
    """读一个以**秒**为单位的配置、出口换成**毫秒**（`BROWSER_ACTION_TIMEOUT` 这类）。

    走 `_env_number` 而不是 `_env_int`：`.env` 里写 `0.5` 表示半秒是合理的，
    先取整会把 0.5 变成 0，再被下限兜成 1 秒 —— 比原值大了一倍。
    """
    return int(_env_number(name, default, minimum=minimum) * 1000)


def _env_json(name, default, kind=None):
    """读一个 JSON 环境变量：解析失败（或类型不对）就回落默认值并告警一次。

    `json.loads(os.getenv(...))` 裸写在 `get_config()` 里，`.env` 手滑少写一个
    引号，整个进程就在 import 期没了 —— 连日志都来不及打。
    """
    raw = os.getenv(name)
    value = default
    if raw is not None and str(raw).strip():
        try:
            value = json.loads(str(raw))
        except (TypeError, ValueError) as exc:
            logger.warning(f"环境变量 {name}={raw!r} 不是合法 JSON（{exc}），回落到默认值 {default!r}")
            value = default
    if kind is not None and not isinstance(value, kind):
        logger.warning(f"环境变量 {name}={raw!r} 不是 {getattr(kind, '__name__', kind)}，回落到默认值 {default!r}")
        value = default
    return value


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv(
            "MESSAGE_TEMPLATE",
            "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]",
        ),
        "hitokotoTypes": _env_json(
            "HITOKOTO_TYPES", ["文学", "影视", "诗词", "哲学"], kind=list
        ),
        # .env 里统一用**秒**，出口按消费方的单位给：
        #   browserActionTimeout / friendListSettleMs 带单位后缀 → 已是毫秒，调用方直接用
        #   imScanTimeout / imReadyTimeout / imMaxSteps 原本就是秒/步
        # 下面这些以前是裸 `int(os.getenv(...))`：`.env` 里手滑写成 `30 分钟` 就会在
        # `get_config()` 里抛 ValueError —— 那是**首次读配置**的时刻，进程连日志都
        # 打不出来就死了。现在统一走 `_env_int` / `_env_ms` / `_env_json`：写错只告警。
        "browserActionTimeout": _env_ms(
            "BROWSER_ACTION_TIMEOUT", 120, minimum=1
        ),  # 单次浏览器操作/导航超时（Playwright 级），毫秒
        "imScanTimeout": _env_int(
            "IM_SCAN_TIMEOUT", 120, minimum=1
        ),  # 扫描总预算，秒
        "imReadyTimeout": _env_int(
            "IM_READY_TIMEOUT", 120, minimum=1
        ),  # 门禁等待上限，秒
        "friendListSettleMs": _env_ms(
            "FRIEND_LIST_WAIT_TIME", 3, minimum=0
        ),  # 资料静默窗，毫秒
        "imMaxSteps": _env_int(
            "IM_MAX_STEPS", 200, minimum=1
        ),  # 滚动步数硬上限
        # 每个目标"没拿到可信回执"后最多再试几次。**默认 1**：重试要重新选中并再
        # 合成一次发送（还可能等满一轮回执超时），线上一直就是这个节奏 ——
        # 2026-10-11 之前这个键在面板上可改、`core/` 里却 0 处消费，写几都只重试
        # 一次；现在真按它执行（见 core/tasks.py::_retry_budget）。
        "taskRetryTimes": _env_int("TASK_RETRY_TIMES", 1, minimum=0),
        "logLevel": os.getenv("LOG_LEVEL", "Debug"),  # 日志级别

        # ---- 空跑与限流熔断（融合新增，见 FUSION-DESIGN.md §5/§6）----------
        # 空跑档位：off=正常发送 / select=只确认选中 / full=真输入并读回校验。
        # 这一项**同时**是命令行与 webui 的默认值，实际档位由调用方显式传参覆盖。
        "dryRun": os.getenv("DRY_RUN", "off").strip().lower() or "off",
        # 熔断开关。关掉它 = 命中关键词也继续发，只在日志里留痕（排障用，不建议）
        "limitGuardEnabled": os.getenv("LIMIT_GUARD_ENABLED", "true").lower()
        not in ("0", "false", "no", "off"),
        "limitCooldownMinutes": _env_int(
            "LIMIT_COOLDOWN_MINUTES", 30, minimum=1
        ),  # 命中熔断后的冷却时长，分钟
        # "禁跑时间窗"：这段绝对不发，用来避免两个程序抢同一个抖音账号。
        # 默认 off —— 2026-10-01 起 .172 上只剩这一套在发，窗口没必要；
        # 真要用（比如夜里不想发）就写 "HH:MM-HH:MM"；留空 / off / none 表示不限制。
        # 注意：窗口盖住定时器时间会让那一轮整轮跳过，面板会报红。
        "quietWindow": os.getenv("QUIET_WINDOW", "off").strip(),
        # 两次发送之间的最小/最大间隔（秒）。0/0 = 不额外等待（保持 C 的原始节奏）；
        # cloakbrowser 的 humanize 本身就有拟人节奏，这里只是给"明显太快"兜底。
        "sendIntervalMinSeconds": float(os.getenv("SEND_INTERVAL_MIN_SECONDS", "8")),
        "sendIntervalMaxSeconds": float(os.getenv("SEND_INTERVAL_MAX_SECONDS", "20")),
        # 运行账本与运行锁（webui 也读这两个文件，见 core/run_ledger.py）
        "runLedgerFile": os.getenv("RUN_LEDGER_FILE", os.path.join("logs", "runs.jsonl")),
        "runLockFile": os.getenv("RUN_LOCK_FILE", os.path.join("logs", "run.lock")),
        "rateLimitFile": os.getenv("RATE_LIMIT_FILE", os.path.join("logs", "rate_limit.json")),

        # ---- 账号级发送时间表（见 core/schedule.py）------------------------
        # 到点之后在这个范围里再抖一下，免得几个账号卡在同一分钟发出去。
        # 0 = 严格按点。它同时决定 systemd timer 敲门的疏密（见 schedule.tick_minutes）。
        "scheduleJitterMinutes": _env_int("SCHEDULE_JITTER_MINUTES", 15, minimum=0),
        # "今天这个账号发过了"的账本。和 runLockFile 一样是相对 CWD 的路径。
        "scheduleStateFile": os.getenv(
            "SCHEDULE_STATE_FILE", os.path.join("logs", "schedule_state.json")
        ),
    }

    return config


#: 由"启动方"决定、**不**该被 `.env` 覆盖的键。
#: 它们是"这一轮怎么跑 / 服务怎么起"，不是配置本身：
#:   RUN_MODE / DRY_RUN —— 环境变量与命令行是显式覆盖（main.py 的文档承诺）
#:   RUN_SOURCE         —— 定时器 unit 注入的"谁触发的"；.env 三个入口共用，不该写它
#:   ENV_FILE           —— 指向另一份 .env 的开关（本模块自己读它）
PROCESS_WINS_KEYS = ("RUN_MODE", "DRY_RUN", "RUN_SOURCE", "ENV_FILE")


def load_env_file(path=None, *, override=True, keep=PROCESS_WINS_KEYS):
    """把 `.env` 读进进程环境，并清掉本模块的两个缓存。

    **文件是配置的唯一真相**：面板预览读的是这个文件、人手动改的也是这个文件，
    所以进程环境里冒出来的同名值一律可疑 —— 最典型的来源是 systemd 的
    `EnvironmentFile=`：它按"转义序列"解析值，`.env` 里消息模板写的字面 `\\n`
    会被吃成裸 `n`，发出去的消息就成了一整行（2026-10-02 那一轮的真实故障）。

    返回：
      * `None` —— 没读成（文件不存在，或者没装 python-dotenv）
      * `dict` —— 读了；`{KEY: (进程原值, 文件值)}`，只列"读之前进程里已经有
        一个不同值、且已被文件覆盖"的键。调用方拿它打日志 —— 这种"有人动过
        进程环境"的情况必须留痕，不能静默盖掉。

    `keep` 里的键不会被文件覆盖（读完之后原样还回去）。
    `override=False` 时文件不覆盖已有值（保留"导出的环境变量优先"的老语义），
    此时返回值恒为空 —— 因为什么都没被纠正，没啥可报的。

    已知限制（与旧 reload_env 相同）：`load_dotenv` 只能覆盖/新增，
    **从 .env 里删掉的键不会从环境变量里消失**（它没有删除语义）——
    删键请重启进程。
    """
    global config, userData

    path = path or os.getenv("ENV_FILE") or ".env"
    if not os.path.exists(path):
        return None
    try:
        from dotenv import dotenv_values, load_dotenv
    except Exception:  # pragma: no cover - 没装 dotenv 时保持原样
        return None

    try:
        file_values = dotenv_values(path) or {}
    except Exception:  # pragma: no cover - 文件读坏了就交给 load_dotenv 报
        file_values = {}

    kept = {k: os.environ[k] for k in keep if k in os.environ}
    drift = {}
    if override:
        for key, value in file_values.items():
            if key in kept or value is None:
                continue
            before = os.environ.get(key)
            if before is not None and before != value:
                drift[key] = (before, value)

    load_dotenv(path, override=override)
    for key, value in kept.items():  # 启动方的决定原样还回去
        os.environ[key] = value

    config = None
    userData = None
    return drift


def reload_env(env_file=None):
    """重新把 `.env` 读进环境变量，并清掉本模块的两个缓存。

    为什么需要它：`get_config()` / `get_userData()` 都是**首次调用即冻结**的
    模块级快照，而 webui 是常驻进程 —— 用户在面板外改完 `.env`
    （填进真实的 `TASKS` / `COOKIES_<uid>`）再回来点"立即运行"，
    不重读就还是启动那一刻的旧值，看起来就像"配置改了没用"。

    返回是否真的读了文件。语义与实现都在 `load_env_file()` 里
    （**文件优先于进程环境**，见那里的说明）。

    `DEBUG` 是 import 期算好的模块常量，这里不动它（它是
    `from utils.config import DEBUG` 被别处拿走的，中途换值只会更混乱）。
    """
    return load_env_file(env_file) is not None


def sanitize_cookies(cookies):
    for cookie in cookies:
        if "sameSite" in cookie:
            cookie.pop("sameSite")  # 移除 sameSite 字段，Playwright 可能不支持该字段
    return cookies


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    tasks = json.loads(os.getenv("TASKS", "[]"))
    
    if DEBUG:
        logger.info(f"读取到tasks：{tasks}")

    userData = []

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        fingerprint = task.get("fingerprint")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = (
            os.getenv(cookies_key, "").encode("utf-8").decode("unicode_escape")
        )
        if not cookies_str:
            logger.warning(f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过")
            continue
        try:
            cookies = json.loads(cookies_str)
        except json.JSONDecodeError:
            logger.warning(f"{username} 的任务 {cookies_key} 格式不正确，已跳过")
            continue

        userData.append(
            {
                "unique_id": unique_id,
                "username": username,
                "fingerprint": fingerprint,
                # 这个账号专属的发送内容（TASKS[].message）。空串 = 没配过，
                # 由 core/msg_builder.build_message 回落到全局 MESSAGE_TEMPLATE。
                # 和 targets 一样：读取端原样带出去，不做加工。
                "message": "" if task.get("message") is None else str(task.get("message")),
                # 这个账号专属的发送时间（TASKS[].time，"HH:MM"）。空串 = 没配过，
                # 由 core/schedule 回落到 .env 的 TASK_SCHEDULE_TIMES。
                # 这里**故意不校验格式**（和 targets 同理：读取端不加工）——
                # 校验收敛在 webui/envfile.set_account_time 一处，
                # 手写进 .env 的怪值由 schedule.parse_time 兜底当"没配"。
                "time": "" if task.get("time") is None else str(task.get("time")),
                "cookies": sanitize_cookies(cookies),
                # 目标列表保持原样（不在这里归一化）。
                # 归一化统一由 tasks.py 在匹配前做 —— 读取端不做加工，
                # 配置读出来什么就是什么，避免同一份数据两处变换。
                "targets": list(task.get("targets", [])),
            }
        )

    return userData
