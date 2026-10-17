"""认证：单口令 + itsdangerous 签名 cookie（服务端只留一份登出黑名单）。

设计要点
--------
* 会话 token 的载荷只有展示名、**口令指纹**和一个一次性随机数（签发时刻由
  itsdangerous 自己塞进签名段，``max_age`` 靠它生效），绝不装口令本身
  （口令的 SHA256 也不装 —— 签名已经保证了完整性，多存一份只是多一个泄露面）。
  指纹来自 ``config.credential_fingerprint()``：改口令时它变了，所有已发出的
  cookie 立刻失效（这就是"改了口令要重新登录"的实现方式）。
* **登出要真的登出**：token 是无状态的，光删浏览器里的 cookie 等于没作废 ——
  复制走同一个 cookie 的人在有效期内照样能用。所以登出时把 token 的摘要记进
  进程内的黑名单（``revoke_token``），``read_token`` 见到它就拒。存摘要不存原文，
  上限 ``_REVOKED_MAX`` 条（token 最多活 ``WEBUI_SESSION_MAX_AGE``，不需要无限长）。
  面板是单进程 uvicorn（``webui/__main__.py`` 只调一次 ``uvicorn.run``），
  进程内黑名单足够；重启后黑名单清空，但那时旧 cookie 还能用 —— 与"改口令才失效"
  是同一档，真要跨重启作废就得落盘，本模块刻意不引入磁盘状态。
* cookie 固定 ``httponly=True`` / ``same_site="lax"``；``secure`` 只在 https 请求下开，
  否则从局域网 http 访问会拿不到 cookie。
* ``same_site="lax"`` 意味着跨站 POST 不带 cookie —— 这本身就是一层 CSRF 防护；
  在此之上 ``app.py`` 还对非安全方法校验 Origin/Referer。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from typing import Optional, Set

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from webui import config

__all__ = ["SESSION_SALT", "issue_token", "read_token", "revoke_token", "is_revoked"]

# 独立 salt：即使 WEBUI_SESSION_SECRET 被别处复用，token 也不能互换。
SESSION_SALT = "dsf-webui-session-v1"

#: 黑名单上限：面板只有一个人用，8 小时内的登出次数远达不到这个量级。
_REVOKED_MAX = 256

_REVOKED: Set[str] = set()
_REVOKED_LOCK = threading.Lock()


def _token_id(token: str) -> str:
    """token 的稳定摘要 —— 黑名单里只存它，不存 token 原文（原文等同口令）。"""
    return hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()[:32]


def revoke_token(token: Optional[str]) -> bool:
    """把 token 拉黑（登出时调用）。返回是否真的记下了（空 token 返回 False）。"""
    if not token:
        return False
    digest = _token_id(token)
    with _REVOKED_LOCK:
        _REVOKED.add(digest)
        overflow = len(_REVOKED) - _REVOKED_MAX
        if overflow > 0:
            # 集合无序，丢哪几条都行：被丢掉的只是"更早登出的 token"，
            # 它们本来也快过期了。
            for old in list(_REVOKED)[:overflow]:
                _REVOKED.discard(old)
    return True


def is_revoked(token: Optional[str]) -> bool:
    """这个 token 是不是已经登出过。"""
    if not token:
        return False
    with _REVOKED_LOCK:
        return _token_id(token) in _REVOKED


def _serializer() -> URLSafeTimedSerializer:
    """每次调用都重建 —— 但 config.session_secret() 会缓存随机生成的那一份。"""
    return URLSafeTimedSerializer(secret_key=config.session_secret(), salt=SESSION_SALT)


def issue_token(username: str = "admin") -> str:
    """签发会话 token（内容仅时间戳 + 展示名 + 当前口令指纹 + 一次性随机数）。

    ★ `j` 这个随机数不是装饰：itsdangerous 的时间戳只精确到**秒**，载荷里又没有
    别的变量 —— 少了它，同一秒内签发的 token **完全相同**。后果不是理论上的：
    登出黑名单是按 token 原文的摘要记的，于是"登出"会把同一秒里另一个浏览器
    签发的会话一起踢掉；更常见的是"登出 → 立刻重新登录"落在同一秒里，新会话
    一签发就已经在黑名单上（表现为刚登录就被弹回登录页）。
    每次签发都换一个 16 位十六进制随机数，黑名单就只作用于它自己那一条。
    """
    return _serializer().dumps(
        {
            "u": username or "admin",
            "p": config.credential_fingerprint(),
            "j": secrets.token_hex(8),
        }
    )


def read_token(token: Optional[str]) -> Optional[str]:
    """校验 token；过期 / 被篡改 / 密钥换了 / **口令改了** / **已登出** → 返回 None。"""
    if not token:
        return None
    if is_revoked(token):
        # 登出后复制走的那份 cookie：签名仍然有效，但它已经在黑名单里。
        return None
    try:
        payload = _serializer().loads(token, max_age=config.session_max_age())
    except (BadSignature, SignatureExpired):
        return None
    except Exception:  # 任何意外都不该把请求打成 500
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("p") or "") != config.credential_fingerprint():
        # 口令在别处被改过（面板里改的、或者直接编辑 .env 后重启）：旧会话作废。
        return None
    return str(payload.get("u") or "admin")
