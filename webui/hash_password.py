"""``python -m webui.hash_password`` —— 生成 WEBUI_PASSWORD_HASH 的小工具。

用法::

    python -m webui.hash_password               # 交互式输入（不回显）
    python -m webui.hash_password '我的口令'     # 命令行直接给
    echo 我的口令 | python -m webui.hash_password --stdin

输出一行 ``pbkdf2_sha256$salt$digest``，直接贴进 .env 的 WEBUI_PASSWORD_HASH。
"""

from __future__ import annotations

import sys

from webui.password import ALGORITHM, ITERATIONS, PasswordError, hash_password, random_password


def _read_interactive() -> str:
    import getpass

    first = getpass.getpass("请输入口令（不回显）：")
    if not first:
        print("口令为空，已取消。", file=sys.stderr)
        return ""
    again = getpass.getpass("请再输一次确认：")
    if first != again:
        print("两次输入不一致，已取消。", file=sys.stderr)
        return ""
    return first


def _read_stdin() -> str:
    """从管道读一行；Windows 上管道可能是 GBK，按可能出现的编码逐个试。"""
    buffer = sys.stdin.buffer
    raw = buffer.readline()
    for encoding in ("utf-8", sys.stdin.encoding or "utf-8", "gbk", "latin-1"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding).rstrip("\r\n")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace").rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if any(item in {"-h", "--help"} for item in args):
        print(__doc__)
        return 0

    if "--random" in args:
        print(f"生成的随机口令：{random_password()}")
        print("（把它填进 WEBUI_PASSWORD，或者用上面这行再算成哈希）")
        return 0

    use_stdin = "--stdin" in args
    values = [item for item in args if not item.startswith("-")]

    if values:
        password = values[0]
    elif use_stdin or not sys.stdin.isatty():
        password = _read_stdin()
    else:
        password = _read_interactive()

    if not password:
        print("没有拿到口令，已取消。", file=sys.stderr)
        return 1

    try:
        encoded = hash_password(password)
    except PasswordError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1

    print(encoded)
    print(
        f"# 算法={ALGORITHM} 轮数={ITERATIONS}（轮数已经写进哈希串本身，校验时按串里的轮数算，\n"
        f"# 所以以后调高默认轮数也不会作废这个哈希）；把上一行整行写进 .env：\n"
        f"# WEBUI_PASSWORD_HASH={encoded[:26]}…（完整值见上）",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
