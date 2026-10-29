# -*- coding: utf-8 -*-
"""每账号出口（issue #18）：解析、遮掩、自检。

为什么值得单独一个模块：出口 IP 是这个项目**最贵的一条经验** —— README 与 FAQ 都把
"掉登录"的头号原因记在"出口 IP 变了"上，可面板以前从不显示谁走哪个出口。而出口地址
里通常带着代理口令（`http://user:pass@host:port`），一旦原样打进日志、渲染到页面、
或者塞进账本，就等于把口令抄在所有看得到面板的人眼前。

所以这里的规矩只有两条：

1. **口令永不外露**：`mask()` 是唯一允许出现在界面/日志里的形态；
2. **自检只在人点了按钮时联网一次**，不轮询、不在发送链路里加网络依赖。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

__all__ = [
    "normalize",
    "mask",
    "describe",
    "source_text",
    "check",
    "read_state",
    "write_state",
    "state_for",
    "CHECK_URL",
    "STATE_FILE",
]

# 自检用的回显服务：只回一行 IP，不带任何业务语义。
CHECK_URL = "https://api.ipify.org?format=json"

STATE_FILE = os.path.join("logs", "proxy_state.json")

# 支持 socks5 是因为 cloakbrowser/playwright 都吃，跟着 .env 的既有写法走。
_ALLOWED_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4"}


def _split(text: str):
    """urlsplit 的保险版：写坏的 URL（`http://[::1` 这种）会抛 ValueError。

    这一层不能省：出口地址是用户手填的，一个写坏的方括号就能让 urlsplit 抛错，而
    调用它的地方是**渲染首页**（_attach_proxy 给每张卡算「出口」那一行）。让它抛，
    首页就是 500 —— 填错一个出口地址，整块面板打不开。
    """
    try:
        return urlsplit(text)
    except ValueError:
        return None


def normalize(raw: Any) -> str:
    """把用户写的出口地址归一成 `scheme://[user:pass@]host:port`；空串表示直连。

    不做过度校验：写错了要**在界面上说得出来**，而不是悄悄当成直连 —— 静默降级
    会让"配了代理却仍在直连"这种事故看起来一切正常。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def problem(raw: Any) -> str:
    """校验出口地址；没问题返回空串，有问题返回一句给用户看的话。"""
    text = normalize(raw)
    if not text:
        return ""
    parts = _split(text)
    if parts is None:
        return "出口地址看不懂（URL 写坏了；IPv6 要写成 [::1]:1080）"
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"出口只支持 http/https/socks5 这几种协议，写的是 {parts.scheme}://"
    if not parts.hostname:
        return "出口地址里没有主机名（应该写成 host:port）"
    try:
        port = parts.port
    except ValueError:
        return "出口地址里的端口不是数字"
    if port is None:
        return "出口地址缺少端口（应该写成 host:port）"
    return ""


def mask(raw: Any) -> str:
    """界面上唯一允许的形态：口令换成 ***，其余照旧。"""
    text = normalize(raw)
    if not text:
        return ""
    parts = _split(text)
    if parts is None:
        # 解析不了就**什么都不给**：宁可显示"***"，也不能把原文（可能带口令）漏出去
        return "***"
    host = parts.hostname or ""
    port = parts.port
    host_text = f"{host}:{port}" if port else host
    # 与面板表单（webui/store.py::mask_proxy）逐字一致：有口令就把口令换成 ***、
    # 用户名留着（那是账号，不是秘密）；**只有用户名**的那种形态整段遮掉 —— 出口
    # 口令经常就是这一串，露一半等于没遮。
    if parts.password:
        userinfo = f"{parts.username or ''}:***@"
    elif parts.username:
        userinfo = "***@"
    else:
        userinfo = ""
    return f"{parts.scheme}://{userinfo}{host_text}"


def describe(raw: Any) -> str:
    """账号卡上那一行：直连，或者 host:port（连协议都省掉，够认了）。"""
    text = normalize(raw)
    if not text:
        return "直连"
    parts = _split(text)
    if parts is None:
        return "出口地址写坏了"
    host = parts.hostname or text
    port = parts.port
    return f"{host}:{port}" if port else host


def source_text(own: Any, global_proxy: Any) -> str:
    """这句告诉用户"这个出口是从哪来的"，改错地方时能自己发现。"""
    if normalize(own):
        return "这个账号单独配置"
    if normalize(global_proxy):
        return "全局 PROXY_ADDRESS"
    return "没有配置代理（直连）"


def check(raw: Any = "", *, fetcher: Optional[Any] = None, timeout: float = 8.0) -> Dict[str, Any]:
    """用这个出口访问一次回显服务，返回 {ok, ip, error, via}。

    `fetcher` 只为测试注入；返回值里**永远不含口令**（nor 原始异常里的 URL，
    因为 requests 会把代理地址（含口令）拼进异常文案）。
    """
    url = normalize(raw)
    label = mask(url) or "直连"
    if fetcher is None:
        try:
            import requests

            def fetcher(target: str, proxies: Dict[str, str], timeout: float):
                return requests.get(target, proxies=proxies, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 缺依赖也要给人话
            return {"ok": False, "ip": "", "via": label, "error": f"没法发起请求：{exc}"}

    proxies = {"http": url, "https": url} if url else {}
    try:
        response = fetcher(CHECK_URL, proxies, timeout)
    except Exception as exc:  # noqa: BLE001 - 网络异常种类太多，统一归一
        text = str(exc)
        if url:
            text = text.replace(url, label)
        return {"ok": False, "ip": "", "via": label, "error": f"连不上：{text}"}
    status = getattr(response, "status_code", 0)
    if status and status >= 400:
        return {"ok": False, "ip": "", "via": label, "error": f"回显服务返回 {status}"}
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 有的服务回纯文本
        payload = None
    ip = ""
    if isinstance(payload, dict):
        ip = str(payload.get("ip") or "").strip()
    if not ip:
        raw_text = str(getattr(response, "text", "") or "").strip()
        ip = raw_text.splitlines()[0].strip() if raw_text else ""
    ip = str(ip or "").strip()
    if not ip:
        return {"ok": False, "ip": "", "via": label, "error": "回显里没有 IP"}
    return {"ok": True, "ip": ip, "via": label, "error": ""}


def _state_path(path: Optional[str] = None) -> str:
    if path:
        return path
    try:
        from utils.config import resolve_path

        return resolve_path(STATE_FILE)
    except Exception:  # noqa: BLE001 - 读不到配置也不能让自检崩
        return STATE_FILE


def read_state(path: Optional[str] = None) -> Dict[str, Any]:
    target = _state_path(path)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: Dict[str, Any], path: Optional[str] = None) -> bool:
    target = _state_path(path)
    try:
        folder = os.path.dirname(target)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def state_for(uid: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """取出某个账号上一次自检的记录（可能为空）。"""
    accounts = state.get("accounts") if isinstance(state, dict) else None
    if not isinstance(accounts, dict):
        return {}
    item = accounts.get(str(uid or ""))
    return item if isinstance(item, dict) else {}
