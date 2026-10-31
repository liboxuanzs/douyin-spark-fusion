"""面板静态资源与模板的**接线守卫**（不需要 fastapi，本机可跑）。

这一批断言守的是"模板挂了钩子、JS 却在找另一个名字"这类**静默失效**：页面能打开、
不报错、看起来一切正常，功能就是不动。2026-10-17 的前端审计一次性抓到三处
（分段控件整块死代码、被阻断账号的红边 10 秒后消失、日志页把"登录过期"说成
"刷新成功"），所以钉在源码文本上 —— 这类错误只有真在浏览器里点一遍才会暴露，
而我们的测试跑在无头环境里。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """把源码压成一行：断言可以跨那种被格式化折行的调用。"""
    return " ".join(text.split())


def _function_source(rel: str, name: str) -> str:
    """截出某个顶层函数的源码（到下一个顶格的 def/class/分节注释为止）。"""
    src = _read(rel)
    start = src.index(f"\ndef {name}(") + 1
    rest = src[start:]
    match = re.search(r"\n(?=(?:def |class |# ---))", rest)
    return rest[: match.start()] if match else rest


def _webui_sources() -> list:
    """webui/ 下的源码文本（跳过 __pycache__ 这类二进制缓存）。"""
    out = []
    for path in sorted((ROOT / "webui").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix not in {".py", ".html", ".js", ".css"}:
            continue
        out.append((str(path.relative_to(ROOT)), path.read_text(encoding="utf-8")))
    return out


class SegmentControlWiringTests(unittest.TestCase):
    """分段控件（dashboard 的空跑档位 / runs 的目标档位）必须真的能点。"""

    def test_app_js_认模板上真正挂的属性(self) -> None:
        src = _read("webui/static/app.js")
        self.assertIn("[data-segment-group], [data-segment-owner]", src)

    def test_模板挂的是_owner(self) -> None:
        for rel in ("webui/templates/dashboard.html", "webui/templates/runs.html"):
            with self.subTest(rel=rel):
                self.assertIn("data-segment-owner", _read(rel))


class AccountCardStateTests(unittest.TestCase):
    """账号卡首屏配色必须和轮询写回的值同一套词表。"""

    def test_模板用_state_css_而不是_state(self) -> None:
        src = _read("webui/templates/dashboard.html")
        self.assertIn('data-account-state="{{ account.state_css }}"', src)
        self.assertNotIn('data-account-state="{{ account.state }}"', src)

    def test_app_js_写回的是_stateCss(self) -> None:
        self.assertIn("account.stateCss || account.state", _read("webui/static/app.js"))


class OverviewPollingTests(unittest.TestCase):
    """轮询：同一时刻一个请求、失败退避、401 要明说登录过期。"""

    def setUp(self) -> None:
        self.src = _read("webui/static/app.js")

    def test_同一时刻只允许一个请求在飞(self) -> None:
        self.assertIn("inflight", self.src)
        self.assertIn("if (document.visibilityState !== \"visible\" || inflight) return;", self.src)

    def test_失败会退避(self) -> None:
        self.assertIn("Math.min(40000, 10000 * 2 ** Math.max(0, failures - 1))", self.src)
        self.assertIn("window.setTimeout(refresh, wait)", self.src)

    def test_401_说的是登录过期(self) -> None:
        self.assertIn('response.status === 401', self.src)
        self.assertIn("登录已过期，请重新登录", self.src)

    def test_只有一个_timer_声明(self) -> None:
        self.assertEqual(self.src.count("let timer = null;"), 1)


class LogsPageTests(unittest.TestCase):
    """日志页：登录过期不能被显示成"刷新成功"，切走/关闭要停轮询。"""

    def setUp(self) -> None:
        self.src = _read("webui/templates/logs.html")

    def test_拿不到_log_body_就算失败(self) -> None:
        self.assertIn("if (!body) {", self.src)
        self.assertIn("登录已过期（请重新登录）", self.src)
        self.assertNotIn('renderBody(body ? body.textContent : "")', self.src)

    def test_隐藏页不拉日志(self) -> None:
        self.assertIn("const refreshIfVisible = () => {", self.src)
        self.assertIn('document.visibilityState === "visible") refresh();', self.src)
        self.assertIn("setInterval(refreshIfVisible, 5000)", self.src)

    def test_pagehide_会清掉定时器(self) -> None:
        self.assertIn('window.addEventListener("pagehide"', self.src)


class RefreshEnvMemoTests(unittest.TestCase):
    """`store.refresh_env()` 不能每个请求都白解析两遍 .env。"""

    def setUp(self) -> None:
        self.src = _read("webui/store.py")

    def test_按_mtime_与_size_记忆化(self) -> None:
        self.assertIn("_ENV_REFRESH_KEY", self.src)
        self.assertIn("def _env_refresh_key()", self.src)
        self.assertIn("stat.st_mtime_ns, stat.st_size", self.src)

    def test_留了_force_出口(self) -> None:
        self.assertIn("def refresh_env(*, force: bool = False) -> bool:", self.src)


class OverviewPayloadFieldsTests(unittest.TestCase):
    """issue #2/#4：首屏用到、轮询必须重画的字段，`overview_payload` 一个都不能漏。

    漏发的后果不是报错而是"悄悄改错口径"：数字没变、说明被换掉，或者干脆停在首屏。
    """

    def setUp(self) -> None:
        self.src = _function_source("webui/viewmodel.py", "overview_payload")

    def test_summary_带上_mixedRuns_与轮数(self) -> None:
        self.assertIn('"mixedRuns": bool(summary.get("mixed_runs"))', self.src)
        self.assertIn('"runCount": _int(summary.get("run_count"))', self.src)

    def test_schedule_带上定时器挂了的诊断(self) -> None:
        self.assertIn('"timerStallText": schedule.get("timerStallText", "")', self.src)
        self.assertIn('"timerActiveState": schedule.get("timerActiveState", "")', self.src)

    def test_账号卡把会变的那几样也发了(self) -> None:
        for key in (
            '"stateLabel": card.get("state_label", "")',
            '"chip": card.get("chip", "")',
            '"runMissing": bool(card.get("run_missing"))',
            '"ownRunText": card.get("own_run_text", "")',
            '"sendWindow": card.get("send_window", "")',
            '"sendState": card.get("send_state", "")',
            '"sendStateText": card.get("send_state_text", "")',
            '"hasMessage": bool(card.get("has_message"))',
            '"messageLines": _int(card.get("message_lines"))',
        ):
            with self.subTest(key=key):
                self.assertIn(key, self.src)


class PollingHookWiringTests(unittest.TestCase):
    """issue #4：模板上的轮询挂点，app.js 必须真的去找它 —— 两头都不能少。"""

    HOOKS = (
        "data-overview-mixed-runs",
        "data-overview-mixed-runs-count",
        "data-account-state-text",
        "data-account-run-missing",
        "data-account-run-missing-own",
        "data-account-run-missing-never",
        "data-account-run-missing-own-text",
        "data-account-run-missing-tpl",
        "data-time-window",
        "data-time-state",
        "data-message-summary",
    )

    def test_每个挂点模板与_app_js_都有(self) -> None:
        template = _read("webui/templates/dashboard.html")
        js = _read("webui/static/app.js")
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                # 模板里是一个完整属性名；app.js 里是一个完整属性选择器 ——
                # 用 `[hook]` 而不是裸名，免得 `data-x` 被 `data-x-typo` 蒙过去。
                self.assertRegex(template, rf"(?<![-\w]){re.escape(hook)}[\s=>]")
                self.assertIn(f"[{hook}]", js)

    def test_app_js_找的值槽在模板里都存在(self) -> None:
        """反向守卫：app.js 读的每个 `data-overview-value` / `-help` 都要有元素，
        否则那行 setText 是死代码（issue #4 里的 sentTotal / receiptOnly 就是这样）。"""
        js = _read("webui/static/app.js")
        templates = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "webui" / "templates").glob("*.html"))
        )
        for attr in ("data-overview-value", "data-overview-help"):
            names = sorted(set(re.findall(rf"{attr}='([A-Za-z]+)'", js)))
            self.assertTrue(names, f"找不出任何 {attr} 选择器")
            for name in names:
                with self.subTest(attr=attr, name=name):
                    self.assertIn(f'{attr}="{name}"', templates)

    def test_两个死选择器没有留下(self) -> None:
        # 模板刻意不渲染这两张卡：「本轮已发送」上面那条进度条里已经有，
        # 「仅 HTTP 回执」在 C 里恒为 0（都不该靠轮询去写一个不存在的元素）。
        js = _read("webui/static/app.js")
        self.assertNotIn("data-overview-value='sentTotal'", js)
        self.assertNotIn("data-overview-value='receiptOnly'", js)


class NextTriggerHintOrderTests(unittest.TestCase):
    """issue #2：`nextTriggerHint` 的判断顺序，轮询必须与服务端首屏逐字同序。

    顺序错了的后果：定时器真挂了的时候，首屏给"哪里坏了"、10 秒后变成一句废话。
    """

    def test_app_js_先看_timerStallText_再看_armed(self) -> None:
        js = _read("webui/static/app.js")
        start = js.index("[data-overview-value='nextTriggerHint']")
        body = js[start : js.index("\n    );", start)]
        self.assertIn("schedule.timerStallText", body)
        self.assertLess(
            body.index("schedule.timerStallText"),
            body.index("!armed"),
            "定时器挂了的诊断必须排在「定时器没启用」前面",
        )

    def test_模板同一顺序(self) -> None:
        tpl = _read("webui/templates/dashboard.html")
        start = tpl.index('data-overview-value="nextTriggerHint"')
        body = tpl[start : tpl.index("</div>", start)]
        self.assertIn("view.schedule.timerStallText", body)
        self.assertLess(
            body.index("view.schedule.timerStallText"),
            body.index("not view.schedule.armed"),
        )


class MixedRunsScopeTests(unittest.TestCase):
    """issue #2：「各账号各自最近一次」/「上一轮」的口径两端同一判据。"""

    def test_app_js_按_summary_mixedRuns_选口径(self) -> None:
        js = _flat(_read("webui/static/app.js"))
        self.assertIn('summary.mixedRuns ? "各账号各自最近一次合计，" : "上一轮"', js)
        self.assertIn('summary.mixedRuns ? "最近这些轮次里" : "上一轮"', js)

    def test_模板同一判据(self) -> None:
        tpl = _read("webui/templates/dashboard.html")
        self.assertIn("'各账号各自最近一次合计，' if summary.mixed_runs else '上一轮'", tpl)

    def test_说明块是常驻_按信号切_hidden(self) -> None:
        """只有轮询能出现/消失的元素才配常驻：服务端必须把开关一起发出来。"""
        tpl = _flat(_read("webui/templates/dashboard.html"))
        self.assertIn(
            'data-overview-mixed-runs {% if not summary.mixed_runs %}hidden{% endif %}', tpl
        )

    def test_本轮没有它的提示由母版克隆_既能补上也能撤掉(self) -> None:
        """这一块是全站唯一一处"必须真的不在页面上"的挂点，所以只有它能被轮询增删。

        2026-10-17：原本也想跟别的挂点一样「常驻 + hidden」，但 tests/test_webui.py
        按账号切片断言过"跑过的那一轮里不该有它"（那个文件不许改），页面上真的不能有；
        于是改成从模板里的母版克隆 / 撤掉 —— 文案仍然只有一份，不会两边各写各的。
        """
        tpl = _flat(_read("webui/templates/dashboard.html"))
        self.assertIn("<template data-account-run-missing-tpl>", tpl)
        self.assertIn("{% macro run_missing_block(account) %}", tpl)
        # 首屏那一份与母版必须是同一段宏，改一处就够
        self.assertIn("{% if account.run_missing %}{{ run_missing_block(account) }}{% endif %}", tpl)
        self.assertIn("{{ run_missing_block({}) }}", tpl)
        js = _flat(_read("webui/static/app.js"))
        self.assertIn('document.querySelector("template[data-account-run-missing-tpl]")', js)
        self.assertIn("tpl.content.firstElementChild.cloneNode(true)", js)
        self.assertIn("missing.remove();", js)
        self.assertIn("renderIcons();", js, "克隆来的 <i data-lucide> 不点亮就是个空图标")

    def test_app_js_把轮数也重画了(self) -> None:
        js = _flat(_read("webui/static/app.js"))
        self.assertIn("count.textContent = summary.runCount || 0;", js)
        self.assertIn("node.hidden = !summary.mixedRuns;", js)


class AccountStateChipVocabularyTests(unittest.TestCase):
    """账号卡配色的词表：服务端 STATE_CHIP 写进去，app.js 必须认得全。"""

    def test_app_js_的词表与服务端一致(self) -> None:
        match = re.search(r"const CHIP_NAMES = \[([^\]]*)\]", _read("webui/static/app.js"))
        self.assertIsNotNone(match, "app.js 里的 CHIP_NAMES 词表不见了")
        client = set(re.findall(r'"([a-z]+)"', match.group(1)))
        vm = _read("webui/viewmodel.py")
        block = vm[vm.index("STATE_CHIP: Dict[str, str] = {") :]
        server = set(re.findall(r':\s*"([a-z]+)"', block[: block.index("}")]))
        self.assertEqual(client, server, "服务端会给的 chip 类名，app.js 一个都不能漏认")


class StrongConfirmWordingTests(unittest.TestCase):
    """issue #7：同一件事（回执 ok 且能点名收件人）只留一套说法。

    旧说法听起来比真实判据更严（收件人校验不过只降级成弱证据，ok 仍为 True），
    而这句话是用户判断"这条到底算不算发出去"的唯一依据。
    """

    CANON = "服务器确认已发出，并且确认发给了本人"

    def test_三处说的是同一句话(self) -> None:
        for rel in (
            "webui/templates/dashboard.html",
            "webui/templates/runs.html",
            "webui/static/app.js",
        ):
            with self.subTest(rel=rel):
                self.assertIn(self.CANON, _read(rel))

    def test_旧的更严的说法清干净了(self) -> None:
        for rel, src in _webui_sources():
            for phrase in ("会话选择", "会话与回执双确认", "双确认的目标"):
                with self.subTest(rel=rel, phrase=phrase):
                    self.assertNotIn(phrase, src, f"{rel} 里还留着旧说法「{phrase}」")


class LoginShotHideTests(unittest.TestCase):
    """issue #6：会话结束后，旧二维码截图必须跟着消失（不能只写成单向）。"""

    def setUp(self) -> None:
        self.src = _read("webui/static/app.js")

    def test_shot_是双向的并且清掉旧图(self) -> None:
        self.assertIn("shot.hidden = !session.has_shot;", self.src)
        self.assertIn('shot.removeAttribute("src");', self.src)

    def test_没有残留的单向分支(self) -> None:
        self.assertNotIn("if (shot && session.has_shot) {", self.src)

    def test_同一个_apply_里两个元素一个规矩(self) -> None:
        self.assertIn("codeWrap.hidden = !session.need_code;", self.src)


if __name__ == "__main__":
    unittest.main()
class NextTriggerHintLastBranchTests(unittest.TestCase):
    """issue #2 的最后一处首屏/轮询分歧：末支两边必须同源（都用 timerText）。

    末支＝"定时器启用着、又不是派发器"的经典模式。首屏写 `timerText`（"定时器已
    启用"），而 JS 以前自己编了一句"仍有 N 个待发送目标" —— 那个数字在
    `data-overview-value="pending"` 那格里已经有一份，10 秒后却把这句话换掉，正是
    issue 里说的"互相打脸"。
    """

    def test_app_js_末支与模板同源(self) -> None:
        js = _read("webui/static/app.js")
        start = js.index("[data-overview-value='nextTriggerHint']")
        body = js[start : js.index("\n    );", start)]
        self.assertIn("schedule.timerText", body, "末支必须用服务端给的 timerText")
        self.assertNotIn(
            "仍有 ${remainingForNext}", body, "末支不许再自己编一句和首屏不一样的话"
        )

    def test_模板末支也是_timerText(self) -> None:
        tpl = _read("webui/templates/dashboard.html")
        start = tpl.index('data-overview-value="nextTriggerHint"')
        body = tpl[start : tpl.index("</div>", start)]
        self.assertIn("view.schedule.timerText", body)
