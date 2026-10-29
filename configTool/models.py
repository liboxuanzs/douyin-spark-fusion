"""配置数据模型与默认值。

默认值全部对齐 docs/static/js/main.js 的 form 初值，键顺序与 .env.example 一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 可选项
# ---------------------------------------------------------------------------
HITOKOTO_OPTIONS = [
    "动画",
    "漫画",
    "游戏",
    "文学",
    "原创",
    "来自网络",
    "影视",
    "诗词",
    "哲学",
    "抖机灵",
    "其他",
]

TZ_OPTIONS = ["Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "UTC"]

# utils/logger.py::resolve_log_level 会做 lower() 映射，所以这里保持页面的写法
LOG_LEVEL_OPTIONS = ["Debug", "Info", "Warning", "Error"]

# ---------------------------------------------------------------------------
# 默认值与取值范围
# ---------------------------------------------------------------------------
DEFAULT_PROXY_ADDRESS = ""
# 每账号浏览器状态快照目录（core/browser_state.py 直读，界面没有输入框）。
# 和 PROXY_ADDRESS 一样是字符串键，但**必须登记**：漏了它，用户在 GUI 里
# 保存一次就把 .env 里这一行删掉 —— 程序回落到同一个默认值，所以不报错，
# 只是"我明明设过"变成静默失效。
DEFAULT_BROWSER_STATE_DIR = "logs/browser-state"
# 浏览器语言/时区（core/browser.py 直读，界面没有输入框）。时区留空 = 跟系统时区
# 走；语言留空 = zh-CN。两者都必须登记，否则 GUI 保存一次就从 .env 里消失。
DEFAULT_BROWSER_TIMEZONE = ""
DEFAULT_BROWSER_LOCALE = "zh-CN"
DEFAULT_RUN_TIME = "09:00:00"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_MESSAGE_TEMPLATE = "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]"
DEFAULT_HITOKOTO_TYPES = ["文学", "影视", "诗词", "哲学"]
DEFAULT_BROWSER_ACTION_TIMEOUT = 120
DEFAULT_IM_SCAN_TIMEOUT = 120
DEFAULT_IM_READY_TIMEOUT = 120
DEFAULT_FRIEND_LIST_WAIT_TIME = 3
DEFAULT_IM_MAX_STEPS = 200
DEFAULT_TASK_RETRY_TIMES = 1
# ⚠️ 大小写与 LOG_LEVEL_OPTIONS 保持一致（"Info" 而非 "INFO"）：
# tkinter Combobox 对不在 values 里的值不会高亮匹配项 → 框看着是空的。
# utils.logger.resolve_log_level 内部 level.lower()，所以两种写法日志行为相同，
# 这里只为 GUI 显示正确。
# 默认值 "Info"（2026-10 改，原 "Debug"）：与 utils/config.py 的
# `os.getenv("LOG_LEVEL", "Info")`、main.py 的兜底、.env.example、docs 网页版
# 四处对齐（tests/test_config_tool.py::test_log_level_aligned 四处逐字断言）。
# 以前默认 "Debug"，于是"没动过这一项"的用户被写进一份 Debug 的 .env，
# 与代码侧默认不一致；Debug 仍是下拉里的可选项，只是不再作为默认值。
DEFAULT_LOG_LEVEL = "Info"
# 发送时刻的抖动窗口（分钟）：每个账号在基准时刻前后再随机偏移一点，
# 账号多了不至于整点齐刷刷一起发。0 = 不抖动。
# ⚠️ 必须与 utils/config.py 的 `_env_int("SCHEDULE_JITTER_MINUTES", 15, minimum=0)`
#    以及 .env.example / docs 保持一致（tests/test_config_tool.py 四处对齐用例）。
DEFAULT_SCHEDULE_JITTER_MINUTES = 15

# 抖动窗口的取值范围：**只有下限是真的**（utils/config.py 那侧也只有 `minimum=0`，
# 没有上限），上限取一天纯粹是给 number() 一个夹紧边界 —— 手滑把量级写错
# （比如把秒当分钟填成 1800）时夹到一天比原样写回更容易看出问题。
SCHEDULE_JITTER_RANGE = (0, 1440)
# "门禁没过/命中风控"（skipped）的账号当天最多被门铃重试几次。
# ⚠️ 必须与 core/schedule.py 的 `DEFAULT_SKIP_RETRY_MAX`、.env.example 一致
#    （tests/test_config_tool.py::SkipRetryKeyTests 把三处钉在一起）。
# 为什么默认 6：skipped 的语义是"下一次门铃再试"，而门铃 5 分钟一次 ——
# 不设上限的账号一天会被踹起来 288 次，等于持续喂风控。0 = 不限制。
DEFAULT_SCHEDULE_SKIP_RETRY_MAX = 6

# 重试上限的取值范围：288 = 一天最多能被敲到的次数（24h × 60 / 5 分钟刻度），
# 再大等价于"不限制"。用 max 夹一下能挡住手滑多打几位（0 与非 0 的语义不会变）。
SCHEDULE_SKIP_RETRY_RANGE = (0, 288)

# 定时器顺带做磁盘维护的间隔（小时）：默认 168（一周），0 = 关闭。
# ⚠️ 必须与 core/maintenance.py 的 `DEFAULT_AUTO_PRUNE_HOURS`、.env.example 一致
#    （tests/test_config_tool.py::AutoPruneKeyTests 把三处钉在一起）。
# 为什么要有它：Chrome 缓存只涨不落（实测约 180 MB/天/账号），磁盘满 = 发送直接失败。
DEFAULT_MAINTENANCE_AUTO_PRUNE_HOURS = 168

# 间隔的取值范围：8760 = 一年，再大等价于关闭（core/maintenance.py 也会夹）。
MAINTENANCE_AUTO_PRUNE_HOURS_RANGE = (0, 8760)

# 单位统一为秒
BROWSER_ACTION_TIMEOUT_RANGE = (5, 300)
IM_SCAN_TIMEOUT_RANGE = (10, 1800)
IM_READY_TIMEOUT_RANGE = (5, 300)
FRIEND_LIST_WAIT_RANGE = (1, 120)
IM_MAX_STEPS_RANGE = (10, 2000)
RETRY_TIMES_RANGE = (0, 5)   # 0 = 不重试（与面板 webui/settings.py 的 minimum=0 对齐）

# 写进 .env 的键顺序：先基础变量，再按账户顺序追加 COOKIES_*
BASE_ENV_KEYS = [
    "PROXY_ADDRESS",
    # 浏览器状态快照目录：core/browser_state.py 直读，界面没有输入框；
    # 读回来再写回去，GUI 保存不会把它从 .env 里抹掉。
    "BROWSER_STATE_DIR",
    # 浏览器语言/时区：同样是「界面没有输入框、但必须原样写回去」的键，
    # 漏了它们，用户手改的 BROWSER_TIMEZONE 会被一次保存抹成空。
    "BROWSER_TIMEZONE",
    "BROWSER_LOCALE",
    # CRON_* 只有 Docker 定时器（docker/entrypoint-cron.sh）读，
    # 但它就是"到点了叫醒程序"的那个闹钟，删了 Docker 部署就不发消息了 → 保留。
    # ⚠️ 光有 CRON_* 不够：程序内的发送闸门只认 TASK_SCHEDULE_TIMES（见下），
    #    systemd 部署更是完全不看 CRON_*。两者必须一起写。
    "CRON_HOUR",
    "CRON_MINUTE",
    "CRON_SECOND",
    "TZ",
    "MESSAGE_TEMPLATE",
    "HITOKOTO_TYPES",
    "BROWSER_ACTION_TIMEOUT",
    "IM_SCAN_TIMEOUT",
    "IM_READY_TIMEOUT",
    "FRIEND_LIST_WAIT_TIME",
    "IM_MAX_STEPS",
    "TASK_RETRY_TIMES",
    "LOG_LEVEL",
    "TASK_SCHEDULE_TIMES",
    "SCHEDULE_JITTER_MINUTES",
    # 门铃重试上限：程序内直读（core/schedule.py::skip_retry_max_env），
    # 界面上没有输入框，但读回来再写回去 —— 用户手改的 0（= 不限制）不该被保存动作抹掉。
    "SCHEDULE_SKIP_RETRY_MAX",
    # 自动清理间隔：同样是程序内直读（core/maintenance.py::auto_prune_hours），
    # 界面没有输入框；读回来再写回去，用户手改的 0（= 关闭）不该被保存动作抹掉。
    "MAINTENANCE_AUTO_PRUNE_HOURS",
    "TASKS",
]

# unique_id 直接决定 COOKIES_<unique_id> 的键名，必须是环境变量安全的字符
SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def split_run_time(value: str) -> tuple[str, str, str]:
    """把 HH:MM:SS 拆成 (时, 分, 秒)，非法时回落到默认值。"""
    parts = (value or "").split(":")
    if len(parts) != 3:
        parts = DEFAULT_RUN_TIME.split(":")
    result = []
    for index, part in enumerate(parts):
        try:
            number = int(part.strip())
        except ValueError:
            number = int(DEFAULT_RUN_TIME.split(":")[index])
        result.append(max(0, min(59 if index else 23, number)))
    return (f"{result[0]:02d}", f"{result[1]:02d}", f"{result[2]:02d}")


def build_run_time(hour, minute, second) -> str:
    def clamp(raw, top):
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            number = 0
        return max(0, min(top, number))

    return f"{clamp(hour, 23):02d}:{clamp(minute, 59):02d}:{clamp(second, 59):02d}"


def parse_schedule_times(value) -> tuple:
    """`TASK_SCHEDULE_TIMES` 的第一个可用时刻 → `("HH:MM:SS", 秒是不是写出来的)`。

    认不出来返回 `("", False)`。

    `TASK_SCHEDULE_TIMES` 才是程序内真正说了算的那个键（core/tasks.py →
    core/schedule.py::fallback_time 取**第一项**），`CRON_*` 只有 Docker 定时器读。
    以前 `from_env_map()` 只认 `CRON_*`，于是用户在面板里把发送时间改成 20:30，
    回头在 GUI 里随便改个字段、1.5 秒后的自动保存就把它按 `CRON_HOUR` 改回 09:00
    —— 静默、无报错（本次修的缺陷）。

    分隔口径与 `core/schedule.py::_as_list()` 对齐（**逗号**分隔、逐项 strip）：
    多写的时刻只取第一项，因为界面上只有一个时间输入框，"跟哪个点"必须只有一个
    答案。接受 `HH:MM`（秒补 00）与 `HH:MM:SS`；非数字、越界（25:99）都算认不出来。
    第二个返回值说明"秒是文件里写出来的，还是我替它补的 0" —— `HH:MM` 补出来的 0
    不该盖掉用户在 `CRON_SECOND` 里设的秒（见 from_env_map）。
    """
    for item in str(value or "").split(","):
        chunks = [chunk.strip() for chunk in item.split(":")]
        explicit_seconds = len(chunks) == 3
        if len(chunks) == 2:
            chunks.append("0")
        if len(chunks) != 3:
            continue
        try:
            hour, minute, second = (int(chunk) for chunk in chunks)
        except ValueError:
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            continue
        return f"{hour:02d}:{minute:02d}:{second:02d}", explicit_seconds
    return "", False


# ---------------------------------------------------------------------------
# 账户
# ---------------------------------------------------------------------------

#: `TASKS[].enabled` 里算「停用」/「启用」的写法。与 `utils/config.py` 的
#: `_TASK_DISABLED` / `_TASK_ENABLED` **必须逐字一致**：同一份 .env 会被主程序、
#: 面板和本工具轮流读写，两边认的值不一样，就会出现「面板显示已停用、本工具
#: 把它当成启用写回去」这种静默失效。新增同义写法时两处一起加。
TASK_DISABLED = {"false", "0", "no", "off", "disabled", "stop", "stopped", "停用", "暂停"}
TASK_ENABLED = {"true", "1", "yes", "on", "enabled", "enable", "启用", "恢复"}


def task_enabled(task) -> bool:
    """`TASKS` 里这一项启用没有（`enabled` 缺键 = 启用，向后兼容）。

    口径与 `utils/config.py::task_enabled` 相同（那边多一句告警日志）：
    缺键/None/空串/认不出的值都按启用处理 —— 老 .env 里根本没有这一项，
    按停用处理等于升级当天所有账号静默停发。
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
    if not text or text in TASK_ENABLED:
        return True
    if text in TASK_DISABLED:
        return False
    return True


@dataclass
class Account:
    """一个抖音账号。

    所有身份字段（用户名 / 抖音号 / cookies）都由浏览器登录后自动抓取，
    界面上只读 —— 手填只会填错，而抖音号直接决定 .env 里的 COOKIES_ 键名。

    profile_folder  浏览器配置目录名。由 profile_store 分配并持久化到 profiles.json，
                    加载 .env 后可以凭 unique_id 找回来。空串表示还没分配过。
    fingerprint     浏览器指纹种子。profiles.json 里存一份（权威来源），固定不变，
                    打开浏览器时以 --fingerprint=<它> 传给隐身浏览器；生成 TASKS 时
                    也会一起写进 .env，让主程序能用同一个种子。空串表示还没有记录。
    cookies         单行 JSON 文本（就是写进 COOKIES_<unique_id> 的形态）
    targets         目标好友，只能从抓取到的会话列表里勾选
    message         这个账号专属的发送内容（TASKS[].message）。空串表示没单独配过，
                    主程序就用全局 MESSAGE_TEMPLATE。本工具不编辑它，但**必须原样
                    写回** —— to_task() 是整条 TASKS 的重建，漏了就等于把它删了。
    time            这个账号专属的发送时间（TASKS[].time，形如 "17:20"）。
                    与 message 同理：本工具不编辑它，但**必须原样写回** ——
                    漏一次就等于把「这个账号 17:20 发」悄悄改成全局时间。
    enabled         是否还在自动发送（TASKS[].enabled，缺键 = 启用）。本工具界面上
                    不显示也不编辑它，但**必须原样写回** —— 与 message/time 同一个
                    道理：to_task() 是整条 TASKS 的重建，漏了就等于把用户在面板上
                    停用的账号静默重新启用（2026-10-24 发现的缺陷）。
    proxy           这个账号专属的出口（TASKS[].proxy，空串 = 跟全局 PROXY_ADDRESS）。
                    本工具界面上不显示也不编辑（本工具自己的抓 cookie 代理是另一处
                    配置，见 tunnel.py），但**必须原样写回**：漏一次就等于把账号的
                    出口悄悄换回另一个 IP —— 出口一变，抖音那边就是要重新扫码
                    （2026-10-29 issue #18）。
    """

    username: str = ""
    unique_id: str = ""
    cookies: str = ""
    targets: list = field(default_factory=list)
    # name → [{"uid","conv_id"}, …]：面板勾选好友列表时记下的身份，界面上不显示
    # 但必须原样写回。**值是列表**：重名是真实存在的（同名的好友与群、两个同名
    # 账号），按名字存单个身份会让后一个盖掉前一个 —— 存回去就把目标指向别人了。
    target_meta: dict = field(default_factory=dict)
    profile_folder: str = ""
    fingerprint: str = ""
    message: str = ""
    time: str = ""
    # 缺键/看不懂 = 启用（见 task_enabled）；写回时只在 False 时落 `"enabled": false`。
    enabled: bool = True
    # TASKS[].proxy：空串 = 跟全局；写回时只在非空时落这个键（见 to_task）。
    proxy: str = ""

    @property
    def display_name(self) -> str:
        return self.username or self.unique_id or "（未登录账号）"

    @property
    def cookies_key(self) -> str:
        """对应的 .env 键名。config.py 里是 f"cookies_{unique_id}".upper()。"""
        return f"COOKIES_{self.unique_id.strip().upper()}"

    @property
    def cookie_count(self) -> int:
        text = (self.cookies or "").strip()
        if not text:
            return 0
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return 0
        return len(data) if isinstance(data, list) else 0

    @property
    def has_cookies(self) -> bool:
        return self.cookie_count > 0

    @property
    def cookie_status(self) -> str:
        if not (self.cookies or "").strip():
            return "待登录"
        return f"已获取 {self.cookie_count} 项" if self.cookie_count else "格式错误"

    def to_task(self) -> dict:
        """TASKS 数组里的一项。

        fingerprint 一并写进 .env：主程序拿它可以给同一个账号固定浏览器指纹，
        用的是与 configTool 打开浏览器时完全相同的种子（profiles.json 里那份
        仍是权威来源，这里只是把值带出去）。

        targets 里带 `target_meta` 的项写回**对象**形态
        （`{"name","uid","conv_id"}`）—— 那是面板勾选好友列表时记下的身份，
        本工具界面上只显示名字，但**不能把它丢掉**：主程序靠 id 精确匹配，
        丢了就退回名字匹配（会重名、会误伤）。往返必须幂等。

        重名的目标按出现顺序逐个取用身份（`target_meta[name]` 是列表），
        这样"两个小明各归各的 id"也能原样写回。
        """
        targets = []
        used = {}
        for name in self.targets:
            meta = None
            if isinstance(self.target_meta, dict):
                slot = self.target_meta.get(str(name))
                if isinstance(slot, list):
                    # 按名字**数**着取第几个，而不是 pop(0)。to_task() 是个只读的
                    # 格式化函数，会被反复调用（保存、预览、导出各一次）；pop 会把
                    # 身份列表越吃越短，于是从第二次调用起所有目标的 uid/conv_id
                    # 就没了，主程序退回按名字匹配 —— 重名就会发给另一个人。
                    index = used.get(str(name), 0)
                    meta = slot[index] if 0 <= index < len(slot) else None
                    used[str(name)] = index + 1
                else:
                    meta = slot
            if isinstance(meta, dict) and (meta.get("uid") or meta.get("conv_id")):
                entry = {"name": str(name)}
                if meta.get("uid"):
                    entry["uid"] = str(meta["uid"])
                if meta.get("conv_id"):
                    entry["conv_id"] = str(meta["conv_id"])
                targets.append(entry)
            else:
                targets.append(str(name))
        task = {
            "username": self.username,
            "unique_id": self.unique_id.strip(),
            "fingerprint": self.fingerprint.strip(),
            "targets": targets,
        }
        # 空串不写进 .env：写一个 "message": "" 会让"这个账号到底有没有单独配过"
        # 在文件里看不出区别，主程序那边也把空串当"没配"，两种写法等价但这里更干净。
        # 有内容就**必须带上** —— 这个工具不编辑它，可它是整条重建，漏了就丢了。
        text = str(self.message or "")
        if text.strip():
            task["message"] = text
        # 发送时间同一个道理：空串不写进 .env（= 跟全局 TASK_SCHEDULE_TIMES），
        # 有值就**必须带上** —— to_task() 是整条重建，漏了就是静默改配置。
        at = str(self.time or "").strip()
        if at:
            task["time"] = at
        # 停用状态同理，但方向相反：**缺键 = 启用**（主程序 core/schedule.py 与面板
        # 都这么认），所以只在停用时写 `"enabled": false`，启用的账号不落这个键 ——
        # 少一个键，手改 .env 时也少一处要看。
        if not self.enabled:
            task["enabled"] = False
        # 账号专属出口（TASKS[].proxy）同理：空串 = 跟全局，不落键；有值就必须带上 ——
        # 这个工具不编辑它，可 to_task() 是整条重建，漏了就是把出口换回另一个 IP。
        proxy = str(self.proxy or "").strip()
        if proxy:
            task["proxy"] = proxy
        return task


# ---------------------------------------------------------------------------
# 整体配置
# ---------------------------------------------------------------------------
@dataclass
class Config:
    proxy_address: str = DEFAULT_PROXY_ADDRESS
    # 浏览器状态快照目录（见 DEFAULT_BROWSER_STATE_DIR）。**空串回落默认值**：
    # 不能像 PROXY_ADDRESS 那样"空就写空"，否则等于把快照写到仓库根。
    browser_state_dir: str = DEFAULT_BROWSER_STATE_DIR
    # 浏览器时区（空 = 系统时区）与语言（空 = zh-CN）。
    browser_timezone: str = DEFAULT_BROWSER_TIMEZONE
    browser_locale: str = DEFAULT_BROWSER_LOCALE
    run_time: str = DEFAULT_RUN_TIME
    tz: str = DEFAULT_TZ
    # 内存里保存真实换行，写盘时才转成字面 \n
    message_template: str = DEFAULT_MESSAGE_TEMPLATE
    hitokoto_types: list = field(default_factory=lambda: list(DEFAULT_HITOKOTO_TYPES))
    browser_action_timeout: int = DEFAULT_BROWSER_ACTION_TIMEOUT
    im_scan_timeout: int = DEFAULT_IM_SCAN_TIMEOUT
    im_ready_timeout: int = DEFAULT_IM_READY_TIMEOUT
    friend_list_wait_time: int = DEFAULT_FRIEND_LIST_WAIT_TIME
    im_max_steps: int = DEFAULT_IM_MAX_STEPS
    task_retry_times: int = DEFAULT_TASK_RETRY_TIMES
    log_level: str = DEFAULT_LOG_LEVEL
    # 门铃重试上限（见 DEFAULT_SCHEDULE_SKIP_RETRY_MAX）。与上面的抖动键不同，
    # 这个键**要读回来**：0 是有意义的值（= 不限制），保存一次就被写回 6 会让人
    # 莫名其妙地"改不动"。
    schedule_skip_retry_max: int = DEFAULT_SCHEDULE_SKIP_RETRY_MAX
    # 发送抖动窗口（设定时间前后各 N 分钟，见 DEFAULT_SCHEDULE_JITTER_MINUTES，0 = 严格按点）。
    # 同 SCHEDULE_SKIP_RETRY_MAX：界面里没有输入框、但面板（webui）有，而且
    # **0 是有意义的值** —— 必须从 .env 读回来再原样写回去，否则保存一次就把
    # 用户在面板里设的 40 分钟抹回默认的 15（本次修的缺陷）。
    schedule_jitter_minutes: int = DEFAULT_SCHEDULE_JITTER_MINUTES
    # 自动清理间隔（见 DEFAULT_MAINTENANCE_AUTO_PRUNE_HOURS）。同 SCHEDULE_SKIP_RETRY_MAX：
    # 0 是有意义的值（= 关闭），必须读回来再写回去。
    maintenance_auto_prune_hours: int = DEFAULT_MAINTENANCE_AUTO_PRUNE_HOURS
    accounts: list = field(default_factory=list)

    # -- 序列化 -------------------------------------------------------------
    def schedule_times_text(self) -> str:
        """界面上那个 HH:MM:SS 时刻 → `TASK_SCHEDULE_TIMES` 要的 `HH:MM`。

        与 `CRON_*` 同源（都来自 `self.run_time`），所以面板里改时间时两者
        不会各说各话 —— 这正是本次要修的缺陷：以前只写了 `CRON_*`，
        程序内的发送闸门读不到，界面里设的 09:00 被静默忽略成默认的 20:30。
        """
        hour, minute, _second = split_run_time(self.run_time)
        return f"{hour}:{minute}"

    def to_env_map(self) -> dict:
        """生成 键 -> 值 的映射，值已经是能直接写进 .env 的形态。"""
        hour, minute, second = split_run_time(self.run_time)
        # 真换行 → 字面 \n 写进 .env，供 GUI 文本框里编辑。
        # \r\n 必须先于孤立 \r 收掉：这两条**是**有顺序的（\r\n 里的 \r 会被
        # 孤立 \r 规则单独吃掉，先跑后者就把 CRLF 拆成了两个 LF）。
        # 但「真」与「字面」两类规则之间顺序无所谓（字符集不相交）。
        # 字面 \r\n 不在这里收：那是磁盘上的形态，交由读取端（from_env_map）处理，
        # 保证 to_env_map / from_env_map 往返对称。
        template = (
            (self.message_template or "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\\n")
        )

        env = {
            "PROXY_ADDRESS": self.proxy_address or "",
            # 浏览器状态快照目录：界面没有输入框，值从 .env 读回来再原样写回去
            # （空串回落默认值，理由见 DEFAULT_BROWSER_STATE_DIR 的注释）。
            "BROWSER_STATE_DIR": (self.browser_state_dir or "").strip()
            or DEFAULT_BROWSER_STATE_DIR,
            # 时区留空是**合法值**（= 跟系统时区走），这里不能像上面那样回落默认
            # 值，否则用户特意清空的动作会被改写。
            "BROWSER_TIMEZONE": (self.browser_timezone or "").strip(),
            "BROWSER_LOCALE": (self.browser_locale or "").strip()
            or DEFAULT_BROWSER_LOCALE,
            # ① Docker 闹钟：docker/entrypoint-cron.sh 生成 cron 表达式时读这三个。
            #    ⚠️ systemd 部署下**完全无效**（没有任何 systemd 单元读它们）。
            "CRON_HOUR": hour,
            "CRON_MINUTE": minute,
            "CRON_SECOND": second,
            "TZ": self.tz or DEFAULT_TZ,
            "MESSAGE_TEMPLATE": template,
            "HITOKOTO_TYPES": json.dumps(
                self.hitokoto_types or [], ensure_ascii=False, separators=(",", ":")
            ),
            "BROWSER_ACTION_TIMEOUT": str(int(self.browser_action_timeout)),
            "IM_SCAN_TIMEOUT": str(int(self.im_scan_timeout)),
            "IM_READY_TIMEOUT": str(int(self.im_ready_timeout)),
            "FRIEND_LIST_WAIT_TIME": str(int(self.friend_list_wait_time)),
            "IM_MAX_STEPS": str(int(self.im_max_steps)),
            "TASK_RETRY_TIMES": str(int(self.task_retry_times)),
            "LOG_LEVEL": self.log_level or DEFAULT_LOG_LEVEL,
            # ② 程序内的发送闸门（core/tasks.py → core/schedule.py）：**只认这一个键**。
            #    取 HH:MM 是因为 schedule.parse_time 会把 "09:00:00" 判为非法并回落到
            #    默认的 20:30 —— 用户界面里设的 09:00 会被静默吃掉（本次修的缺陷）。
            #    秒级差异无关紧要：调度刻度是 5 分钟（schedule.TICK_MINUTES）。
            #    它同时也是界面时刻的来源：from_env_map 优先按这个键重建 run_time，
            #    面板里设的 20:30 读回来、再原样写回去，保存动作绝不覆盖它。
            "TASK_SCHEDULE_TIMES": self.schedule_times_text(),
            # ③ 抖动窗口：界面里没有输入框，但面板（webui）有，而且值是从 .env
            #    读回来的（含 0 = 严格按点）—— 读回来再写回去，绝不覆盖用户/面板
            #    的手改值。
            "SCHEDULE_JITTER_MINUTES": str(int(self.schedule_jitter_minutes)),
            # ④ 门铃重试上限：core/schedule.py 直读，界面没有输入框，
            #    但值是从 .env 读回来的（含 0），保存不会覆盖用户的手改值。
            "SCHEDULE_SKIP_RETRY_MAX": str(int(self.schedule_skip_retry_max)),
            # ⑤ 自动清理间隔：core/maintenance.py 直读，界面没有输入框，
            #    值从 .env 读回来（含 0 = 关闭），保存不会覆盖用户的手改值。
            "MAINTENANCE_AUTO_PRUNE_HOURS": str(int(self.maintenance_auto_prune_hours)),
            # TASKS 不走 unicode_escape，保持中文可读（与 index.html / .env.example 一致）
            "TASKS": json.dumps(
                [account.to_task() for account in self.accounts],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

        for account in self.accounts:
            # 抖音号为空时没有合法键名，跳过（校验环节会报错提醒）
            if not account.unique_id.strip():
                continue
            # Cookie 为空时**不写**这个键：写出 `COOKIES_x=`（空串）会让面板/
            # 本工具把"已配置"显示成真，而主程序每轮都会跳过没有 Cookie 的账号
            # —— 用户会在"看起来配好了、其实一条都不发"的状态里干等。
            # 有值的照写；想清空请用「清理旧变量」，别用空串顶掉。
            cookies = (account.cookies or "").strip()
            if cookies:
                env[account.cookies_key] = cookies

        return env

    def render_env_text(self) -> str:
        """「复制 .env 内容」用的文本，逐行 KEY=VALUE。"""
        return "\n".join(f"{key}={value}" for key, value in self.to_env_map().items())

    # -- 反序列化 -----------------------------------------------------------
    @classmethod
    def from_env_map(cls, mapping: dict) -> "Config":
        def text(key: str, default: str = "") -> str:
            value = mapping.get(key)
            return default if value is None else str(value)

        def number(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(float(text(key, str(default)).strip() or default))
            except (TypeError, ValueError, OverflowError):
                # OverflowError 也要接：`float("1e999")` 是合法的 inf，`int(inf)`
                # 抛的却是 OverflowError —— 漏掉它，手写一个天文数字会让整个配置
                # 读不进来（GUI 打开就崩），而这条路径本该"坏值回落默认值"。
                return default
            return max(low, min(high, value))

        accounts: list = []
        try:
            raw_tasks = json.loads(text("TASKS", "[]") or "[]")
        except json.JSONDecodeError:
            raw_tasks = []
        if not isinstance(raw_tasks, list):
            raw_tasks = []

        for task in raw_tasks:
            if not isinstance(task, dict):
                continue
            unique_id = str(task.get("unique_id", "") or "").strip()
            cookies_key = f"COOKIES_{unique_id.upper()}"
            raw_targets = task.get("targets") or []
            if not isinstance(raw_targets, list):
                raw_targets = [raw_targets]
            names = []
            meta = {}
            for raw in raw_targets:
                if isinstance(raw, dict):
                    name = str(raw.get("name") or raw.get("display") or "").strip()
                    if not name:
                        continue
                    names.append(name)
                    entry = {}
                    if str(raw.get("uid") or "").strip():
                        entry["uid"] = str(raw["uid"]).strip()
                    if str(raw.get("conv_id") or "").strip():
                        entry["conv_id"] = str(raw["conv_id"]).strip()
                    if entry:
                        meta.setdefault(name, []).append(entry)
                else:
                    # 别把这个局部变量叫 text —— 上面那个 text() 取值函数会被它遮住，
                    # 后面的 text(cookies_key) 就会 TypeError: 'str' object is not callable
                    # 口径与面板的 clean_targets 一致：None 丢掉，其余原样转字符串
                    plain = "" if raw is None else str(raw).strip()
                    if plain:
                        names.append(plain)
            accounts.append(
                Account(
                    username=str(task.get("username", "") or ""),
                    unique_id=unique_id,
                    cookies=text(cookies_key).strip(),
                    targets=names,
                    target_meta=meta,
                    # 老版本的 TASKS 里没有这个字段，取不到就留空
                    fingerprint=str(task.get("fingerprint", "") or "").strip(),
                    # 面板写的账号专属发送内容，本工具界面上不显示，
                    # 但重建 TASKS 时要原样带回去（见 to_task）
                    message=str(task.get("message", "") or ""),
                    # 同上：面板写的账号专属发送时间
                    time=str(task.get("time", "") or ""),
                    # 面板上的停用开关（TASKS[].enabled，缺键 = 启用）。同样不显示、
                    # 不编辑，但重建 TASKS 时必须原样带回去（见 to_task）。
                    enabled=task_enabled(task),
                    # 账号专属出口（TASKS[].proxy，issue #18）。同样不显示、不编辑，
                    # 但重建 TASKS 时必须原样带回去（见 to_task）。
                    proxy=str(task.get("proxy", "") or "").strip(),
                )
            )

        # 界面时刻的来源（按优先级）：
        #   ① `TASK_SCHEDULE_TIMES` 的第一个时刻 —— 程序内真正生效、面板也在改的键；
        #   ② `CRON_*` —— 只有 Docker 定时器读它，这里当老 .env 的兼容兜底；
        #   ③ 两个都认不出来才用 DEFAULT_RUN_TIME。
        # 以前这里**只**看 CRON_*：面板里设的 20:30 会在下一次保存时被改回 09:00。
        cron_time = build_run_time(
            text("CRON_HOUR", split_run_time(DEFAULT_RUN_TIME)[0]),
            text("CRON_MINUTE", split_run_time(DEFAULT_RUN_TIME)[1]),
            text("CRON_SECOND", split_run_time(DEFAULT_RUN_TIME)[2]),
        )
        schedule_time, schedule_has_seconds = parse_schedule_times(
            text("TASK_SCHEDULE_TIMES", "")
        )
        if not schedule_time:
            run_time = cron_time
        elif schedule_has_seconds or cron_time[:5] != schedule_time[:5]:
            run_time = schedule_time
        else:
            # `TASK_SCHEDULE_TIMES` 只存到分钟，界面上那个框却是 HH:MM:SS ——
            # 两个键的 HH:MM 一致时（= 它们本来就是同一个时刻写出来的）把秒继承
            # 回来，否则"改个别的字段保存一次"就会把用户设的 00:05:30 变成 00:05:00。
            # 不一致说明时间是被面板/手改过的，`CRON_SECOND` 是旧值，不能要。
            run_time = f"{schedule_time[:5]}:{cron_time[6:]}"

        try:
            hitokoto = json.loads(text("HITOKOTO_TYPES", "[]") or "[]")
        except json.JSONDecodeError:
            hitokoto = list(DEFAULT_HITOKOTO_TYPES)
        if not isinstance(hitokoto, list) or not hitokoto:
            hitokoto = list(DEFAULT_HITOKOTO_TYPES)

        return cls(
            proxy_address=text("PROXY_ADDRESS"),
            # 空/纯空白回落默认值（与 to_env_map 同口径，否则一个空的
            # BROWSER_STATE_DIR= 会把快照写到仓库根）。
            browser_state_dir=text("BROWSER_STATE_DIR", DEFAULT_BROWSER_STATE_DIR).strip()
            or DEFAULT_BROWSER_STATE_DIR,
            # 时区允许为空（= 系统时区）；语言为空时回落到 zh-CN。
            browser_timezone=text("BROWSER_TIMEZONE", DEFAULT_BROWSER_TIMEZONE).strip(),
            browser_locale=text("BROWSER_LOCALE", DEFAULT_BROWSER_LOCALE).strip()
            or DEFAULT_BROWSER_LOCALE,
            run_time=run_time,
            tz=text("TZ", DEFAULT_TZ) or DEFAULT_TZ,
            # 磁盘上是字面 \n，换回真实换行方便在文本框里编辑。
            # 字面 \r\n 先收成字面 \n 再解 —— 但注意 `.replace("\\n","\n")` 对
            # 字面 \r\n **匹配不到**（\r\n 里没有字面 \n），所以是那条 `\\r\\n` 规则
            # 在干活；两条规则字符集不相交，顺序其实无所谓，这里按可读性排。
            # ⚠️ 与 to_env_map() 成对：那边出的必须这边能读回来（往返幂等）。
            message_template=text("MESSAGE_TEMPLATE", DEFAULT_MESSAGE_TEMPLATE)
            .replace("\\r\\n", "\\n")
            .replace("\\n", "\n"),
            hitokoto_types=[str(item) for item in hitokoto],
            browser_action_timeout=number(
                "BROWSER_ACTION_TIMEOUT", DEFAULT_BROWSER_ACTION_TIMEOUT,
                *BROWSER_ACTION_TIMEOUT_RANGE
            ),
            im_scan_timeout=number(
                "IM_SCAN_TIMEOUT", DEFAULT_IM_SCAN_TIMEOUT, *IM_SCAN_TIMEOUT_RANGE
            ),
            im_ready_timeout=number(
                "IM_READY_TIMEOUT", DEFAULT_IM_READY_TIMEOUT, *IM_READY_TIMEOUT_RANGE
            ),
            friend_list_wait_time=number(
                "FRIEND_LIST_WAIT_TIME", DEFAULT_FRIEND_LIST_WAIT_TIME, *FRIEND_LIST_WAIT_RANGE
            ),
            im_max_steps=number(
                "IM_MAX_STEPS", DEFAULT_IM_MAX_STEPS, *IM_MAX_STEPS_RANGE
            ),
            task_retry_times=number(
                "TASK_RETRY_TIMES", DEFAULT_TASK_RETRY_TIMES, *RETRY_TIMES_RANGE
            ),
            log_level=text("LOG_LEVEL", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL,
            # 负数夹到 0（与 core/schedule.py::skip_retry_max_env 同口径），
            # 空/非数字回落默认值 6。
            schedule_skip_retry_max=number(
                "SCHEDULE_SKIP_RETRY_MAX", DEFAULT_SCHEDULE_SKIP_RETRY_MAX,
                *SCHEDULE_SKIP_RETRY_RANGE
            ),
            # 0 = 严格按点（有意义的值），空/非数字回落默认值 15，负数夹到 0。
            schedule_jitter_minutes=number(
                "SCHEDULE_JITTER_MINUTES", DEFAULT_SCHEDULE_JITTER_MINUTES,
                *SCHEDULE_JITTER_RANGE
            ),
            # 0 = 关闭（有意义的值），空/非数字回落默认值 168。
            maintenance_auto_prune_hours=number(
                "MAINTENANCE_AUTO_PRUNE_HOURS", DEFAULT_MAINTENANCE_AUTO_PRUNE_HOURS,
                *MAINTENANCE_AUTO_PRUNE_HOURS_RANGE
            ),
            accounts=accounts,
        )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate(config: Config) -> list:
    """返回 [(级别, 说明)]，级别为 '错误' 或 '警告'。

    目标是提前拦住那些「只在运行时才暴露」的问题，例如 config.py 里
    「缺少 COOKIES_xxx 环境变量，已跳过」这种静默跳过的坑。
    """
    issues: list = []

    hour, minute, second = split_run_time(config.run_time)
    if f"{hour}:{minute}:{second}" != (config.run_time or "").strip():
        issues.append(("错误", f"执行时间格式应为 HH:MM:SS，当前是「{config.run_time}」"))
    if not config.message_template.strip():
        issues.append(("错误", "消息模板不能为空"))
    if not config.hitokoto_types:
        issues.append(("警告", "一言类型一个都没勾选，[API] 可能拿不到内容"))
    if not config.accounts:
        issues.append(("错误", "至少要有一个账户"))

    seen: dict = {}
    for account in config.accounts:
        name = account.username.strip() or "（未命名账户）"
        unique_id = account.unique_id.strip()

        if not name or name == "（未命名账户）":
            issues.append(("警告", f"{name}：用户名只是日志标识，建议填一个"))
        if not unique_id:
            issues.append(("错误", f"{name}：抖音号必填，它决定 .env 里的 COOKIES_ 键名"))
        else:
            bad = sorted({ch for ch in unique_id if ch not in SAFE_ID_CHARS})
            if bad:
                issues.append(
                    (
                        "错误",
                        f"{name}：抖音号含非法字符 {' '.join(bad)}，会导致 {account.cookies_key} 不是合法的环境变量名",
                    )
                )
            key = account.cookies_key
            if key in seen:
                issues.append(
                    ("错误", f"{name}：抖音号与「{seen[key]}」重复，{key} 会互相覆盖")
                )
            else:
                seen[key] = name

        if not (account.cookies or "").strip():
            issues.append(
                ("错误", f"{name}：还没有 Cookies，点「刷新登录信息」会自动打开浏览器抓取")
            )
        else:
            try:
                data = json.loads(account.cookies)
            except json.JSONDecodeError as exc:
                issues.append(("错误", f"{name}：Cookies 不是合法 JSON（{exc.msg}）"))
            else:
                if not isinstance(data, list):
                    issues.append(("错误", f"{name}：Cookies 必须是 JSON 数组"))
                elif not data:
                    issues.append(("错误", f"{name}：Cookies 数组是空的"))

        if not account.targets:
            issues.append(("错误", f"{name}：至少要填一个目标好友"))

        if not account.fingerprint.strip():
            # 不拦着保存：没有配置目录的新账号本来也分不到指纹。
            # 但 TASKS 里的 fingerprint 会是空串，主程序若依赖它就会每次换指纹。
            issues.append(
                ("警告", f"{name}：还没有浏览器指纹，TASKS 里的 fingerprint 会是空的")
            )

    return issues
