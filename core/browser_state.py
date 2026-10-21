"""每个账号的浏览器状态快照（Playwright `storage_state`）。

为什么要有这一层
----------------
发送路径 `core/tasks.do_user_task` 每轮都是 `browser.new_context()` —— 一个全新的
无痕上下文。除了 `.env` 里那份登录快照，什么都没留下：

* `localStorage`（抖音在里面放设备/实验分组标记，扫码登录那一路是累积下来的）；
* 服务端在页面上**新签发**的 cookie（`x-web-secsdk-uid`、刷新后的 `ttwid`、
  `web_sign_token` 里那个时间戳）；
* IndexedDB 等其它 storage。

于是每一轮都像"一台刚装好的浏览器拿着一张旧登录票"：同一份 `sessionid` 背后
不断出现新的设备号。2026-10-20 两个账号同日被 `KICK` 之后，这是最值得先消掉的
痕迹（调查见交付文档"掉登录根因调查"）。

现在的做法
----------
每轮收尾（`context.close()` 之前）把 `context.storage_state()` 存成
`logs/browser-state/<抖音号>.json`（0600、原子替换），下一轮
`new_context(storage_state=...)` 装回去。cookie 的优先级由 `same_session()` 决定：

* 快照里的登录会话与 `.env` **是同一份** → 只信快照（它更全，且服务端新签发的
  值都在里面），不再拿 `.env` 的旧值覆盖一遍；
* 不是同一份（用户重新扫码 / 手动换了 `COOKIES_*`）→ `.env` 赢，但快照里
  其余的 cookie 与 localStorage 继续用（换票不该换设备）。

除了每轮发送收尾，**扫码登录成功时也会写一份**（`core/qr_login._save_device_snapshot`）：
登录用的持久化资料目录与发送用的新上下文本来是两套设备身份（实测 `web_runtime_security_uid`
分别是 `283d8355…` 与 `21d1e1d3…`），登录时整份写过来，发送才会沿用"登录这一台"。

失败永远是"降级"而不是"报错"：读不出来按全新上下文跑，写不进去只记一条 warning
—— 这一层是锦上添花，不能因为它让整轮发送失败。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path

from core import cookie_utils
from utils.config import resolve_path

logger = logging.getLogger(__name__)

#: 快照放哪：默认 `<仓库根>/logs/browser-state`。测试与多实例部署用环境变量改，
#: 免得用例把状态写进仓库、也免得两套实例互相踩。
STATE_DIR_ENV = "BROWSER_STATE_DIR"
DEFAULT_DIR = os.path.join("logs", "browser-state")

#: 抖音号已经有一道字符集校验（`COOKIES_<它>` 要当变量名），这里按文件名再收一次：
#: 不允许路径分隔符、不允许 `..`、不允许空。
#: 用 `cookie_utils.UNIQUE_ID_RE` 的同一套字符集，只是更短。
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def state_dir() -> Path:
    """状态目录（不存在也可以返回，写入时再建）。"""
    override = os.getenv(STATE_DIR_ENV) or ""
    if override.strip():
        return Path(os.path.expanduser(override.strip())).resolve()
    return Path(resolve_path(DEFAULT_DIR))


def state_path(unique_id) -> Path | None:
    """`<状态目录>/<抖音号>.json`；抖音号不合法时返回 None（宁可不存，也别存到怪路径）。"""
    uid = str(unique_id or "").strip()
    if not _NAME_RE.match(uid):
        return None
    return state_dir() / f"{uid}.json"


def read(unique_id) -> dict | None:
    """读回快照；没有、读坏了、结构不对都返回 None（调用方走"全新上下文"）。"""
    path = state_path(unique_id)
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # 只报文件名，不报内容：这个文件里是整份登录态。
        logger.warning(f"⚠️ 浏览器状态快照读不出来（{path.name}）：{exc} —— 本轮按全新上下文跑")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        logger.warning(f"⚠️ 浏览器状态快照结构不对（{path.name}），本轮按全新上下文跑")
        return None
    return data


def write(unique_id, state) -> Path | None:
    """保存快照（原子替换 + 0600）。失败只记 warning，返回 None。"""
    path = state_path(unique_id)
    if path is None:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return None

    payload = _trim(state)
    if not payload["cookies"]:
        # 一条目标域的 cookie 都没有：存下去只有害处（下轮会拿一个空 jar 覆盖注入）
        return None

    directory = path.parent
    tmp_name = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _lock_down_dir(directory)
        handle_fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        tmp_name = None
        return path
    except OSError as exc:
        logger.warning(f"⚠️ 浏览器状态快照写不进去（{path.name}）：{exc}")
        return None
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _lock_down_dir(directory: Path) -> None:
    """状态目录只给本用户读：里面就是整份登录态。已有目录不强行改权限（可能被别人共享）。"""
    try:
        mode = directory.stat().st_mode & 0o777
        if mode != 0o700:
            os.chmod(directory, 0o700)
    except OSError:
        pass


def _trim(state: dict) -> dict:
    """只留抖音相关域的 cookie，`origins` 原样保留（localStorage 就在这里）。"""
    cookies = []
    for cookie in state.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if not cookie_utils.matches_target(domain):
            continue
        if not str(cookie.get("name") or "").strip():
            continue
        cookies.append(cookie)
    origins = state.get("origins")
    return {
        "cookies": cookies,
        "origins": origins if isinstance(origins, list) else [],
    }


def same_session(state: dict, cookies) -> bool:
    """快照里的登录会话与给定 cookie 是不是**同一份**。

    只看"有没有 sessionid"不够（名字在、值早被服务端换掉了），见
    `core/cookie_utils.login_cookie_signature` 的说明。
    """
    if not isinstance(state, dict):
        return False
    state_sig = cookie_utils.login_cookie_signature(state.get("cookies") or [])
    return bool(state_sig) and state_sig == cookie_utils.login_cookie_signature(cookies or [])


def storage_state_for(unique_id) -> dict | None:
    """给 `new_context(storage_state=...)` 用的那份；没有就 None（调用方别传这个参数）。"""
    return read(unique_id)
