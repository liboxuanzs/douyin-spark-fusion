"""HAR 录制工具（**不是测试**）—— 打开抖音聊天页录一段流量供离线分析。

用法::

    python tools/record_har.py                      # 第一个账号，录 300 秒
    python tools/record_har.py --seconds 60         # 录 60 秒
    python tools/record_har.py --account 12345678901
    python tools/record_har.py --out har_logs/my.har

产出放在 har_logs/ 下，文件名 `<用户名>_<时间戳>.har`。
用户名里的路径分隔符 / 控制字符 / `..` 等会被净化（见 `_safe_name`），
净化结果与原始名的差异会记一条日志，方便事后对账。
`--out` 可以指定到别的目录，但**不允许借它把 HAR 写到目标目录之外**。

⚠️ 产出的 HAR **含登录凭据（cookie）与好友资料（昵称/备注/uid），绝不入库**。
   har_logs/ 已在 .gitignore 里；同目录还会生成一批 <sha1>.dat（二进制报文体），
   它们与 .har 是配套的，一起移动/一起删除。

⚠️ 为什么这里用 `record_har_content="attach"`：
   Playwright 录 HAR 时二进制响应的内容不写进 .har 的 content.text，
   而是落到同目录 <sha1>.dat 并由 content._file 引用。
   解析时必须带上同目录全部 .dat，否则 protobuf 全是空壳 —— 详见 har_logs 里的取证记录。
"""

import argparse
import logging
import os
import sys
import time
import traceback
import unicodedata
from datetime import datetime

# 以 `python tools/record_har.py` 直接跑时，项目根目录不在 sys.path 里
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if os.path.exists(os.path.join(_ROOT, ".env")):
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT, ".env"))

from core.browser import close_context, get_browser  # noqa: E402
from utils.config import get_config, get_userData  # noqa: E402

try:                                           # noqa: E402
    from utils.logger import setup_logger
    logger = setup_logger("record_har")
except Exception:                              # pragma: no cover - 日志不是命脉
    # 取不到项目 logger 也不能让录制起不来：退化成标准 logging（默认打 stderr）。
    # 净化改名的告警必须留下痕迹，所以这里不回落到完全静默。
    logger = logging.getLogger("record_har")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)

HAR_LOG_DIR = os.path.join(_ROOT, "har_logs")
CHAT_URL = "https://www.douyin.com/chat"

#: 用户名净化后一个字符都不剩时的兜底名（确定性，不含任何可疑字符）。
_FALLBACK_NAME = "har"
#: 净化后的名字再长也截断 —— 文件名没人读，但撞上 255 字节上限会让整次录制失败。
_MAX_NAME_LEN = 80
#: 额外要清掉的分隔符：`\` 之外还有 Unicode 里的各种"斜杠"。
#: 这些字符在 Windows 上**也是**路径分隔符，光过滤 `/` 和 `os.sep` 挡不住。
_EXTRA_SEPARATORS = "\\/∕⁄⧵⧸⧹﹨＼"
#: Windows 非法文件名字符 + 控制字符之外还要收掉的字符。
_WINDOWS_ILLEGAL = '<>:"|?*'


def _safe_name(raw, fallback=_FALLBACK_NAME):
    """把任意用户名字段洗成一个**安全的文件名片段**。

    ⚠️ 这个函数的产出会被当成**纯名字片段**拼进 `HAR_LOG_DIR`，绝不能带路径语义。
    抖音昵称是完全不受控的输入，而 HAR 里含登录 cookie；净化漏一个字符，
    用户就能把凭据文件写到 har_logs/ 之外（`../../.ssh/...`），
    或者让一次录制直接因为 `FileNotFoundError` 崩掉。

    清洗顺序都是有原因的，别随手调整：

    1. ``NFKC`` 归一：先把全角 `／`、兼容字符收敛成 ASCII 形态，
       否则下面按码位做的过滤会被"换个长得像的字符"绕过。
    2. 剥掉所有路径分隔符（`/`、`\\` 与 Unicode 里的同类字符）。
    3. 删掉控制字符（含 `\\x00`-`\\x1f`、`\\x7f`）与 Windows 非法字符。
    4. 把 ``..`` 折叠成 `.`：`.` 单个是安全的（不是目录项），
       连着的 `..` 才是"上一级"。折叠而不是删除，是为了尽量保住原名的可读。
    5. 收掉首尾空白与点号 —— 末尾的点在 Windows 上会被系统悄悄吃掉，
       首尾空白在 `ls` 里根本看不出来（用户会以为文件没生成）。
    6. 还没内容就回落到 `fallback`（确定性兜底名）。

    中文与常规字符原样保留：用户的抖音号很可能就是中文昵称。
    """
    text = unicodedata.normalize("NFKC", str(raw if raw is not None else ""))
    text = "".join(ch for ch in text if ch not in _EXTRA_SEPARATORS)
    text = "".join(
        ch for ch in text
        if ch not in _WINDOWS_ILLEGAL and unicodedata.category(ch)[0] != "C"
    )
    # 折叠点号串：`a..b` → `a.b`，`....` → `.`（第 5 步会把剩下的点收掉）
    while ".." in text:
        text = text.replace("..", ".")
    text = text.strip().strip(". \t").strip()
    if len(text) > _MAX_NAME_LEN:
        text = text[:_MAX_NAME_LEN].strip().strip(".")
    return text or fallback


def _assert_har_path_inside(har_path, expected_dir):
    """落盘前的收敛检查：最终路径必须落在 `expected_dir` 之内。

    `_safe_name` 已经不该放出路径了，这里是**第二道锁**：万一以后有人给
    净化函数加了"保留子目录"之类的特性、或者平台路径语义有出入，
    HAR（含 cookie）也不会被写到日志目录之外 —— 宁可这次录制直接失败。

    用 `realpath` 而不是 `abspath`：要穿透 symlink 看**真实**落点，
    比较用 `commonpath` 而不是 `startswith` —— 后者会把
    `/x/har_logs_backup` 误判成"在 /x/har_logs 里面"。
    这里只比较路径、不读文件，路径里不含 cookie，写进日志是安全的。
    """
    root = os.path.realpath(expected_dir)
    target = os.path.realpath(har_path)
    try:
        inside = os.path.commonpath([root, target]) == root
    except ValueError:
        # 绝对/相对混用、或跨盘符时 commonpath 直接抛异常 —— 那就当作越界。
        inside = False
    if not inside:
        raise ValueError(
            f"拒绝把 HAR 写到 {root} 之外：{target}"
            f"（HAR 含登录凭据，绝不外溢）"
        )


def record(user, seconds, out_path=None):
    """录一个账号的 HAR，返回最终文件名。"""
    config = get_config()
    username = user.get("username", "未知用户")
    fingerprint = user.get("fingerprint", None)

    # ⚠️ 用户名是**完全不受控**的输入（抖音昵称可以是任意字符）：
    #    含 `/` 会让文件名变成一层目录 → 工具直接不可用；
    #    含 `..` 能把带 cookie 的 HAR 写到 har_logs/ 之外（本模块 docstring
    #    写明"HAR 含登录凭据，绝不入库"）。所以拼路径前先净化。
    safe_username = _safe_name(username)
    if safe_username != str(username):
        logger.info(
            f"用户名 {username!r} 含文件名非法字符，已改写为 {safe_username!r} 用于产物命名"
        )

    os.makedirs(HAR_LOG_DIR, exist_ok=True)
    if out_path:
        har_path = out_path
        # out_path 由调用方给定（命令行 --out / 别的脚本直接调 record()），
        # 这里当成**信道**而不是"受信"：它同样不许把 HAR 写到指定目录之外。
        # 指定目录取 out_path 自己所在的目录 —— 也就是说"你可以自己挑文件名，
        # 但不能借它跳到别的目录去"。
        _assert_har_path_inside(har_path, os.path.dirname(har_path) or ".")
    else:
        har_path = os.path.join(
            HAR_LOG_DIR,
            f"{safe_username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.har",
        )
        _assert_har_path_inside(har_path, HAR_LOG_DIR)

    print(f"开始录制 HAR → {har_path}")
    browser = get_browser(fingerprint)
    try:
        context = browser.new_context(
            record_har_path=har_path,
            record_har_content="attach",   # 二进制体落 <sha1>.dat（见模块 docstring）
        )
        context.set_default_navigation_timeout(config["browserActionTimeout"])
        context.set_default_timeout(config["browserActionTimeout"])
        try:
            page = context.new_page()
            context.add_cookies(user["cookies"])
            page.goto(CHAT_URL)
            print(f"页面已打开，录制 {seconds}s（期间请在页面上做要记录的操作）…")
            # 用 page.wait_for_timeout 而不是 time.sleep：
            # Playwright sync API 只在调用其接口时派发事件，纯 sleep 会漏事件。
            page.wait_for_timeout(int(seconds * 1000))
        finally:
            context.close()
    finally:
        # 同 tools/browser_probe.py：直接 close() 会漏掉 playwright 句柄回收。
        close_context(browser)

    print(f"HAR 录制完成 → {har_path}")
    print("提示：同目录的 <sha1>.dat 是配套二进制体，分析时必须一起带上；"
          "两者都含敏感信息，不要提交。")
    return har_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tools/record_har.py",
        description="录制抖音聊天页 HAR（产物含敏感信息，不入库）")
    ap.add_argument("--account", help="抖音号（.env 里 COOKIES_<抖音号> 的后缀）；默认取第一个")
    ap.add_argument("--seconds", type=float, default=300.0, help="录制时长（秒），默认 300")
    ap.add_argument(
        "--out",
        help="输出路径；默认 har_logs/<用户名>_<时间戳>.har。"
             "可以换个目录，但不允许靠 ../ 跳出该目录（HAR 含登录凭据）")
    args = ap.parse_args(argv)

    users = get_userData()
    if args.account:
        users = [u for u in users if str(u.get("unique_id")) == str(args.account)]
    if not users:
        print("❌ .env 里没读到可用账号（或 --account 不匹配）", file=sys.stderr)
        return 2

    try:
        record(users[0], args.seconds, args.out)
        return 0
    except Exception:
        print("【顶层捕获异常】", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
