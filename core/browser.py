"""浏览器启动。首选 cloakbrowser（自带拟人化），失败时回落原生 Playwright。

融合版相比上游改了三处，都是被 本机 的实测逼出来的：

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
import re
import time
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
    # 不要 --disable-gpu：那会让指纹档案自报的 GPU 与 WebGL 全部落空（页面里
    # WebGL 直接报 "no context"），是这台设备最容易被分辨的硬特征之一。
    # 无 GPU 的机器上 Chrome 会自己退到软件渲染，--enable-unsafe-swiftshader 兜住它。
    "--enable-unsafe-swiftshader",
]

# Chromium 的磁盘缓存默认**没有上限**：实测每个账号每天长 ~180MB
# （资料目录里 Default/Cache 最大），跑一两个月就能把磁盘吃满 ——
# 磁盘满的直接后果是发送静默失败。这里给它一个上限，配合
# `core/maintenance.py` 的缓存清理（`main.py prune` / 面板）双保险。
DISK_CACHE_SIZE_MB = 256
BASE_CHROME_ARGS.append(f"--disk-cache-size={DISK_CACHE_SIZE_MB * 1024 * 1024}")

# 语言与时区必须和"出口 IP 所在的城市"对得上。
# 这台服务器的系统时区是 Asia/Shanghai，而实测出口是香港 HGC 住宅宽带
# （223.19.25.170）—— UA 自称 Windows、时区却和 IP 不在同一地，是风控里
# 最便宜的一条对照。默认值放在 .env（见 .env.example），代码只负责传下去：
#   BROWSER_TIMEZONE  空 = 用系统时区；写错只告警、退回系统时区，绝不拖垮发送
#   BROWSER_LOCALE    空 = zh-CN
DEFAULT_LOCALE = "zh-CN"
#: BCP 47 的宽松形状：`zh` / `zh-CN` / `zh-Hans-CN` 都收，`zh_CN`、带空格/斜杠的不收。
#: 不校验的话写错一个下划线就会静默落到默认值（Playwright 只认连字符）。
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def browser_locale():
    """本次启动用的语言（BCP 47）。空值回落到 zh-CN，写错也回落到 zh-CN（只告警）。"""
    raw = (os.environ.get("BROWSER_LOCALE") or "").strip()
    if not raw:
        return DEFAULT_LOCALE
    if not _LOCALE_RE.match(raw):
        logger.warning(f"[BROWSER] BROWSER_LOCALE={raw!r} 不是合法的 BCP 47 语言标记，"
                       f"本轮按 {DEFAULT_LOCALE} 走")
        return DEFAULT_LOCALE
    return raw


def browser_timezone():
    """本次启动用的 IANA 时区；没配或写错名字时返回 None（= 系统时区）。"""
    raw = (os.environ.get("BROWSER_TIMEZONE") or "").strip()
    if not raw:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(raw)
    except Exception as exc:                    # noqa: BLE001 —— 配置错不许拖垮整轮发送
        logger.warning(f"[BROWSER] BROWSER_TIMEZONE={raw!r} 不是合法时区"
                       f"（{type(exc).__name__}: {exc}），本轮按系统时区走")
        return None
    return raw


def launch_options():
    """cloakbrowser 启动参数里的语言/时区（它俩叫 locale/timezone）。"""
    opts = {"locale": browser_locale()}
    tz = browser_timezone()
    if tz:
        opts["timezone"] = tz
    return opts


def context_options():
    """browser.new_context() 的同义参数：Playwright 把它叫 timezone_id。"""
    opts = {"locale": browser_locale()}
    tz = browser_timezone()
    if tz:
        opts["timezone_id"] = tz
    return opts

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
    kw.update(launch_options())
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
    try:
        browser = pw.chromium.launch(**kw)
    except BaseException:
        # 启动失败也要把 driver 还回去：`sync_playwright().start()` 已经拉起了一个
        # node 子进程，只有 stop() 才会回收（成功路径交给 close_context 回收）。
        # 最常见的失败原因恰恰是"资料目录被另一只 Chromium 占着"，会连着重试很多次。
        _stop_silently(pw)
        raise
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
                kw.update(launch_options())
                if proxy:
                    kw["proxy"] = proxy
                return _launch(profile_dir, **kw)
            if name == "playwright":
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                kw = {"headless": headless, "args": args}
                # Playwright 的 launch_persistent_context 收 locale/timezone_id
                kw.update(context_options())
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
                try:
                    ctx = pw.chromium.launch_persistent_context(profile_dir, **kw)
                except BaseException:
                    # 同 `_launch_playwright`：driver 已经起来了，启动失败必须 stop()，
                    # 否则每失败一次就漏一个 node 子进程（"目录被占"正是最常见的失败）。
                    _stop_silently(pw)
                    raise
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


def _stop_silently(pw):
    """把**启动失败**那次 `sync_playwright().start()` 拉起的 driver 还回去。

    为什么单独一个函数：`close_context()` 是"关一只已经开起来的浏览器"，靠
    `_KEEPALIVE` 里的身份对上号才能回收句柄。而启动中途失败时句柄**根本还没登记**
    （登记发生在 launch 成功之后），所以必须在这里就地 stop()。漏掉的后果是每次
    失败漏一个 node 子进程 —— 而"资料目录被占"正是最常见的失败原因，会连着重试。
    """
    try:
        pw.stop()
    except Exception as exc:                             # noqa: BLE001
        logger.warning(f"[BROWSER] ⚠️ 回收启动失败的 playwright 句柄时出错："
                       f"{type(exc).__name__}: {exc}")


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
    matched = False
    for entry in list(_KEEPALIVE):
        pw, owner = entry
        if owner is not target:
            continue
        matched = True
        _KEEPALIVE.remove(entry)
        try:
            pw.stop()
        except Exception as exc:                         # noqa: BLE001
            logger.warning(f"[BROWSER] ⚠️ 回收 playwright 句柄失败：{type(exc).__name__}: {exc}")
    if not matched and _KEEPALIVE and _looks_like_playwright(target):
        # 登记表里还有别的句柄、关掉的偏偏是一个 playwright 对象 ⇒ 多半有人直接
        # `browser.close()` / `ctx.close()` 绕过了这里，那个 driver 就永远不回收了。
        # （cloakbrowser 后端本来就不登记句柄，`_KEEPALIVE` 为空时不会误报。）
        logger.warning("[BROWSER] ⚠️ close_context 收到一个没登记过句柄的对象"
                       "（可能是被 browser.close()/ctx.close() 直接关掉的）："
                       "playwright driver 可能没回收，请改用 close_context()")
    return ok


def _looks_like_playwright(target) -> bool:
    """是不是 playwright 自己的对象（而不是 cloakbrowser 之类的壳）。"""
    try:
        return type(target).__module__.startswith("playwright")
    except Exception:                                    # noqa: BLE001
        return False


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


def kill_profile_processes(profile_dir, *, grace: float = 3.0) -> int:
    """把还在用这个资料目录的 Chromium 进程结束掉（先 SIGTERM，宽限后 SIGKILL）。

    ★ 2026-10-17 审计：`page.evaluate` / `ctx.cookies()` 这类调用**没有超时**，
    浏览器一旦卡住，扫码登录/读好友列表的线程会永远停在那一行 —— `cancel()`
    只是置旗，卡住的调用根本收不到。面板为了不"永远忙"只能把会话记录摘掉，
    结果下一次登录又去抢同一个 user-data-dir（SingletonLock 冲突）。
    守夜线程需要一个**跨线程安全**的收尾手段：Playwright 的对象不能跨线程碰
    （`greenlet.error: Cannot switch to a different thread`），但"按命令行里的
    `--user-data-dir=` 找到进程再杀"可以。

    返回被信号命中的进程数（一个都没找到返回 0，不算失败）。非 Linux 直接返回 0。
    """
    target = os.path.realpath(str(profile_dir or ""))
    if not target or not os.path.isdir("/proc"):
        return 0
    me = os.getpid()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if not raw:
            continue
        for part in raw.split(b"\0"):
            if not part.startswith(b"--user-data-dir="):
                continue
            value = part[len(b"--user-data-dir="):].decode("utf-8", "replace").rstrip("/")
            if value and os.path.realpath(value) == target:
                pids.append(pid)
            break
    killed = 0
    for sig in (15, 9):                                # SIGTERM → 宽限 → SIGKILL
        alive = []
        for pid in pids:
            try:
                os.kill(pid, sig)
            except OSError:
                continue
            alive.append(pid)
        killed = max(killed, len(alive))
        if sig == 15 and alive:
            deadline = time.time() + max(0.0, float(grace or 0.0))
            while time.time() < deadline:
                if not any(_pid_alive(pid) for pid in alive):
                    break
                time.sleep(0.2)
        pids = [pid for pid in alive if _pid_alive(pid)]
        if not pids:
            break
    return killed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
