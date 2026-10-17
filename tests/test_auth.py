"""会话 token 的测试：签发 / 校验 / 登出拉黑。

只依赖 ``itsdangerous``（``webui/auth.py`` 的全部外部依赖）；没有它的机器上整类跳过
—— 面板自己的端到端用例在 ``tests/test_webui.py``（需要 fastapi）。

为什么值得单独测：token 是无状态的，**登出如果不拉黑就等于没登出** ——
复制走同一份 cookie 的人在有效期内照样能用。这条安全线只在本文件里有覆盖。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:  # itsdangerous 是 webui/auth.py 的唯一外部依赖
    from webui import auth

    AUTH_OK = True
except Exception:  # noqa: BLE001 —— 缺依赖时整类跳过，而不是让测试套炸掉
    auth = None  # type: ignore[assignment]
    AUTH_OK = False


@unittest.skipUnless(AUTH_OK, "本机没有 itsdangerous：跳过 webui.auth 的用例")
class TokenRevokeTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都从干净的黑名单开始：模块级状态跨用例会互相干扰。
        with auth._REVOKED_LOCK:
            auth._REVOKED.clear()

    def tearDown(self) -> None:
        with auth._REVOKED_LOCK:
            auth._REVOKED.clear()

    def test_签发与校验往返(self) -> None:
        token = auth.issue_token("admin")
        self.assertTrue(token)
        self.assertEqual(auth.read_token(token), "admin")
        self.assertIsNone(auth.read_token(None))
        self.assertIsNone(auth.read_token(""))

    def test_登出后同一个_token_立刻失效(self) -> None:
        token = auth.issue_token("admin")
        self.assertEqual(auth.read_token(token), "admin")
        self.assertTrue(auth.revoke_token(token))
        # ★ 这就是"复制走 cookie 也能继续用"的堵口：签名没变、但已进黑名单。
        self.assertIsNone(auth.read_token(token))
        self.assertTrue(auth.is_revoked(token))

    def test_登出一个_token_不影响另一个(self) -> None:
        # ★ 载荷里有一次性随机数（`j`），所以**同一秒、同一个用户名**签发的
        #   token 也各不相同 —— 这正是"登出只踢自己那一条"的前提。
        first = auth.issue_token("admin")
        second = auth.issue_token("admin")
        self.assertNotEqual(first, second, "同秒同名的两个会话不能是同一个 token")
        auth.revoke_token(first)
        self.assertIsNone(auth.read_token(first))
        self.assertEqual(auth.read_token(second), "admin")
        third = auth.issue_token("另一个人")
        self.assertEqual(auth.read_token(third), "另一个人")

    def test_同秒签发的_token_互不相同(self) -> None:
        """回归：载荷没有随机数时，同秒签发的 token 完全相同。

        症状（2026-10-14 远端全量里 237 条 webui 失败的真凶）：`test_登出会清掉会话`
        先跑，把那一秒的 token 摘要拉黑；同一秒里后续每个用例重新登录拿到的都是
        **同一个** token → 一登录就被判成已登出，整批用例变成"没登录"。
        """
        tokens = {auth.issue_token("admin") for _ in range(5)}
        self.assertEqual(len(tokens), 5, "每次签发都必须是独立会话")
        for token in tokens:
            self.assertEqual(auth.read_token(token), "admin")

    def test_空_token_不算拉黑(self) -> None:
        self.assertFalse(auth.revoke_token(None))
        self.assertFalse(auth.revoke_token(""))
        self.assertFalse(auth.is_revoked(None))
        self.assertFalse(auth.is_revoked(""))

    def test_黑名单只存摘要不存原文(self) -> None:
        token = auth.issue_token("admin")
        auth.revoke_token(token)
        with auth._REVOKED_LOCK:
            stored = set(auth._REVOKED)
        # token 原文等同口令：黑名单里出现它等于把口令写进了内存日志面。
        self.assertNotIn(token, stored)
        self.assertIn(auth._token_id(token), stored)

    def test_黑名单有上限且不无限增长(self) -> None:
        limit = auth._REVOKED_MAX
        self.assertGreaterEqual(limit, 8)
        for _ in range(limit + 5):
            auth.revoke_token(auth.issue_token("admin"))
        with auth._REVOKED_LOCK:
            self.assertLessEqual(len(auth._REVOKED), limit)

    def test_篡改过的_token_依然过不了签名(self) -> None:
        token = auth.issue_token("admin")
        payload, _, rest = token.partition(".")
        # 改**载荷段**的第一个字符：末尾那个字符承载的位数不足一字节，
        # 换掉它可能解出同样的签名，测不出"签名没校验"。
        forged = ("B" if payload[0] != "B" else "C") + payload[1:] + "." + rest
        self.assertNotEqual(forged, token)
        self.assertIsNone(auth.read_token(forged))

    def test_口令指纹变了旧_token_作废(self) -> None:
        token = auth.issue_token("admin")
        with mock.patch.object(auth.config, "credential_fingerprint", return_value="另一个指纹"):
            self.assertIsNone(auth.read_token(token))


class LogoutWiringTests(unittest.TestCase):
    """登出路由必须真的调用拉黑（路由本身要 fastapi 才能跑，这里查源码接线）。"""

    def test_登出路由调用_revoke_token(self) -> None:
        source = (REPO_ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/logout")')
        body = source[start : start + 900]
        self.assertIn("auth.revoke_token(", body)
        # 顺序：先拉黑，再删 cookie —— 反过来会出现"删了 cookie 但没作废 token"。
        self.assertLess(body.index("auth.revoke_token("), body.index("delete_cookie("))
        # 拉黑的必须是**请求里带来的**那个 token，而不是新签一个。
        self.assertIn("request.cookies.get(", body)


if __name__ == "__main__":
    unittest.main()
