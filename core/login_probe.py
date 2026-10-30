"""撤账之前先问一句：这个账号现在还登录着吗？

为什么需要（2026-10-30 wheat 事故）
------------------------------------
`POST /ops/account/requeue` 的语义是「我重新登录好了，把今天那次失败记账撤掉，
等下一次门铃补发」。可这条路径从来不检查「重新登录」是否真的发生过：用户点了
一下，账就撤了；下一轮门铃开浏览器才发现登录早已失效 —— 白跑一轮、账本多一条
`login_dead`，人还在那边等补发。事后在日志里解释不管用，**撤账之前**先问一句
才管用。

成本与边界
----------
一次浏览器上下文 + 一次 IM 页加载（约 5~15 秒），所以只给**人点的那条路径**用；
定时器那一轮照旧直接开浏览器 —— 它本来就要付这份成本，没必要再多开一次。

只读：不写 `browser_state` 快照、不回写 `.env` 的 cookie、不碰时间表与账本。
探测结果随上下文关闭一起丢掉 —— 死会话里抖音可能已经把 sessionid 换成一份匿名
的，存下来只会把真凭据盖掉（同 `core/tasks.py` 收尾那段注释的理由）。

判定本身**不在这里**：页面怎么算已登录是 `core/douyin_im.check_login` 的活
（SSR + DOM 反证 + cookie 佐证，2026-10-09 那次"扫码页上谎报已登录"就是它修的）。
这一层只负责「开一个和发送时一样的上下文，把那个函数跑一遍」。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from utils.config import get_config, get_userData
from utils.logger import setup_logger
from core import browser_state, cookie_utils
from core.browser import close_context, get_browser
from core.douyin_im import CHAT_URL, attach, check_login

logger = setup_logger("login_probe")

#: 等页面给出结论的上限（秒）。超过就当"未知"—— 拦人不能拦到用户怀疑面板坏了。
PROBE_TIMEOUT_SECONDS = 20.0
#: 每次复判之间给的静默时间（毫秒）。首屏脚本与弹窗都要时间。
PROBE_SETTLE_MS = 1200

DETAILS = {
    "LOGGED_IN": "页面已登录",
    "EXPIRED": "登录已失效（浏览器里有 sessionid，服务端已经不认了）",
    "NOT_LOGGED_IN": "没有登录（IM 页停在未登录 / 扫码状态）",
    "UNKNOWN": "页面既没给出登录框也没给出用户信息，判不出来",
}


def _out(started: float, state: str, detail: str = "", *, blocked: bool = False,
         **extra) -> Dict[str, Any]:
    out = {
        "state": str(state or "UNKNOWN"),
        "detail": str(detail or DETAILS.get(str(state or ""), "")),
        "blocked": bool(blocked),          # True = 根本没探成，不能拿来拒绝撤账
        "elapsed": round(time.time() - started, 1),
    }
    out.update(extra)
    return out


def _tune(context) -> None:
    """与发送时同一套超时（`core/tasks.do_user_task` 里那两行）。失败不影响判定。"""
    try:
        timeout_ms = int(get_config().get("browserActionTimeout") or 20000)
    except Exception:  # noqa: BLE001 —— 配置读不出来就用默认值，探测不能因此不跑
        timeout_ms = 20000
    for setter in ("set_default_navigation_timeout", "set_default_timeout"):
        try:
            getattr(context, setter)(timeout_ms)
        except Exception:  # noqa: BLE001
            pass


def _judge(page, mon, started: float, timeout: float) -> Dict[str, Any]:
    """反复问页面"登录着吗"，直到有结论或超时。"""
    deadline = time.time() + max(0.0, float(timeout))
    verdict: Dict[str, Any] = {}
    for _ in range(64):                          # 硬上限：timeout 被传成极大值时也别死循环
        try:
            page.wait_for_timeout(PROBE_SETTLE_MS)
        except Exception:  # noqa: BLE001 —— 页面已经关了/崩了，下面照样给结论
            pass
        try:
            verdict = check_login(page, mon) or {}
        except Exception as exc:  # noqa: BLE001
            return _out(started, "UNKNOWN", f"判定出错（{type(exc).__name__}: {exc}）")
        state = str(verdict.get("state") or "UNKNOWN")
        if state != "UNKNOWN" or time.time() >= deadline:
            break
    state = str(verdict.get("state") or "UNKNOWN")
    detail = DETAILS.get(state, DETAILS["UNKNOWN"])
    user_id = verdict.get("user_id")
    nickname = verdict.get("nickname")
    if state == "LOGGED_IN":
        bits = [detail]
        if user_id:
            bits.append(f"user_id={user_id}")
        if nickname:
            bits.append(f"昵称={nickname}")
        detail = "  ".join(bits)
    return _out(started, state, detail,
                user_id=user_id, nickname=nickname,
                line=str(verdict.get("log") or ""))


def probe(unique_id, *, users: Optional[List[dict]] = None, browser=None,
          timeout: Optional[float] = None) -> Dict[str, Any]:
    """探一次登录态 → `{state, detail, blocked, elapsed, user_id?, nickname?, line?}`。

    `state` 取值与 `core/douyin_im.check_login` 一致：
    `LOGGED_IN` / `EXPIRED` / `NOT_LOGGED_IN` / `UNKNOWN`。

    * `blocked=True` 表示**没探成**（账号不存在、浏览器起不来……）—— 调用方只能
      当"没结论"处理，不能拿去拒绝撤账：探不了不等于登录死了。
    * `users` / `browser` 只给测试注入；不传就现读配置、自己起浏览器。
    * 绝不抛异常：它跑在面板的请求线程里，抛出去就是一个 500。
    """
    started = time.time()
    uid = str(unique_id or "").strip()
    if not uid:
        return _out(started, "UNKNOWN", "没有账号", blocked=True)

    try:
        if users is None:
            users = get_userData()
    except Exception as exc:  # noqa: BLE001
        return _out(started, "UNKNOWN",
                    f"读不到账号配置（{type(exc).__name__}: {exc}）", blocked=True)
    user = None
    for item in users or []:
        if isinstance(item, dict) and str(item.get("unique_id") or "").strip() == uid:
            user = item
            break
    if user is None:
        return _out(started, "UNKNOWN", f"TASKS 里没有账号 {uid}", blocked=True)

    snapshot = browser_state.read(uid)
    try:
        cookies, _stats = cookie_utils.filter_cookies(user.get("cookies") or [],
                                                     domains_only=False)
    except Exception:  # noqa: BLE001
        cookies = list(user.get("cookies") or [])
    if not cookies and not snapshot:
        # 两条路都没有凭据：不必起浏览器也知道结果（而且要起也起不出登录态）
        return _out(started, "NOT_LOGGED_IN",
                    "既没有 .env 里的 cookie，也没有浏览器状态快照")

    context = None
    own_browser = browser is None
    engine = browser
    try:
        if engine is None:
            # proxy 口径与发送时一致（issue #18）：None=回落全局，空串=强制直连
            engine = get_browser(user.get("fingerprint") or None,
                                 proxy=user.get("proxy") or None)
        from core.tasks import _new_context        # 函数内 import：避免与 tasks 形成导入环
        context, _used = _new_context(engine, snapshot)
        _tune(context)
        page = context.new_page()
        mon = attach(page)                          # ★ 必须先挂钩子再导航
        try:
            page.goto(CHAT_URL, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 —— 与发送路径同款：导航失败也继续判定
            logger.warning(f"[LOGIN-PROBE] ⚠️ 导航异常（继续尝试判定）：{exc}")
        return _judge(page, mon, started,
                      PROBE_TIMEOUT_SECONDS if timeout is None else timeout)
    except Exception as exc:  # noqa: BLE001
        return _out(started, "UNKNOWN",
                    f"探测没跑起来（{type(exc).__name__}: {exc}）", blocked=True)
    finally:
        if context is not None:
            close_context(context)
        if own_browser and engine is not None:
            close_context(engine)
