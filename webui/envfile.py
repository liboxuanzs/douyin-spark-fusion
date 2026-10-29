"""面板侧写 `.env`：就地更新，只碰自己管的键。

和 `configTool/env_store.py` 一个口径：用 python-dotenv 的 `set_key` 就地改，
**你手写的注释、空行、其它变量的顺序原样保留**（它只重写自己那一行）。
面板能写进来的是这五样，每样都有自己的入口与校验：

    COOKIES_<抖音号>    扫码登录取回来的 Cookie（JSON 数组，单行）
    TASKS               账号清单（username / unique_id / fingerprint / targets）
    运行参数            白名单见 `webui/settings.py`（`/ops/config/save`）
    WEBUI_PASSWORD*     面板自己的访问口令（`/ops/password`，见 `set_webui_password`）

写之前一定先备份：`<env>.bak-<时间戳>`，只保留最近 `BACKUP_KEEP` 份。
`sanitize` 之类的读取端加工不在这里做 —— 那是 `utils/config.py` 的事。
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import random
import re
import shutil
import threading
import time
from functools import wraps
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

try:                                                     # pragma: no cover - 平台分支
    import fcntl
except ImportError:                                      # Windows 没有 fcntl
    fcntl = None

from core import cookie_utils
from core import proxy as proxy_mod
from core import schedule as schedule_mod
from webui.password import hash_password, validate_new_password

# 保留多少份备份（多了占地方，少了不够翻账）
BACKUP_KEEP = 5

# 写 .env 必须串行：每个写操作都是"读全文件 → 改一处 → 整份写回"，
# 两个线程同时做就会互相覆盖（真会发生的组合：勾选保存的目标好友，
# 同时后台扫码登录成功在写 cookie）。同一进程内用一把可重入锁，
# 覆盖整个「读-改-写」，不是只锁最后那次写盘。
_WRITE_LOCK = threading.RLock()

#: 跨**进程**的锁文件后缀：`<env>.lock`。为什么锁一个单独的文件而不是 `.env` 自己 ——
#: dotenv 的 `set_key` 最后一步是 `os.replace(tmp, <env>)`，inode 换了；锁旧文件
#: 等于没锁（下一个人打开的是新 inode）。锁文件不会被动过，才是一根稳定的桩。
#:
#: 生产上真的会同时写同一份 .env 的进程有两个：web 面板（扫码/改配置/改口令）和
#: 定时任务（P2 的 cookie 回写，见 core/cookie_sync.py）。只在进程内加锁挡不住它俩，
#: 交错的后果是**一方整份写回时把另一方刚改的那一行盖掉**（丢的是 TASKS / COOKIES，
#: 都是命根子）。configTool 是第三个写者，但它通常跑在操作者自己的机器上，不共享
#: 这个锁文件；真跑在同一台机器上时仍只剩很小的窗口，见 docs/deploy.md。
ENV_LOCK_SUFFIX = ".lock"

#: 已经拿到的文件锁按线程记深度：`_serialized` 之间会互相调用（update_settings →
#: backup），而 `flock` 不是可重入的 —— 同一进程再开一个 fd 去锁同一个文件会**死锁**。
_tls = threading.local()


def _env_lock_target(func, args, kwargs) -> Path:
    """这个调用要改哪份 .env（用来选锁文件）。"""
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        explicit = bound.arguments.get("path")
    except (TypeError, ValueError):                       # pragma: no cover - 签名读不到
        explicit = kwargs.get("path")
    return env_path(explicit)


def _acquire_env_lock(target: Path):
    """拿 `<env>.lock` 的排它锁（同线程可重入）。拿不到返回 None —— 不挡写盘。

    锁文件建不出来（只读目录、权限不对）时**退化回旧行为**：宁可保留一个极小的
    覆盖窗口，也不能让"设置了口令却保存不了"这种事发生。
    """
    if fcntl is None:
        return None
    real = Path(os.path.realpath(str(target)))
    location = real.with_name(real.name + ENV_LOCK_SUFFIX)
    key = str(location)
    held = getattr(_tls, "held", None)
    if held is None:
        held = _tls.held = {}
    entry = held.get(key)
    if entry is not None:
        entry[1] += 1
        return key
    try:
        fd = os.open(str(location), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        _warn(f"建不出 .env 的文件锁 {location}（{type(exc).__name__}: {exc}），本次写入只做进程内串行")
        return None
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)          # 锁文件里没有秘密，但没理由让它更宽
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:                # pragma: no cover - 文件系统不支持 flock
        _warn(f"拿不到 .env 的文件锁 {location}（{type(exc).__name__}: {exc}），本次只做进程内串行")
        os.close(fd)
        return None
    held[key] = [fd, 1]
    return key


def _release_env_lock(key) -> None:
    if not key:
        return
    held = getattr(_tls, "held", {})
    entry = held.get(key)
    if entry is None:                                     # pragma: no cover - 不该发生
        return
    entry[1] -= 1
    if entry[1] > 0:
        return
    held.pop(key, None)
    try:
        fcntl.flock(entry[0], fcntl.LOCK_UN)
    except OSError:                                       # pragma: no cover
        pass
    try:
        os.close(entry[0])
    except OSError:                                       # pragma: no cover
        pass


def _serialized(func):
    """把整个「读-改-写」放进同一把锁里。

    两层：进程内是 `_WRITE_LOCK`（可重入，允许函数之间互相调用），跨进程是
    `<env>.lock` 上的 `flock`。**先拿进程内锁再拿文件锁**，顺序只有这一种，
    不会互等。
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        with _WRITE_LOCK:
            key = _acquire_env_lock(_env_lock_target(func, args, kwargs))
            try:
                return func(*args, **kwargs)
            finally:
                _release_env_lock(key)

    return wrapper

# 指纹种子的取值范围，与 cloakbrowser 的 --fingerprint= 一致
FINGERPRINT_MIN = 10000
FINGERPRINT_MAX = 99999


def env_path(path=None) -> Path:
    """`.env` 的位置：显式路径 > `ENV_FILE` > 项目根的 `.env`。"""
    if path:
        return Path(path)
    return Path(os.getenv("ENV_FILE") or ".env")


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def read_keys(path=None) -> list:
    target = env_path(path)
    if not target.exists():
        return []
    try:
        return [key for key, value in dotenv_values(target).items() if value is not None]
    except Exception:                                            # noqa: BLE001
        return []


def read_map(path=None) -> dict:
    """读整份 `.env` 的**原文**（键 → 字符串值，注释与空行不进来）。

    与 ``store.config_snapshot()`` 的区别：那是"加工过的展示值"
    （``browserActionTimeout`` 会变成 "120000 毫秒"），这里拿到的是可以直接
    回填进表单、也可以原样写回去的文件内容 —— 面板改配置必须以原文为准，
    否则"打开表单看到的"和"文件里真的躺着的"会是两回事。
    """
    target = env_path(path)
    if not target.exists():
        return {}
    try:
        values = dotenv_values(target)
    except Exception:                                            # noqa: BLE001
        return {}
    return {str(key): str(value) for key, value in values.items() if value is not None}


def read_tasks(path=None) -> list:
    """读 TASKS。坏 JSON 直接抛 ValueError —— 面板要把它显示出来，不能默默当空。"""
    target = env_path(path)
    if not target.exists():
        return []
    raw = dotenv_values(target).get("TASKS") or ""
    text = str(raw).strip()
    if not text:
        return []
    try:
        tasks = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"TASKS 不是合法 JSON（{exc.msg}，第 {exc.lineno} 行第 {exc.colno} 列）") from exc
    if not isinstance(tasks, list):
        raise ValueError("TASKS 必须是 JSON 数组")
    return [task for task in tasks if isinstance(task, dict)]


def task_for(unique_id, path=None):
    wanted = str(unique_id or "").strip()
    for task in read_tasks(path):
        if str(task.get("unique_id") or "").strip() == wanted:
            return task
    return None


def cookie_key(unique_id) -> str:
    return f"COOKIES_{str(unique_id or '').strip()}".upper()


def cookie_configured(unique_id, path=None) -> bool:
    return cookie_key(unique_id) in read_keys(path)


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
@_serialized
def backup(path=None) -> str:
    """把当前 `.env` 复制一份带时间戳的备份，返回备份路径（文件不存在时返回空串）。"""
    target = env_path(path)
    if not target.exists():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = target.with_name(f"{target.name}.bak-{stamp}")
    index = 2
    while dest.exists():                                         # 同一秒内连写两次也不覆盖
        dest = target.with_name(f"{target.name}.bak-{stamp}-{index}")
        index += 1
    shutil.copy2(target, dest)
    # copy2 会把**源文件**的 mtime 一起复制过来，而 `_prune_backups` 按 mtime 排序
    # 只留最新的几份 —— 于是刚生成的这份备份看起来最旧，会被立刻删掉（留下来的反而
    # 是更早的备份）。落盘后把 mtime 拨到现在，别让备份把最新备份挤掉。
    try:
        os.utime(dest, None)
    except OSError:
        pass
    _prune_backups(target)
    return str(dest)


def _prune_backups(target: Path) -> None:
    try:
        olds = sorted(
            target.parent.glob(f"{target.name}.bak-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in olds[BACKUP_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def _lock_down(target: Path) -> None:
    """POSIX 上把权限收回 600 —— 这个文件里有 Cookie，等于账号密码。"""
    if os.name == "nt":
        return
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


#: `.env.tmp*` 残留多久算"上一个进程死在这儿了"（秒）。
#:
#: python-dotenv 落盘是「写 `<env>.tmp_<随机>` → chmod 成原权限 → os.replace」，
#: 那个中间态只活几毫秒；5 分钟这个门槛足够把"正在写的进程"与"上次被杀的残留"
#: 分开。**门槛不能短**：面板和 configTool 可能同时改这份 `.env`，删掉别人刚
#: 建出来的中间态会让对方那一次保存直接失败 —— 那比慢一点清理糟得多。
ENV_TMP_STALE_SECONDS = 300.0

#: `follow_symlinks` 是 python-dotenv 后加的形参（本项目 requirements 钉的是
#: 1.2.2，但部署机上那份可能是旧的），所以按**真实签名**判断，不写死。
_FOLLOW_SYMLINKS = "follow_symlinks"


class EnvWriteRefused(RuntimeError):
    """这次写盘会破坏用户的 `.env`（例如把软链接换成普通文件）→ 直接拒绝。"""


def _warn(message: str) -> None:
    """记一条中文 warning（走项目自己的日志器；记日志本身出问题也绝不让写盘失败）。"""
    try:
        from utils.logger import setup_logger

        setup_logger(level=os.getenv("LOG_LEVEL", "Info")).warning(
            f"[webui/envfile] {message}"
        )
    except Exception:                                            # noqa: BLE001
        try:
            logging.getLogger(__name__).warning("%s", message)
        except Exception:                                        # noqa: BLE001
            pass


def _is_symlink(target: Path) -> bool:
    try:
        return target.is_symlink()
    except OSError:                                              # pragma: no cover
        return False


def _env_dirs(target: Path) -> list:
    """`.env` 自己和（若是软链）真身所在的目录。

    dotenv 的中间态建在 `<env>` 旁边；跟着软链写时它也可能建在真身旁边，
    两边都扫一遍才不会漏。
    """
    out = [target.parent]
    if _is_symlink(target):
        real = Path(os.path.realpath(str(target))).parent
        if real != target.parent:
            out.append(real)
    return out


def _looks_like_env_temp(path: Path) -> bool:
    """`.tmp_*` 这个前缀太泛，删之前先确认"它真是一份 .env 的中间态"。

    python-dotenv 1.2.x 的 `rewrite()` 是
    `NamedTemporaryFile(prefix=".tmp_", dir=<.env 所在目录>)`，所以真名是
    `.tmp_xxxxxxxx`（**不是** `.env.tmp_*`）—— 仓库根目录下任何别的工具留下的
    `.tmp_*` 都可能撞上这个前缀，删错文件的代价比漏删大，所以再加两道门槛：
    内容是文本、且带 `KEY=` 形状的行（`.env` 全是这个形状）。
    """
    try:
        if path.stat().st_size > 4 * 1024 * 1024:    # 一份 .env 不该这么大
            return False
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    if b"\x00" in head:                              # 二进制文件，不是文本配置
        return False
    for line in head.splitlines():
        text = line.strip()
        if not text or text.startswith(b"#"):
            continue
        if b"=" in text and text.split(b"=", 1)[0].strip():
            return True
    return False


def _sweep_env_tmp(target: Path) -> list:
    """清掉 `<env>.tmp*` / `.tmp_*` 残留 —— 那些文件里是**完整的 .env**（含 Cookie 与口令哈希）。

    为什么需要这一步：python-dotenv 的写盘流程是「写临时文件 → `chmod(tmp, 原权限)`
    → `os.replace(tmp, <env>)`」。在 chmod 与 replace 之间被 kill（OOM、断电、
    容器被停）就会留下一个权限可能是 0644、内容却是整份 `.env` 的文件躺在仓库根
    目录里 —— 那是所有账号的 Cookie 加上面板口令。面板每保存一次配置都会走那条路，
    所以每次写盘前后各扫一次。

    ⚠️ 临时文件的**真名**是 `.tmp_xxxxxxxx`（`NamedTemporaryFile(prefix=".tmp_",
    dir=<.env 目录>)`，实测 python-dotenv 1.2.4；仓库 requirements 钉的是 1.2.2）。
    从前只匹配 `<env名>.tmp*`，等于一个真中间态都清不掉 —— 两个前缀都认。
    `.tmp_*` 太泛，所以那一路还要过 `_looks_like_env_temp()`（内容得像 .env）。

    **只删够老的**（ENV_TMP_STALE_SECONDS）：刚出现的那个正是本次写盘（或别的
    进程的写盘）的中间态，删了会打断写入。删除前先把权限收紧成 0600 ——
    这样"删失败"也至少不再是 0644。返回被删掉的文件名列表。
    """
    prefixes = (target.name + ".tmp", ".tmp_")
    now = time.time()
    removed: list = []
    for folder in _env_dirs(target):
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for item in entries:
            if not item.name.startswith(prefixes):
                continue
            try:
                if item.is_dir():                # 只可能是普通文件；目录不动
                    continue
                age = now - item.stat().st_mtime
            except OSError:                      # 刚好被别人删了/改不动，跳过
                continue
            if age < ENV_TMP_STALE_SECONDS:
                continue
            if not item.name.startswith(target.name + ".tmp") and not _looks_like_env_temp(item):
                continue                         # 泛前缀 + 内容不像 .env → 不是我们的中间态
            try:
                if os.name != "nt":
                    os.chmod(item, 0o600)        # 先收紧；万一 unlink 失败也不再是 0644
                item.unlink()
            except OSError as exc:
                _warn(f"清理残留中间文件 {item} 失败：{type(exc).__name__}: {exc}")
                continue
            removed.append(str(item))
            _warn(
                f"清掉了一个残留的中间文件 {item}（{int(age)} 秒前留下的）："
                f"它里面有 Cookie / 口令哈希，是 python-dotenv 写盘被中断的痕迹；"
                f"已先 chmod 600 再删除"
            )
    return removed


def _dotenv_supports_follow_symlinks() -> bool:
    """本机这份 python-dotenv 的 `set_key` / `unset_key` 认不认 `follow_symlinks`？"""
    for func in (set_key, unset_key):
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):          # 读不到签名（C 实现/装饰器）就按不支持
            return False
        if _FOLLOW_SYMLINKS not in params:
            return False
    return True


def symlink_write_problem(target: Path) -> str:
    """`.env` 是软链、而本机 dotenv 改不动软链时，返回中文原因（否则空串）。

    为什么要专门拦这一下：python-dotenv 默认 `follow_symlinks=False`，它最后
    那步是 `os.replace(tmp, <env>)` —— 对软链来说这一步会把**软链本身换成普通
    文件**。用户把 `.env` 软链到 `/etc/...`、或用 stash 这类工具管着配置时，
    真正的配置在别处：面板"保存成功"了，被读取的还是老文件（于是"改了配置不
    生效"），软链指向的真身被悄悄留在原地。要么带上 `follow_symlinks` 去改
    真身，要么明确拒绝 —— 绝不静默换掉用户的软链。
    """
    if not _is_symlink(target):
        return ""
    if _dotenv_supports_follow_symlinks():
        return ""
    try:
        real = os.path.realpath(str(target))
    except OSError:                                              # pragma: no cover
        real = "?"
    return (
        f"{target} 是一个软链接（指向 {real}），而本机安装的 python-dotenv "
        f"不认识 follow_symlinks 参数：按默认写法保存会把软链接替换成普通文件，"
        f"真正被读取的那份配置其实没改（面板上看起来「保存了却不生效」）。"
        f"请把 python-dotenv 升级到支持 follow_symlinks 的版本"
        f"（requirements.txt 里钉的是 1.2.2），或者直接把配置写进 {real}"
    )


def _write_kwargs(target: Path) -> dict:
    """软链要跟着真身写时给 dotenv 的额外参数；本机不支持就抛 EnvWriteRefused。"""
    problem = symlink_write_problem(target)
    if problem:
        raise EnvWriteRefused(problem)
    return {_FOLLOW_SYMLINKS: True} if _is_symlink(target) else {}


def _set_key(target: Path, key: str, value: str) -> None:
    # quote_mode="never"：与 .env.example 一致不加引号；加了引号反而会让
    # JSON 值里的转义在别处被二次解释（Docker env_file / unicode_escape 都会踩）。
    kwargs = _write_kwargs(target)
    _sweep_env_tmp(target)          # 上一次被杀的进程可能在这里留了一整份 .env
    try:
        set_key(str(target), key, value, quote_mode="never", encoding="utf-8", **kwargs)
    finally:
        # 兜底：本次写盘半路出事时留下的中间态，够老的话这里顺手清掉。
        # 够新的一律不碰 —— 那可能是另一个进程（configTool）正在写的中间态。
        _sweep_env_tmp(target)


def _unset_key(target: Path, key: str) -> None:
    """删掉 `.env` 里的一行（`remove_account` 删 Cookie 用）；软链同样跟着真身写。"""
    kwargs = _write_kwargs(target)
    _sweep_env_tmp(target)
    try:
        unset_key(str(target), key, quote_mode="never", encoding="utf-8", **kwargs)
    finally:
        _sweep_env_tmp(target)


# ---------------------------------------------------------------------------
# 写：运行配置（webui/settings.py 的白名单）
# ---------------------------------------------------------------------------
@_serialized
def update_settings(values, *, path=None) -> dict:
    """把面板改的运行配置写进 `.env`。

    与另外三个写入口（cookie / 目标 / 移除账号）共用同一把写锁与同一套备份，
    所以并发保存不会互相覆盖。**只动传进来的键**：其它行、注释、顺序原样保留。

    没变的键一个字节都不写 —— 否则用户点一次"保存"，`mtime` 与备份都会动，
    看起来像改了什么，其实什么都没改。
    """
    target = env_path(path)
    if not target.exists():
        return {"ok": False, "error": f"找不到 {target}，无法写入", "changed": {}}
    # 软链 + 本机 dotenv 改不动软链：宁可不写，也不能把用户的软链换成普通文件。
    problem = symlink_write_problem(target)
    if problem:
        return {"ok": False, "error": problem, "changed": {}}

    wanted = {
        str(key): str(value if value is not None else "")
        for key, value in dict(values or {}).items()
        if str(key).strip()
    }
    if not wanted:
        return {"ok": False, "error": "没有要写的配置项", "changed": {}}

    current = read_map(target)
    changed = {key: value for key, value in wanted.items() if current.get(key, "") != value}
    if not changed:
        return {
            "ok": True,
            "changed": {},
            "kept": sorted(wanted),
            "path": str(target),
            "backup": "",
        }

    # 每个值都是**原样**落进 .env（`_set_key` 不加引号），所以先逐项确认 dotenv
    # 不会把它改坏：「空白 + #」会被当注释截断、「${」会被当变量插值。
    # 任何一项有问题就整批不写 —— 与调用方 `settings.normalize` 的
    # "不留半截状态"保持同一套语感。
    for key, value in changed.items():
        reason = dotenv_hazard(value)
        if reason:
            return {"ok": False, "error": f"{key} 的值{reason}", "changed": {}}

    saved = backup(target)
    for key, value in changed.items():
        _set_key(target, key, value)
    _lock_down(target)
    return {
        "ok": True,
        "changed": changed,
        "kept": sorted(key for key in wanted if key not in changed),
        "path": str(target),
        "backup": saved,
    }


# ---------------------------------------------------------------------------
# 写：面板访问口令（校验在 webui/password.py，入口是 webui/app.py 的 /ops/password）
# ---------------------------------------------------------------------------
#: 口令存成明文（`.env` 里 `WEBUI_PASSWORD=...`，能直接看到）
PASSWORD_MODE_PLAIN = "plain"
#: 口令存成 pbkdf2 哈希（`.env` 里 `WEBUI_PASSWORD_HASH=...`，不可逆）
PASSWORD_MODE_HASH = "hash"


def _set_key_quoted(target: Path, key: str, value: str) -> None:
    """写**带引号**的值（只给口令用）。

    其它键一律 `quote_mode="never"`（见 `_set_key`）：它们的值是 JSON / 数字 /
    枚举，加引号反而会被别处二次解释。口令不一样 —— 它可能带 `#`、空格、`$`，
    不加引号会被 dotenv 当成注释开头或变量展开截断，于是"设了口令却登不上"。
    """
    kwargs = _write_kwargs(target)
    _sweep_env_tmp(target)
    try:
        set_key(str(target), key, value, quote_mode="always", encoding="utf-8", **kwargs)
    finally:
        _sweep_env_tmp(target)


def password_mode(*, path=None) -> str:
    """当前口令在 `.env` 里是什么形式：``"plain"`` / ``"hash"`` / ``""``。"""
    values = read_map(path)
    if str(values.get("WEBUI_PASSWORD_HASH") or "").strip():
        return PASSWORD_MODE_HASH
    if str(values.get("WEBUI_PASSWORD") or "").strip():
        return PASSWORD_MODE_PLAIN
    return ""


@_serialized
def set_webui_password(password_value, *, store=PASSWORD_MODE_PLAIN, path=None) -> dict:
    """把面板里设的访问口令写进 `.env`（返回给面板显示的结果字典）。

    ``store="plain"`` 写 ``WEBUI_PASSWORD``（明文），``"hash"`` 写
    ``WEBUI_PASSWORD_HASH``（pbkdf2）。**两个键永远一起写**：只写一个的话，
    另一种形式的旧值还留在文件里，而登录校验是"哈希优先"—— 用户会撞上
    "明明改过口令，新口令却说不对"。

    返回的 ``changed`` 是**键名列表**，不是键值对：这个结果会被拼进 303 重定向
    的查询串（flash），口令绝不能出现在 URL 里。
    """
    target = env_path(path)
    mode = (
        PASSWORD_MODE_HASH
        if str(store).strip().lower() == PASSWORD_MODE_HASH
        else PASSWORD_MODE_PLAIN
    )
    problem = validate_new_password(password_value)
    if problem:
        return {"ok": False, "error": problem, "changed": [], "mode": mode}
    if not target.exists():
        return {"ok": False, "error": f"找不到 {target}，无法写入", "changed": [], "mode": mode}
    problem = symlink_write_problem(target)
    if problem:
        return {"ok": False, "error": problem, "changed": [], "mode": mode}

    value = str(password_value)
    wanted = {
        "WEBUI_PASSWORD": "" if mode == PASSWORD_MODE_HASH else value,
        "WEBUI_PASSWORD_HASH": hash_password(value) if mode == PASSWORD_MODE_HASH else "",
    }
    current = read_map(target)
    # 两个键都要在文件里留一行（不用的那个写成空值）。这不是留白：
    # `load_dotenv(override=True)` 会把环境里可能残留的旧值一起清成空串 ——
    # 否则"改成哈希了，但进程环境里还留着上次的明文"，`password_plain()` 照样能读到它。
    changed = [key for key, val in wanted.items() if current.get(key, "") != val or key not in current]
    if not changed:
        return {
            "ok": True,
            "changed": [],
            "mode": mode,
            "path": str(target),
            "backup": "",
        }

    saved = backup(target)
    for key in changed:
        _set_key_quoted(target, key, wanted[key])
    _lock_down(target)
    return {
        "ok": True,
        "changed": changed,
        "mode": mode,
        "path": str(target),
        "backup": saved,
    }


# ---------------------------------------------------------------------------
# 写：Cookie
# ---------------------------------------------------------------------------
def _escape_for_dotenv(text: str) -> str:
    r"""把值里的每个反斜杠再翻一倍 —— 抵消读取端那一次 `unicode_escape`。

    `utils/config.py` 读 `COOKIES_*` 时会做
    `os.getenv(key).encode("utf-8").decode("unicode_escape")`（上游遗留，
    见 `core/cookie_utils.cookies_to_json` 的说明）。那一步只对**反斜杠序列**
    有意见，而且两种都是静默事故：

    * cookie 值里有反斜杠（`a\b`）：存进 .env 的 JSON 是 `a\\b`，读回来
      被吃成 `a\b`，`json.loads` 再把它当转义符 → 值变成 `a` + 退格符，
      **登录态静默失效**；
    * cookie 值里有双引号（`a"b`）：存进去是 `a\"b`，读回来只剩 `a"b`，
      JSON 直接解析失败 → 这个账号被「格式不正确，已跳过」**静默跳过、一条都不发**。

    翻倍之后，读取端那一步正好还原成原样。**没有反斜杠的值文本零变化**，
    所以现存 .env 不需要迁移。
    """
    return str(text or "").replace("\\", "\\\\")


@_serialized
def save_cookie(unique_id, cookies_json, *, path=None, expect_login=True) -> dict:
    """把扫码得到的 Cookie 写进 `COOKIES_<抖音号>`。返回给面板显示的结果字典。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号，无法决定写进哪个变量名"}
    try:
        cookie_utils.env_key_for(uid)                            # 只做合法性校验
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    text = str(cookies_json or "").strip()
    cookies, error = cookie_utils.parse_cookies(text)
    if error:
        return {"ok": False, "error": f"Cookie 不是合法 JSON：{error}"}
    if expect_login and not cookie_utils.has_login_cookie(cookies):
        return {"ok": False, "error": "这份 Cookie 里没有 sessionid，写进去也发不出消息"}
    if expect_login and not cookie_utils.has_usable_login_cookie(cookies):
        # 名字对、但 expires 已经写在过去：存下去只会让面板显示"已配置"，发送时
        # 拿到 EXPIRED。宁可现在就说清楚，也别让用户以为保存成功了。
        return {"ok": False,
                "error": "这份 Cookie 的 sessionid 已经过期了（expires 是过去时间），"
                         "写进去也发不出消息；请重新扫码登录拿一份新的"}
    reason = dotenv_hazard(text)
    if reason:
        # 值本身解得出 JSON，但原样写进 .env 之后会被 dotenv 改坏 ——
        # 那时候读端只会说「格式不正确，已跳过」，用户完全看不出发生了什么。
        return {"ok": False, "error": f"这份 Cookie 里{reason}，写进去读不回来"}

    target = env_path(path)
    if not target.exists():
        return {"ok": False, "error": f"找不到 {target}，无法写入"}
    problem = symlink_write_problem(target)
    if problem:
        return {"ok": False, "error": problem}
    saved = backup(target)

    key = cookie_utils.env_key_for(uid)
    # 落盘前转义，别直接写 `text`：读取端会 unicode_escape 一次（见上面那个函数）
    _set_key(target, key, _escape_for_dotenv(text))
    _lock_down(target)
    return {
        "ok": True,
        "key": key,
        "path": str(target),
        "backup": saved,
        "cookies": len(cookies),
        "names": cookie_utils.cookie_names(cookies),
    }


# ---------------------------------------------------------------------------
# 写：账号清单
# ---------------------------------------------------------------------------
def _fingerprint_from(value) -> int:
    """把调用方给的指纹种子变成能写进 TASKS 的整数。

    只接受范围内（FINGERPRINT_MIN..FINGERPRINT_MAX）的整数或数字字符串；
    给了但读不出来（写错、超范围）就当作没给，随机补一个 —— 宁可换一个随机种子，
    也不要往文件里写一个 cloakbrowser 不认的值。
    """
    text = str(value or "").strip()
    if text:
        try:
            number = int(text)
        except (TypeError, ValueError):
            number = 0
        if FINGERPRINT_MIN <= number <= FINGERPRINT_MAX:
            return number
    return random.randint(FINGERPRINT_MIN, FINGERPRINT_MAX)


@_serialized
def upsert_account(unique_id, *, username="", fingerprint=None, targets=None,
                   message=None, send_time=None, enabled=None, proxy=None,
                   path=None, add_if_missing=True) -> dict:
    """把账号加进 TASKS（已在则就地更新），返回结果字典。

    只动给出的字段：`username` 为空就保留原值，`targets` 为 None 就保留原列表，
    `message` / `send_time` / `enabled` 为 None 就保留原值。
    fingerprint 只在账号**还没有**指纹时生效（读不出来就随机补一个）；已有指纹
    一律保留 —— 它是"这个账号固定用哪台设备"的锚，必须写进文件、以后每次都读
    同一份，不能让浏览器每次自己随机，也不能被顺手传进来的值悄悄换掉。

    参数名是 `send_time` 而不是 `time`：本模块头部 `import time` 拿了标准库的
    `time`，同名形参会把它遮住，以后再想用 `time.time()` 就是一个很难查的
    `AttributeError`（或更糟：静默拿到别的东西）。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号，无法写进 TASKS"}

    target = env_path(path)
    if not target.exists():
        return {"ok": False, "error": f"找不到 {target}，无法写入"}
    problem = symlink_write_problem(target)
    if problem:
        return {"ok": False, "error": problem}

    try:
        tasks = read_tasks(target)
    except ValueError as exc:
        return {"ok": False, "error": f"{exc}（先在 configTool 里修好 TASKS 再回面板扫码）"}

    entry = None
    for task in tasks:
        if str(task.get("unique_id") or "").strip() == uid:
            entry = task
            break

    created = False
    if entry is None:
        if not add_if_missing:
            return {"ok": True, "added": False, "changed": False, "path": str(target),
                    "message": f"{uid} 不在 TASKS 里，按你的要求没有添加"}
        entry = {"username": str(username or uid), "unique_id": uid, "targets": []}
        tasks.append(entry)
        created = True

    changed = created
    if username and str(entry.get("username") or "") != str(username):
        entry["username"] = str(username)
        changed = True
    if targets is not None:
        wanted = clean_targets(targets)
        if list(entry.get("targets") or []) != wanted:
            entry["targets"] = wanted
            changed = True
    if message is not None:
        wanted_message = clean_message(message)
        current_message = clean_message(entry.get("message"))
        if wanted_message:
            if current_message != wanted_message:
                entry["message"] = wanted_message
                changed = True
        elif "message" in entry:
            # 写空 = 恢复用全局模板。**把键删掉**而不是留一个 ""：
            # 空串在下游也能工作（build_message 把空串当"没配"），但删掉更干净，
            # 也让"这个账号到底有没有单独配过"在 .env 里一眼可辨。
            entry.pop("message", None)
            changed = True
    if send_time is not None:
        wanted_time = clean_time(send_time)
        current_time = clean_time(entry.get("time"))
        if wanted_time:
            if current_time != wanted_time:
                entry["time"] = wanted_time
                changed = True
        elif "time" in entry:
            # 写空 = 恢复跟 `.env` 的 TASK_SCHEDULE_TIMES。和 message 一样**删键**，
            # 而不是留一个 "" —— "这个账号到底有没有单独配过时间"要一眼可辨。
            entry.pop("time", None)
            changed = True
    if enabled is not None:
        # 停用写成 `"enabled": false`；启用则把这个键**删掉**（缺键 = 启用）。
        # 和 message/time 同一语感：写空 = 回默认，键留着只会让"这个账号到底停过
        # 没有"变得更难一眼看出。别的形态（字符串 "false"、"0"）一律重写成
        # 布尔 false，读取端的 task_enabled 认它，面板也不至于显示错。
        if enabled:
            if "enabled" in entry:
                entry.pop("enabled", None)
                changed = True
        elif entry.get("enabled") is not False:
            entry["enabled"] = False
            changed = True
    if proxy is not None:
        # 每账号出口（issue #18）：写空 = 回落全局 PROXY_ADDRESS，所以**删键**，
        # 与 message/time/enabled 同一语感。值只做归一（补协议），合法性由
        # set_account_proxy 在入口处挡；这里再存一份原文是为了让"账号自己配过"
        # 这件事在 .env 里一眼可辨。
        wanted_proxy = proxy_mod.normalize(proxy)
        if wanted_proxy:
            if str(entry.get("proxy") or "") != wanted_proxy:
                entry["proxy"] = wanted_proxy
                changed = True
        elif "proxy" in entry:
            entry.pop("proxy", None)
            changed = True
    # 指纹是"这个账号固定用哪台设备"的锚，一旦定下就不许漂。
    # 传进来的种子只在**账号还没有指纹**时生效；已有指纹时保留原值，
    # 免得面板/命令行顺手传一个值就把账号悄悄换成"另一台设备"。
    kept = ""
    current = str(entry.get("fingerprint") or "").strip()
    if not current:
        entry["fingerprint"] = _fingerprint_from(fingerprint)
        changed = True
    elif str(fingerprint or "").strip() and str(fingerprint).strip() != current:
        kept = current

    if not changed:
        result = {"ok": True, "added": False, "changed": False, "path": str(target),
                  "username": entry.get("username"),
                  "targets": list(entry.get("targets") or []),
                  "fingerprint": entry.get("fingerprint"),
                  "time": str(entry.get("time") or ""),
                  "enabled": schedule_mod.user_enabled(entry),
                  "proxy": proxy_mod.normalize(entry.get("proxy")),
                  "message": "TASKS 里已经是这个账号，没有改动"}
        if kept:
            result["fingerprint_kept"] = kept
            result["message"] += f"（指纹保持 {kept}）"
        return result

    # 最后一道闸：不管上面哪条路径改的，序列化出来的那一行只要会被 dotenv
    # 弄坏（注释截断 / 变量插值）就绝不落盘。宁可这次保存失败并说清原因，
    # 也不能把 `.env` 写坏 —— 写坏的后果是账号库整个读不出来，面板自己也修不回来。
    payload = json.dumps(tasks, ensure_ascii=False, separators=(",", ":"))
    reason = dotenv_hazard(payload)
    if reason:
        return {
            "ok": False,
            "error": f"写进去会让 TASKS 那一行被 .env 弄坏：{reason}；"
                     f"请改掉这个账号的昵称、目标好友名或发送内容再保存",
        }

    saved = backup(target)
    _set_key(target, "TASKS", payload)
    _lock_down(target)
    result = {
        "ok": True,
        "added": created,
        "changed": True,
        "path": str(target),
        "backup": saved,
        "username": entry.get("username"),
        "targets": list(entry.get("targets") or []),
        "fingerprint": entry.get("fingerprint"),
        "time": str(entry.get("time") or ""),
        "enabled": schedule_mod.user_enabled(entry),
        "proxy": proxy_mod.normalize(entry.get("proxy")),
        "message": "已加入 TASKS" if created else "已更新 TASKS",
    }
    if kept:
        result["fingerprint_kept"] = kept
        result["message"] += f"（指纹保持 {kept}）"
    return result


#: 单个账号自定义发送内容的长度上限（字符）。
#: 定这个数不是为了美观：`TASKS` 是 `.env` 里的**一行** JSON，值太长会把那一行
#: 撑成几万字符的巨行，`configTool` 和任何手改 `.env` 的编辑器都会很难受；
#: 超长文本抖音的聊天输入框本身也会截断。超限**拒绝写盘**而不是静默截断。
MESSAGE_MAX_CHARS = 2000


def clean_message(message) -> str:
    """账号自定义内容的归一：换行统一成 `\\n`，再去掉首尾空白。

    换行**照原样留着**（不转义、不丢弃）：写盘时 `json.dumps` 会把它转义成
    `\\n` 塞进 `.env` 那一行，读回来仍是真换行；下游
    `core/douyin_im.line_list` 同时认「字面 \\n」与「真换行」两种形态，
    所以面板里敲的回车不会白敲。

    空值（None / 空串 / 只有空白）统一表示"这个账号没单独配过"，
    由 `core/msg_builder.build_message` 回落到全局 `MESSAGE_TEMPLATE`。
    """
    text = "" if message is None else str(message)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


#: 撞上 `.env` 注释规则时的统一说法。
COMMENT_HAZARD_HINT = (
    "不能出现「空白 + #」—— .env 会把 # 后面整段当成注释，"
    "写进去的那一行会被从中间切断，账号库直接读不出来"
)


def comment_hazard(value) -> bool:
    """这段文本里有没有会被 dotenv 当成注释开头的「空白 + #」？

    python-dotenv 解析**无引号值**的最后一步是 `re.sub(r"\\s+#.*", "", part)`，
    也就是说「空白 + #」之后的内容会被整段丢掉。而 `TASKS` 正是一个不带引号的
    单行 JSON（见 `_set_key` 的 `quote_mode="never"`），所以只要目标名、账号名或
    账号专属发送内容里出现这种东西，那一行就会从中间断开：`json.loads` 从此失败，
    账号库整个读不出来 —— 面板自己也读不了，等于把用户锁在门外。

    `\\s` 在 Python 的 str 正则里是 Unicode 空白（含 tab、NBSP、全角空格），
    正好对上 dotenv 那侧的 `\\s`。
    """
    return bool(re.search(r"(?:^|\s)#", str(value or "")))


def dotenv_hazard(value) -> str:
    """值原样落进 .env 会不会被 dotenv 弄坏？返回人话原因，没问题返回空串。

    `_set_key` 用的是 `quote_mode="never"`（不加引号、原样落盘），dotenv 解析
    无引号值时有两道坑，而且都是**静默**改坏数据：

    * 「空白 + #」→ 之后整段被当行内注释删掉（正则见 `comment_hazard`）；
    * 「${」→ 变量插值，`${VAR}` 被换成环境变量的值（没定义就换成空串）。

    第二条对 `COOKIES_<抖音号>` 尤其致命：值被改写不会当场报错，而是等到读端
    `json.loads` 失败，那个账号被「格式不正确，已跳过」静静地丢掉 ——
    面板上看起来"登录成功了"，实际上一条都发不出去。
    """
    text = str(value or "")
    if comment_hazard(text):
        return COMMENT_HAZARD_HINT
    if "${" in text:
        return "不能出现「${」—— .env 会把它当变量插值，写进去的值会被悄悄改写"
    if text[:1] in ('"', "'"):
        # 无引号落盘 + 首字符是引号 = dotenv 会把它当成"带引号的值"重新解析：
        # 不成对时**整条键消失**（模板回落到内置默认值，面板却显示保存成功），
        # 成对时引号被悄悄吃掉。两条都是"面板说成功、实际配置变了"。
        return ("不能以引号开头 —— .env 会把无引号的值当成引号包裹重新解析，"
                "整条配置可能读不到")
    return ""


#: 时间格式的说明。`clean_time` 认不出来时，回给用户的就是这句话。
TIME_HINT = "请写成 24 小时制的 HH:MM，例如 08:00 或 20:30"


def clean_time(value) -> str:
    """账号发送时间的归一：`"8:00"` → `"08:00"`；空值/认不出来 → `""`。

    ★ 解析规则**只有一份**，就是 `core.schedule.parse_time`。这里不另写一套正则 ——
    否则迟早出现"面板存得进去、调度却读不出来"（用户会直接认为面板保存坏了）。

    返回 `""` 的含义是**恢复跟全局默认时间**：`upsert_account` 会把 `time`
    这个键整个删掉，和 `message` 写空时的处理保持一套语感。
    """
    if value is None:
        return ""
    return schedule_mod.parse_time(value) or ""


def clean_targets(targets) -> list:
    """目标列表归一：字符串原样留下，`{"name","uid","conv_id"}` 只保留这三个键。

    面板勾选好友列表写入的是**带身份**的目标（抖音自己算的 uid / conv_id），
    手写的老目标仍是字符串 —— 两种都留着，去重按「身份优先、名字兜底」：

    - 只有名字的 dict 写成**字符串**（别把老格式变成一坨对象）；
    - 同一个名字，如果列表里还有带身份的版本，就只留带身份的那条（同一个人别发两次）。
    """
    parsed = []
    for item in targets or []:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("display") or "").strip()
            uid = str(item.get("uid") or "").strip()
            conv_id = str(item.get("conv_id") or "").strip()
            if not (name or uid or conv_id):
                continue
            if uid or conv_id:
                entry = {"name": name or conv_id or uid}
                if uid:
                    entry["uid"] = uid
                if conv_id:
                    entry["conv_id"] = conv_id
                ident = ("conv:" + conv_id) if conv_id else ("uid:" + uid)
            else:
                entry, ident = name, ""
        else:
            entry = str(item if item is not None else "").strip()
            if not entry:
                continue
            name, ident = entry, ""
        name_key = ("name:" + name) if name else ""
        if not (ident or name_key):
            continue
        parsed.append((entry, ident, name_key))

    identified = {name_key for _entry, ident, name_key in parsed if ident}
    cleaned, seen = [], set()
    for entry, ident, name_key in parsed:
        if ident:
            key = ident
        else:
            if name_key in identified:
                continue            # 这个人已经有带身份的目标了
            key = name_key
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(entry)
    return cleaned


@_serialized
def set_account_message(unique_id, message, *, path=None) -> dict:
    """只改这个账号的发送内容（面板上账号卡里的「保存内容」）。

    写空 = 恢复用全局 `MESSAGE_TEMPLATE`（`upsert_account` 会把 `message`
    这个键整个删掉）。超长**拒绝写盘**并说清原因，不静默截断 —— 用户敲了
    两千字却只发出去一半，比直接报错难查得多。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号"}
    if task_for(uid, path) is None:
        return {"ok": False, "error": f"{uid} 还不在 TASKS 里，先完成一次扫码登录"}

    text = clean_message(message)
    if len(text) > MESSAGE_MAX_CHARS:
        return {
            "ok": False,
            "error": f"内容太长了（{len(text)} 字，上限 {MESSAGE_MAX_CHARS} 字）",
        }
    reason = dotenv_hazard(text)
    if reason:
        # 内容会原样进 `TASKS` 那一行 JSON，写进去就等于把账号库弄坏
        return {"ok": False, "error": f"发送内容{reason}"}

    result = upsert_account(uid, message=text, path=path)
    if result.get("ok"):
        result["message_set"] = text
    return result


@_serialized
def set_account_time(unique_id, time_text, *, path=None) -> dict:
    """只改这个账号的发送时间（面板账号卡里的「保存时间」）。

    写空 = 恢复跟 `.env` 的 `TASK_SCHEDULE_TIMES`（`upsert_account` 会把 `time`
    这个键整个删掉）。写了内容但解析不出来则**拒绝写盘**并说清正确格式 ——
    静默按"没配"处理的话，用户以为设成了 08:00，实际却跟着默认时间在 20:30 发，
    这种"面板显示成功、行为完全不同"的偏差最难查。

    返回里刻意用 `time_set` 而不是 `time`：`upsert_account` 的 `result["message"]`
    是人话提示（"已更新 TASKS"），`result["time"]` 是当前值，都不能被占用。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号"}
    if task_for(uid, path) is None:
        return {"ok": False, "error": f"{uid} 还不在 TASKS 里，先完成一次扫码登录"}

    raw = "" if time_text is None else str(time_text).strip()
    text = clean_time(raw)
    if raw and not text:
        return {"ok": False, "error": f"“{raw}” 不是时间：{TIME_HINT}"}

    result = upsert_account(uid, send_time=text, path=path)
    if result.get("ok"):
        result["time_set"] = text
    return result


@_serialized
def set_account_enabled(unique_id, enabled, *, path=None) -> dict:
    """停用 / 启用一个账号（面板账号卡上的「停用」「启用」）。

    ★ 停用只是**不自动发**：TASKS 里的昵称、目标好友、发送时间、指纹和
      `COOKIES_*` 全部留着，点「启用」就恢复原样 —— 这正是它和
      `remove_account` 的区别（那个连 cookie 一起删，想再发就得重新扫码）。

    返回里的 `enabled` 是**落盘后**的状态（`upsert_account` 同一口径），
    `message` 仍是人话提示。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号"}
    if task_for(uid, path) is None:
        return {"ok": False, "error": f"{uid} 还不在 TASKS 里，先完成一次扫码登录"}

    result = upsert_account(uid, enabled=bool(enabled), path=path)
    if result.get("ok"):
        result["enabled"] = bool(enabled)
        result["message"] = (
            "已启用：到点后照常自动发送（今天还没发过的话，下一次门铃就会补上）"
            if enabled else
            "已停用：不再自动发送；配置、cookie、好友都还留着，随时可以启用"
        )
    return result


def account_proxy(unique_id, path=None) -> str:
    """这个账号生效的出口原文（空串 = 回落全局 PROXY_ADDRESS）。

    只读、不写盘：扫码登录与好友扫描启动浏览器前都要问一句"这个号走哪个出口"，
    答错了会让账号在另一个 IP 上登录 —— 那正是掉登录最常见的起因。
    """
    task = task_for(unique_id, path)
    if not isinstance(task, dict):
        return ""
    return proxy_mod.normalize(task.get("proxy"))


@_serialized
def set_account_proxy(unique_id, proxy, *, path=None) -> dict:
    """只改这个账号的出口（issue #18）。

    写空 = 改回全局 `PROXY_ADDRESS`（键整个删掉）。写错协议/缺端口**拒绝落盘**并
    说清格式：出口写坏却当成直连，等于让这个号在另一个 IP 上发消息，事后极难发现。

    返回里只放遮掩后的形态（`http://***@host:port`），口令绝不回显。
    """
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号"}
    if task_for(uid, path) is None:
        return {"ok": False, "error": f"{uid} 还不在 TASKS 里，先完成一次扫码登录"}

    raw = "" if proxy is None else str(proxy).strip()
    reason = proxy_mod.problem(raw)
    if reason:
        return {"ok": False, "error": reason}

    result = upsert_account(uid, proxy=raw, path=path)
    if result.get("ok"):
        applied = proxy_mod.normalize(result.get("proxy"))
        result["proxy_set"] = applied
        result["proxy_text"] = proxy_mod.mask(applied) or "直连"
        result["message"] = (
            f"已单独配置出口：{result['proxy_text']}（扫码与发送都走它）"
            if applied else
            "已改回全局出口（PROXY_ADDRESS；那里也没配就是直连）"
        )
    return result


@_serialized
def set_targets(unique_id, targets, *, path=None) -> dict:
    """只改目标好友列表（面板上好友选择器的保存按钮）。"""
    uid = str(unique_id or "").strip()
    if not uid:
        return {"ok": False, "error": "没读到抖音号"}
    if task_for(uid, path) is None:
        return {"ok": False, "error": f"{uid} 还不在 TASKS 里，先完成一次扫码登录"}

    cleaned = clean_targets(targets)
    for item in cleaned:
        # 目标名同样是 `TASKS` 那一行里的一段字符串
        name = item.get("name") if isinstance(item, dict) else item
        reason = dotenv_hazard(name)
        if reason:
            return {"ok": False, "error": f"目标好友「{name}」的名字里{reason}，先取消勾选它"}
    return upsert_account(uid, targets=cleaned, path=path)


@_serialized
def remove_account(unique_id, *, keep_cookie=False, path=None) -> dict:
    """把账号连同它的 `COOKIES_*` 一起删掉（Cookie 可选保留）。"""
    uid = str(unique_id or "").strip()
    target = env_path(path)
    if not uid or not target.exists():
        return {"ok": False, "error": "没有这个账号"}
    problem = symlink_write_problem(target)
    if problem:
        return {"ok": False, "error": problem}
    try:
        tasks = read_tasks(target)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    left = [task for task in tasks if str(task.get("unique_id") or "").strip() != uid]
    if len(left) == len(tasks):
        # 常见于"只在某轮运行记录里出现过、TASKS 里其实没有"的账号：
        # 直接说清楚，别让用户以为是按钮坏了。
        return {
            "ok": False,
            "error": (
                f"TASKS 里没有 {uid}，没有什么可移除的"
                "（它可能只是上一轮运行记录里的账号，配置里早就没有了）"
            ),
        }

    saved = backup(target)
    _set_key(target, "TASKS", json.dumps(left, ensure_ascii=False, separators=(",", ":")))
    if not keep_cookie:
        try:
            _unset_key(target, cookie_utils.env_key_for(uid))
        except Exception:                                        # noqa: BLE001
            pass
    _lock_down(target)
    return {"ok": True, "path": str(target), "backup": saved,
            "message": f"已移除 {uid}" + ("（保留了 Cookie）" if keep_cookie else "（连同 Cookie 一起删除）")}


# ---------------------------------------------------------------------------
# 写完之后
# ---------------------------------------------------------------------------
@_serialized
def reload(path=None) -> bool:
    """让当前进程立刻看到新写的值（面板是常驻进程，不重读就还是旧的）。"""
    try:
        from utils.config import reload_env
    except Exception:                                            # noqa: BLE001
        return False
    try:
        return bool(reload_env(str(env_path(path))))
    except Exception:                                            # noqa: BLE001
        return False
