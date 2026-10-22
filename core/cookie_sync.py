"""把浏览器里的最新 cookie 合并回 `.env` 的 `COOKIES_<抖音号>`（P2）。

为什么要有这一层
----------------
`.env` 里的 `COOKIES_*` 是**扫码那一刻**的快照。之后每轮发送，服务端会在页面上
新签发/刷新一批 cookie（`x-web-secsdk-uid`、`ttwid`、`web_sign_token` 的时间戳…），
以前这些值随着上下文关闭被直接丢掉，下一轮又从旧快照开始。表现就是
`web_sign_token` 的时间戳永远停在登录时刻 —— 一个能直接算出来"这台设备多久没
正常刷新过"的痕迹。

现在在每轮收尾（拿得到 `context.storage_state()` 的时候）比一次：值变了才写，
没变一个字节都不动。实测正常一轮下来 cookie **零变化**（`/tmp/inv/rot.py`），
所以这条路径平时根本不落盘，也就不会把 `.env` 写出一堆备份。

为什么复用面板的写入器
----------------------
`webui/envfile.save_cookie()` 已经把写 `.env` 的坑都踩完了：先备份（只留最近几份）、
校验 JSON、校验 sessionid 没过期、拦截 `dotenv` 会改写值的危险字符、写入前做
`unicode_escape` 的抵消、重启后 0600。这里**不另写一套**：`.env` 是这个项目的命根子，
两套写盘逻辑迟早会有一套把注释/转义写坏。代价是 core 反向依赖了 webui 的一个模块，
所以是**函数内部惰性 import**：`main.py task` 那种不带面板的跑法只要仓库在，
import 得到；哪天真拆出去了，`_envfile()` 返回 None，这里就安静跳过（发送照常）。
"""
from __future__ import annotations

import json
import logging

from core import cookie_utils

logger = logging.getLogger(__name__)

#: 写不进去的时候只在日志里说一次（每轮每个账号都报一遍等于刷屏）
_WRITER_WARNED = False


def _envfile():
    """惰性拿面板的 `.env` 写入器；拿不到返回 None（只告警一次）。"""
    global _WRITER_WARNED
    try:
        from webui import envfile  # noqa: PLC0415  （故意惰性：core 不该在 import 期拉面板）
    except Exception as exc:                                     # noqa: BLE001
        if not _WRITER_WARNED:
            _WRITER_WARNED = True
            logger.warning(f"⚠️ 拿不到 .env 写入器（{type(exc).__name__}: {exc}），cookie 不回写")
        return None
    return envfile


def _current_cookies(unique_id):
    """`.env` 里当前那份 cookie（读法与 `utils/config.get_userData` 完全一致）。"""
    envfile = _envfile()
    if envfile is None:
        return None
    raw = str(envfile.read_map().get(envfile.cookie_key(unique_id)) or "")
    if not raw.strip():
        return []
    try:
        # utils/config.py 读 COOKIES_* 时会做这一步（上游遗留的 unicode_escape），
        # 比较时必须走同一条路，否则"只差一个转义"会被当成值变了，白写一次盘。
        text = raw.encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        text = raw
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _target_only(cookies) -> list:
    """只留抖音相关域的条目（与 `filter_cookies(domains_only=True)` 同一判定，但
    不带"目标域太少就整体回退"的策略 —— 比较时不需要那个回退）。"""
    return [c for c in cookies or []
            if isinstance(c, dict) and cookie_utils.matches_target(str(c.get("domain") or ""))]


def _canonical(cookies) -> tuple:
    """与顺序无关的比较键：每条 cookie 只留决定"是不是同一份"的字段。"""
    items = []
    for cookie in cookies or []:
        clean = cookie_utils.clean_cookie(cookie)
        if clean is None:
            continue
        items.append((clean["name"], clean["domain"], clean["path"],
                      str(clean["value"]), str(clean["expires"])))
    return tuple(sorted(items))


def sync(unique_id, cookies, *, allow=True) -> dict:
    """把这一轮浏览器里的 cookie 合并回 `.env`。

    `allow=False` 用于"这一轮会话已经被踢/掉登录"：那时候 cookie 可能已经是死的，
    写回去只会把一份还能用的快照盖掉。

    返回结构化结果（日志与测试都看它）：
      reason ∈ {"", "bad-uid", "skipped", "no-login", "expired-login",
                "no-env", "no-writer", "unchanged", "rejected"}
    """
    result = {"ok": True, "changed": False, "reason": "", "written": 0, "error": ""}
    try:
        cookie_utils.env_key_for(unique_id)                       # 只做合法性校验
    except ValueError:
        result.update(ok=False, reason="bad-uid")
        return result
    if not allow:
        result.update(ok=False, reason="skipped")
        return result

    fresh, _stats = cookie_utils.filter_cookies(cookies, domains_only=True)
    if not cookie_utils.has_login_cookie(fresh):
        result.update(ok=False, reason="no-login")
        return result
    if not cookie_utils.has_usable_login_cookie(fresh):
        # 服务端已经把它作废了（expires 写在过去，或者只是个空壳）：不写。
        result.update(ok=False, reason="expired-login")
        return result

    envfile = _envfile()
    if envfile is None:
        result.update(ok=False, reason="no-writer")
        return result

    if not envfile.env_path().exists():
        # 没有 .env（单元测试、ENV_FILE 指错、或者部署成了只读配置）：安静跳过。
        # 不做这层判断的话，_current_cookies() 会读出一份空的映射，于是每次都
        # 走到 save_cookie() 拿一句"找不到 …，无法写入"，日志里全是假的失败。
        result.update(ok=False, reason="no-env")
        return result

    current = _current_cookies(unique_id)
    if current is None:
        result.update(ok=False, reason="no-writer")
        return result
    # 比较口径必须和写回口径一致：写回去的只有抖音域的 cookie，比较时也不能把
    # .env 里用户自己粘进来的其它域算进去，否则首轮就会"因为多了几条别的域"而判定
    # 变了、把那些条目静默抹掉。
    if _canonical(_target_only(current)) == _canonical(fresh):
        result.update(reason="unchanged")
        return result

    fresh = sorted(fresh, key=lambda c: (str(c.get("name")), str(c.get("domain")), str(c.get("path"))))
    saved = envfile.save_cookie(unique_id, cookie_utils.cookies_to_json(fresh))
    if not saved.get("ok"):
        result.update(ok=False, reason="rejected", error=str(saved.get("error") or ""))
        logger.warning(f"⚠️ 账号 {unique_id} 的 cookie 没能回写 .env：{result['error']}")
        return result

    result.update(changed=True, reason="", written=len(fresh))
    logger.info(f"账号 {unique_id} 的 cookie 已回写 .env（{len(fresh)} 条，登录会话已更新）")
    return result
