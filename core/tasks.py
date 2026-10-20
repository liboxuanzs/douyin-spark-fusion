"""一个账号的完整流程 + 整轮编排。

融合改造（相对上游 C）：
* **dry-run**（§5）：`runTasks(dry_run=...)` 三档 off/select/full，档位一路透传到
  `DouyinIM.type_and_send`；空跑同样走严格确认，所以空跑结果≈真跑结果。
* **限流熔断**（§6）：发送回执 + 页面提示两个来源一起看（`core/limit_guard.py`），
  命中即停下本轮剩余目标并写冷却；冷却期内**连新的一轮都不启动**。
* **结构化结果 + 运行账本**：`runTasks()` 返回本轮汇总，并把同样的结构追加进
  `logs/runs.jsonl`（`core/run_ledger.py`），仪表盘读它。上游这里是没有返回值的，
  所以"发了几条、失败几个"只能靠翻日志。
* **运行锁**：跨进程互斥（定时任务与 web 手动触发是两个进程），
  绝不允许两个 Chromium 同时对同一个账号操作。
"""
import os
import time
import traceback

from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message
from core.browser import close_context, get_browser
from core.douyin_im import (
    DouyinIM, STATUS_READY, STATUS_EXPIRED, STATUS_LOGGED_OUT, STATUS_LOGIN_LOST,
    target_name, target_refs,
)
from core import cookie_utils
from core import limit_guard
from core import quiet_window
from core import run_ledger as ledger
from core import schedule
from core.util_time import (
    DRY_RUN_OFF, cooldown_human, human_duration, iso, now, resolve_dry_run,
)


config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))

ENTRY_CAP = 500      # 单个账号最多记这么多条发送明细，防止账本被超长名单撑爆


def _gate_reason(status):
    """门禁终态 → 人话原因。终端态都要显式打印，方便从日志一眼看出是哪种失败。"""
    return {
        "LOGGED_OUT": "未登录（没有 sessionid）",
        "EXPIRED": "登录已失效（有 sessionid 但服务端不认）",
        "LOGIN_LOST": "运行期掉登录",
        "TIMEOUT": "等待超时",
        "ERROR": "内部错误",
    }.get(status, status)


# 门禁判出「这个账号的登录态已经死了」的终态。
#
# 这类失败**重试没有任何意义**：页面再开一百次，服务端认的还是同一份作废的
# sessionid；而每 5 分钟敲一次门的定时器（`deploy/douyin-spark-fusion-daily.timer`
# 的 `OnCalendar=*:0/5`）真的会把它开一百次 —— 2026-10-10 示例号 实测：20:48 起
# 每 5 分钟一次「开浏览器 → goto /chat → 判 EXPIRED」，一天最多 288 次，
# 全部是同一个形状。对一个正在被风控盯着的账号，这些重复请求只有坏处。
#
# 所以这类失败按 `error` 记账（`_mark_status`）：占掉当天名额、当天不再重试；
# 面板计入「需要处理」并提示重新扫码；用户重新登录成功后，webui 的扫码回调会
# 撤掉当天记账，下一个门铃**自动补发**（见 webui/app.py 的扫码成功回调）。
#
# 只有这类「人不动手就不会好」的失败才配占掉当天名额；TIMEOUT / ERROR 这类
# 可能是网络抖动的，仍然记 `skipped`，让门铃当天继续试。
GATE_DEAD_STATUSES = (STATUS_EXPIRED, STATUS_LOGGED_OUT, STATUS_LOGIN_LOST)


def _gate_death_confirmed(im, res, username) -> bool:
    """门禁判出「登录态已死」之后再向服务端确认一次：真死了才认账。

    为什么要多问一次：判死之后**当天就不再重试**，而一次页面抖动（慢加载、
    风控弹窗、SSR 抽风都可能画成登录框）就会让这个账号白丢一整天。这份确认
    的代价只是同一条页面上的再一次 `check_login` —— 相比"每 5 分钟重开一次
    浏览器"的 288 次，几乎不要钱。

    只有 EXPIRED 需要确认：它的判定是「带着 sessionid 却画着登录框」，最容易被
    页面抖动骗到；LOGGED_OUT（连 sessionid 都没有）没有这种歧义。

    确认本身炸了（页面已经关掉之类）时按"死"处理：宁可让用户去扫一次码，
    也不要拿一份疑似失效的登录态继续跑。
    """
    if str(res.get("status") or "") != STATUS_EXPIRED:
        return True
    recheck = getattr(im, "recheck_login", None)
    if not callable(recheck):
        return True
    try:
        lg = recheck() or {}
    except Exception as exc:                        # noqa: BLE001 —— 复检失败不该让整轮炸掉
        logger.warning(f"账号 {username} 复检登录态时炸了：{type(exc).__name__}: {exc}")
        return True
    state = str(lg.get("state") or "")
    if state == "LOGGED_IN":
        logger.warning(
            f"账号 {username} 门禁判了 {res.get('status')}，但复检又说登录还在"
            f" —— 当成偶发抖动：本轮跳过，当天继续按时间表重试"
        )
        return False
    logger.info(f"账号 {username} 复检确认登录态已失效（state={state or 'UNKNOWN'}）")
    return True


# 面板「任务重试次数」（`TASK_RETRY_TIMES`）的上限，与 `webui/settings.py` 的
# `maximum=5` 对齐：`.env` 里手滑写成 999 也不会真的再合成 999 次发送。
RETRY_TIMES_MAX = 5


def _retry_budget(raw) -> int:
    """把 `TASK_RETRY_TIMES` 收成一个安全的整数：0 = 不重试。

    这个键 2026-10-11 之前在面板上可改、`core/` 里却 **0 处消费** —— 无论
    写 1 还是 3，代码都只重试一次（写死的）。现在真按它执行：调用方拿返回值
    当"每个目标失败后最多再试几次"。
    """
    try:
        n = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        # OverflowError 也要接：`TASK_RETRY_TIMES=1e999` 是"合法数字"，
        # `int(inf)` 抛的却是 OverflowError —— 漏掉它，这一行会在**每个**目标的
        # 重试判断里炸出去，整轮发送直接变成账号级异常（用户看到的是"任务异常
        # 终止"，看不出是配置写错了）。
        logger.warning(f"TASK_RETRY_TIMES={raw!r} 不是数字 —— 按默认 1 次重试")
        return 1
    if n < 0:
        return 0
    if n > RETRY_TIMES_MAX:
        logger.warning(f"TASK_RETRY_TIMES={n} 超过上限 {RETRY_TIMES_MAX} —— 按上限执行")
        return RETRY_TIMES_MAX
    return n


def _entry(friend, r, dry_run):
    """一条发送明细 —— 字段与 `core/run_ledger.py` 的 schema 一致。"""
    return {
        "target": friend.get("display") or friend.get("conv_id"),
        "conv_id": friend.get("conv_id"),
        "ok": bool(r.get("ok")),
        "level": r.get("level") or "",
        "reason": r.get("reason") or "",
        "via": r.get("via"),
        "message_id": r.get("message_id"),
        "dry_run": r.get("dry_run") or dry_run,
        "rate_limited": bool(r.get("rate_limited")),
        "at": iso(now()),
    }


def do_user_task(browser, username, cookies, targets, unique_id=None,
                 dry_run=DRY_RUN_OFF, guard=None, run_id=None, message=""):
    """一个账号的完整流程：门禁 → 滚动找人 → 发送 → 回执确认。

    实现委托给 `core.douyin_im.DouyinIM`：
      任务一（门禁）    DouyinIM 构造时自动完成，结论在 wait_ready() 里
      任务二（找人）    iter_find_and_select —— yield 时该会话已选中且 conv_id 已校验
      任务三（发送）    im.type_and_send —— 全选 + 可信 insertText 输入（落地校验，
                        失败当场退回 JS 合成输入覆盖重打）+ HTTP/DOM 回执双确认
    拟人化节奏由 cloakbrowser 的 humanize 负责，这里只补一个"别太快"的下限。

    `message` 是这个账号专属的发送内容（`TASKS[].message`）；留空就用全局
    `MESSAGE_TEMPLATE`。逐条发送前才渲染（见 `core/msg_builder.build_message`），
    所以 `[API]` 这类占位符是**每个好友各取一次**，不会一个账号的朋友收到同一句。

    返回该账号的结构化结果（`accounts[]` 里的一项）；调用方看 `abort_run`
    决定是否中止整轮（命中熔断时为 True）。
    """
    guard = guard or limit_guard.LimitGuard()
    sent_ok = sent_fail = 0     # 必须在 try 之外初始化：门禁阶段抛异常也要能走到收尾
    login_lost = False          # 运行期掉登录（发送时复检出来的）—— 决定账号级结论
    kicked = ""                 # 非空 = 服务端把会话踢下线（回执 decision=KICK）
    out = {
        "username": username, "unique_id": unique_id,
        "status": "ok", "gate": None, "sent_ok": 0, "sent_fail": 0,
        "scanned_all": None, "steps": None, "visited": None,
        "missing": [], "select_failed": [],
        "targets": [target_name(t) for t in (targets or [])],
        "entries": [], "entries_truncated": False, "abort_run": False,
        "limit": None, "error": None, "note": None, "dry_run": dry_run,
    }

    context = browser.new_context()  # 每个任务使用独立的上下文
    context.set_default_navigation_timeout(
        config["browserActionTimeout"]
    )  # 导航超时（毫秒，config 已换算好）
    context.set_default_timeout(
        config["browserActionTimeout"]
    )  # 单次操作默认超时（毫秒）

    page = context.new_page()

    # 注入 Cookie。★ 必须先清洗：Playwright 的 add_cookies 是**整批**语义，
    # 一条 name/domain 为空的条目就会让整批被拒，这个账号随后会被判成"掉登录"
    # （面板提示重新扫码）。抖音自己就会产出这种空名 cookie（value="douyin.com"，
    # 见 core/cookie_utils.py:51-77），而 .env 里的 COOKIES_* 是历史快照，
    # 完全可能带着它。filter_cookies 只丢不可用条目，不动其它字段。
    safe_cookies, cookie_stats = cookie_utils.filter_cookies(cookies, domains_only=False)
    if cookie_stats.get("invalid"):
        logger.warning(
            f"⚠️ 注入前丢弃不可用 cookie {cookie_stats['invalid']} 条"
            f"（空 name/空 domain）—— 留着会让整批注入失败"
        )
    if not safe_cookies:
        raise RuntimeError("cookie 全部不可用（空 name/domain），无法注入登录态")
    context.add_cookies(safe_cookies)

    im = None
    try:
        # 打开抖音网页聊天页面由库内部完成（先挂钩子再导航，顺序不可颠倒）
        # 扫描参数全部来自配置：总预算/门禁等待是秒，静默窗是毫秒（见 utils.config）
        im = DouyinIM(
            page,
            timeout=config["imScanTimeout"],
            ready_timeout=config["imReadyTimeout"],
            settle_ms=config["friendListSettleMs"],
            max_steps=config["imMaxSteps"],
        )

        res = im.wait_ready()
        out["gate"] = res.get("status")
        if res.get("status") != STATUS_READY:
            if res.get("status") in GATE_DEAD_STATUSES and _gate_death_confirmed(im, res, username):
                # 登录态已死：一条都发不出去，而且**人不动手就不会好**。
                # 按 error 记账 = 占掉当天名额、当天不再重试（见 GATE_DEAD_STATUSES 注释）。
                out["status"] = "error"
                out["error"] = (
                    f"login_dead: 登录已失效（{_gate_reason(res.get('status'))}），"
                    "本轮一条都没发出去，当天也不会再重试"
                    "（拿同一份失效 cookie 反复开浏览器对风控只有坏处）；"
                    "请到面板「扫码登录」重新登录该账号，登录成功后当天会自动补发"
                )
                logger.error(
                    f"账号 {username} 操作前检查未通过：{_gate_reason(res.get('status'))}"
                    f" —— 登录态已死，当天不再重试；到面板「扫码登录」重新登录这个账号，"
                    f"登录成功后当天会自动补发"
                )
            else:
                out["status"] = "skipped"
                logger.error(f"账号 {username} 操作前检查未通过：{_gate_reason(res.get('status'))}，跳过该账号")
            return out

        logger.info(
            f"账号 {username} 门禁通过  user_id={res.get('user_id')} "
            f"nickname={res.get('nickname')} 会话列表就绪"
        )

        # 门禁刚过就先扫一次页面提示：上一轮留下的风控弹窗可能还挂着，
        # 这时候再发只会火上浇油。
        #
        # 空跑（select/full）只**报告**不落盘：空跑一条都没发出去，把它记成熔断并写
        # 30 分钟冷却毫无道理 —— 而定时任务每天只跑一轮，撞上这个凭空来的冷却窗口
        # 就是整天不发（`FUSION-DESIGN.md §5` 也写明空跑不落盘）。
        pre_hit = im.check_limit()
        if pre_hit and config.get("limitGuardEnabled", True):
            if dry_run == DRY_RUN_OFF:
                rec = guard.trip(pre_hit, target=None, run_id=run_id)
                out.update({"status": "skipped", "abort_run": True, "limit": rec})
                logger.error(f"账号 {username} 门禁后发现风控提示，本轮中止：{pre_hit}；"
                             f"冷却至 {rec['cooldown_until']}")
                return out
            logger.warning(f"账号 {username} 门禁后发现风控提示：{pre_hit} —— "
                           f"空跑不写冷却、不中止本轮（一条都没发出去）")

        lo, hi = limit_guard.sleep_jitter_bounds(
            config.get("sendIntervalMinSeconds"), config.get("sendIntervalMaxSeconds"))
        if dry_run != DRY_RUN_OFF:
            lo = hi = 0.0      # 空跑不发送，没必要为"风控"而放慢

        # 生成器：yield 出来的那一刻，对应好友的会话已经被选中
        for friend in im.iter_find_and_select(targets):
            logger.debug(f"账号 {username} 已选中好友 {friend['display']}，准备发送")
            text = build_message(message)
            try:
                r = im.type_and_send(friend, text, dry_run=dry_run)
            except Exception as exc:                                # noqa: BLE001
                # 输入环节自己拒绝发送（打不进去 / 可能叠发）时抛异常。以前这会冒到
                # `do_user_task` 的外层 except，把**整个账号**带崩 —— 后面的好友
                # 一条都不发。现在只记这一条失败，后面的目标继续；重试没有意义
                # （同一条输入通道刚失败过），所以直接换下一个目标。
                logger.error(f"账号 {username} → {friend['display']} 输入环节拒绝发送：{exc}")
                logger.debug(traceback.format_exc())
                sent_fail += 1
                r = {"ok": False, "level": "none", "reason": f"input_failed: {exc}",
                     "via": None, "message_id": None, "dry_run": dry_run,
                     "rate_limited": False}
                if len(out["entries"]) < ENTRY_CAP:
                    out["entries"].append(_entry(friend, r, dry_run))
                else:
                    out["entries_truncated"] = True
                continue
            tag = "空跑" if dry_run != DRY_RUN_OFF else ""
            if r["ok"]:
                sent_ok += 1
                logger.info(
                    f"账号 {username} → {friend['display']} {tag}完成"
                    f"（{r.get('via')} message_id={r.get('message_id') or '-'}）"
                )
            else:
                sent_fail += 1
                if r.get("login_lost"):
                    # 掉登录：重试没有意义，剩下的目标也别试了（每个目标白等 20 秒
                    # 回执再复检一次，只会把风控喂大）。账号级结论由下面标成 error，
                    # 面板据此提示「请重新扫码登录」—— 见 core/douyin_im.recheck_login。
                    login_lost = True
                    if r.get("kicked"):
                        # 服务端直接回 `{"decision": "KICK"}`：这个会话已经作废。
                        # 与掉登录同样是"账号级、立即停手"，但要分开说 —— 重试同一个
                        # 死会话只会白等 20 秒超时，把当天的名额一条条烧光
                        # （2026-10-20 两个账号整天没发出去就是这个形状）。
                        kicked = r.get("kick_decision") or "KICK"
                        logger.error(
                            f"账号 {username} → {friend['display']} 被服务端踢下线"
                            f"（decision={kicked}）：这个登录态已经作废，本轮立即停止，"
                            f"不重试也不占今天的名额 —— 请到面板「扫码登录」重新登录"
                        )
                    else:
                        logger.error(
                            f"账号 {username} → {friend['display']} 发送时发现登录已失效"
                            f"（{r.get('login_state') or 'UNKNOWN'}）：停止本轮发送，"
                            f"请到面板「扫码登录」重新登录这个账号"
                        )
                    if len(out["entries"]) < ENTRY_CAP:
                        out["entries"].append(_entry(friend, r, dry_run))
                    else:
                        out["entries_truncated"] = True
                    break
                # 失败重试：次数由面板「任务重试次数」（`TASK_RETRY_TIMES`）决定，
                # 每次都用 conv_id 重新选中（列表可能已滚动，原来的下标失效）。
                #
                # 两种情况**一次都不重试**：
                #   * 上一发已经吃到风控信号 —— 面板把「限流熔断」写成"命中即停"，
                #     再试一次就是在喂风控（旧代码正是在这里多合成了一次发送）；
                #   * 重试时才发现掉登录 —— 重试没有意义，交给下面的账号级结论。
                retries = _retry_budget(config.get("taskRetryTimes", 1))
                if retries and r.get("rate_limited") and config.get("limitGuardEnabled", True):
                    logger.warning(
                        f"账号 {username} → {friend['display']} 已命中风控信号"
                        f"（{r.get('limit', {}).get('keyword')}）—— 不重试，交给熔断"
                    )
                    retries = 0
                for attempt in range(1, retries + 1):
                    logger.warning(
                        f"账号 {username} → {friend['display']} 未拿到可信回执，"
                        f"重试 {attempt}/{retries}"
                    )
                    try:
                        if not (friend.get("reselect") and friend["reselect"]()):
                            logger.warning(
                                f"账号 {username} → {friend['display']} 重选失败，不再重试"
                            )
                            break
                        r2 = im.type_and_send(friend, text, dry_run=dry_run)
                        r = r2
                        if r2["ok"]:
                            sent_ok += 1
                            sent_fail -= 1
                            logger.info(
                                f"账号 {username} → {friend['display']} "
                                f"重试 {attempt}/{retries} 成功"
                            )
                            break
                        if r2.get("login_lost"):
                            login_lost = True
                            if r2.get("kicked"):
                                kicked = r2.get("kick_decision") or "KICK"
                            logger.error(
                                f"账号 {username} → {friend['display']} 重试时发现登录已失效"
                                f"（{r2.get('login_state') or 'UNKNOWN'}）：停止本轮发送，"
                                f"请到面板「扫码登录」重新登录这个账号"
                            )
                            break
                        if r2.get("rate_limited") and config.get("limitGuardEnabled", True):
                            logger.warning(
                                f"账号 {username} → {friend['display']} 重试命中风控信号"
                                f"（{r2.get('limit', {}).get('keyword')}）—— 停止重试"
                            )
                            break
                    except Exception:
                        logger.warning(traceback.format_exc())
                        break
                if login_lost:
                    # 重试时才发现掉登录：和"首发就掉登录"一样立刻收摊 ——
                    # 剩下的目标每个都要白等一轮回执超时再复检，只会把风控喂大。
                    if len(out["entries"]) < ENTRY_CAP:
                        out["entries"].append(_entry(friend, r, dry_run))
                    else:
                        out["entries_truncated"] = True
                    break

            if len(out["entries"]) < ENTRY_CAP:
                out["entries"].append(_entry(friend, r, dry_run))
            else:
                out["entries_truncated"] = True

            # 熔断：命中即停 —— 继续发只会把风控喂大
            if r.get("rate_limited") and config.get("limitGuardEnabled", True):
                if dry_run != DRY_RUN_OFF:
                    # 空跑：报告但不落盘、不中止（见上面门禁处的同一段理由）
                    logger.warning(
                        f"账号 {username} → {friend.get('display')} 空跑检测到风控信号"
                        f"（{r.get('limit', {}).get('keyword')}）—— 不写冷却、不中止本轮"
                    )
                    page.wait_for_timeout(800)
                    continue
                rec = guard.trip(r.get("limit"), target=friend.get("display"),
                                 run_id=run_id)
                out.update({"abort_run": True, "limit": rec})
                logger.error(
                    f"账号 {username} 命中限流/风控信号（{r.get('limit', {}).get('keyword')}），"
                    f"立即停止本轮剩余目标；冷却 {cooldown_human(rec.get('cooldown_seconds'))}"
                    f"（至 {rec['cooldown_until']}）"
                )
                break

            # 发送完让列表状态落定，再继续滚动（发送会把该会话移到顶部）
            page.wait_for_timeout(800)
            if hi > 0:
                import random
                gap = random.uniform(lo, hi)
                logger.debug(f"账号 {username} 下一条目标前等待 {gap:.1f}s")
                time.sleep(gap)

        if login_lost:
            # 账号级结论：**掉登录 ≠ 发送失败**。记成 ok 会让调度把这一天
            # 当成"已发"（core/schedule.select 当天不再重试），面板上也没有任何
            # 提示 —— 2026-10-09 示例号 就是这么静默丢掉一整天的。记成 error 后
            # 面板计入「需要处理」，人只要重新扫码登录，当天就会被补发
            # （见 webui/app.py 的扫码成功回调）。
            out["status"] = "error"
            if kicked:
                # 同一个"账号级、今天别再试"的结论，但换个前缀让面板说真话：
                # 用户看到的是"登录已被踢下线"，不是一句含糊的"登录已失效"。
                out["error"] = (
                    f"session_kicked: 服务端把会话踢下线（decision={kicked}），"
                    f"本轮发送成功 {sent_ok} 条、失败 {sent_fail} 条；"
                    "请到面板「扫码登录」重新登录该账号，登录成功后当天会自动补发"
                )
            else:
                out["error"] = (
                    "login_lost: 登录已失效（服务端不认这个 sessionid），"
                    f"本轮发送成功 {sent_ok} 条、失败 {sent_fail} 条；"
                    "请到面板「扫码登录」重新登录该账号，登录成功后当天会自动补发"
                )
        elif dry_run == DRY_RUN_OFF and sent_ok == 0 and out.get("abort_run"):
            # **发送过程中命中风控熔断，而这一轮一条都没送出去**。同一次熔断，
            # 上面门禁处写的是 `status="skipped"`，这里却什么状态都不写 → 保持
            # `out["status"]` 的初值 "ok"（见上面 out 的构造）→ `_mark_status()`
            # 兜成 STATUS_OK → 时间表认定"今天已发"、当天名额被吃掉，而用户不知道
            # 消息其实一条都没送到（`_mark_status` 的 docstring 点名要避免的形状）。
            # 触发很普通：**第一个目标**（或前几个都没成功时）的回执带
            # `rate_limited=True`（「操作频繁」「安全验证」），此时 sent_ok == 0。
            # 判据用 `sent_ok == 0` 而不是"有没有熔断"：先成功、后熔断的账号确实
            # 送达过，必须照旧记 ok（反向钉子见 tests/test_tasks_fusion.py）。
            # 记 skipped 而不是 error：熔断是**有意为之**，不该像坏 cookie 那样把
            # 账号判成「需要处理」；skipped 只吃掉一次重试预算（上限 6 次），
            # 且冷却窗口本身会挡住接下来的门铃（limit_guard），重新登录也会撤账。
            out["status"] = "skipped"
            out["error"] = (
                "rate_aborted: 发送中命中风控"
                f"（{(out.get('limit') or {}).get('keyword') or '触发限流'}），"
                "本轮一条都没送达；名额留着，冷却结束或重新登录后会补发"
            )
        elif (dry_run == DRY_RUN_OFF and sent_ok == 0
              and not out.get("abort_run")):
            # **一条都没送达**。这里刻意**不再要求 `sent_fail > 0`**：从前只有
            # 「试过但失败」才算坏消息，于是下面这些形状会被 `_mark_status` 兜成
            # ok —— 时间表认定今天已发、名额被吃掉，面板上还自相矛盾
            # （时间表行写「今天已发」、状态胶囊写「尚未运行」），用户第二天才发现
            # 朋友根本没收到：
            #   * 计划里的目标一个都没匹配上（好友改了备注名 / 被折叠 / 页面没加载
            #     出来）→ `sent_ok == sent_fail == 0`，一次发送都没发生；
            #   * 目标列表是空的（上面那条 no_targets 分支会先兜住，这里是双保险）。
            # 2026-10-09 示例号、2026-10-10 🌈示例号🌈 走的是 `sent_fail > 0` 那一支
            # （每个目标都点了发送、回执全消失）。
            scan_now = (im.last_scan if im is not None else None) or {}
            missing = [str(x) for x in (scan_now.get("missing") or [])]
            failed = [str(x) for x in (scan_now.get("select_failed") or [])]
            if sent_fail:
                why = f"本轮 {sent_fail} 个目标一条都没送达（全部没拿到可信回执）"
                hint = "通常是登录被服务端作废或风控拦截"
            elif missing or failed:
                why = (f"本轮 {len(targets)} 个目标一条都没发出去"
                       f"（没找到 {missing or '无'}；找到但没选中 {failed or '无'}）")
                hint = "多半是好友改了备注名、被折叠进别的分组，或页面没加载出来"
            else:
                why = f"本轮 {len(targets)} 个目标一条都没发出去"
                hint = "页面可能没加载出来，或发送被拦下了"
            out["status"] = "error"
            out["error"] = (
                f"nothing_sent: {why} —— {hint}；"
                "请到面板核对目标名，或重新扫码登录该账号"
                "（登录成功后当天会自动补发）"
            )

        scan = im.last_scan or {}
        out.update({
            "scanned_all": scan.get("scanned_all"),
            "steps": scan.get("steps"),
            "visited": scan.get("visited"),
            "missing": list(scan.get("missing") or []),
            "select_failed": list(scan.get("select_failed") or []),
            "note": scan.get("note"),
        })
        logger.info(
            f"账号 {username} 扫描结束：停止原因={scan.get('stopped')} "
            f"步数={scan.get('steps')} 访问会话={scan.get('visited')} "
            f"{'空跑' if dry_run != DRY_RUN_OFF else '发送'}成功={sent_ok} "
            f"{'空跑' if dry_run != DRY_RUN_OFF else '发送'}失败={sent_fail}"
        )
        if scan.get("missing"):
            # 这两句必须区分开：scanned_all=False 时"没找到"不代表"不存在"
            logger.warning(
                f"账号 {username} 未找到的目标：{scan['missing']}"
                f"（{scan.get('note')}）"
            )
        if scan.get("select_failed"):
            logger.warning(
                f"账号 {username} 找到但选中失败：{scan['select_failed']}"
            )

        folds = im.fold_groups()
        if any(v for v in folds.values() if v):
            logger.warning(
                f"账号 {username} 注意：折叠组/陌生人组里有内容 {folds}，"
                f"主列表扫不到，目标可能被折叠"
            )
    except Exception as e:
        out["status"] = "error"
        out["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"账号 {username} 执行出错：{out['error']}")
        logger.debug(traceback.format_exc())
    finally:
        if im is not None:
            try:
                im.detach()
            except Exception:
                pass
        context.close()  # 任务完成后关闭上下文

    out["sent_ok"] = sent_ok
    out["sent_fail"] = sent_fail
    return out


def _idle_line(run_id, result):
    """没到点时的那一行日志。

    要能一眼回答"为什么现在什么也没干"和"下一个是谁" —— 否则用户在面板上
    点"立即运行"看到"没反应"，翻日志只有满屏的"跳过"。
    """
    waiting = result.get("waiting") or []
    sent = result.get("sent") or []
    soon = sorted(waiting, key=lambda pair: str(pair[1].get("due_text") or ""))[:3]
    parts = [f"⏳ 没有账号到点，本轮不发  run_id={run_id}"]
    parts.append(f"等 {len(waiting)} 个、今天已发 {len(sent)} 个")
    if soon:
        parts.append("最近到点：" + "、".join(
            f"{p['username']}@{p['due_text']}" for _, p in soon))
    if result.get("fallback"):
        parts.append(f"默认时间 {result['fallback']}")
    return "  ".join(parts)


def _skip(rec, source, *, reason, **extra):
    """记一次"整轮被拦下"，返回 rec。

    ★ **timer 触发的早退不写账本**。以前 timer 一天只响一次，"今天这一轮被跳过了"
    是必须留痕的大事；现在它每 5 分钟响一次（见 core/schedule.py），
    光"还没到点"一天就有 288 次，全写进 logs/runs.jsonl 会把真正的运行记录淹掉，
    面板的"最近运行"直接失守。日志照打，账本只留给 cli / webui 这两个
    **人主动发起**的来源 —— 那时候"被跳过了"才是用户想看见的答案。
    """
    rec["skipped"] = {"reason": reason, **extra}
    rec["finished_at"] = iso(now())
    if str(source or "") != ledger.SOURCE_TIMER:
        ledger.append_run(rec)
    return rec


def _schedule_brief(result=None, planned=None, marks=None):
    """把时间表的判定压成能进账本、能给面板看的小结构。

    刻意**不带 user 原文**：账本是长期保留的 jsonl，里面塞进 cookies 就麻烦了。
    只留抖音号、昵称、时间和结局。
    """
    def items(pairs):
        brief = []
        for entry in pairs or []:
            user, plan = entry[0], entry[1]
            row = {
                "unique_id": plan.get("unique_id"),
                "username": plan.get("username"),
                "time": plan.get("time"),
                "due_at": plan.get("due_text"),
            }
            if len(entry) > 2 and isinstance(entry[2], dict):
                row["status"] = entry[2].get("status")
                row["sent_at"] = entry[2].get("sent_at")
            brief.append(row)
        return brief

    result = result or {}
    return {
        "fallback": result.get("fallback"),
        "jitter_minutes": result.get("jitter_minutes"),
        "due": items(result.get("due")),
        "waiting": items(result.get("waiting")),
        "sent": items(result.get("sent")),
        "ran": [
            {
                "unique_id": plan.get("unique_id"),
                "username": plan.get("username"),
                "time": plan.get("time"),
                "due_at": plan.get("due_text"),
                "status": (marks or {}).get(plan.get("unique_id"), ("", ""))[0],
            }
            for _, plan in (planned or [])
        ],
    }


def _mark_status(result) -> str:
    """把账号级结果翻译成发送时间表里的状态。

    - ``ok``      → 真跑过（个别好友失败也算跑过）→ 记成今天已发
    - ``error``   → 真跑过但炸了 / 登录态已死（``login_dead``）→ **同样记**：
                    坏 cookie 不会因为多试几次就好，不记的话每 5 分钟一次的
                    门铃会把它一天踹起来 288 次
    - ``skipped`` → 门禁没过 / 命中风控提前中止，**一条都没发出去**。必须与
                    ``ok`` 区分：记成「已发」会让面板明晃晃写着已发、而当天的
                    名额就这么被吃掉，用户完全看不出来（见 core/schedule.select）
    """
    status = str((result or {}).get("status") or "")
    if status == "error":
        return schedule.STATUS_ERROR
    if status == "skipped":
        return schedule.STATUS_SKIPPED
    return schedule.STATUS_OK


def _persist_mark(state, uid, status, note, plan, *, stamped, run_id, source,
                  count_attempt=True):
    """把一个账号的记账**立刻**合并写进状态文件。返回是否写进了 `state`。

    ★ 为什么不能只等收尾那一次：从前所有记账都压在 `runTasks` 收尾的 `finally`
      里，整轮被 SIGKILL / 断电时，**已经发出去**的账号在文件里一条记录都没有
      —— 下一个门铃重新算「今天还没发」→ 同一个好友收到第二条火花（本项目最
      刺眼的线上事故）。逐账号落盘把"已发出"这件事的暴露窗口从整轮（分钟级）
      缩到一个账号。

    用 `save_marks`（只合并点名的这**一个**账号）而不是 `save_state`：`state`
    是整轮开始时读的快照，整体覆盖会把面板在这中间改过的别的账号抹掉。

    这里**绝不抛**：记不上账的最坏后果是"可能重复发一次"，而抛出去会让这一轮
    剩下的账号一个都不跑、连收尾的账本行也没有 —— 两者不对称，所以一律吞掉只
    记警告（与 `core/schedule.py` 里 `save_marks` 明写的"不抛"契约一致）。
    """
    try:
        schedule.mark_sent(state, uid, when=stamped, plan=plan, run_id=run_id,
                           status=status, source=source, note=note,
                           count_attempt=count_attempt)
    except Exception as exc:                    # noqa: BLE001 —— 记账不许打断发送
        logger.warning(f"⚠️ 账号 {uid} 的记账没写进内存（{type(exc).__name__}: {exc}）"
                       f" —— 收尾那次合并写还会再试一遍")
        return False
    try:
        if not schedule.save_marks(state, [uid]):
            logger.warning(
                f"⚠️ 账号 {uid} 的记账没能立刻落盘（{schedule.state_path()}）——"
                f"这一轮若中途被杀，它今天可能被重复发送；检查目录是否存在/可写"
            )
    except Exception as exc:                    # noqa: BLE001
        logger.warning(f"⚠️ 账号 {uid} 的记账落盘时出错（{type(exc).__name__}: {exc}）"
                       f" —— 收尾那次合并写还会再试一遍")
    return True


def _dedupe_plans(planned):
    """按 `unique_id` 去重（保留第一条），返回 `(去重后的列表, 被丢掉的名字)`。

    重复的账号在同一轮里会被完整处理两次 —— 同一个好友收到两条一模一样的火花。
    来源有两处：手改 `.env` 写出两条同号账号；以及 `utils.config.get_userData()`
    的账号名单在"面板每请求刷新 + 运行线程 reload"并发重建时重复（见那里的说明）。
    这里是最靠近伤害点的一道闸：无论上游怎么错，同一个 uid 一轮只发一次。
    """
    seen = set()
    kept = []
    dropped = []
    for user, plan in planned or []:
        uid = str((plan or {}).get("unique_id") or "")
        if uid and uid in seen:
            dropped.append(str((plan or {}).get("username") or uid))
            continue
        if uid:
            seen.add(uid)
        kept.append((user, plan))
    return kept, dropped


def runTasks(dry_run=None, source="cli", force=False, only=""):
    """跑一轮。返回结构化汇总，并追加进运行账本。

    :param dry_run: off / select / full；None 时回落到环境变量 DRY_RUN
    :param source:  触发来源（cli / webui / timer），只记录不参与逻辑
    :param force:   True = 不看时间表、不管今天发没发过，候选账号全跑一遍
                    （面板"立即运行"和 `python main.py task --force` 走这条）；
                    跑完**照样记账** —— 否则手动跑一轮之后，到点那一轮会再发一遍。
    :param only:    只处理这一个账号（抖音号或昵称）。它只**缩小候选集**，
                    要不要看时间表仍由 `force` 决定：
                    `--only X` = "就看 X 到点没有"，`--only X --force` = "就发 X，现在"。
    """
    level = resolve_dry_run(dry_run, config.get("dryRun"))
    run_id = ledger.make_run_id()
    started = now()
    # 冷却时长读配置：`LIMIT_COOLDOWN_MINUTES` 从前是个死键（文档和 .env 都写了，
    # 但这里用无参构造永远 30 分钟）—— 运维想冷却两小时完全无效。
    try:
        cooldown_seconds = max(1, int(config.get("limitCooldownMinutes", 30))) * 60
    except (TypeError, ValueError):
        cooldown_seconds = limit_guard.DEFAULT_COOLDOWN_SECONDS
    guard = limit_guard.LimitGuard(cooldown_seconds=cooldown_seconds)

    rec = {
        "run_id": run_id, "started_at": iso(started), "finished_at": None,
        "dry_run": level, "source": source,
        "rate_limited": False, "limit": None, "skipped": None, "accounts": [],
    }
    logger.info("=" * 64)
    logger.info(f"开始执行任务  run_id={run_id}  触发来源={source}  "
                f"空跑档位={level}"
                + ("" if level == DRY_RUN_OFF else "（不会真的发送）"))

    # 冷却期内连启动都不启动：重试只会把风控喂大，而且定时任务会反复撞上来
    # 熔断开关关掉时，冷却门禁也要一起关：不然用户在面板上把 LIMIT_GUARD_ENABLED
    # 设成 false，预检和风控判定都放行了，却仍然被上一次留下的冷却记录整轮拦下 ——
    # 表现就是「开关关了，还是一动不动」，日志里只有一行 cooldown。
    cd = guard.in_cooldown() if config.get("limitGuardEnabled", True) else None
    if cd:
        logger.warning(
            f"⛔ 上一轮命中限流熔断，冷却中（剩 {cooldown_human(cd.get('remaining_seconds'))}，"
            f"至 {cd.get('cooldown_until')}，关键词={cd.get('keyword')}）—— 本轮跳过"
        )
        return _skip(rec, source, reason="cooldown",
                     until=cd.get("cooldown_until"), record=cd)

    # 禁跑时间窗：窗口内整轮跳过（默认 off；融合版是唯一的发送方，
    # 这个窗口只在用户自己设了时段时才生效）。
    # 两个 Chromium 用同一个抖音账号会互踢，所以撞窗就跳过，而不是硬发。
    qw = quiet_window.check()
    if qw["active"]:
        logger.warning(f"⛔ {qw['reason']}；本轮跳过（用 QUIET_WINDOW= 或 "
                       f"QUIET_WINDOW_OVERRIDE=1 可临时放行）")
        return _skip(rec, source, reason="quiet_window", until=qw["until"],
                     record=quiet_window.summary(qw))

    if config.get("limitGuardEnabled", True) and guard.read():
        # 冷却已过期但记录还在：留着记录会让仪表盘一直显示"熔断中"，清掉
        guard.clear()
        logger.info("上一次熔断的冷却已结束，清除冷却记录")

    # ---- 时间表闸门（见 core/schedule.py）--------------------------------
    # 候选集默认是所有账号；`only` 只**缩小范围**，要不要看时间表仍由 `force` 决定。
    # 放在"打印当前配置"之前：定时器每 5 分钟敲一次门，绝大多数敲门都是"没到点"，
    # 那种情况只该留一行日志，不该把整段配置刷一遍。
    users = list(userData)
    if only:
        wanted = str(only).strip()
        users = [
            u for u in users
            if wanted in (str(u.get("unique_id") or ""), str(u.get("username") or ""))
        ]
        if not users:
            logger.error(f"⛔ --only {wanted} 没匹配到任何账号（现有："
                         + "、".join(f"{u.get('username')}/{u.get('unique_id')}"
                                     for u in userData) + "）")
            return _skip(rec, source, reason="no_such_account", only=wanted)

    jitter = config.get("scheduleJitterMinutes")
    fallback = os.getenv("TASK_SCHEDULE_TIMES", "")
    plan_result = None
    if force:
        # 强制（面板的「立即运行」/ 命令行的 --force）：**不看"到没到点"**，
        # 但今天已经真发过的账号仍然跳过 —— 否则面板上那个按钮就成了重复发送
        # 按钮：手抖点两下，每个好友就收到两条一模一样的话。
        # 真要连"今天已发"一起无视（人工补一轮），命令行上用 --force-all，
        # 那时 force == "all"。
        # 时间照样算出来：状态文件与账本里要能看到"这个账号今天其实是 08:07
        # 该发的，14:20 被人手动补掉了"。
        forced = schedule.select(users, when=started, fallback=fallback, jitter=jitter)
        everything = [(u, forced["plans"][str(u.get("unique_id") or "")]) for u in users]
        if force == "all":
            planned = everything
        else:
            done = {str(p["unique_id"]) for _, p, _ in forced["sent"]}
            planned = [pair for pair in everything if str(pair[1]["unique_id"]) not in done]
            if not planned and users:
                # 说清楚，别让面板报"运行成功、0 个账号"——用户会以为程序坏了。
                logger.info(
                    f"⏭️ 本轮是强制发送，但这 {len(done)} 个账号今天都已经发过了；"
                    f"要无视这一点请用 --force-all"
                )
                return _skip(rec, source, reason="already_sent", sent=len(done))
    else:
        plan_result = schedule.select(
            users, when=started, fallback=fallback, jitter=jitter
        )
        planned = plan_result["due"]
        if not planned:
            logger.info(_idle_line(run_id, plan_result))
            rec["nothing_due"] = True
            rec["schedule"] = _schedule_brief(plan_result)
            rec["finished_at"] = iso(now())
            # 刻意**不**进账本：定时器一天敲 288 次门，绝大多数都是这一种，
            # 全写进 logs/runs.jsonl 会把真正的运行记录挤没。
            return rec
        logger.info(
            f"时间表：本轮该发 {len(planned)} 个账号（"
            + "、".join(f"{p['username']}@{p['due_text']}" for _, p in planned)
            + f"）；另有 {len(plan_result['waiting'])} 个没到点、"
            f"{len(plan_result['sent'])} 个今天已经发过"
        )

    # 同一轮里同一个账号被排两次 = 好友收到两条一样的火花（本项目最刺眼的线上事故）。
    # `unique_id` 本来就是账号的键，配置层又"读出来什么就是什么"（手改 .env 可以写出
    # 两条同号账号），所以在这里按 uid 去重，并明确留痕。
    planned, duplicates = _dedupe_plans(planned)
    if duplicates:
        logger.warning(
            f"⚠️ TASKS 里有重复的账号（{('、'.join(duplicates))}），"
            f"本轮每个账号只处理一次；请到面板核对账号列表"
        )

    logger.debug("当前配置如下：")
    # ★ 用 repr：模板里的换行是**字面 `\n`（两个字符）**，直接打印看不出区别 ——
    #   而"配置层把反斜杠吃掉了"正是最隐蔽的一类故障（日志里只剩一个裸 n，
    #   消息就成了一整行）。repr 一眼能分辨 '\\n' 与 'n'。
    logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')!r}")
    logger.debug(f"一言类型: {config['hitokotoTypes']}")
    logger.debug(f"发送间隔: {config.get('sendIntervalMinSeconds')}~"
                 f"{config.get('sendIntervalMaxSeconds')}s（空跑时不等待）")
    for user in userData:
        # `user['targets']` 可能是 `None`（手改 .env 写成 `"targets": null`）——
        # 这里只是打日志，绝不能因为一行 DEBUG 把整轮发送掀翻。
        logger.debug(f"用户: {user.get('username', '未知用户')}, "
                     f"目标好友: {[target_name(t) for t in (user.get('targets') or [])]}")

    lock = ledger.RunLock(
        source=source, dry_run=level, run_id=run_id,
        # 把这一轮要碰的账号写进运行锁：面板靠它拦住"派发器正在发这个账号、
        # 又去给它扫码登录/读好友列表"（两只会同时打开同一个资料目录）。
        accounts=[plan["unique_id"] for _user, plan in planned],
    )
    try:
        lock.acquire()
    except ledger.RunLocked as e:
        rec["skipped"] = {"reason": "locked", "record": e.info}
        rec["finished_at"] = iso(now())
        logger.warning(f"⛔ {e} —— 本轮跳过（不允许两个浏览器同时操作同一个账号）")
        return _skip(rec, source, reason="locked", record=e.info)

    # 时间表状态：这一轮**真的走到了**哪个账号就记哪个（成功、失败都算），
    # 当天不再重试。不记的话，一个坏了 cookie 的账号会被 5 分钟一次的门铃
    # 反复踹起来开浏览器，一天 288 次。
    state = schedule.load_state()
    marks = {}          # 抖音号 -> (结局, 备注)
    recording = level == DRY_RUN_OFF      # 空跑轮次什么都不记（见收尾注释）
    flushed = set()     # 已经"跑完就立刻落盘"的账号：收尾合并写别再 mark 一遍
    free_skips = set()  # 没开浏览器的那种跳过（targets 为空），不计入重试上限
    try:
        for user, plan in planned:
            if rec.get("rate_limited"):
                break       # 命中熔断：剩下的账号也不跑了，**也不记账** ——
                            # 让它们等冷却结束，而不是被白白判成"今天办过了"
            uid = plan["unique_id"]
            if uid in marks:
                # 真的撞上重复配置时（上面的 _dedupe_plans 是常规闸门），
                # 宁可不发也不能给同一个好友发两条。
                logger.warning(f"⚠️ 账号 {plan.get('username') or uid} 在本轮里出现了两次，"
                               f"第二次直接跳过（避免重复发送）")
                continue
            cookies = user["cookies"]
            # 归一化在**这里**做（配置读取端不做）：`target_refs()` 用 `douyin_im.norm`
            # 归一名字，`DouyinIM._match` 内部用同一套 —— 两边都归过才谈得上相等，
            # 否则配置里的「Ｌｕ瞳」永远匹配不上页面上的「Lu瞳」。
            # 面板勾选写入的目标带 uid/conv_id，走 id 精确匹配；手写名字走名字链路。
            # 空项在这里丢掉：留在剩余名单里永远扣不掉，会白滚到底。
            targets = target_refs(user["targets"])
            username = user.get("username", "未知用户")
            if not targets:
                # 这个账号一个目标都没有（TASKS 里漏写 targets、面板上全取消勾选、
                # 或者 JSON 写成了 null）。**别开浏览器**：开起来也只会白等一轮超时，
                # 而每 5 分钟一次的门铃会把它反复踹起来 —— 那正是要避免的风控面。
                # 也**别记成 ok**：记 ok 等于把今天的名额吃掉，面板还会自相矛盾
                # （时间表行写「今天已发」、状态胶囊写「尚未运行」）。
                logger.error(
                    f"账号 {username} 没有配置任何目标，本轮跳过（不记「今天已发」，"
                    f"修好 TASKS 的 targets 后下一个门铃就会补发）"
                )
                # 时间表记 `skipped`（而不是 error）：`select()` 只有对 skipped 才
                # 认为"当天名额还在"（见 core/schedule.py:550 的注释）。记成 error
                # 会让这个账号今天彻底静音 —— 用户中午把目标加回来，也要等到明天
                # 才会发出去；而它一条消息都没发过，没有任何理由占掉今天的名额。
                marks[uid] = (schedule.STATUS_SKIPPED,
                              "no_targets: 这个账号没有配置任何目标（TASKS 的 targets 为空）")
                # 这种跳过**不开浏览器**，风控上没有成本（见上面两行）：不计入
                # `SCHEDULE_SKIP_RETRY_MAX` 的重试上限。理由反过来说更清楚 ——
                # 计数只会在"重新扫码"时归零，而用户补配 targets 时不走扫码，
                # 计进去 = 他半小时后才把目标填回来，这个账号当天再也不会被重试。
                free_skips.add(uid)
                rec["accounts"].append({
                    "username": username, "unique_id": user.get("unique_id"),
                    # 账本这一行写 error：面板上要一眼看得见（"这个账号没配目标"），
                    # 与时间表那侧的 skipped 是给两个不同的读者看的。
                    "status": "error", "sent_ok": 0, "sent_fail": 0, "entries": [],
                    "error": f"no_targets: {username} 的 TASKS 条目里 targets 是空的",
                })
                continue
            fingerprint = user.get("fingerprint", None)
            message = str(user.get("message") or "")
            logger.info(f"开始处理账号 {username}（计划 {plan['time']}，"
                        f"今天落在 {plan['due_text']}）")
            # 用 repr：模板里的换行是**字面 `\n`**，直接打印分不出 '\\n' 与 'n'。
            logger.debug(
                f"账号 {username} 的发送内容："
                + (
                    f"账号专属 {message!r}"
                    if message.strip()
                    else f"未单独配置，用全局模板 {config.get('messageTemplate', '')!r}"
                )
            )
            browser = None
            try:
                browser = get_browser(fingerprint)
                res = do_user_task(
                    browser, username, cookies, targets,
                    unique_id=user.get("unique_id"), dry_run=level,
                    guard=guard, run_id=run_id, message=message,
                )
                rec["accounts"].append(res)
                marks[uid] = (_mark_status(res), str(res.get("error") or ""))
                if recording and _persist_mark(
                    state, uid, marks[uid][0], marks[uid][1], plan,
                    stamped=started, run_id=run_id, source=source,
                ):
                    # ★ 这一个账号的记账已经落盘了。收尾那次合并写不要再 mark
                    #   一遍：skipped 的 attempts 会被同一轮加两次（6 次上限只撑
                    #   3 轮）。`state` 里已经有这条记录，收尾的 `save_marks` 照样
                    #   会把它写出去。
                    flushed.add(uid)
                if res.get("limit"):
                    rec["rate_limited"] = True
                    rec["limit"] = res["limit"]
                logger.info(f"账号 {username} 任务完成")
            except Exception as e:
                # 单个账号炸掉不能带走整轮：记下来继续下一个
                logger.error(f"账号 {username} 任务异常终止：{type(e).__name__}: {e}")
                logger.debug(traceback.format_exc())
                marks[uid] = (schedule.STATUS_ERROR, f"{type(e).__name__}: {e}")
                if recording and _persist_mark(
                    state, uid, marks[uid][0], marks[uid][1], plan,
                    stamped=started, run_id=run_id, source=source,
                ):
                    flushed.add(uid)
                rec["accounts"].append({
                    "username": username, "unique_id": user.get("unique_id"),
                    "status": "error", "error": f"{type(e).__name__}: {e}",
                    "sent_ok": 0, "sent_fail": 0, "entries": [],
                })
            finally:
                if browser is not None:
                    # close_context 顺手回收这个浏览器的 playwright 句柄：
                    # `get_browser` 的回落路径每轮起一个 driver 子进程，
                    # 以前只关浏览器不还句柄，跑久了机器上全是 node。
                    close_context(browser)
    finally:
        # ★ 记账必须在**释放锁之前**做完：锁一放开，下一个进程（或面板）就可能
        #   读走状态文件再写回来，而我们随后用手里这份旧快照整体覆盖 —— 它的改动
        #   就被抹掉了（2026-10-09 示例号 掉登录静默丢一天，走的正是这条路径）。
        #   用嵌套 finally：记账这段自己抛了异常，锁也必须放掉。
        try:
            # 记账放在 finally 里：就算上面是"熔断提前 break"或账号级异常，
            # 已经跑过的账号也必须落下记录 —— 否则门铃下一轮会把它们再踹起来。
            if marks and level == DRY_RUN_OFF:
                # 用**本轮开始**的时刻记账，而不是收尾时刻：23:58 起跑、00:01 收尾的
                # 那一轮若按收尾时刻记，落的是明天的日期，于是明天一整天都被当成
                # 「今天已经发过了」—— 跨零点的那一次会把第二天的火花静默吃掉。
                # 逐账号落盘（`_persist_mark`）用的也是这个时刻，两边口径一致。
                stamped = started
                for user, plan in planned:
                    uid = plan["unique_id"]
                    if uid not in marks or uid in flushed:
                        # flushed：那个账号跑完时已经 mark 过并落盘了，再来一遍会把
                        # skipped 的 attempts 在同一轮里多加一次（见上面的注释）。
                        continue
                    status, note = marks[uid]
                    schedule.mark_sent(
                        state, uid, when=stamped, plan=plan, run_id=run_id,
                        status=status, source=source, note=note,
                        count_attempt=uid not in free_skips,
                    )
                # 只合并**本轮真走到**的这几个账号：磁盘上别的账号（以及面板在我们
                # 跑的过程中改过的同一批账号）以文件为准，别拿旧快照盖回去。
                if not schedule.save_marks(state, list(marks)):
                    logger.warning(
                        f"⚠️ 发送时间表写不进去（{schedule.state_path()}）——"
                        f"这些账号今天可能被重复发送，检查目录是否存在/可写"
                    )
            elif marks:
                # 空跑轮次不记账：什么都没发出去，记了就占掉当天的真发名额，
                # 面板上还会显示「今天已发」——用户以为发过了，其实一条都没有。
                logger.info("本轮是空跑（dry-run），不写发送时间表：什么都没发出去，"
                            "不能占掉这些账号今天的名额")
            if planned:
                brief = _schedule_brief(plan_result, planned, marks)
                brief["forced"] = bool(force)
                rec["schedule"] = brief
            rec["finished_at"] = iso(now())
            ledger.append_run(rec)
        finally:
            lock.release()

    s = ledger.summarize(rec)
    logger.info(
        f"本轮结束  run_id={run_id}  账号={s['accounts']}  "
        f"成功={s['sent_ok']} 失败={s['sent_fail']}  "
        f"耗时={human_duration(s['duration'])}"
        + ("  ⛔ 命中限流熔断" if s["rate_limited"] else "")
        + (f"  ⏭ 跳过：{rec['skipped'].get('reason')}" if rec["skipped"] else "")
    )
    return rec
