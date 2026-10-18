"""configTool 配置数据层的纯逻辑单测（不依赖 tkinter）。

只测 `models.Config` 的序列化往返 —— 尤其是 MESSAGE_TEMPLATE 的换行编码，
这是 GUI（真换行）与 `.env`（字面 `\\n`）之间的桥，两侧必须严格对称。

⚠️ configTool 已经是包，直接 `from configTool import models` 即可
   （从仓库根跑测试时仓库根就在 sys.path 上）。models.py 是纯数据层，
   不 import tkinter，可以安全在无 GUI 环境跑。
"""
import ast
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from configTool import models


def _roundtrip(gui_text: str) -> tuple:
    """GUI 文本 → to_env_map（落盘形态）→ from_env_map（读回形态）。"""
    cfg = models.Config.from_env_map({})
    cfg.message_template = gui_text
    disk = cfg.to_env_map()["MESSAGE_TEMPLATE"]
    back = models.Config.from_env_map({"MESSAGE_TEMPLATE": disk}).message_template
    return disk, back


class MessageTemplateRoundTripTests(unittest.TestCase):
    """GUI ↔ .env 的换行编码必须往返对称。"""

    def test_real_newlines_become_literal(self):
        """GUI 里的真换行 → 磁盘上是字面 \\n（单行）。"""
        disk, back = _roundtrip("甲\n乙")
        self.assertEqual(disk, "甲\\n乙")
        self.assertEqual(back, "甲\n乙")

    def test_three_lines_roundtrip(self):
        gui = "[盖瑞]今日火花[加一]\n—— 每日一句 ——\n[API]"
        disk, back = _roundtrip(gui)
        self.assertEqual(disk, "[盖瑞]今日火花[加一]\\n—— 每日一句 ——\\n[API]")
        self.assertEqual(back, gui)

    def test_real_crlf_is_normalized_not_doubled(self):
        """★ 真 CRLF 必须收成**一个**字面 \\n，不能变成两个（空一行）。

        这是唯一真正依赖 replace 顺序的地方：
          正确 = replace("\\r\\n","\\n") 再 replace("\\r","\\n")
          写反 = 先 replace("\\r","\\n")，CRLF 里的 \\r 被单独吃掉 → "\\n\\n"
        若顺序写反，GUI 里一个回车会变成空行。
        """
        disk, back = _roundtrip("甲\r\n乙")
        self.assertEqual(disk, "甲\\n乙", "真 CRLF 被拆成了两个字面 \\n（多空一行）")
        self.assertEqual(back, "甲\n乙")

    def test_bare_cr_is_normalized(self):
        """孤立 \\r 也要收成换行。"""
        disk, _ = _roundtrip("甲\r乙")
        self.assertEqual(disk, "甲\\n乙")

    def test_disk_literal_crlf_is_read_back_without_leaking_cr(self):
        """磁盘上是字面 \\r\\n（老 .env 的写法）→ 读回真换行，不留裸露 \\r。"""
        back = models.Config.from_env_map(
            {"MESSAGE_TEMPLATE": "甲\\r\\n乙"}
        ).message_template
        self.assertEqual(back, "甲\n乙")
        self.assertNotIn("\r", back, "读回后残留了裸露的 \\r")

    def test_empty_and_single_line(self):
        for gui in ("", "单行"):
            with self.subTest(gui=gui):
                disk, back = _roundtrip(gui)
                self.assertEqual(back, gui)

    def test_to_env_map_is_idempotent(self):
        """跑两遍 to_env_map 结果必须一致（GUI 反复保存不会漂移）。"""
        cfg = models.Config.from_env_map({})
        cfg.message_template = "甲\n乙\r\n丙"
        first = cfg.to_env_map()["MESSAGE_TEMPLATE"]
        again = models.Config.from_env_map(
            cfg.to_env_map()
        ).to_env_map()["MESSAGE_TEMPLATE"]
        self.assertEqual(first, again)


class EnvKeysTests(unittest.TestCase):
    """BASE_ENV_KEYS 与 dataclass / to_env_map / from_env_map 四处必须齐。"""

    def test_no_key_is_silently_dropped(self):
        m = models.Config.from_env_map({}).to_env_map()
        missing = [k for k in models.BASE_ENV_KEYS if k not in m]
        extra = [k for k in m if k not in models.BASE_ENV_KEYS]
        self.assertEqual(missing, [], f"这些键在 to_env_map 里丢了: {missing}")
        self.assertEqual(extra, [], f"这些键没登记进 BASE_ENV_KEYS: {extra}")

    def test_env_map_is_flat_strings(self):
        """落盘的每个值都必须是 str（dotenv 只认字符串）。"""
        for k, v in models.Config.from_env_map({}).to_env_map().items():
            with self.subTest(key=k):
                self.assertIsInstance(v, str, f"{k} 不是字符串")


class DefaultsAlignmentTests(unittest.TestCase):
    """★ 各处的默认值必须一致（2026-09-19 对齐过一轮，2026-10-14 补上第 5 处）。

    同一个键有五个"默认"来源，历史上已经漂移过两次：
        configTool/models.py  DEFAULT_*        —— GUI 新建配置的初始值
        .env.example                            —— 给人抄的示例值
        docs/static/js/main.js  form            —— 网页版初始值
        utils/config.py  os.getenv(..., 兜底)   —— 程序无 .env 时的底裤
        webui/settings.py  Setting(default=)    —— 面板卡片显示 / 写回 .env 的值

    漂移不会报错，只会让"干净克隆"和"GUI 生成"两条路径行为不同 —— 极难排查。
    这个测试把它们钉在一起（面板只登记了一部分键，有的键才参与比对）。

    ⚠️ docs/main.js 是手抄的、没有构建期联动，是最容易漏的一处。
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls):
        import re

        cls.re = re
        # ① configTool DEFAULT_*
        cls.ct = {
            k[len("DEFAULT_"):]: v
            for k, v in vars(models).items()
            if k.startswith("DEFAULT_") and not k.startswith("DEFAULT_HITOKOTO")
        }
        # ② .env.example
        cls.env = dict(
            re.findall(
                r"^([A-Z_]+)=(.*)$",
                (cls.ROOT / ".env.example").read_text(encoding="utf-8"),
                re.M,
            )
        )
        # ③ docs/static/js/main.js 的 form 初始值
        js = (cls.ROOT / "docs/static/js/main.js").read_text(encoding="utf-8")
        block = js.split("const form = reactive({", 1)[1].split("ACCOUNTS:", 1)[0]
        cls.js = dict(re.findall(r'^\s{6}([A-Z_]+):\s*"?(.*?)"?,?\s*$', block, re.M))
        # ④ utils/config.py 的 os.getenv 兜底
        source = (cls.ROOT / "utils/config.py").read_text(encoding="utf-8")
        cls.pyf = dict(re.findall(r'os\.getenv\("([A-Z_]+)",\s*"([^"]*)"\)', source))
        # 2026-10 全盘审查：裸 os.getenv 换成了 _env_int/_env_ms/_env_json（一个好手滑的
        # 值不再让整个进程在 import 期死掉），默认值还是要跟 .env.example / docs /
        # configTool 四处对齐，所以把新写法也一并抠出来。
        cls.pyf.update(re.findall(r'_env_(?:int|ms|number)\(\s*"([A-Z_]+)",\s*([0-9.]+)', source))
        # ⑤ webui/settings.py 的 `Setting(..., default=...)` —— 面板卡片上显示、
        #    并会写回 .env 的那一份默认值（.env 里没这个键时它就是唯一可见值）。
        panel_src = (cls.ROOT / "webui" / "settings.py").read_text(encoding="utf-8")
        cls.panel = {}
        for match in re.finditer(r'Setting\(\s*\n\s*"([A-Z_]+)",(.*?)\n    \),', panel_src, re.S):
            found = re.search(r'default="([^"]*)"', match.group(2))
            if found:
                cls.panel[match.group(1)] = found.group(1)

    def _check(self, env_key, default_attr):
        ct = self.ct.get(default_attr)
        env = self.env.get(env_key)
        js = self.js.get(env_key)
        pyf = self.pyf.get(env_key)
        places = {"configTool": str(ct), ".env.example": str(env),
                  "docs": str(js), "config.py": str(pyf)}
        if env_key in self.panel:                    # 面板只管一部分键，有的就一起钉
            places["webui/settings.py"] = self.panel[env_key]
        self.assertEqual(
            len(set(places.values())), 1,
            f"{env_key} 各处默认值不一致: {places}",
        )

    def test_ready_timeout_aligned(self):
        self._check("IM_READY_TIMEOUT", "IM_READY_TIMEOUT")

    def test_friend_list_wait_aligned(self):
        self._check("FRIEND_LIST_WAIT_TIME", "FRIEND_LIST_WAIT_TIME")

    def test_log_level_aligned(self):
        self._check("LOG_LEVEL", "LOG_LEVEL")

    def test_all_numeric_defaults_aligned(self):
        for env_key, attr in [
            ("BROWSER_ACTION_TIMEOUT", "BROWSER_ACTION_TIMEOUT"),
            ("IM_SCAN_TIMEOUT", "IM_SCAN_TIMEOUT"),
            ("IM_MAX_STEPS", "IM_MAX_STEPS"),
            ("TASK_RETRY_TIMES", "TASK_RETRY_TIMES"),
        ]:
            with self.subTest(key=env_key):
                self._check(env_key, attr)

    def test_log_level_matches_dropdown_option(self):
        """★ 默认日志级别必须**逐字**等于下拉框里的选项。

        tkinter Combobox 对不在 values 里的值不会高亮匹配项 → 框看着是空的，
        用户会以为没设置。`utils.logger` 内部 upper/lower 了，所以两种写法
        日志行为**相同**，踩坑时不会报错、只会在 GUI 上显示异常。
        """
        self.assertIn(
            models.DEFAULT_LOG_LEVEL, models.LOG_LEVEL_OPTIONS,
            f"{models.DEFAULT_LOG_LEVEL!r} 不在下拉选项 {models.LOG_LEVEL_OPTIONS} 里",
        )

    def test_docs_log_level_matches_dropdown_option(self):
        """网页版同上：main.js 的默认值必须在其 log_level_options 里。"""
        js = (self.ROOT / "docs/static/js/main.js").read_text(encoding="utf-8")
        options = self.re.findall(r'value:\s*"(\w+)"', js)
        self.assertIn(self.js["LOG_LEVEL"], options,
                      f"{self.js['LOG_LEVEL']!r} 不在网页版选项 {options} 里")


class TargetsRoundTripTests(unittest.TestCase):
    """面板写进 TASKS 的**带身份目标**，configTool 开一次再存不能丢。

    这是两个工具共用一份 .env 最容易出事的地方：面板勾好友时写的是对象
    （`{"name","uid","conv_id"}`，主程序靠 id 精确匹配），而 configTool 的界面上
    只有名字。如果 configTool 读进来只留名字、写回时把对象变成字符串，
    用户的勾选就白做了 —— 下次发送退回名字匹配，重名/改名就会发错人。
    """

    IDENTITY = {
        "name": "示例号",
        "uid": "1122334455667788",
        "conv_id": "0:1:902043898218172:1122334455667788",
    }
    GROUP_ONLY = {"name": "妙妙屋", "conv_id": "7400789474252292647"}

    def _env(self, targets) -> dict:
        return {
            "TASKS": models.json.dumps(
                [{"username": "甲", "unique_id": "111", "fingerprint": "53435",
                  "targets": targets}],
                ensure_ascii=False, separators=(",", ":"),
            )
        }

    def _roundtrip(self, targets) -> list:
        cfg = models.Config.from_env_map(self._env(targets))
        text = cfg.to_env_map()["TASKS"]
        return models.json.loads(text)[0]["targets"]

    def test_identity_targets_survive(self):
        self.assertEqual(self._roundtrip([self.IDENTITY]), [self.IDENTITY])

    def test_group_target_without_uid_survives(self):
        self.assertEqual(self._roundtrip([self.GROUP_ONLY]), [self.GROUP_ONLY])

    def test_plain_names_stay_plain_strings(self):
        """手写的老目标不能被升级成对象（.env 会变得没法手改）。"""
        self.assertEqual(self._roundtrip(["好友A", "好友B"]), ["好友A", "好友B"])

    def test_mixed_list_keeps_order_and_shape(self):
        mixed = ["好友A", self.IDENTITY, "好友B", self.GROUP_ONLY]
        self.assertEqual(self._roundtrip(mixed), mixed)

    def test_two_people_with_the_same_name_keep_their_own_ids(self):
        one = {"name": "小明", "uid": "1", "conv_id": "0:1:2:1"}
        two = {"name": "小明", "uid": "2", "conv_id": "0:1:2:2"}
        self.assertEqual(self._roundtrip([one, two]), [one, two])

    def test_junk_shapes_do_not_crash(self):
        cfg = models.Config.from_env_map(self._env([{}, None, 3, "好友A"]))
        out = models.json.loads(cfg.to_env_map()["TASKS"])[0]["targets"]
        self.assertEqual(out, ["3", "好友A"], "空对象和 None 丢掉，其余照留")

    def test_string_target_does_not_shadow_the_key_reader(self):
        """★ 回归：普通名字目标曾经会把 cookies 读崩。

        `from_env_map` 里那个取值的闭包叫 `text`，而处理字符串目标时又写了
        `text = str(raw).strip()` —— 闭包被遮住，接下来 `text(cookies_key)`
        就 `TypeError: 'str' object is not callable`。凡是 TASKS 里有手写名字目标的
        用户，configTool 一打开就炸（面板写的带身份目标反而不触发，所以两边一直没对上）。
        """
        env = self._env(["好友A"])
        env["COOKIES_111"] = '[{"name":"sessionid","value":"v"}]'
        cfg = models.Config.from_env_map(env)          # 不许抛
        account = cfg.accounts[0]
        self.assertEqual(account.targets, ["好友A"])
        self.assertTrue(account.has_cookies, "cookies 必须真的读出来")
        self.assertEqual(account.cookie_count, 1)
        self.assertEqual(account.cookie_status, "已获取 1 项")


class ScheduleTimeEmissionTests(unittest.TestCase):
    """★ 2026-10 全盘审查：configTool 以前只写 CRON_*，程序内的发送闸门读不到。

    `CRON_HOUR/MINUTE/SECOND` 只有 Docker 定时器（docker/entrypoint-cron.sh）
    读，systemd 部署下**没有任何东西**看它们；真正决定"每个账号几点发"的是
    `TASK_SCHEDULE_TIMES`（core/tasks.py → core/schedule.py 的 fallback），
    读不出来还会回落到 `core/schedule.py::DEFAULT_TIME = "20:30"`。

    后果：用户在界面上设 09:00，实际按 20:30 + 抖动发 —— 静默、无报错。
    所以这里把"界面时刻 → 两个键同时正确"钉死。
    """

    def test_ui_time_is_written_to_task_schedule_times(self):
        for ui, expected in [("09:00:00", "09:00"),
                             ("17:20:00", "17:20"),
                             ("23:59:59", "23:59")]:
            with self.subTest(ui=ui):
                env = models.Config(run_time=ui).to_env_map()
                self.assertEqual(env["TASK_SCHEDULE_TIMES"], expected)

    def test_cron_keys_are_still_written_for_docker(self):
        """CRON_* 仍是 Docker 定时器的输入，不能被"修死键"顺手删掉。"""
        env = models.Config(run_time="09:30:15").to_env_map()
        self.assertEqual(
            [env["CRON_HOUR"], env["CRON_MINUTE"], env["CRON_SECOND"]],
            ["09", "30", "15"])

    def test_both_keys_come_from_the_same_ui_value(self):
        """同一个时刻必须同时进两个键，否则又会漂移成两套时间。"""
        env = models.Config(run_time="06:45:07").to_env_map()
        self.assertEqual(env["TASK_SCHEDULE_TIMES"],
                         env["CRON_HOUR"] + ":" + env["CRON_MINUTE"])

    def test_jitter_is_emitted_and_matches_runtime_default(self):
        env = models.Config.from_env_map({}).to_env_map()
        self.assertEqual(env["SCHEDULE_JITTER_MINUTES"],
                         str(models.DEFAULT_SCHEDULE_JITTER_MINUTES))

    def test_schedule_keys_are_registered(self):
        """写进 .env 的键必须在 BASE_ENV_KEYS 里（否则 EnvKeysTests 认不出）。"""
        for key in ("TASK_SCHEDULE_TIMES", "SCHEDULE_JITTER_MINUTES"):
            with self.subTest(key=key):
                self.assertIn(key, models.BASE_ENV_KEYS)

    def test_run_time_round_trips_through_env(self):
        """面板改完时间存盘、再打开，界面时刻不能变。"""
        for ui in ("09:00:00", "17:20:00", "00:05:30"):
            with self.subTest(ui=ui):
                cfg = models.Config(run_time=ui)
                back = models.Config.from_env_map(cfg.to_env_map())
                self.assertEqual(back.run_time, ui)
                self.assertEqual(back.to_env_map()["TASK_SCHEDULE_TIMES"],
                                 ui[:5])


class SkipRetryKeyTests(unittest.TestCase):
    """`SCHEDULE_SKIP_RETRY_MAX`（2026-10 新增，core/schedule.py 直读）的登记一致性。

    这个键界面里**没有输入框**（跟 `SCHEDULE_JITTER_MINUTES` 一样），但它必须
    出现在 `BASE_ENV_KEYS` 与 `to_env_map()` 里 —— 否则 configTool 保存一次就把它
    从用户的 .env 里删掉了（`EnvKeysTests` 两个方向都管着）；而它还必须能**读回来**，
    因为 0 是有意义的值（= 关掉重试上限），被默认值 6 覆盖掉等于这个开关不存在。
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_default_matches_runtime(self):
        """configTool 的默认值必须等于 core/schedule.py 的 DEFAULT_SKIP_RETRY_MAX。

        两处漂移不会报错，只会让"GUI 生成的 .env"和"程序内置默认"行为不同。
        """
        from core import schedule

        self.assertEqual(models.DEFAULT_SCHEDULE_SKIP_RETRY_MAX,
                         schedule.DEFAULT_SKIP_RETRY_MAX)

    def test_example_file_registers_the_key(self):
        """.env.example 抄出来的值也要等于运行时默认值（否则"抄示例"就漂了）。"""
        for line in (self.ROOT / ".env.example").read_text(
                encoding="utf-8").splitlines():
            if line.startswith("SCHEDULE_SKIP_RETRY_MAX="):
                self.assertEqual(line.split("=", 1)[1].strip(), "6")
                break
        else:
            self.fail(".env.example 里没有登记 SCHEDULE_SKIP_RETRY_MAX")

    def test_key_is_registered_and_emitted(self):
        self.assertIn("SCHEDULE_SKIP_RETRY_MAX", models.BASE_ENV_KEYS)
        env = models.Config.from_env_map({}).to_env_map()
        self.assertEqual(env["SCHEDULE_SKIP_RETRY_MAX"], "6",
                         "新建配置必须写默认值 6")

    def test_manual_value_survives_a_save(self):
        """用户手改的值（含 0 = 不限制）不能被一次保存抹回默认值。"""
        for raw in ("0", "3", "288"):
            with self.subTest(raw=raw):
                env = models.Config.from_env_map(
                    {"SCHEDULE_SKIP_RETRY_MAX": raw}).to_env_map()
                self.assertEqual(env["SCHEDULE_SKIP_RETRY_MAX"], raw)

    def test_junk_value_falls_back(self):
        """空/非数字回落默认值 6；负数按 core/schedule.py 的口径夹到 0。"""
        for raw, expected in [("", "6"), ("abc", "6"), ("-1", "0"), ("9999", "288")]:
            with self.subTest(raw=raw):
                env = models.Config.from_env_map(
                    {"SCHEDULE_SKIP_RETRY_MAX": raw}).to_env_map()
                self.assertEqual(env["SCHEDULE_SKIP_RETRY_MAX"], expected)


class AutoPruneKeyTests(unittest.TestCase):
    """`MAINTENANCE_AUTO_PRUNE_HOURS`（2026-10 新增，core/maintenance.py 直读）的登记一致性。

    和 `SCHEDULE_SKIP_RETRY_MAX` 同一类东西：界面里**没有输入框**，但它必须
    出现在 `BASE_ENV_KEYS` 与 `to_env_map()` 里（否则 configTool 保存一次就把它从
    用户的 .env 里删掉了），而且必须能**读回来** —— 0 是有意义的值（= 关掉自动清理）。

    为什么值得单独钉：这个键管的是"磁盘别被 Chrome 缓存吃满"，配置漂移的后果
    不是报错，而是几个月后发送突然失败（磁盘满）—— 属于最难查的那一类。
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_default_matches_runtime(self):
        """configTool 的默认值必须等于 core/maintenance.py 的 DEFAULT_AUTO_PRUNE_HOURS。"""
        from core import maintenance

        self.assertEqual(float(models.DEFAULT_MAINTENANCE_AUTO_PRUNE_HOURS),
                         float(maintenance.DEFAULT_AUTO_PRUNE_HOURS))

    def test_example_file_registers_the_key(self):
        """.env.example 抄出来的值也要等于运行时默认值（否则"抄示例"就漂了）。"""
        for line in (self.ROOT / ".env.example").read_text(
                encoding="utf-8").splitlines():
            if line.startswith("MAINTENANCE_AUTO_PRUNE_HOURS="):
                self.assertEqual(line.split("=", 1)[1].strip(), "168")
                break
        else:
            self.fail(".env.example 里没有登记 MAINTENANCE_AUTO_PRUNE_HOURS")

    def test_docs_mention_the_switch(self):
        """部署文档要提到它，否则运维不知道有这个开关。"""
        text = (self.ROOT / "docs/deploy.md").read_text(encoding="utf-8")
        self.assertIn("MAINTENANCE_AUTO_PRUNE_HOURS", text)

    def test_key_is_registered_and_emitted(self):
        self.assertIn("MAINTENANCE_AUTO_PRUNE_HOURS", models.BASE_ENV_KEYS)
        env = models.Config.from_env_map({}).to_env_map()
        self.assertEqual(env["MAINTENANCE_AUTO_PRUNE_HOURS"], "168",
                         "新建配置必须写默认值 168")

    def test_manual_value_survives_a_save(self):
        """用户手改的值（含 0 = 关闭）不能被一次保存抹回默认值。"""
        for raw in ("0", "24", "8760"):
            with self.subTest(raw=raw):
                env = models.Config.from_env_map(
                    {"MAINTENANCE_AUTO_PRUNE_HOURS": raw}).to_env_map()
                self.assertEqual(env["MAINTENANCE_AUTO_PRUNE_HOURS"], raw)

    def test_junk_value_falls_back(self):
        """空/非数字回落默认值 168；负数夹到 0（= 关闭）；超上限夹到 8760。"""
        for raw, expected in [("", "168"), ("abc", "168"), ("-1", "0"), ("99999", "8760")]:
            with self.subTest(raw=raw):
                env = models.Config.from_env_map(
                    {"MAINTENANCE_AUTO_PRUNE_HOURS": raw}).to_env_map()
                self.assertEqual(env["MAINTENANCE_AUTO_PRUNE_HOURS"], expected)


class ScheduleTimesSurviveSaveTests(unittest.TestCase):
    """面板/手改的发送时间与抖动窗口，GUI 保存时不能被静默改写。

    缺陷现场：`to_env_map()` 无条件写 TASK_SCHEDULE_TIMES（由 run_time 推出）和
    SCHEDULE_JITTER_MINUTES（恒为默认 15），而 `from_env_map()` 只认 CRON_* ——
    于是 GUI 里随便改个字段，1.5 秒后的自动保存（main.AUTOSAVE_DELAY_MS）就把
    面板里设好的 20:30 / 40 分钟改回 09:00 / 15，静默且无报错。
    修法：这两个键都"读回来、再原样写回去"。
    """

    def test_manual_task_schedule_times_survives_a_save(self):
        """TASK_SCHEDULE_TIMES 的手改值经 from_env_map → to_env_map 存活。"""
        for raw, expected in [
            ("20:30", "20:30"),
            ("00:00", "00:00"),
            ("23:59", "23:59"),
            (" 20:30 ", "20:30"),
            # 秒不写回 TASK_SCHEDULE_TIMES —— core/schedule.py::parse_time 只认
            # HH:MM，写 "20:30:45" 会被判非法、整条回落到 20:30
            ("20:30:45", "20:30"),
            ("07:05:00", "07:05"),
        ]:
            with self.subTest(raw=raw):
                env = models.Config.from_env_map({"TASK_SCHEDULE_TIMES": raw}).to_env_map()
                self.assertEqual(env["TASK_SCHEDULE_TIMES"], expected)

    def test_seconds_come_back_from_cron_when_the_minute_matches(self):
        """界面时间带秒时也要能往返：TASK 只有 HH:MM，就从 CRON_SECOND 取回秒。"""
        cfg = models.Config.from_env_map({
            "TASK_SCHEDULE_TIMES": "20:30",
            "CRON_HOUR": "20",
            "CRON_MINUTE": "30",
            "CRON_SECOND": "45",
        })
        self.assertEqual(cfg.run_time, "20:30:45")
        self.assertEqual(cfg.to_env_map()["TASK_SCHEDULE_TIMES"], "20:30")

    def test_stale_cron_second_is_not_inherited(self):
        """TASK 与 CRON 不是同一时刻时，CRON_SECOND 是旧值，不能拿来拼。"""
        cfg = models.Config.from_env_map({
            "TASK_SCHEDULE_TIMES": "20:30",
            "CRON_HOUR": "09",
            "CRON_MINUTE": "00",
            "CRON_SECOND": "45",
        })
        self.assertEqual(cfg.run_time, "20:30:00")

    def test_manual_jitter_survives_a_save(self):
        """0 是有意义的值（严格按点、不抖动），必须读回来再写回去。"""
        for raw in ("0", "3", "40", "15", "1440"):
            with self.subTest(raw=raw):
                env = models.Config.from_env_map({"SCHEDULE_JITTER_MINUTES": raw}).to_env_map()
                self.assertEqual(env["SCHEDULE_JITTER_MINUTES"], raw)

    def test_jitter_junk_falls_back_to_default(self):
        for raw in ("", "abc", "   ", "1e999"):
            with self.subTest(raw=raw):
                env = models.Config.from_env_map({"SCHEDULE_JITTER_MINUTES": raw}).to_env_map()
                self.assertEqual(
                    env["SCHEDULE_JITTER_MINUTES"],
                    str(models.DEFAULT_SCHEDULE_JITTER_MINUTES),
                )

    def test_jitter_is_clamped_to_range(self):
        cases = [("99999", str(models.SCHEDULE_JITTER_RANGE[1])), ("-5", "0")]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                env = models.Config.from_env_map({"SCHEDULE_JITTER_MINUTES": raw}).to_env_map()
                self.assertEqual(env["SCHEDULE_JITTER_MINUTES"], expected)

    def test_cron_still_works_when_task_key_is_absent(self):
        """老 .env 只有 CRON_*（Docker 定时器那套）时照旧走兼容路径。"""
        env = models.Config.from_env_map(
            {"CRON_HOUR": "17", "CRON_MINUTE": "5", "CRON_SECOND": "0"}
        ).to_env_map()
        self.assertEqual(env["TASK_SCHEDULE_TIMES"], "17:05")

    def test_task_schedule_times_wins_over_cron(self):
        """两个键都在（面板设过时间，Docker 变量还留着旧值）时以 TASK 为准。"""
        cfg = models.Config.from_env_map(
            {"TASK_SCHEDULE_TIMES": "20:30", "CRON_HOUR": "09", "CRON_MINUTE": "00"}
        )
        self.assertEqual(cfg.run_time[:5], "20:30")
        self.assertEqual(cfg.to_env_map()["TASK_SCHEDULE_TIMES"], "20:30")

    def test_junk_task_key_falls_back_to_cron(self):
        for raw in ("", "abc", "25:99", "09:00:00:00"):
            with self.subTest(raw=raw):
                env = models.Config.from_env_map({
                    "TASK_SCHEDULE_TIMES": raw,
                    "CRON_HOUR": "17",
                    "CRON_MINUTE": "05",
                }).to_env_map()
                self.assertEqual(env["TASK_SCHEDULE_TIMES"], "17:05")

    def test_multiple_times_take_the_first(self):
        """core/schedule.py::fallback_time 取第一项；界面也只有一个时间输入框。"""
        env = models.Config.from_env_map(
            {"TASK_SCHEDULE_TIMES": "09:00,20:30"}
        ).to_env_map()
        self.assertEqual(env["TASK_SCHEDULE_TIMES"], "09:00")


class EmptyCookieKeyTests(unittest.TestCase):
    """没有 Cookie 的账号**不写** COOKIES_x=（空值会被面板当成"已配置"）。

    空串会让 webui 的"已配置"判定为真，而主程序每轮都跳过没有 Cookie 的账号 ——
    用户会在"看起来配好了、其实一条都不发"的状态里干等。
    """

    @staticmethod
    def _config(cookies: str, unique_id: str = "12345678901") -> "models.Config":
        return models.Config(accounts=[models.Account(unique_id=unique_id, cookies=cookies)])

    def test_empty_cookies_are_not_written(self):
        env = self._config("").to_env_map()
        self.assertNotIn("COOKIES_12345678901", env)
        # 账号本身还在 TASKS 里（不然保存一次账号就没了）
        self.assertIn("12345678901", env["TASKS"])

    def test_whitespace_only_cookies_are_not_written(self):
        self.assertNotIn("COOKIES_12345678901", self._config("   ").to_env_map())

    def test_non_empty_cookies_are_written(self):
        self.assertEqual(self._config("[]").to_env_map()["COOKIES_12345678901"], "[]")

    def test_empty_key_stays_empty_after_round_trip(self):
        cfg = self._config("")
        back = models.Config.from_env_map(cfg.to_env_map())
        self.assertEqual(len(back.accounts), 1)
        self.assertEqual(back.accounts[0].cookies, "")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class LocalSettingsModeTests(unittest.TestCase):
    """本地设置里有明文口令，落盘必须是 0600（缺陷 D）。"""

    def test_saved_file_is_private(self):
        if os.name != "posix":
            self.skipTest("POSIX 权限位")
        from configTool import local_settings

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "local_settings.json"
            with mock.patch.object(local_settings, "SETTINGS_FILE", str(target)):
                local_settings.save_proxy({
                    "enabled": True,
                    "tunnel": "tunnel.example:443",
                    "user": "u",
                    "password": "p",
                })
            self.assertTrue(target.exists(), "设置文件没写出来")
            self.assertEqual(_mode(target), 0o600)
            # 旧版本以 644 落盘过：再存一次要收紧
            os.chmod(target, 0o644)
            with mock.patch.object(local_settings, "SETTINGS_FILE", str(target)):
                local_settings.save_proxy({"enabled": False})
            self.assertEqual(_mode(target), 0o600)


class GuiSourceGuardTests(unittest.TestCase):
    """GUI 改动只能在源码层面钉住（本机缺 libtk8.6.so，真窗口跑不起来）。

    每条守卫都对应一个实际缺陷：
      * browser_login.is_running 曾在 Tk 主线程调 ctx.cookies()，跨线程碰
        Playwright 抛 greenlet.error，被 except 吞成"浏览器没在运行"；
      * login_dialog 曾先显示「已登录 ✔」再回填，写盘失败时用户看到成功、
        窗口还在 1.2 秒后自动关掉，Cookie 白抓；
      * main.save_now 曾不返回成败，调用方拿不到任何信号。
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls):
        read = lambda name: (cls.ROOT / "configTool" / name).read_text(encoding="utf-8")
        cls.browser = read("browser_login.py")
        cls.dialog = read("login_dialog.py")
        cls.main = read("main.py")
        cls.settings = read("local_settings.py")

    @staticmethod
    def _body(source: str, name: str) -> str:
        """抠出 `def name(` 那一段（到下一个同级 def/class 之前）。"""
        out: list = []
        indent = None
        for line in source.splitlines():
            if indent is None:
                if line.lstrip().startswith(f"def {name}("):
                    indent = len(line) - len(line.lstrip())
                    out.append(line)
                continue
            stripped = line.strip()
            if (
                stripped
                and (len(line) - len(line.lstrip())) <= indent
                and (stripped.startswith("def ") or stripped.startswith("class "))
            ):
                break
            out.append(line)
        assert out, f"没有找到 def {name}(（源文件改过名？）"
        return "\n".join(out)

    @classmethod
    def _code_body(cls, source: str, name: str) -> str:
        """函数体的**可执行代码**（不含 docstring）——注释和文档里提到 self.ctx
        是说明，不算"碰了 Playwright 对象"。"""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = [
                    item for item in node.body
                    if not (
                        isinstance(item, ast.Expr)
                        and isinstance(item.value, ast.Constant)
                        and isinstance(item.value.value, str)
                    )
                ]
                if not body:
                    return ""
                lines = source.splitlines()
                return "\n".join(lines[body[0].lineno - 1:body[-1].end_lineno])
        raise AssertionError(f"没有找到 def {name}(（源文件改过名？）")

    # -- browser_login -------------------------------------------------------
    def test_is_running_does_not_touch_playwright(self):
        """is_running 会被 Tk 主线程调用，绝不能碰工作线程的 Playwright 对象。"""
        body = self._code_body(self.browser, "is_running")
        self.assertNotIn("self.ctx", body)
        self.assertNotIn("cookies(", body)
        self.assertIn("_browser_running", body)

    def test_running_flag_is_maintained_by_the_worker(self):
        for name in ("_open", "_close", "_reset"):
            with self.subTest(method=name):
                self.assertIn("_browser_running", self._body(self.browser, name))

    def test_first_goto_has_its_own_shorter_timeout(self):
        """开浏览器那次 goto 在工作线程里同步等：超时短一点，关窗才能尽快收尾。"""
        self.assertIn("OPEN_GOTO_TIMEOUT_MS", self.browser)
        self.assertIn("timeout=OPEN_GOTO_TIMEOUT_MS", self._body(self.browser, "_open"))

    # -- login_dialog --------------------------------------------------------
    def test_success_banner_comes_after_saving(self):
        body = self._body(self.dialog, "_apply_grab")
        save_at = body.index("if not self._notify_saved():")
        banner_at = body.index('self.var_login.set("已登录 ✔")')
        close_at = body.index("AUTO_CLOSE_DELAY_MS")
        self.assertLess(save_at, banner_at, "必须先落盘再报成功")
        self.assertLess(save_at, close_at, "必须先落盘再排自动关窗")
        self.assertIn("return", body[save_at:banner_at], "失败分支必须先 return")

    def test_dialog_watches_for_real_save_result(self):
        body = self._body(self.dialog, "_notify_saved")
        self.assertIn("result is False", body)
        self.assertIn("self._notified = False", body)

    def test_pump_only_treats_empty_queue_as_end(self):
        body = self._body(self.dialog, "_pump")
        self.assertIn("except queue.Empty", body)
        self.assertIn("import queue", self.dialog)
        self.assertNotIn("except Exception:\n            pass", body)

    # -- main ----------------------------------------------------------------
    def test_save_now_reports_whether_it_worked(self):
        body = self._body(self.main, "save_now")
        self.assertIn("-> bool", body)
        self.assertIn("return False", body)
        self.assertIn("return True", body)

    def test_login_saved_forces_a_save_and_returns_it(self):
        body = self._body(self.main, "_on_login_saved")
        self.assertIn("saved = self.save_now(force=True)", body)
        self.assertIn("return saved", body)

    def test_login_saved_also_requeues_todays_mark(self):
        """issue #12：configTool 是 README 里与面板并列的第二条正规登录路径。

        原来只有面板那半条路会撤当天的记账，于是同一份新 cookie，面板扫码进来当天补发、
        从这里扫码进来当天照样不发 —— 而两边显示的都是"登录成功了"。
        """
        body = self._body(self.main, "_on_login_saved")
        self.assertIn("self._requeue_after_relogin(account)", body)
        self.assertLess(
            body.index("save_now"),
            body.index("_requeue_after_relogin"),
            "必须先落盘再撤账：写盘失败还撤账 = 白扔一天",
        )

    def test_requeue_helper_uses_the_shared_schedule_function(self):
        """撤账判定只许有一份（`core/schedule.requeue_after_relogin`），别再自己判一遍。"""
        body = self._body(self.main, "_requeue_after_relogin")
        self.assertIn("schedule.requeue_after_relogin", body)
        self.assertNotIn("retract_mark", body, "判定属于 core/schedule，工具里不许再写一份")
        self.assertIn("self._set_message", body, "撤没撤必须说出来，不能让用户猜")

    def test_only_one_login_dialog_at_a_time(self):
        body = self._body(self.main, "_open_login_dialog")
        self.assertIn("self._login_dialog", body)
        self.assertIn("winfo_exists", body)
        self.assertIn("lift()", body)

    def test_close_confirms_when_saving_failed(self):
        body = self._body(self.main, "_on_close")
        self.assertNotIn("except Exception:\n            pass", body)
        self.assertIn("askyesno", body)
        self.assertIn("_shutdown_child_windows", body)

    def test_child_windows_are_shut_down_before_leaving(self):
        body = self._body(self.main, "_shutdown_child_windows")
        self.assertIn("_shutdown", body)
        self.assertIn("root.update()", body)


class DocsGeneratorParityTests(unittest.TestCase):
    """docs/static/js/main.js 是**另一个**官方 .env 生成器，口径必须一致。

    它以前跟 configTool 一样只写 CRON_*，而且 TASKS 里没有 fingerprint ——
    后者会让主程序每天给这个账号换一个设备指纹（docs/webui.md:161 承诺它是固定的）。
    网页版没有构建期联动，只能靠这里静态钉住。
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls):
        cls.js = (cls.ROOT / "docs/static/js/main.js").read_text(encoding="utf-8")

    def test_emits_task_schedule_times_derived_from_run_time(self):
        self.assertIn("TASK_SCHEDULE_TIMES", self.js)
        self.assertIn("form.RUN_TIME.slice(0, 5)", self.js,
                      "TASK_SCHEDULE_TIMES 必须由界面上的时刻推出（HH:MM）")

    def test_emits_jitter_key(self):
        self.assertIn("SCHEDULE_JITTER_MINUTES", self.js)

    def test_tasks_items_carry_fingerprint(self):
        self.assertIn("fingerprint: account.fingerprint", self.js,
                      "TASKS 少了 fingerprint，账号天天换设备指纹")

    def test_new_account_gets_a_fingerprint(self):
        self.assertIn("randomFingerprint()", self.js,
                      "新增账号时必须分配指纹种子")

    def test_fingerprint_range_matches_config_tool(self):
        """范围必须与 configTool/profile_store.py 的 10000~99999 一致。"""
        from configTool import profile_store
        self.assertIn(f"FINGERPRINT_MIN = {profile_store.FINGERPRINT_MIN}", self.js)
        self.assertIn(f"FINGERPRINT_MAX = {profile_store.FINGERPRINT_MAX}", self.js)

    def test_fingerprint_copy_is_present(self):
        """界面文案里必须有"不要改它"的提醒（否则用户重扫时顺手就改了）。"""
        html = (self.ROOT / "docs/generator/index.html").read_text(encoding="utf-8")
        self.assertIn("换号重扫不要改它", html)


if __name__ == "__main__":
    unittest.main()
