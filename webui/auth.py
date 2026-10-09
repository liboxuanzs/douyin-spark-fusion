"""认证：单口令 + itsdangerous 签名 cookie（不存任何服务端状态）。

设计要点
--------
* 会话 token 只装"登录时刻的时间戳"、展示名和**口令指纹**，绝不装口令本身
  （口令的 SHA256 也不装 —— 签名已经保证了完整性，多存一份只是多一个泄露面）。
  指纹来自 ``config.credential_fingerprint()``：改口令时它变了，所有已发出的
  cookie 立刻失效（这就是"改了口令要重新登录"的实现方式，不需要服务端状态）。
* cookie 固定 ``httponly=True`` / ``same_site="lax"``；``secure`` 只在 https 请求下开，
  否则从局域网 http 访问会拿不到 cookie。
* ``same_site="lax"`` 意味着跨站 POST 不带 cookie —— 这本身就是一层 CSRF 防护；
  在此之上 ``app.py`` 还对非安全方法校验 Origin/Referer。
"""

from __future__ import annotations

from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from webui import config

__all__ = ["SESSION_SALT", "issue_token", "read_token"]

# 独立 salt：即使 WEBUI_SESSION_SECRET 被别处复用，token 也不能互换。
SESSION_SALT = "dsf-webui-session-v1"


def _serializer() -> URLSafeTimedSerializer:
    """每次调用都重建 —— 但 config.session_secret() 会缓存随机生成的那一份。"""
    return URLSafeTimedSerializer(secret_key=config.session_secret(), salt=SESSION_SALT)


def issue_token(username: str = "admin") -> str:
    """签发会话 token（内容仅时间戳 + 展示名 + 当前口令指纹）。"""
    return _serializer().dumps(
        {"u": username or "admin", "p": config.credential_fingerprint()}
    )


def read_token(token: Optional[str]) -> Optional[str]:
    """校验 token；过期 / 被篡改 / 密钥换了 / **口令改了** → 一律返回 None。"""
    if not token:
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
