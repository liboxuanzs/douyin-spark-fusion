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
# ⚠️ 大小写与 LOG_LEVEL_OPTIONS 保持一致（"Debug" 而非 "DEBUG"）：
# tkinter Combobox 对不在 values 里的值不会高亮匹配项 → 框看着是空的。
# utils.logger.resolve_log_level 内部 level.lower()，所以两种写法日志行为相同，
# 这里只为 GUI 显示正确。
DEFAULT_LOG_LEVEL = "Debug"

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


# ---------------------------------------------------------------------------
# 账户
# ---------------------------------------------------------------------------
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
        return task


# ---------------------------------------------------------------------------
# 整体配置
# ---------------------------------------------------------------------------
@dataclass
class Config:
    proxy_address: str = DEFAULT_PROXY_ADDRESS
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
    accounts: list = field(default_factory=list)

    # -- 序列化 -------------------------------------------------------------
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
            # TASKS 不走 unicode_escape，保持中文可读（与 index.html / .env.example 一致）
            "TASKS": json.dumps(
                [account.to_task() for account in self.accounts],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

        for account in self.accounts:
            # 抖音号为空时没有合法键名，跳过（校验环节会报错提醒）
            if account.unique_id.strip():
                env[account.cookies_key] = (account.cookies or "").strip()

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
            except (TypeError, ValueError):
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
                )
            )

        run_time = build_run_time(
            text("CRON_HOUR", split_run_time(DEFAULT_RUN_TIME)[0]),
            text("CRON_MINUTE", split_run_time(DEFAULT_RUN_TIME)[1]),
            text("CRON_SECOND", split_run_time(DEFAULT_RUN_TIME)[2]),
        )

        try:
            hitokoto = json.loads(text("HITOKOTO_TYPES", "[]") or "[]")
        except json.JSONDecodeError:
            hitokoto = list(DEFAULT_HITOKOTO_TYPES)
        if not isinstance(hitokoto, list) or not hitokoto:
            hitokoto = list(DEFAULT_HITOKOTO_TYPES)

        return cls(
            proxy_address=text("PROXY_ADDRESS"),
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
