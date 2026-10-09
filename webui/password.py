"""口令哈希：``pbkdf2_sha256$<轮数>$<salt_hex>$<digest_hex>``（PBKDF2-HMAC-SHA256，默认 480000 轮）。

只依赖标准库 ``hashlib`` / ``hmac`` / ``secrets``，不引入 passlib 之类的新依赖。
口令一律按 UTF-8 编码 —— 中文口令也能正常用。

本模块不读任何环境变量，可以在 import 期安全使用。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = [
    "ALGORITHM",
    "ITERATIONS",
    "MAX_LENGTH",
    "MIN_LENGTH",
    "SALT_BYTES",
    "PasswordError",
    "hash_password",
    "parse_hash",
    "random_password",
    "validate_new_password",
    "verify_hash",
    "verify_plain",
]

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 480000
SALT_BYTES = 16
DIGEST_BYTES = 32

#: 面板里设口令时的长度口径（``webui/app.py`` 的 ``/ops/password`` 用）。
MIN_LENGTH = 8
MAX_LENGTH = 128


class PasswordError(ValueError):
    """口令或哈希串本身不合法（例如 UTF-16 代理对未配对）。"""


def _password_bytes(password: str) -> bytes:
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError as exc:  # 极端情况：控制台给出的孤立代理字符
        raise PasswordError(
            "口令包含无法用 UTF-8 编码的字符（可能是控制台编码问题）。"
            "请改用 ASCII 口令，或设置 PYTHONUTF8=1 后重试。"
        ) from exc


def hash_password(password: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    """把明文口令哈希成 ``pbkdf2_sha256$<轮数>$salt_hex$digest_hex``。

    **轮数写进哈希串里**（早期版本没写，这是个真 bug）：``iterations`` 是本函数的
    公开参数，但校验时只按模块默认值重算，后果是

    * 用非默认轮数生成的哈希**永远校验不过**（``hash_password(pw, iterations=1000)``
      的结果会被按 480000 轮重算，必然不匹配），这个参数等于摆设；
    * 以后想调高 ``ITERATIONS`` 会一次性作废所有已部署的哈希，且没有任何提示。

    轮数自带后，校验方按串里写的轮数算，两件事都不会发生。
    """
    if password is None or password == "":
        raise PasswordError("口令不能为空")
    rounds = int(iterations)
    if rounds < 1:
        raise PasswordError("轮数必须是正整数")
    salt_bytes = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", _password_bytes(password), salt_bytes, rounds)
    return f"{ALGORITHM}${rounds}${salt_bytes.hex()}${digest.hex()}"


def parse_hash(encoded: str) -> tuple[bytes, bytes, int] | None:
    """拆哈希串 → ``(salt, digest, iterations)``；格式不合法返回 ``None``。

    兼容两种写法：

    * ``pbkdf2_sha256$<轮数>$<salt_hex>$<digest_hex>`` —— 现在 ``hash_password`` 生成的；
    * ``pbkdf2_sha256$<salt_hex>$<digest_hex>`` —— 早期没有轮数字段的格式，
      按模块默认 ``ITERATIONS`` 校验（老 ``.env`` 里的哈希仍然能登录，不需要重新生成）。
    """
    if not encoded:
        return None
    parts = encoded.strip().split("$")
    if not parts or parts[0] != ALGORITHM:
        return None
    if len(parts) == 4:
        try:
            rounds = int(parts[1])
        except (TypeError, ValueError):
            return None
        if rounds < 1:
            return None
        salt_hex, digest_hex = parts[2], parts[3]
    elif len(parts) == 3:
        rounds = ITERATIONS
        salt_hex, digest_hex = parts[1], parts[2]
    else:
        return None
    try:
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except ValueError:
        return None
    if not salt or not digest:
        return None
    return salt, digest, rounds


def verify_hash(password: str, encoded: str) -> bool:
    """校验明文口令与哈希串是否匹配；任何格式问题都返回 False（不抛异常）。

    轮数从哈希串里读（见 ``hash_password`` / ``parse_hash``），
    用 ``hmac.compare_digest`` 做定长比较，避免时序侧信道。
    """
    if not password or not encoded:
        return False
    parsed = parse_hash(encoded)
    if parsed is None:
        return False
    salt, expected, iterations = parsed
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256", _password_bytes(password), salt, iterations, dklen=len(expected)
        )
    except PasswordError:
        return False
    return hmac.compare_digest(actual, expected)


def verify_plain(password: str, plain: str) -> bool:
    """明文口令比对（同样用定长比较）。"""
    if not password or not plain:
        return False
    try:
        return hmac.compare_digest(_password_bytes(password), _password_bytes(plain))
    except PasswordError:
        return False


def random_password(length: int = 24) -> str:
    """生成一个随机口令（给部署脚本用；webui 内部不会自动调用）。"""
    return secrets.token_urlsafe(max(8, int(length)))


def validate_new_password(password_value: str) -> str:
    """面板里"自己设口令"的准入检查：返回错误原因，通过则返回空串。

    规则只有三条，都是被真实约束逼出来的，不是洁癖：

    * **长度 8~128** —— 太短没意义，太长会拖慢每次登录的 PBKDF2（48 万轮）；
    * **不许有换行** —— 换行会把 `.env` 的一行拆成两行，后面所有键都被顶掉；
    * **不许有英文单引号** —— 写回 `.env` 用 ``KEY='值'`` 这种引号形式，
      值里再出现 ``'`` 就没有可靠的转义写法（python-dotenv 的 ``set_key``
      不转义引号，dotenv 解析会截断）。

    其余字符（空白、``#``、``$``、``"``、``\\``、中文）都能原样往返 ——
    **只有 ``${`` 不行**，理由见下面那条检查。
    """
    value = password_value if isinstance(password_value, str) else ""
    if not value:
        return "新口令不能为空"
    # 换行/单引号先于长度检查：从文件里粘贴口令时最常见的失误就是尾巴上带一个换行，
    # 此时"至少 8 位"这种提示会把人带偏，不如直接说清楚问题在哪。
    if "\n" in value or "\r" in value:
        return "新口令不能包含换行（多半是从文件里粘贴时带上了行尾）"
    if "'" in value:
        return "新口令不能包含英文单引号 '（写进 .env 时它是引号定界符）"
    if "${" in value:
        # 实测（python-dotenv 读 `KEY='值'`）：`a${b}c` 读回来是 `ac`，
        # `${HOME}x` 读回本机的 `/home/<用户>x`。**引号挡不住变量插值**，
        # 唯一的办法就是别让这种口令进文件 —— 否则写进去的和输入的不一样，
        # 而且从此"新口令怎么都不对"，用户完全看不出是 .env 干的。
        return "新口令不能包含 ${（.env 会把它当变量插值，写进去的和你输入的不一样）"
    if len(value) < MIN_LENGTH:
        return f"新口令至少 {MIN_LENGTH} 位（现在 {len(value)} 位）"
    if len(value) > MAX_LENGTH:
        return f"新口令最多 {MAX_LENGTH} 位（现在 {len(value)} 位）"
    return ""
