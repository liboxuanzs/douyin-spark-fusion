"""webui 仪表盘的测试。

覆盖：导入安全（不碰 playwright / cloakbrowser）、启动前校验（绑非回环必须配口令）、
口令与签名 cookie、账本容错、限流冷却展示、运行锁与手动触发、日志尾部读取与转义、
以及"零外链 / 无 innerHTML"的静态扫描。

约定：所有外部路径都指向临时目录，绝不读写仓库里的真实 logs/，也不会真的启动服务器。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock
from urllib.parse import unquote

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import _purity  # noqa: E402  跨进程的导入纯净性探针（为什么必须跨进程见该模块）
from webui import app as webui_app  # noqa: E402
from webui import auth, config, envfile, logtail, password, settings, store, viewmodel  # noqa: E402

PASSWORD = "测试口令-abc123"
UID = "12345678905"
ACCOUNT_NAME = "验收账号甲"
OTHER_ACCOUNT = "验收账号乙"
OTHER_UID = "98765432109"
FRIEND_MAIN = "验收好友甲"
FRIEND_MISSING = "不存在的验收好友"

# 会被每个用例清空的环境变量（清空后再按需设置，保证用例之间互不干扰）
ENV_KEYS = (
    "WEBUI_HOST",
    "WEBUI_PORT",
    "WEBUI_PASSWORD",
    "WEBUI_PASSWORD_HASH",
    "WEBUI_SESSION_SECRET",
    "WEBUI_SESSION_MAX_AGE",
    "WEBUI_AUTH",
    "WEBUI_LOG_FILE",
    "OPS_LOG_FILE",
    "RUN_LEDGER_FILE",
    "RATE_LIMIT_FILE",
    "RUN_LOCK_FILE",
    "TASKS",
    "TASK_SCHEDULE_TIMES",
    "WEBUI_TASK_SCHEDULE",
    "DRY_RUN",
)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class WebUITestCase(unittest.TestCase):
    """公共脚手架：临时目录 + 干净的环境变量 + 可注入的 utils.config 状态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dsf-webui-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore_env)

        self.ledger = self.tmp / "runs.jsonl"
        self.rate_file = self.tmp / "rate_limit.json"
        self.lock_file = self.tmp / "run.lock"
        self.log_file = self.tmp / "app.log"

        os.environ["RUN_LEDGER_FILE"] = str(self.ledger)
        os.environ["RATE_LIMIT_FILE"] = str(self.rate_file)
        os.environ["RUN_LOCK_FILE"] = str(self.lock_file)
        os.environ["OPS_LOG_FILE"] = str(self.log_file)
        os.environ["WEBUI_LOG_FILE"] = str(self.log_file)
        os.environ["TASK_SCHEDULE_TIMES"] = "09:00,21:30"

        # utils.config 是模块级快照，这里直接注入，避免依赖真实 .env / COOKIES_* 解码
        self.utils_config = self._load_utils_config()
        if self.utils_config is not None:
            self._patch_utils_config()
            self.addCleanup(self._restore_utils_config)

        # 免得真的往仓库 logs/ 里写一行
        self.addCleanup(self._noop_patch, webui_app, "_log")

        # 口令失败记账是 webui.app 的**模块级全局状态**，而且是有意设计：60 秒窗口内
        # 累计 30 次失败就对**所有来源**返回 429（防的是"换 IP 撞口令把单进程的 PBKDF2
        # 打满"，窗口一滑过去自动恢复，见 webui/app.py 的 _note_login_fail）。
        # 产品代码不该动 —— 错的是测试套件：同一个进程里一个接一个跑十几个类，每个类
        # 都继承着那条 `test_错误口令不通过`，累到第 14 个类（MaintenanceWebUITestCase）
        # 时窗口已经满了，于是连"正确口令"都被判 429（issue #3 里那 31 条）。
        # 所以由夹具按"一个类一份干净起点"归零，口径与 LoginFailBudgetTests 完全一致。
        self._reset_login_fail_budget()
        self.addCleanup(self._reset_login_fail_budget)

    # ------------------------------------------------------------ 脚手架

    @staticmethod
    def _noop_patch(target: Any, name: str) -> None:
        original = getattr(target, name)

        def restore() -> None:
            setattr(target, name, original)

        setattr(target, name, lambda *args, **kwargs: None)
        restore()

    @staticmethod
    def _reset_login_fail_budget() -> None:
        """归零口令失败记账：按来源的罚站表 + 那扇 60 秒的全局窗口。

        用与产品代码**同一把锁**（不是裸操作字典）：用例里虽然是单线程，
        但口径一致以后不容易被后人改坏；两张表都 `.clear()` 而不是重新赋值，
        免得把 `webui_app` 里那份模块级对象换掉。
        """
        with webui_app._LOGIN_FAILS_LOCK:
            webui_app._LOGIN_FAILS.clear()
            webui_app._LOGIN_FAIL_TIMES.clear()

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _load_utils_config():
        try:
            from utils import config as utils_config  # noqa: PLC0415

            return utils_config
        except Exception:
            return None

    def _patch_utils_config(self) -> None:
        module = self.utils_config
        self._original_config = getattr(module, "config", None)
        self._original_userdata = getattr(module, "userData", None)
        module.config = {
            "proxyAddress": "http://user:secret-pass@127.0.0.1:1080",
            "messageTemplate": "[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]",
            "hitokotoTypes": ["文学", "影视"],
            "browserActionTimeout": 120000,
            "imScanTimeout": 120,
            "imReadyTimeout": 120,
            "friendListSettleMs": 3000,
            "imMaxSteps": 200,
            "taskRetryTimes": 3,
            "logLevel": "Debug",
        }
        module.userData = [
            {
                "unique_id": UID,
                "username": ACCOUNT_NAME,
                "fingerprint": "fp-1",
                "cookies": [{"name": "sessionid", "value": "SUPER-SECRET-COOKIE-VALUE"}],
                "targets": [FRIEND_MAIN],
            }
        ]

    def _restore_utils_config(self) -> None:
        setattr(self.utils_config, "config", self._original_config)
        setattr(self.utils_config, "userData", self._original_userdata)

    # ------------------------------------------------------------ 建数据

    def make_client(self) -> TestClient:
        return TestClient(webui_app.create_app())

    def login(self, client: TestClient) -> None:
        response = client.post(
            "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
        )
        self.assertIn(response.status_code, (302, 303), "正确口令应当登录成功")

    def write_ledger(self, rows: List[Any]) -> None:
        lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def sample_account(
        entries: Optional[List[Dict[str, Any]]] = None,
        unique_id: str = UID,
        username: str = ACCOUNT_NAME,
        sent_ok: int = 2,
        sent_fail: int = 1,
    ) -> Dict[str, Any]:
        return {
            "username": username,
            "unique_id": unique_id,
            "status": "ok",
            "gate": "READY",
            "sent_ok": sent_ok,
            "sent_fail": sent_fail,
            "scanned_all": True,
            "steps": 12,
            "visited": 30,
            "missing": [FRIEND_MISSING],
            "select_failed": [],
            "targets": [FRIEND_MAIN],
            "entries": entries
            if entries is not None
            else [
                {
                    "target": FRIEND_MAIN,
                    "ok": True,
                    "level": "strong",
                    "reason": "",
                    "via": "http",
                    "message_id": "mid-1",
                    "dry_run": "off",
                    "rate_limited": False,
                    "at": _iso(datetime.now(timezone.utc)),
                }
            ],
        }

    def sample_run(
        self,
        run_id: str = "20261001-090000-ab12",
        dry_run: str = "off",
        limit: Any = None,
        skipped: Any = None,
        rate_limited: bool = False,
        accounts: Optional[List[Dict[str, Any]]] = None,
        started: Optional[datetime] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        start = started or datetime.now(timezone.utc) - timedelta(minutes=12)
        payload = {
            "run_id": run_id,
            "started_at": _iso(start),
            "finished_at": _iso(start + timedelta(seconds=750)),
            "dry_run": dry_run,
            "rate_limited": rate_limited,
            "limit": limit,
            "skipped": skipped,
            "accounts": accounts if accounts is not None else [self.sample_account()],
        }
        if source is not None:
            payload["source"] = source
        return payload

    def write_rate_limit(self, until: datetime, keyword: str = "操作频繁") -> None:
        payload = {
            "tripped_at": _iso(datetime.now(timezone.utc)),
            "cooldown_until": _iso(until),
            "keyword": keyword,
            "source": "dom",
            "target": FRIEND_MAIN,
            "run_id": "20261001-090000-ab12",
            "note": "验收用例写入",
        }
        self.rate_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def write_lock(self, pid: Optional[int] = None, started_at: Optional[datetime] = None) -> None:
        payload = {
            "pid": os.getpid() if pid is None else pid,
            "started_at": _iso(started_at or datetime.now(timezone.utc)),
            "dry_run": "off",
            "source": "webui",
            "run_id": "20261001-090000-ab12",
        }
        self.lock_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ============================================================ 1. 导入安全

    def test_导入_webui_app_不引入浏览器依赖(self) -> None:
        """webui 是薄层：任何模块都不得 import playwright / cloakbrowser。

        回归（issue #3 机制一）：这两条断言原来直接查本进程的 `sys.modules`，
        而它是**进程级**状态 —— 本文件里有一批"真把面板跑一遍"的用例会把浏览器依赖
        留在里面（下面 `test_缺少浏览器依赖时返回_503_中文说明` 那句真的
        `import cloakbrowser` 就会）。于是断言测到的是"这条用例排在第几个"：
        pytest 对 unittest.TestCase 按方法名字典序跑，ASCII 名字排在中文名字之前，
        `test_导入_…` 又排在把 cloakbrowser 拉进来那句 `test_缺少…` 之前，
        所以同一个文件里"单跑 1 passed、整文件跑 60 failed"。
        现在把断言搬进全新子进程（tests/_purity.py）：那里 `sys.modules` 只可能有
        `import webui.app` 自己造成的变化 —— 断言重新能真失败、也真能通过。
        """
        self.assertIn("webui.app", sys.modules, "import 阶段就应当已经把 webui.app 装进 sys.modules")
        _purity.assert_imports_clean(
            note="import webui.app 不应触发 cloakbrowser / playwright / core.tasks（它依赖 core.browser）"
        )

    def test_webui_源码里没有浏览器依赖的导入(self) -> None:
        # 测试自身的问题（2026-10 修正）：这里原来用 `"playwright" in source` 这种**整文件子串**
        # 判定，于是 webui/__init__.py、app.py、config.py 里"我们不 import playwright /
        # cloakbrowser"这类**注释与文档字符串**、以及 503 的中文提示文案，全都被算成了违规导入
        # （原来的失败列表正是 ['__init__.py', 'app.py', 'config.py'] —— 而这三个文件恰恰是
        # 最强调"不许引入浏览器依赖"的地方）。这种启发式既会误报、又拦不住真正的写法
        # （`importlib.import_module("cloakbrowser")` 里根本没有 import 关键字）。
        # 改成 ast 解析：只看真正的 import 语句，以及动态导入里的**字符串字面量**参数；
        # 注释/说明文字不再算违规，实现侧也就不必为了迎合子串扫描而删掉这些说明。
        import ast

        browser_roots = {"playwright", "cloakbrowser"}
        offenders = []
        for path in sorted((REPO_ROOT / "webui").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: tuple = ()
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names = (node.module or "",)
                elif isinstance(node, ast.Call):
                    func = node.func
                    dynamic = (
                        isinstance(func, ast.Attribute)
                        and func.attr in {"import_module", "__import__"}
                    ) or (isinstance(func, ast.Name) and func.id == "__import__")
                    if dynamic and node.args and isinstance(node.args[0], ast.Constant):
                        literal = node.args[0].value
                        names = (literal if isinstance(literal, str) else "",)
                for name in names:
                    root = str(name).split(".")[0]
                    if root in browser_roots:
                        offenders.append(f"{path.name}:{root}")
        self.assertEqual(
            sorted(set(offenders)),
            [],
            f"这些 webui 模块真的导入了浏览器依赖（注释里提到不算数）：{offenders}",
        )

    def test_webui_源码里没有_innerHTML(self) -> None:
        offenders = []
        targets = [
            path
            for path in list((REPO_ROOT / "webui").rglob("*.js"))
            + list((REPO_ROOT / "webui").rglob("*.html"))
            # lucide.min.js 是 B 原样带过来的第三方图标库（自带 LICENSE），
            # 不是我们写的代码，不在这条规矩的管辖范围内。
            if path.name != "lucide.min.js"
        ]
        for path in sorted(targets):
            source = path.read_text(encoding="utf-8")
            for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
                if banned in source:
                    offenders.append(f"{path.name}:{banned}")
        self.assertEqual(offenders, [], f"前端不许出现这些写法：{offenders}")

    def test_模板与静态文件没有外网引用(self) -> None:
        offenders = []
        targets = [
            path
            for path in list((REPO_ROOT / "webui").rglob("*.html"))
            + list((REPO_ROOT / "webui").rglob("*.css"))
            + list((REPO_ROOT / "webui").rglob("*.js"))
            # 同上：vendored 的图标库里必然有 SVG 命名空间等 http 字符串。
            if path.name != "lucide.min.js"
        ]
        for path in sorted(targets):
            source = path.read_text(encoding="utf-8")
            if "http://" in source or "https://" in source:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"零外链要求被打破：{offenders}")

    def test_页面只引用本站静态资源(self) -> None:
        """模板里的 src/href 只允许指向 /static/ 或本站路由 —— 内网部署不拉外网。

        vendored 的图标库内容不算（它自己内部有命名空间字符串），这里看的是
        **标签属性**，也就是浏览器真的会去请求什么。
        """
        import re

        pattern = re.compile(r'(?:src|href)\s*=\s*"([^"]+)"')
        offenders = []
        for path in sorted((REPO_ROOT / "webui" / "templates").rglob("*.html")):
            for url in pattern.findall(path.read_text(encoding="utf-8")):
                if url.startswith(("//", "http://", "https://")):
                    offenders.append(f"{path.name}:{url}")
        self.assertEqual(offenders, [], f"这些引用会发外网请求：{offenders}")

    def test_每个表单动作都指向真实路由(self) -> None:
        """页面上**不许出现按了没反应的按钮**。

        用户对这个面板的硬要求是"不得有无意义的前端按钮"（m04018）。模板里凡是有
        ``action=`` 的表单、或指向站内的 ``href=``，都必须能在 FastAPI 路由表里找到
        对应项；找不到的（拼错的路径、被删掉的老路由残留）一律当场失败。
        """
        import re

        attr = re.compile(r'(?:action|href)\s*=\s*"([^"]+)"')
        routes = set()
        for route in webui_app.create_app().routes:
            path = getattr(route, "path", "")
            if path:
                routes.add(path)
        offenders = []
        for path in sorted((REPO_ROOT / "webui" / "templates").rglob("*.html")):
            for url in attr.findall(path.read_text(encoding="utf-8")):
                if not url.startswith("/"):
                    continue  # 纯锚点 "#xxx"、外链已在另一条用例里管
                target = url.split("?", 1)[0].split("#", 1)[0]
                if target in ("", "/"):
                    target = "/"
                if target in routes:
                    continue
                # /static 是挂载点，挂载点下的具体文件路径不在路由表里。
                if target.startswith("/static/") and any(
                    item == "/static" or item.startswith("/static/") for item in routes
                ):
                    continue
                # /ops/runs#account-xxx 这种：锚点前面那段就是路由
                if target != "/" and target.rstrip("/") in {item.rstrip("/") for item in routes}:
                    continue
                offenders.append(f"{path.name}:{url}")
        self.assertEqual(offenders, [], f"这些链接/表单没有对应的路由：{offenders}")

    # ============================================================ 2. 启动前校验

    def test_绑非回环且无口令时拒绝启动(self) -> None:
        os.environ["WEBUI_HOST"] = "0.0.0.0"
        with self.assertRaises(config.ConfigError) as ctx:
            config.preflight()
        self.assertIn("口令", str(ctx.exception))

    def test_cli_绑非回环且无口令时以退出码_2_拒绝启动(self) -> None:
        os.environ["WEBUI_HOST"] = "0.0.0.0"
        from webui import __main__ as webui_main

        code = webui_main.run(["--check", "--no-env-file"])
        self.assertEqual(code, 2)
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.assertEqual(webui_main.run(["--check", "--no-env-file"]), 0)

    def test_回环无口令允许启动但页面明确警告(self) -> None:
        os.environ["WEBUI_HOST"] = "127.0.0.1"
        notes = config.preflight()
        self.assertTrue(any("未启用认证" in note for note in notes), f"应当带出未启用认证的警告：{notes}")

        with self.make_client() as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("未启用认证", response.text)

    def test_auth_off_只允许回环(self) -> None:
        os.environ["WEBUI_AUTH"] = "off"
        os.environ["WEBUI_HOST"] = "0.0.0.0"
        with self.assertRaises(config.ConfigError):
            config.preflight()
        os.environ["WEBUI_HOST"] = "127.0.0.1"
        config.preflight()  # 回环下允许

    # ============================================================ 3. 认证

    def test_未登录访问首页会被重定向到登录页(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            response = client.get("/", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertIn("/login", response.headers.get("location", ""))

    def test_错误口令不通过(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            response = client.post("/login", data={"password": "错的口令", "next": "/"})
            self.assertEqual(response.status_code, 401)
            self.assertIn("口令不正确", response.text)
            self.assertNotIn(config.session_cookie_name(), response.cookies)

    def test_正确口令拿到签名_cookie_且首页包含账号名(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            response = client.post(
                "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
            )
            self.assertIn(response.status_code, (302, 303))
            token = response.cookies.get(config.session_cookie_name())
            self.assertIsNotNone(token, "登录成功必须下发会话 cookie")
            self.assertTrue(token.count(".") == 2, "itsdangerous 的签名 cookie 应当是三段式")

            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(ACCOUNT_NAME, page.text, "总览页要有账号名")
            # 目标级明细（谁、发没发出去）在运行记录页，总览页只放计数。
            runs = client.get("/ops/runs")
            self.assertEqual(runs.status_code, 200)
            self.assertIn(FRIEND_MAIN, runs.text)

    def test_会话_cookie_是签名且可校验(self) -> None:
        token = auth.issue_token("admin")
        self.assertEqual(auth.read_token(token), "admin")
        self.assertIsNone(auth.read_token(token + "x"), "被改动的签名必须无效")

    def test_口令哈希优先且能通过登录(self) -> None:
        os.environ["WEBUI_PASSWORD_HASH"] = password.hash_password(
            PASSWORD, iterations=1000, salt=b"0123456789abcdef"
        )
        os.environ["WEBUI_PASSWORD"] = "这是明文口令但哈希优先"
        with self.make_client() as client:
            wrong = client.post("/login", data={"password": "这是明文口令但哈希优先", "next": "/"})
            self.assertEqual(wrong.status_code, 401)
            right = client.post("/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False)
            self.assertIn(right.status_code, (302, 303))

    def test_登出会清掉会话(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            self.assertEqual(client.get("/").status_code, 200)
            client.post("/logout", follow_redirects=False)
            self.assertEqual(client.get("/", follow_redirects=False).status_code, 303)

    def test_页面不回显_cookie_值(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        os.environ[f"COOKIES_{UID}"] = json.dumps(
            [{"name": "sessionid", "value": "SUPER-SECRET-COOKIE-VALUE"}], ensure_ascii=False
        )
        with self.make_client() as client:
            self.login(client)
            page = client.get("/ops/runs")
            overview = client.get("/api/overview")
        self.assertNotIn("SUPER-SECRET-COOKIE-VALUE", page.text)
        self.assertNotIn("SUPER-SECRET-COOKIE-VALUE", overview.text)
        self.assertIn("已配置 1 项 cookie", page.text, "应当只回报配置了几项")

    # ============================================================ 4. 账本容错

    def test_账本不存在时返回空态而不是_500(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.assertFalse(self.ledger.exists())
        with self.make_client() as client:
            self.login(client)
            overview = client.get("/api/overview")
            self.assertEqual(overview.status_code, 200)
            payload = overview.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["runs"], [])
            self.assertIsNone(payload["last_run"])
            page = client.get("/")
            runs_page = client.get("/ops/runs")
        self.assertEqual(page.status_code, 200)
        # 账本为空时总览页照常渲染（账号来自配置，计数全 0），不能 500。
        self.assertIn(ACCOUNT_NAME, page.text)
        self.assertEqual(runs_page.status_code, 200)
        self.assertIn("账本还是空的", runs_page.text)

    def test_坏_json_行被跳过仍返回_200(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ledger(["{ 这不是合法 JSON", self.sample_run(), "]", ""])
        with self.make_client() as client:
            self.login(client)
            overview = client.get("/api/overview")
            self.assertEqual(overview.status_code, 200)
            payload = overview.json()
            self.assertEqual(len(payload["runs"]), 1)
            self.assertEqual(payload["runs"][0]["run_id"], "20261001-090000-ab12")
            self.assertGreaterEqual(payload["ledger_bad_lines"], 2)
            page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(ACCOUNT_NAME, page.text)

    def test_账本最新一轮倒序在前且明细可展开(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        older = self.sample_run(run_id="20261001-080000-aaaa")
        newer = self.sample_run(run_id="20261001-100000-bbbb")
        self.write_ledger([older, newer])
        with self.make_client() as client:
            self.login(client)
            payload = client.get("/api/overview").json()
            self.assertEqual([item["run_id"] for item in payload["runs"]], ["20261001-100000-bbbb", "20261001-080000-aaaa"])
            page = client.get("/ops/runs")
        self.assertIn("mid-1", page.text, "逐条明细（message_id）应当出现在运行记录页上")

    def test_账号门禁与计数来自最近一轮(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ledger(
            [
                self.sample_run(
                    accounts=[self.sample_account(sent_ok=5, sent_fail=0)],
                )
            ]
        )
        with self.make_client() as client:
            self.login(client)
            accounts = client.get("/api/overview").json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["username"], ACCOUNT_NAME)
        self.assertEqual(accounts[0]["unique_id"], UID)
        self.assertEqual(accounts[0]["gate"], "READY")
        self.assertEqual(accounts[0]["sent_ok"], 5)
        self.assertEqual(accounts[0]["sent_fail"], 0)
        self.assertEqual(accounts[0]["missing"], [FRIEND_MISSING])
        self.assertEqual(accounts[0]["targets"], [FRIEND_MAIN])

    # ================================================ 4c. 每个账号自己的那一轮

    def add_second_account(self, *, unique_id: str = OTHER_UID) -> None:
        """再挂一个账号 —— 每个账号有自己的发送时间，一轮里通常只有一两个账号。"""
        self.utils_config.userData.append(
            {
                "unique_id": unique_id,
                "username": OTHER_ACCOUNT,
                "fingerprint": "fp-2",
                "cookies": [],
                "targets": [FRIEND_MAIN],
            }
        )

    def two_account_ledger(self) -> Tuple[str, str]:
        """账本里两轮：先跑 UID（甲），一小时后的那一轮只跑 OTHER_UID（乙）。

        返回 (`甲那一轮的 started_text`, `乙那一轮的 started_text`) —— 面板上
        "它最近一次是 …" 应该写的就是这两个串。
        """
        first_start = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
        second_start = first_start + timedelta(hours=1)
        older = self.sample_run(
            run_id="20261001-120000-aaaa", started=first_start, accounts=[self.sample_account()]
        )
        newer = self.sample_run(
            run_id="20261001-130000-bbbb",
            started=second_start,
            accounts=[self.sample_account(unique_id=OTHER_UID, username=OTHER_ACCOUNT)],
        )
        self.write_ledger([older, newer])
        return store.fmt_stamp(_iso(first_start)), store.fmt_stamp(_iso(second_start))

    @staticmethod
    def account_segments(html: str, attribute: str = 'data-run-account="') -> Dict[str, str]:
        """按账号把页面切开（每张卡从自己的挂点起到下一张卡），便于逐卡断言。"""
        segments: Dict[str, str] = {}
        for part in html.split(attribute)[1:]:
            unique_id, _, body = part.partition('"')
            segments[unique_id] = body
        return segments

    def test_总览按各账号自己的最近一轮算(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.add_second_account()
        self.two_account_ledger()
        with self.make_client() as client:
            self.login(client)
            accounts = {
                item["uniqueId"]: item
                for item in client.get("/api/ops/overview").json()["accounts"]
            }
            page = client.get("/")
        self.assertEqual(page.status_code, 200)
        first = accounts[UID]
        self.assertEqual(first["confirmed"], 1, "甲上一轮确认过了")
        self.assertEqual(first["pending"], 0, "最近一轮没轮到甲，不能因此报「未尝试」")
        self.assertNotEqual(first["state"], "idle", "跑过而且成功过的账号不能说成还没跑过")
        self.assertEqual(accounts[OTHER_UID]["confirmed"], 1)
        self.assertEqual(accounts[OTHER_UID]["pending"], 0)
        self.assertIn("各账号各自最近一次", page.text, "数字来自不止一轮时必须说出来")
        self.assertIn("各账号各自最近一次合计", page.text, "「最近强确认」那格不能再说「上一轮」")
        self.assertIn(
            "各账号自己最近一轮里还没轮到的目标",
            page.text,
            "「未尝试」那格的解释也得跟着换口径",
        )
        # 总览页每张卡都取自己那一轮 —— 谁也不该被"最近一轮没轮到它"波及。
        dashboard = self.account_segments(page.text, 'data-account-overview="')
        self.assertNotIn("data-account-run-missing", dashboard[UID])
        self.assertNotIn("data-account-run-missing", dashboard[OTHER_UID])
        self.assertIn("数字来自", dashboard[UID], "卡片要说清数字来自哪一轮")

    def test_运行记录页说清这一轮没轮到谁(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.add_second_account()
        first_time, second_time = self.two_account_ledger()
        with self.make_client() as client:
            self.login(client)
            page = client.get("/ops/runs")
            frozen = client.get("/ops/runs?run=20261001-120000-aaaa")
        # 这一页冻在"选中的那一轮"（默认 = 最新一轮，只跑了乙）：甲那一行要解释清楚。
        self.assertEqual(page.status_code, 200)
        segments = self.account_segments(page.text)
        self.assertEqual(len(segments), 2, "两个账号都该有一行")
        self.assertIn("data-account-run-missing", segments[UID], "最新一轮里没有甲，要说清")
        self.assertIn(first_time, segments[UID], "还要说清它最近一次是哪一轮")
        self.assertNotIn("data-account-run-missing", segments[OTHER_UID])
        # 反过来选中甲那一轮：这次轮到乙解释。
        self.assertEqual(frozen.status_code, 200)
        frozen_segments = self.account_segments(frozen.text)
        self.assertIn("data-account-run-missing", frozen_segments[OTHER_UID])
        self.assertIn(second_time, frozen_segments[OTHER_UID], "它最近一次是乙刚跑的那轮")
        self.assertNotIn("data-account-run-missing", frozen_segments[UID])

    # ============================================================ 4b. 触发来源

    def test_触发来源会显示成中文标签(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ledger(
            [
                self.sample_run(run_id="20261001-090000-cli1", source="cli"),
                self.sample_run(run_id="20261001-100000-web1", source="webui"),
                self.sample_run(run_id="20261001-110000-tmr1", source="timer"),
                self.sample_run(run_id="20261001-120000-non1"),  # 老账本没有 source 键
            ]
        )
        with self.make_client() as client:
            self.login(client)
            payload = client.get("/api/overview").json()
            page = client.get("/ops/runs")
        by_id = {item["run_id"]: item for item in payload["runs"]}
        self.assertEqual(by_id["20261001-100000-web1"]["source_text"], "面板触发")
        self.assertEqual(by_id["20261001-090000-cli1"]["source_text"], "命令行")
        self.assertEqual(by_id["20261001-110000-tmr1"]["source_text"], "定时任务")
        self.assertEqual(by_id["20261001-120000-non1"]["source"], "")
        self.assertEqual(by_id["20261001-120000-non1"]["source_text"], "—", "老账本缺 source 不能崩，回退占位符")
        self.assertIn("面板触发", page.text)

    def test_未知触发来源原样显示(self) -> None:
        from webui import store as store_mod

        self.assertEqual(store_mod.source_text("cron"), "cron")
        self.assertEqual(store_mod.source_text(""), "—")
        self.assertEqual(store_mod.source_text(None), "—")
        # 人手动跑一轮验收时最顺手的写法，页面上不该显示成英文
        self.assertEqual(store_mod.source_text("manual"), "手动运行")
        self.assertEqual(store_mod.source_text(" MANUAL "), "手动运行")

    # ============================================================ 5. 限流冷却

    def test_冷却未过期时带出熔断信息(self) -> None:

        os.environ["WEBUI_PASSWORD"] = PASSWORD
        until = datetime.now(timezone.utc) + timedelta(minutes=30)
        self.write_rate_limit(until, keyword="操作频繁")
        with self.make_client() as client:
            self.login(client)
            cooldown = client.get("/api/overview").json()["cooldown"]
            page = client.get("/")
        self.assertTrue(cooldown["tripped"])
        self.assertTrue(cooldown["active"])
        self.assertFalse(cooldown["expired"])
        self.assertEqual(cooldown["keyword"], "操作频繁")
        self.assertIsNotNone(cooldown["seconds_left"])
        self.assertGreater(cooldown["seconds_left"], 0)
        self.assertIn("操作频繁", page.text)
        self.assertIn("冷却", page.text)
        self.assertIn("data-countdown-until", page.text, "倒计时要能继续走")

    def test_冷却已过期时不再显示为熔断(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_rate_limit(datetime.now(timezone.utc) - timedelta(minutes=5))
        with self.make_client() as client:
            self.login(client)
            cooldown = client.get("/api/overview").json()["cooldown"]
        self.assertTrue(cooldown["tripped"])
        self.assertFalse(cooldown["active"], "过了截止时间就不算冷却中")
        self.assertTrue(cooldown["expired"])

    def test_冷却文件不存在时不算熔断(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.assertFalse(self.rate_file.exists())
        with self.make_client() as client:
            self.login(client)
            cooldown = client.get("/api/overview").json()["cooldown"]
        self.assertFalse(cooldown["exists"])
        self.assertFalse(cooldown["tripped"])
        self.assertFalse(cooldown["active"])

    def test_整轮被冷却跳过时页面有提示(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ledger(
            [
                self.sample_run(
                    skipped={"reason": "cooldown", "until": _iso(datetime.now(timezone.utc) + timedelta(hours=1))},
                    accounts=[],
                )
            ]
        )
        with self.make_client() as client:
            self.login(client)
            payload = client.get("/api/overview").json()
            page = client.get("/")
        self.assertEqual(payload["runs"][0]["status"], "skipped")
        self.assertTrue(payload["runs"][0]["skipped"])
        self.assertIn("整轮跳过", page.text)

    # ============================================================ 6. 锁与手动触发

    def test_锁被当前进程持有时触发返回_409(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_lock()
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "off"})
            self.assertEqual(response.status_code, 409)
            body = response.json()
            self.assertFalse(body["started"])
            self.assertIn("正在运行", body["error"])
            page = client.get("/")
        self.assertIn("正在运行", page.text)

    def test_残留锁不影响触发(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_lock(pid=999_999_991)
        state = store.read_lock()
        self.assertFalse(state.running)

    def test_真跑两小时仍然算在跑(self) -> None:
        """活着的进程不能按时间判残留：多账号 × 多目标跑两小时很正常。

        判残留就会让第二个进程夺锁、两个 Chromium 抢同一个账号（会互踢）。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_lock(started_at=datetime.now(timezone.utc) - timedelta(hours=3))
        state = store.read_lock()
        self.assertTrue(state.running)
        self.assertIn("仍在运行", state.reason)

    def test_活着的_pid_卡太久也算残留(self) -> None:
        """pid 还活着 ≠ 真在跑：pid 会被复用，面板那条线程也可能卡死。

        与 `core.run_ledger.lock_info()` 的 `alive_too_long` 同口径（6 小时）——
        旧代码只看 `pid_alive is True`，这种锁会让「立即运行」永久 409，
        只能有人登机器 `rm` 锁文件。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_lock(started_at=datetime.now(timezone.utc) - timedelta(hours=7))
        state = store.read_lock()
        self.assertFalse(state.running)
        self.assertIn("6 小时", state.reason)
        self.assertTrue(state.exists, "锁文件还在，只是不再拦住触发")

    def test_dry_run_取值非法时返回_400(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "乱填"})
        self.assertEqual(response.status_code, 400)

    def test_空跑档位的规范化(self) -> None:
        """挂点值 `dry-full` 必须被认成 `full`。

        回归（2026-10 审计发现 1）：分段控件的挂点是 `dry-off`/`dry-select`/`dry-full`，
        前端把它原样写进隐藏域，服务端只认 off/select/full → **点「立即运行」永远 400**，
        core.runTasks 从未被调用、账本一行都不写，而旧测试全都直接提交 "off"，所以一直是绿的。
        """
        self.assertEqual(webui_app.normalize_dry_run("dry-full"), "full")
        self.assertEqual(webui_app.normalize_dry_run("dry-off"), "off")
        self.assertEqual(webui_app.normalize_dry_run("DRY-SELECT"), "select")
        self.assertEqual(webui_app.normalize_dry_run(" full "), "full")
        self.assertEqual(webui_app.normalize_dry_run(""), "off")
        self.assertEqual(webui_app.normalize_dry_run(None), "off")
        self.assertEqual(webui_app.normalize_dry_run("乱填"), "乱填")

    def test_带前缀的空跑档位真的能跑起来(self) -> None:
        """点按钮到真跑通：`dry-full` → 202 + runTasks(dry_run="full")。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        fired = threading.Event()
        seen: List[dict] = []

        def fake_run_tasks(dry_run=None, source="cli") -> None:
            seen.append({"dry_run": dry_run, "source": source})
            fired.set()

        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = fake_run_tasks
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)

        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "dry-full"})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(response.json()["started"])
            self.assertTrue(fired.wait(5), "带前缀的档位也必须真的跑起来")

        self.assertEqual(seen, [{"dry_run": "full", "source": "webui"}])
        page = None
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertNotIn("不是一个能识别的档位", page.text)

    def test_触发会起后台线程且不导入_cloakbrowser(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        fired = threading.Event()
        calls: List[str] = []
        seen_kwargs: List[dict] = []

        def fake_run_tasks(dry_run=None, source="cli") -> None:
            calls.append("runTasks")
            seen_kwargs.append({"dry_run": dry_run, "source": source})
            # 回归（2026-10 融合缺陷）：webui 曾经自己先写一把运行锁，
            # 于是 core.runTasks 用 O_EXCL 建锁时发现"已有他人持锁"，
            # 整轮直接 skipped —— 页面上的"立即运行"永远空跑。
            # 现在锁必须由 core 独占：调用点这里不能有任何锁文件。
            assert not self.lock_file.exists(), "调用 runTasks 时不该已经有锁（锁归 core 建）"
            fired.set()

        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = fake_run_tasks
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)

        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "select"})
            self.assertEqual(response.status_code, 202)
            body = response.json()
            self.assertTrue(body["started"])
            self.assertTrue(body["run_id"])
            self.assertTrue(fired.wait(5), "后台线程应当真的调用 runTasks")
            self.assertFalse(self.lock_file.exists(), "webui 不该自己写锁")

        self.assertEqual(calls, ["runTasks"])
        self.assertEqual(seen_kwargs, [{"dry_run": "select", "source": "webui"}],
                         "档位与来源要显式传给 runTasks（账本靠 source 区分触发者）")
        # 这条纯净性同样只能跨进程看（本进程里别的用例早就把 cloakbrowser 拉进来了）。
        # 子进程里注入同一个 stub core.tasks，再调一次 `load_run_tasks()` ——
        # 那正是"立即运行"这条路径唯一的惰性导入点，见 tests/_purity.py。
        _purity.assert_imports_clean(
            trigger=True,
            targets=_purity.TRIGGER_STACK,
            note="触发路径应当只拿注入的 core.tasks，不该把浏览器依赖拉进来",
        )
        self.assertIsNone(os.environ.get("DRY_RUN"), "跑完要把 DRY_RUN 恢复原状")

    # ---------------------------------------------- 6b. 禁跑时间窗（F6）与返回值（F10）

    def _patch_quiet_window(self, active: bool, reason: str = "当前处于禁跑时间窗 20:20-21:00，至 21:00"):
        def fake_check(*args, **kwargs):
            return {"active": active, "spec": "20:20-21:00",
                    "until": "2026-10-01T21:00:00" if active else "",
                    "override": False, "reason": reason if active else ""}

        module = importlib.import_module("core.quiet_window")
        patcher = mock.patch.object(module, "check", fake_check)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_禁跑时间窗内触发返回_409(self) -> None:
        """窗口内（默认 off，只有显式设了窗口才会进这里）点"立即运行"必须被拦下并说明原因。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self._patch_quiet_window(True)
        sys.modules.pop("core.tasks", None)
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "off"})
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertFalse(body["started"])
        self.assertIn("禁跑时间窗", body["error"])
        self.assertIn("QUIET_WINDOW_OVERRIDE", body["detail"])
        self.assertEqual(body["quiet_window"]["spec"], "20:20-21:00")
        self.assertFalse(self.lock_file.exists(), "被拦下的一轮不该留下锁")
        self.assertNotIn("core.tasks", sys.modules, "拦下了就不该去导入 core.tasks")

    def test_禁跑时间窗外触发照常(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self._patch_quiet_window(False)
        fired = threading.Event()

        def fake_run_tasks(dry_run=None, source="cli") -> None:
            fired.set()

        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = fake_run_tasks
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "select"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(fired.wait(5))

    def test_被跳过的一轮不会谎报成功(self) -> None:
        """**F10**：`run_once` 从前丢掉 `runTasks` 的返回值，`ok` 只代表"没抛异常"。

        整轮被冷却跳过时面板也会显示成功 —— 而这正是最需要有人知道的一种结果。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = lambda **kw: {
            "run_id": "20261001-090000", "dry_run": "off",
            "skipped": {"reason": "cooldown", "until": "2026-10-01T10:00:00"},
            "rate_limited": False, "accounts": [],
        }
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)
        with self.make_client():
            result = webui_app.run_once("20261001-090000", "off")
        self.assertFalse(result["ok"])
        self.assertIn("cooldown", result["error"])
        self.assertEqual(result["summary"]["skipped"]["reason"], "cooldown")

    def test_全部账号失败时也不谎报成功(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = lambda **kw: {
            "run_id": "x", "dry_run": "off", "skipped": None, "rate_limited": False,
            "accounts": [{"username": "账号甲", "status": "error"},
                         {"username": "账号乙", "status": "error"}],
        }
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)
        with self.make_client():
            result = webui_app.run_once("x", "off")
        self.assertFalse(result["ok"])
        self.assertIn("全部账号", result["error"])

    def test_正常一轮返回_ok_并带上汇总(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = lambda **kw: {
            "run_id": "y", "dry_run": "full", "skipped": None, "rate_limited": False,
            "accounts": [{"username": "账号甲", "status": "ok"}],
        }
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)
        with self.make_client():
            result = webui_app.run_once("y", "full")
        self.assertTrue(result["ok"])
        self.assertNotIn("error", result)
        self.assertEqual(result["summary"]["run_id"], "y")

    def test_缺少浏览器依赖时返回_503_中文说明(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        sys.modules.pop("core.tasks", None)
        # 别的用例（tests/test_tasks_fusion.py）会往 sys.modules 里塞一个假 cloakbrowser；
        # 残留没清干净时下面这行 import 会"成功"，这条测试就被推进 skip —— 整套的
        # passed/skipped 数字会跟着抖。先把它摘掉，让这里只看真实安装情况。
        sys.modules.pop("cloakbrowser", None)
        # 下面那句 `import cloakbrowser` 成功时会把**真的**包留在 sys.modules 里，
        # 而 skipTest 不会替你收拾 —— 本文件里"浏览器依赖不该被拉进来"的断言因此
        # 全都开始看顺序（issue #3 机制一）。纯净性断言现在已经搬进子进程
        # （tests/_purity.py），残留本身仍然是脏的：这里自己清掉再走。
        self.addCleanup(sys.modules.pop, "cloakbrowser", None)
        # 仓库里没装 cloakbrowser，所以真实导入 core.tasks 必然失败
        try:
            import cloakbrowser  # noqa: F401,PLC0415
        except ImportError:
            pass
        else:  # pragma: no cover - 部署机上装了依赖时跳过
            self.skipTest("本机已安装 cloakbrowser，无法验证 503 分支")

        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/run-now", data={"dry_run": "off"})
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["started"])
        self.assertIn("cloakbrowser", body["error"])
        self.assertIn("无法执行任务", body["error"])
        self.assertFalse(self.lock_file.exists(), "503 时不应写下运行锁")

    def test_runTasks_抛异常也不会让进程崩(self) -> None:
        runner = lambda: (_ for _ in ()).throw(RuntimeError("浏览器启动失败"))  # noqa: E731
        fake_module = type(sys)("core.tasks")
        fake_module.runTasks = runner
        sys.modules["core.tasks"] = fake_module
        self.addCleanup(sys.modules.pop, "core.tasks", None)
        result = webui_app.run_once("20261001-090000-test", "off")
        self.assertFalse(result["ok"])
        self.assertIn("RuntimeError", result["error"])

    # ============================================================ 7. 日志

    def test_日志尾部只读取请求的行数(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        lines = [f"第 {index} 行日志" for index in range(1, 41)]
        self.log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/logs?lines=5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("第 40 行日志", response.text)
        self.assertIn("第 36 行日志", response.text)
        self.assertNotIn("第 35 行日志", response.text)

    def test_日志里的_html_特殊字符被转义(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        payload = "<script>alert(1)</script> & <b>粗体</b>"
        self.log_file.write_text(payload + "\n", encoding="utf-8")
        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/logs?lines=10")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", response.text, "日志内容必须是转义后的文本")
        self.assertIn("&lt;script&gt;", response.text)

    def test_超大日志只读尾部且不炸(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        filler = "填充日志 " + "x" * 200 + "\n"
        with self.log_file.open("w", encoding="utf-8") as handle:
            for index in range(6500):
                handle.write(f"{index:05d} {filler}")
        # 测试自身的问题（2026-10 修正）：这里原来断言 `> 5 * 1024 * 1024`（并注释"约 800
        # 字节/行、约 5 MB"），但本用例自己写出来的文件只有约 1.44 MB —— 每行是
        # `5 位序号 + 空格 + "填充日志" + 空格 + 200 个 x + 换行`，"填充日志" 4 个汉字按
        # UTF-8 是 12 字节，整行 220 字节，6500 行 ≈ 1.43 MB（估算时按字符数当字节数了）。
        # 断言与用例自己造的数据自相矛盾，实现侧无论怎么写都不可能通过。
        # 改成与"只读尾部"真正相关的下限：文件必须明显大于一次回读的字节上限
        # （MAX_TAIL_BYTES = 512 KB），否则"把整个文件读进内存"也看不出区别。
        self.assertGreater(
            self.log_file.stat().st_size,
            2 * logtail.MAX_TAIL_BYTES,
            "日志文件要足够大（> 1 MB），'只读尾部' 的断言才有意义",
        )

        tail = logtail.read_tail(self.log_file, 10)
        self.assertEqual(len(tail.lines), 10)
        self.assertTrue(tail.lines[-1].startswith("06499"), "应当读到文件最后一行")
        self.assertLessEqual(tail.bytes_read, logtail.MAX_TAIL_BYTES, "不许把整个文件读进内存")

        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/logs?lines=10")
        self.assertEqual(response.status_code, 200)
        self.assertIn("06499", response.text)
        self.assertNotIn("06000", response.text)

    def test_日志行数会被钳制在上限内(self) -> None:
        self.assertEqual(logtail.clamp_lines(None), logtail.DEFAULT_LINES)
        self.assertEqual(logtail.clamp_lines("abc"), logtail.DEFAULT_LINES)
        self.assertEqual(logtail.clamp_lines("99999"), logtail.MAX_LINES)
        self.assertEqual(logtail.clamp_lines("0"), 1)
        self.assertEqual(logtail.clamp_lines("-5"), logtail.DEFAULT_LINES)

    def test_日志文件不存在时页面照常渲染(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.assertFalse(self.log_file.exists())
        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/logs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("文件不存在", response.text)

    def test_日志下载带_no_store(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.assert_file_download_no_store()

    def assert_file_download_no_store(self) -> None:
        self.log_file.write_text("下载测试\n", encoding="utf-8")
        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/logs/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertIn("下载测试", response.text)

    def test_日志下载在文件缺失时返回_404(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/logs/download")
        self.assertEqual(response.status_code, 404)

    # ============================================================ 8. 其它路由

    def test_healthz_免登录可用(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_未登录访问_api_返回_401(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.assertEqual(client.get("/api/overview").status_code, 401)
            # 测试自身的问题（2026-10 修正）：httpx/starlette 的 TestClient 默认
            # follow_redirects=True，303 会被自动跟到 /login 并返回 200 —— 不带这个参数就
            # **永远**看不到 303（同文件 test_未登录访问首页会被重定向到登录页 就显式带了
            # follow_redirects=False）。这里补上同样的参数，断言才能命中"未登录被重定向"。
            self.assertEqual(client.get("/ops/logs", follow_redirects=False).status_code, 303)

    def test_静态样式表可以拿到(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            response = client.get("/static/app.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("--bg", response.text)

    def test_图标库真的会被调用(self) -> None:
        """26 个 `<i data-lucide>` 全靠 `lucide.createIcons()` 变成 svg。

        B 把这一句放在 `app.js` 的最末尾（原件 :1149-1153），我们只搬了前 378 行，
        第一次真机截图时页面上一个图标都没有（`svg.lucide` 数量为 0，只剩 26 个空 `<i>`）。
        这里钉住两件事：模板按 lucide → app.js 的顺序引入，且 app.js 里确实调了。
        """
        base = (REPO_ROOT / "webui" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("/static/lucide.min.js", base)
        self.assertLess(
            base.index("/static/lucide.min.js"),
            base.index("/static/app.js"),
            "图标库必须在 app.js 之前加载，否则 window.lucide 还不存在",
        )
        script = (REPO_ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("lucide.createIcons", script)

    def test_分段面板不能是自带_display_的元素(self) -> None:
        """`hidden` 盖不住带 `display` 的类。

        `.notice-inline` 自带 `display: flex`，把它直接标成 `data-segment-panel` 的话，
        第一档之外的两条提示会**同时显示**（第一次真机截图就是这样）。
        分段面板必须是裸 div，里面再放样式元素。
        """
        import re

        tag = re.compile(r"<div[^>]*data-segment-panel[^>]*>", re.S)
        offenders = []
        for path in sorted((REPO_ROOT / "webui" / "templates").rglob("*.html")):
            for found in tag.findall(path.read_text(encoding="utf-8")):
                if "class=" in found:
                    offenders.append(f"{path.name}: {found.strip()}")
        self.assertEqual(offenders, [], f"这些分段面板带了 class，hidden 可能盖不住：{offenders}")

    def test_指标卡保持_b_的四张一行(self) -> None:
        """B 的 `.metric-grid` 是 auto-fit 栅格，第五张会单独掉到第二行。"""
        for name in ("dashboard.html", "runs.html"):
            source = (REPO_ROOT / "webui" / "templates" / name).read_text(encoding="utf-8")
            self.assertEqual(source.count('class="metric-card '), 4, f"{name} 的指标卡不是四张")

    def test_页面自带图标避免_favicon_404(self) -> None:
        """浏览器默认要 /favicon.ico，本服务没有这个路由（日志里每次访问一条 404）。"""
        for name in ("base.html", "login.html"):
            source = (REPO_ROOT / "webui" / "templates" / name).read_text(encoding="utf-8")
            self.assertIn('rel="icon"', source, f"{name} 没有声明图标")
            self.assertNotIn('rel="icon" href="http', source, f"{name} 的图标指向了外网")

    def test_配置展示做了脱敏且只有白名单那一个写入口(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            config_payload = client.get("/api/overview").json()["config"]
            page = client.get("/")
        self.assertNotIn("secret-pass", json.dumps(config_payload, ensure_ascii=False))
        self.assertIn("***", config_payload["proxy_masked"])
        self.assertIn("盖瑞", config_payload["message_template"])
        self.assertIn("盖瑞", page.text, "消息模板原文应当只读展示")
        # 写入口只有「运行配置」这一个，而且只认 webui/settings.py 的白名单键
        # （用户原话：「我希望运行配置能在webui改动」）。别的一律没有。
        self.assertEqual(page.text.count("/ops/config"), page.text.count("/ops/config/save"))
        self.assertEqual(page.text.count('action="/ops/config/save"'), 1)
        for banned in ("/ops/env", "/ops/settings"):
            self.assertNotIn(banned, page.text)

    def test_下次定时来自配置(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            payload = client.get("/api/overview").json()
        self.assertEqual(payload["schedule_text"], "09:00、21:30")
        self.assertTrue(payload["next_run"])
        self.assertIn("09:00", payload["next_run_text"] + payload["schedule_text"])


# ---------------------------------------------------------------------------
# 扫码登录：面板里的「抖音扫码 → 写 COOKIES_<抖音号>」这条路
# ---------------------------------------------------------------------------

SAMPLE_QR_ENV = """\
# 这份注释必须活下来（就地更新，不是重写文件）
WEBUI_PORT=8787
PROXY_ADDRESS=http://user:secret-pass@127.0.0.1:1080
MESSAGE_TEMPLATE=[盖瑞]今日火花[加一]
HITOKOTO_TYPES=["文学","影视"]

TASKS=[{"username":"验收账号甲","unique_id":"12345678905","targets":["验收好友甲"],"fingerprint":12345}]

COOKIES_12345678905=[{"name":"sessionid","value":"SUPER-SECRET-COOKIE-VALUE","domain":".douyin.com"}]
"""

FAKE_QR_PNG = b"\x89PNG\r\n\x1a\n-fake-qr"

SAMPLE_FRIEND_ENV = f"""# 这一行注释必须原样保留
WEBUI_PORT=8787
PROXY_ADDRESS=http://user:secret-pass@127.0.0.1:1080
TASKS=[{{"username":"{ACCOUNT_NAME}","unique_id":"{UID}","fingerprint":12345,"targets":["{FRIEND_MAIN}"]}}]
COOKIES_{UID}=[{{"name":"sessionid","value":"SUPER-SECRET-COOKIE-VALUE","domain":".douyin.com"}}]
"""
# PROXY_ADDRESS 那行不能省：build_context() 会经 store.refresh_env() 重读这份 .env，
# 把父类 setUp 注入的 utils.config.config 覆盖掉 —— 父类的
# test_配置展示做了脱敏且不提供写入口 会因此从「已脱敏」变成「未配置」。


class _FakeScanSession:
    """替身：一次"读好友列表"。items 就是面板要勾的那些人。"""

    instances: List["_FakeScanSession"] = []

    def __init__(self, profile_dir, *, fingerprint="", proxy=None, timeout=0.0,
                 headless=True, owner="", items=(), active=True):
        self.profile_dir = str(profile_dir)
        self.fingerprint = fingerprint
        self.timeout = timeout
        self.owner = owner
        self.cancelled = False
        self._items = list(items)
        self._active = active
        self.adopt_dir = ""
        self.updated_at = time.time()
        self.age_seconds = 2
        _FakeScanSession.instances.append(self)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": "cancelled" if self.cancelled else ("scanning" if self._active else "done"),
            "state_label": "已取消" if self.cancelled else ("正在读取会话列表" if self._active else "读取完成"),
            "message": "已读到 2 个会话…",
            "error": "",
            "count": len(self._items),
            "active": self._active and not self.cancelled,
            "owner": self.owner,
            "fingerprint": self.fingerprint,
            "started_at": time.time(),
            "updated_at": self.updated_at,
            "finished_at": 0.0,
            "age_seconds": self.age_seconds,
            "profile_dir": self.profile_dir,
            "adopt_dir": self.adopt_dir,
            "scanned_all": True,
            "visited": len(self._items),
            "steps": 3,
            "note": "",
            "truncated": False,
            "groups": 0,
        }

    def items(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._items]

    def cancel(self) -> None:
        self.cancelled = True


class FriendPickerWebUITestCase(WebUITestCase):
    """好友列表：读取 → 勾选 → 保存（`/ops/friends/*` 与 `/ops/account/targets`）。"""

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(SAMPLE_FRIEND_ENV, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self.addCleanup(self._restore_env_file)
        self.addCleanup(webui_app._FRIEND_SESSIONS.clear)
        _FakeScanSession.instances = []
        self.addCleanup(setattr, _FakeScanSession, "instances", [])

    def _restore_env_file(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file

    def env_text(self) -> str:
        return self.env.read_text(encoding="utf-8")

    def tasks(self) -> List[Dict[str, Any]]:
        line = [l for l in self.env_text().splitlines() if l.startswith("TASKS=")][0]
        return json.loads(line[len("TASKS="):])

    def install_fake_friend_scan(self, items=(), active=True) -> None:
        """假模块要同时塞 sys.modules 和 core 包属性（见 install_fake_qr_login 的说明）。"""
        import core  # noqa: PLC0415

        module = type(sys)("core.friend_scan")
        module.DEFAULT_TIMEOUT_SECONDS = 300.0
        module.start_session = lambda profile_dir, **kwargs: _FakeScanSession(
            profile_dir, items=items, active=active, **kwargs
        )
        sys.modules["core.friend_scan"] = module
        self.addCleanup(sys.modules.pop, "core.friend_scan", None)

        saved = getattr(core, "friend_scan", None)
        core.friend_scan = module
        if saved is None:
            self.addCleanup(lambda: delattr(core, "friend_scan") if hasattr(core, "friend_scan") else None)
        else:
            self.addCleanup(setattr, core, "friend_scan", saved)

    @staticmethod
    def scanned(name, *, uid="", conv_id="", **extra):
        item = {"name": name, "title": name, "remark": "", "nickname": "",
                "douyin_id": "", "uid": uid, "sec_uid": "", "conv_id": conv_id,
                "is_group": False, "unread": False, "is_muted": False}
        item.update(extra)
        return item

    def pick_value(self, name, *, uid="", conv_id="") -> str:
        payload = {"name": name}
        if uid:
            payload["uid"] = uid
        if conv_id:
            payload["conv_id"] = conv_id
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # ------------------------------------------------ 页面

    def test_首页有好友选择器且老目标已勾上(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertIn("data-friend-picker", page.text)
        self.assertIn("data-friend-search", page.text)
        self.assertIn("data-friend-option", page.text)
        self.assertIn(FRIEND_MAIN, page.text)
        self.assertIn("checked", page.text, "已经保存过的目标要默认勾上")
        self.assertIn("读取好友列表", page.text)
        self.assertIn("保存勾选", page.text)
        self.assertNotIn("SUPER-SECRET-COOKIE-VALUE", page.text)

    def test_读到的会话会出现在勾选列表里(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_friend_scan(
            items=[self.scanned(FRIEND_MAIN, conv_id="0:1:2"),
                   self.scanned("验收好友乙", uid="2002", douyin_id="user_bbb")],
            active=False,
        )
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/friends/scan", data={"unique_id": UID})
            page = client.get("/")
        self.assertIn("验收好友乙", page.text)
        self.assertIn("新读到", page.text, "新读到的要标出来，让人知道哪些是刚拉到的")
        self.assertIn("已读到 2 个会话", page.text)

    # ------------------------------------------------ 读取 / 轮询 / 停止

    def test_未登录时开始读取被挡(self) -> None:
        with self.make_client() as client:
            payload = client.post("/ops/friends/scan", headers={"Accept": "application/json"})
            html = client.post("/ops/friends/scan", headers={"Accept": "text/html"},
                               follow_redirects=False)
        self.assertEqual(payload.status_code, 401)
        self.assertEqual(html.status_code, 303)
        self.assertIn("/login", html.headers["location"])

    def test_开始读取后能轮询到进度(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_friend_scan(items=[self.scanned("验收好友乙", uid="2002")])
        with self.make_client() as client:
            self.login(client)
            started = client.post("/ops/friends/scan", headers={"Accept": "application/json"},
                                  data={"unique_id": UID})
            self.assertEqual(started.status_code, 202, started.text)
            self.assertTrue(started.json()["session"])
            status = client.get("/api/ops/friends").json()
        self.assertTrue(status["ok"])
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["session"]["state"], "scanning")
        self.assertTrue(status["session"]["active"])
        session = _FakeScanSession.instances[-1]
        self.assertIn(UID, session.profile_dir, "读哪个账号就用哪个账号的 profile")
        self.assertEqual(session.owner, UID)
        self.assertEqual(session.timeout, 300.0)

    def test_同时只能读一次(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_friend_scan()
        with self.make_client() as client:
            self.login(client)
            first = client.post("/ops/friends/scan", headers={"Accept": "application/json"},
                                data={"unique_id": UID})
            second = client.post("/ops/friends/scan", headers={"Accept": "application/json"},
                                 data={"unique_id": ""})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertIn("已经有一次好友列表读取在进行", second.json()["error"])
        self.assertEqual(len(_FakeScanSession.instances), 1, "第二次不该再开一个浏览器")

    def test_停止读取(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_friend_scan()
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/friends/scan", headers={"Accept": "application/json"},
                        data={"unique_id": UID})
            stopped = client.post("/ops/friends/cancel", headers={"Accept": "application/json"})
            status = client.get("/api/ops/friends").json()
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(_FakeScanSession.instances[-1].cancelled)
        self.assertFalse(status["session"]["active"])

    def test_没有会话时停止会被拒(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/friends/cancel", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 409)

    def test_表单提交后重定向回首页(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_friend_scan()
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/friends/scan", headers={"Accept": "text/html"},
                                   data={"unique_id": UID}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("friends=", response.headers["location"])

    # ------------------------------------------------ 保存勾选

    def test_勾选保存会写进_env并带上身份(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/targets", headers={"Accept": "application/json"},
                data={"unique_id": UID,
                      "picked": [self.pick_value(FRIEND_MAIN, conv_id="0:1:2"),
                                 self.pick_value("验收好友乙", uid="2002")]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"], response.text)
        targets = self.tasks()[0]["targets"]
        self.assertEqual(targets, [{"name": FRIEND_MAIN, "conv_id": "0:1:2"},
                                   {"name": "验收好友乙", "uid": "2002"}])
        self.assertIn("# 这一行注释必须原样保留", self.env_text())
        self.assertEqual(self.tasks()[0]["fingerprint"], 12345, "指纹不能被改掉")

    def test_保存时点名这次少掉了哪些目标(self) -> None:
        """整表覆盖式保存会把这次没勾上的旧目标删掉，提示里必须报出删了谁。

        2026-10-11 示例号 的目标从 4 个变成 2 个，面板当时只说"已保存 2 个"，
        少掉的两个要到下一轮发送时才发现 —— 不能让这种删除继续静默。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/targets", headers={"Accept": "text/html"},
                follow_redirects=False,
                data={"unique_id": UID,
                      "picked": [self.pick_value("验收好友乙", uid="2002")]},
            )
        self.assertEqual(response.status_code, 303, response.text)
        warn = unquote(response.headers["location"])
        self.assertIn("已保存（1 个", warn)
        self.assertIn("已移除 1 个", warn)
        self.assertIn(FRIEND_MAIN, warn)
        # 提示归提示，写进去的就是勾上的那一个
        self.assertEqual(self.tasks()[0]["targets"], [{"name": "验收好友乙", "uid": "2002"}])

    def test_没少目标时不多嘴(self) -> None:
        """对照：勾上原来那个目标时提示里不该出现"移除了"。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/targets", headers={"Accept": "text/html"},
                follow_redirects=False,
                data={"unique_id": UID, "picked": [self.pick_value(FRIEND_MAIN)]},
            )
        self.assertEqual(response.status_code, 303, response.text)
        warn = unquote(response.headers["location"])
        self.assertIn("已保存（1 个", warn)
        self.assertNotIn("移除", warn)

    def test_手填文本仍然能用(self) -> None:
        """老用法（直接写名字）不能因为改了界面就失效。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/account/targets",
                                   headers={"Accept": "application/json"},
                                   data={"unique_id": UID, "targets": "验收好友丙\n验收好友丁"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.tasks()[0]["targets"], ["验收好友丙", "验收好友丁"])

    def test_保存非法值不会写坏_env(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        before = self.env_text()
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/account/targets",
                                   headers={"Accept": "application/json"},
                                   data={"unique_id": "不存在的账号", "picked": [self.pick_value("甲")]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.env_text(), before, "拒绝时不许动文件")


class AccountMessageWebUITestCase(WebUITestCase):
    """账号卡里的「发送内容」（`POST /ops/account/message`）。

    这是"每个账号发不同的话"的用户入口，必须能写进 `.env`、能在页面上看见回显，
    并且拒绝的时候一个字节都不动。
    """

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(SAMPLE_FRIEND_ENV, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self.addCleanup(self._restore_env_file)

    def _restore_env_file(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file

    def env_text(self) -> str:
        return self.env.read_text(encoding="utf-8")

    def task(self) -> Dict[str, Any]:
        line = [l for l in self.env_text().splitlines() if l.startswith("TASKS=")][0]
        return json.loads(line[len("TASKS="):])[0]

    def test_保存内容会写进env(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/message", headers={"Accept": "application/json"},
                data={"unique_id": UID, "message": "只发给这个账号的一句话"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"], response.text)
        self.assertEqual(self.task()["message"], "只发给这个账号的一句话")
        self.assertIn("# 这一行注释必须原样保留", self.env_text())
        self.assertEqual(self.task()["fingerprint"], 12345, "指纹不能被改掉")
        self.assertEqual(self.task()["targets"], [FRIEND_MAIN], "目标不能被改掉")

    def test_多行内容能存进env的一行里(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/account/message",
                                   headers={"Accept": "application/json"},
                                   data={"unique_id": UID, "message": "第一行\n第二行"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.task()["message"], "第一行\n第二行")
        tasks_line = [l for l in self.env_text().splitlines() if l.startswith("TASKS=")]
        self.assertEqual(len(tasks_line), 1, "TASKS 必须还是一行")

    def test_清空就删掉那个键(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/account/message", headers={"Accept": "application/json"},
                        data={"unique_id": UID, "message": "先配一条"})
            response = client.post("/ops/account/message",
                                   headers={"Accept": "application/json"},
                                   data={"unique_id": UID, "message": ""})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("message", self.task())

    def test_太长会被拒绝且不动文件(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        before = self.env_text()
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/message", headers={"Accept": "application/json"},
                data={"unique_id": UID,
                      "message": "长" * (envfile.MESSAGE_MAX_CHARS + 1)},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertIn("太长", response.json()["error"])
        self.assertEqual(self.env_text(), before, "拒绝时不许动文件")

    def test_不存在的账号会被拒绝(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/account/message",
                                   headers={"Accept": "application/json"},
                                   data={"unique_id": "不存在的账号", "message": "x"})
        self.assertEqual(response.status_code, 400)

    def test_页面表单分支会跳回首页并提示(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/message", headers={"Accept": "text/html"},
                data={"unique_id": UID, "message": "表单分支的内容"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.task()["message"], "表单分支的内容")

    def test_账号卡上看得见输入框和回显(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/account/message", headers={"Accept": "application/json"},
                        data={"unique_id": UID, "message": "卡片上要看得见我"})
            page = client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn('action="/ops/account/message"', html)
        self.assertIn('name="message"', html)
        self.assertIn("卡片上要看得见我", html)
        self.assertIn("这个账号单独配了内容", html)

    def test_没配过的账号显示用的是全局模板(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertIn("没单独配置，下一轮用全局模板", page.text)


class _FakeQrSession:
    """替身：只实现面板真正用到的那几个成员（不碰浏览器）。"""

    instances: List["_FakeQrSession"] = []

    def __init__(self, profile_dir, *, fingerprint="", proxy=None, ttl=0.0, on_success=None, headless=True):
        self.profile_dir = str(profile_dir)
        self.fingerprint = fingerprint
        self.ttl = ttl
        self.on_success = on_success
        self.cancelled = False
        self._state = "waiting_scan"
        self._png = FAKE_QR_PNG
        # 二次验证（风控账号）：面板提交的验证码收在这里，供测试断言
        self.codes: List[str] = []
        self._need_code = False
        # 登录成功后要搬去的目录（面板靠它判断"搬迁途中"也不能被抢）
        self.adopt_dir = ""
        self.updated_at = time.time()
        self.age_seconds = 3
        _FakeQrSession.instances.append(self)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "state": "cancelled" if self.cancelled else self._state,
            "state_label": "已取消" if self.cancelled else "等待扫码",
            "message": "用抖音 App 扫一扫" if not self.cancelled else "已请求取消，正在关闭浏览器…",
            "hint": "",
            "error": "",
            "qr_seq": 1,
            "has_qr": bool(self._png),
            "active": not self.cancelled,
            "started_at": time.time(),
            "updated_at": self.updated_at,
            "finished_at": 0.0,
            "age_seconds": self.age_seconds,
            "profile_dir": self.profile_dir,
            "adopt_dir": self.adopt_dir,
            "account": None,
            "persist": None,
            "need_code": self._need_code,
            "code_tries": len(self.codes),
            "code_max_attempts": 3,
            "code_error": "",
            "code_options": [],
            "has_shot": bool(self._png),
            "shot_seq": 1,
        }

    def qr_png(self) -> bytes:
        return self._png

    def shot_png(self) -> bytes:
        return self._png

    def submit_code(self, code: str) -> Dict[str, Any]:
        text = "".join(str(code or "").split())
        if not text.isdigit():
            return {"ok": False, "error": "验证码应该是一串数字"}
        self.codes.append(text)
        return {"ok": True}

    def cancel(self) -> None:
        self.cancelled = True


class QrLoginWebUITestCase(WebUITestCase):
    """`/ops/login/*` 与账号表；全部走临时 .env，绝不碰仓库里那份。"""

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(SAMPLE_QR_ENV, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self.addCleanup(self._restore_env_file)
        self.addCleanup(webui_app._LOGIN_SESSIONS.clear)
        _FakeQrSession.instances = []
        self.addCleanup(setattr, _FakeQrSession, "instances", [])

    def _restore_env_file(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file

    def install_fake_qr_login(self) -> None:
        """把假模块同时塞进 sys.modules **和** core 包属性。

        只塞 sys.modules 是不够的：别的测试（tests/test_qr_login.py）一旦真的 import 过
        `core.qr_login`，`from core import qr_login` 会优先取 core 包上的那个属性，
        假模块就被跳过了 —— 单跑本文件能过、全量跑就失败。
        """
        import core  # noqa: PLC0415

        module = type(sys)("core.qr_login")
        module.DEFAULT_TTL_SECONDS = 300.0
        module.STATE_WAITING_SCAN = "waiting_scan"
        module.start_session = lambda profile_dir, **kwargs: _FakeQrSession(profile_dir, **kwargs)
        sys.modules["core.qr_login"] = module
        self.addCleanup(sys.modules.pop, "core.qr_login", None)

        saved = getattr(core, "qr_login", None)
        core.qr_login = module
        if saved is None:
            self.addCleanup(lambda: delattr(core, "qr_login") if hasattr(core, "qr_login") else None)
        else:
            self.addCleanup(setattr, core, "qr_login", saved)

    # ------------------------------------------------ 页面

    def test_首页有扫码段与账号表(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
            payload = client.get("/api/ops/login").json()
        self.assertIn("扫码登录", page.text)
        self.assertIn("data-login-root", page.text)
        self.assertIn("data-login-qr", page.text)
        self.assertIn("data:," , page.text, "还没开始时不该去拉 qr.png（会白挨一个 404）")
        self.assertIn(ACCOUNT_NAME, page.text)
        self.assertIn("COOKIES_12345678905", page.text, "账号表要写清楚写到哪个变量名")
        self.assertNotIn("SUPER-SECRET-COOKIE-VALUE", page.text, "页面只报数量，不回显 cookie 值")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["session"]["active"], "还没开始扫码，状态应当是静止的")

    def test_导航里有扫码登录入口(self) -> None:
        source = (REPO_ROOT / "webui" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn('href="/#login-panel"', source)

    # ------------------------------------------------ 开始 / 轮询 / 取消

    def test_未登录时开始扫码被挡(self) -> None:
        with self.make_client() as client:
            json_response = client.post("/ops/login/start", headers={"Accept": "application/json"})
            html_response = client.post(
                "/ops/login/start", headers={"Accept": "text/html"}, follow_redirects=False
            )
        self.assertEqual(json_response.status_code, 401)
        self.assertEqual(html_response.status_code, 303)
        self.assertIn("/login", html_response.headers["location"])

    def test_开始扫码后能轮询到状态并取到二维码(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            started = client.post(
                "/ops/login/start", headers={"Accept": "application/json"},
                data={"unique_id": UID, "username": ACCOUNT_NAME},
            )
            self.assertEqual(started.status_code, 202, started.text)
            session_id = started.json()["session"]

            status = client.get("/api/ops/login").json()
            qr = client.get("/ops/login/qr.png")

        self.assertTrue(status["ok"])
        self.assertEqual(status["session"]["state"], "waiting_scan")
        self.assertTrue(status["session"]["has_qr"])
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.headers["content-type"], "image/png")
        self.assertIn("no-store", qr.headers.get("cache-control", ""))
        self.assertEqual(qr.content, FAKE_QR_PNG)
        self.assertTrue(session_id)
        session = _FakeQrSession.instances[-1]
        self.assertIn(UID, session.profile_dir, "每个账号一个持久化 profile，别互相踩")
        self.assertEqual(session.ttl, 300.0)

    def test_同时只能有一次扫码(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            first = client.post("/ops/login/start", headers={"Accept": "application/json"})
            second = client.post("/ops/login/start", headers={"Accept": "application/json"})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertIn("已经有一次扫码登录在进行", second.json()["error"])
        self.assertEqual(len(_FakeQrSession.instances), 1, "第二次不该真的再起一个浏览器")

    def test_二维码还没出时返回_404(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.get("/ops/login/qr.png")
        self.assertEqual(response.status_code, 404)

    def test_取消扫码(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/login/start", headers={"Accept": "application/json"})
            cancelled = client.post("/ops/login/cancel", headers={"Accept": "application/json"})
            status = client.get("/api/ops/login").json()
        self.assertEqual(cancelled.status_code, 200)
        self.assertTrue(cancelled.json()["ok"])
        self.assertTrue(_FakeQrSession.instances[-1].cancelled)
        self.assertEqual(status["session"]["state"], "cancelled")

    def test_验证码送进正在等它的会话(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/login/start", headers={"Accept": "application/json"})
            response = client.post(
                "/ops/login/code", headers={"Accept": "application/json"},
                data={"code": "  1 2 3 4  "},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(_FakeQrSession.instances[-1].codes, ["1234"], "空白要去掉")

    def test_没有会话时提交验证码会被拒(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/login/code", headers={"Accept": "application/json"}, data={"code": "1234"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("没有发起过扫码登录", response.json()["error"])

    def test_验证码页截图(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            before = client.get("/ops/login/shot.png")
            client.post("/ops/login/start", headers={"Accept": "application/json"})
            after = client.get("/ops/login/shot.png")
        self.assertEqual(before.status_code, 404, "还没有截图时别给张空图")
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.headers["content-type"], "image/png")

    def test_首页带着验证码输入框(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertIn("data-login-code-wrap", page.text)
        self.assertIn("data-login-code-input", page.text)
        self.assertIn('action="/ops/login/code"', page.text)

    def test_没有会话时取消会被拒(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post("/ops/login/cancel", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("没有正在进行的扫码登录", response.json()["error"])

    def test_表单提交后重定向回首页(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/login/start", headers={"Accept": "text/html"},
                data={"unique_id": UID}, follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("login=", response.headers["location"])

    # ------------------------------------------------ 资料目录跟着账号走

    def _persist_with(self, session, unique_id: str) -> Dict[str, Any]:
        """按面板真实路径调一次 on_success（假 session 只是拿着回调）。"""
        return session.on_success({
            "cookies": [{"name": "sessionid", "value": "x", "domain": ".douyin.com"}],
            "cookies_json": '[{"name": "sessionid", "value": "x"}]',
            "account": {"unique_id": unique_id, "nickname": ACCOUNT_NAME},
            "profile_dir": session.profile_dir,
        })

    def test_新账号登录成功后把资料目录改挂到账号名下(self) -> None:
        """真机踩过：新建账号时还不知道抖音号，扫码只能用 `default/`，
        登录成功后按账号读好友列表打开的是 `<抖音号>/` —— 报"这个账号还没登录"。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/login/start", headers={"Accept": "application/json"})
        session = _FakeQrSession.instances[-1]
        self.assertTrue(session.profile_dir.endswith("default"), session.profile_dir)
        persist = self._persist_with(session, UID)
        self.assertTrue(persist["ok"], persist)
        self.assertEqual(persist["adopt_profile"], str(webui_app.login_profile_dir(UID)),
                         "新建账号：登录成功后要把 default 搬到 <抖音号>/")

    def test_已有账号再登录不会重复搬目录(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.install_fake_qr_login()
        with self.make_client() as client:
            self.login(client)
            client.post("/ops/login/start", headers={"Accept": "application/json"},
                        data={"unique_id": UID})
        session = _FakeQrSession.instances[-1]
        self.assertIn(UID, session.profile_dir)
        persist = self._persist_with(session, UID)
        self.assertEqual(persist["adopt_profile"], "", "目录已经对了，不该再搬")

    # ------------------------------------------------ 账号表上的两个写操作
    def test_保存目标好友会就地写进_env(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/targets", headers={"Accept": "application/json"},
                data={"unique_id": UID, "targets": "验收好友乙, 验收好友丙，验收好友乙\n验收好友丁"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"], response.text)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("# 这份注释必须活下来（就地更新，不是重写文件）", text)
        tasks = json.loads(
            [line for line in text.splitlines() if line.startswith("TASKS=")][0][len("TASKS=") :]
        )
        self.assertEqual(
            tasks[0]["targets"], ["验收好友乙", "验收好友丙", "验收好友丁"],
            "中英文逗号与换行都要认，重复项要去掉",
        )
        self.assertEqual(tasks[0]["fingerprint"], 12345, "已有指纹不能被动")

    def test_给未知账号保存目标会报错(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/targets", headers={"Accept": "application/json"},
                data={"unique_id": "99999999999", "targets": "验收好友甲"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_移除账号会删掉_cookie(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/remove", headers={"Accept": "application/json"},
                data={"unique_id": UID},
            )
            page = client.get("/")
        self.assertEqual(response.status_code, 200, response.text)
        text = self.env.read_text(encoding="utf-8")
        self.assertNotIn("COOKIES_12345678905", text)
        self.assertEqual(json.loads([l for l in text.splitlines() if l.startswith("TASKS=")][0][6:]), [])
        self.assertNotIn(ACCOUNT_NAME, page.text, "移除后账号表里不该还有它")

    def test_页面上的写操作都带_csrf_校验(self) -> None:
        """无 Origin/Referer 时 _csrf_ok 放行（命令行友好），但跨站要拒。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/targets",
                headers={"Accept": "application/json", "Origin": "http://evil.example"},
                data={"unique_id": UID, "targets": "验收好友甲"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("跨站", response.json()["error"])


GHOST_UID = "12345678906"
GHOST_NAME = "验收幽灵账号"


class GhostAccountWebUITestCase(WebUITestCase):
    """只活在运行记录里、`TASKS` 里已经没有的账号。

    真机事故（2026-10-01）：用户删掉账号后它**又从账本里冒出来**，
    对着这张卡点「移除」只会拿到"TASKS 里没有 X"，看起来就像按钮坏了。
    现在它必须被标成 orphan、不提供移除按钮、也不计入汇总。
    """

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(SAMPLE_FRIEND_ENV, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self.addCleanup(self._restore_env_file)

    def _restore_env_file(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file

    def write_ghost_ledger(self) -> None:
        self.write_ledger(
            [
                self.sample_run(
                    accounts=[
                        self.sample_account(),
                        self.sample_account(
                            unique_id=GHOST_UID, username=GHOST_NAME, sent_ok=0, sent_fail=0
                        ),
                    ]
                )
            ]
        )
        self.utils_config.userData = [
            {
                "unique_id": UID,
                "username": ACCOUNT_NAME,
                "fingerprint": "12345",
                "cookies": [{"name": "sessionid", "value": "x"}],
                "targets": [FRIEND_MAIN],
            }
        ]

    def test_幽灵账号被标出来且不给移除按钮(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ghost_ledger()
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertIn("data-account-orphan", text, "幽灵账号必须有一段说明")
        self.assertIn("已不在配置里", text)
        self.assertIn(GHOST_NAME, text, "它仍然要显示出来，只是标明来路")
        # 不能再给它一个点了也没用的「移除」
        ghost_card = text.split(f'data-account-overview="{GHOST_UID}"')[1]
        ghost_card = ghost_card.split("</article>")[0]
        self.assertNotIn("/ops/account/remove", ghost_card)
        self.assertNotIn("/ops/login/start", ghost_card)
        # 真账号照旧有按钮
        live_card = text.split(f'data-account-overview="{UID}"')[1].split("</article>")[0]
        self.assertIn("/ops/account/remove", live_card)

    def test_幽灵账号不计入共几个目标(self) -> None:
        self.write_ghost_ledger()
        context = webui_app.build_context()
        view = context["view"]
        ghost = [
            card for card in view["account_cards"] if card["unique_id"] == GHOST_UID
        ]
        self.assertEqual(len(ghost), 1)
        self.assertTrue(ghost[0]["orphan"])
        self.assertEqual(view["summary"]["accounts"], 1, "汇总只算配置里的账号")
        live_total = sum(
            card["total"] for card in view["account_cards"] if not card["orphan"]
        )
        self.assertEqual(view["summary"]["total"], live_total)
        self.assertEqual(view["summary"]["accounts_blocked"], 0, "幽灵账号不该算成被阻断")

    def test_对幽灵账号点移除会给出能看懂的原因(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ghost_ledger()
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/account/remove",
                headers={"Accept": "application/json"},
                data={"unique_id": GHOST_UID},
            )
        self.assertEqual(response.status_code, 400, response.text)
        error = response.json()["error"]
        self.assertIn("TASKS 里没有", error)
        self.assertIn("运行记录", error)
        self.assertEqual(
            self.env.read_text(encoding="utf-8").count("TASKS="), 1, ".env 不该被改动"
        )


class StaticAssetWebUITestCase(WebUITestCase):
    """静态资源的缓存：以前版本串写死，浏览器拿旧 CSS 配新 HTML = 页面错位。"""

    def test_版本串跟着文件_mtime_走(self) -> None:
        version = webui_app.asset_version()
        self.assertRegex(version, r"^\d{12}$", f"版本串应当是 12 位时间戳，实际 {version!r}")
        # 取静态文件里较新的那个 mtime（任何一个变了版本串就得变；两套皮肤都算）
        stamp = max(
            (webui_app.STATIC_DIR / name).stat().st_mtime
            for name in ("app.css", "app-aurora.css", "app.js")
        )
        expected = time.strftime("%Y%m%d%H%M", time.localtime(stamp))
        self.assertEqual(version, expected)

    def test_页面引用的版本串就是当前版本(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertIn(f"app.css?v={webui_app.asset_version()}", page.text)
        self.assertIn(f"app.js?v={webui_app.asset_version()}", page.text)
        self.assertNotIn("app.css?v=20260919", page.text, "不能再用那个写死的版本串")

    def test_静态文件要求浏览器回源校验(self) -> None:
        with self.make_client() as client:
            response = client.get("/static/app.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers.get("cache-control", "").lower())

    def test_hidden_属性必须压得住_display_规则(self) -> None:
        """兜底规则不能丢：`[hidden]` 必须赢过任何 `display:`。

        2026-10-10 用户报「没扫码也一直显示这个账号需要二次验证」：`.code-box { display: grid }`
        把 `hidden` 顶掉了（UA 的 `[hidden] { display: none }` 优先级低于任何作者样式），
        服务端 `need_code=false`、JS 也把 `hidden` 置上了，那一块照样亮着。
        """
        css = (webui_app.STATIC_DIR / "app.css").read_text(encoding="utf-8")
        self.assertRegex(
            css,
            r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
            "app.css 缺少 `[hidden] { display: none !important; }` 兜底规则",
        )

    def test_二次验证框默认是收起的(self) -> None:
        """没有会话（更没扫码）时，那块「这个账号需要二次验证」必须带 hidden。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/")
        self.assertRegex(
            page.text,
            r'class="code-box"[^>]*\bhidden\b',
            "默认收起：服务端渲染时 need_code=false，那一段必须带 hidden",
        )


class ProfileContentionWebUITestCase(WebUITestCase):
    """同一个 Chromium 资料目录一次只能开一个实例。

    好友列表读取（`core/friend_scan.py`）和扫码登录（`core/qr_login.py`）都用
    `login_profile_dir(uid)` 当 user-data-dir，两边撞上时后起的那个会莫名起不来，
    所以面板必须在**起之前**拦住。
    """

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(SAMPLE_FRIEND_ENV, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self.addCleanup(self._restore_env_file)
        self.addCleanup(webui_app._FRIEND_SESSIONS.clear)
        self.addCleanup(webui_app._LOGIN_SESSIONS.clear)
        _FakeScanSession.instances = []
        self.addCleanup(setattr, _FakeScanSession, "instances", [])
        self.addCleanup(setattr, webui_app, "_FRIEND_LATEST", webui_app._FRIEND_LATEST)

    def _restore_env_file(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file

    def _put_scan(self, uid: str, *, active: bool = True) -> None:
        session = _FakeScanSession(webui_app.login_profile_dir(uid), owner=uid, active=active)
        webui_app._FRIEND_SESSIONS["scan-1"] = session
        self.addCleanup(setattr, webui_app, "_FRIEND_LATEST", "scan-1")

    def test_资料目录名不接受纯点号(self) -> None:
        root = webui_app.login_profile_dir("")
        self.assertEqual(webui_app.login_profile_dir(".."), root)
        self.assertEqual(webui_app.login_profile_dir("...."), root)
        self.assertEqual(webui_app.login_profile_dir("---"), root)
        self.assertEqual(webui_app.login_profile_dir("../../etc"), root.parent / "....etc")
        self.assertEqual(
            webui_app.login_profile_dir("../../etc").parent,
            root.parent,
            "再离谱的抖音号也只能落在资料根目录里",
        )

    def test_正在读好友列表时不许对同一账号重新登录(self) -> None:
        self._put_scan(UID)
        result = webui_app.start_login_session(unique_id=UID)
        self.assertFalse(result["ok"])
        self.assertIn("读取好友列表", result["error"])

    def test_正在扫码时不许读同一账号的好友列表(self) -> None:
        webui_app._LOGIN_SESSIONS["login-1"] = _FakeQrSession(
            webui_app.login_profile_dir(UID)
        )
        self.addCleanup(setattr, webui_app, "_LOGIN_LATEST", "login-1")
        blocked = webui_app.start_friend_scan(unique_id=UID)
        self.assertFalse(blocked["ok"])
        self.assertIn("扫码登录", blocked["error"])

    def test_别的账号在扫码不影响读这个账号的列表(self) -> None:
        webui_app._LOGIN_SESSIONS["login-2"] = _FakeQrSession(
            webui_app.login_profile_dir("99998888777")
        )
        self.addCleanup(setattr, webui_app, "_LOGIN_LATEST", "login-2")
        result = webui_app.start_friend_scan(unique_id=UID)
        self.assertTrue(result["ok"], "资料目录不一样，不该被拦")


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102 - 只给 _csrf_ok 用
        return dict.get(self, str(key).lower(), default)


class _FakeUrl:
    def __init__(self, scheme: str = "http") -> None:
        self.scheme = scheme


class _FakeRequest:
    """`_csrf_ok` 只用到 headers 与 url.scheme，不必真起一个 Starlette 请求。"""

    def __init__(self, headers: Optional[Dict[str, str]] = None, scheme: str = "http") -> None:
        self.headers = _FakeHeaders({str(k).lower(): v for k, v in (headers or {}).items()})
        self.url = _FakeUrl(scheme)


HOST = "127.0.0.1:8787"


class CrossSiteGuardTests(WebUITestCase):
    """CSRF：两个头都没有就放行的老缺口，用 `Sec-Fetch-Site` 补上。"""

    def test_非浏览器客户端仍然放行(self) -> None:
        self.assertTrue(webui_app._csrf_ok(_FakeRequest({})))

    def test_同源请求放行(self) -> None:
        self.assertTrue(webui_app._csrf_ok(_FakeRequest({"sec-fetch-site": "same-origin"})))
        self.assertTrue(
            webui_app._csrf_ok(_FakeRequest({"origin": f"http://{HOST}", "host": HOST}))
        )

    def test_跨站标记的请求一律拒绝(self) -> None:
        self.assertFalse(webui_app._csrf_ok(_FakeRequest({"sec-fetch-site": "cross-site"})))
        self.assertFalse(
            webui_app._csrf_ok(
                _FakeRequest({"sec-fetch-site": "cross-site", "origin": f"http://{HOST}"})
            ),
            "哪怕 Origin 看着对，跨站标记也说了算",
        )
        self.assertFalse(
            webui_app._csrf_ok(_FakeRequest({"origin": "http://evil.example", "host": HOST}))
        )

    def test_同站但不同源的请求也拒绝(self) -> None:
        """`same-site` = 同站点的另一个端口/子域，没有理由驱动这台机器的面板。"""
        self.assertFalse(webui_app._csrf_ok(_FakeRequest({"sec-fetch-site": "same-site"})))

    def test_不透明来源_null_拒绝(self) -> None:
        """沙箱 iframe / file:// / data: 发出来的就是 `Origin: null`。"""
        self.assertFalse(
            webui_app._csrf_ok(_FakeRequest({"origin": "null", "host": HOST})),
            "origin=null 不能当同源放行",
        )

    def test_伪造域名_host_拒绝(self) -> None:
        """DNS rebinding：让 evil.example 解析到本机，Host 与 Origin 都自洽。"""
        self.assertFalse(
            webui_app._csrf_ok(
                _FakeRequest({"host": "evil.example", "origin": "http://evil.example"})
            ),
            "带点的域名不是本机的名字，一律当跨站",
        )

    def test_局域网_ip_与短名照常放行(self) -> None:
        """面板是局域网工具：私网 IP、短名（机器名）都得能用。"""
        self.assertTrue(
            webui_app._csrf_ok(
                _FakeRequest({"host": "192.168.1.100:8787", "origin": "http://192.168.1.100:8787"})
            )
        )
        self.assertTrue(
            webui_app._csrf_ok(_FakeRequest({"host": "nas:8787", "origin": "http://nas:8787"}))
        )
        self.assertTrue(webui_app._csrf_ok(_FakeRequest({"host": "testserver"})), "测试客户端不发 Origin")

    def test_显式信任的域名放行(self) -> None:
        """用户用 dsf.local 这类名字访问时，写进 WEBUI_TRUSTED_HOSTS 就能放行。"""
        saved = os.environ.get("WEBUI_TRUSTED_HOSTS")

        def restore() -> None:
            if saved is None:
                os.environ.pop("WEBUI_TRUSTED_HOSTS", None)
            else:
                os.environ["WEBUI_TRUSTED_HOSTS"] = saved
            webui_app._HOST_CACHE = None

        self.addCleanup(restore)
        os.environ["WEBUI_TRUSTED_HOSTS"] = "panel.corp"
        webui_app._HOST_CACHE = None
        self.assertTrue(
            webui_app._csrf_ok(_FakeRequest({"host": "panel.corp", "origin": "http://panel.corp"}))
        )


class ProfileAdoptGuardTests(WebUITestCase):
    """扫码登录成功后会"搬迁"资料目录：搬迁途中那个目标目录也不能被抢。"""

    UID = "12345678905"

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(webui_app._LOGIN_SESSIONS.clear)
        self.addCleanup(webui_app._FRIEND_SESSIONS.clear)
        self.addCleanup(setattr, webui_app, "_LOGIN_LATEST", webui_app._LOGIN_LATEST)

    def test_搬迁目标目录也算被占着(self) -> None:
        session = _FakeQrSession(webui_app.login_profile_dir(""))
        session.adopt_dir = str(webui_app.login_profile_dir(self.UID))
        webui_app._LOGIN_SESSIONS["login-adopt"] = session
        webui_app._LOGIN_LATEST = "login-adopt"
        blocked = webui_app.start_friend_scan(unique_id=self.UID)
        self.assertFalse(blocked["ok"], "搬迁目标就是账号自己的目录，不能被好友读取抢走")
        self.assertIn("扫码登录", blocked["error"])
        self.assertEqual(blocked["session"] if "session" in blocked else "", "")


class StuckSessionReapTests(WebUITestCase):
    """卡住的会话必须能被回收：否则那个账号在面板上永久失效。"""

    UID = "12345678905"

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(webui_app._FRIEND_SESSIONS.clear)
        self.addCleanup(webui_app._LOGIN_SESSIONS.clear)
        self.addCleanup(setattr, webui_app, "_FRIEND_LATEST", webui_app._FRIEND_LATEST)
        self.addCleanup(setattr, webui_app, "_LOGIN_LATEST", webui_app._LOGIN_LATEST)

    def _stuck(self) -> _FakeScanSession:
        session = _FakeScanSession(webui_app.login_profile_dir(self.UID), owner=self.UID)
        session.updated_at = time.time() - (webui_app.SESSION_STUCK_SECONDS + 60)
        session.age_seconds = int(webui_app.SESSION_STUCK_SECONDS + 60)
        return session

    def test_卡住的会话会被清出去(self) -> None:
        stuck = self._stuck()
        webui_app._FRIEND_SESSIONS["scan-stuck"] = stuck
        webui_app._FRIEND_LATEST = "scan-stuck"
        dropped = webui_app._reap_stuck_sessions(
            webui_app._FRIEND_SESSIONS, webui_app._FRIEND_LOCK, label="好友列表读取"
        )
        self.assertEqual(dropped, ["scan-stuck"])
        self.assertNotIn("scan-stuck", webui_app._FRIEND_SESSIONS)
        self.assertTrue(stuck.cancelled, "清出去之前先发一次取消信号")

    def test_还在动的会话不许动(self) -> None:
        fresh = _FakeScanSession(webui_app.login_profile_dir(self.UID), owner=self.UID)
        webui_app._FRIEND_SESSIONS["scan-live"] = fresh
        dropped = webui_app._reap_stuck_sessions(
            webui_app._FRIEND_SESSIONS, webui_app._FRIEND_LOCK, label="好友列表读取"
        )
        self.assertEqual(dropped, [])
        self.assertIn("scan-live", webui_app._FRIEND_SESSIONS)

    def test_卡死之后账号还能重新发起(self) -> None:
        webui_app._FRIEND_SESSIONS["scan-stuck"] = self._stuck()
        webui_app._FRIEND_LATEST = "scan-stuck"
        result = webui_app.start_friend_scan(unique_id=self.UID)
        self.assertTrue(result["ok"], "卡死的旧会话不该把账号永久锁住")


class FailedRunLedgerTests(WebUITestCase):
    """后台线程出事时，账本里必须留下一条失败记录（否则页面像什么都没发生）。"""

    def setUp(self) -> None:
        super().setUp()
        self.ledger = self.tmp / "runs.jsonl"
        self.addCleanup(setattr, webui_app, "_RUN_RESULTS", dict(webui_app._RUN_RESULTS))

    def _records(self) -> List[Dict[str, Any]]:
        if not self.ledger.exists():
            return []
        return [
            json.loads(line)
            for line in self.ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_线程崩了要补一条失败记录(self) -> None:
        from core import run_ledger

        self.addCleanup(setattr, run_ledger, "ledger_path", run_ledger.ledger_path)
        run_ledger.ledger_path = lambda: self.ledger  # type: ignore[assignment]

        def boom(_run_id: str, _dry_run: str, **_kw) -> Dict[str, Any]:
            # `run_once` 现在还会收到 force（面板「立即运行」= 不看「到没到点」）
            raise RuntimeError("runTasks 炸了")

        self.addCleanup(setattr, webui_app, "run_once", webui_app.run_once)
        webui_app.run_once = boom  # type: ignore[assignment]
        result = webui_app.run_in_thread("20261001-120000-abcd", "off")
        self.assertFalse(result["ok"])
        self.assertIn("runTasks 炸了", result["error"])
        records = self._records()
        self.assertEqual(len(records), 1, "一轮点击 = 一条账本记录")
        self.assertEqual(records[0]["run_id"], "20261001-120000-abcd")
        self.assertEqual(records[0]["status"], "error")
        self.assertIn("runTasks 炸了", records[0]["error"])
        self.assertEqual(records[0]["source"], "webui")

    def test_core_自己写了记录就不重复记(self) -> None:
        from core import run_ledger

        self.addCleanup(setattr, run_ledger, "ledger_path", run_ledger.ledger_path)
        run_ledger.ledger_path = lambda: self.ledger  # type: ignore[assignment]

        def wrote_a_row(run_id: str, _dry_run: str, **_kw) -> Dict[str, Any]:
            run_ledger.append_run({"run_id": "core-1", "status": "ok", "started_at": "2026-10-01T12:00:00"})
            return {"run_id": run_id, "ok": False, "error": "全部账号都执行失败"}

        self.addCleanup(setattr, webui_app, "run_once", webui_app.run_once)
        webui_app.run_once = wrote_a_row  # type: ignore[assignment]
        webui_app.run_in_thread("20261001-120500-abcd", "off")
        records = self._records()
        self.assertEqual([row["run_id"] for row in records], ["core-1"], "不能记两遍")

    def test_补记的失败记录在页面上不许显示成成功(self) -> None:
        """账本里 `status="error"` 但 accounts 为空时，不能因为"没有账号失败"就算成功。

        回归（2026-10 审计）：`normalize_run()` 的失败判定只看每个账号的结果，
        面板替 core 补记的"这一轮没跑起来"记录 accounts 是空的 → 页面上是绿色的「成功」，
        而且那句兜底文案还会把它说成"账号凭据可能未配置"。
        """
        row = store.normalize_run(
            {
                "run_id": "20261001-120000-abcd",
                "started_at": "2026-10-01T12:00:00+08:00",
                "finished_at": "2026-10-01T12:00:01+08:00",
                "dry_run": "off",
                "source": "webui",
                "status": "error",
                "error": "任务线程崩了：RuntimeError: runTasks 炸了",
                "note": "面板触发的一轮没能跑起来（core 连账本都没来得及写）",
                "accounts": [],
            }
        )
        self.assertEqual(row.status, "error")
        self.assertEqual(row.status_text, "有失败")
        self.assertIn("没能跑起来", row.note)
        self.assertIn("runTasks 炸了", row.note, "崩在哪要看得见")
        self.assertNotIn("凭据可能未配置", row.note, "别把崩溃说成没配 cookie")
        view = viewmodel.run_row(row)
        self.assertEqual(view["chip"], "danger")

    def test_不认识的账号状态不许默认成成功(self) -> None:
        row = store.normalize_run(
            {
                "run_id": "r1",
                "started_at": "2026-10-01T12:00:00+08:00",
                "accounts": [{"username": "甲", "unique_id": "1", "status": "weird_new_state"}],
            }
        )
        self.assertEqual(row.accounts[0]["status"], "weird_new_state", "原样留着才查得出问题")
        self.assertEqual(row.accounts[0]["status_text"], "weird_new_state")

    def test_没有账号结果的轮次不许显示成成功(self) -> None:
        """一轮跑完却没有任何账号结果（例如历史条目被清理掉了）→「没有账号结果」。

        以前这里会显示绿色的「成功」，还会补一句猜出来的"账号凭据可能未配置"。
        """
        row = store.normalize_run(
            {
                "run_id": "20261001-071826-abcd",
                "started_at": "2026-10-01T07:18:26+08:00",
                "finished_at": "2026-10-01T07:18:28+08:00",
                "dry_run": "full",
                "source": "cli",
                "accounts": [],
                "note": "这一轮的账号条目已经从账本里清理掉了（那个账号已不在 .env 的 TASKS 里）",
            }
        )
        self.assertEqual(row.status, "empty")
        self.assertEqual(row.status_text, "没有账号结果")
        self.assertNotIn("账号凭据可能未配置", row.note, "不许替用户猜原因")
        self.assertIn("已经从账本里清理掉了", row.note, "账本自己写的话要留着")
        self.assertEqual(viewmodel.run_row(row)["chip"], "warning")

    def test_账号全被门禁拦下也不算成功(self) -> None:
        """每个账号都被跳过（登录失效 / 掉登录 / 超时），一条都没尝试过 →「账号全被跳过」。"""
        blocked = store.normalize_run(
            {
                "run_id": "r-blocked",
                "started_at": "2026-10-01T07:40:23+08:00",
                "dry_run": "off",
                "source": "timer",
                "accounts": [
                    {
                        "username": "账号甲",
                        "unique_id": "10001",
                        "status": "skipped",
                        "gate": "EXPIRED",
                        "targets": ["好友a"],
                        "entries": [],
                    }
                ],
            }
        )
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.status_text, "账号全被跳过")
        self.assertEqual(viewmodel.run_row(blocked)["chip"], "warning")

        # 只要有一个账号真的跑了，这一轮就还是「成功」——别把话说反了
        mixed = store.normalize_run(
            {
                "run_id": "r-mixed",
                "started_at": "2026-10-01T07:41:00+08:00",
                "accounts": [
                    {"username": "账号甲", "unique_id": "10001", "status": "skipped", "gate": "EXPIRED"},
                    {
                        "username": "账号乙",
                        "unique_id": "10002",
                        "status": "ok",
                        "sent_ok": 1,
                        "entries": [{"target": "好友a", "ok": True, "level": "strong"}],
                    },
                ],
            }
        )
        self.assertEqual(mixed.status, "ok")
        self.assertEqual(viewmodel.run_row(mixed)["chip"], "success")


CONFIG_ENV_TEXT = (
    "# 测试用 .env：注释、空行、变量顺序都要原样留着\n"
    "WEBUI_PORT=8787\n"
    "PROXY_ADDRESS=http://user:secret-pass@127.0.0.1:1080\n"
    "LOG_LEVEL=Debug\n"
    "MESSAGE_TEMPLATE=[盖瑞]今日火花[加一]\\n—— 每日一言 ——\\n[API]\n"
    'HITOKOTO_TYPES=["文学","影视"]\n'
    "SEND_INTERVAL_MIN_SECONDS=1\n"
    "SEND_INTERVAL_MAX_SECONDS=3\n"
    "BROWSER_ACTION_TIMEOUT=120\n"
    "IM_SCAN_TIMEOUT=120\n"
    "IM_READY_TIMEOUT=120\n"
    "FRIEND_LIST_WAIT_TIME=3\n"
    "IM_MAX_STEPS=200\n"
    "TASK_RETRY_TIMES=3\n"
    "LIMIT_GUARD_ENABLED=true\n"
    "LIMIT_COOLDOWN_MINUTES=30\n"
    "QUIET_WINDOW=20:20-21:00\n"
    "\n"
    "# 账号区：保存运行配置时，下面两行一个字节都不许动\n"
    f'TASKS=[{{"username":"{ACCOUNT_NAME}","unique_id":"{UID}","fingerprint":12345,'
    f'"targets":["{FRIEND_MAIN}"]}}]\n'
    f'COOKIES_{UID}=[{{"name":"sessionid","value":"SUPER-SECRET-COOKIE-VALUE",'
    '"domain":".douyin.com"}]\n'
)
# 两个"父类会重读这份 .env"的坑，别踩：
#   * `PROXY_ADDRESS` 不能省 —— build_context() 经 store.refresh_env() 重读后，
#     父类的 test_配置展示做了脱敏且不提供写入口 会从「已脱敏」变成「未配置」。
#   * `DRY_RUN` 不要写进去 —— 父类的 test_触发会起后台线程且不导入_cloakbrowser
#     会断言跑完以后 os.environ 里没有 DRY_RUN（这份 .env 会被 load_dotenv 灌进环境）。

#: 这份 fixture 会写进 os.environ（`envfile.reload()` → `load_dotenv`），用完要还回去
CONFIG_ENV_KEYS = (
    "DRY_RUN",
    "LOG_LEVEL",
    "MESSAGE_TEMPLATE",
    "HITOKOTO_TYPES",
    "SEND_INTERVAL_MIN_SECONDS",
    "SEND_INTERVAL_MAX_SECONDS",
    "BROWSER_ACTION_TIMEOUT",
    "IM_SCAN_TIMEOUT",
    "IM_READY_TIMEOUT",
    "FRIEND_LIST_WAIT_TIME",
    "IM_MAX_STEPS",
    "TASK_RETRY_TIMES",
    "LIMIT_GUARD_ENABLED",
    "LIMIT_COOLDOWN_MINUTES",
    "QUIET_WINDOW",
    "TASKS",
    "COOKIES_12345678905",
)


class ConfigEditorWebUITestCase(WebUITestCase):
    """「运行配置」能在面板上改，但只改白名单里的键。

    真机背景（2026-10-01，用户原话：）「我希望运行配置能在webui改动」——
    以前这一块是只读展示，改一个超时都得 ssh 上服务器编辑 `.env` 再重启。

    这里钉住三件事：值来自 `.env` 原文（不是加工过的展示值）、坏值整批不写、
    账号区（`TASKS` / `COOKIES_*`）与注释永远不会被这个表单碰到。
    """

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(CONFIG_ENV_TEXT, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self._saved_keys = {key: os.environ.get(key) for key in CONFIG_ENV_KEYS}
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file
        for key, value in self._saved_keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------------------ 脚手架

    def env_text(self) -> str:
        return self.env.read_text(encoding="utf-8")

    def backups(self) -> List[Path]:
        return sorted(self.tmp.glob(".env.bak-*"))

    def post_config(self, client: TestClient, data: Dict[str, str], **kwargs: Any):
        kwargs.setdefault("follow_redirects", False)
        return client.post("/ops/config/save", data=data, **kwargs)

    def login_client(self, client: TestClient) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.login(client)

    def account_lines(self, text: str) -> List[str]:
        return [line for line in text.splitlines() if line.startswith(("TASKS=", "COOKIES_"))]

    # ------------------------------------------------------------ 表单内容

    def test_表单里的键全都来自白名单(self) -> None:
        rows = settings.form_state(envfile.read_map())
        keys = [row["key"] for row in rows]
        self.assertEqual(keys, list(settings.BY_KEY), "白名单顺序就是面板顺序")
        for key in keys:
            self.assertFalse(key.startswith("COOKIES_"), "账号类键不在这里")
        self.assertNotIn("TASKS", keys, "TASKS 有自己的一套入口与校验")

    def test_表单显示的是_env_原文而不是加工过的展示值(self) -> None:
        rows = {row["key"]: row for row in settings.form_state(envfile.read_map())}
        # store.config_snapshot() 会把它写成 "120000 毫秒"，表单里必须是能直接写回去的 "120"
        self.assertEqual(rows["BROWSER_ACTION_TIMEOUT"]["value"], "120")
        self.assertEqual(rows["LIMIT_GUARD_ENABLED"]["value"], "true")
        self.assertEqual(rows["QUIET_WINDOW"]["value"], "20:20-21:00")
        self.assertTrue(rows["WEBUI_TZ"]["missing"], "fixture 里没有这一项 → 提示用默认值")
        self.assertEqual(rows["WEBUI_TZ"]["value"], "")

    def test_认不出的下拉值也要出现在下拉框里(self) -> None:
        """否则浏览器默认选第一项，用户点一次保存就把一个坏值悄悄换成别的。"""
        self.env.write_text(
            CONFIG_ENV_TEXT.replace("LOG_LEVEL=Debug", "LOG_LEVEL=verbose"), encoding="utf-8"
        )
        rows = {row["key"]: row for row in settings.form_state(envfile.read_map())}
        self.assertEqual(rows["LOG_LEVEL"]["value"], "verbose")
        self.assertEqual(rows["LOG_LEVEL"]["options"][0]["value"], "verbose")
        self.assertTrue(rows["LOG_LEVEL"]["bad"])
        self.assertEqual(
            [item["key"] for item in settings.problems(envfile.read_map())], ["LOG_LEVEL"]
        )

    def test_env_里没有的下拉项不动它就一个字节都不写(self) -> None:
        """fixture 的 `.env` 里没有 `DRY_RUN`。

        浏览器遇到"值不在选项里"会默认选中第一项，用户不动它直接保存，就会把一个
        从来没配过的键写成 `DRY_RUN=off` —— 面板不该替用户做这个决定。
        """
        rows = {row["key"]: row for row in settings.form_state(envfile.read_map())}
        self.assertTrue(rows["DRY_RUN"]["missing"])
        self.assertEqual(rows["DRY_RUN"]["value"], "")
        self.assertEqual(rows["DRY_RUN"]["options"][0]["value"], "", "空值占位项在最前面")
        self.assertIn("用默认值 off", rows["DRY_RUN"]["options"][0]["label"])
        self.assertEqual(
            [option["value"] for option in rows["DRY_RUN"]["options"]], ["", "off", "select", "full"]
        )
        before = self.env_text()

        self.assertEqual(settings.collect({"DRY_RUN": ""}, envfile.read_map()), {}, "没动过")
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            # 浏览器提交的就是占位项的值 ""，等价于"用户没动这个下拉框"
            response = self.post_config(
                client, {"cfg_DRY_RUN": ""}, headers={"Accept": "text/html"}
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("没有变化", unquote(response.headers["location"]))
        self.assertEqual(self.env_text(), before, "什么都没写，连备份都不该有")
        self.assertEqual(self.backups(), [])
        self.assertNotIn("DRY_RUN", envfile.read_map())

    def test_env_里没有的下拉项真选了才写(self) -> None:
        result = settings.apply({"DRY_RUN": "full"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], {"DRY_RUN": "full"})
        self.assertIn("DRY_RUN=full", self.env_text())

    def test_登录保持时长能在面板上改(self) -> None:
        """`WEBUI_SESSION_MAX_AGE` 是后加进白名单的键（默认 30 天，上限 400 天）。"""
        rows = {row["key"]: row for row in settings.form_state(envfile.read_map())}
        self.assertIn("WEBUI_SESSION_MAX_AGE", rows)
        self.assertTrue(rows["WEBUI_SESSION_MAX_AGE"]["missing"], "fixture 里没有 → 用默认值")

        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client, {"cfg_WEBUI_SESSION_MAX_AGE": "604800"}, headers={"Accept": "text/html"}
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("已保存 1 项", unquote(response.headers["location"]))
        self.assertIn("WEBUI_SESSION_MAX_AGE=604800", self.env_text())

        # 非法值 / 越界值一个字节都不写
        before = self.env_text()
        for bad in ("", "abc", "0", "-1", "59", str(config.MAX_SESSION_MAX_AGE + 1)):
            with self.subTest(bad=bad):
                result = settings.apply({"WEBUI_SESSION_MAX_AGE": bad})
                self.assertFalse(result["ok"], bad)
        self.assertEqual(self.env_text(), before, "拒绝时不许动文件")

    def test_代理口令在表单里是打码的(self) -> None:
        self.env.write_text(
            CONFIG_ENV_TEXT + "PROXY_ADDRESS=http://user:secret-pass@127.0.0.1:1080\n",
            encoding="utf-8",
        )
        rows = {row["key"]: row for row in settings.form_state(envfile.read_map())}
        self.assertEqual(rows["PROXY_ADDRESS"]["value"], "http://user:***@127.0.0.1:1080")
        self.assertTrue(rows["PROXY_ADDRESS"]["masked"])

    # ------------------------------------------------------------ 保存

    def test_保存只动改过的行_注释与账号一个字节不动(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client,
                {"cfg_LOG_LEVEL": "Info", "cfg_TASK_RETRY_TIMES": "5", "cfg_TASKS": "[]"},
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 303)
        location = unquote(response.headers["location"])
        self.assertIn("config-panel", location, "回到面板那一段")
        self.assertIn("已保存 2 项", location)
        after = self.env_text()
        self.assertIn("LOG_LEVEL=Info", after)
        self.assertIn("TASK_RETRY_TIMES=5", after)
        self.assertIn("# 账号区", after, "注释还在")
        self.assertEqual(self.account_lines(after), self.account_lines(CONFIG_ENV_TEXT))
        self.assertNotIn("cfg_", after, "表单字段名不会漏进 .env")
        self.assertEqual(len(self.backups()), 1, "改之前先备份")

    def test_面板下次渲染就能看到新值(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            self.post_config(
                client,
                {"cfg_LOG_LEVEL": "Info"},
                headers={"Accept": "text/html"},
            )
            page = client.get("/", headers={"Accept": "text/html"})
        self.assertIn('<option value="Info" selected>', page.text)

    def test_坏值整批不写(self) -> None:
        before = self.env_text()
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client,
                {"cfg_LOG_LEVEL": "Info", "cfg_IM_MAX_STEPS": "3"},
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 303)
        location = unquote(response.headers["location"])
        self.assertIn("IM 最大步数", location, "错误里要用中文标签说清是哪一项")
        self.assertIn("#config-panel", location, "回落到配置区，不用自己找")
        self.assertEqual(self.env_text(), before, "有一项不合法就整批不写")
        self.assertEqual(self.backups(), [], "不合法连备份都不产生")

    def test_json_接口能说出是哪一项不合法(self) -> None:
        before = self.env_text()
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/config/save?format=json",
                data={"cfg_QUIET_WINDOW": "25:00-26:00"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual([item["key"] for item in payload["errors"]], ["QUIET_WINDOW"])
        self.assertEqual(payload["errors"][0]["label"], "禁跑时间窗")
        self.assertIn("HH:MM-HH:MM", payload["error"])
        self.assertEqual(self.env_text(), before)

    def test_默认发送时间会归一成两位(self) -> None:
        """`8:00` 要存成 `08:00` —— 面板与账号卡两处都按 HH:MM 渲染。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client, {"cfg_TASK_SCHEDULE_TIMES": "8:00, 21:30"},
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("TASK_SCHEDULE_TIMES=08:00,21:30", self.env_text())

    def test_默认发送时间写错就整批不写(self) -> None:
        before = self.env_text()
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/config/save?format=json",
                data={"cfg_TASK_SCHEDULE_TIMES": "8点"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual([item["key"] for item in payload["errors"]], ["TASK_SCHEDULE_TIMES"])
        self.assertEqual(payload["errors"][0]["label"], "默认发送时间")
        self.assertIn("HH:MM", payload["error"])
        self.assertEqual(self.env_text(), before, "有一项不合法就整批不写")

    def test_发送时间抖动要落成整数(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client, {"cfg_SCHEDULE_JITTER_MINUTES": "15.0"},
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("SCHEDULE_JITTER_MINUTES=15", self.env_text())

    def test_没改动的保存什么都不写(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client,
                {"cfg_LOG_LEVEL": "Debug", "cfg_IM_SCAN_TIMEOUT": "120", "cfg_QUIET_WINDOW": "20:20-21:00"},
                headers={"Accept": "text/html"},
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("没有变化", unquote(response.headers["location"]))
        self.assertEqual(self.env_text(), CONFIG_ENV_TEXT)
        self.assertEqual(self.backups(), [])

    def test_下拉框大小写不同也算没改(self) -> None:
        """工具读日志级别会小写化，`debug` 与 `Debug` 是同一个意思 —— 不该被顺手改写。"""
        self.env.write_text(
            CONFIG_ENV_TEXT.replace("LOG_LEVEL=Debug", "LOG_LEVEL=debug"), encoding="utf-8"
        )
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            self.post_config(client, {"cfg_LOG_LEVEL": "debug"}, headers={"Accept": "text/html"})
        self.assertIn("LOG_LEVEL=debug", self.env_text())
        self.assertNotIn("LOG_LEVEL=Debug", self.env_text())
        self.assertEqual(self.backups(), [])

    def test_跨站提交不许改配置(self) -> None:
        before = self.env_text()
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = self.post_config(
                client,
                {"cfg_DRY_RUN": "full"},
                headers={"Accept": "text/html", "Origin": "http://evil.example"},
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.env_text(), before)

    def test_未登录既写不了也拿不到_json(self) -> None:
        before = self.env_text()
        with self.make_client() as client:
            html = self.post_config(
                client, {"cfg_DRY_RUN": "full"}, headers={"Accept": "text/html"}
            )
            payload = client.post("/ops/config/save?format=json", data={"cfg_DRY_RUN": "full"})
        self.assertEqual(html.status_code, 303)
        self.assertIn("/login", html.headers["location"])
        self.assertEqual(payload.status_code, 401)
        self.assertEqual(self.env_text(), before)

    def test_间隔上下限跨字段检查(self) -> None:
        before = self.env_text()
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            response = client.post(
                "/ops/config/save?format=json",
                data={"cfg_SEND_INTERVAL_MIN_SECONDS": "30", "cfg_SEND_INTERVAL_MAX_SECONDS": "5"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能小于下限", response.json()["error"])
        self.assertEqual(self.env_text(), before)

    def test_白名单外的键连收都不收(self) -> None:
        env_map = envfile.read_map()
        prepared = settings.collect(
            {
                "TASKS": "[]",
                "COOKIES_12345678905": "[]",
                "RUN_LEDGER_FILE": "/tmp/x.jsonl",
                "DRY_RUN": "full",
            },
            env_map,
        )
        self.assertEqual(prepared, {"DRY_RUN": "full"})

    def test_重读失败时不能说已重读(self) -> None:
        """文件写成功 ≠ 本进程读到新值 —— 这两件事必须分开说。

        `store.refresh_env()` 把异常吞掉返回 False（读权限、编码、并发改坏），
        而 `.env` 其实已经写进去了。旧代码丢掉返回值，页面照旧说"并重读 .env"，
        用户以为新配置已经在跑，实际还在用内存里的旧值，直到重启面板 ——
        而且会以为是"保存没生效"去反复点保存。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            with mock.patch.object(store, "refresh_env", return_value=False):
                response = self.post_config(
                    client, {"cfg_LOG_LEVEL": "Info"}, headers={"Accept": "text/html"}
                )
        self.assertEqual(response.status_code, 303, "文件确实写成功了，不能报成保存失败")
        location = unquote(response.headers["location"])
        self.assertIn("重读失败", location)
        self.assertIn("重启面板后生效", location)
        self.assertNotIn("并重读 .env", location)
        self.assertIn("LOG_LEVEL=Info", self.env_text(), "该写的还是要写")

    def test_重读成功时照旧说已重读(self) -> None:
        """对照组：`refresh_env()` 返回 True 时文案不能跟着变。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            with mock.patch.object(store, "refresh_env", return_value=True):
                response = self.post_config(
                    client, {"cfg_LOG_LEVEL": "Info"}, headers={"Accept": "text/html"}
                )
        location = unquote(response.headers["location"])
        self.assertIn("并重读 .env", location)
        self.assertNotIn("重读失败", location)

    # ------------------------------------------------------------ 页面

    def test_首页把配置表单渲染出来(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/", headers={"Accept": "text/html"})
        self.assertIn("data-config-form", page.text)
        self.assertIn('name="cfg_DRY_RUN"', page.text)
        self.assertIn('data-config-input="TASK_RETRY_TIMES"', page.text)
        self.assertIn("data-config-submit", page.text)
        self.assertIn("项可改", page.text)
        self.assertNotIn("只读展示；改配置请改", page.text)

    def test_env_里现在就坏的值会在面板顶上标出来(self) -> None:
        self.env.write_text(
            CONFIG_ENV_TEXT.replace("IM_MAX_STEPS=200", "IM_MAX_STEPS=1"), encoding="utf-8"
        )
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/", headers={"Accept": "text/html"})
        self.assertIn("data-config-problems", page.text)
        self.assertIn("data-config-bad", page.text)
        self.assertIn("IM_MAX_STEPS", page.text)

    def test_消息模板预览是渲染后的文本(self) -> None:
        rendered = viewmodel.render_message("[盖瑞]今日火花[加一]\\n—— [API]")
        self.assertEqual(rendered.splitlines(), ["[盖瑞]今日火花[加一]", "—— [API]"])
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        with self.make_client() as client:
            self.login(client)
            page = client.get("/", headers={"Accept": "text/html"})
        self.assertIn("data-message-preview", page.text)
        self.assertIn("data-message-raw", page.text)


class PasswordPanelWebUITestCase(WebUITestCase):
    """面板的访问口令能在面板里自己改（用户原话 m09112：「我不希望是用密钥登录，

    你把它改成用密码登录吧，其中密码我可以在webui自行设定」）。

    这里钉住四件事：改口令必须先证明是本人（知道旧口令）、写进 `.env` 后立刻生效、
    **两个键永远只留一种**（登录校验是哈希优先，两个都留着就会出现"改过口令却登不上"）、
    以及改完口令所有旧会话立刻作废。还有一条：面板永远不回显口令本身。
    """

    NEW_PASSWORD = "新口令-xyz789"
    ODD_PASSWORD = "p@ss#word$1 带空格"
    FIRST_PASSWORD = "第一版口令-abc"

    def setUp(self) -> None:
        super().setUp()
        self.env = self.tmp / ".env"
        self.env.write_text(CONFIG_ENV_TEXT, encoding="utf-8")
        self._saved_env_file = os.environ.get("ENV_FILE")
        os.environ["ENV_FILE"] = str(self.env)
        self._saved_keys = {key: os.environ.get(key) for key in CONFIG_ENV_KEYS}
        self.addCleanup(self._restore)
        # 注意：**不在 setUp 里设口令**。基类有一批"没口令时该怎么办"的用例
        # （拒绝裸奔、回环放行但警告），一个字都不能提前塞进环境里。

    def _restore(self) -> None:
        if self._saved_env_file is None:
            os.environ.pop("ENV_FILE", None)
        else:
            os.environ["ENV_FILE"] = self._saved_env_file
        for key, value in self._saved_keys.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------------------ 脚手架

    def login(self, client: TestClient) -> None:
        """本类的登录统一把口令放在**进程环境**里（不写 `.env`）。

        这样"面板里改口令"才是真的从"`.env` 里一个口令都没有"开始 —— 也正是
        `password_mode()` 要报告的那种真实状态。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        super().login(client)

    def env_text(self) -> str:
        return self.env.read_text(encoding="utf-8")

    def backups(self) -> List[Path]:
        return sorted(self.tmp.glob(".env.bak-*"))

    def post_password(self, client: TestClient, data: Dict[str, str], **kwargs: Any):
        kwargs.setdefault("follow_redirects", False)
        return client.post("/ops/password", data=data, **kwargs)

    def change(self, client: TestClient, current: str, fresh: str, **extra: str):
        data = {"current": current, "new": fresh, "confirm": fresh, "store": "plain"}
        data.update(extra)
        return self.post_password(client, data, headers={"Accept": "text/html"})

    # ------------------------------------------------------------ 单测：写盘

    def test_写口令会同时清掉另一种形式(self) -> None:
        """只写一个键，另一个的旧值还躺在 `.env` 里 —— 而校验是哈希优先。"""
        result = envfile.set_webui_password(self.FIRST_PASSWORD, store=envfile.PASSWORD_MODE_PLAIN)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            sorted(result["changed"]),
            ["WEBUI_PASSWORD", "WEBUI_PASSWORD_HASH"],
            "两个键一起写：不用的那个留一行空值，顺手把环境里可能残留的旧口令清成空串",
        )
        values = envfile.read_map()
        self.assertEqual(values["WEBUI_PASSWORD"], self.FIRST_PASSWORD)
        self.assertEqual(values["WEBUI_PASSWORD_HASH"], "")
        self.assertEqual(envfile.password_mode(), envfile.PASSWORD_MODE_PLAIN)

        result = envfile.set_webui_password("第二版口令-xyz", store=envfile.PASSWORD_MODE_HASH)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            sorted(result["changed"]), ["WEBUI_PASSWORD", "WEBUI_PASSWORD_HASH"], "两个一起写"
        )
        values = envfile.read_map()
        self.assertEqual(values["WEBUI_PASSWORD"], "", "明文必须清掉")
        self.assertTrue(values["WEBUI_PASSWORD_HASH"].startswith("pbkdf2_sha256$"))
        self.assertEqual(envfile.password_mode(), envfile.PASSWORD_MODE_HASH)
        self.assertEqual(self.env_text().count("WEBUI_PASSWORD="), 1, "不能写出第二行")
        self.assertEqual(self.env_text().count("WEBUI_PASSWORD_HASH="), 1)

    def test_口令里的井号空格美元符号都要原样活下来(self) -> None:
        """不加引号写出去，dotenv 会把 `#` 之后当注释、把 `$x` 当变量展开。"""
        self.assertTrue(envfile.set_webui_password(self.ODD_PASSWORD)["ok"])
        self.assertIn("'" + self.ODD_PASSWORD + "'", self.env_text(), "带引号写")
        self.assertEqual(envfile.read_map()["WEBUI_PASSWORD"], self.ODD_PASSWORD)
        # 真的走一遍读入环境变量的路径（登录校验读的是 os.environ）
        os.environ.pop("WEBUI_PASSWORD", None)
        from utils.config import reload_env  # noqa: PLC0415

        reload_env(str(self.env))
        self.assertEqual(config.password_plain(), self.ODD_PASSWORD)
        self.assertTrue(password.verify_plain(self.ODD_PASSWORD, config.password_plain()))

    def test_不合格的口令一个字节都不写(self) -> None:
        before = self.env_text()
        for bad, hint in (
            ("", "空"),
            ("短口令", "8"),
            ("a" * 200, "128"),
            ("换行\n口令", "换行"),
            ("带'单引号", "单引号"),
        ):
            result = envfile.set_webui_password(bad)
            self.assertFalse(result["ok"], bad)
            self.assertIn(hint, result["error"], bad)
        self.assertEqual(self.env_text(), before)
        self.assertEqual(self.backups(), [])

    def test_没设过口令时也能设第一个口令(self) -> None:
        os.environ.pop("WEBUI_PASSWORD", None)
        self.assertEqual(envfile.password_mode(), "")
        self.assertTrue(envfile.set_webui_password(self.FIRST_PASSWORD).get("ok"))
        self.assertEqual(envfile.password_mode(), envfile.PASSWORD_MODE_PLAIN)

    def test_口令没变就是一个字节都不写(self) -> None:
        self.assertTrue(envfile.set_webui_password(self.FIRST_PASSWORD)["ok"])
        before = self.env_text()
        backups_before = self.backups()
        result = envfile.set_webui_password(self.FIRST_PASSWORD)
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(self.env_text(), before)
        self.assertEqual(self.backups(), backups_before, "没变化就不该产生备份")

    # ------------------------------------------------------------ 表单流程

    def test_在面板里改口令_旧会话失效_新口令能登(self) -> None:
        with self.make_client() as client:
            self.login(client)
            cookie = client.cookies.get(config.session_cookie_name())
            response = self.change(client, PASSWORD, self.NEW_PASSWORD)
            self.assertEqual(response.status_code, 303)
            location = unquote(response.headers["location"])
            self.assertTrue(location.startswith("/login?"), location)
            self.assertIn("口令已更新", location)

            # 这个会话（连同任何别的设备）已经作废
            self.assertIsNone(auth.read_token(cookie))
            after = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
            self.assertEqual(after.status_code, 303)
            self.assertIn("/login", after.headers["location"])

            # 旧口令进不去，新口令能进
            wrong = client.post(
                "/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False
            )
            self.assertEqual(wrong.status_code, 401)
            fresh = client.post(
                "/login", data={"password": self.NEW_PASSWORD, "next": "/"}, follow_redirects=False
            )
            self.assertIn(fresh.status_code, (302, 303))
            self.assertIsNotNone(client.get("/", headers={"Accept": "text/html"}).status_code == 200)

        self.assertEqual(envfile.read_map()["WEBUI_PASSWORD"], self.NEW_PASSWORD)
        self.assertEqual(len(self.backups()), 1, "改前必须备份一份")
        self.assertIn("WEBUI_PASSWORD=", self.env_text())

    def test_当前口令不对就不写盘(self) -> None:
        before = self.env_text()
        with self.make_client() as client:
            self.login(client)
            response = self.change(client, "冒充者的口令", self.NEW_PASSWORD)
            self.assertEqual(response.status_code, 303)
            location = unquote(response.headers["location"])
            self.assertIn("当前口令不正确", location)
            self.assertIn("#password-panel", location)
        self.assertEqual(self.env_text(), before)
        self.assertEqual(self.backups(), [])
        self.assertTrue(_check_password_via_app(self.NEW_PASSWORD) is False)

    def test_两次输入不一致就不写盘(self) -> None:
        before = self.env_text()
        with self.make_client() as client:
            self.login(client)
            response = self.post_password(
                client,
                {
                    "current": PASSWORD,
                    "new": self.NEW_PASSWORD,
                    "confirm": self.NEW_PASSWORD + "打错了",
                    "store": "plain",
                },
                headers={"Accept": "text/html"},
            )
        self.assertIn("两次输入的新口令不一样", unquote(response.headers["location"]))
        self.assertEqual(self.env_text(), before)

    def test_不合格的新口令会被拒绝并说明原因(self) -> None:
        before = self.env_text()
        with self.make_client() as client:
            self.login(client)
            response = self.change(client, PASSWORD, "短")
        self.assertIn("8", unquote(response.headers["location"]))
        self.assertEqual(self.env_text(), before)
        self.assertEqual(self.backups(), [])

    def test_存成哈希_新口令照样能登(self) -> None:
        with self.make_client() as client:
            self.login(client)
            response = self.change(client, PASSWORD, self.NEW_PASSWORD, store="hash")
            self.assertEqual(response.status_code, 303)
        values = envfile.read_map()
        self.assertEqual(values["WEBUI_PASSWORD"], "", "明文必须清掉")
        self.assertTrue(values["WEBUI_PASSWORD_HASH"].startswith("pbkdf2_sha256$"))
        self.assertTrue(password.verify_hash(self.NEW_PASSWORD, values["WEBUI_PASSWORD_HASH"]))
        with self.make_client() as client:
            ok = client.post(
                "/login", data={"password": self.NEW_PASSWORD, "next": "/"}, follow_redirects=False
            )
            self.assertIn(ok.status_code, (302, 303))

    def test_未登录不能改口令(self) -> None:
        before = self.env_text()
        with self.make_client() as client:
            page = self.post_password(
                client,
                {"current": PASSWORD, "new": self.NEW_PASSWORD, "confirm": self.NEW_PASSWORD},
                headers={"Accept": "text/html"},
            )
            self.assertEqual(page.status_code, 303)
            self.assertIn("/login", page.headers["location"])
            api = self.post_password(
                client,
                {"current": PASSWORD, "new": self.NEW_PASSWORD, "confirm": self.NEW_PASSWORD},
                headers={"Accept": "application/json"},
            )
            self.assertEqual(api.status_code, 401)
        self.assertEqual(self.env_text(), before)

    def test_跨站表单改不了口令(self) -> None:
        before = self.env_text()
        with self.make_client() as client:
            self.login(client)
            response = self.post_password(
                client,
                {"current": PASSWORD, "new": self.NEW_PASSWORD, "confirm": self.NEW_PASSWORD},
                headers={"Accept": "application/json", "Origin": "http://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.env_text(), before)
        self.assertEqual(self.backups(), [])

    def test_保存方式填了怪值就按明文写(self) -> None:
        with self.make_client() as client:
            self.login(client)
            response = self.change(client, PASSWORD, self.NEW_PASSWORD, store="../../etc/passwd")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(envfile.password_mode(), envfile.PASSWORD_MODE_PLAIN)

    # ------------------------------------------------------------ 页面

    def test_首页有口令面板且不回显任何凭据(self) -> None:
        with self.make_client() as client:
            self.login(client)
            page = client.get("/", headers={"Accept": "text/html"})
        self.assertIn("data-password-panel", page.text)
        self.assertIn('action="/ops/password"', page.text)
        self.assertIn('data-password-input="current"', page.text)
        self.assertIn('data-password-input="new"', page.text)
        self.assertIn('data-password-input="confirm"', page.text)
        self.assertIn('data-password-input="store"', page.text)
        self.assertNotIn(PASSWORD, page.text, "面板不许把口令渲染出来")
        self.assertNotIn("pbkdf2_sha256$", page.text, "哈希也不许渲染")

    def test_状态那一行说的是真的(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        state = webui_app.password_panel_state()
        self.assertEqual(state["mode"], "", ".env 里没有这一项 → 形式是空的")
        self.assertEqual(state["mode_label"], "")
        self.assertTrue(
            state["from_environment"], "口令只在进程环境里（systemd 的 Environment= 就是这么干的）"
        )
        self.assertTrue(state["has_password"], "进程环境里有口令，也算设置了")
        self.assertFalse(state["inline"], "文件里没有明文，就谈不上明文形式")

        os.environ.pop("WEBUI_PASSWORD", None)
        self.assertTrue(envfile.set_webui_password(self.FIRST_PASSWORD)["ok"])
        state = webui_app.password_panel_state()
        self.assertEqual(state["mode"], envfile.PASSWORD_MODE_PLAIN)
        self.assertTrue(state["mode_label"])
        self.assertFalse(state["from_environment"], "文件里有，就不算来自环境")
        self.assertTrue(state["inline"])
        self.assertTrue(state["has_password"])

        for key in ("mode", "mode_label", "has_password", "inline", "session_hours",
                    "session_seconds", "session_text"):
            self.assertIn(key, state)
        self.assertEqual(state["session_seconds"], config.DEFAULT_SESSION_MAX_AGE)
        self.assertEqual(state["session_text"], "30 天", "面板上要说人话，不写秒数")
        self.assertFalse(
            any("password" in key.lower() and key != "has_password" for key in state),
            "口令面板的状态里不许出现任何凭据字段",
        )

    def test_面板上显示的是人话时长(self) -> None:
        template = (REPO_ROOT / "webui" / "templates" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("session_text", template)
        self.assertIn("session_seconds", template)
        self.assertIn("登录保持", template)

    def test_口令表单不会被总览轮询改写(self) -> None:
        """总览轮询只认 `data-overview-*`；表单里挂的是 `data-password-*`。"""
        app_js = (REPO_ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("data-password", app_js, "轮询不该碰口令表单")
        template = (REPO_ROOT / "webui" / "templates" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("data-password-form", template)
        self.assertIn('id="password-panel"', template)
        # 侧栏那个入口靠 hash 展开折叠段（app.js 的 reveal()）
        base = (REPO_ROOT / "webui" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("/#password-panel", base)

    def test_登录页指向面板里的改口令入口(self) -> None:
        login_page = (REPO_ROOT / "webui" / "templates" / "login.html").read_text(encoding="utf-8")
        self.assertIn("访问口令", login_page)
        self.assertIn("{% if saved %}", login_page, "改完口令跳回登录页要能显示那句提示")
        with self.make_client() as client:
            page = client.get("/login?saved=口令已更新")
        self.assertIn("口令已更新", page.text)


def _check_password_via_app(candidate: str) -> bool:
    """直接问 app 的校验函数（比走一遍登录更贴近"面板里到底认哪个口令"）。"""
    return bool(webui_app._check_password(candidate))


class FrontendAgreementTests(unittest.TestCase):
    """前端与服务端必须说同一句话 —— 总览轮询每 10 秒会覆盖一次页面上的文字。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dashboard = (REPO_ROOT / "webui" / "templates" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        cls.runs = (REPO_ROOT / "webui" / "templates" / "runs.html").read_text(encoding="utf-8")
        cls.logs = (REPO_ROOT / "webui" / "templates" / "logs.html").read_text(encoding="utf-8")
        cls.js = (REPO_ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")

    def test_运行记录页的本轮数字不会被总览轮询改写(self) -> None:
        """`data-overview-value` 是"今天总览"的挂点，前端每 10 秒往里写一次。

        运行记录页那几个数字说的是"选中的那一轮"，拿总览数字去覆盖它，
        页面就会一边写「本轮结论：… 2026-10-01 07:52:21 那一轮」一边显示今天的合计。
        """

        self.assertNotIn("data-overview-value", self.runs)
        self.assertIn("data-run-value", self.runs)
        for key in ("confirmed", "pageEcho", "attention", "pending"):
            self.assertIn(f'data-run-value="{key}"', self.runs)
        runner = self.runs.split("{% block", 1)[-1]
        self.assertNotIn("data-overview-value", runner)

    def test_配置表单不会被总览轮询改写(self) -> None:
        """配置表单里装的是"用户正在编辑的值"，总览轮询绝不能碰它。

        `data-overview-value` 是服务端渲染 + 前端每 10 秒重写的挂点；
        表单字段用的是 `data-config-input`，两套名字必须井水不犯河水 ——
        否则用户正在改超时，10 秒后轮询把输入框里的内容换成 `.env` 里的旧值。
        """

        # 从 `data-config-panel` 切，不要从 `data-config-form` 切 —— 后者会把
        # `<form action=...>` 标签本身切掉，`action` 断言就成了假通过。
        form = self.dashboard.split("data-config-panel", 1)[-1].split("</form>", 1)[0]
        self.assertIn("data-config-input", form)
        self.assertNotIn("data-overview-value", form)
        self.assertNotIn("data-overview-badge", form)
        # 前端不认识 data-config-input：配置只在提交时读一次，轮询不写它
        self.assertNotIn("data-config-input", self.js)
        # 提交按钮与表单动作不能被脚本改掉
        self.assertIn('action="/ops/config/save"', form)
        self.assertIn("method=\"post\"", form)

    def test_总览挂点只给首页用(self) -> None:
        self.assertIn("data-overview-value", self.dashboard)
        self.assertNotIn("data-overview-value", self.logs)
        self.assertIn("data-overview-root", self.dashboard)
        self.assertIn("data-overview-root", self.runs, "运行记录页仍要实时状态与倒计时")

    def _read(self, *parts: str) -> str:
        return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_空跑标记前后端用同一句话(self) -> None:
        template = self._read("webui", "templates", "dashboard.html")
        script = self._read("webui", "static", "app.js")
        self.assertIn("上一轮是空跑", template)
        self.assertIn("上一轮是空跑", script, "轮询会覆盖进度文案，这句必须一起写进 JS")

    def test_不会自动跑这句话前端也要认(self) -> None:
        template = self._read("webui", "templates", "dashboard.html")
        script = self._read("webui", "static", "app.js")
        self.assertIn("不会自动跑", template)
        self.assertIn("不会自动跑", script)
        self.assertIn("只能手动点「立即运行」", script)

    def test_前端配色用_stateCss_而不是_state(self) -> None:
        """`orphan` 这类状态没有自己的配色规则，前端直接写 state 会掉样式。"""
        script = self._read("webui", "static", "app.js")
        self.assertIn("account.stateCss || account.state", script)
        payload = viewmodel.overview_payload(
            viewmodel.build(
                records=[],
                accounts=[store.AccountSnapshot(username="甲", unique_id="1")],
                lock=None, rate_limit=None, cfg={}, schedules=[],
            )
        )
        self.assertIn("stateCss", payload["accounts"][0])
        self.assertIn("dryRun", payload["accounts"][0])
        self.assertIn("dryRunText", payload["accounts"][0])
        self.assertIn("armed", payload["schedule"])

    def test_空跑档位提交前必须去掉挂点前缀(self) -> None:
        """挂点是 `dry-off`/`dry-select`/`dry-full`（同一前缀还要配对说明面板），
        提交给服务端的必须是没有前缀的 off/select/full。

        回归（2026-10 审计发现 1）：原样提交挂点值 → 服务端 400 →「立即运行」点了没反应；
        而且 `const real = value === "off"` 恒假，真实发送档位下按钮仍是柔和色 + 写着「开始空跑」。
        """
        script = self._read("webui", "static", "app.js")
        self.assertIn('raw.replace(/^dry-/, "")', script)
        self.assertIn("input.value = level", script)
        self.assertIn('level === "off"', script)
        self.assertIn('data-segment-target="dry-{{ level }}"', self.dashboard)
        self.assertIn('id="dry-run-value"', self.dashboard)

    def test_首页今日进度条前后端都画(self) -> None:
        """「今天发了多少」这一格是标签 + 大数字 + 进度条 + 说明（不再用圆环）。

        服务端渲染两段宽度，前端每 10 秒改写同样的两段宽度；少一边就会出现
        「数字是新的、条子还停在 0%」这种自相矛盾的页面。
        """
        for hook in ("data-overview-bar-strong", "data-overview-bar-echo"):
            self.assertIn(hook, self.dashboard, f"服务端少了进度条挂点 {hook}")
            self.assertIn(hook, self.js, f"轮询没重画进度条 {hook}")
        self.assertIn("width:{{ summary.strong_pct }}%", self.dashboard)
        self.assertIn("width:{{ summary.weak_pct }}%", self.dashboard)
        # 宽度算法与 viewmodel.progress_pair 一致：弱段 = 已发送占比 − 强确认占比
        self.assertIn("const weakPct = sentPct - strongPct;", self.js)
        # 圆环已经拆掉，别再留下挂点或样式
        self.assertNotIn("progress-badge", self.dashboard)
        self.assertNotIn("data-overview-badge", self.js)
        self.assertNotIn("data-overview-badge", self.dashboard)

    def test_空跑药丸与两种说明都能被轮询重画(self) -> None:
        """issue #9：这两块原来由 `{% if %}` 决定在不在页面上。

        首屏打开时档位是 off → 之后在首页切到「输入后清空」跑一轮 → `summary.dry_run`
        变成 full，而药丸和「一条都没真发出去」都不出现（页面上根本没有可写的节点），
        页面于是安安静静地把排练讲成了发送。
        """
        for hook in ("data-overview-dry-run", "data-overview-dry-run-hint"):
            self.assertIn(hook, self.dashboard, f"模板少了挂点 {hook}")
            self.assertIn(hook, self.js, f"轮询没重画 {hook}")
        # 两种说明都常驻（各账号档位不一致 / 整页只有一个档位），靠 hidden 切显隐；
        # 档位文字单独挂一个槽，文案本身只写在模板里，轮询不另写一句。
        self.assertIn('data-overview-dry-run-hint="mixed"', self.dashboard)
        self.assertIn('data-overview-dry-run-hint="single"', self.dashboard)
        self.assertIn("data-overview-dry-run-text", self.dashboard)
        self.assertIn("data-overview-dry-run-text", self.js)
        self.assertIn("overviewDryRunHint", self.js)
        # 服务端必须把档位发出来（键名与 app.js 里的消费逐字一致）
        record = {
            "run_id": "20261018-090000",
            "dry_run": "full",
            "finished_at": "",
            "started_at": "",
            "accounts": [{
                "username": "甲", "unique_id": "1", "status": "ok",
                "sent_ok": 1, "sent_fail": 0, "entries": [],
                "missing": [], "select_failed": [], "targets": [],
            }],
        }
        payload = viewmodel.overview_payload(
            viewmodel.build(
                records=[record],
                accounts=[store.AccountSnapshot(username="甲", unique_id="1")],
                lock=None, rate_limit=None, cfg={}, schedules=[],
            )
        )
        self.assertEqual(payload["summary"]["dryRun"], "full")
        self.assertIn("dryRunText", payload["summary"])

    def test_顶部横幅由轮询重画(self) -> None:
        """issue #10：冷却 / 禁跑窗 / 运行中 / 运行锁 / 阻断 / 上一轮跳过都会**自己开始**。

        只在首屏渲染一遍，实际观感就是"跑到一半命中风控 → 页面顶部什么都不说"，
        而那条 `data-countdown-until` 倒计时也永远等不到它的横幅。
        """
        for hook in ("data-status-banner-list", "data-status-banner-tpl",
                     "data-status-banner-icon", "data-status-banner-text"):
            self.assertIn(hook, self.dashboard, f"首页少了挂点 {hook}")
            self.assertIn(hook, self.runs, f"运行记录页少了挂点 {hook}")
            self.assertIn(hook, self.js, f"轮询没重画 {hook}")
        self.assertIn("updateBanners", self.js)
        # 倒计时必须**每次**重新取节点：否则轮询新插进来的横幅没有倒计时
        self.assertNotIn('const nodes = [...document.querySelectorAll("[data-countdown-until]")]', self.js)
        self.assertIn('document.querySelectorAll("[data-countdown-until]").forEach', self.js)
        # 服务端把同一份横幅发出来（文案只有 viewmodel._banners() 一处）
        payload = viewmodel.overview_payload(
            viewmodel.build(
                records=[],
                accounts=[store.AccountSnapshot(username="甲", unique_id="1")],
                lock=None, rate_limit=None, cfg={}, schedules=[],
            )
        )
        self.assertIn("banners", payload)
        self.assertIsInstance(payload["banners"], list)

    def test_运行中禁用账号动作由轮询接管(self) -> None:
        """issue #11：`updateActions()` 认的三个挂点在 templates/ 下命中数是 0。

        它们从 `9c7a3a8`（同步进本仓那天）起就是死代码，而且不是"兼容旧写法"那一类
        （对比 `data-segment-group`：那个是有意保留的旧选择器）。实验证明它承诺的两件事
        一件都没发生：按钮上不会出现待办数字，运行期间也不会被禁用。这里删掉那半段，
        把真正要做的那件事（运行期间不许动账号）交给 `data-disable-while-running`。
        """
        # 查的是"选择器还在不在"，不是注释里提没提到它 —— 注释解释这段历史正是要留的。
        self.assertNotIn('querySelectorAll("[data-action-count', self.js, "死挂点必须删干净")
        self.assertNotIn("dataset.actionCountSource", self.js)
        self.assertNotIn("data-action-count", self.dashboard)
        self.assertNotIn("const updateActions", self.js, "死函数必须删干净")
        self.assertNotIn("updateActions(summary", self.js, "调用点也要一起删")
        self.assertIn("data-disable-while-running", self.dashboard)
        self.assertIn("data-disable-while-running", self.js)
        # 「扫码会话开着」是另一路禁用理由，也必须发给轮询 —— 否则 10 秒后会把
        # 服务端禁用的按钮重新点亮（首屏与轮询同源是这套前端的硬规矩）。
        self.assertIn("data-login-active", self.dashboard)
        self.assertIn("data-login-active", self.js)
        for action in ("/ops/login/start", "/ops/account/remove", "/ops/account/requeue"):
            block = self.dashboard.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]
            with self.subTest(action=action):
                self.assertIn("data-disable-while-running", block)
                self.assertIn("disabled", block, "首屏与轮询必须同源：运行态服务端也要渲染 disabled")


class TimerStateTests(unittest.TestCase):
    """`store.timer_state()` 必须反映 systemd 的**真配置**，不是 `.env` 里的一厢情愿。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dsf-timer-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def _write_unit(self, text: str, *, enabled: bool) -> None:
        (self.root / "douyin-spark-fusion-daily.timer").write_text(text, encoding="utf-8")
        wants = self.root / "timers.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        if enabled:
            (wants / "douyin-spark-fusion-daily.timer").symlink_to(
                self.root / "douyin-spark-fusion-daily.timer"
            )

    def test_从单元文件里读出真的时间与抖动(self) -> None:
        self._write_unit(
            "[Timer]\nOnCalendar=*-*-* 12:30:00\nRandomizedDelaySec=20m\nUnit=x.service\n",
            enabled=True,
        )
        info = store.timer_state(unit_root=str(self.root))
        self.assertEqual(info["state"], "enabled")
        self.assertEqual(info["onCalendar"], ["12:30"])
        self.assertEqual(info["jitterText"], "最多 20 分钟")

    def test_没启用就是_disabled(self) -> None:
        self._write_unit("[Timer]\nOnCalendar=*-*-* 12:30:00\n", enabled=False)
        info = store.timer_state(unit_root=str(self.root))
        self.assertEqual(info["state"], "disabled")
        self.assertFalse(info["enabled"])

    def test_派发器写法被认成敲门间隔(self) -> None:
        """`OnCalendar=*:0/5` 不是"每天几点"，是"每 5 分钟敲一次门"。"""
        self._write_unit("[Timer]\nOnCalendar=*:0/5\nUnit=x.service\n", enabled=True)
        info = store.timer_state(unit_root=str(self.root))
        self.assertEqual(info["dispatchMinutes"], 5)
        self.assertEqual(info["onCalendar"], [], "*/5 不能被当成一个'每天'的时刻")

    def test_每天定时没有敲门间隔(self) -> None:
        self._write_unit("[Timer]\nOnCalendar=*-*-* 12:30:00\n", enabled=True)
        info = store.timer_state(unit_root=str(self.root))
        self.assertEqual(info["dispatchMinutes"], 0)

    def test_认不出的写法就不猜时间(self) -> None:
        self._write_unit("[Timer]\nOnCalendar=Mon..Fri 08:00:00\n", enabled=True)
        info = store.timer_state(unit_root=str(self.root))
        self.assertEqual(info["onCalendar"], [], "工作日写法不能猜成'每天 08:00'")

    def test_单元不存在时不会瞎报(self) -> None:
        info = store.timer_state(unit_root=str(self.root))
        # Windows/开发机上没有 /run/systemd/system，应当是 unknown 而不是 missing
        self.assertIn(info["state"], ("missing", "unknown"))
        self.assertEqual(info["onCalendar"], [])


class ScheduleTimerWebUITestCase(WebUITestCase):
    """面板不能画一个不会发生的"下次自动触发"（真机上就抓到过：timer 是 disabled 的）。"""

    def _page_with_timer(self, state: str, on_calendar=(), dispatch=0, active="") -> str:
        original = webui_app.store.timer_state
        webui_app.store.timer_state = lambda *a, **kw: {
            "dispatchMinutes": dispatch,
            "name": "douyin-spark-fusion-daily.timer",
            "installed": state != "missing",
            "enabled": state == "enabled",
            "state": state,
            "activeState": active,
            "onCalendar": list(on_calendar),
        }
        try:
            os.environ["WEBUI_PASSWORD"] = PASSWORD
            with self.make_client() as client:
                self.login(client)
                return client.get("/").text
        finally:
            webui_app.store.timer_state = original

    def _with_quiet_window(self, spec: str) -> None:
        before = os.environ.get("QUIET_WINDOW")
        if spec is None:
            os.environ.pop("QUIET_WINDOW", None)
        else:
            os.environ["QUIET_WINDOW"] = spec

        def restore():
            if before is None:
                os.environ.pop("QUIET_WINDOW", None)
            else:
                os.environ["QUIET_WINDOW"] = before

        self.addCleanup(restore)

    def test_定时器没启用时首页明说不会自动跑(self) -> None:
        page = self._page_with_timer("disabled")
        self.assertIn("data-schedule-warning", page)
        self.assertIn("不会自动跑", page)
        self.assertIn("systemctl enable --now douyin-spark-fusion-daily.timer", page,
                      "要说清楚怎么开，不然用户只能猜")

    def test_定时器启用时不出现报警(self) -> None:
        page = self._page_with_timer("enabled")
        self.assertNotIn("data-schedule-warning", page)

    def test_启用但没在跑时明说到点不会触发(self) -> None:
        """`systemctl stop` 过的 timer 仍然 enabled —— 面板不能只画"已启用"。"""
        page = self._page_with_timer("enabled", ["20:30"], active="inactive")
        self.assertIn("data-schedule-warning", page)
        self.assertIn("当前是 inactive", page)
        self.assertIn("systemctl start douyin-spark-fusion-daily.timer", page)
        # 概览格里的"下次自动触发"也要改成不会自动跑（stalled → armed=False）
        self.assertIn("定时器未启用", page)

    def test_运行态查询本身失败也不抛(self) -> None:
        """没有 systemctl（容器/开发机）时 helper 必须返回空串，绝不冒泡。"""
        store = webui_app.store
        store._ACTIVE_CACHE.update({"unit": "", "at": 0.0, "state": ""})
        with mock.patch("webui.store.subprocess.run", side_effect=OSError("no systemctl")):
            self.assertEqual(store.timer_active_state("does-not-exist.timer"), "")

    def test_运行态只认白名单里的值(self) -> None:
        """systemctl 打印别的怪话（或报错）时按"不知道"处理，不要显示到面板上。"""
        store = webui_app.store
        store._ACTIVE_CACHE.update({"unit": "", "at": 0.0, "state": ""})
        done = mock.Mock(stdout="Unit foo.timer could not be found.\n", stderr="")
        with mock.patch("webui.store.subprocess.run", return_value=done):
            self.assertEqual(store.timer_active_state("foo.timer"), "")
        store._ACTIVE_CACHE.update({"unit": "", "at": 0.0, "state": ""})
        done = mock.Mock(stdout="failed\n", stderr="")
        with mock.patch("webui.store.subprocess.run", return_value=done):
            self.assertEqual(store.timer_active_state("foo.timer"), "failed")

    def test_运行态拿不到时不吓人(self) -> None:
        """查不到 `systemctl is-active`（开发机/容器）时按原来那样渲染。"""
        page = self._page_with_timer("enabled", ["20:30"], active="")
        self.assertNotIn("data-schedule-warning", page)
        self.assertNotIn("当前是", page)

    def test_没装定时器单元也说清楚(self) -> None:
        page = self._page_with_timer("missing")
        self.assertIn("data-schedule-warning", page)
        self.assertIn("没装", page)

    def test_禁跑窗盖住定时器时间时报红(self) -> None:
        """真机上差点踩到：`.env` 没写 QUIET_WINDOW、代码默认 20:20-21:00，正好包住 20:30。"""
        self._with_quiet_window("20:20-21:00")
        page = self._page_with_timer("enabled", ["20:30"])
        self.assertIn("data-schedule-quiet-clash", page)
        self.assertIn("20:20-21:00", page)
        self.assertIn("整轮跳过", page)

    def test_禁跑窗没盖住定时器时间时不出红框(self) -> None:
        self._with_quiet_window("20:20-21:00")
        page = self._page_with_timer("enabled", ["12:30"])
        self.assertNotIn("data-schedule-quiet-clash", page)

    def test_派发器定时器要说清是每N分钟敲门(self) -> None:
        """新架构：timer 只负责"敲门"，几点发由各账号自己的 time 决定。"""
        page = self._page_with_timer("enabled", dispatch=5)
        self.assertNotIn("data-schedule-warning", page)
        self.assertIn("每 5 分钟敲一次门", page)
        self.assertNotIn("系统定时器：", page, "别再把 */5 说成'系统定时器：每天 20:30'")

    def test_禁跑窗关掉时不出红框(self) -> None:
        self._with_quiet_window(None)
        page = self._page_with_timer("enabled", ["20:30"])
        self.assertNotIn("data-schedule-quiet-clash", page)
        self.assertNotIn("data-schedule-warning", page)

    def _with_next_due(self, due):
        """把"下一个该发的账号"钉死（不然断言要跟着跑测试那台机器的钟走）。"""
        original = webui_app.viewmodel.next_due
        webui_app.viewmodel.next_due = lambda *a, **kw: due
        self.addCleanup(lambda: setattr(webui_app.viewmodel, "next_due", original))

    def test_派发器模式下不许说下次触发不会实际发送(self) -> None:
        """timer 每 5 分钟敲一次门、各账号按自己的时间发 —— 这时"上一轮还剩几个目标"
        跟"下一次触发会不会发"是两件事（线上就出现过：面板说"下一次触发不会实际
        发送"，而 20:30 那个账号还没到点）。"""
        self._with_next_due(None)
        page = self._page_with_timer("enabled", dispatch=5)
        self.assertIn("今天该发的都发完了", page, "今天没有下一个 due 时要说这句话")
        self.assertNotIn("当前没有待发送目标，下一次触发不会实际发送", page)
        script = (REPO_ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("schedule.dispatcher", script, "前端必须按这个标志换说法")
        self.assertIn("今天该发的都发完了", script)

    def test_派发器模式下说清下一个是谁几点(self) -> None:
        self._with_next_due({"at": "2026-10-07T23:59:00+08:00", "text": "下一个 甲：今天 23:59"})
        page = self._page_with_timer("enabled", dispatch=5)
        self.assertIn("下一个 甲：今天 23:59", page, "面板要说清下一个发的是谁")
        self.assertIn("按各账号自己的时间发", page)
        self.assertNotIn("当前没有待发送目标，下一次触发不会实际发送", page)

    def test_值槽短句与字号类前后端一致(self) -> None:
        """值槽只有一行位置（26px 大字）：派发器模式下喂它「甲 · 已过 23:59」短句，
        整句话留给提示行。服务端渲染和 app.js 轮询必须用同一套回落顺序和同样两个
        类名 —— 不然每 10 秒刷一次，字号和句式就跳一次。"""
        self._with_next_due({"at": "2026-10-07T23:59:00+08:00", "name": "甲",
                             "due_text": "23:59", "imminent": True,
                             "text": "下一个 甲：已过 23:59，等下一次敲门就发"})
        page = self._page_with_timer("enabled", dispatch=5)
        self.assertIn("甲 · 已过 23:59", page, "值槽要短句，不是整句话")
        self.assertIn("overview-value--short", page)
        script = (REPO_ROOT / "webui" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("nextTriggerShort", script, "轮询不带短句就会把值槽改回整句话")
        self.assertIn("overview-value--short", script)
        self.assertIn("overview-value--prose", script)
        css = (REPO_ROOT / "webui" / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn(".overview-value--short", css)
        self.assertIn(".overview-value--prose", css)

    def test_接口带出派发器标志与那句话(self) -> None:
        self._with_next_due(None)
        original = webui_app.store.timer_state
        webui_app.store.timer_state = lambda *a, **kw: {
            "dispatchMinutes": 5,
            "name": "douyin-spark-fusion-daily.timer",
            "installed": True,
            "enabled": True,
            "state": "enabled",
            "onCalendar": [],
        }
        try:
            os.environ["WEBUI_PASSWORD"] = PASSWORD
            with self.make_client() as client:
                self.login(client)
                schedule = client.get("/api/ops/overview").json()["schedule"]
        finally:
            webui_app.store.timer_state = original
        self.assertTrue(schedule["dispatcher"])
        self.assertIn("nextTriggerText", schedule, "app.js 读的就是这个键")
        self.assertIn("今天该发的都发完了", schedule["nextTriggerText"])



class SkinWebUITestCase(unittest.TestCase):
    """第二套皮肤「极光棱镜」：两套样式表互斥加载，切换不刷新页面。

    原来那套是扁平控制台，这一套换成玻璃 + 极光 + 光谱流光。换皮肤只许动样式 ——
    结构、挂点、文案一个字都不许变 —— 所以这里钉的都是"两套皮肤必须共同满足"的契约：

    * 两套 CSS 都在，都带 `[hidden] { display: none !important }` 兜底（缺了就会把
      藏起来的东西照样画出来）；
    * 每张页面自己在 <head> 里**同步**挑皮肤：异步挑会先画一帧默认皮肤再跳一下；
    * 模板里用到的类，这套皮肤必须全都覆盖 —— 漏一个就是"换了皮还有一块是旧的"。
    """

    AURORA = REPO_ROOT / "webui" / "static" / "app-aurora.css"
    CLASSIC = REPO_ROOT / "webui" / "static" / "app.css"
    SCRIPT = REPO_ROOT / "webui" / "static" / "app.js"
    PAGES = ("base.html", "login.html")
    # 只当 JS 挂点、不需要自己样式的类（现在没有）。
    UNSTYLED_BY_DESIGN = frozenset()

    def test_极光皮肤是完整一套而不是薄薄一层覆盖(self) -> None:
        css = self.AURORA.read_text(encoding="utf-8")
        self.assertGreater(
            len(css.encode("utf-8")), 30 * 1024, "极光皮肤是独立的一整份样式表，不该只是覆盖几行"
        )
        for token in ("--bg", ".overview-value--short", ".overview-value--prose", ".aurora-scene"):
            self.assertIn(token, css)

    def test_两套皮肤都在且都过_hidden_兜底(self) -> None:
        for path in (self.CLASSIC, self.AURORA):
            css = path.read_text(encoding="utf-8")
            self.assertRegex(
                css,
                r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
                f"{path.name} 缺少 `[hidden] {{ display: none !important; }}` 兜底规则",
            )

    def test_浅色下渐变文字换成深色渐变(self) -> None:
        """渐变文字（background-clip: text + color: transparent）在深色底上是白字，
        浅色底上就等于把字藏了。凡是用光谱/亮色渐变画的标题都必须有一份浅色覆盖。"""
        css = self.AURORA.read_text(encoding="utf-8")
        for selector in (".brand-title", ".page-heading h1"):
            self.assertIn(
                f'html[data-theme="light"] {selector} {{',
                css,
                f"{selector} 缺浅色覆盖，浅色主题下会是一块看不见的渐变字",
            )

    def test_极光皮肤也能从静态路由取到(self) -> None:
        client = TestClient(webui_app.create_app())
        response = client.get("/static/app-aurora.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response.headers.get("cache-control", "").lower())

    def test_模板里用到的类极光皮肤都覆盖了(self) -> None:
        css = self.AURORA.read_text(encoding="utf-8")
        styled = set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", css))
        used = set()
        for name in sorted(p.name for p in (REPO_ROOT / "webui" / "templates").glob("*.html")):
            text = (REPO_ROOT / "webui" / "templates" / name).read_text(encoding="utf-8")
            for value in re.findall(r'class="([^"{}]*)"', text):
                used.update(value.split())
        missing = sorted(used - styled - set(self.UNSTYLED_BY_DESIGN))
        self.assertEqual(missing, [], f"这些类换了极光皮肤就没人管了：{missing}")

    def test_每张页面自己同步挑皮肤(self) -> None:
        """皮肤要在 <head> 里、样式表之前就定下来：放到 app.js 里挑 = 先闪一帧默认皮肤。"""
        for name in self.PAGES:
            page = (REPO_ROOT / "webui" / "templates" / name).read_text(encoding="utf-8")
            self.assertIn('id="app-stylesheet"', page, f"{name} 的样式表没有 id，切皮肤就换不了 href")
            self.assertIn("sparkflow-skin", page, f"{name} 少了同步挑皮肤的脚本")
            self.assertIn("app-aurora.css", page)
            head = page.split("</head>", 1)[0]
            self.assertIn("sparkflow-skin", head, f"{name} 挑皮肤的脚本必须在 <head> 里同步跑")
            self.assertIn('getElementById("app-stylesheet")', head)

    def test_皮肤开关桌面与手机各一个(self) -> None:
        page = (REPO_ROOT / "webui" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertEqual(page.count("data-skin-toggle"), 2, "手机顶栏与页头各要一个")
        for icon in ("skin-icon-classic", "skin-icon-aurora"):
            self.assertIn(icon, page)

    def test_切换只换样式表不刷新页面(self) -> None:
        script = self.SCRIPT.read_text(encoding="utf-8")
        for token in ("sparkflow-skin", "[data-skin-toggle]", "aurora-scene", "swapSheet"):
            self.assertIn(token, script)



class MaintenanceWebUITestCase(WebUITestCase):
    """「磁盘与资料目录」区块：只读盘点 + 手动清理，默认只预演。

    真机背景（2026-10-14）：生产机上资料目录已经涨到 1GB（单账号 `Default/Cache`
    就 451MB），磁盘只剩 25G —— **磁盘满 = 发送直接失败**，而缓存是可再生的。
    所以面板要有一个"看得见、能预演、再手动执行"的口子。

    这里钉的是**路由层**（清理规则本身在 `tests/test_maintenance.py`）：登录与
    CSRF 两个校验都在、参数丢了只会更保守、名单读不到时把错误显示出来且
    **真的一个文件都没删**。
    """

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "login-profile"
        self.root.mkdir()
        self._saved_profile_dir = os.environ.get("LOGIN_PROFILE_DIR")
        os.environ["LOGIN_PROFILE_DIR"] = str(self.root)
        self.addCleanup(self._restore_profile_dir)
        self.profile(UID, cache_bytes=4096)
        self.profile(OTHER_UID, cache_bytes=2048)

    def _restore_profile_dir(self) -> None:
        if self._saved_profile_dir is None:
            os.environ.pop("LOGIN_PROFILE_DIR", None)
        else:
            os.environ["LOGIN_PROFILE_DIR"] = self._saved_profile_dir

    # ------------------------------------------------------------ 脚手架

    def profile(self, name: str, *, cache_bytes: int = 0) -> Path:
        """造一个资料目录：`Default/Cache`（可再生） + `Cookies`（绝不能被清掉）。"""
        base = self.root / name / "Default"
        cache = base / "Cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "data_0").write_bytes(b"x" * max(0, cache_bytes))
        (base / "Cookies").write_bytes(b"sessionid-not-a-real-one")
        (self.root / name / "Local State").write_text("{}", encoding="utf-8")
        return self.root / name

    def orphan(self, name: str, *, idle_days: float = 30.0) -> Path:
        """造一个"不在名单里 + 很久没动"的目录（`is_orphan` 看的是 mtime）。"""
        base = self.profile(name)
        stamp = time.time() - idle_days * 86400
        for path in sorted(base.rglob("*"), reverse=True):
            os.utime(path, (stamp, stamp))
        os.utime(base, (stamp, stamp))
        return base

    def login_client(self, client: TestClient) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.login(client)

    def tree(self) -> List[Tuple[str, int]]:
        """整棵资料目录树的 (相对路径, 字节数) —— 用来证明"预演一个字节都没动"。"""
        rows: List[Tuple[str, int]] = []
        for path in sorted(self.root.rglob("*")):
            size = path.stat().st_size if path.is_file() else -1
            rows.append((str(path.relative_to(self.root)), size))
        return rows

    def report(self, client: TestClient):
        return client.get("/api/ops/maintenance", headers={"Accept": "application/json"})

    def prune(self, client: TestClient, action: str, apply: bool = False, **kwargs: Any):
        kwargs.setdefault("follow_redirects", False)
        return client.post(
            f"/ops/maintenance/{action}",
            data={"apply": "1" if apply else "0"},
            **kwargs,
        )

    # -------------------------------------------------------------- 用例

    def test_未登录拿不到盘点(self) -> None:
        with self.make_client() as client:
            response = self.report(client)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["ok"])

    def test_盘点列出每个资料目录与磁盘剩余(self) -> None:
        os.environ["TASKS"] = json.dumps(
            [{"unique_id": UID, "username": ACCOUNT_NAME, "targets": []}], ensure_ascii=False
        )
        with self.make_client() as client:
            self.login_client(client)
            response = self.report(client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store", "盘点是实时数字")
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(sorted(row["name"] for row in data["rows"]), sorted([UID, OTHER_UID]))
        # 目录名就是抖音号，光看号认不出人 → 昵称来自 TASKS。
        row = next(item for item in data["rows"] if item["name"] == UID)
        self.assertEqual(row["label"], f"{ACCOUNT_NAME}（{UID}）")
        self.assertEqual(row["human_cache_bytes"], "4.0 KB")
        self.assertIn("在册", row["tags"])
        self.assertIn("永不删除", row["hint"])
        self.assertEqual(data["totals"]["count"], 2)
        self.assertIn("资料目录合计", data["totals"]["text"])
        self.assertEqual(data["grace_days"], 7)
        self.assertTrue(data["disk"]["ok"], data["disk"])
        self.assertGreater(data["disk"]["free_bytes"], 0)
        self.assertTrue(data["disk"]["text"].startswith("磁盘剩余"))

    def test_预演一个字节都不动(self) -> None:
        before = self.tree()
        with self.make_client() as client:
            self.login_client(client)
            response = self.prune(client, "caches", apply=False)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertFalse(data["applied"])
        self.assertTrue(data["dry_run"])
        self.assertGreater(data["freed_bytes"], 0, "预演也要说清能释放多少")
        self.assertIn("预演", data["headline"])
        self.assertEqual(self.tree(), before, "预演是只读的")

    def test_执行只删在册账号的可再生缓存(self) -> None:
        with self.make_client() as client:
            self.login_client(client)
            response = self.prune(client, "caches", apply=True)
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["applied"])
        self.assertFalse((self.root / UID / "Default" / "Cache").exists())
        self.assertGreater(data["freed_bytes"], 0)
        # 登录态绝不能碰：清了就等于把账号踢下线（还得重新扫码）。
        self.assertTrue((self.root / UID / "Default" / "Cookies").exists())
        self.assertTrue((self.root / UID / "Local State").exists())
        # 不在名单里的目录不归"清缓存"管（那是"清孤儿目录"的事，还带宽限期）。
        self.assertTrue((self.root / OTHER_UID / "Default" / "Cache" / "data_0").exists())

    def test_参数丢了只会更保守(self) -> None:
        """`apply` 是决定"删不删文件"的字段：缺失/写错只能当成"不删"。"""
        before = self.tree()
        with self.make_client() as client:
            self.login_client(client)
            missing = client.post("/ops/maintenance/caches", follow_redirects=False)
            garbage = client.post(
                "/ops/maintenance/orphans", data={"apply": "yes-please"}, follow_redirects=False
            )
        for response in (missing, garbage):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["applied"])
        self.assertEqual(self.tree(), before)

    def test_名单读不到时执行也一个都不删(self) -> None:
        """安全线：名单读不到 → 每个目录都像"孤儿"，这时删任何东西都是灾难。"""
        self.utils_config.userData = []      # get_userData() 读到空名单
        os.environ["TASKS"] = "[]"
        before = self.tree()
        with self.make_client() as client:
            self.login_client(client)
            listing = self.report(client)
            response = self.prune(client, "caches", apply=True)
        self.assertFalse(listing.json()["ok"], "面板据此把「执行」藏起来")
        self.assertIn("名单", listing.json()["error"])
        self.assertEqual(listing.json()["rows"], [])
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertIn("名单", data["error"])
        self.assertIn("没动任何文件", data["headline"])
        self.assertEqual(self.tree(), before, "名单读不到时一个文件都不许动")

    def test_清孤儿目录预演与执行(self) -> None:
        dead = self.orphan("99999999999", idle_days=30)
        with self.make_client() as client:
            self.login_client(client)
            dry = self.prune(client, "orphans", apply=False)
            self.assertTrue(dead.exists(), "预演不删")
            done = self.prune(client, "orphans", apply=True)
        dry_data = dry.json()
        self.assertTrue(dry_data["ok"], dry_data)
        self.assertIn("99999999999", [item["label"] for item in dry_data["removed"]])
        data = done.json()
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["applied"])
        self.assertFalse(dead.exists())
        # 在册账号的目录永不删，宽限期内的孤儿也不动（见 core.maintenance 的安全线）。
        self.assertTrue((self.root / UID).exists())
        self.assertIn(UID, data["kept"])
        self.assertEqual(data["grace_days"], 7)

    def test_跨站与未登录都挡在门外(self) -> None:
        with self.make_client() as client:
            anonymous = self.prune(client, "caches", apply=True)
        self.assertEqual(anonymous.status_code, 401)
        with self.make_client() as client:
            self.login_client(client)
            cross = self.prune(
                client, "caches", apply=True, headers={"Origin": "http://evil.example"}
            )
        self.assertEqual(cross.status_code, 403)
        self.assertTrue(
            (self.root / UID / "Default" / "Cache" / "data_0").exists(),
            "被挡住的请求不能有任何副作用",
        )

    def test_首页渲染出这一块(self) -> None:
        with self.make_client() as client:
            self.login_client(client)
            page = client.get("/", headers={"Accept": "text/html"})
        self.assertIn("data-maintenance-panel", page.text)
        self.assertIn('data-maintenance-action="caches"', page.text)
        self.assertIn('data-maintenance-action="orphans"', page.text)
        self.assertIn('data-apply="1"', page.text)
        self.assertIn("data-maintenance-disk", page.text)
        self.assertIn("data-maintenance-rows", page.text)
        self.assertIn("data-maintenance-result", page.text)


class ReloginRequeueTests(unittest.TestCase):
    """重新扫码登录成功后，当天没发出去的要自动补发。

    时间表把"失败"也算办过（防的是坏 cookie 一天被门铃踹 288 次），所以掉登录
    那一轮记完之后，人重新登录还得手点「立即运行」才会补发 —— 2026-10-09 示例号
    掉登录那天就丢在这儿。`_requeue_after_relogin()` 在登录成功的瞬间撤掉记账，
    下一个 5 分钟门铃自动补上；**已经成功发过的账号绝不撤**（撤了就一天发两遍）。

    ★ 刻意**不继承** `WebUITestCase`：这里只碰账本与时间表两个文件，
      继承会把整套面板用例再跑一遍（还会带上那两条环境相关的既有失败）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dsf-requeue-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.ledger = self.tmp / "runs.jsonl"
        self.schedule_file = self.tmp / "schedule_state.json"

        self._saved_env = {k: os.environ.get(k)
                           for k in ("RUN_LEDGER_FILE", "SCHEDULE_STATE_FILE")}
        os.environ["RUN_LEDGER_FILE"] = str(self.ledger)
        os.environ["SCHEDULE_STATE_FILE"] = str(self.schedule_file)

        def restore() -> None:
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)

        from core import schedule
        self.schedule = schedule
        state = schedule.load_state(self.schedule_file)
        schedule.mark_sent(state, UID, when=schedule.now(),
                           status=schedule.STATUS_ERROR, note="login_lost")
        schedule.save_state(state, self.schedule_file)

    # ------------------------------------------------------------ 脚手架

    def write_ledger(self, rows: List[Any]) -> None:
        lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _run_today(self, sent_ok: Any, unique_id: str = UID) -> Dict[str, Any]:
        started = datetime.now().astimezone()
        return {
            "run_id": "20261009-173500-abcd",
            "started_at": _iso(started),
            "finished_at": _iso(started),
            "dry_run": "off",
            "accounts": [{
                "username": ACCOUNT_NAME if unique_id == UID else OTHER_ACCOUNT,
                "unique_id": unique_id,
                "status": "error" if sent_ok == 0 else "ok",
                "gate": "READY",
                "sent_ok": sent_ok,
                "sent_fail": 0,
                "entries": [],
                "missing": [],
                "select_failed": [],
                "targets": [],
            }],
        }

    def _marked_today(self) -> bool:
        state = self.schedule.load_state(self.schedule_file)
        return self.schedule.entry_for(state, UID, self.schedule.now()) is not None

    def _clear_for_retry(self) -> bool:
        """今天还能不能补发 —— 这才是「撤记账」真正要保证的那件事。

        `_marked_today()` 只看"有没有一条记录"，而 `schedule.select` 认的是**状态**：
        只有 `ok` 这类"真发出去了"的记录才吃掉当天名额，`skipped` 的意思是
        "今天试过但没发出去"，名额还在（见 `core/schedule.py:550`）。
        """
        state = self.schedule.load_state(self.schedule_file)
        users = [{"unique_id": UID, "username": ACCOUNT_NAME, "targets": []}]
        plan = self.schedule.select(users, state=state, fallback="00:00", jitter=0)
        return any(user["unique_id"] == UID for user, _plan in plan["due"])

    def _mark_today(self, status: str) -> None:
        """把今天的记账改成指定状态（setUp 预置的是 `error` 墓碑）。"""
        state = self.schedule.load_state(self.schedule_file)
        self.schedule.mark_sent(
            state, UID, when=self.schedule.now(), status=status, note="test"
        )
        self.schedule.save_state(state, self.schedule_file)

    def _clear_marks(self) -> None:
        """清空状态文件 —— 测"两边都没有今天的记录"那一支。

        ★ 2026-10-14 起状态文件也算"今天跑过"（见 `_requeue_after_relogin`）：
          setUp 预置的那条 error 墓碑会走"撤账补发"，所以想测"今天根本没轮到它"
          就必须把状态也清干净。
        """
        self.schedule.save_state({}, self.schedule_file)

    # -------------------------------------------------------------- 用例

    def test_zero_sent_today_clears_the_mark(self):
        self.write_ledger([self._run_today(sent_ok=0)])
        out = webui_app._requeue_after_relogin(UID)
        self.assertTrue(out["requeued"], out)
        # 撤记账写成 `skipped` 墓碑，而不是把记录删掉：面板要能说出"今天试过但没
        # 发出去"，而"还能不能发"由状态决定 —— 所以两个断言都要。
        state = self.schedule.load_state(self.schedule_file)
        record = self.schedule.entry_for(state, UID, self.schedule.now())
        self.assertEqual(str(record.get("status") or ""), self.schedule.STATUS_SKIPPED)
        self.assertTrue(self._clear_for_retry(), "重新登录之后当天必须还能补发")

    def test_already_sent_today_keeps_the_mark(self):
        self.write_ledger([self._run_today(sent_ok=2)])
        out = webui_app._requeue_after_relogin(UID)
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "already-sent")
        self.assertTrue(self._marked_today())

    def test_partial_success_also_keeps_the_mark(self):
        """只要当天发出去过一条，就不许再补 —— 补了朋友会收到两遍。"""
        self.write_ledger([self._run_today(sent_ok=1)])
        out = webui_app._requeue_after_relogin(UID)
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "already-sent")
        self.assertTrue(self._marked_today())

    def test_unreadable_count_is_treated_as_sent(self):
        self.write_ledger([self._run_today(sent_ok="不知道")])
        out = webui_app._requeue_after_relogin(UID)
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "already-sent")
        self.assertTrue(self._marked_today())

    def test_no_run_today_is_left_alone(self):
        """今天根本没轮到它 → 没有记账要撤，保持原样（信息类返回，不报错）。"""
        self._clear_marks()
        self.write_ledger([])
        out = webui_app._requeue_after_relogin(UID)
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "no-run-today")
        self.assertFalse(self._marked_today())

    def test_another_accounts_run_does_not_requeue(self):
        """账本里今天的记录是**别的**账号的、状态里也没有它 → 不能撤这个账号的记账。"""
        self._clear_marks()
        self.write_ledger([self._run_today(sent_ok=0, unique_id=OTHER_UID)])
        out = webui_app._requeue_after_relogin(UID)
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "no-run-today")
        self.assertFalse(self._marked_today())

    def test_ok_mark_today_keeps_the_mark(self):
        """状态里今天已经是 `ok` → 账本读不到也不能补发（补了朋友会收到两遍）。

        账本和状态是**两次写**，账本还可能被轮转/截断；状态里明明记着"今天发出去了"，
        只看账本就会当成"今天没跑过"而撤账补发。
        """
        self._mark_today(self.schedule.STATUS_OK)
        self.write_ledger([])
        out = webui_app._requeue_after_relogin(UID)
        self.assertFalse(out["requeued"], out)
        self.assertEqual(out["reason"], "already-sent")
        state = self.schedule.load_state(self.schedule_file)
        record = self.schedule.entry_for(state, UID, self.schedule.now())
        self.assertEqual(str(record.get("status") or ""), self.schedule.STATUS_OK)
        self.assertFalse(self._clear_for_retry(), "已经发过了，当天名额不能还回来")

    def test_error_mark_without_ledger_still_requeues(self):
        """状态里有今天（error）、账本里没有今天的行 → 允许撤账补发。

        收尾时先写状态、后追加账本，中间被 SIGKILL/OOM/重启打断就是这个样子。
        旧口径只看账本 → `no-run-today` → 既不撤账也不补发，当天火花静默丢失。
        """
        # setUp 写的就是 error 墓碑；账本留空 = 那一行没落盘。
        self.write_ledger([])
        out = webui_app._requeue_after_relogin(UID)
        self.assertTrue(out["requeued"], out)
        self.assertEqual(out["reason"], "")
        self.assertTrue(self._clear_for_retry(), "撤账之后当天必须还能补发")

    def test_skipped_mark_without_ledger_still_requeues(self):
        """`skipped`（试过但没发出去）同样不该吃掉当天名额。"""
        self._mark_today(self.schedule.STATUS_SKIPPED)
        self.write_ledger([])
        out = webui_app._requeue_after_relogin(UID)
        self.assertTrue(out["requeued"], out)
        self.assertTrue(self._clear_for_retry())

    def test_empty_uid_is_a_noop(self):
        out = webui_app._requeue_after_relogin("")
        self.assertFalse(out["requeued"])
        self.assertEqual(out["reason"], "no-uid")
        self.assertTrue(self._marked_today())


class PollingStateWebUITestCase(WebUITestCase):
    """issue #9/#10/#11：首屏渲染与 10 秒轮询必须给同一份状态。

    这三条是同一个形状的坑：服务端把状态算好了，但页面上没有可写的节点（#9）、
    压根没把状态发给前端（#10）、或者挂点在模板里根本不存在（#11）。
    """

    def _payload(self, client: TestClient) -> Dict[str, Any]:
        response = client.get("/api/ops/overview", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        return response.json()

    @staticmethod
    def _tag(page: str, marker: str) -> str:
        """marker 所在那个开标签的属性段 —— 用来断言 hidden / disabled 这类属性。"""
        return page.split(marker, 1)[1].split(">", 1)[0]

    @classmethod
    def _form(cls, page: str, action: str) -> str:
        return page.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]

    def test_空跑档位药丸在首屏常驻而轮询能点亮(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ledger([self.sample_run(dry_run="full")])
        with self.make_client() as client:
            self.login(client)
            page = client.get("/").text
            payload = self._payload(client)
        self.assertEqual(payload["summary"]["dryRun"], "full")
        self.assertIn("输入后清空", payload["summary"]["dryRunText"])
        self.assertNotIn("hidden", self._tag(page, "data-overview-dry-run "),
                         "这一轮是空跑：药丸必须在页面上露着")
        self.assertNotIn("hidden", self._tag(page, 'data-overview-dry-run-hint="single"'),
                         "空跑那一句必须露着")
        self.assertIn("hidden", self._tag(page, 'data-overview-dry-run-hint="mixed"'))

    def test_不是空跑时药丸常驻但藏着(self) -> None:
        """元素必须常驻：不常驻的话，档位改了轮询也没法让它出现（issue #9 的正因）。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_ledger([self.sample_run(dry_run="off")])
        with self.make_client() as client:
            self.login(client)
            page = client.get("/").text
            payload = self._payload(client)
        self.assertEqual(payload["summary"]["dryRun"], "off")
        self.assertIn("data-overview-dry-run ", page)
        self.assertIn("hidden", self._tag(page, "data-overview-dry-run "))
        self.assertIn("hidden", self._tag(page, 'data-overview-dry-run-hint="single"'))
        self.assertIn("hidden", self._tag(page, 'data-overview-dry-run-hint="mixed"'))

    def test_冷却开始时横幅在首屏与轮询里都在(self) -> None:
        """#10：冷却由"上一轮命中关键词"自己成立 —— 不刷新页面也得看得见。"""
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_rate_limit(datetime.now(timezone.utc) + timedelta(minutes=30))
        with self.make_client() as client:
            self.login(client)
            page = client.get("/").text
            runs_page = client.get("/ops/runs").text
            payload = self._payload(client)
        kinds = [banner["kind"] for banner in payload["banners"]]
        self.assertIn("cooldown", kinds, "冷却必须进 payload，否则轮询无从重画")
        banner = next(item for item in payload["banners"] if item["kind"] == "cooldown")
        self.assertIn("风控冷却中", banner["title"])
        self.assertTrue(banner["until"], "冷却横幅要带 until，倒计时才有的算")
        self.assertIn("data-status-banner-list", page)
        self.assertIn("风控冷却中", page)
        self.assertIn("data-countdown-until", page)
        self.assertIn("风控冷却中", runs_page, "运行记录页与首页是同一块横幅")

    def test_运行中账号动作按钮被禁用(self) -> None:
        """#11：轮询手里只有 running，而"运行期间不许动账号"必须落到按钮上。

        重新登录会去抢同一份资料目录、移除会把正在跑的账号从 TASKS 里删掉、补发会撤
        当天的记账 —— 三件事在运行期间都不该点得动（首屏与轮询必须同源）。
        """
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        self.write_lock(pid=os.getpid())
        with self.make_client() as client:
            self.login(client)
            running_page = client.get("/").text
        self.lock_file.unlink()
        with self.make_client() as client:
            self.login(client)
            idle_page = client.get("/").text
        for action in ("/ops/login/start", "/ops/account/remove", "/ops/account/requeue"):
            with self.subTest(action=action):
                self.assertIn("data-disable-while-running", self._form(running_page, action))
                self.assertIn("disabled", self._form(running_page, action), "运行中必须禁用")
                self.assertNotIn("disabled", self._form(idle_page, action), "空闲时不该禁用")


class RequeueRouteTests(unittest.TestCase):
    """issue #12：面板那条"撤回今天的记账，现在补发一次"的明路。

    账号当天掉登录之后，时间表把 `error` 也当成办过（当天不再重试），人只能靠"重新
    登录"顺带撤账。两条登录路径现在都会自动撤账，但要是哪条都没走到（登录窗口没抓到
    抖音号、cookie 是手工贴的……），用户唯一的办法是手改 `logs/schedule_state.json`
    —— 一个内部状态文件，文档里没有面向用户的说明。

    这一类自己起临时状态文件与客户端（不用 WebUITestCase：那套脚手架会把它的
    60 多条用例在每个子类里再跑一遍，而这里只需要一条路由）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dsf-requeue-route-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.ledger = self.tmp / "runs.jsonl"
        self.schedule_file = self.tmp / "schedule_state.json"
        self._saved_env = {k: os.environ.get(k)
                           for k in ("RUN_LEDGER_FILE", "SCHEDULE_STATE_FILE", "WEBUI_PASSWORD")}
        os.environ["RUN_LEDGER_FILE"] = str(self.ledger)
        os.environ["SCHEDULE_STATE_FILE"] = str(self.schedule_file)

        def restore() -> None:
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.addCleanup(self._restore_log)
        self._log_patcher = mock.patch.object(webui_app, "_log")
        self._log_patcher.start()

        from core import schedule
        self.schedule = schedule
        state = schedule.load_state(self.schedule_file)
        schedule.mark_sent(state, UID, when=schedule.now(),
                           status=schedule.STATUS_ERROR, note="login_lost")
        schedule.save_state(state, self.schedule_file)

    def _restore_log(self) -> None:
        if getattr(self, "_log_patcher", None) is not None:
            self._log_patcher.stop()
            self._log_patcher = None

    def _client(self) -> TestClient:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        client = TestClient(webui_app.create_app())
        client.post("/login", data={"password": PASSWORD, "next": "/"},
                    follow_redirects=False)
        return client

    def _clear_for_retry(self) -> bool:
        """今天还能不能补发 —— 与 ReloginRequeueTests 同一口径。"""
        state = self.schedule.load_state(self.schedule_file)
        users = [{"unique_id": UID, "username": ACCOUNT_NAME, "targets": []}]
        plan = self.schedule.select(users, state=state, fallback="00:00", jitter=0)
        return any(user["unique_id"] == UID for user, _plan in plan["due"])

    @staticmethod
    def _post(client: TestClient, unique_id: str = UID):
        return client.post(
            "/ops/account/requeue",
            data={"unique_id": unique_id},
            headers={"Accept": "application/json"},
        )

    def test_点了补发之后当天真的还能补发(self) -> None:
        response = self._post(self._client())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"], response.text)
        self.assertTrue(self._clear_for_retry(), "撤账之后当天必须还能补发")

    def test_今天已经发过的账号拒绝补发(self) -> None:
        """按钮不许造出"一天发两遍"：已经成功发过就拒绝（判定在 core/schedule 里）。"""
        state = self.schedule.load_state(self.schedule_file)
        self.schedule.mark_sent(state, UID, when=self.schedule.now(),
                                status=self.schedule.STATUS_OK, note="test")
        self.schedule.save_state(state, self.schedule_file)
        response = self._post(self._client())
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("今天已经成功发过", response.json()["error"])
        self.assertFalse(self._clear_for_retry(), "已经发过了，当天名额不能还回来")

    def test_没登录时不许撤账(self) -> None:
        os.environ["WEBUI_PASSWORD"] = PASSWORD
        response = self._post(TestClient(webui_app.create_app()))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self._clear_for_retry(), "没登录时根本不该动到记账")

class StartSlotTests(unittest.TestCase):
    """开启窗口的占位：忙检查与"占位登记"必须在同一把锁里完成。

    真实场景是双击「扫码登录」：旧代码在锁内查忙、出锁去拉起 Chromium（秒级）、
    **最后**才登记会话。第二个请求于是在"还没登记"的窗口里也通过了忙检查，两个
    Chromium 抢同一份 `logs/login-profile/<uid>/`，第二个抛 SingletonLock 之后
    context 没有引用、那目录再也打不开。现在第一个请求一进临界区就占位，第二个
    必须当场被拒（路由层是 409 + 中文原因）。
    """

    def setUp(self) -> None:
        self.addCleanup(webui_app._LOGIN_SESSIONS.clear)
        self.addCleanup(webui_app._FRIEND_SESSIONS.clear)
        self.addCleanup(setattr, webui_app, "_LOGIN_LATEST", "")
        self.addCleanup(setattr, webui_app, "_FRIEND_LATEST", "")
        self._log_patcher = mock.patch.object(webui_app, "_log")
        self._log_patcher.start()
        self.addCleanup(self._log_patcher.stop)

    def _claim(self, sessions, lock, *, profile, busy_error, owner=UID, kind="login"):
        placeholder = webui_app._StartingSession(profile_dir=profile, owner=owner, kind=kind)
        return placeholder, webui_app._claim_start_slot(
            sessions, lock, placeholder, busy_error=busy_error
        )

    def test_开启窗口里的第二个请求会被当场拒掉(self) -> None:
        _, (first_id, error) = self._claim(
            webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK,
            profile="/tmp/probe/profile-a", busy_error=webui_app._busy_login_error,
        )
        self.assertTrue(first_id, "第一个请求应该拿到占位")
        self.assertEqual(error, "")

        second, (second_id, second_error) = self._claim(
            webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK,
            profile="/tmp/probe/profile-a", busy_error=webui_app._busy_login_error,
        )
        self.assertEqual(second_id, "", "同一个窗口里的第二个请求不能再拿到占位")
        self.assertIn("该账号的扫码登录正在进行中", second_error)
        # 既有断言用的关键字必须保留（tests/test_webui.py 里的「同时只能有一次扫码」）
        self.assertIn("已经有一次扫码登录在进行", second_error)
        self.assertNotIn(second, list(webui_app._LOGIN_SESSIONS.values()))

    def test_好友侧走的是同一套(self) -> None:
        _, (first_id, error) = self._claim(
            webui_app._FRIEND_SESSIONS, webui_app._FRIEND_LOCK,
            profile="/tmp/probe/profile-b", busy_error=webui_app._busy_friend_error,
            kind="friend",
        )
        self.assertTrue(first_id)
        self.assertEqual(error, "")

        _, (second_id, second_error) = self._claim(
            webui_app._FRIEND_SESSIONS, webui_app._FRIEND_LOCK,
            profile="/tmp/probe/profile-b", busy_error=webui_app._busy_friend_error,
            kind="friend",
        )
        self.assertEqual(second_id, "")
        self.assertIn("该账号的好友列表读取正在进行中", second_error)
        self.assertIn("已经有一次好友列表读取在进行", second_error)

    def test_占位会被_profile_in_use_看见也能撤掉(self) -> None:
        placeholder, (session_id, error) = self._claim(
            webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK,
            profile="/tmp/probe/profile-c", busy_error=webui_app._busy_login_error,
        )
        self.assertEqual(error, "")
        self.assertEqual(webui_app._profile_in_use("/tmp/probe/profile-c"), "这个账号正在扫码登录")

        self.assertTrue(webui_app._drop_claim(webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK, session_id))
        self.assertEqual(webui_app._profile_in_use("/tmp/probe/profile-c"), "",
                         "占位撤掉之后 profile 必须立刻空出来（否则就是「永远忙」的假状态）")
        self.assertFalse(webui_app._drop_claim(webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK, session_id))

    def test_卡死的占位会被更早回收(self) -> None:
        stale, (stale_id, _) = self._claim(
            webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK,
            profile="/tmp/probe/profile-d", busy_error=webui_app._busy_login_error,
        )
        # 浏览器启动线程死了：占位自己不会更新，也不能等满 SESSION_STUCK_SECONDS(20 分钟)
        stale._updated = time.time() - (webui_app.START_STUCK_SECONDS + 5)

        dropped = webui_app._reap_stuck_sessions(
            webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK, label="扫码登录"
        )
        self.assertIn(stale_id, dropped)
        self.assertNotIn(stale_id, webui_app._LOGIN_SESSIONS)
        self.assertEqual(webui_app._profile_in_use("/tmp/probe/profile-d"), "")
        self.assertTrue(stale.cancelled)

    def test_刚开始的占位不会被误杀(self) -> None:
        fresh, (fresh_id, _) = self._claim(
            webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK,
            profile="/tmp/probe/profile-e", busy_error=webui_app._busy_login_error,
        )
        self.assertEqual(
            webui_app._reap_stuck_sessions(webui_app._LOGIN_SESSIONS, webui_app._LOGIN_LOCK,
                                           label="扫码登录"),
            [],
        )
        self.assertIn(fresh_id, webui_app._LOGIN_SESSIONS)
        self.assertFalse(fresh.cancelled)

    def test_占位期间面板读取是安全的(self) -> None:
        """占位对象要给出登录/好友状态里用到的字段（模板与 JS 靠 falsy 兜底）。"""
        placeholder = webui_app._StartingSession(profile_dir="/tmp/probe/profile-f", owner=UID)
        snap = placeholder.snapshot()
        self.assertEqual(snap["state"], "starting")
        self.assertTrue(snap["active"], "占位期间必须算 active，否则忙检查看不见它")
        self.assertEqual(placeholder.items(), [])
        self.assertEqual(placeholder.qr_png(), b"")
        self.assertEqual(placeholder.shot_png(), b"")


class LoginFailBudgetTests(unittest.TestCase):
    """口令失败记账的三层治理：按来源罚站 / 全局限流 / 表有上界。"""

    def setUp(self) -> None:
        with webui_app._LOGIN_FAILS_LOCK:
            webui_app._LOGIN_FAILS.clear()
            webui_app._LOGIN_FAIL_TIMES.clear()
        self.addCleanup(self._reset)
        self._log_patcher = mock.patch.object(webui_app, "_log")
        self._log_patcher.start()
        self.addCleanup(self._log_patcher.stop)

    def _reset(self) -> None:
        with webui_app._LOGIN_FAILS_LOCK:
            webui_app._LOGIN_FAILS.clear()
            webui_app._LOGIN_FAIL_TIMES.clear()

    @staticmethod
    def _request(host: str):
        client = type("FakeClient", (), {"host": host})()
        return type("FakeRequest", (), {"client": client})()

    def test_连错到额度开始罚站并且成功一次就清掉(self) -> None:
        request = self._request("10.1.1.1")
        for _ in range(webui_app._LOGIN_FAIL_LIMIT):
            self.assertEqual(webui_app._login_wait_left(request), 0.0, "还没到额度不该拦住")
            webui_app._note_login_fail(request)
        self.assertGreater(webui_app._login_wait_left(request), 0.0, "连错到额度必须罚站")

        webui_app._clear_login_fail(request)
        self.assertEqual(webui_app._login_wait_left(request), 0.0)

    def test_记账表有上界(self) -> None:
        for index in range(webui_app._LOGIN_FAIL_TRACK_MAX + 88):
            webui_app._note_login_fail(self._request(f"10.9.{index}"))
        with webui_app._LOGIN_FAILS_LOCK:
            tracked = len(webui_app._LOGIN_FAILS)
        self.assertLessEqual(tracked, webui_app._LOGIN_FAIL_TRACK_MAX,
                             "换 IP 猜口令不能把面板的内存吃掉")

    def test_早就走人的记账会被清掉(self) -> None:
        request = self._request("10.8.8.8")
        with webui_app._LOGIN_FAILS_LOCK:
            webui_app._LOGIN_FAILS["10.8.8.8"] = [
                1, 0.0, time.monotonic() - webui_app._LOGIN_FAIL_FORGET - 60,
            ]
        self.assertEqual(webui_app._login_wait_left(request), 0.0)
        self.assertNotIn("10.8.8.8", webui_app._LOGIN_FAILS)

    def test_全局限流能挡住换_IP_的(self) -> None:
        for index in range(webui_app._LOGIN_FAIL_GLOBAL_MAX):
            self.assertEqual(webui_app._login_global_wait_left(), 0.0)
            webui_app._note_login_fail(self._request(f"10.2.0.{index}"))
        self.assertGreater(webui_app._login_global_wait_left(), 0.0,
                           "所有来源合计打满之后要整体拒一小会儿")
        self.assertGreater(webui_app._LOGIN_FAIL_GLOBAL_MAX, 4,
                           "正常运维输错几次（手滑）不能被全局限流锁死")


if __name__ == "__main__":
    unittest.main()
