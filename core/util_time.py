"""跨模块共用的小工具：时间 与 dry-run 档位。纯 stdlib，不依赖项目内任何模块。

## 为什么单独一个模块

这两组东西被 `douyin_im`（dry-run 执行）、`limit_guard`（冷却计时）、
`run_ledger`（账本时间戳）、`tasks`（汇总）、`webui`（展示倒计时）同时需要。
分散实现过一次，结果是"同一个 dry-run 档位在三个地方有三种解析规则"——
所以宁可多一个小文件，也不要第二套写法。
"""
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def now():
    """当前时间（带本地时区）。单测一律传 `at=` 覆盖，不改系统时钟。"""
    return datetime.now().astimezone()


def iso(dt):
    """ISO 8601，**秒级精度**（刻意丢掉微秒）。

    日志、账本、锁文件里都是给人看的时间戳，多六位小数只会碍眼。
    代价是"写入时被截断的微秒"会让基于它的时间差比真实值小一点点，
    所以凡是做**精确时间差**的地方（冷却剩余秒数）都先把时间戳归到整秒
    （见 `limit_guard.trip`），别让半秒的零头把 500 算成 499。
    """
    return dt.isoformat(timespec="seconds")


def parse_iso(s):
    """解析 ISO 时间串；容忍 `Z` 结尾与缺时区（缺时区按本地算）。

    解析不了返回 None —— 调用方必须把 None 当"不知道"，**不能**当"已过期"。
    """
    if not s:
        return None
    t = str(s).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=now().tzinfo)
    return d


def make_run_id(at=None):
    """`YYYYMMDD-HHMMSS` —— 本地时间，人能直接读，也天然按时间排序。"""
    return (at or now()).strftime("%Y%m%d-%H%M%S")


def cooldown_human(seconds):
    """冷却剩余时间的人话形式（日志与仪表盘共用）。"""
    s = int(seconds or 0)
    if s <= 0:
        return "已结束"
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分{sec} 秒" if sec else f"{m} 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时{m} 分" if m else f"{h} 小时"


def human_duration(seconds):
    """耗时的人话形式（与 `cooldown_human` 的措辞保持一致：整点不带零头）。"""
    s = int(seconds or 0)
    if s < 60:
        return f"{s} 秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} 分{sec} 秒" if sec else f"{m} 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时{m} 分" if m else f"{h} 小时"


def in_seconds(seconds, at=None):
    return iso((at or now()) + timedelta(seconds=int(seconds or 0)))


# ---------------------------------------------------------------------------
# dry-run 档位
# ---------------------------------------------------------------------------

DRY_RUN_OFF = "off"        # 正常发送
DRY_RUN_SELECT = "select"  # 只到"输入前的最后一步"：不输入、不发送
DRY_RUN_FULL = "full"      # 真输入进 Draft.js 并读回校验，然后清空，不发送
DRY_RUN_LEVELS = (DRY_RUN_OFF, DRY_RUN_SELECT, DRY_RUN_FULL)

DRY_RUN_LABELS = {
    DRY_RUN_OFF: "正常发送",
    DRY_RUN_SELECT: "空跑：只确认选中",
    DRY_RUN_FULL: "空跑：真输入并读回校验",
}

_DRY_RUN_ALIASES = {
    "": DRY_RUN_OFF, "0": DRY_RUN_OFF, "false": DRY_RUN_OFF, "no": DRY_RUN_OFF,
    "none": DRY_RUN_OFF, "off": DRY_RUN_OFF,
    "scan": DRY_RUN_SELECT, "select": DRY_RUN_SELECT, "send": DRY_RUN_SELECT,
    "on": DRY_RUN_FULL, "1": DRY_RUN_FULL, "true": DRY_RUN_FULL, "yes": DRY_RUN_FULL,
    "all": DRY_RUN_FULL, "full": DRY_RUN_FULL, "dry": DRY_RUN_FULL,
}


def norm_dry_run(value):
    """把配置/表单里的各种写法收敛成三档之一。

    认不出来的值**回落到 off**（照常发送，保持 C 的默认行为）。
    刻意不回落 full：拼错档位时人期望的是"按默认来"，而默认就是真发；
    反过来（拼错就空跑）会让人以为消息发出去了，那才是危险的。
    """
    if value is None:
        return DRY_RUN_OFF
    return _DRY_RUN_ALIASES.get(str(value).strip().lower(), DRY_RUN_OFF)


def resolve_dry_run(cli_value=None, env_value=None):
    """优先级：命令行/表单一侧 > 环境变量 > off。"""
    if cli_value not in (None, ""):
        return norm_dry_run(cli_value)
    if env_value not in (None, ""):
        return norm_dry_run(env_value)
    return DRY_RUN_OFF
