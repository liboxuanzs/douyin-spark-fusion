"""浏览器启动。首选 cloakbrowser（自带拟人化），失败时回落原生 Playwright。

融合版相比上游改了三处，都是被 .172 的实测逼出来的：

1. **惰性 import**：上游在模块级 `from cloakbrowser import launch`，导致只要没装
   cloakbrowser，`import core.browser`（进而 `core.tasks`）就直接失败 —— 本机
   做离线单测、webui 在没有浏览器依赖的机器上启动都会被拖死。改到函数里 import。
2. **root 下自动 `--no-sandbox`**：Ubuntu 24.04 上
   `kernel.apparmor_restrict_unprivileged_userns = 1`，Chromium 的 user namespace
   沙箱用不了、setuid 沙箱在 root 下也不可用，不加这个参数浏览器**起不来**。
3. **Playwright 回落**：cloakbrowser 的浏览器二进制要另外下载，机房网络下不保证
   成功；回落时优先用 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` / `CLOAKBROWSER_BINARY_PATH`
   指定的二进制，其次 `channel="chrome"` 复用系统 Chrome。两条路都实测可用。
"""
import os
import traceback

from utils.config import DEBUG, get_config
from utils.logger import setup_logger

logger = setup_logger("browser", level=get_config().get("logLevel", "Info"))

# 浏览器二进制的定位走 cloakbrowser 的约定（不是 Playwright 的 PLAYWRIGHT_BROWSERS_PATH）：
#   CLOAKBROWSER_BINARY_PATH  二进制绝对路径
#   CLOAKBROWSER_AUTO_UPDATE  是否允许自更新
# Docker 里由 Dockerfile 的 ENV 统一给出（/opt/cloakbrowser/chrome）；
# 本地没设时回落到仓库自带的 chrome/ 目录。用 setdefault 是为了不覆盖 Docker 的值。
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_CHROME = _REPO_ROOT / "chrome" / ("chrome.exe" if os.name == "nt" else "chrome")
if "CLOAKBROWSER_BINARY_PATH" not in os.environ and _LOCAL_CHROME.exists():
    os.environ["CLOAKBROWSER_BINARY_PATH"] = str(_LOCAL_CHROME)
os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")

BASE_CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-popup-blocking",
    "--disable-background-networking",
    "--metrics-recording-only",
    "--ignore-gpu-blocklist",
    "--disable-gpu",
    "--enable-unsafe-swiftshader",
]

# playwright 回落时的 `sync_playwright()` 句柄必须活到浏览器关闭，
# 否则一旦被 GC 回收，浏览器进程会连着一起被杀。
#
# 元素是 `(句柄, 它启动的那个上下文/浏览器)` 二元组：光留着句柄会让每个 driver
# 子进程（node）**只增不减** —— 每开一次浏览器漏一个，跑几天机器上全是 node。
# 关的时候按对象身份找回自己的句柄，`stop()` 掉再摘除（见 `close_context`）。
_KEEPALIVE = []


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _needs_no_sandbox():
    """root（setuid 沙箱不可用）或显式要求时，必须关掉 Chromium 沙箱。"""
    flag = os.environ.get("CHROME_NO_SANDBOX", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return _is_root()


def chrome_args(fingerprint=None):
    """本次启动用的 Chromium 参数（含指纹与沙箱处理）。"""
    args = list(BASE_CHROME_ARGS)
    if _needs_no_sandbox():
        # 两个都给：`--no-sandbox` 是开关，`--disable-setuid-sandbox` 防止它回头去找
        # setuid 辅助进程（找不到会直接崩）。
        args += ["--no-sandbox", "--disable-setuid-sandbox"]
    if fingerprint:
        args.append(f"--fingerprint={str(fingerprint)}")
    return args


def _launch_cloakbrowser(headless, args, proxy):
    from cloakbrowser import launch          # 惰性：没装也要能 import 本模块
    kw = {"headless": headless, "humanize": True, "args": args}
    if proxy:
        kw["proxy"] = proxy
    return launch(**kw)


def _launch_playwright(headless, args, proxy):
    from playwright.sync_api import sync_playwright
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or os.environ.get("CLOAKBROWSER_BINARY_PATH")
    pw = sync_playwright().start()
    kw = {"headless": headless, "args": args}
    if exe and os.path.exists(exe):
        kw["executable_path"] = exe
    elif os.environ.get("PLAYWRIGHT_CHANNEL"):
        kw["channel"] = os.environ["PLAYWRIGHT_CHANNEL"]
    elif os.name != "nt" and os.path.exists("/opt/google/chrome/chrome"):
        # 实测可用的兜底：系统 Chrome（不带 channel 参数时 Playwright 会去找自己
        # 下载的 Chromium，机房下不保证存在）。
        kw["executable_path"] = "/opt/google/chrome/chrome"
    if proxy:
        kw["proxy"] = {"server": proxy}
    browser = pw.chromium.launch(**kw)
    _KEEPALIVE.append((pw, browser))
    return browser


def launch_persistent_context(profile_dir, *, fingerprint=None, proxy=None,
                              headless=True, args=None):
    """启动**持久化**上下文（cloakbrowser 优先，失败回落 Playwright）。

    和 `get_browser()` 的区别：这里用固定的用户资料目录，浏览器带着同一个指纹与
    设备信息反复打开同一个账号 —— 风控看到的是「同一台设备再来一次」，而不是
    「又一台新设备」。扫码登录（`core.qr_login`）与读好友列表（`core.friend_scan`）
    都用它。

    失败时抛 RuntimeError（与 `get_browser()` 同口径，调用方写进状态/账本）。
    """
    profile_dir = str(profile_dir)
    if args is None:
        args = chrome_args(fingerprint)
    backend = (os.environ.get("BROWSER_BACKEND") or "auto").strip().lower()
    order = ["cloakbrowser", "playwright"] if backend == "auto" else [backend]
    errors = []
    for name in order:
        try:
            if name == "cloakbrowser":
                from cloakbrowser import launch_persistent_context as _launch
                kw = {"headless": headless, "humanize": True, "args": args}
                if proxy:
                    kw["proxy"] = proxy
                return _launch(profile_dir, **kw)
            if name == "playwright":
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                kw = {"headless": headless, "args": args}
                exe = (os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
                       or os.environ.get("CLOAKBROWSER_BINARY_PATH"))
                if exe and os.path.exists(exe):
                    kw["executable_path"] = exe
                elif os.environ.get("PLAYWRIGHT_CHANNEL"):
                    kw["channel"] = os.environ["PLAYWRIGHT_CHANNEL"]
                elif os.name != "nt" and os.path.exists("/opt/google/chrome/chrome"):
                    kw["executable_path"] = "/opt/google/chrome/chrome"
                if proxy:
                    kw["proxy"] = {"server": proxy}
                ctx = pw.chromium.launch_persistent_context(profile_dir, **kw)
                # 句柄必须活到浏览器关闭，否则被 GC 时浏览器一起死（见 _KEEPALIVE）
                _KEEPALIVE.append((pw, ctx))
                return ctx
            raise ValueError(f"未知的 BROWSER_BACKEND：{backend}")
        except Exception as e:                       # noqa: BLE001 —— 要跨后端串起来报
            errors.append(f"{name}: {type(e).__name__}: {e}")
            if "Executable doesn't exist" in str(e):
                errors[-1] += "（浏览器可执行文件不存在，请先安装浏览器或设置 CLOAKBROWSER_BINARY_PATH）"
            traceback.print_exc()
    raise RuntimeError("浏览器启动失败 —— " + " | ".join(errors))


def close_context(target) -> bool:
    """关掉一个由本模块启动的上下文/浏览器，并把它自己的 playwright 句柄一起回收。

    返回**是否关干净**（失败已记日志）：关不干净就别去动它的用户资料目录 ——
    目录还被活着的那只 Chromium 占着，改名只会留下僵尸进程和 SingletonLock。

    为什么要有这个函数：`_KEEPALIVE` 以前只 `append` 从不回收，Playwright 回落
    路径每开一次浏览器就漏一个 driver 子进程（node）。调用方以前散着写
    `ctx.close()` / `browser.close()`，谁都没想到句柄也要还。
    """
    ok = True
    try:
        target.close()
    except Exception as exc:                             # noqa: BLE001
        ok = False
        logger.warning(f"[BROWSER] ⚠️ 关浏览器失败：{type(exc).__name__}: {exc}")
    for entry in list(_KEEPALIVE):
        pw, owner = entry
        if owner is not target:
            continue
        _KEEPALIVE.remove(entry)
        try:
            pw.stop()
        except Exception as exc:                         # noqa: BLE001
            logger.warning(f"[BROWSER] ⚠️ 回收 playwright 句柄失败：{type(exc).__name__}: {exc}")
    return ok


def get_browser(fingerprint=None):
    """
    启动浏览器实例。

    后端由 `BROWSER_BACKEND` 决定：`auto`（默认，cloakbrowser 失败则回落 Playwright）、
    `cloakbrowser`、`playwright`。失败时抛 RuntimeError（**不再 sys.exit**：
    库函数杀掉进程会让整轮任务的错误处理失效，调用方需要拿到异常写进账本）。
    """
    cfg = get_config()
    proxyAddress = cfg.get("proxyAddress") or ""
    headless = not DEBUG
    args = chrome_args(fingerprint)
    backend = (os.environ.get("BROWSER_BACKEND") or "auto").strip().lower()

    order = ["cloakbrowser", "playwright"] if backend == "auto" else [backend]
    errors = []
    for name in order:
        try:
            if name == "cloakbrowser":
                return _launch_cloakbrowser(headless, args, proxyAddress)
            if name == "playwright":
                return _launch_playwright(headless, args, proxyAddress)
            raise ValueError(f"未知的 BROWSER_BACKEND：{backend}")
        except Exception as e:                       # noqa: BLE001 —— 要跨后端串起来报
            errors.append(f"{name}: {type(e).__name__}: {e}")
            if "Executable doesn't exist" in str(e):
                errors[-1] += "（浏览器可执行文件不存在，请先安装浏览器或设置 CLOAKBROWSER_BINARY_PATH）"
            traceback.print_exc()
    raise RuntimeError("浏览器启动失败 —— " + " | ".join(errors))
