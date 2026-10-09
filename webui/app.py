"""FastAPI 应用工厂 —— 仪表盘的全部路由。

路由一览
--------
============================  ==========  =========================================================
路径                           方法        说明
============================  ==========  =========================================================
``/``                         GET         单页仪表盘（需登录）
``/login``                    GET/POST    登录页 / 提交口令
``/logout``                   POST        登出（清 cookie）
``/api/overview``             GET         JSON：状态 + KPI + 冷却 + 账号 + 最近运行
``/api/runs/{run_id}``        GET         JSON：某轮的逐条目标明细（展开时按需取）
``/ops/run-now``              POST        后台线程启动一轮（409 = 已在跑；503 = 缺浏览器依赖）
``/ops/log-tail``             GET         日志尾部的 HTML 片段（前端用 DOMParser + textContent 刷新）
``/ops/logs``                 GET         日志页（5 秒自动刷新 + 关键词过滤 + 下载链接）
``/ops/logs/download``        GET         下载完整日志（Cache-Control: no-store）
``/healthz``                  GET         ``{"ok": true}``，**不需要登录**
``/static/*``                 GET         静态资源（挂载 StaticFiles）
============================  ==========  =========================================================

安全口径
--------
* 所有用户/环境数据都通过 Jinja2（``autoescape=True``）或 ``JSONResponse`` 输出；
  前端只允许 ``textContent``，源码里不存在 ``innerHTML``。
* 非安全方法校验 Origin/Referer 必须是同源（配合 cookie 的 ``same_site=lax``）。
* ``core.tasks`` 只在真正要跑任务时于**函数内部**导入；导入失败返回 503 + 中文说明。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webui import auth, config, envfile, logtail, password, settings, store, viewmodel

__all__ = ["build_context", "create_app", "run_once", "start_run_thread"]

WEBUI_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEBUI_DIR / "templates"
STATIC_DIR = WEBUI_DIR / "static"

DRY_RUN_LEVELS = ("off", "select", "full")
DRY_RUN_LABELS = {
    "off": "真实发送",
    "select": "只选中不发送",
    "full": "输入后清空",
}


def normalize_dry_run(value) -> str:
    """把面板传来的档位归一成 off / select / full。

    分段控件的挂点叫 `dry-off`/`dry-select`/`dry-full`（同一个前缀还要配对下面的
    说明面板），前端一度把这个带前缀的值直接塞进隐藏域 → 服务端只认光秃秃的三档，
    于是**点了按钮什么都不会发生**，只在首页顶上来一行红字（真机复现过）。
    前端已经改对了，但这里再兜一层：老页面被浏览器缓存住时也不会白点。
    """
    text = str(value or "").strip().lower()
    if text.startswith("dry-"):
        text = text[4:]
    return text or "off"

# 缺浏览器依赖时的统一中文说明（HTTP 503）。
BROWSER_MISSING_HINT = (
    "本机未安装 cloakbrowser / playwright，无法执行任务。"
    "请在部署机（.172）的 .venv 里安装依赖后重试；"
    "仪表盘的查看类功能不受影响。"
)

# 浏览器依赖的模块名：只要能拿到其中**任意一个**就算能跑（core 侧两个都认）。
BROWSER_MODULES = ("cloakbrowser", "playwright")

_RUN_THREADS: List[threading.Thread] = []
#: 面板触发的那一轮的结局（run_id → run_once 的返回值）。
#: 后端线程出事时，页面上至少还能查到"这一轮到底怎么了"。
_RUN_RESULTS: Dict[str, Dict[str, Any]] = {}
_RUN_RESULTS_LOCK = threading.Lock()


class RunUnavailable(RuntimeError):
    """拿不到 core.tasks.runTasks（通常是缺 cloakbrowser / playwright）。"""


# ---------------------------------------------------------------- 模板


def _call(value):
    """模板过滤器：调用一个无参可调用对象，配合 ``map(attribute=…) | map('call')`` 用。

    Jinja2 里 ``record.summary`` 取到的是**绑定方法对象**，直接把方法塞进 ``tojson``
    会炸 ``TypeError: Object of type method is not JSON serializable``；而模板里又没法在
    ``map(attribute=…)`` 后面直接加括号调用。补一个 ``call`` 过滤器，把"取属性"和
    "调用"两步分开，JSON 里就只会是具体值。
    """
    return value() if callable(value) else value


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = True  # 显式声明：这是防 XSS 的第一道闸
    templates.env.filters["duration"] = store.fmt_duration
    templates.env.filters["stamp"] = store.fmt_stamp
    templates.env.filters["call"] = _call
    templates.env.globals["DRY_RUN_LABELS"] = DRY_RUN_LABELS
    templates.env.globals["asset_version"] = asset_version
    return templates


_ASSET_VERSION = ""


def asset_version() -> str:
    """静态资源的版本串：跟着 app.css / app.js 的 mtime 走。

    以前这里是写死的 `20260919-3`，而 StaticFiles 只发 ETag 不发 Cache-Control，
    浏览器就按"上次修改时间"启发式缓存 —— 结果新 HTML 配上旧 CSS，页面看着像错位。
    改成跟着文件 mtime 变，每次部署 URL 都换，缓存自然失效。
    """
    global _ASSET_VERSION
    if _ASSET_VERSION:
        return _ASSET_VERSION
    stamp = 0.0
    for name in ("app.css", "app.js"):
        try:
            stamp = max(stamp, (STATIC_DIR / name).stat().st_mtime)
        except OSError:
            continue
    if not stamp:
        _ASSET_VERSION = "0"
    else:
        _ASSET_VERSION = time.strftime("%Y%m%d%H%M", time.localtime(stamp))
    return _ASSET_VERSION


# ---------------------------------------------------------------- 运行状态装载


def build_context(
    lines: int = logtail.DEFAULT_LINES,
    selected_run_id: str = "",
    per_account_records: bool = True,
) -> Dict[str, Any]:
    """一次请求内把所有只读数据装好（账本只读一遍，避免重复 IO）。

    ``per_account_records``：账号卡是否各取各的最近一轮。总览页要（每个账号有自己的
    发送时间，"全局最近一轮"里通常只有一两个账号，拿它当所有账号的成绩会谎报"未尝试"）；
    运行记录页不要 —— 那一页讲的就是"选中的那一轮"，必须所有账号共用同一条记录。
    """
    tz = config.load_timezone()
    ledger = store.read_ledger()
    records = [store.normalize_run(raw, tz) for raw in ledger.records]
    accounts = store.account_snapshots(records)
    lock = store.read_lock()
    rate_limit = store.read_rate_limit()
    cfg = store.config_snapshot()

    last = records[0] if records else None
    tail = logtail.read_tail_with_fallback(config.log_file_candidates(), lines)
    tz_name = config.default_timezone()
    next_run = config.next_run_at()
    # 有派发器时 timer 的心跳说明不了任何事，面板要显示的是"下一个发出去的账号"。
    due = viewmodel.next_due(accounts)
    missing = missing_browser_modules()

    # 禁跑时间窗：纯 stdlib，不 import core 也能算；判断不了就当没命中。
    try:
        from core import quiet_window

        quiet = quiet_window.summary(quiet_window.check())
    except Exception:  # pragma: no cover - 面板不该因为 core 出问题就整页崩
        quiet = {"active": False, "spec": "", "until": "", "override": False, "reason": ""}

    default_dry_run = os.getenv("WEBUI_DEFAULT_DRY_RUN", "").strip().lower()
    if default_dry_run not in DRY_RUN_LEVELS:
        default_dry_run = "off"

    selected = None
    if selected_run_id:
        selected = next((r for r in records if r.run_id == selected_run_id), None)

    view = viewmodel.build(
        records=records,
        accounts=accounts,
        lock=lock,
        rate_limit=rate_limit,
        cfg=cfg,
        next_run=next_run,
        schedules=config.schedule_times(),
        quiet=quiet,
        missing_browser=missing,
        timer=store.timer_state(),
        next_due=due,
        default_dry_run=default_dry_run,
        selected=selected,
        per_account=per_account_records,
        paths={
            "运行账本": str(ledger.file or config.ledger_file()),
            "运行锁": str(config.run_lock_file()),
            "冷却文件": str(config.rate_limit_file()),
            "面板日志": tail.path,
        },
    )
    # 扫码登录那一段是面板自己的状态（浏览器会话），不属于账本视图
    view["login"] = login_view()
    # 账号卡要把"目标好友勾选"一起带上：同一批账号在页面上只出现一次
    view["account_cards"] = merge_account_rows(
        view["accounts"], view["login"].get("accounts") or []
    )
    # 可改的运行配置：值取自 .env **原文**（不是 cfg 里加工过的展示值），
    # 并且把"现在就写错了"的键标出来（那些值 next run 会让 get_config() 抛异常）。
    env_map = envfile.read_map()
    view["config_form"] = settings.form_state(env_map)
    view["config_problems"] = settings.problems(env_map)
    view["config_editable"] = len(view["config_form"])
    # 面板自己的访问口令：只说"现在是什么形式"，绝不回显口令值（见 password_panel_state）。
    view["password_panel"] = password_panel_state()

    return {
        "now_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tz": tz_name,
        "lock": lock,
        "running": lock.running,
        "rate_limit": rate_limit,
        "records": records,
        "last_run": last,
        "accounts": accounts,
        "config": cfg,
        "log_tail": tail,
        "log_lines": lines,
        "auth_enabled": bool(config.password_plain() or config.password_hash())
        and config.webui_auth_mode() != "off",
        "dry_run_levels": DRY_RUN_LEVELS,
        "dry_run_labels": DRY_RUN_LABELS,
        "default_dry_run": default_dry_run,
        "schedule_text": cfg.schedule_text,
        "next_run_text": store.fmt_stamp(next_run.isoformat(), tz) if next_run else "未配置定时",
        "next_run_iso": next_run.isoformat() if next_run else "",
        "ledger_file": ledger.file or str(config.ledger_file()),
        "ledger_bad_lines": ledger.bad_lines,
        "log_file": tail.path,
        # B 的模板全都读 `view.*`；下面这些旧键留给 /api/overview 的兼容层与测试。
        "view": view,
    }


def _payload(context: Dict[str, Any]) -> Dict[str, Any]:
    """把上下文压成 ``/api/overview`` 的 JSON（前端每 5 秒轮询它）。"""
    lock = context["lock"]
    rate_limit = context["rate_limit"]
    last = context["last_run"]
    return {
        "now": context["now_iso"],
        "tz": context["tz"],
        "running": lock.running,
        "lock": lock.as_dict(),
        "auth_enabled": context["auth_enabled"],
        "dry_run_labels": context["dry_run_labels"],
        "schedule_text": context["schedule_text"],
        "next_run_text": context["next_run_text"],
        "next_run": context["next_run_iso"],
        "cooldown": rate_limit.as_dict(),
        "last_run": last.summary() if last is not None else None,
        "runs": [record.summary() for record in context["records"]],
        "accounts": [account.as_dict() for account in context["accounts"]],
        "config": context["config"].as_dict(),
        "log_file": context["log_file"],
        "ledger_file": context["ledger_file"],
        "ledger_bad_lines": context["ledger_bad_lines"],
    }


def missing_browser_modules() -> List[str]:
    """返回**缺失**的浏览器依赖模块名（都在就返回空列表）。

    ``core.tasks`` / ``core.browser`` 对浏览器驱动的 import 是**惰性**的：本机没装
    cloakbrowser 时 ``import core.tasks`` 照样成功，只有真跑起来才在后台线程里炸。
    所以手动触发之前必须自己探一次，否则用户点一下按钮只会得到"已开始"，然后在
    日志里看到一行"浏览器启动失败" —— 那是 503 该说的事。
    """
    import importlib.util
    import sys

    missing: List[str] = []
    for name in BROWSER_MODULES:
        if name in sys.modules:
            continue
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return missing


def _reload_if_possible(module):
    """把已经导入过的 ``core.tasks`` 重新执行一次，刷新它的模块级配置快照。

    ``core.tasks`` 在模块级就 `config = get_config()` / `userData = get_userData()`，
    而 ``utils.config`` 的那两个缓存是"首次调用即冻结"。面板是常驻进程 ——
    用户改完 ``.env``（填进真实 TASKS / COOKIES）回来点"立即运行"，
    不重载就还是启动那一刻的旧账号、旧 cookie，看着像"配置改了没用"。

    重载失败（测试注入的假模块没有 ``__spec__``、或模块代码本身出错）就用原来那个：
    宁可跑一趟旧的，也不要因为"刷新配置"这个动作把任务搞黄。
    """
    import importlib

    if getattr(module, "__spec__", None) is None:
        return module
    try:
        return importlib.reload(module)
    except Exception:
        return module


def load_run_tasks():
    """惰性拿到 ``core.tasks.runTasks``；拿不到就抛 ``RunUnavailable``。

    两条路径，都是为了**可测**与**可恢复**：

    1. ``sys.modules["core.tasks"]`` 里有现成模块（测试注入的 stub、或别处已经导入过）→ 直接用；
    2. 否则 ``importlib.import_module("core.tasks")``。若上一次导入失败会在 ``sys.modules``
       里留下 ``None`` 哨兵，这里先清掉，保证"装好依赖后不重启也能恢复"。

    自己导入成功后**还要探一次浏览器依赖**（见 ``missing_browser_modules``）：core 那边是
    惰性 import，缺依赖时 import 一样成功，不探就会起一个注定失败的后台线程。
    调用方显式注入了 ``core.tasks`` 时不探 —— 那是调用方自己的选择。
    """
    import importlib
    import sys

    module = sys.modules.get("core.tasks")
    if module is None:
        if "core.tasks" in sys.modules:
            sys.modules.pop("core.tasks", None)  # 清掉失败哨兵后再试一次
        try:
            module = importlib.import_module("core.tasks")
        except ImportError as exc:
            raise RunUnavailable(str(exc)) from exc
        missing = missing_browser_modules()
        if len(missing) == len(BROWSER_MODULES):
            raise RunUnavailable(f"缺少浏览器依赖：{' / '.join(missing)}")
    else:
        # 已经有模块了（上一次跑过）——它的配置快照可能是旧的，重载一次
        module = _reload_if_possible(module)
    runner = getattr(module, "runTasks", None)
    if not callable(runner):
        raise RunUnavailable("core.tasks 里没有可调用的 runTasks")
    return runner


def run_once(run_id: str, dry_run: str, force: bool = False) -> Dict[str, Any]:
    """真正跑一轮 —— 只在后台线程里被调用。**函数内部**惰性导入 core.tasks。

    返回结构化结果而不是抛异常：调用方（线程）拿不到异常，只能读返回值。
    """
    result: Dict[str, Any] = {"run_id": run_id, "dry_run": dry_run, "ok": False}
    # 先把 .env 重新读进环境 + 清掉 utils.config 的缓存，再去拿 runTasks
    # （load_run_tasks 会顺带把 core.tasks 重载一次，让它重新做模块级快照）。
    # 顺序不能反：先重载模块、后刷新环境，快照就还是旧的。
    store.refresh_env()
    try:
        runner = load_run_tasks()
    except RunUnavailable as exc:
        result["error"] = f"无法导入 core.tasks：{exc}"
        result["hint"] = BROWSER_MISSING_HINT
        _log(f"手动触发失败（run_id={run_id}）：{result['error']}")
        return result
    except Exception as exc:  # pragma: no cover - 导入期的意外
        result["error"] = f"导入 core.tasks 时出错：{type(exc).__name__}: {exc}"
        _log(f"手动触发失败（run_id={run_id}）：{result['error']}")
        return result

    previous = os.environ.get("DRY_RUN")
    if dry_run in DRY_RUN_LEVELS:
        os.environ["DRY_RUN"] = dry_run  # core 侧若支持 DRY_RUN 环境变量即可读到
    try:
        # 按签名传参（而不是 try/except TypeError —— 万一 runTasks 内部抛 TypeError，
        # 兜底会把整轮任务再跑一遍）。账本里的 source 因此会是 webui。
        import inspect

        try:
            params = inspect.signature(runner).parameters
        except (TypeError, ValueError):  # pragma: no cover - 内建/不可反射的替身
            params = {}
        kwargs = {}
        if "dry_run" in params:
            kwargs["dry_run"] = dry_run
        if "source" in params:
            kwargs["source"] = "webui"
        # 面板的「立即运行」= 不看「到没到点」（但 `core.tasks` 那边仍会跳过
        # 今天已经真发过的账号，见 runTasks 里 force 的说明）。
        # 以前这里**从不**传 force —— 于是「立即运行」在没到点时一个账号都不发，
        # 面板还报运行成功；而交付说明里写的是「立即运行 = 强制」。
        if "force" in params:
            kwargs["force"] = bool(force)
        outcome = runner(**kwargs)
        # 返回值要接住：`runTasks` 会返回本轮汇总（被跳过 / 命中熔断 / 全账号失败
        # 都在里面）。丢掉了 `ok` 就只代表"没抛异常"，那是误导。
        if isinstance(outcome, dict):
            result["summary"] = {
                key: outcome.get(key)
                for key in ("run_id", "dry_run", "skipped", "rate_limited", "limit", "error")
                if key in outcome
            }
            if outcome.get("skipped"):
                result["ok"] = False
                result["error"] = f"本轮被跳过：{outcome['skipped'].get('reason')}"
                _log(f"手动触发的一轮被跳过（run_id={run_id}）：{result['error']}")
                return result
            accounts = outcome.get("accounts") or []
            if accounts and all((a or {}).get("status") == "error" for a in accounts):
                result["ok"] = False
                result["error"] = "全部账号都执行失败"
                _log(f"手动触发的一轮全部失败（run_id={run_id}）")
                return result
        result["ok"] = True
    except BaseException as exc:  # noqa: BLE001 - 线程里必须兜住一切
        result["error"] = f"任务执行失败：{type(exc).__name__}: {exc}"
        _log(f"手动触发异常（run_id={run_id}）：{result['error']}")
    finally:
        if previous is None:
            os.environ.pop("DRY_RUN", None)
        else:
            os.environ["DRY_RUN"] = previous
    return result


def _log(message: str) -> None:
    """把 webui 自己的动作写进同一份日志（失败就算了，绝不因此让请求 500）。"""
    try:
        from utils.logger import setup_logger

        setup_logger(level=os.getenv("LOG_LEVEL", "Info")).info(f"[webui] {message}")
    except Exception:
        pass


def _ledger_marker() -> str:
    """账本最后一条的指纹，用来判断这一轮到底有没有留下记录。"""
    try:
        from core import run_ledger

        runs = run_ledger.read_runs(limit=1)
    except Exception:  # noqa: BLE001 - 读不到就当"没有账本"，下面会走补记分支
        return ""
    if not runs:
        return ""
    last = runs[0] or {}
    return str(last.get("run_id") or last.get("started_at") or "")


def _record_failed_run(run_id: str, dry_run: str, result: Dict[str, Any], before: str) -> None:
    """这一轮连账本都没写进去时，替 core 补一条失败记录。

    面板点「立即运行」后立刻回 202/303，任务在后台线程里跑。以前线程出事
    （比如 runTasks 抛异常）只在日志里留一行，**账本一个字都没有** —— 页面于是
    显示"还没有可查看的轮次"，看起来像什么都没发生过。这里补上，让面板和
    实际一致：点过一次就是一轮，失败也是轮次。
    """
    try:
        from core import run_ledger

        after = _ledger_marker()
        if after and after != before:
            return  # core 自己写了记录，别记两遍
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        record = {
            "run_id": run_id,
            "started_at": now,
            "finished_at": now,
            "dry_run": dry_run if dry_run in DRY_RUN_LEVELS else "off",
            "source": "webui",
            "status": "error",
            "error": str(result.get("error") or "任务没有跑起来")[:500],
            "note": "面板触发的一轮没能跑起来（core 连账本都没来得及写）",
            "accounts": [],
        }
        if run_ledger.append_run(record):
            _log(f"已补记失败轮次（run_id={run_id}）：{record['error']}")
        else:
            _log(f"补记失败轮次时账本写不进去（run_id={run_id}）")
    except Exception as exc:  # noqa: BLE001 - 补记失败绝不能把线程带崩
        _log(f"补记失败轮次时出错（run_id={run_id}）：{type(exc).__name__}: {exc}")


def run_in_thread(run_id: str, dry_run: str, force: bool = False) -> Dict[str, Any]:
    """线程入口：**无论成功、失败还是崩掉，都要在账本里留下一条记录。**

    ``run_once`` 已经把异常兜成了返回值，这里再兜一层，是为了防它自己在
    兜底代码里出事（那样线程会静默死掉，面板还在说"已开始"）。
    """
    before = _ledger_marker()
    try:
        result = run_once(run_id, dry_run, force=force)
    except BaseException as exc:  # noqa: BLE001 - 线程顶层，兜住一切
        result = {
            "run_id": run_id,
            "dry_run": dry_run,
            "ok": False,
            "error": f"任务线程崩了：{type(exc).__name__}: {exc}",
        }
        _log(f"手动触发的线程崩了（run_id={run_id}）：{result['error']}")
    if not result.get("ok"):
        _record_failed_run(run_id, dry_run, result, before)
    with _RUN_RESULTS_LOCK:
        _RUN_RESULTS[run_id] = result
        for stale in list(_RUN_RESULTS)[:-8]:  # 只留最近 8 轮
            _RUN_RESULTS.pop(stale, None)
    return result


def start_run_thread(dry_run: str, run_id: str, force: bool = True) -> threading.Thread:
    """起一个 daemon 线程跑任务（进程退出时不会因为它在跑而卡住）。

    ``force=True`` 是**默认**：面板上的「立即运行」就该是「现在发」，
    不该因为时间表还没到点而什么都不做。
    """
    thread = threading.Thread(
        target=run_in_thread,
        args=(run_id, dry_run, force),
        name=f"webui-run-{run_id}",
        daemon=True,
    )
    thread.start()
    _RUN_THREADS.append(thread)
    del _RUN_THREADS[:-8]  # 只留最近 8 个引用，避免长跑进程里无限增长
    return thread


def new_run_id(now: Optional[datetime] = None) -> str:
    moment = now.astimezone() if now is not None else datetime.now()
    return f"{moment.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------- 认证 / CSRF


def _csrf_ok(request: Request) -> bool:
    """非安全方法必须是同源请求（cookie 是 same_site=lax，这层是额外保险）。"""
    # 现代浏览器（Chrome/Edge 76+、Firefox 90+、Safari 16.4+）一定会带
    # Sec-Fetch-Site；跨站表单提交是 "cross-site"，同源是 "same-origin"。
    # 这一条补上了"两个头都没有就放行"的缺口（历史上的 F12）。
    # `same-site` 也拒：同一个站点里的另一个端口/子域没有理由驱动这台机器的面板。
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        return False
    # 请求头里的 Host 是可以伪造的（DNS rebinding 的典型手法：让 evil.example
    # 解析到本机，浏览器于是带着 Host: evil.example 发请求，Origin 也自洽）。
    # 所以只认"本机自己的名字"：回环、私网 IP 字面量、机器名、绑定地址，
    # 以及显式配置的 WEBUI_TRUSTED_HOSTS。
    host = (request.headers.get("host") or "").strip()
    if host and not _host_trusted(host):
        return False
    expected = _expected_origins(request)
    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        if raw.strip().lower() == "null":
            # 沙箱 iframe / file:// 之类的不透明来源：没有理由驱动这个面板
            return False
        try:
            parts = urlsplit(raw)
        except ValueError:
            return False
        if not parts.scheme or not parts.netloc:
            continue
        return f"{parts.scheme}://{parts.netloc}" in expected
    return True  # 非浏览器客户端（curl / 测试 / 探针）不带这些头


_HOST_CACHE: Optional[set] = None


def _local_host_names() -> set:
    """本机可以被称为的名字（缓存一次，别每个请求都去解析）。"""
    global _HOST_CACHE
    if _HOST_CACHE is not None:
        return _HOST_CACHE
    names = {"127.0.0.1", "localhost", "::1", "[::1]"}
    bind = (config.webui_host() or "").strip().lower()
    if bind and bind not in ("0.0.0.0", "::"):
        names.add(bind)
    try:
        import socket

        names.add(socket.gethostname().lower())
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(str(info[4][0]).lower())
    except Exception:  # noqa: BLE001 - 解析不了就只认上面那些
        pass
    for extra in (os.getenv("WEBUI_TRUSTED_HOSTS") or "").split(","):
        extra = extra.strip().lower()
        if extra:
            names.add(extra)
    _HOST_CACHE = names
    return names


def _host_trusted(host: str) -> bool:
    """请求里的 Host 是不是"本机自己的名字"。

    这里刻意**不**一律拒绝陌生的 Host：面板是局域网工具，用户可能用机器名、
    短名、各种私网 IP 访问，硬白名单会把人挡在门外。只拒绝"带点的域名"
    （DNS rebinding 必须用一个域名，攻击者没法让公网 IP 字面量指到内网），
    以及显式配置过 `WEBUI_TRUSTED_HOSTS` 之后仍不在名单里的名字。
    域名要放行就写进 `WEBUI_TRUSTED_HOSTS=dsf.local,panel.corp`。
    """
    name = host.strip().lower()
    if name.startswith("["):  # [::1]:8787
        name = name.split("]", 1)[0].lstrip("[")
    elif ":" in name:
        name = name.rsplit(":", 1)[0]
    if not name:
        return False
    if name in _local_host_names():
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(name)
    except ValueError:
        # 不是 IP：短名（nas / testserver 之类）按本机处理，带点的域名不认
        return "." not in name
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _expected_origins(request: Request) -> set:
    host = request.headers.get("host", "")
    scheme = request.url.scheme or "http"
    origins = {f"http://{host}", f"https://{host}"}
    origins.add(f"{scheme}://{host}")
    return origins


def current_user(request: Request) -> Optional[str]:
    """已登录返回展示名；未登录返回 None。``WEBUI_AUTH=off`` 时恒为 ``"匿名（未启用认证）"``。"""
    if config.webui_auth_mode() == "off":
        return "匿名（未启用认证）"
    return auth.read_token(request.cookies.get(config.session_cookie_name()))


def _safe_next(value: str) -> str:
    """把用户给的 `next` 收敛成本站路径。

    只判断 `startswith("/")` 是不够的：`//evil.example` 是**协议相对 URL**，
    浏览器会拿它当 `https://evil.example` 跳过去 —— 于是真面板发出去的
    跳转可以被钓鱼页复用。`/\\evil.example` 同理（部分浏览器把反斜杠当斜杠）。
    只接受单个 `/` 开头的本站路径，其余一律回首页。
    """
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//") or text.startswith("/\\"):
        return "/"
    return text


#: 口令连续失败计数：{来源 IP: [连续失败次数, 罚站结束时刻]}。
#:
#: 为什么要罚站：面板是**单进程**，`/login` 每验一次口令要跑 480000 轮 PBKDF2
#: （见 `webui/password.py`），并发打几次就能把这个进程占满 —— 面板连不上事小，
#: 正在跑的那一轮发消息被拖慢事大。内存记账就够：进程重启本来就要重新登录。
_LOGIN_FAILS: Dict[str, list] = {}
_LOGIN_FAIL_LIMIT = 5            # 连错几次开始罚站
_LOGIN_FAIL_BASE = 30.0          # 首次罚站秒数，之后每多错一次翻倍
_LOGIN_FAIL_MAX = 300.0          # 罚站上限


def _login_key(request: Request) -> str:
    """按来源 IP 罚站；取不到来源就退化成全局一把（也不能不罚）。"""
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "?")


def _login_wait_left(request: Request) -> float:
    """还要罚站多少秒（0 = 可以试）。"""
    entry = _LOGIN_FAILS.get(_login_key(request))
    if not entry:
        return 0.0
    return max(0.0, float(entry[1]) - time.monotonic())


def _note_login_fail(request: Request) -> None:
    """记一次口令失败；连错到额度就开始罚站，越错越久。"""
    key = _login_key(request)
    entry = _LOGIN_FAILS.setdefault(key, [0, 0.0])
    entry[0] = int(entry[0]) + 1
    if entry[0] >= _LOGIN_FAIL_LIMIT:
        entry[1] = time.monotonic() + min(
            _LOGIN_FAIL_MAX, _LOGIN_FAIL_BASE * (2 ** (entry[0] - _LOGIN_FAIL_LIMIT)))


def _clear_login_fail(request: Request) -> None:
    _LOGIN_FAILS.pop(_login_key(request), None)


def require_login(request: Request) -> Optional[Response]:
    """页面路由的登录闸：未登录就 303 跳登录页（不抛异常，方便测试断言）。"""
    if current_user(request) is not None:
        return None
    target = request.url.path or "/"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={target}", status_code=303)


def _check_password(candidate: str) -> bool:
    """WEBUI_PASSWORD_HASH 优先于 WEBUI_PASSWORD。"""
    encoded = config.password_hash()
    if encoded:
        return password.verify_hash(candidate, encoded)
    plain = config.password_plain()
    if plain:
        return password.verify_plain(candidate, plain)
    return False


# 口令存在 `.env` 里的两种形式（面板只显示"是哪种"，**从不回显口令本身**）。
_PASSWORD_MODE_LABELS = {
    envfile.PASSWORD_MODE_PLAIN: "明文（.env 里直接能读到）",
    envfile.PASSWORD_MODE_HASH: "哈希（pbkdf2_sha256，文件里看不到原口令）",
}


def password_panel_state() -> Dict[str, Any]:
    """口令面板要显示的状态。

    刻意**不含**任何口令值或它的哈希 —— 页面被谁看到都不该泄露凭据；面板连
    Cookie 都只显示"56 项"，口令更不该回显。这里只说清三件事：现在是什么形式、
    会话能撑多久、以及"改了会怎样"。
    """
    mode = envfile.password_mode()
    live = bool(config.password_plain() or config.password_hash())
    auth_mode = config.webui_auth_mode()
    return {
        "mode": mode,
        "mode_label": _PASSWORD_MODE_LABELS.get(mode, ""),
        # `.env` 里没有、但进程环境里有（例如 systemd 的 Environment=）
        "from_environment": (not mode) and live,
        "has_password": bool(mode) or live,
        "auth_enabled": live and auth_mode != "off",
        "auth_mode": auth_mode,
        "inline": bool(mode) and mode == envfile.PASSWORD_MODE_PLAIN,
        "session_hours": round(config.session_max_age() / 3600, 1),
        "secret_configured": config.session_secret_configured(),
        "min_length": password.MIN_LENGTH,
        "max_length": password.MAX_LENGTH,
        "modes": [
            {
                "value": envfile.PASSWORD_MODE_PLAIN,
                "label": "明文（.env 里直接能读到，密码管理器里存一份）",
            },
            {
                "value": envfile.PASSWORD_MODE_HASH,
                "label": "哈希（pbkdf2_sha256，谁打开 .env 都看不到原口令）",
            },
        ],
        "default_mode": envfile.PASSWORD_MODE_PLAIN,
        "env_file": str(envfile.env_path()),
    }


def _set_session(response: Response, request: Request, username: str) -> None:
    response.set_cookie(
        config.session_cookie_name(),
        auth.issue_token(username),
        max_age=config.session_max_age(),
        httponly=True,
        # 注意参数名是 `samesite`（不是 `same_site`）—— 写错会直接 TypeError，
        # 而且只在真正登录时才炸，测试里表现为一整片登录相关用例失败。
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _login_context(
    request: Request, error: str = "", next_url: str = "/", saved: str = ""
) -> Dict[str, Any]:
    return {
        "error": error,
        "saved": saved,
        "next": _safe_next(next_url),
        "auth_enabled": bool(config.password_plain() or config.password_hash()),
        "auth_mode": config.webui_auth_mode(),
        "host": config.webui_host(),
        "loopback": config.is_loopback_host(config.normalize_bind_host(config.webui_host())),
    }


def flash_from_query(params: Any) -> Optional[Dict[str, str]]:
    """把 303 重定向带回来的查询参数翻译成一条 flash。

    C 没有服务端会话存储（单口令 + 签名 cookie），所以操作结果靠重定向的查询
    参数回传 —— 不引入任何服务端状态，也不会串台。
    """
    error = (params.get("error") or "").strip()
    if error:
        return {"level": "error", "message": error}
    saved = (params.get("saved") or "").strip()
    if saved:
        return {"level": "success", "message": saved}
    started = (params.get("started") or "").strip()
    if started:
        level = normalize_dry_run(params.get("dry_run"))
        label = DRY_RUN_LABELS.get(level, level)
        return {
            "level": "success",
            "message": f"已开始一轮任务（run_id={started}，档位={label}）。"
            "任务在后台跑，本页每 10 秒自动刷新。",
        }
    warn = (params.get("warn") or "").strip()
    if warn:
        return {"level": "warning", "message": warn}
    return None


def view_context(
    request: Request,
    lines: int = logtail.DEFAULT_LINES,
    selected_run_id: str = "",
    per_account_records: bool = True,
) -> Dict[str, Any]:
    """页面路由共用的上下文：只读数据 + 登录名 + flash。"""
    context = build_context(
        lines=lines,
        selected_run_id=selected_run_id,
        per_account_records=per_account_records,
    )
    user = current_user(request)
    context["user"] = user
    context["current_user"] = user
    context["flash"] = flash_from_query(request.query_params)
    return context


def wants_html(request: Request) -> bool:
    """表单提交要的是人看的页面，``curl`` 要的是 JSON。"""
    if (request.query_params.get("format") or "").strip().lower() == "json":
        return False
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept


def redirect_with(
    request: Request, *, path: str = "/", anchor: str = "", **params: str
) -> RedirectResponse:
    query = "&".join(f"{key}={quote(str(value))}" for key, value in params.items() if value)
    url = f"{path}?{query}" if query else path
    if anchor:
        url = f"{url}#{anchor}"
    return RedirectResponse(url=url, status_code=303)


# ---------------------------------------------------------------- 扫码登录会话

# 同一时刻只留一个扫码会话：两个 Chromium 抢同一个抖音账号会互踢，
# 而且面板上也只能显示一张二维码。历史会话（已结束的）留几个备查。
_LOGIN_SESSIONS: Dict[str, Any] = {}
_LOGIN_LOCK = threading.Lock()
_LOGIN_LATEST = ""
_LOGIN_KEEP = 3
#: 会话多久没有任何动静就当成卡死（正常会话每次推进都会刷新 updated_at）。
SESSION_STUCK_SECONDS = 20 * 60


def login_profile_dir(unique_id: str = "") -> Path:
    """扫码用的浏览器资料目录：一个账号一个，反复登录才不会天天换设备。

    目录名只保留字母数字与 ``._-``，并且**必须至少含一个字母或数字**：
    否则 ``unique_id=".."`` 会让 ``base / ".."`` 指到资料根目录的上一层，
    把 Chromium 的 user-data-dir 开在 ``logs/`` 上（既跑不起来，也脏了日志目录）。
    """
    base = os.getenv("LOGIN_PROFILE_DIR") or config.resolve_path(
        os.path.join("logs", "login-profile")
    )
    cleaned = "".join(
        ch for ch in str(unique_id or "").strip() if ch.isalnum() or ch in "._-"
    )
    name = cleaned if any(ch.isalnum() for ch in cleaned) else "default"
    return Path(base) / name


def _profile_dirs_of(snap: Dict[str, Any]) -> List[str]:
    """一个会话实际占着的资料目录（可能不止一个）。

    扫码登录成功后会把资料目录**搬**到账号名下（``adopt_dir``）。搬迁是在浏览器
    关掉之后做的，中间那段时间会话的 ``profile_dir`` 还是旧目录、而目标目录已经
    注定属于它 —— 只比 ``profile_dir`` 就会把目标目录当成空闲，让另一个会话抢先进去
    （于是 ``shutil.move`` 发现目标已存在、放弃搬迁，用户刚登录的那份资料就"丢了"）。
    """
    out: List[str] = []
    for key in ("profile_dir", "adopt_dir"):
        value = str(snap.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def _profile_in_use(path: Any, *, skip_login: str = "", skip_friend: str = "") -> str:
    """这个浏览器资料目录现在是不是被别的后台会话占着？返回人话原因（没占用返回空串）。

    Chromium 对同一个 user-data-dir 不能开两个实例（单例锁）：好友列表读取和
    扫码登录都用 ``login_profile_dir()``，两边撞上时后起的那个会莫名其妙地起不来。
    这里在**起之前**就拦住，并说清楚是谁占着。
    """
    target = os.path.abspath(str(path))
    with _LOGIN_LOCK:
        logins = list(_LOGIN_SESSIONS.items())
    for key, item in logins:
        if key == skip_login:
            continue
        snap = item.snapshot() if hasattr(item, "snapshot") else {}
        if not snap.get("active"):
            continue
        if any(os.path.abspath(p) == target for p in _profile_dirs_of(snap)):
            return "这个账号正在扫码登录"
    with _FRIEND_LOCK:
        friends = list(_FRIEND_SESSIONS.items())
    for key, item in friends:
        if key == skip_friend:
            continue
        snap = item.snapshot() if hasattr(item, "snapshot") else {}
        if not snap.get("active"):
            continue
        if any(os.path.abspath(p) == target for p in _profile_dirs_of(snap)):
            return "这个账号正在读取好友列表"
    return ""


def _latest_session():
    with _LOGIN_LOCK:
        return _LOGIN_SESSIONS.get(_LOGIN_LATEST)


def _reap_stuck_sessions(
    sessions: Dict[str, Any], lock: threading.RLock, *, label: str
) -> List[str]:
    """把"卡住不动"的会话清出注册表，返回被清掉的 key。

    正常会话每次推进都会刷新 ``updated_at``；线程若死在半路就再也不会刷新，
    这个 key 会永远 active —— ``start_login_session`` / ``start_friend_scan``
    的"已经有一次在进行"判断于是把这个账号**永久锁死**，面板上还没有任何
    入口能恢复。这里给一条兜底出路，并且写日志，不静默。
    """
    now = time.time()
    dropped: List[str] = []
    with lock:
        for key, item in list(sessions.items()):
            try:
                snap = item.snapshot() if hasattr(item, "snapshot") else {}
            except Exception:  # noqa: BLE001 - 快照都拿不到，按卡死处理
                snap = {}
            if not snap.get("active"):
                continue
            updated = float(snap.get("updated_at") or 0)
            idle = (now - updated) if updated else float(snap.get("age_seconds") or 0)
            if idle < SESSION_STUCK_SECONDS:
                continue
            try:
                item.cancel()
            except Exception:  # noqa: BLE001 - 取消失败也要把它清出去
                pass
            sessions.pop(key, None)
            dropped.append(key)
            _log(
                f"{label}会话 {key} 已经 {int(idle)} 秒没有任何动静，"
                f"判定为卡死并清理（state={snap.get('state') or '?'}），账号可以重新发起"
            )
    return dropped


def _forget_old_sessions() -> None:
    with _LOGIN_LOCK:
        ended = [
            key
            for key, item in _LOGIN_SESSIONS.items()
            if key != _LOGIN_LATEST and not item.snapshot().get("active")
        ]
        for key in ended[: max(0, len(_LOGIN_SESSIONS) - _LOGIN_KEEP)]:
            _LOGIN_SESSIONS.pop(key, None)
    _reap_stuck_sessions(_LOGIN_SESSIONS, _LOGIN_LOCK, label="扫码登录")


def _split_targets(text: str) -> Optional[List[str]]:
    """把文本框里的目标好友切成列表。空文本返回 None（= 不改动已有目标）。"""
    items: List[str] = []
    for chunk in str(text or "").replace(",", "\n").replace("，", "\n").splitlines():
        name = chunk.strip()
        if name and name not in items:
            items.append(name)
    return items or None


def login_view() -> Dict[str, Any]:
    """面板「账号与扫码登录」那一段要的全部数据（只读）。"""
    session = _latest_session()
    if session is None:
        snap: Dict[str, Any] = {
            "id": "",
            "state": "idle",
            "state_label": "未开始",
            "message": "还没有发起过扫码登录。",
            "hint": "",
            "error": "",
            "has_qr": False,
            "qr_seq": 0,
            "active": False,
            "age_seconds": 0,
            "account": None,
            "persist": None,
            # 二次验证那几个字段：没会话时面板照样会读它们
            "need_code": False,
            "code_tries": 0,
            "code_max_attempts": 0,
            "code_error": "",
            "code_text": "",
            "code_options": [],
            "has_shot": False,
            "shot_seq": 0,
        }
    else:
        snap = dict(session.snapshot())
        with _LOGIN_LOCK:
            snap["id"] = _LOGIN_LATEST

    try:
        tasks = envfile.read_tasks()
        tasks_error = ""
    except ValueError as exc:
        tasks, tasks_error = [], str(exc)
    keys = set(envfile.read_keys())
    _, friend_session = _latest_friend_session()
    scan_active = bool(friend_session is not None and friend_session.snapshot().get("active"))
    scan_owner = str(friend_session.snapshot().get("owner") or "") if friend_session else ""
    scan_items = friend_session.items() if friend_session is not None else []

    rows = []
    for task in tasks:
        uid = str(task.get("unique_id") or "").strip()
        key = envfile.cookie_key(uid) if uid else ""
        targets = list(task.get("targets") or [])
        mine = scan_owner == uid          # 这次读的列表是不是这个账号的
        rows.append(
            {
                "unique_id": uid,
                "username": str(task.get("username") or uid),
                "fingerprint": task.get("fingerprint") or "",
                "message": str(task.get("message") or ""),
                "send_time": str(task.get("time") or ""),
                "targets": [store.target_label(item) for item in targets],
                "targets_text": "\n".join(store.target_label(item) for item in targets),
                "cookie_key": key,
                "cookie_configured": bool(key) and key in keys,
                "profile_dir": str(login_profile_dir(uid)),
                "picker": friend_picker(targets, scan_items if mine else [],
                                        scan_active=scan_active and mine),
            }
        )

    return {
        "session": snap,
        "accounts": rows,
        "friend": friend_view(),
        "tasks_error": tasks_error,
        "env_path": str(envfile.env_path()),
        "profile_root": str(login_profile_dir()),
    }


def _requeue_after_relogin(unique_id: str) -> Dict[str, Any]:
    """重新登录成功后，把"今天已经办过"的记账撤掉 —— **前提是今天一条都没发出去**。

    为什么需要它：登录失效那一轮记的是 `status=error`（见 `core/tasks.py`），
    而时间表把"失败"也算办过（当天不再重试，否则坏 cookie 一天被门铃踹 288 次）。
    于是重新扫码登录之后，人还得手动点「立即运行」才会补发 —— 2026-10-09 wheat
    掉登录那次就是丢在这里。登录成功的瞬间把今天的记账撤掉，下一个 5 分钟门铃
    就会补上；**今天已经成功发过的账号绝不撤**（撤了就变成一天发两遍）。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return {"requeued": False, "reason": "no-uid"}
    try:
        from core import run_ledger, schedule
    except Exception as exc:  # noqa: BLE001 —— 补发判断失败不能让扫码登录本身失败
        return {"requeued": False, "reason": f"import-failed: {type(exc).__name__}"}

    today = schedule.day_of(schedule.now())
    seen_today = sent_today = False
    for record in run_ledger.read_runs(limit=200):
        if not str(record.get("started_at") or "").startswith(today):
            continue
        for account in record.get("accounts") or []:
            if str(account.get("unique_id") or "") != uid:
                continue
            seen_today = True
            try:
                if int(account.get("sent_ok") or 0) > 0:
                    sent_today = True
            except (TypeError, ValueError):
                sent_today = True      # 读不懂就当发过：宁可不补发，也不能补出双份
    if sent_today:
        return {"requeued": False, "reason": "already-sent"}
    if not seen_today:
        return {"requeued": False, "reason": "no-run-today"}
    try:
        state = schedule.load_state()
        changed = schedule.clear_mark(state, uid, when=schedule.now())
        saved = schedule.save_state(state) if changed else False
    except Exception as exc:  # noqa: BLE001
        return {"requeued": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"requeued": bool(changed), "saved": bool(saved),
            "reason": "" if changed else "no-mark"}


def start_login_session(
    *, unique_id: str = "", username: str = "", fingerprint: str = "",
) -> Dict[str, Any]:
    """起一次扫码登录。成功只代表"已经开跑"，登录结果要靠轮询。"""
    try:
        from core import qr_login
    except Exception as exc:  # noqa: BLE001 —— 面板不能因为缺依赖整页崩
        return {"ok": False, "error": f"扫码登录模块不可用：{type(exc).__name__}: {exc}"}

    # 先清掉卡死的旧会话，否则下面两条"正在进行"的判断会把账号永久锁死。
    _forget_old_sessions()
    with _LOGIN_LOCK:
        busy = next(
            (item for item in _LOGIN_SESSIONS.values() if item.snapshot().get("active")), None
        )
    if busy is not None:
        return {"ok": False, "error": "已经有一次扫码登录在进行，先等它结束或点「取消」"}

    profile = login_profile_dir(unique_id)
    occupied = _profile_in_use(profile)
    if occupied:
        return {
            "ok": False,
            "error": f"{occupied}（同一个浏览器资料目录只能开一个），先等它结束再来",
        }

    def on_success(result: Dict[str, Any]) -> Dict[str, Any]:
        account = result.get("account") or {}
        real_uid = str(account.get("unique_id") or "").strip() or str(unique_id or "").strip()
        nickname = str(account.get("nickname") or "").strip() or str(username or "").strip()
        if not real_uid:
            return {
                "ok": False,
                "error": "登录成功了，但页面上没读到抖音号，无法决定写进哪个变量名；"
                "请在面板里填上抖音号后重扫一次",
                "account": {"nickname": nickname},
            }
        written = envfile.save_cookie(real_uid, result.get("cookies_json") or "")
        if not written.get("ok"):
            return written
        merged = envfile.upsert_account(
            real_uid,
            username=nickname,
            fingerprint=str(fingerprint or "").strip() or None,
        )
        envfile.reload()                 # 让本进程立刻用上新的 cookie / 账号
        try:
            store.refresh_env()
        except Exception:  # noqa: BLE001
            pass
        # 新建账号时还不知道抖音号，扫码只能用 `default/` 那份资料目录；现在知道了，
        # 让 qr_login 在浏览器关掉之后把它搬到 `<抖音号>/`，否则"按账号读好友列表"
        # 打开的是另一个空目录，会误报"这个账号还没登录"。
        adopt = str(login_profile_dir(real_uid))
        if adopt == str(result.get("profile_dir") or ""):
            adopt = ""
        requeue = _requeue_after_relogin(real_uid)
        return {
            "ok": bool(merged.get("ok")),
            "error": merged.get("error") or "",
            "key": written.get("key"),
            "backup": written.get("backup"),
            "cookies": written.get("cookies"),
            "names": written.get("names"),
            "account": {"unique_id": real_uid, "nickname": nickname},
            "tasks": merged,
            "note": merged.get("message") or "",
            "adopt_profile": adopt,
            "requeue": requeue,
        }

    try:
        ttl = float(os.getenv("QR_LOGIN_TTL_SECONDS", "") or qr_login.DEFAULT_TTL_SECONDS)
    except ValueError:
        ttl = qr_login.DEFAULT_TTL_SECONDS

    try:
        session = qr_login.start_session(
            login_profile_dir(unique_id),
            fingerprint=str(fingerprint or "").strip(),
            ttl=ttl,
            on_success=on_success,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动扫码登录失败：{type(exc).__name__}: {exc}"}

    global _LOGIN_LATEST
    session_id = uuid.uuid4().hex[:12]
    with _LOGIN_LOCK:
        _LOGIN_SESSIONS[session_id] = session
        _LOGIN_LATEST = session_id
    _forget_old_sessions()
    _log(f"发起扫码登录：session={session_id} profile={session.profile_dir}")
    return {"ok": True, "session": session_id}


def cancel_login_session() -> Dict[str, Any]:
    session = _latest_session()
    if session is None or not session.snapshot().get("active"):
        return {"ok": False, "error": "当前没有正在进行的扫码登录"}
    session.cancel()
    return {"ok": True}


def submit_login_code(code: str) -> Dict[str, Any]:
    """把面板上的短信验证码转交给正在等它的扫码会话。

    会话没停在「等你填验证码」那一步时直接说清楚，不静默丢弃 —— 用户点了
    提交却什么都没发生，比报个错更难查。
    """
    session = _latest_session()
    if session is None:
        return {"ok": False, "error": "还没有发起过扫码登录"}
    if not hasattr(session, "submit_code"):
        return {"ok": False, "error": "当前这次登录用不上验证码"}
    return session.submit_code(code)


# ------------------------------------------------------------ 好友列表
#
# 「目标好友」以前是手打名字，主程序再去页面上比对 —— 名字会改会重名，
# 比对链最后还有一条子串兜底（「小明」会命中「小明明」）。现在改成：
# 无头浏览器把会话列表滚一遍读出来 → 人在面板上勾 → 勾选时拿到 uid/conv_id，
# 保存进 .env 的就是带身份的条目，发送时只按 id 匹配，名字只用来显示。

_FRIEND_SESSIONS: Dict[str, Any] = {}
_FRIEND_LOCK = threading.Lock()
_FRIEND_LATEST = ""
_FRIEND_KEEP = 3


def _latest_friend_session():
    with _FRIEND_LOCK:
        key = _FRIEND_LATEST
        if key and key in _FRIEND_SESSIONS:
            return key, _FRIEND_SESSIONS[key]
        return None, None


def _forget_old_friend_sessions() -> None:
    with _FRIEND_LOCK:
        ended = [
            key
            for key, item in _FRIEND_SESSIONS.items()
            if key != _FRIEND_LATEST and not item.snapshot().get("active")
        ]
        for key in ended[: max(0, len(_FRIEND_SESSIONS) - _FRIEND_KEEP)]:
            _FRIEND_SESSIONS.pop(key, None)
    _reap_stuck_sessions(_FRIEND_SESSIONS, _FRIEND_LOCK, label="好友列表读取")


def _target_parts(raw) -> tuple:
    """TASKS 里的一条目标 → (显示名, uid, conv_id)。字符串是旧格式，只有名字。"""
    if isinstance(raw, dict):
        name = str(
            raw.get("name") or raw.get("display") or raw.get("title")
            or raw.get("conv_id") or raw.get("uid") or ""
        ).strip()
        return name, str(raw.get("uid") or "").strip(), str(raw.get("conv_id") or "").strip()
    return str(raw or "").strip(), "", ""


def _pick_value(row: Dict[str, Any]) -> str:
    """勾选框的 value：一小段 JSON，浏览器原样提交，服务端再解析回来。"""
    payload = {"name": row.get("name") or ""}
    if row.get("uid"):
        payload["uid"] = row["uid"]
    if row.get("conv_id"):
        payload["conv_id"] = row["conv_id"]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _pick_key(row: Dict[str, Any]) -> str:
    if row.get("conv_id"):
        return "conv:" + row["conv_id"]
    if row.get("uid"):
        return "uid:" + row["uid"]
    return "name:" + str(row.get("name") or "")


def friend_picker(saved, scanned, *, scan_active: bool = False) -> Dict[str, Any]:
    """账号行里的勾选列表：已保存的排前面并勾上，读到的其余会话排后面。"""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for raw in saved or []:
        name, uid, conv_id = _target_parts(raw)
        if not (name or conv_id or uid):
            continue
        row = {"name": name or conv_id or uid, "uid": uid, "conv_id": conv_id,
               "douyin_id": "", "is_group": False, "selected": True, "saved": True}
        key = _pick_key(row)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    for item in scanned or []:
        row = {
            "name": str(item.get("name") or ""),
            "uid": str(item.get("uid") or ""),
            "conv_id": str(item.get("conv_id") or ""),
            "douyin_id": str(item.get("douyin_id") or ""),
            "is_group": bool(item.get("is_group")),
            "selected": False,
            "saved": False,
        }
        key = _pick_key(row)
        if key in seen or not (row["name"] or row["conv_id"]):
            continue
        seen.add(key)
        rows.append(row)

    for row in rows:
        row["value"] = _pick_value(row)
        row["search"] = " ".join(
            part for part in (row["name"], row["douyin_id"], row["uid"]) if part
        ).lower()
    selected = sum(1 for row in rows if row["selected"])
    new_count = sum(1 for row in rows if not row["saved"])
    has_saved = any(row["saved"] for row in rows)
    if not rows:
        summary = "点右边「读取好友列表」，把这个账号的会话读出来"
    elif not selected:
        summary = f"共 {len(rows)} 个可选，勾上要续火花的人再保存"
    elif has_saved and new_count:
        summary = f"已勾选 {selected} 个（另有 {new_count} 个是新读到的）"
    else:
        summary = f"已勾选 {selected} 个，共 {len(rows)} 个可选"
    return {"rows": rows, "selected": selected, "summary": summary,
            "has_saved": has_saved, "new_count": new_count,
            "scan_active": bool(scan_active)}


def merge_account_rows(cards, rows) -> List[Dict[str, Any]]:
    """把「扫码登录」那份账号行（cookie 状态 / 指纹 / 目标好友选择器）合进总览账号卡。

    两边的账号都来自 `TASKS`，所以正常是一一对应；这样页面上同一批账号只出现一次，
    一个账号的"状态 + 目标好友"在同一张卡里，不用上下对着看。
    """
    by_id: Dict[str, Any] = {}
    for row in rows or []:
        key = str(row.get("unique_id") or "")
        if key:
            by_id[key] = row

    merged: List[Dict[str, Any]] = []
    for card in cards or []:
        item = dict(card)
        row = by_id.pop(str(item.get("unique_id") or ""), None)
        if row:
            for key in ("cookie_configured", "cookie_names", "cookie_message",
                        "fingerprint", "picker", "targets", "targets_text"):
                if key in row:
                    item[key] = row[key]
        item["has_login_row"] = bool(row)
        merged.append(item)

    # 只在 TASKS 里、账本里还没出现过的账号：也要给一张卡，否则它的好友勾不了
    for row in by_id.values():
        item = viewmodel.build_account(row)
        for key in ("cookie_configured", "cookie_names", "cookie_message",
                    "fingerprint", "picker", "targets", "targets_text"):
            if key in row:
                item[key] = row[key]
        item["has_login_row"] = True
        merged.append(item)
    return merged


def friend_view(*, unique_id: str = "") -> Dict[str, Any]:
    """好友选择器要的实时状态（面板每 2 秒读一次；不含会话明细，明细在页面里）。"""
    key, session = _latest_friend_session()
    if session is None:
        snap: Dict[str, Any] = {
            "id": "",
            "owner": "",
            "state": "idle",
            "state_label": "还没读过",
            "message": "还没有读取过好友列表。",
            "error": "",
            "count": 0,
            "active": False,
            "age_seconds": 0,
            "scanned_all": False,
            "truncated": False,
            "groups": 0,
            "note": "",
            "steps": None,
            "visited": None,
        }
        items: List[Dict[str, Any]] = []
    else:
        snap = dict(session.snapshot())
        snap["id"] = key
        items = session.items()
    if unique_id:
        snap["mine"] = str(snap.get("owner") or "") == str(unique_id)
    else:
        snap["mine"] = True
    return {"session": snap, "total": len(items)}


def start_friend_scan(*, unique_id: str = "", fingerprint: str = "",
                      timeout: Optional[float] = None) -> Dict[str, Any]:
    """起一次"读好友列表"。成功只代表已经开跑，结果要靠轮询。"""
    try:
        from core import friend_scan
    except Exception as exc:  # noqa: BLE001 —— 缺依赖不能让面板整页崩
        return {"ok": False, "error": f"好友列表读取模块不可用：{type(exc).__name__}: {exc}"}

    # 同扫码登录：先清掉卡死的旧会话，别让账号被一条永远 active 的记录锁住。
    _forget_old_friend_sessions()
    with _FRIEND_LOCK:
        busy = next(
            (item for item in _FRIEND_SESSIONS.values() if item.snapshot().get("active")), None
        )
    if busy is not None:
        owner = busy.snapshot().get("owner") or "(默认)"
        return {"ok": False, "error": f"已经有一次好友列表读取在进行（账号 {owner}），先等它结束或点「停止」"}

    profile = login_profile_dir(unique_id)
    occupied = _profile_in_use(profile)
    if occupied:
        return {
            "ok": False,
            "error": f"{occupied}（同一个浏览器资料目录只能开一个），先等它结束再来",
        }

    try:
        limit = float(
            timeout if timeout is not None
            else (os.getenv("FRIEND_SCAN_TIMEOUT_SECONDS", "") or friend_scan.DEFAULT_TIMEOUT_SECONDS)
        )
    except (TypeError, ValueError):
        limit = friend_scan.DEFAULT_TIMEOUT_SECONDS

    try:
        session = friend_scan.start_session(
            login_profile_dir(unique_id),
            fingerprint=str(fingerprint or "").strip(),
            timeout=limit,
            owner=str(unique_id or ""),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动好友列表读取失败：{type(exc).__name__}: {exc}"}

    global _FRIEND_LATEST
    session_id = uuid.uuid4().hex[:12]
    with _FRIEND_LOCK:
        _FRIEND_SESSIONS[session_id] = session
        _FRIEND_LATEST = session_id
    _forget_old_friend_sessions()
    _log(f"发起好友列表读取：session={session_id} 账号={unique_id or '(默认)'} profile={session.profile_dir}")
    return {"ok": True, "session": session_id, "owner": str(unique_id or "")}


def cancel_friend_scan() -> Dict[str, Any]:
    _, session = _latest_friend_session()
    if session is None or not session.snapshot().get("active"):
        return {"ok": False, "error": "当前没有正在进行的读取"}
    session.cancel()
    return {"ok": True}


def parse_picked(values) -> List[Dict[str, str]]:
    """勾选框提交上来的值（每个是一小段 JSON）→ 目标列表。解析不了就当成名字。"""
    picked: List[Dict[str, str]] = []
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            picked.append({"name": text})
            continue
        if isinstance(data, dict):
            name, uid, conv_id = _target_parts(data)
            if name or uid or conv_id:
                picked.append({"name": name or conv_id or uid, "uid": uid, "conv_id": conv_id})
        elif isinstance(data, str) and data.strip():
            picked.append({"name": data.strip()})
    return picked


# ---------------------------------------------------------------- 应用工厂


class _RevalidatingStaticFiles(StaticFiles):
    """静态文件一律要求浏览器回源校验（``no-cache``，不是 ``no-store``）。

    默认的 StaticFiles 只发 ETag / Last-Modified、**不发 Cache-Control**，浏览器就会
    用启发式缓存（大约"距上次修改 10% 的时间"）自作主张地复用旧文件 —— 部署完新 HTML
    配着旧 CSS，页面看着就是错位的。带上 ``no-cache`` 后浏览器每次都用 ETag 问一次，
    没变就 304，变了立刻拿到新的。
    """

    def file_response(self, *args, **kwargs):  # pragma: no cover - 由 Starlette 调用
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title="DouYinSparkFlow 仪表盘",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = _templates()
    app.mount("/static", _RevalidatingStaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - 只在真正启动时跑
        _log(f"webui 启动：host={config.webui_host()} port={config.port()} 日志={config.log_file()}")

    # ------------------------------------------------------------ 页面

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        blocked = require_login(request)
        if blocked is not None:
            return blocked
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=view_context(request),
        )

    @app.get("/ops/runs", response_class=HTMLResponse)
    def ops_runs(request: Request) -> Response:
        blocked = require_login(request)
        if blocked is not None:
            return blocked
        selected = (request.query_params.get("run") or "").strip()
        # 这一页冻在"选中的那一轮"：账号卡不许各取各的最近一轮。
        context = view_context(request, selected_run_id=selected, per_account_records=False)
        return templates.TemplateResponse(
            request=request,
            name="runs.html",
            context=context,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        if current_user(request) is not None:
            return RedirectResponse(url="/", status_code=303)
        next_url = _safe_next(request.query_params.get("next", "/"))
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_login_context(
                request,
                next_url=next_url,
                saved=(request.query_params.get("saved") or "").strip(),
            ),
        )

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        password_field: str = Form("", alias="password"),
        next_field: str = Form("/", alias="next"),
    ) -> Response:
        if not _csrf_ok(request):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_login_context(request, error="跨站请求被拒绝（Origin/Referer 不同源）"),
                status_code=403,
            )
        next_url = _safe_next(next_field)
        wait = _login_wait_left(request)
        if wait > 0:
            # 罚站：连错 5 次开始，30 秒起、每多错一次翻倍、最多 5 分钟。
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_login_context(request,
                                       error=f"口令连续输错，请 {int(wait) + 1} 秒后再试",
                                       next_url=next_url),
                status_code=429,
            )
        if not _check_password(password_field):
            _note_login_fail(request)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_login_context(request, error="口令不正确", next_url=next_url),
                status_code=401,
            )
        _clear_login_fail(request)
        response = RedirectResponse(url=next_url, status_code=303)
        _set_session(response, request, "admin")
        return response

    @app.post("/logout")
    def logout(request: Request) -> Response:
        if not _csrf_ok(request):
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(config.session_cookie_name(), path="/")
        return response

    # ------------------------------------------------------------ JSON API

    @app.get("/api/overview")
    def api_overview(request: Request) -> Response:
        """完整快照（脚本用）：锁、冷却、全部历史轮次、账号原始视图。"""
        user = current_user(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        payload = _payload(build_context())
        payload["ok"] = True
        payload["user"] = user
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/api/ops/overview")
    def api_ops_overview(request: Request) -> Response:
        """B 的 ``static/app.js`` 每 10 秒轮询这个地址，键名不能改。"""
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        context = build_context()
        payload = viewmodel.overview_payload(
            context["view"], now_iso=context["now_iso"], tz=context["tz"]
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}")
    def api_run(request: Request, run_id: str) -> Response:
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        tz = config.load_timezone()
        ledger = store.read_ledger(limit=200)
        for raw in ledger.records:
            record = store.normalize_run(raw, tz)
            if record.run_id == run_id:
                payload = record.summary()
                payload["ok"] = True
                payload["entries"] = [
                    entry for account in record.accounts for entry in account.get("entries", [])
                ]
                return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        return JSONResponse({"ok": False, "error": f"账本里没有这一轮：{run_id}"}, status_code=404)

    @app.get("/healthz")
    def healthz() -> Response:
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ 日志

    @app.get("/ops/logs", response_class=HTMLResponse)
    def ops_logs(request: Request) -> Response:
        blocked = require_login(request)
        if blocked is not None:
            return blocked
        lines = logtail.clamp_lines(request.query_params.get("lines"))
        return templates.TemplateResponse(
            request=request, name="logs.html", context=view_context(request, lines=lines)
        )

    @app.get("/ops/log-tail", response_class=HTMLResponse)
    def ops_log_tail(request: Request) -> Response:
        blocked = require_login(request)
        if blocked is not None:
            return blocked
        lines = logtail.clamp_lines(request.query_params.get("lines"))
        keyword = (request.query_params.get("q") or "").strip()
        tail = logtail.read_tail_with_fallback(config.log_file_candidates(), lines)
        return templates.TemplateResponse(
            request=request,
            name="_log_body.html",
            context={"log_tail": tail, "log_lines": lines, "log_keyword": keyword},
        )

    @app.get("/ops/logs/download")
    def ops_log_download(request: Request) -> Response:
        blocked = require_login(request)
        if blocked is not None:
            return blocked
        for candidate in config.log_file_candidates():
            if candidate.exists():
                return FileResponse(
                    candidate,
                    media_type="text/plain; charset=utf-8",
                    filename=candidate.name,
                    headers={
                        "Cache-Control": "no-store",
                        "Pragma": "no-cache",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
        return JSONResponse(
            {"ok": False, "error": f"日志文件不存在：{config.log_file()}"}, status_code=404
        )

    # ------------------------------------------------------------ 手动触发

    @app.post("/ops/run-now")
    def ops_run_now(request: Request, dry_run: str = Form("off")) -> Response:
        """手动触发一轮。

        两种调用方：面板的 HTML 表单（要 303 回首页 + flash）和脚本的
        ``curl -d 'dry_run=off'``（要 JSON）。看 ``Accept`` 分派，行为一致。
        """
        as_html = wants_html(request)

        def fail(message: str, status: int, **extra: Any) -> Response:
            _log(f"手动触发被拒绝：{message}")
            if as_html:
                return redirect_with(request, path="/", error=message)
            return JSONResponse({"started": False, "error": message, **extra}, status_code=status)

        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        level = normalize_dry_run(dry_run)
        if level not in DRY_RUN_LEVELS:
            return fail(f"dry_run 只能是 off / select / full，收到 {dry_run!r}", 400)

        # 禁跑时间窗（默认 off）：窗口内整轮不发。这里先拦下来并说清原因，
        # 而不是让它跑起来再在 core 里被跳过（那时用户只看到"什么都没发生"）。
        # 函数内 import：core 的模块一律不在 webui 的 import 期出现。
        try:
            from core import quiet_window

            quiet = quiet_window.check()
        except Exception:  # pragma: no cover - 判断不了就放行，交给 core 再判一次
            quiet = {"active": False, "reason": "", "spec": "", "until": ""}
        if quiet.get("active"):
            detail = (
                "这段时间整轮不发（禁跑时间窗）。要到「运行配置」里改 QUIET_WINDOW，"
                "设 QUIET_WINDOW_OVERRIDE=1 可强行放行。"
            )
            if as_html:
                return redirect_with(request, path="/", error=f"{quiet['reason']}。{detail}")
            _log(f"手动触发被禁跑时间窗拒绝：{quiet['reason']}")
            return JSONResponse(
                {
                    "started": False,
                    "error": quiet["reason"],
                    "detail": detail,
                    "quiet_window": {"spec": quiet["spec"], "until": quiet["until"]},
                },
                status_code=409,
            )

        lock = store.read_lock()
        if lock.running:
            message = "已有一轮任务正在运行"
            if as_html:
                return redirect_with(request, path="/", error=f"{message}（{lock.reason or '运行中'}）")
            return JSONResponse(
                {"started": False, "error": message, "detail": lock.reason, "lock": lock.as_dict()},
                status_code=409,
            )

        try:
            load_run_tasks()  # 探测依赖是否真的可导入（拿不到就 503，不写锁不起线程）
        except RunUnavailable as exc:
            message = f"{BROWSER_MISSING_HINT}（导入失败：{exc}）"
            return fail(message, 503, detail=str(exc))
        except Exception as exc:
            return fail(f"导入 core.tasks 时出错：{type(exc).__name__}: {exc}", 503)

        run_id = new_run_id()
        # 这里**故意不写运行锁**：锁由 core.runTasks 自己用 O_EXCL 原子建，它才是
        # 唯一的持锁者。webui 若先写一把，runTasks 会把自己人当外人 →
        # [IM] ⛔ 已有一轮任务在运行 —— 本轮跳过，页面上"立即运行"永远空跑。
        start_run_thread(level, run_id)
        _log(f"手动触发一轮任务：run_id={run_id} dry_run={level}")
        if as_html:
            return redirect_with(request, path="/", started=run_id, dry_run=level)
        return JSONResponse(
            {
                "started": True,
                "run_id": run_id,
                "dry_run": level,
                "dry_run_text": DRY_RUN_LABELS.get(level, level),
                "note": "任务在后台线程里跑，页面每 10 秒刷新一次即可看到进度",
            },
            status_code=202,
        )

    # ------------------------------------------------------------ 扫码登录
    #
    # 为什么不学 B 上 noVNC：抖音的登录码就是个 178×178 的 `data:image/png;base64`
    # 图片，把它转发给面板就够了 —— 服务器不用装 Xvfb/x11vnc/websockify，
    # 浏览器也不用开着 websocket。

    @app.post("/ops/login/start")
    def ops_login_start(
        request: Request,
        unique_id: str = Form(""),
        username: str = Form(""),
        fingerprint: str = Form(""),
    ) -> Response:
        """开始扫码登录。

        故意**不收**目标好友：目标一律由下面的好友选择器勾选写入（带 uid/conv_id），
        这样发送时只按 id 匹配，不靠名字猜。老的手写名字路径保留在
        `/ops/account/targets` 的 `targets` 字段里（给命令行和老配置用）。
        """
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        result = start_login_session(
            unique_id=unique_id,
            username=username,
            fingerprint=fingerprint,
        )
        if not result.get("ok"):
            if as_html:
                return redirect_with(request, path="/", error=result.get("error") or "无法开始扫码登录")
            return JSONResponse(result, status_code=409)
        if as_html:
            return redirect_with(request, path="/", login=result["session"])
        return JSONResponse(result, status_code=202)

    @app.get("/api/ops/login")
    def api_ops_login(request: Request) -> Response:
        """面板每 2 秒轮询一次：登录到哪一步了、二维码该不该换。"""
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        view = login_view()
        view["ok"] = True
        return JSONResponse(view)

    @app.get("/ops/login/qr.png")
    def ops_login_qr(request: Request) -> Response:
        # 这一组接口统一口径：未登录回 401 JSON（以前这里回 303，
        # 浏览器把跳转后的登录页当图片解码，静默失败，看不出是没登录）。
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        session = _latest_session()
        png = session.qr_png() if session is not None else b""
        if not png:
            return JSONResponse({"ok": False, "error": "还没有二维码"}, status_code=404)
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/ops/login/shot.png")
    def ops_login_shot(request: Request) -> Response:
        """二次验证页的截图：让用户在面板上看见抖音到底在问什么。

        风控账号扫完码之后抖音会甩一道「身份验证」，光看文字提示用户不知道
        该点哪儿；把页面拍下来摆在面板上，出错时也有个现场可看。
        """
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        session = _latest_session()
        png = session.shot_png() if session is not None else b""
        if not png:
            return JSONResponse({"ok": False, "error": "还没有页面截图"}, status_code=404)
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.post("/ops/login/code")
    def ops_login_code(request: Request, code: str = Form("")) -> Response:
        """把用户收到的短信验证码送进正在等它的那次扫码会话。

        会话线程只负责"填进页面、点提交、等登录态"，这里不碰 Playwright ——
        page 不能跨线程用。
        """
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)
        result = submit_login_code(code)
        if as_html:
            if result.get("ok"):
                return redirect_with(request, path="/", warn="验证码已提交，正在等抖音放行")
            return redirect_with(request, path="/", error=result.get("error") or "验证码没送进去")
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/ops/login/cancel")
    def ops_login_cancel(request: Request) -> Response:
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)
        result = cancel_login_session()
        if as_html:
            if result.get("ok"):
                return redirect_with(request, path="/", warn="已取消扫码登录")
            return redirect_with(request, path="/", error=result.get("error") or "取消失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/ops/account/targets")
    def ops_account_targets(
        request: Request,
        unique_id: str = Form(""),
        targets: str = Form(""),
        picked: List[str] = Form(default=[]),
    ) -> Response:
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        # 两种来源：勾选列表（每个 value 是一小段 JSON）优先，其次是旧的手填文本。
        items = parse_picked(picked)
        if not items:
            items = [{"name": name} for name in (_split_targets(targets) or [])]
        result = envfile.set_targets(unique_id, items)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
        if as_html:
            if result.get("ok"):
                count = len(result.get("targets") or [])
                with_identity = sum(
                    1 for item in (result.get("targets") or []) if isinstance(item, dict)
                )
                note = f"，其中 {with_identity} 个带身份（只按 id 匹配）" if with_identity else ""
                return redirect_with(
                    request, path="/",
                    warn=f"{unique_id} 的目标好友已保存（{count} 个{note}）",
                )
            return redirect_with(request, path="/", error=result.get("error") or "保存失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/account/message")
    def ops_account_message(
        request: Request,
        unique_id: str = Form(""),
        message: str = Form(""),
    ) -> Response:
        """保存这个账号的发送内容（留空 = 恢复用全局 MESSAGE_TEMPLATE）。"""
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        result = envfile.set_account_message(unique_id, message)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
        if as_html:
            if result.get("ok"):
                text = str(result.get("message_set") or "")
                if text:
                    lines = len([line for line in text.splitlines() if line.strip()]) or 1
                    note = f"已保存（{len(text)} 字、{lines} 行）"
                else:
                    note = "已清空，这个账号改用全局模板"
                return redirect_with(
                    request, path="/", warn=f"{unique_id} 的发送内容{note}",
                )
            return redirect_with(request, path="/", error=result.get("error") or "保存失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/account/time")
    def ops_account_time(
        request: Request,
        unique_id: str = Form(""),
        time: str = Form(""),
    ) -> Response:
        """保存这个账号的发送时间（留空 = 跟 .env 的 TASK_SCHEDULE_TIMES）。

        表单字段就叫 `time`（和 `TASKS[].time`、`<input type="time">` 一致），
        但函数里立刻把它交给 `envfile.set_account_time`，不再当变量转手 ——
        本模块顶部 `import time` 拿了标准库的 `time`，同名局部变量会把它遮住。
        """
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        result = envfile.set_account_time(unique_id, time)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
        if as_html:
            if result.get("ok"):
                saved = str(result.get("time_set") or "")
                note = (f"已改成每天 {saved} 发送"
                        if saved else "已清空，这个账号跟全局默认时间")
                return redirect_with(
                    request, path="/", warn=f"{unique_id} 的发送时间{note}",
                )
            return redirect_with(request, path="/", error=result.get("error") or "保存失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/friends/scan")
    def ops_friends_scan(
        request: Request,
        unique_id: str = Form(""),
        fingerprint: str = Form(""),
    ) -> Response:
        """读一次好友列表（无头浏览器把会话列表滚一遍）。"""
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        result = start_friend_scan(unique_id=unique_id, fingerprint=fingerprint)
        if not result.get("ok"):
            if as_html:
                return redirect_with(request, path="/", error=result.get("error") or "无法开始读取")
            return JSONResponse(result, status_code=409)
        if as_html:
            return redirect_with(request, path="/", friends=result["session"])
        return JSONResponse(result, status_code=202)

    @app.get("/api/ops/friends")
    def api_ops_friends(request: Request) -> Response:
        """面板每 2 秒轮询一次：读到多少了、是不是读完了。"""
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        view = friend_view()
        view["ok"] = True
        return JSONResponse(view)

    @app.post("/ops/friends/cancel")
    def ops_friends_cancel(request: Request) -> Response:
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)
        result = cancel_friend_scan()
        if as_html:
            if result.get("ok"):
                return redirect_with(request, path="/", warn="已停止读取好友列表")
            return redirect_with(request, path="/", error=result.get("error") or "停止失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @app.post("/ops/account/remove")
    def ops_account_remove(request: Request, unique_id: str = Form("")) -> Response:
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        result = envfile.remove_account(unique_id)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
        if as_html:
            if result.get("ok"):
                return redirect_with(request, path="/", warn=result.get("message") or "已移除账号")
            return redirect_with(request, path="/", error=result.get("error") or "移除失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/config/save")
    async def ops_config_save(request: Request) -> Response:
        """保存运行配置（`webui/settings.py` 的白名单）。

        只认白名单里的键：表单里混进 `TASKS` / `COOKIES_*` 也写不进去 ——
        那些各有自己的入口，混在一起就等于给面板开了一个"绕过校验改账号"的后门。
        """
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        form = await request.form()
        env_map = envfile.read_map()
        # 只认识 `cfg_<白名单键>`：表单里混进别的名字（TASKS / COOKIES_* / 路径）
        # 一律忽略 —— 那些各有自己的入口与校验，不该从"运行配置"这个口子进来。
        submitted = {
            key: str(form.get(f"cfg_{key}") or "")
            for key in settings.BY_KEY
            if f"cfg_{key}" in form
        }
        prepared = settings.collect(submitted, env_map)
        result = settings.apply(prepared)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
        changed = result.get("changed") or {}
        if as_html:
            if result.get("ok"):
                if not changed:
                    return redirect_with(
                        request, path="/", anchor="config-panel", saved="运行配置没有变化，什么都没写"
                    )
                pairs = "、".join(f"{key}={value}" for key, value in changed.items())
                return redirect_with(
                    request,
                    path="/",
                    anchor="config-panel",
                    saved=f"已保存 {len(changed)} 项并重读 .env：{pairs}（改前的 .env 已备份）",
                )
            return redirect_with(
                request, path="/", anchor="config-panel", error=result.get("error") or "保存失败"
            )
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/password")
    async def ops_password_save(request: Request) -> Response:
        """改面板自己的访问口令（写 `.env` 的 `WEBUI_PASSWORD` / `WEBUI_PASSWORD_HASH`）。

        三件事必须一起发生，少一件就会把用户锁在门外：

        1. 校验"当前口令" —— 面板能改口令，但**必须知道旧口令**（这是防"谁摸到
           一台没锁屏的电脑就能改口令把主人踢出去"）；
        2. 写 `.env` 后立刻 `envfile.reload()` + `store.refresh_env()`，否则常驻进程
           还在用内存里的旧口令；
        3. 会话 token 里带着口令指纹，改完连**当前这个会话也失效** —— 所以直接 303
           回登录页并说一句"用新口令登录"，而不是留在一个点哪儿都跳登录的页面。
        """
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        form = await request.form()
        current = str(form.get("current") or "")
        fresh = str(form.get("new") or "")
        confirm = str(form.get("confirm") or "")
        store_mode = str(form.get("store") or envfile.PASSWORD_MODE_PLAIN).strip().lower()
        if store_mode not in (envfile.PASSWORD_MODE_PLAIN, envfile.PASSWORD_MODE_HASH):
            store_mode = envfile.PASSWORD_MODE_PLAIN

        def fail(message: str) -> Response:
            if as_html:
                return redirect_with(request, path="/", anchor="password-panel", error=message)
            return JSONResponse({"ok": False, "error": message}, status_code=400)

        if not _check_password(current):
            return fail("当前口令不正确")
        if fresh != confirm:
            return fail("两次输入的新口令不一样")
        problem = password.validate_new_password(fresh)
        if problem:
            return fail(problem)

        result = envfile.set_webui_password(fresh, store=store_mode)
        if not result.get("ok"):
            return fail(result.get("error") or "写 .env 失败")
        envfile.reload()
        try:
            store.refresh_env()
        except Exception:  # noqa: BLE001 - 重读失败不该把"已写盘"说成失败
            pass

        changed = result.get("changed") or []
        mode = result.get("mode") or envfile.PASSWORD_MODE_PLAIN
        mode_label = _PASSWORD_MODE_LABELS.get(mode, mode)
        if as_html:
            if not changed:
                return redirect_with(
                    request,
                    path="/",
                    anchor="password-panel",
                    saved="口令和现在的一模一样，什么都没写",
                )
            message = (
                f"口令已更新（存为{mode_label}），改前的 .env 已备份；"
                "所有旧会话（包括这个）都已失效，请用新口令登录"
            )
            return RedirectResponse(url=f"/login?saved={quote(message)}", status_code=303)
        return JSONResponse(
            {
                "ok": True,
                "changed": changed,
                "mode": mode,
                "backup": result.get("backup") or "",
                "note": "所有旧会话已失效，请用新口令重新登录",
            },
            status_code=200,
        )

    return app


# ---------------------------------------------------------------- 模块级 app

# 直接建实例（不读环境变量、不建目录、不连任何东西），
# 这样 `uvicorn webui.app:app` 和 `from webui.app import app` 都能直接用。
app = create_app()
