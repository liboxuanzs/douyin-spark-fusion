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
import math
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
from core import maintenance
from core import notify as notify_mod
from core import proxy as proxy_mod
from core import streak as streak_mod

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
    "请在部署机（本机）的 .venv 里安装依赖后重试；"
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
    """静态资源的版本串：跟着静态文件的 mtime 走（两套皮肤 + app.js）。

    以前这里是写死的 `20260919-3`，而 StaticFiles 只发 ETag 不发 Cache-Control，
    浏览器就按"上次修改时间"启发式缓存 —— 结果新 HTML 配上旧 CSS，页面看着像错位。
    改成跟着文件 mtime 变，每次部署 URL 都换，缓存自然失效。
    """
    global _ASSET_VERSION
    if _ASSET_VERSION:
        return _ASSET_VERSION
    stamp = 0.0
    for name in ("app.css", "app-aurora.css", "app.js"):
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

    # 火花连续天数（issue #15）：从**整份账本**算，与"选中的那一轮"无关，所以在这里算一次、
    # 传给视图模型。账本行要用归一化前的 record.raw —— store 归一化时会丢掉 entry 的
    # conv_id（好友改名后还得靠它认出是同一个人），而连续天数正是按 conv_id 归天的。
    try:
        streak_view = streak_mod.compute(
            [getattr(record, "raw", None) or {} for record in records],
            targets_by_account={
                str(getattr(item, "unique_id", "") or ""): list(getattr(item, "targets", []) or [])
                for item in accounts
                if str(getattr(item, "unique_id", "") or "")
            },
        )
    except Exception:  # pragma: no cover - 连续天数算不出来不该让整页崩
        streak_view = {}

    # 出口自检的最近结果（issue #18）：读不到就当没自检过，绝不能因此让首页 500。
    try:
        proxy_state = proxy_mod.read_state()
    except Exception:  # pragma: no cover - 只读小文件，坏成什么样都不该影响渲染
        proxy_state = {}

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
        streak=streak_view,
        proxy_state=proxy_state,
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


#: 口令连续失败计数：{来源 IP: [连续失败次数, 罚站结束时刻, 最后一次记账时刻]}。
#:
#: 为什么要罚站：面板是**单进程**，`/login` 每验一次口令要跑 480000 轮 PBKDF2
#: （见 `webui/password.py`），并发打几次就能把这个进程占满 —— 面板连不上事小，
#: 正在跑的那一轮发消息被拖慢事大。内存记账就够：进程重启本来就要重新登录。
#:
#: 记账本身也必须有上界，否则"换 IP 猜口令"就是一条内存泄漏（旧版按 IP 只增不减）。
#: 现在三层一起管：
#:   * 表最多记 `_LOGIN_FAIL_TRACK_MAX` 个来源，超了淘汰"最久没更新"的那些；
#:   * 罚站一到期就不再拦人（`_login_wait_left` 立刻回 0），一条记账在
#:     `_LOGIN_FAIL_FORGET` 秒没有新动静后整个删掉；
#:   * 另加**全局限流**：所有来源合计一分钟内失败超过 `_LOGIN_FAIL_GLOBAL_MAX`
#:     次就整体拒一小会儿（换 IP 也绕不过去），按 IP 那一层只拦得住单个来源。
_LOGIN_FAILS: Dict[str, list] = {}
_LOGIN_FAIL_LIMIT = 5            # 连错几次开始罚站
_LOGIN_FAIL_BASE = 30.0          # 首次罚站秒数，之后每多错一次翻倍
_LOGIN_FAIL_MAX = 300.0          # 罚站上限
#: 最多同时跟踪多少个来源 IP。512 个：正常运维（家里 + 公司 + 手机 + 内网反代）
#: 连零头都用不到，而每条记账只有三个数字（几十字节），512 条撑死几十 KB；
#: 再多就说明有人在换 IP 扫 —— 那时候该优先保住的是面板自己的响应。
_LOGIN_FAIL_TRACK_MAX = 512
#: 一条记账多久没有新动静就整个删掉（秒）。10 分钟：比最长罚站（5 分钟）长一倍，
#: 所以"罚站刚过又接着输错"仍然会记账累加、罚站翻倍，而早就走人的来源不会一直占着表。
_LOGIN_FAIL_FORGET = 600.0
#: 全局限流：所有来源合计每分钟最多认多少次失败。
#: 30 次/分钟：正常运维一天也输错不了这么多（真忘了口令是去改 `.env`，不是撞 30 次），
#: 而攻击者哪怕换 IP 也只有 30 次/分钟的试错额度 —— 每轮 480000 轮 PBKDF2 之下，
#: 这点额度对暴力破解毫无意义，又不会把"自己多试几次"变成锁死。
_LOGIN_FAIL_GLOBAL_MAX = 30
_LOGIN_FAIL_GLOBAL_WINDOW = 60.0
#: 最近一分钟内的失败时刻（单调钟），只留窗口内的那几次。
_LOGIN_FAIL_TIMES: List[float] = []
#: `/login` 是普通 `def` 路由，FastAPI 把它放进线程池跑 —— 上面几张表会被多个
#: 线程同时改，所以自己配一把锁（这里没有 await，用普通锁就够）。
_LOGIN_FAILS_LOCK = threading.Lock()


def _login_key(request: Request) -> str:
    """按来源 IP 罚站；取不到来源就退化成全局一把（也不能不罚）。"""
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "?")


def _prune_login_fails_locked(now: float) -> None:
    """删掉"罚站已过期且很久没动静"的记账（调用方必须持 `_LOGIN_FAILS_LOCK`）。"""
    for key, entry in list(_LOGIN_FAILS.items()):
        try:
            until = float(entry[1])
            touched = float(entry[2])
        except (IndexError, TypeError, ValueError):              # 记账坏了就当过期
            until = touched = 0.0
        if until <= now and now - touched > _LOGIN_FAIL_FORGET:
            _LOGIN_FAILS.pop(key, None)


def _trim_login_fails_locked() -> None:
    """表超过上界就按"最久没更新"淘汰（调用方必须持锁）。

    淘汰的必须是"最久没更新"而不是"最早开始罚站"的：正在扫的那个来源刚刚
    记过账，本来就应该留在表里（它正被罚站），先出局的该是早就没动静的那些。
    """
    extra = len(_LOGIN_FAILS) - _LOGIN_FAIL_TRACK_MAX
    if extra <= 0:
        return
    victims = sorted(_LOGIN_FAILS, key=lambda key: _LOGIN_FAILS[key][2])[:extra]
    for key in victims:
        _LOGIN_FAILS.pop(key, None)
    _log(
        f"口令失败的来源 IP 太多（超过 {_LOGIN_FAIL_TRACK_MAX} 个），"
        f"按「最久没更新」淘汰了 {len(victims)} 个，腾出记账空间"
    )


def _forget_old_fail_times_locked(now: float) -> None:
    """只留限流窗口内的失败时刻（调用方必须持锁）。"""
    cutoff = now - _LOGIN_FAIL_GLOBAL_WINDOW
    while _LOGIN_FAIL_TIMES and _LOGIN_FAIL_TIMES[0] <= cutoff:
        _LOGIN_FAIL_TIMES.pop(0)


def _login_wait_left(request: Request) -> float:
    """这个来源还要罚站多少秒（0 = 可以试）。"""
    now = time.monotonic()
    key = _login_key(request)
    with _LOGIN_FAILS_LOCK:
        _prune_login_fails_locked(now)
        entry = _LOGIN_FAILS.get(key)
    if not entry:
        return 0.0
    return max(0.0, float(entry[1]) - now)


def _login_global_wait_left() -> float:
    """全局限流还要等多少秒才放行（0 = 现在可以试）。

    与按 IP 罚站的区别：那一层只拦单个来源，换个 IP 就能接着猜；这一层算的是
    **所有来源合计**的失败次数，换 IP 也绕不过去。它给的是"稍后再试"而不是
    "谁被锁死"：窗口一滑过去就自动恢复，不需要任何人干预。
    """
    now = time.monotonic()
    with _LOGIN_FAILS_LOCK:
        _forget_old_fail_times_locked(now)
        if len(_LOGIN_FAIL_TIMES) < _LOGIN_FAIL_GLOBAL_MAX:
            return 0.0
        return max(0.0, _LOGIN_FAIL_GLOBAL_WINDOW - (now - _LOGIN_FAIL_TIMES[0]))


def _note_login_fail(request: Request) -> None:
    """记一次口令失败；连错到额度就开始罚站，越错越久。"""
    now = time.monotonic()
    key = _login_key(request)
    with _LOGIN_FAILS_LOCK:
        _prune_login_fails_locked(now)
        entry = _LOGIN_FAILS.get(key)
        if not entry:
            entry = [0, 0.0, now]
            _LOGIN_FAILS[key] = entry
        entry[0] = int(entry[0]) + 1
        entry[2] = now
        count = int(entry[0])
        wait = 0.0
        if count >= _LOGIN_FAIL_LIMIT:
            wait = min(
                _LOGIN_FAIL_MAX, _LOGIN_FAIL_BASE * (2 ** (count - _LOGIN_FAIL_LIMIT)))
            entry[1] = now + wait
        _LOGIN_FAIL_TIMES.append(now)
        _forget_old_fail_times_locked(now)
        recent = len(_LOGIN_FAIL_TIMES)
        _trim_login_fails_locked()
    # 日志里只有来源、次数、秒数 —— 绝不记口令原文或哈希。全局限流封顶 30 次/分钟，
    # 所以这行日志本身也不会被失败请求刷爆。
    if wait > 0:
        _log(f"口令连续失败：来源 {key} 已经错了 {count} 次，罚站 {int(wait)} 秒")
    else:
        _log(f"口令失败：来源 {key} 累计错了 {count} 次"
             f"（连错 {_LOGIN_FAIL_LIMIT} 次开始罚站）")
    if recent >= _LOGIN_FAIL_GLOBAL_MAX:
        _log(
            f"一分钟内口令失败的次数已经达到全局限流上限（{recent} 次，来源 {key} 的"
            f"这次也在内），接下来 {int(_LOGIN_FAIL_GLOBAL_WINDOW)} 秒内所有来源的"
            f"口令尝试都会被拒绝 —— 大概率是有人在撞口令，不是运维手滑"
        )


def _clear_login_fail(request: Request) -> None:
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.pop(_login_key(request), None)
    _log(f"口令验证通过，清掉来源 {_login_key(request)} 的失败记账")


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
        "session_seconds": config.session_max_age(),
        "session_text": config.session_max_age_text(),
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


def _form_flag(value: Any) -> bool:
    """表单里的开关：``1/true/on/yes`` 算开，其余（含空、``0``）算关。

    ★ 默认必须是**关**：`apply` 这类字段直接决定"删不删文件"，参数丢了/写错时
    只能更保守，绝不能因为解析成真值就把东西删了。
    """
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


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
#: "占位会话"多久算卡死（秒）。它代表的那次浏览器启动正常是秒级的事，
#: 2 分钟还没返回就说明启动线程死在半路了 —— 那时候必须把占位撤掉，
#: 否则这个账号会被一条永远 active 的记录锁住（比旧版的"资料目录永久损坏"好一点，
#: 但仍然是"永远忙"的假状态）。
START_STUCK_SECONDS = 120.0


class _StartingSession:
    """占位会话：浏览器还在启动（秒级），但**资料目录已经先被占住**。

    为什么要有这个东西 —— 这是一个真实的 TOCTOU：旧写法是「在 `_LOGIN_LOCK` 里查忙 →
    出锁调用 `qr_login.start_session()`（内部真的拉起 Chromium，耗时以秒计）→ 最后才登记
    进 `_LOGIN_SESSIONS`」。双击（或开两个标签页）时第二个请求会落在那段"还没登记"的窗口里，
    忙检查放它过去，于是**同一份** `logs/login-profile/<uid>/` 被第二个 Chromium 抢：
    抢不到的那个抛 `SingletonLock`，而它已经创建的 context 再也没有引用，那个资料目录
    从此再也打不开，只能人工去删目录。

    修法就是"先占位、再启动"：忙检查和占位登记在**同一把锁**里完成，占位对象进会话表
    （所以 `_profile_in_use` / `_reap_stuck_sessions` / 忙检查都看得见它），出锁才去启动
    浏览器。启动成功就把同一个 key 换成真会话（双击的第二个请求在占位期间就已经被拒了），
    失败/异常/用户取消/等待超时都会把这个占位撤掉。占位只实现"会话表需要的那几个成员"，
    和真会话一样能过 `snapshot()` / `cancel()`。
    """

    #: 超过这个时长还没被换成真会话就当卡死（`_reap_stuck_sessions` 会认这个属性）。
    STUCK_AFTER_SECONDS = START_STUCK_SECONDS

    def __init__(self, *, profile_dir: Any, owner: str = "", fingerprint: str = "",
                 kind: str = "login") -> None:
        now = time.time()
        self.profile_dir = str(profile_dir)
        self.adopt_dir = ""
        self.owner = str(owner or "")
        self.fingerprint = str(fingerprint or "")
        self.kind = str(kind or "login")
        self.cancelled = False
        self.started_at = now
        self._updated = now
        self._message = "正在启动浏览器（要几秒）……"

    def note(self, message: str = "") -> None:
        """记一句进展/失败原因并刷新 `updated_at`（避免被判卡死）。"""
        self._updated = time.time()
        if message:
            self._message = str(message)

    def snapshot(self) -> Dict[str, Any]:
        """给面板看的快照：字段与真会话的取同一批名字，缺的都留空/假值。"""
        now = time.time()
        return {
            "id": "",
            "state": "starting",
            "state_label": "正在启动浏览器",
            "message": self._message,
            "hint": "",
            "error": "",
            "has_qr": False,
            "qr_seq": 0,
            "active": True,
            "started_at": self.started_at,
            "updated_at": self._updated,
            "finished_at": 0.0,
            "age_seconds": int(max(0.0, now - self.started_at)),
            "profile_dir": self.profile_dir,
            "adopt_dir": self.adopt_dir,
            "owner": self.owner,
            "account": {"unique_id": self.owner} if self.owner else {},
            "persist": False,
            "need_code": False,
            "code_tries": 0,
            "code_max_attempts": 0,
            "code_error": "",
            "code_text": "",
            "code_options": [],
            "has_shot": False,
            "shot_seq": 0,
            "scanned_all": 0,
            "truncated": False,
            "groups": [],
            "note": self._message,
            "count": 0,
            "steps": [],
            "visited": 0,
        }

    def cancel(self) -> None:
        self.cancelled = True
        self.note("已取消")

    def items(self) -> List[Any]:
        return []

    def qr_png(self) -> bytes:
        return b""

    def shot_png(self) -> bytes:
        return b""


def _busy_login_error(owner: str = "") -> str:
    """双击/多标签页时第二个请求看到的中文原因（路由会把它当 409 返回）。"""
    who = str(owner or "").strip() or "(默认)"
    return (
        f"该账号的扫码登录正在进行中（{who}）"
        f"：已经有一次扫码登录在进行，先等它结束或点「取消」"
    )


def _busy_friend_error(owner: str = "") -> str:
    who = str(owner or "").strip() or "(默认)"
    return (
        f"该账号的好友列表读取正在进行中（{who}）"
        f"：已经有一次好友列表读取在进行，先等它结束或点「停止」"
    )


def _claim_start_slot(
    sessions: Dict[str, Any],
    lock: threading.Lock,
    placeholder: "_StartingSession",
    *,
    busy_error,
) -> tuple:
    """在**忙检查的同一把锁**里把占位登记进会话表；返回 `(session_id, error)`。

    这是缺陷 1 的核心：忙检查和登记必须原子。任何"检查完再出锁去启动"的写法都会
    留下第二个请求能钻的窗口。失败时 `session_id` 为空字符串、`error` 是给用户的
    中文原因（由 `busy_error` 生成，保留各自既有的文案前缀）。
    """
    with lock:
        busy = next(
            (item for item in sessions.values() if item.snapshot().get("active")), None
        )
        if busy is not None:
            try:
                snap = busy.snapshot()
            except Exception:  # noqa: BLE001
                snap = {}
            owner = str(snap.get("owner") or snap.get("account") or "")
            return "", busy_error(owner)
        session_id = uuid.uuid4().hex[:12]
        sessions[session_id] = placeholder
    return session_id, ""


def _drop_claim(sessions: Dict[str, Any], lock: threading.Lock, session_id: str) -> bool:
    """撤掉占位（启动失败、被取消、被判卡死时都要走这里）；返回是否真的撤掉了。"""
    if not session_id:
        return False
    with lock:
        return sessions.pop(session_id, None) is not None


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


def _dispatcher_accounts() -> set:
    """派发器当前这一轮正在处理哪些账号（没在跑 / 读不到锁 → 空集合）。

    ★ 2026-10-17 审计：面板的扫码登录/读好友列表 与 派发器发送之间原本**没有**
    账号级互斥 —— 同一个账号能被两只 Chromium 同时打开（两份设备指纹、同一个资料
    目录），登录成功还会在派发器跑着的时候把 `.env` 里的 cookie 换掉。这里做"看得
    见"的那半边：派发器把本轮账号写进运行锁（`core.run_ledger.RunLock(accounts=…)`），
    面板起会话前先查一遍。
    """
    try:
        state = store.read_lock()
    except Exception:                                    # noqa: BLE001 - 读不到就放行
        return set()
    if not state.running:
        return set()
    return {str(x) for x in (getattr(state, "accounts", None) or []) if str(x).strip()}


def _dispatcher_blocks(unique_id: str) -> str:
    """派发器正在处理这个账号？返回人话原因（不忙返回空串）。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return ""
    if uid in _dispatcher_accounts():
        return ("派发器正在跑这个账号（同一个资料目录不能开两只浏览器），"
                "等这一轮结束再试")
    return ""


def _panel_busy_reason() -> str:
    """面板上有没有正在进行的扫码/读好友会话？有就别让派发器起跑。

    与 `_dispatcher_accounts()` 是同一件事的两半：反方向也要拦，否则"先点扫码、
    再点立即运行"照样能撞上同一个账号。
    """
    for sessions, lock, what in ((_LOGIN_SESSIONS, _LOGIN_LOCK, "扫码登录"),
                                 (_FRIEND_SESSIONS, _FRIEND_LOCK, "读取好友列表")):
        with lock:
            items = list(sessions.values())
        for item in items:
            snap = item.snapshot() if hasattr(item, "snapshot") else {}
            if snap.get("active"):
                return (f"面板上有正在进行的{what}，等它结束再跑"
                        "（同一个账号不能开两只浏览器）")
    return ""


def _latest_session():
    with _LOGIN_LOCK:
        return _LOGIN_SESSIONS.get(_LOGIN_LATEST)


def _latest_login_slot() -> tuple:
    """最新那个扫码会话的 `(key, 会话)`；没有则 `(None, None)`。

    取消时要知道 key：占位会话要连占位一起撤掉，光 `cancel()` 会让账号继续"忙"。
    """
    with _LOGIN_LOCK:
        return _LOGIN_LATEST, _LOGIN_SESSIONS.get(_LOGIN_LATEST)


def _profile_still_locked(item: Any) -> bool:
    """会话卡死了，但它占着的资料目录是不是**真的**还被浏览器锁着？

    `cancel()` 只是置旗：卡在 `page.evaluate` 里的线程收不到（2026-10-17 审计）。
    注册表一旦被 pop，`_profile_in_use` 立刻认为目录空闲，下一次登录就会对同一个
    user-data-dir 再起一只 Chromium（SingletonLock 抢不到 → 用户看到"浏览器启动
    失败"）。真正收尾靠会话自己的守夜线程，这里只判断"能不能动它"。
    """
    try:
        snap = item.snapshot() if hasattr(item, "snapshot") else {}
        dirs = [snap.get("profile_dir"), snap.get("adopt_dir")]
    except Exception:                                    # noqa: BLE001
        return False
    for raw in dirs:
        if not raw:
            continue
        try:
            if maintenance.profile_in_use(raw):
                return True
        except Exception:                                # noqa: BLE001 - 判不了就不拦
            continue
    return False


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
            # 占位会话（浏览器还在启动）自带更短的期限：秒级的启动不该拖到 20 分钟。
            limit = SESSION_STUCK_SECONDS
            own_limit = getattr(item, "STUCK_AFTER_SECONDS", None)
            if own_limit:
                try:
                    limit = min(limit, float(own_limit))
                except (TypeError, ValueError):  # pragma: no cover - 定义写坏了就当没有
                    pass
            updated = float(snap.get("updated_at") or 0)
            idle = (now - updated) if updated else float(snap.get("age_seconds") or 0)
            if idle < limit:
                continue
            if _profile_still_locked(item):
                # 卡死的是面板这条线程，但浏览器**真的还活着**、还占着那个资料目录。
                # 先不动它（动了会让下一次登录去抢同一个目录），等守夜线程收尾。
                _log(
                    f"{label}会话 {key} 已经 {int(idle)} 秒没有动静，"
                    f"但它占用的资料目录还被浏览器锁着，先不动它（等它自己收尾）"
                )
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


def _target_labels(unique_id: str) -> List[str]:
    """TASKS 里这个账号当前的目标名（保存前记一份，用来对比"这次删掉了哪些"）。"""
    try:
        task = envfile.task_for(unique_id) or {}
    except Exception:  # noqa: BLE001
        return []
    return [store.target_label(item) for item in (task.get("targets") or [])]


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

    判定逻辑已经搬到 `core/schedule.py::requeue_after_relogin`：这是**两条**登录路径
    （面板扫码 + configTool）共用的同一件事，写在面板里就注定只会有一半生效
    （2026-10-18 issue #12）。这里只负责把它翻成面板的调用口径，不再自己判一遍。
    """
    try:
        from core import schedule
    except Exception as exc:  # noqa: BLE001 —— 补发判断失败不能让扫码登录本身失败
        return {"requeued": False, "saved": False, "reason": f"import-failed: {type(exc).__name__}"}
    return dict(schedule.requeue_after_relogin(unique_id))


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

    # 派发器正在发这个账号时不许再开浏览器（见 `_dispatcher_blocks`）。
    blocked = _dispatcher_blocks(unique_id)
    if blocked:
        return {"ok": False, "error": blocked}

    profile = login_profile_dir(unique_id)

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

    # 缺陷 1：占位与忙检查必须在同一把锁里完成 —— 出锁之后才开始启动浏览器，
    # 双击的第二个请求因此在占位期间就被明确拒绝（409 + 中文原因），不会再去抢
    # 同一份资料目录。
    claim = _StartingSession(
        profile_dir=profile, owner=str(unique_id or "").strip(),
        fingerprint=str(fingerprint or "").strip(), kind="login",
    )
    session_id, busy_error = _claim_start_slot(
        _LOGIN_SESSIONS, _LOGIN_LOCK, claim, busy_error=_busy_login_error
    )
    if busy_error:
        return {"ok": False, "error": busy_error}
    global _LOGIN_LATEST
    with _LOGIN_LOCK:
        _LOGIN_LATEST = session_id

    try:
        # 占位自己占着 profile，所以查"资料目录被占"时要跳过它自己。
        occupied = _profile_in_use(profile, skip_login=session_id)
        if occupied:
            _drop_claim(_LOGIN_SESSIONS, _LOGIN_LOCK, session_id)
            return {
                "ok": False,
                "error": f"{occupied}（同一个浏览器资料目录只能开一个），先等它结束再来",
            }

        try:
            ttl = float(os.getenv("QR_LOGIN_TTL_SECONDS", "") or qr_login.DEFAULT_TTL_SECONDS)
            if math.isnan(ttl) or math.isinf(ttl) or ttl <= 0:
                # 裸 float 能过 `1e999`（= inf）和 `nan`，而这两个值拿去当会话 TTL
                # 会让"什么时候回收这个会话"永远算不出来 —— 面板上表现为会话不回收
                # 或立刻过期。写成垃圾值就回落默认，别让它变成 500。
                raise ValueError(f"不是有限正数：{ttl!r}")
        except (TypeError, ValueError):
            ttl = qr_login.DEFAULT_TTL_SECONDS

        try:
            session = qr_login.start_session(
                login_profile_dir(unique_id),
                fingerprint=str(fingerprint or "").strip(),
                ttl=ttl,
                on_success=on_success,
            )
        except Exception as exc:  # noqa: BLE001
            # 启动失败：把占位撤掉再报错，否则这个账号会一直显示"正在进行"。
            _drop_claim(_LOGIN_SESSIONS, _LOGIN_LOCK, session_id)
            _log(f"启动扫码登录失败，已撤回占位：session={session_id} profile={profile}")
            return {"ok": False, "error": f"启动扫码登录失败：{type(exc).__name__}: {exc}"}

        with _LOGIN_LOCK:
            still_ours = _LOGIN_SESSIONS.get(session_id) is claim
            if still_ours:
                # 同一个 key 换成真会话：占位期间的那次 id 依然有效，而忙检查
                # 继续拦着后来的人，直到这次登录真的结束。
                _LOGIN_SESSIONS[session_id] = session
        if not still_ours:
            # 占位已经被撤（用户点了取消，或等待超时被判卡死）—— 刚起来的浏览器
            # 必须关掉，否则就是"没人看着的 Chromium 抓着资料目录"。
            try:
                session.cancel()
            except Exception:  # noqa: BLE001
                pass
            _log(f"扫码会话的占位已经被撤，关掉刚启动的浏览器：session={session_id}")
            return {
                "ok": False,
                "error": "这次扫码登录已经被取消（或等待太久被当成卡死清理），"
                         "刚起来的浏览器已经关掉，请重新发起",
            }
    except Exception:
        # 兜底：任何没预料到的异常都不能让占位留在表里（"永远忙"的假状态）。
        _drop_claim(_LOGIN_SESSIONS, _LOGIN_LOCK, session_id)
        raise

    _forget_old_sessions()
    _log(f"发起扫码登录：session={session_id} profile={session.profile_dir}")
    return {"ok": True, "session": session_id}


def cancel_login_session() -> Dict[str, Any]:
    session_id, session = _latest_login_slot()
    if session is None or not session.snapshot().get("active"):
        return {"ok": False, "error": "当前没有正在进行的扫码登录"}
    if isinstance(session, _StartingSession):
        # 占位：取消要连占位一起撤掉 —— 只 cancel() 的话，忙检查仍然会看到它 active，
        # 于是这个账号在浏览器启动完成之前一直"取消不掉"。
        _drop_claim(_LOGIN_SESSIONS, _LOGIN_LOCK, session_id)
        session.cancel()
        _log(f"取消扫码登录（浏览器还在启动，占位已撤）：session={session_id}")
        return {"ok": True}
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

    # 派发器正在发这个账号时不许再开浏览器（见 `_dispatcher_blocks`）。
    blocked = _dispatcher_blocks(unique_id)
    if blocked:
        return {"ok": False, "error": blocked}

    profile = login_profile_dir(unique_id)
    # 缺陷 1（好友列表这一侧同构）：占位与忙检查在同一把锁里完成，出锁才启动浏览器。
    # 否则双击两次"读取好友列表"会拿同一份 logs/login-profile/<uid>/ 去开两个 Chromium。
    claim = _StartingSession(
        profile_dir=profile, owner=str(unique_id or "").strip(),
        fingerprint=str(fingerprint or "").strip(), kind="friend",
    )
    session_id, busy_error = _claim_start_slot(
        _FRIEND_SESSIONS, _FRIEND_LOCK, claim, busy_error=_busy_friend_error
    )
    if busy_error:
        return {"ok": False, "error": busy_error}
    global _FRIEND_LATEST
    with _FRIEND_LOCK:
        _FRIEND_LATEST = session_id

    try:
        # 占位自己占着 profile，所以查"资料目录被占"时要跳过它自己。
        occupied = _profile_in_use(profile, skip_friend=session_id)
        if occupied:
            _drop_claim(_FRIEND_SESSIONS, _FRIEND_LOCK, session_id)
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
            # 启动失败：撤占位再报错，不能让这个账号一直显示"正在读取"。
            _drop_claim(_FRIEND_SESSIONS, _FRIEND_LOCK, session_id)
            _log(f"启动好友列表读取失败，已撤回占位：session={session_id} profile={profile}")
            return {"ok": False, "error": f"启动好友列表读取失败：{type(exc).__name__}: {exc}"}

        with _FRIEND_LOCK:
            still_ours = _FRIEND_SESSIONS.get(session_id) is claim
            if still_ours:
                # 同一个 key 换成真会话：忙检查继续拦着后来的人，直到这次读取结束。
                _FRIEND_SESSIONS[session_id] = session
        if not still_ours:
            # 占位已经被撤（点了「停止」，或等待超时被判卡死）—— 关掉刚起来的浏览器。
            try:
                session.cancel()
            except Exception:  # noqa: BLE001
                pass
            _log(f"好友列表读取的占位已经被撤，关掉刚启动的浏览器：session={session_id}")
            return {
                "ok": False,
                "error": "这次好友列表读取已经被停止（或等待太久被当成卡死清理），"
                         "刚起来的浏览器已经关掉，请重新发起",
            }
    except Exception:
        # 兜底：任何没预料到的异常都不能让占位留在表里（"永远忙"的假状态）。
        _drop_claim(_FRIEND_SESSIONS, _FRIEND_LOCK, session_id)
        raise

    _forget_old_friend_sessions()
    _log(f"发起好友列表读取：session={session_id} 账号={unique_id or '(默认)'} profile={session.profile_dir}")
    return {"ok": True, "session": session_id, "owner": str(unique_id or "")}


def cancel_friend_scan() -> Dict[str, Any]:
    session_id, session = _latest_friend_session()
    if session is None or not session.snapshot().get("active"):
        return {"ok": False, "error": "当前没有正在进行的读取"}
    if isinstance(session, _StartingSession):
        # 占位：连占位一起撤掉，否则浏览器启动完成之前这个账号一直"停不掉"。
        _drop_claim(_FRIEND_SESSIONS, _FRIEND_LOCK, session_id)
        session.cancel()
        _log(f"停止好友列表读取（浏览器还在启动，占位已撤）：session={session_id}")
        return {"ok": True}
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
                headers={"Retry-After": str(int(wait) + 1)},
            )
        # 全局限流：按 IP 那一层换个 IP 就绕过去了，这一层算的是**所有来源合计**的
        # 失败次数 —— 撞口令的人换 IP 也没用。给的是"稍后再试"而不是"谁被锁死"：
        # 窗口一滑过去就自动恢复，正常运维输错几次根本碰不到这个上限。
        global_wait = _login_global_wait_left()
        if global_wait > 0:
            seconds = int(global_wait) + 1
            _log(
                f"口令尝试被全局限流挡下（来源 {_login_key(request)}）："
                f"一分钟内失败次数已达上限 {_LOGIN_FAIL_GLOBAL_MAX}，请 {seconds} 秒后再试"
            )
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context=_login_context(
                    request,
                    error=f"最近一分钟口令输错的次数太多，请 {seconds} 秒后再试"
                          f"（全局限流：所有来源合计每分钟最多 {_LOGIN_FAIL_GLOBAL_MAX} 次失败）",
                    next_url=next_url,
                ),
                status_code=429,
                headers={"Retry-After": str(seconds)},
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
        # ★ 先吊销再删 cookie：token 是无状态的，只删 cookie 等于"这台浏览器
        #   不带了"，复制走同一份 cookie 的人在有效期内照样能进。这里把 token
        #   记进进程内黑名单（`auth.is_revoked`），删 cookie 只是顺手清理。
        auth.revoke_token(request.cookies.get(config.session_cookie_name()))
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

        busy = _panel_busy_reason()
        if busy:
            return fail(busy, 409)

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

        # 勾选列表是**整表覆盖**：这次没勾上的旧目标会被删掉。先记下原来有哪些，
        # 保存后把"删掉了谁"一并写进提示 —— 否则用户只看到"已保存 N 个"，
        # 少掉的目标要到下一轮发送时才发现（2026-10-11 示例号 就是这样少过 2 个）。
        before = _target_labels(unique_id)
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
                saved_targets = result.get("targets") or []
                count = len(saved_targets)
                with_identity = sum(
                    1 for item in saved_targets if isinstance(item, dict)
                )
                note = f"，其中 {with_identity} 个带身份（只按 id 匹配）" if with_identity else ""
                after = [store.target_label(item) for item in saved_targets]
                removed = [name for name in before if name and name not in after]
                if removed:
                    shown = "、".join(removed[:3]) + ("…" if len(removed) > 3 else "")
                    note += f"；这次没勾选、已移除 {len(removed)} 个：{shown}"
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

    # ---- 磁盘与资料目录（只读盘点 + 手动清理，默认只预演） ----------------------

    @app.get("/api/ops/maintenance")
    def api_ops_maintenance(request: Request) -> Response:
        """「磁盘与资料目录」区块：面板**展开时**拉一次，不轮询。

        刻意不并进首页渲染：`core.maintenance.profile_report()` 会把每个资料目录
        递归走一遍（线上两个目录就一千多 MB、几万个缓存文件），并进首页等于每次
        翻页都白扫一遍盘。用户点了才付这份钱。
        """
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        try:
            store.refresh_env()          # 面板可能刚改过 .env，名单要按最新的读
        except Exception:  # noqa: BLE001
            pass
        tasks, _tasks_error = store.read_tasks_env()
        # 昵称只是给人看的（目录名是抖音号，光看号认不出是谁）；名单的**权威**判定
        # 在 core.maintenance.known_ids() 里，读不到它会返回 ok=False。
        names = {
            str(item.get("unique_id") or ""): str(item.get("username") or "")
            for item in tasks
            if str(item.get("unique_id") or "")
        }
        payload = viewmodel.maintenance_view(names=names)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post("/ops/maintenance/caches")
    def ops_maintenance_caches(request: Request, apply: str = Form("0")) -> Response:
        """清**可再生缓存**（`Cache` / `Code Cache` / `GPUCache` …）。

        安全线全在 `core.maintenance`：只删白名单里的目录/文件，绝不动
        `Cookies` / `Local Storage` / `Local State`；在跑的资料目录跳过；
        **账号名单读不到时一个都不删**（返回 ok=False）。`apply` 默认 "0" = 只预演。
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

        try:
            store.refresh_env()
        except Exception:  # noqa: BLE001
            pass
        # 名单读不到时 known_ids() 会把错误带出来，prune_caches 见到错误直接空手而归
        # —— 这里不再自己判断，避免出现"两套安全线"。
        payload = viewmodel.prune_view(
            maintenance.prune_caches(apply=_form_flag(apply)), what="缓存"
        )
        if as_html:
            if payload.get("ok"):
                return redirect_with(
                    request, path="/", anchor="maintenance-panel",
                    saved=payload.get("headline") or "已处理",
                )
            return redirect_with(
                request, path="/", anchor="maintenance-panel",
                error=payload.get("error") or "清理缓存失败",
            )
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 409)

    @app.post("/ops/maintenance/orphans")
    def ops_maintenance_orphans(request: Request, apply: str = Form("0")) -> Response:
        """删**孤儿资料目录**（不在 `TASKS` 里 + 闲置超过宽限期 + 没在跑）。

        `apply` 默认 "0" = 只预演；真删要走前端 `confirm()`。宽限期用
        `core.maintenance.ORPHAN_GRACE_DAYS`（7 天），防的是"刚扫码登录、
        还没写进 TASKS 的账号目录被当成孤儿删掉"。
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

        try:
            store.refresh_env()
        except Exception:  # noqa: BLE001
            pass
        payload = viewmodel.prune_view(
            maintenance.prune_orphans(apply=_form_flag(apply)), what="孤儿目录"
        )
        if as_html:
            if payload.get("ok"):
                return redirect_with(
                    request, path="/", anchor="maintenance-panel",
                    saved=payload.get("headline") or "已处理",
                )
            return redirect_with(
                request, path="/", anchor="maintenance-panel",
                error=payload.get("error") or "清理孤儿目录失败",
            )
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 409)

    @app.post("/ops/account/enabled")
    def ops_account_enabled(request: Request, unique_id: str = Form(""),
                            enabled: str = Form("")) -> Response:
        """停用 / 启用一个账号（账号卡上的「停用」「启用」）。

        ★ 停用只写 `TASKS[].enabled = false`：不派发、不自动发，但昵称、好友、
          发送时间、指纹和 cookie 全留着 —— 和「移除」是两件事（那个连 cookie 一起删，
          想再发就得重新扫码）。当天已经发过的记录**不动**：恢复后如果今天还没发，
          下一次门铃就会补上（不需要手动补发）。

        表单里的 `enabled` 用 "1"/"0"：HTML 的 checkbox 不勾就不提交，两个都当按钮
        提交更省事，也不用在模板里写隐藏域兜底。
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

        flag = str(enabled or "").strip().lower() in ("1", "true", "on", "yes", "enable", "enabled")
        uid = str(unique_id or "").strip()
        result = envfile.set_account_enabled(uid, flag)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
            _log(f"账号 {uid} 已{'启用' if flag else '停用'}（TASKS[].enabled={'true' if flag else 'false'}）")
        if as_html:
            if result.get("ok"):
                return redirect_with(
                    request, path="/", anchor=f"account-{uid}",
                    saved=result.get("message") or ("已启用" if flag else "已停用"),
                )
            return redirect_with(request, path="/", error=result.get("error") or "操作失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/account/proxy")
    def ops_account_proxy(request: Request, unique_id: str = Form(""),
                          proxy: str = Form(""), clear: str = Form("")) -> Response:
        """保存这个账号的出口（issue #18）。

        表单里 `proxy` 留空**不等于**清空：那多半是点错了按钮。要改回全局得勾
        `clear=1`（卡片上的「改回全局」按钮），这样"手滑保存"不会把账号的出口悄悄
        换回另一个 IP —— 出口一变，轻则掉登录，重则账号被风控盯上。
        """
        as_html = wants_html(request)
        uid = str(unique_id or "").strip()
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）")
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        wanted = "" if _form_flag(clear) else str(proxy or "").strip()
        if not wanted and not _form_flag(clear):
            result = {"ok": False, "error": "没填出口地址；要改回全局请点「改回全局」"}
        else:
            result = envfile.set_account_proxy(uid, wanted)
        if result.get("ok"):
            envfile.reload()
            try:
                store.refresh_env()
            except Exception:  # noqa: BLE001
                pass
            _log(f"账号 {uid} 的出口已更新：{result.get('proxy_text') or '直连'}")
        if as_html:
            if result.get("ok"):
                return redirect_with(request, path="/", anchor=f"account-{uid}",
                                     saved=result.get("message") or "出口已保存")
            return redirect_with(request, path="/", error=result.get("error") or "保存出口失败")
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/account/proxy-check")
    def ops_account_proxy_check(request: Request, unique_id: str = Form("")) -> Response:
        """用这个账号的出口访问一次回显服务，顺便记下结果（issue #18）。

        只在人点了按钮时联网一次：发送链路里绝不能多一次外网请求（那是每轮都要付的
        风险与时间，还会把"发送失败"和"自检失败"混成一团）。

        返回值里**没有出口地址原文**，只有遮掩后的 host:port 与 IP。
        """
        uid = str(unique_id or "").strip()
        if current_user(request) is None:
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        own = ""
        try:
            own = envfile.account_proxy(uid)
        except Exception:  # noqa: BLE001 - 账号被删了也照常返回"失败"而不是 500
            own = ""
        try:
            global_proxy = str(get_config().get("proxyAddress") or "")
        except Exception:  # noqa: BLE001
            global_proxy = ""
        effective = own or global_proxy

        result = proxy_mod.check(effective)
        state = proxy_mod.read_state()
        accounts = state.get("accounts") if isinstance(state.get("accounts"), dict) else {}
        previous = accounts.get(uid) if isinstance(accounts.get(uid), dict) else {}
        changed = bool(previous.get("ip")) and previous.get("ip") != result.get("ip")
        at = iso(now())
        accounts[uid] = {
            "ip": result.get("ip") or "",
            "at": at,
            "via": result.get("via") or "",
            "ok": bool(result.get("ok")),
            "error": str(result.get("error") or "")[:200],
        }
        state["accounts"] = accounts
        proxy_mod.write_state(state)

        if result.get("ok"):
            text = f"出口 {result.get('ip')}（{result.get('via')}）"
            if changed:
                text += f"；和上次的 {previous.get('ip')} 不一样 —— 出口 IP 变了是掉登录的头号起因"
        else:
            text = f"自检失败：{result.get('error') or '连不上'}"
        _log(f"出口自检 {uid}：{text}")
        return JSONResponse({
            "ok": bool(result.get("ok")),
            "ip": result.get("ip") or "",
            "via": result.get("via") or "",
            "changed": changed,
            "previous": str(previous.get("ip") or ""),
            "text": text,
            "at": at,
        }, status_code=200)

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

    @app.post("/ops/account/requeue")
    def ops_account_requeue(request: Request, unique_id: str = Form("")) -> Response:
        """手动撤掉"今天已经办过"的记账，让当天的门铃补发一次。

        为什么要有这条**明路**（2026-10-18 issue #12）：账号当天掉登录之后，时间表把
        `error` 也当成办过（当天不再重试，否则坏 cookie 一天被门铃踹 288 次），人要补发
        只能靠"重新登录"这个动作顺带撤账。两条登录路径现在都会自动撤账，但要是哪条都没
        走到（登录窗口没抓到抖音号、cookie 是手工贴的……），用户唯一的办法是手改
        `logs/schedule_state.json` —— 一个内部状态文件，文档里没有面向用户的说明。

        安全性靠判定本身：**今天已经成功发过的账号会被拒绝**（`already-sent`），
        所以这个按钮造不出"一天发两遍"。
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

        result = _requeue_after_relogin(unique_id)
        reason = str(result.get("reason") or "")
        if result.get("requeued"):
            message = ("已撤回今天的记账 —— 下一次门铃（≤5 分钟）会补发；"
                       "想马上发就点「立即运行」。")
            if as_html:
                return redirect_with(request, path="/", warn=message)
            return JSONResponse({"ok": True, "message": message, **result})
        if reason == "already-sent":
            message = "今天已经成功发过这个账号了，不撤账、不补发（避免一天发两遍）。"
        elif reason == "no-run-today":
            message = "今天还没有跑过这个账号，门铃本来就会发它，不需要补发。"
        elif reason == "no-mark":
            message = "今天没有可撤回的记账（可能刚刚已经被撤过）。"
        else:
            message = f"补发失败：{reason or '未知原因'}"
        # 被拒绝就是 400：调它的脚本/curl 要看得出"这一下没生效"，
        # 不能像成功那样只给一个 200（同一条路线上的 /ops/account/remove 也是这个口径）。
        if as_html:
            level = "error" if message.startswith("补发失败") else "warn"
            return redirect_with(request, path="/", **{level: message})
        return JSONResponse({"ok": False, "error": message, **result}, status_code=400)

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
        # `store.refresh_env()` 会重读 .env、把新值灌进本进程的 os.environ（`config.load()`
        # 读到的东西）。文件写成功但它失败是可能的（读权限、编码、并发改坏），
        # 而失败时页面**不能**说"已重读" —— 那会让用户以为新配置已经在跑，
        # 实际还在用旧值，直到重启面板。返回值以前被丢掉了，这里必须留着。
        reloaded = False
        if result.get("ok"):
            envfile.reload()
            try:
                reloaded = bool(store.refresh_env())
            except Exception:  # noqa: BLE001
                reloaded = False
        result["env_reloaded"] = reloaded
        changed = result.get("changed") or {}
        if as_html:
            if result.get("ok"):
                if not changed:
                    return redirect_with(
                        request, path="/", anchor="config-panel", saved="运行配置没有变化，什么都没写"
                    )
                pairs = "、".join(f"{key}={value}" for key, value in changed.items())
                # ok=True 不变：文件确实写进去了（还备份了旧文件），失败的是"本进程
                # 重新读进内存"这一步 —— 两者必须分开说，否则用户以为整个保存都失败了，
                # 会去反复点保存。
                reload_text = (
                    "并重读 .env"
                    if reloaded
                    else "但本进程重读失败（重启面板后生效）"
                )
                return redirect_with(
                    request,
                    path="/",
                    anchor="config-panel",
                    saved=f"已保存 {len(changed)} 项{reload_text}：{pairs}（改前的 .env 已备份）",
                )
            return redirect_with(
                request, path="/", anchor="config-panel", error=result.get("error") or "保存失败"
            )
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.post("/ops/notify/test")
    def ops_notify_test(request: Request) -> Response:
        """发一条测试通知：确认 webhook 地址写对了。

        不发送任何消息、不碰运行锁；最坏等满 NOTIFY_TIMEOUT_SECONDS（默认 10 秒）。
        失败时把原因原样带回去 —— 用户正在调地址，HTTP 码与接口报错比"失败"两个字有用。
        """
        as_html = wants_html(request)
        if current_user(request) is None:
            if as_html:
                return RedirectResponse(url="/login?next=/", status_code=303)
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        if not _csrf_ok(request):
            if as_html:
                return redirect_with(
                    request, path="/", error="跨站请求被拒绝（Origin/Referer 不同源）"
                )
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)

        try:
            ok, detail = notify_mod.send_test()
        except Exception as exc:  # noqa: BLE001 - 测试入口自身不能 500
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        _log(("测试通知已发出：" if ok else "测试通知失败：") + detail)
        if as_html:
            if ok:
                return redirect_with(request, path="/", anchor="config-panel", saved=detail)
            return redirect_with(request, path="/", anchor="config-panel", error=detail)
        return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 400)

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
