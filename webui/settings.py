# -*- coding: utf-8 -*-
"""面板上能改的运行配置：白名单 + 校验 + 写回 ``.env``。

三条口径（和 docs/webui.md §8「面板说的」和「工具真做的」必须一致）：

1. 只放**工具真的会读**的键（都会在 ``utils/config.get_config()`` / ``core`` 里
   找到读它的那一行）。面板上出现、工具不看的键一律不出现。
2. 每项自带校验，写错就**拒绝写盘**并说清原因。这不是装饰：
   ``get_config()`` 里是 ``json.loads(HITOKOTO_TYPES)`` / ``int(float(...))``，
   面板要是把一个坏值写进 ``.env``，下一轮任务会直接抛异常 —— 那才是真正的
   "面板一套、实际一套"。
3. 账号类（``TASKS`` / ``COOKIES_*``）、口令类（``WEBUI_PASSWORD*``）、
   路径类不在这里：它们各有专门的入口（账号卡、目标勾选、命令行）。

写盘一律走 ``envfile.update_settings()``：同一把写锁 + 先备份 + 就地改行，
注释与变量顺序原样保留。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from core import schedule as schedule_mod

from webui import envfile, store

# ---------------------------------------------------------------- 控件类型

KIND_TEXT = "text"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_SELECT = "select"
KIND_LIST = "list"  # JSON 数组
KIND_WINDOW = "window"  # "HH:MM-HH:MM" / off
KIND_TIMES = "times"  # 逗号分隔的 HH:MM 列表（"08:00,20:30"）

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
# 与 core/quiet_window.py 的 _OFF_VALUES 保持一致（那边是权威，测试钉住）
_OFF_VALUES = ("", "off", "none", "no", "0", "false", "disable", "disabled")

# 一言类型的已知取值（.env.example 里列的那批）；只是提示，不做白名单：
# 一言 API 自己会加分类，写死了反而会让面板拦掉合法值。
HITOKOTO_KNOWN = ("动画", "漫画", "游戏", "文学", "原创", "来自网络", "影视", "诗词", "哲学", "抖机灵", "其他")

DEFAULT_MESSAGE_TEMPLATE = "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"


class Setting(NamedTuple):
    """一项可改的运行配置。"""

    key: str
    label: str
    kind: str
    hint: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    options: Tuple[str, ...] = ()
    default: str = ""
    allow_empty: bool = False
    masked: bool = False
    restart: bool = False


# 顺序 = 面板上的顺序：先"发送行为"，再"浏览器与扫描"，最后"面板自己"。
SETTINGS: Tuple[Setting, ...] = (
    Setting(
        "DRY_RUN",
        "默认运行模式",
        KIND_SELECT,
        options=("off", "select", "full"),
        default="off",
        hint="面板上「立即运行」选的模式优先于这里；认不出来的值会按 off（真发）处理",
    ),
    Setting(
        "MESSAGE_TEMPLATE",
        "消息模板",
        KIND_TEXT,
        default=DEFAULT_MESSAGE_TEMPLATE,
        hint=r"用 \n 表示换行，[API] 会替换成一言内容",
    ),
    Setting(
        "HITOKOTO_TYPES",
        "一言类型",
        KIND_LIST,
        default='["文学","影视","诗词","哲学"]',
        hint="JSON 数组；已知取值：" + "、".join(HITOKOTO_KNOWN),
    ),
    Setting(
        "SEND_INTERVAL_MIN_SECONDS",
        "发送间隔下限",
        KIND_FLOAT,
        minimum=0,
        maximum=600,
        default="8",
        hint="秒；0/0 表示完全保持浏览器自带的拟人节奏。试运行时不会等待",
    ),
    Setting(
        "SEND_INTERVAL_MAX_SECONDS",
        "发送间隔上限",
        KIND_FLOAT,
        minimum=0,
        maximum=600,
        default="20",
        hint="秒；必须 ≥ 下限",
    ),
    Setting(
        "RUN_START_JITTER_SECONDS",
        "开跑前随机延迟",
        KIND_INT,
        minimum=0,
        maximum=600,
        default="90",
        hint="秒；定时器到点后先随机等 0~N 秒再开浏览器，磨掉每 5 分钟一次的整分边缘。"
             "只影响定时触发（面板/命令行立即运行不等）；0 = 准点开跑",
    ),
    Setting(
        "SEND_INTERVAL_LONG_PAUSE_SECONDS",
        "长尾停顿",
        KIND_INT,
        minimum=0,
        maximum=600,
        default="75",
        hint="秒；约 15% 的概率在正常间隔之外再多停 0~N 秒，让间隔不是整齐的均匀分布；"
             "0 = 关闭（回到纯均匀）",
    ),
    Setting(
        "WARMUP_BROWSE_SECONDS",
        "暖场浏览",
        KIND_INT,
        minimum=0,
        maximum=120,
        default="0",
        hint="秒；登录成功后先在好友列表里滚 N 秒再发（只滚不点，失败不影响发送）；"
             "0 = 关闭（默认）",
    ),
    Setting(
        "TYPING_CHUNK_CHARS",
        "分段输入字数",
        KIND_INT,
        minimum=0,
        maximum=200,
        default="6",
        hint="字；消息按这么多字一块、一块一块地键入编辑器（块间有停顿），"
             "比整段一次性出现更像手打；0 = 整段一次输入",
    ),
    Setting(
        "TYPING_CHUNK_DELAY_MS",
        "分块间隔",
        KIND_INT,
        minimum=0,
        maximum=2000,
        default="110",
        hint="毫秒；块与块之间的停顿（实际在 0.6~1.4 倍之间随机）；"
             "0 = 不停顿，分段输入效果等同整段输入",
    ),
    Setting(
        "LIMIT_GUARD_ENABLED",
        "限流熔断",
        KIND_SELECT,
        options=("true", "false"),
        default="true",
        hint="命中风控文案/回执就中止本轮剩余目标；false = 只在日志里留痕（不建议）",
    ),
    Setting(
        "LIMIT_COOLDOWN_MINUTES",
        "熔断冷却",
        KIND_INT,
        minimum=1,
        maximum=1440,
        default="30",
        hint="分钟；冷却期内定时任务与手动触发都跳过整轮",
    ),
    Setting(
        "TASK_SCHEDULE_TIMES",
        "默认发送时间",
        KIND_TIMES,
        default="20:30",
        hint=(
            "没在账号卡上单独设时间的账号，按这里第一个时间发；"
            "多个时间用逗号隔开（写了 time 的账号不看这一项）"
        ),
    ),
    Setting(
        "SCHEDULE_JITTER_MINUTES",
        "发送时间浮动",
        KIND_INT,
        minimum=0,
        maximum=180,
        default="15",
        hint=(
            "分钟；在设定时间**前后各**随机偏移 0~N 分钟（对称，不是只往后推）。"
            "0 = 严格按点。同一账号同一天的偏移是固定的，不会每 5 分钟飘一次"
        ),
    ),
    Setting(
        "QUIET_WINDOW",
        "禁跑时间窗",
        KIND_WINDOW,
        default="off",
        hint="HH:MM-HH:MM（跨零点也认）；这段绝对不发。默认 off —— 盖住定时器时间会让那一轮整轮跳过，面板会报红",
    ),
    Setting(
        "NOTIFY_WEBHOOK_URL",
        "通知地址",
        KIND_TEXT,
        default="",
        allow_empty=True,
        masked=True,
        hint=(
            "填 Server酱 / 企业微信 / 钉钉 / 飞书 / Telegram 机器人的 webhook 地址"
            "（按主机名自动挑 JSON 形状，认不出来的按通用 JSON 发）；留空 = 不通知。"
            "地址里的 token 会被打码显示，改的时候要整条写全"
        ),
    ),
    Setting(
        "NOTIFY_LEVEL",
        "通知时机",
        KIND_SELECT,
        options=("off", "error", "always"),
        default="error",
        hint=(
            "error = 只在有账号没发成、或命中风控熔断时推一条（默认）；"
            "always = 每轮真发都推；off = 关掉（地址留着也不发）。试运行（dry-run）永远不推"
        ),
    ),
    Setting(
        "NOTIFY_TIMEOUT_SECONDS",
        "通知超时",
        KIND_INT,
        minimum=3,
        maximum=60,
        default="10",
        hint="秒；推送在放锁之后才做，超时就放弃 —— 不影响这一轮发没发、记没记账",
    ),
    Setting(
        "NOTIFY_SILENCE_HOURS",
        "静默报警阈值",
        KIND_FLOAT,
        minimum=0,
        maximum=720,
        default="30",
        hint=(
            "小时；超过这么久没有任何运行记录就报警（体检 doctor 用，默认 30 —— "
            "最短的账号间隔约 21 小时）。0 = 关掉这项判定；"
            "账号全部停用时按设计不报警，那是你自己关的"
        ),
    ),
    Setting(
        "PROXY_ADDRESS",
        "代理",
        KIND_TEXT,
        default="",
        allow_empty=True,
        masked=True,
        hint=(
            "浏览器启动时传给 cloakbrowser/playwright；扫码登录与好友扫描也用它。留空 = 直连。"
            "单个账号可以在它的账号卡里单独配出口（TASKS[].proxy），那里留空就回落这里"
        ),
    ),
    Setting(
        "BROWSER_ACTION_TIMEOUT",
        "浏览器动作超时",
        KIND_FLOAT,
        minimum=5,
        maximum=300,
        default="120",
        hint="秒；单次操作/导航的最长等待（对应 Playwright 的默认超时）",
    ),
    Setting(
        "IM_SCAN_TIMEOUT",
        "IM 扫描超时",
        KIND_INT,
        minimum=10,
        maximum=1800,
        default="120",
        hint="秒；整个扫描过程的时间总预算，超时即停止滚动",
    ),
    Setting(
        "IM_READY_TIMEOUT",
        "IM 就绪超时",
        KIND_INT,
        minimum=5,
        maximum=300,
        default="120",
        hint="秒；登录校验 + 会话列表就绪的等待上限",
    ),
    Setting(
        "FRIEND_LIST_WAIT_TIME",
        "好友列表稳定等待",
        KIND_FLOAT,
        minimum=1,
        maximum=120,
        default="3",
        hint="秒；发现新会话后等这么久没有新资料才算落定（调大会明显拖慢扫描）",
    ),
    Setting(
        "IM_MAX_STEPS",
        "IM 最大步数",
        KIND_INT,
        minimum=10,
        maximum=2000,
        default="200",
        hint="步；滚动硬上限，步长 = 可视高度 40%",
    ),
    Setting(
        "TASK_RETRY_TIMES",
        "任务重试次数",
        KIND_INT,
        minimum=0,
        maximum=5,
        default="1",
        hint="每个目标没拿到可信回执后最多再试几次（0 = 不重试）；命中风控信号或掉登录时一次都不重试",
    ),
    Setting(
        "LOG_LEVEL",
        "日志级别",
        KIND_SELECT,
        options=("Debug", "Info", "Warning", "Error"),
        # 与 utils/config.py、configTool、.env.example、docs 的默认值保持一致（都是
        # Info）。面板这一处是第 5 个"默认值来源"：`.env` 里没这个键时，卡片上显示
        # 的就是它，写回去的也是它 —— 显示 Debug 而代码兜底 Info 会让"我没设过"的
        # 用户看到两者不一致（`tests/test_config_tool.py::DefaultsAlignmentTests`
        # 已把面板这一处也钉进对齐断言）。
        default="Info",
        restart=True,
        hint="面板自己的日志要重启服务才换级别；定时任务下一轮自动生效",
    ),
    Setting(
        "WEBUI_TZ",
        "显示时区",
        KIND_TEXT,
        default="Asia/Shanghai",
        hint="面板上的时间戳按它渲染（IANA 名字，如 Asia/Shanghai）。它只管显示："
             "调度与日志时钟走 .env 里的 TZ（面板改不了，改它要重启服务）——"
             "两边填不一样时，面板显示的时间会和实际触发时间差几个小时",
    ),
    Setting(
        "WEBUI_SESSION_MAX_AGE",
        "登录保持时长",
        KIND_INT,
        # 与 webui/config.py 的 DEFAULT_SESSION_MAX_AGE / MAX_SESSION_MAX_AGE 同口径：
        # 默认 30 天，上限 400 天（Chrome 对 cookie 有效期的硬上限，写更大也会被截断）。
        minimum=60,
        maximum=400 * 24 * 3600,
        default="2592000",
        hint="秒；默认 30 天（2592000），上限 400 天。它读的时候才生效："
             "调大之后已经登录的浏览器立刻受益，调小会把超过新时长的会话立刻踢下线。"
             "改口令永远会作废所有会话，登出会拉黑当前这一份",
    ),
)

BY_KEY: Dict[str, Setting] = {item.key: item for item in SETTINGS}


# ---------------------------------------------------------------- 校验


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int_text(text: str) -> str:
    """把 "120" / "120.0" 都收敛成 "120"。"""
    return str(int(float(text)))


def _float_text(text: str) -> str:
    value = float(text)
    return str(int(value)) if value == int(value) else str(value)


def _range_error(item: Setting, text: str) -> str:
    low, high = item.minimum, item.maximum
    return f"{item.label}要在 {_float_text(str(low))}–{_float_text(str(high))} 之间（收到 {text}）"


def parse(item: Setting, raw: Any) -> Tuple[bool, str, str]:
    """校验一项。返回 ``(是否合法, 要写进 .env 的文本, 错误说明)``。"""
    text = _text(raw)

    if item.kind == KIND_SELECT:
        lowered = text.lower()
        for option in item.options:
            if option.lower() == lowered:
                return True, option, ""
        return False, "", f"{item.label}只能是 {'/'.join(item.options)} 之一（收到 {text or '空'}）"

    if item.kind in (KIND_INT, KIND_FLOAT):
        if not text:
            return False, "", f"{item.label}不能为空"
        try:
            number = float(text)
        except ValueError:
            return False, "", f"{item.label}必须是数字（收到 {text!r}）"
        if number != number or number in (float("inf"), float("-inf")):
            return False, "", f"{item.label}必须是有限数字（收到 {text!r}）"
        if item.kind == KIND_INT and number != int(number):
            return False, "", f"{item.label}必须是整数（收到 {text!r}）"
        if item.minimum is not None and number < item.minimum:
            return False, "", _range_error(item, text)
        if item.maximum is not None and number > item.maximum:
            return False, "", _range_error(item, text)
        return True, (_int_text(text) if item.kind == KIND_INT else _float_text(text)), ""

    if item.kind == KIND_LIST:
        if not text:
            return False, "", f"{item.label}不能为空（要写成 JSON 数组，例如 [\"文学\"]）"
        try:
            payload = json.loads(text)
        except ValueError as exc:
            return False, "", f"{item.label}必须是合法的 JSON 数组（{exc}）"
        if not isinstance(payload, list) or not payload:
            return False, "", f"{item.label}必须是非空 JSON 数组（例如 [\"文学\",\"影视\"]）"
        items: List[str] = []
        for entry in payload:
            entry_text = _text(entry)
            if not entry_text:
                return False, "", f"{item.label}里不能有空项"
            items.append(entry_text)
        return True, json.dumps(items, ensure_ascii=False), ""

    if item.kind == KIND_TIMES:
        if not text:
            return False, "", f"{item.label}不能为空（至少写一个 HH:MM，例如 20:30）"
        times: List[str] = []
        for piece in re.split(r"[,，;；\s]+", text):
            if not piece:
                continue
            one = schedule_mod.parse_time(piece)
            if not one:
                return False, "", (
                    f"{item.label}里的 {piece!r} 不是时间：要写成 24 小时制的 HH:MM，"
                    "多个时间用逗号隔开（例如 08:00,20:30）"
                )
            if one not in times:
                times.append(one)
        if not times:
            return False, "", f"{item.label}至少要写一个时间，例如 20:30"
        return True, ",".join(times), ""

    if item.kind == KIND_WINDOW:
        if text.lower() in _OFF_VALUES:
            return True, "off", ""
        if not _window_ok(text):
            return False, "", (
                f"{item.label}要写成 HH:MM-HH:MM（例如 20:20-21:00），写 off 表示不限制（收到 {text!r}）"
            )
        return True, text, ""

    # KIND_TEXT
    if not text and not item.allow_empty:
        return False, "", f"{item.label}不能为空"
    if "\n" in text or "\r" in text:
        if item.key == "MESSAGE_TEMPLATE":
            return False, "", (
                "消息模板在面板上只能写单行：换行请写两个字符 \\n。"
                "（工具两种写法都认，但带引号的真多行值只能直接编辑 .env）"
            )
        return False, "", f"{item.label}不能包含换行"
    if text.startswith("#") or envfile.comment_hazard(text):
        # 用 envfile 里那道同款守卫：`\s` 是 Unicode 空白，tab / NBSP / 全角空格
        # 后面跟 # 一样会被 dotenv 当注释，只查半角空格是漏的。
        return False, "", f"{item.label}里不能出现 “ #” —— .env 会把 # 后面当成注释，值会被悄悄截断"
    if item.key == "PROXY_ADDRESS" and "***" in text:
        return False, "", (
            "代理地址里带着 *** —— 那是面板打码后的样子，不是真口令。"
            "只改主机端口也请把 user:密码@ 一起写全"
        )
    if item.key == "NOTIFY_WEBHOOK_URL" and text:
        if "***" in text:
            return False, "", (
                "通知地址里带着 *** —— 那是面板打码后的样子，不是真地址。"
                "改主机或路径也请把整条地址写全"
            )
        if not text.lower().startswith(("http://", "https://")):
            return False, "", "通知地址要以 http:// 或 https:// 开头"
        if any(char.isspace() for char in text):
            return False, "", "通知地址里不能有空格（路径里带空格的请先做 URL 编码）"
    if item.key == "WEBUI_TZ" and not _timezone_ok(text):
        return False, "", f"显示时区 {text!r} 不是有效的 IANA 时区名（例：Asia/Shanghai / UTC）"
    return True, text, ""


def masked_view(item: Setting, raw: str) -> str:
    """面板上**显示**的值：打码字段不回显原文。

    显示与"用户有没有动过这一栏"的比较必须出自同一个函数（见 `collect`）——
    两处规则不一致的话，点一次保存就会把打码后的样子写回 `.env`。
    """
    if not item.masked:
        return raw
    if item.key == "NOTIFY_WEBHOOK_URL":
        # 代理那条规则只遮 user:password@；webhook 的 token 在路径/查询里，得整段遮。
        return store.mask_webhook_url(raw)
    return store.mask_proxy(raw)


def collect(submitted: Dict[str, Any], env_map: Dict[str, Any]) -> Dict[str, str]:
    """把表单提交整理成"要校验的值"。

    两类"没动过"的项直接跳过，免得点一次保存就把 `.env` 重新排版一遍：

    * 打码字段（代理、通知地址）提交上来的还是那块打码文本 —— 说明用户没动它。否则会把它当成
      新值写进 `.env`，把真口令换成字面的 ``***``。
    * 提交值与 `.env` 原文完全相同（下拉框大小写不同也算同义，比如 ``Debug`` / ``debug``）。
    """
    values: Dict[str, str] = {}
    for item in SETTINGS:
        if item.key not in submitted:
            continue
        text = _text(submitted.get(item.key))
        current = _text(env_map.get(item.key))
        if item.masked and text == masked_view(item, current):
            continue
        if text == current:
            continue
        if item.kind == KIND_SELECT and text.lower() == current.lower():
            continue
        values[item.key] = text
    return values


def _window_ok(text: str) -> bool:
    if "-" not in text:
        return False
    start_text, _, end_text = text.partition("-")
    return bool(_HHMM_RE.match(start_text.strip()) and _HHMM_RE.match(end_text.strip()))


def _timezone_ok(text: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(text)
        return True
    except Exception:
        # 没有 tzdata 的环境（如某些 Windows）只做格式检查：
        # 宁可放行一个格式正确的名字，也不要因为查不到数据就拒绝合法时区。
        return bool(re.match(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$", text))


def normalize(raw_values: Dict[str, Any]) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """逐项校验整批提交。返回 ``(要写盘的值, 错误列表)``。

    只认识白名单里的键：其它键直接忽略（面板不会替用户改 TASKS / COOKIES_*）。
    """
    values: Dict[str, str] = {}
    errors: List[Dict[str, str]] = []
    for item in SETTINGS:
        if item.key not in raw_values:
            continue
        ok, value, error = parse(item, raw_values.get(item.key))
        if ok:
            values[item.key] = value
        else:
            errors.append({"key": item.key, "label": item.label, "error": error})

    low, high = values.get("SEND_INTERVAL_MIN_SECONDS"), values.get("SEND_INTERVAL_MAX_SECONDS")
    if low and high:
        if float(low) > float(high):
            errors.append(
                {
                    "key": "SEND_INTERVAL_MAX_SECONDS",
                    "label": "发送间隔上限",
                    "error": f"发送间隔上限（{high} 秒）不能小于下限（{low} 秒）",
                }
            )
    return values, errors


def problems(env_map: Dict[str, Any]) -> List[Dict[str, str]]:
    """``.env`` 里**现在就写错了**的可改项（工具会抛异常或悄悄回落默认值）。"""
    found: List[Dict[str, str]] = []
    for item in SETTINGS:
        if item.key not in env_map:
            continue
        raw = _text(env_map.get(item.key))
        ok, _, error = parse(item, raw)
        if not ok:
            found.append({"key": item.key, "label": item.label, "value": raw, "error": error})
    return found


def form_state(env_map: Dict[str, Any]) -> List[Dict[str, Any]]:
    """给模板用的可改项列表（值取自 ``.env`` 原文，不是加工过的展示值）。"""
    rows: List[Dict[str, Any]] = []
    bad_keys = {item["key"] for item in problems(env_map)}
    for item in SETTINGS:
        raw = _text(env_map.get(item.key))
        value = masked_view(item, raw)
        options: List[Dict[str, str]] = []
        if item.kind == KIND_SELECT:
            if item.key not in env_map:
                # `.env` 里没有这一项：浏览器会默认选中**第一项**，用户不动它直接点保存，
                # 就把"没配过"变成了"写死成第一项"。给个空值占位项，它与 `.env` 原文
                # （也是空）一致 → `collect()` 判定"没动过" → 一个字节都不写。
                options.append(
                    {
                        "value": "",
                        "label": f"（.env 里没有，工具用默认值 {item.default}）",
                    }
                )
            elif value and value not in item.options:
                # 认不出来的当前值也要出现在下拉框里 —— 否则同样会被第一个选项顶掉。
                options.append({"value": value, "label": f"{value}（认不出来，保存会照原样写回）"})
            options.extend({"value": option, "label": option} for option in item.options)
        rows.append(
            {
                "key": item.key,
                "label": item.label,
                "kind": item.kind,
                "value": value,
                "options": options,
                "hint": item.hint,
                "default": item.default,
                "placeholder": item.default,
                "restart": item.restart,
                "masked": item.masked,
                "bad": item.key in bad_keys,
                "missing": item.key not in env_map,
            }
        )
    return rows


def apply(raw_values: Dict[str, Any], *, path: Any = None) -> Dict[str, Any]:
    """校验 + 写盘。任何一项不合法就整批不写（不留半截状态）。"""
    values, errors = normalize(raw_values)
    if errors:
        return {
            "ok": False,
            "errors": errors,
            "error": "；".join(item["error"] for item in errors),
            "changed": {},
        }
    if not values:
        # 表单里每一项都和 `.env` 原文一样（或者压根没提交）—— 不算失败，
        # 但也确实"什么都没写"，调用方据此显示"没有变化"。
        return {
            "ok": True,
            "errors": [],
            "changed": {},
            "kept": [],
            "path": str(envfile.env_path(path)),
            "backup": "",
        }
    result = envfile.update_settings(values, path=path)
    result.setdefault("errors", [])
    return result
