"""面板静态资源与模板的**接线守卫**（不需要 fastapi，本机可跑）。

这一批断言守的是"模板挂了钩子、JS 却在找另一个名字"这类**静默失效**：页面能打开、
不报错、看起来一切正常，功能就是不动。2026-10-17 的前端审计一次性抓到三处
（分段控件整块死代码、被阻断账号的红边 10 秒后消失、日志页把"登录过期"说成
"刷新成功"），所以钉在源码文本上 —— 这类错误只有真在浏览器里点一遍才会暴露，
而我们的测试跑在无头环境里。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()
