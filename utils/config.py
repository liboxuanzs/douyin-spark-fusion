import os, sys
from enum import Enum
import hashlib
import json
import logging
import math
import time
from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True if os.environ.get("DEBUG", "").lower() == "true" else False
config = None
userData = None

#: 本模块与 `utils.logger`（`LOG_FILE`）共用的"仓库根"。以文件位置回推，
#: 不依赖 CWD —— systemd unit 的 `WorkingDirectory=`、docker 的 `cd /app`、
#: 以及"在别的目录里 `python /opt/douyin-spark-fusion/main.py task`"三种起法
#: 的 CWD 各不相同，用相对路径就会让账本/锁落到别处（锁失效 → 两个浏览器
#: 同时开同一个账号，重复发送）。
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 发送时间表默认时区。宿主是 UTC 而这里是 `Asia/Shanghai` 时，不调 tzset()
#: 会让整张时间表平移 8 小时（见 `_apply_tz`）。
DEFAULT_TZ = "Asia/Shanghai"


def resolve_path(value):
    """把配置里的路径落到**仓库根**下：相对路径按仓库根解析，绝对路径原样返回。

    与 `webui.config.resolve_path` 是同一套语义（那边也写了"相对路径按仓库根解析"），
    这里是给不走 webui 的消费方（`core/run_ledger.py` 的账本/锁等）用的。
    """
    if not value:
        return value
    expanded = os.path.expanduser(str(value))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(REPO_ROOT, expanded)


#: 上一次已经 `tzset()` 过的 TZ 值；None = 还没有（首次一定执行）。
_TZ_APPLIED = None


def _apply_tz(force=False):
    """把 `TZ`（默认 `Asia/Shanghai`）落进 libc 的时区缓存：必要时调一次 `time.tzset()`。

    ★ 为什么不能只在"读完了 `.env`"之后调：`.env` 不存在、或者没装 python-dotenv
    时**照样**要生效。宿主时区是 UTC 的部署机上，不调 tzset() 的后果是整张发送
    时间表平移 8 小时 —— 面板按 WEBUI_TZ 显示 20:30，真正发车却在 12:30，而
    这一轮恰好一条日志都不报错，只能靠看时间戳发现。

    ★ 为什么要把 `TZ` **写回** `os.environ`：`tzset()` 认的就是这个变量 —— 它没设时
    tzset() 按宿主的 `/etc/localtime` 落地（实测：`os.environ.pop("TZ")` 之后
    `tzset()` 得到的是宿主时区），"默认 Asia/Shanghai"这条就等于没生效，UTC 宿主上
    照旧平移 8 小时。回写也让子进程（浏览器、cron、`python main.py task`）看到同一个
    时区，不再各按各的算。

    ★ 为什么默认只在 TZ 变了的时候真调：tzset() 会**重新读一遍 tzdata**，而
    `load_env_file()` 在常驻的 webui 里每存一次配置都会被调到（见
    `webui.envfile` → `utils.config.reload_env`），每次多一次系统调用没必要。
    这里用模块级缓存记住"上一次生效的 TZ"，只有首次或值真的变了才落地 ——
    同时也让"同一份 .env 连续读两次"在测试里可断言（tzset 只该被调一次）。

    ★ `force=True` 用在**真的读到了 `.env`** 那条路径上（见 `load_env_file` 末尾）：
    文件是 TZ 的权威来源，而"值比较"分不出"这个值来自宿主/默认"与"这个值刚从 .env
    里读出来"（`main.py` 在 import 期就先读过一次，缓存此时已经热了）。

    Windows 上没有 `tzset`（项目本身支持 Windows）：直接跳过，否则 import 期就会
    AttributeError。
    """
    global _TZ_APPLIED
    tz = os.environ.get("TZ") or DEFAULT_TZ
    if not force and tz == _TZ_APPLIED:
        return
    if os.name == "posix" and hasattr(time, "tzset"):
        os.environ["TZ"] = tz          # 必须先回写：tzset() 认的是它，不是 tz 这个局部量
        try:
            time.tzset()
        except Exception:  # pragma: no cover - 极端环境没有 tzdata 也不能拖垮启动
            pass
    _TZ_APPLIED = tz


def _env_int(name, default, minimum=None, maximum=None):
    """读一个整数环境变量：写错值不炸配置，回落到默认值并告警一次。

    裸 `int(os.getenv(...))` 遇上 `.env` 里手滑写的 `30 分钟` 会抛 ValueError，
    而那是在 `get_config()` 里 —— 整个进程连日志都打不出来就死了。

    非有限值也要挡住：`float("1e999")` 是合法的 inf，`int(inf)` 抛的是
    **OverflowError**（不是 ValueError），漏掉它同样会在读配置时炸。
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = int(default)
    else:
        try:
            value = int(float(str(raw).strip()))
        except (TypeError, ValueError, OverflowError):
            logger.warning(f"环境变量 {name}={raw!r} 不是整数，回落到默认值 {default}")
            value = int(default)
    if minimum is not None and value < minimum:
        logger.warning(f"环境变量 {name}={value} 小于下限 {minimum}，按 {minimum} 处理")
        value = int(minimum)
    if maximum is not None and value > maximum:
        # 上限不是洁癖：`LIMIT_COOLDOWN_MINUTES` 写成一个手滑的大数，机器人就
        # 静悄悄停机到那个时刻（timer exit 0、账本零痕迹），只有面板横幅能看出来。
        logger.warning(f"环境变量 {name}={value} 超过上限 {maximum}，按 {maximum} 处理")
        value = int(maximum)
    return value


def _env_number(name, default, minimum=None, maximum=None):
    """读一个**可以是小数**的数字环境变量（秒数这类）：写错回落默认值并告警一次。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError, OverflowError):
            logger.warning(f"环境变量 {name}={raw!r} 不是数字，回落到默认值 {default}")
            value = float(default)
        else:
            if not math.isfinite(value):
                # `1e999` → inf / `nan` → nan：都能过 float()，但拿去比大小、
                # 做间隔、算冷却全是错的（nan 的比较恒为 False，会静默失效）。
                logger.warning(f"环境变量 {name}={raw!r} 不是有限数字，回落到默认值 {default}")
                value = float(default)
    if minimum is not None and value < minimum:
        logger.warning(f"环境变量 {name}={value} 小于下限 {minimum}，按 {minimum} 处理")
        value = float(minimum)
    if maximum is not None and value > maximum:
        logger.warning(f"环境变量 {name}={value} 超过上限 {maximum}，按 {maximum} 处理")
        value = float(maximum)
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
        # `Info` 与 main.py 的 `setup_logger(level=os.getenv("LOG_LEVEL", "Info"))`
        # 以及 configTool/models.DEFAULT_LOG_LEVEL 对齐：以前这里写 "Debug"，
        # 于是"没设 LOG_LEVEL"的进程按 Debug 落盘（日志量差一个量级），而面板与
        # 文档都按 Info 说；生产 .env 里显式写了 LOG_LEVEL，不受这一改动影响。
        "logLevel": os.getenv("LOG_LEVEL", "Info"),  # 日志级别

        # ---- 空跑与限流熔断（融合新增，见 FUSION-DESIGN.md §5/§6）----------
        # 空跑档位：off=正常发送 / select=只确认选中 / full=真输入并读回校验。
        # 这一项**同时**是命令行与 webui 的默认值，实际档位由调用方显式传参覆盖。
        "dryRun": os.getenv("DRY_RUN", "off").strip().lower() or "off",
        # 熔断开关。关掉它 = 命中关键词也继续发，只在日志里留痕（排障用，不建议）
        "limitGuardEnabled": os.getenv("LIMIT_GUARD_ENABLED", "true").lower()
        not in ("0", "false", "no", "off"),
        "limitCooldownMinutes": _env_int(
            "LIMIT_COOLDOWN_MINUTES", 30, minimum=1, maximum=1440
        ),  # 命中熔断后的冷却时长，分钟（上限 24 小时，与面板 settings.py 的 maximum 对齐）
        # "禁跑时间窗"：这段绝对不发，用来避免两个程序抢同一个抖音账号。
        # 默认 off —— 2026-10-01 起 本机 上只剩这一套在发，窗口没必要；
        # 真要用（比如夜里不想发）就写 "HH:MM-HH:MM"；留空 / off / none 表示不限制。
        # 注意：窗口盖住定时器时间会让那一轮整轮跳过，面板会报红。
        "quietWindow": os.getenv("QUIET_WINDOW", "off").strip(),
        # 两次发送之间的最小/最大间隔（秒）。0/0 = 不额外等待（保持 C 的原始节奏）；
        # cloakbrowser 的 humanize 本身就有拟人节奏，这里只是给"明显太快"兜底。
        # 这两行曾是全模块最后两处裸 `float(os.getenv(...))`：`.env` 里写成
        # `8 秒` / `8s` / `1,5`，`get_config()` 直接抛 ValueError —— 而那是**首次
        # 读配置**的时刻，进程连日志都打不出来就死了（面板也跟着挂）。
        "sendIntervalMinSeconds": _env_number(
            "SEND_INTERVAL_MIN_SECONDS", 8, minimum=0, maximum=3600
        ),
        "sendIntervalMaxSeconds": _env_number(
            "SEND_INTERVAL_MAX_SECONDS", 20, minimum=0, maximum=3600
        ),        # ---- 节奏人性化（2026-10-24，见 FUSION-DESIGN.md §13）----------------
        # 定时器到点后先随机等 0~N 秒再开浏览器：门铃每 5 分钟**整分**敲一次，不磨一
        # 下的话"每天开始发送"的时刻永远是 :00/:05/:10，一年下来没有一次落在 :03。
        # 只对 timer 生效（见 core/tasks.py::_start_delay_seconds），手动触发不等。
        "runStartJitterSeconds": _env_number(
            "RUN_START_JITTER_SECONDS", 90, minimum=0, maximum=600
        ),
        # 长尾停顿：约 15% 的概率在正常间隔之外再多停 0~N 秒
        # （见 core/limit_guard.py::sample_gap_seconds）。0 = 退回纯均匀分布。
        "sendIntervalLongPauseSeconds": _env_number(
            "SEND_INTERVAL_LONG_PAUSE_SECONDS", 75, minimum=0, maximum=600
        ),
        # 暖场浏览：门禁通过后先在会话列表里滚 N 秒再发（0 = 关闭，默认关）。
        "warmupBrowseSeconds": _env_number(
            "WARMUP_BROWSE_SECONDS", 0, minimum=0, maximum=120
        ),
        # 分段输入：一条消息按 N 个字切块打进去、块间停 M 毫秒（0 = 一次打完整段）。
        # 不切块时整段消息在**一帧之内**出现在输入框里，真人不会这样。
        "typingChunkChars": _env_int(
            "TYPING_CHUNK_CHARS", 6, minimum=0, maximum=200
        ),
        "typingChunkDelayMs": _env_int(
            "TYPING_CHUNK_DELAY_MS", 110, minimum=0, maximum=2000
        ),
        # 运行账本与运行锁（webui 也读这两个文件，见 core/run_ledger.py）。
        # 相对路径按**仓库根**解析（与 .env.example 里写的"相对路径按仓库根解析"、
        # webui.config.resolve_path 一致）：按 CWD 解析时，从别的目录执行
        # `python /opt/douyin-spark-fusion/main.py task` 会把运行锁落到那个目录，
        # 锁失效 = 定时任务与手动触发可能同时开两个浏览器发同一个账号。
        "runLedgerFile": resolve_path(
            os.getenv("RUN_LEDGER_FILE", os.path.join("logs", "runs.jsonl"))
        ),
        "runLockFile": resolve_path(
            os.getenv("RUN_LOCK_FILE", os.path.join("logs", "run.lock"))
        ),
        "rateLimitFile": resolve_path(
            os.getenv("RATE_LIMIT_FILE", os.path.join("logs", "rate_limit.json"))
        ),

        # ---- 账号级发送时间表（见 core/schedule.py）------------------------
        # 到点之后在这个范围里再抖一下，免得几个账号卡在同一分钟发出去。
        # 0 = 严格按点。它同时决定 systemd timer 敲门的疏密（见 schedule.tick_minutes）。
        "scheduleJitterMinutes": _env_int("SCHEDULE_JITTER_MINUTES", 15, minimum=0),
        # "今天这个账号发过了"的账本。与上面三个同订：相对路径按仓库根解析，
        # 免得从别的 CWD 起进程时读到另一份"今天还没发过"的状态（= 当天重发）。
        # ⚠️ 真正写它的是 `core/schedule.py::state_path`（不归本次改动），那边仍按
        # CWD 解析；只有消费方（webui/面板）读这里时才会被锚到仓库根。
        "scheduleStateFile": resolve_path(
            os.getenv("SCHEDULE_STATE_FILE", os.path.join("logs", "schedule_state.json"))
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
    # 无论文件读没读到（下面两条 return）、dotenv 装没装，时区都要先落地：
    # 这两条 return 以前在 tzset() **之前**，等价于"没有 .env 就不认 TZ"。
    #
    # ⚠️ 这里**只**落地时区，刻意不顺手清 `config`/`userData`：清缓存必须发生在
    # "真的读到文件"之后（见函数末尾）。理由是 `load_dotenv` 只有新增/覆盖语义、
    # **没有删除语义** —— 把 `.env` 删掉之后进程环境里仍然留着上一次读进来的
    # `TASKS`/`COOKIES_*`，所以"文件不在也清缓存"根本不会让账号列表变空，只会让
    # 每个请求（面板每次请求都 `refresh_env()`）都白解析一遍；而调用方打桩
    # `utils.config.userData` 的场景（见 tests/test_webui.py 的 `_patch_utils_config`）
    # 会被无条件清掉，整批面板用例变成"没有账号"。
    _apply_tz()
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

    # `.env` 里的 TZ 光写进 os.environ 是不够的：libc 把时区缓存在进程里，读完要
    # 调一次 tzset() 才生效。不调的话调度与日志仍按**宿主**时区算，而面板按
    # WEBUI_TZ 显示 —— 用户在面板上看到的时间和真正发车的时刻差几个小时，还查不出
    # 为什么（全仓以前没有一处 tzset，所以这一条在文档里写了却一直没生效）。
    # 这一次**强制**落地：文件是 TZ 的权威来源，而"值比较"分不出"这个 TZ 来自宿主/
    # 默认值"与"它刚刚从 .env 里读出来"——`main.py` 在 import 期就先跑过一次
    # `load_env_file()`，缓存这时已经热了。被"值没变就跳过"挡掉的仍然是**没读到
    # .env** 的那些调用（上面那一次：常驻 webui 每保存一次配置都会 reload_env()，
    # 不该每次都重读 tzdata）。
    _apply_tz(force=True)

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


def _brief(raw, limit=160):
    """把一段用户写的配置原文截断成能进日志的一行（不换行、不过长）。"""
    text = " ".join(str(raw or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


#: 键名里带这些词的一律当凭据：值不进日志，只报长度与指纹。
SECRET_KEY_HINTS = ("COOKIE", "PASSWORD", "PASSWD", "SECRET", "TOKEN", "SIGNATURE", "HASH")


def _secret_key(key) -> bool:
    upper = str(key or "").upper()
    return any(hint in upper for hint in SECRET_KEY_HINTS)


def _fingerprint(raw) -> str:
    """凭据的指纹：长度 + 前 8 位 sha256。用来判断"变没变"，反推不出原文。"""
    text = str(raw or "")
    return f"sha256:{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:8]}"


def drift_report(drift, limit=160) -> str:
    """把 `load_env_file()` 的 drift 拼成一句**不泄密**的日志。

    以前这里直接把两侧的原始值打进日志，而 `.env` 里的 `COOKIES_<抖音号>` 是一整份
    登录凭据（sessionid）—— 等于每启动一次就往 logs/app.log 里抄一份完整登录态。
    凭据键改报"长度 + 指纹"（足够看出被人改过），其余键照旧报值：像 MESSAGE_TEMPLATE
    这种正是要靠原始值才能看出被 systemd 吃掉反斜杠的。
    """
    parts = []
    for key, (before, after) in sorted((drift or {}).items()):
        if _secret_key(key):
            parts.append(f"{key} 进程=<隐藏 {len(str(before or ''))} 字符 "
                         f"{_fingerprint(before)}> → 文件=<隐藏 {len(str(after or ''))} 字符 "
                         f"{_fingerprint(after)}>")
        else:
            parts.append(f"{key} 进程={_brief(before, limit)!r} → 文件={_brief(after, limit)!r}")
    return "；".join(parts)


#: `TASKS[].enabled` 里算"停用"的写法。留这么多同义写法不是为了宽容：这一项
#: 经常被**手写**进 `.env`（面板只是其中一条路径），把 false 写成 0/off/no 太正常了，
#: 把它们当"没写"处理会静默变成"启用"，等于停用失效。
_TASK_DISABLED = {"false", "0", "no", "off", "disabled", "stop", "stopped", "停用", "暂停"}
_TASK_ENABLED = {"true", "1", "yes", "on", "enabled", "enable", "启用", "恢复"}


def task_enabled(task):
    """`TASKS` 里这一项现在启用没有（`enabled` 缺键 = 启用，向后兼容）。

    ★ 缺键必须是启用：老 `.env` 里根本没有这一项，按"停用"处理等于升级当天
      所有账号静默停发（定时器照敲、日志照写"没有账号到点"），最难查的一类故障。
    ★ 认不出的值也按启用 + 告警：宁可多发一天，也不要因为一处拼写写错让账号
      整天静默 —— 停用本来就是"人想少发"，错了立刻会有人发现。
    """
    if not isinstance(task, dict):
        return True
    raw = task.get("enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    text = str(raw).strip().lower()
    if not text or text in _TASK_ENABLED:
        return True
    if text in _TASK_DISABLED:
        return False
    logger.warning(
        f"TASKS 里 {task.get('unique_id') or task.get('username') or '?'} 的 "
        f"enabled 值看不懂（{raw!r}），按启用处理"
    )
    return True


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    # TASKS 是手改 .env 时最容易写坏的一项（中文逗号、少引号、尾逗号），而它是
    # **整轮任务**的输入：裸 `json.loads` 抛出的 JSONDecodeError 会从本函数直接
    # 冒到调用方 —— 定时器每 5 分钟 exit 1、`runs.jsonl` 零记录、面板「立即运行」
    # 503，日志里只有一行 traceback，看不出是 TASKS 的错。这里改成"解析失败 →
    # 当空名单 + 明确告警"，让轮次照常跑完并留下痕迹（谁都没配 vs 配错了，
    # 日志里一眼能分）。
    raw_tasks = os.getenv("TASKS", "[]")
    try:
        tasks = json.loads(raw_tasks)
    except (TypeError, ValueError) as exc:
        logger.error(
            f"⛔ TASKS 不是合法 JSON（{exc}）—— 本轮按「没有账号」处理，"
            f"请在面板或 .env 里修正 TASKS。原文：{_brief(raw_tasks)!r}"
        )
        tasks = []
    if isinstance(tasks, dict):
        # 只配了一个账号时手写成对象是很自然的写法，顺手兼容掉。
        tasks = [tasks]
    if not isinstance(tasks, list):
        logger.error(
            f"⛔ TASKS 的顶层必须是数组，实际是 {type(tasks).__name__}，"
            f"本轮按「没有账号」处理"
        )
        tasks = []

    if DEBUG:
        logger.info(f"读取到tasks：{tasks}")

    # ★ 先在**局部**变量里建好，最后一次性赋给全局名。`userData = []` 之后再逐个
    #   append，等于把半成品挂在全局名上：面板每个请求都会
    #   `refresh_env()` → `userData = None` → 重新调用本函数，而运行线程也会
    #   `importlib.reload(core.tasks)`（模块体里就调本函数）—— 两个线程会互相往
    #   对方刚建的那个 list 里 append，账号名单因此可能重复或缺失
    #   （重复 = 同一轮里同一个好友收到两条火花）。局部构建没有这个窗口。
    data = []

    for task in tasks:
        if not isinstance(task, dict):
            logger.warning(
                f"TASKS 里有一项不是对象（{type(task).__name__}），已跳过：{_brief(task)!r}"
            )
            continue
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

        targets = task.get("targets")
        if targets is None:
            # `"targets": null` 是手写 JSON 的常见形态：`list(None)` 会抛 TypeError，
            # 而它和"写坏了"一样应该只是"这个账号没配目标"。
            targets = []
        if isinstance(targets, (str, bytes)) or not isinstance(targets, (list, tuple)):
            logger.warning(
                f"{username} 的任务 targets 不是数组（{type(targets).__name__}），已按空处理"
            )
            targets = []

        data.append(
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
                # 停用开关（TASKS[].enabled）。缺键 = 启用，见 task_enabled()。
                # 读取端只翻成布尔，语义（不自动派发）由 core/schedule.py 实现。
                "enabled": task_enabled(task),
                "cookies": sanitize_cookies(cookies),
                # 目标列表保持原样（不在这里归一化）。
                # 归一化统一由 tasks.py 在匹配前做 —— 读取端不做加工，
                # 配置读出来什么就是什么，避免同一份数据两处变换。
                "targets": list(targets),
            }
        )

    userData = data
    return userData
