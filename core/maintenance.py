"""磁盘维护 —— 浏览器资料目录的体积盘点与安全清理（纯 stdlib，可离线单测）。

## 为什么需要它

扫码登录与「读取好友列表」都用**持久化**的 Chromium 资料目录
（`logs/login-profile/<抖音号>`，见 `webui.app.login_profile_dir`），
而 Chrome 会把网页资源写进 `Default/Cache`。线上实测：一个只登录过两天的
账号已经攒到 **451 MB**（另一个账号 152 MB），并且**没有任何上限** ——
Chrome 默认按"可用磁盘的百分比"来定缓存上限，机器越大长得越狠。

定时器每 5 分钟敲一次门、每轮都可能建浏览器，机器自己不会回收，最后只有
两种结局：要么磁盘满（发送直接失败，火花断掉），要么人工 SSH 上去 rm。
2026-10-14 盘点时 `logs/` 已经 1.1 GB，其中两个目录属于**早已不在 .env 里**
的账号，白占 396 MB。

本模块只做三件事：

* ``profile_report()``  —— 盘一下：每个资料目录多大、其中多少是可再生的缓存、
  哪些目录已经不属于任何在册账号（孤儿）、哪些目录正被浏览器占着；
* ``prune_orphans()``   —— 删孤儿目录（默认只预演，``apply=True`` 才真删）；
* ``prune_caches()``    —— 只删**可再生**的缓存子目录/文件（绝不动
  ``Cookies`` / ``Local Storage`` / ``IndexedDB`` / ``Login Data`` 这些登录态的载体）。

## 三条安全线

1. **只在自己家里动手**：所有目标先 ``realpath``，再用 ``os.path.commonpath``
   确认它确实落在资料根目录之内 —— 里面放一个指向外部的符号链接也不会误删。
2. **在册账号永不删**：孤儿判定以 ``TASKS``（``utils.config.get_userData()``）
   为准，并且要求目录**闲置**超过 ``grace_days`` 天（默认 7 天）。防的是
   "用户刚在面板上登录了一个还没写进 TASKS 的账号，目录先出现" —— 那种目录
   很新，会被宽限期挡住。
3. **读不到名单就什么都不删**：``TASKS`` 写坏时 ``get_userData()`` 会返回空名单，
   而空名单会让**每个**目录都算孤儿。所以这里把"读不到"当错误上报，
   清理函数见到错误直接空手而归（宁可不清，也不能把两个真账号的登录态删了）。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: 可以随时删掉、下次启动会自己长回来的目录。判断只看**目录名**（basename），
#: 因为同样的东西在两个层级都有：资料根下的 ``ShaderCache`` / ``segmentation_platform``，
#: 以及 ``Default/`` 里的 ``Cache`` / ``Code Cache`` / ``GPUCache``。
CACHE_DIR_NAMES = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "component_crx_cache",
    "extensions_crx_cache",
    "BrowserMetrics",
    "Crashpad",
    "segmentation_platform",
    "optimization_guide_hint_cache_store",
)

#: 同样可再生的缓存**文件**（Chrome 退出前的指标缓存，几 MB 一个）。
CACHE_FILE_PREFIXES = ("BrowserMetrics",)

#: 登录态的载体 —— 只用于把"哪些不能删"写进代码里可读，清理逻辑靠**白名单式**
#: 的 ``CACHE_DIR_NAMES`` 实现：不在名单里的一律不碰，所以这个常量加不加都安全。
KEEP_NAMES = (
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data For Account",
    "Local State",
    "Preferences",
    "Secure Preferences",
    "Network",
    "Local Storage",
    "Session Storage",
    "IndexedDB",
    "Service Worker",
    "Web Data",
    "Extension State",
)

#: 登录**之前**的临时资料目录名（``login_profile_dir("")`` 的产物）。
#: 它不是任何账号的家，但也不能当孤儿删掉：正在扫码的那次会话可能正用它。
SCRATCH_PROFILE_NAME = "default"

#: 孤儿目录的宽限期（天）。见模块 docstring 安全线 2。
ORPHAN_GRACE_DAYS = 7

#: Chromium 在运行时会在这个资料目录里放一把单例锁。
SINGLETON_LOCK = "SingletonLock"

#: 自动清理的默认间隔（小时）：一周一次。见 `maybe_auto_prune`。
DEFAULT_AUTO_PRUNE_HOURS = 168.0

#: 自动清理间隔的上限（小时）= 一年。写更大等价于关闭，这里主动夹住。
MAX_AUTO_PRUNE_HOURS = 8760.0

#: 自动清理失败后的重试间隔（秒）：名单读不到之类的问题不要每 5 分钟刷一遍。
MIN_AUTO_PRUNE_RETRY_SECONDS = 3600.0

#: 自动清理的状态文件名（放 ``logs/`` 下，与其它状态文件一致）。
STATE_FILE_NAME = "maintenance_state.json"


# ---- 基础工具 --------------------------------------------------------------


def human_size(size: Any) -> str:
    """把字节数说成人话；非数字/负数一律当 0（只用于展示）。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        value = 0.0
    if value < 0:
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def dir_size(path: Any) -> int:
    """递归统计体积（字节）。**不跟随符号链接**，读不到的条目直接跳过。

    不跟随链接有两个理由：一是 ``Default/Cache`` 里偶尔有指向外部的链接，
    跟进去会虚高；二是万一有人把资料目录做成指向 ``/`` 的链接，这一层就是
    最后一道闸。
    """
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def profile_root() -> Path:
    """资料根目录。与 ``webui.app.login_profile_dir`` 的口径保持一致。

    ★ 相对路径按**仓库根**解析（不是 CWD）：定时器与面板都从仓库目录启动，
    但手工 `python /opt/.../main.py prune` 时 CWD 可能是别处 —— 按 CWD 解析会
    盘出一个空目录，报"没有账号"，让人以为资料没了。
    """
    env = os.getenv("LOGIN_PROFILE_DIR")
    if env:
        return Path(env)
    try:
        from utils.config import resolve_path  # 函数内 import：避免 import 期耦合

        return Path(resolve_path(os.path.join("logs", "login-profile")))
    except Exception:  # noqa: BLE001 —— 纯 stdlib 兜底，路径问题不能变成异常
        return Path(__file__).resolve().parents[1] / "logs" / "login-profile"


def known_ids() -> Tuple[List[str], str]:
    """在册账号的 ``unique_id`` 列表；第二个返回值是错误说明（空串＝读到了）。

    ★ 取不到名单时**必须**让调用方看见错误：``get_userData()`` 在 ``TASKS``
    写坏、或账号缺 ``COOKIES_*`` 时都会静默少人（甚至全空），而空名单会让
    每个资料目录都变成"孤儿"。
    """
    try:
        from utils.config import get_userData  # 函数内 import：import 期不产生副作用

        users = get_userData() or []
    except Exception as exc:  # noqa: BLE001 —— 读配置怎么炸都不能让清理去猜
        return [], f"读不到账号名单（{type(exc).__name__}: {exc}）"
    ids: List[str] = []
    for user in users:
        uid = str((user or {}).get("unique_id") or "").strip()
        if uid and uid not in ids:
            ids.append(uid)
    if not ids:
        return [], "账号名单是空的（TASKS 没配 / 解析失败 / 都缺 COOKIES_*）"
    return ids, ""


def _within(root: Path, target: Path) -> bool:
    """目标（按 realpath 算）是否确实在 root 之内。"""
    try:
        root_real = os.path.realpath(str(root))
        target_real = os.path.realpath(str(target))
    except OSError:
        return False
    if target_real == root_real:
        return False  # 根目录自己永远不是"要删的东西"
    try:
        return os.path.commonpath([root_real, target_real]) == root_real
    except ValueError:  # 不同盘符（Windows）
        return False


def _lock_pid(path: Path):
    """从 `SingletonLock` 里读出 Chromium 的 pid（读不到返回 None）。

    Chromium 把这个文件做成指向 ``<主机名>-<pid>`` 的**符号链接**，所以 pid 是
    链接目标的最后一段。
    """
    try:
        target = os.readlink(os.path.join(str(path), SINGLETON_LOCK))
    except OSError:
        return None
    tail = str(target).rsplit("-", 1)[-1]
    try:
        return int(tail)
    except (TypeError, ValueError):
        return None


def profile_in_use(path) -> bool:
    """这个资料目录现在**真的**被浏览器占着吗？

    ★ 光看 `SingletonLock` 在不在会永久误判（2026-10-17 审计）：Chromium 被
    `kill -9`、OOM 或断电时锁文件留在原地、pid 早就没了 —— 那之后这个目录再也
    删不掉（`prune_orphans` 永远跳过它），面板的 `_reap_stuck_sessions` 也会在
    浏览器真活着的时候放行、让下一次登录去抢同一个 user-data-dir。所以锁里的
    pid 不在本机 / 已经退出时，一律当**过期锁**。
    """
    if not os.path.lexists(os.path.join(str(path), SINGLETON_LOCK)):
        return False
    pid = _lock_pid(path)
    if pid is None:
        return True          # 读不出 pid（Windows 的普通文件锁等）：保守当成在用
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False         # 进程没了 → 锁是陈旧的
    except PermissionError:
        return True          # 别的用户的进程：保守当成在用
    except OSError:
        return True
    return True


def _in_use(path: Path) -> bool:
    """这个资料目录现在是不是被浏览器占着（有单例锁）？"""
    return profile_in_use(path)


def _idle_days(path: Path, *, now_ts: Optional[float] = None) -> float:
    """最后一次改动到现在多少天（取 mtime；读不到算 0 = 当作很新）。"""
    try:
        mtime = os.path.getmtime(str(path))
    except OSError:
        return 0.0
    seconds = (now_ts if now_ts is not None else time.time()) - mtime
    return max(0.0, seconds / 86400.0)


# ---- 缓存盘点 --------------------------------------------------------------


def _iter_cache_targets(profile: Path) -> Iterable[Tuple[str, Path]]:
    """列出这个资料目录里所有"可再生"的东西：``("dir"/"file", 路径)``。

    只看目录名/文件前缀，**不递归进已命中的缓存目录**（省一次全盘遍历）。
    """
    stack = [profile]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(str(current)))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                name = entry.name
                if entry.is_dir(follow_symlinks=False):
                    if name in CACHE_DIR_NAMES:
                        yield "dir", Path(entry.path)
                    else:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if any(name.startswith(prefix) for prefix in CACHE_FILE_PREFIXES):
                        yield "file", Path(entry.path)
            except OSError:
                continue


def cache_size(profile: Any) -> int:
    """这个资料目录里可再生的缓存占了多大。"""
    total = 0
    for kind, target in _iter_cache_targets(Path(profile)):
        total += dir_size(target) if kind == "dir" else _file_size(target)
    return total


def _file_size(path: Path) -> int:
    try:
        return int(os.path.getsize(str(path)))
    except OSError:
        return 0


# ---- 盘点 ------------------------------------------------------------------


def profile_report(
    root: Any = None,
    ids: Optional[Sequence[str]] = None,
    *,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """盘一下资料根目录。返回的东西**必须**是 JSON 可序列化的（面板要直接透出）。"""
    base = Path(root) if root is not None else profile_root()
    error = ""
    if ids is None:
        id_list, error = known_ids()
    else:
        id_list = [str(i).strip() for i in ids if str(i).strip()]
    known = set(id_list)

    profiles: List[Dict[str, Any]] = []
    total = cache_total = orphan_total = 0
    exists = base.is_dir()
    if exists:
        try:
            children = sorted(
                (p for p in base.iterdir() if p.is_dir() and not p.is_symlink()),
                key=lambda p: p.name,
            )
        except OSError as exc:
            children, error = [], error or f"读不到资料目录：{exc}"
        for child in children:
            size = dir_size(child)
            cached = cache_size(child)
            idle = _idle_days(child, now_ts=now_ts)
            is_orphan = child.name not in known
            profiles.append(
                {
                    "name": child.name,
                    "bytes": size,
                    "cache_bytes": cached,
                    "human_bytes": human_size(size),
                    "human_cache_bytes": human_size(cached),
                    "idle_days": round(idle, 1),
                    "is_orphan": is_orphan,
                    "in_use": _in_use(child),
                    "is_scratch": child.name == SCRATCH_PROFILE_NAME,
                }
            )
            total += size
            cache_total += cached
            if is_orphan:
                orphan_total += size
    profiles.sort(key=lambda row: row["bytes"], reverse=True)
    return {
        "root": str(base),
        "exists": exists,
        "known_ids": id_list,
        "error": error,
        "profiles": profiles,
        "total_bytes": total,
        "cache_bytes": cache_total,
        "orphan_bytes": orphan_total,
        "human_total": human_size(total),
        "human_cache": human_size(cache_total),
        "human_orphan": human_size(orphan_total),
    }


# ---- 清理（默认只预演） ----------------------------------------------------


def prune_caches(
    root: Any = None,
    ids: Optional[Sequence[str]] = None,
    *,
    apply: bool = False,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """只删可再生缓存。默认 ``apply=False``：只报"能回收多少"，不真删。"""
    base = Path(root) if root is not None else profile_root()
    if ids is None:
        id_list, error = known_ids()
    else:
        id_list, error = [str(i).strip() for i in ids if str(i).strip()], ""
    if error:
        return {"ok": False, "error": error, "applied": False, "removed": [],
                "freed_bytes": 0, "human_freed": human_size(0), "skipped_in_use": []}

    removed: List[Dict[str, Any]] = []
    skipped: List[str] = []
    freed = 0
    if base.is_dir():
        names = set(id_list) | {SCRATCH_PROFILE_NAME}
        for child in sorted(base.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.is_symlink() or child.name not in names:
                continue
            if _in_use(child):
                skipped.append(child.name)
                continue
            for kind, target in list(_iter_cache_targets(child)):
                if not _within(base, target):
                    continue
                size = dir_size(target) if kind == "dir" else _file_size(target)
                if apply:
                    try:
                        if kind == "dir":
                            shutil.rmtree(str(target), ignore_errors=False)
                        else:
                            os.remove(str(target))
                    except OSError:
                        continue
                removed.append({"profile": child.name, "path": str(target),
                                "bytes": size, "human_bytes": human_size(size)})
                freed += size
    return {
        "ok": True,
        "error": "",
        "applied": bool(apply),
        "removed": removed,
        "freed_bytes": freed,
        "human_freed": human_size(freed),
        "skipped_in_use": skipped,
        "root": str(base),
    }


def prune_orphans(
    root: Any = None,
    ids: Optional[Sequence[str]] = None,
    *,
    apply: bool = False,
    grace_days: float = ORPHAN_GRACE_DAYS,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """删掉"不在册 + 闲置够久 + 没在跑"的资料目录。默认只预演。"""
    base = Path(root) if root is not None else profile_root()
    if ids is None:
        id_list, error = known_ids()
    else:
        id_list, error = [str(i).strip() for i in ids if str(i).strip()], ""
    if error:
        # ★ 名单不可信时**一个都不删**：空名单会让所有目录都算孤儿。
        return {"ok": False, "error": error, "applied": False, "removed": [],
                "freed_bytes": 0, "human_freed": human_size(0),
                "kept": [], "skipped_recent": [], "skipped_in_use": [],
                "root": str(base)}

    known = set(id_list)
    kept: List[str] = []
    skipped_recent: List[str] = []
    skipped_in_use: List[str] = []
    skipped_symlink: List[str] = []
    removed: List[Dict[str, Any]] = []
    freed = 0

    if base.is_dir():
        try:
            children = sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
        except OSError as exc:
            return {"ok": False, "error": f"读不到资料目录：{exc}", "applied": False,
                    "removed": [], "freed_bytes": 0, "human_freed": human_size(0),
                    "kept": [], "skipped_recent": [], "skipped_in_use": [],
                    "skipped_symlink": [], "root": str(base)}
        for child in children:
            name = child.name
            if child.is_symlink():
                # 符号链接/挂载点不跟、不删：它指到哪儿我们不知道，
                # `shutil.rmtree` 对目录符号链接会直接报错（不是删链接而是删目标）。
                skipped_symlink.append(name)
                continue
            if name in known or name == SCRATCH_PROFILE_NAME:
                kept.append(name)
                continue
            if not _within(base, child):
                # 名字不认识、真实路径又跑到根目录外（挂载点）——不碰。
                skipped_in_use.append(name)
                continue
            if _in_use(child):
                skipped_in_use.append(name)
                continue
            if _idle_days(child, now_ts=now_ts) < max(0.0, float(grace_days)):
                skipped_recent.append(name)
                continue
            size = dir_size(child)
            if apply:
                try:
                    shutil.rmtree(str(child), ignore_errors=False)
                except OSError as exc:
                    skipped_in_use.append(name)
                    del exc
                    continue
            removed.append({"name": name, "path": str(child),
                            "bytes": size, "human_bytes": human_size(size)})
            freed += size
    return {
        "ok": True,
        "error": "",
        "applied": bool(apply),
        "removed": removed,
        "freed_bytes": freed,
        "human_freed": human_size(freed),
        "kept": kept,
        "skipped_recent": skipped_recent,
        "skipped_in_use": skipped_in_use,
        "skipped_symlink": skipped_symlink,
        "grace_days": float(grace_days),
        "root": str(base),
    }


# ---- 人话总结（CLI 用） ----------------------------------------------------


def report_lines(report: Dict[str, Any]) -> List[str]:
    """把 ``profile_report()`` 的结果说成几行人话。"""
    lines = [f"资料目录：{report.get('root')}"]
    if report.get("error"):
        lines.append(f"⚠️ {report['error']}")
    if not report.get("exists"):
        lines.append("（目录还不存在：没登录过任何账号）")
        return lines
    lines.append(
        f"合计 {report.get('human_total')}"
        f"（其中可再生缓存 {report.get('human_cache')}，"
        f"孤儿目录 {report.get('human_orphan')}）"
    )
    for row in report.get("profiles") or []:
        tags = []
        if not row.get("is_orphan"):
            tags.append("在册")
        else:
            tags.append("孤儿" if not row.get("is_scratch") else "临时")
        if row.get("in_use"):
            tags.append("正在用")
        lines.append(
            f"  · {row.get('name'):<20} {row.get('human_bytes'):>9}"
            f"  缓存 {row.get('human_cache_bytes'):>9}"
            f"  闲置 {row.get('idle_days')} 天  [{'/'.join(tags)}]"
        )
    return lines


def prune_lines(result: Dict[str, Any], *, what: str) -> List[str]:
    """把清理结果说成几行人话。"""
    if not result.get("ok"):
        return [f"⛔ 没动任何文件：{result.get('error')}"]
    verb = "已清理" if result.get("applied") else "可清理（预演，未动手）"
    lines = [f"{verb}{what}：{len(result.get('removed') or [])} 项，"
             f"可回收 {result.get('human_freed')}"]
    for row in result.get("removed") or []:
        label = row.get("name") or f"{row.get('profile')}/{os.path.basename(str(row.get('path')))}"
        lines.append(f"  · {label}  {row.get('human_bytes')}")
    if result.get("skipped_recent"):
        lines.append("  跳过（还在宽限期内）：" + "、".join(result["skipped_recent"]))
    if result.get("skipped_in_use"):
        lines.append("  跳过（被占用/不在资料目录内）：" + "、".join(result["skipped_in_use"]))
    return lines


# ---- 定时器顺带维护（自动清理） --------------------------------------------
#
# 为什么要有它：缓存是**只涨不落**的（实测约 180 MB/天/账号），而 `prune` 命令
# 与面板按钮都要人记得去点。2026-10-17 生产盘点：`/` 已用 95G/116G（82%），
# `logs/login-profile` 1.1 GB，其中 394 MB 属于早已删掉的账号。磁盘满 = 发送
# 直接失败 = 火花断掉，所以把"到点自己清一次"做进定时器轮次里。


def repo_root() -> Path:
    """仓库根目录（``logs/`` 在它下面）。取不到就按本文件位置回推。"""
    try:
        from utils.config import REPO_ROOT  # 函数内 import：避免 import 期耦合

        return Path(REPO_ROOT)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[1]


def state_path() -> Path:
    """自动清理的状态文件（只记"上次什么时候清过、清了多少"）。"""
    env = os.getenv("MAINTENANCE_STATE_FILE")
    if env:
        return Path(env)
    return repo_root() / "logs" / STATE_FILE_NAME


def load_state(path: Any = None) -> Dict[str, Any]:
    """读状态。坏 JSON / 读不到一律当"没清过"（下次照做，不做任何假设）。"""
    target = Path(path) if path is not None else state_path()
    try:
        with open(str(target), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:  # noqa: BLE001 —— 状态文件坏掉不是错误，只当没记过
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Dict[str, Any], path: Any = None) -> bool:
    """原子写状态（临时文件 + ``os.replace``）。失败返回 False，绝不抛。"""
    target = Path(path) if path is not None else state_path()
    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(str(tmp), "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(str(tmp), str(target))
        return True
    except Exception:  # noqa: BLE001 —— 状态写不进去顶多下次多清一遍
        try:
            os.remove(str(tmp))
        except OSError:
            pass
        return False


def auto_prune_enabled() -> bool:
    """定时器要不要顺带做磁盘维护（等价于"间隔 > 0"，见 `auto_prune_hours`）。

    默认**开**：缓存是纯派生数据，删了下次启动自己长回来；磁盘满却是硬故障
    （发送直接失败）。要关掉就把 ``MAINTENANCE_AUTO_PRUNE_HOURS`` 写成 ``0``。
    """
    return auto_prune_hours() > 0.0


def auto_prune_hours() -> float:
    """两次自动清理至少隔多少小时。

    默认 168（一周）；``0`` = 关闭；非法值回落默认；负数/超一年按边界夹取
    （上限 8760 小时 = 一年，再大等价于关闭，但不至于让人以为写错了）。
    """
    raw = str(os.getenv("MAINTENANCE_AUTO_PRUNE_HOURS", "") or "").strip()
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_AUTO_PRUNE_HOURS
    if not math.isfinite(hours) or hours < 0:
        return DEFAULT_AUTO_PRUNE_HOURS
    if hours == 0:
        return 0.0
    return max(1.0, min(hours, MAX_AUTO_PRUNE_HOURS))


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def maybe_auto_prune(
    *,
    root: Any = None,
    ids: Optional[Sequence[str]] = None,
    now_ts: Optional[float] = None,
    state_file: Any = None,
    force: bool = False,
) -> Dict[str, Any]:
    """到点就顺手清一次（定时器每轮调一次，绝大多数调用都是空转）。

    只做两件事：删**在册账号**的可再生缓存、删**过宽限期**的孤儿资料目录
    （``prune_caches`` / ``prune_orphans`` 的三条安全线照旧生效）。

    返回 ``{"ran", "reason", "freed_bytes", "human_freed", "caches", "orphans"}``，
    ``reason`` ∈ ``ok`` / ``error`` / ``disabled`` / ``not-due`` / ``retry-wait``。

    ★ **永远不抛异常**：维护是"顺手做的事"，它失败绝不能让发送轮次挂掉。
    ★ 名单读不到时**两个清理都不调用**：``prune_orphans(ids=[])`` 会把空名单
      当成"一个在册账号都没有"，那就等于把每个目录都算孤儿 —— 这条必须在这里
      就掐掉，不能指望下游。这种情况**不推进** ``last_auto_prune``，但至少隔
      ``MIN_AUTO_PRUNE_RETRY_SECONDS`` 才再试一次，免得每 5 分钟刷一行错误。
    """
    now = float(now_ts if now_ts is not None else time.time())
    idle = {"ran": False, "freed_bytes": 0, "human_freed": human_size(0),
            "caches": {}, "orphans": {}}
    #: 出错的两次返回都用 `ran=True`：调用方（`main.py`）靠它决定要不要把
    #: 错误打进日志 —— 静默失败正是这套东西最不该有的行为。
    failed = dict(idle, ran=True, reason="error")
    if not force and not auto_prune_enabled():
        return dict(idle, reason="disabled")

    state = load_state(state_file)
    interval = auto_prune_hours() * 3600.0
    last_ok = _as_float(state.get("last_auto_prune"))
    last_try = _as_float(state.get("last_attempt"))
    if not force:
        if last_ok and (now - last_ok) < interval:
            return dict(idle, reason="not-due",
                        next_in_seconds=max(0.0, interval - (now - last_ok)))
        if last_try and (now - last_try) < MIN_AUTO_PRUNE_RETRY_SECONDS:
            return dict(idle, reason="retry-wait",
                        next_in_seconds=max(0.0, MIN_AUTO_PRUNE_RETRY_SECONDS - (now - last_try)))

    if ids is None:
        id_list, roster_error = known_ids()
    else:
        id_list, roster_error = [str(i).strip() for i in ids if str(i).strip()], ""
    if roster_error:
        state.update({"last_attempt": now, "last_error": roster_error})
        save_state(state, state_file)
        return dict(failed, error=roster_error)

    try:
        caches = prune_caches(root, id_list, apply=True, now_ts=now)
        orphans = prune_orphans(root, id_list, apply=True, now_ts=now)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        message = f"{type(exc).__name__}: {exc}"
        state.update({"last_attempt": now, "last_error": message})
        save_state(state, state_file)
        return dict(failed, error=message)

    ok = bool(caches.get("ok")) and bool(orphans.get("ok"))
    freed = int(caches.get("freed_bytes") or 0) + int(orphans.get("freed_bytes") or 0)
    error = "" if ok else (caches.get("error") or orphans.get("error") or "")
    state.update({
        "last_attempt": now,
        "last_freed_bytes": freed,
        "last_caches_bytes": int(caches.get("freed_bytes") or 0),
        "last_orphans_bytes": int(orphans.get("freed_bytes") or 0),
        "last_error": error,
        "runs": int(_as_float(state.get("runs"))) + 1,
    })
    if ok:
        # 只有真清成功才推进时间戳；失败走 retry-wait 那条闸。
        state["last_auto_prune"] = now
    save_state(state, state_file)
    return {"ran": True, "reason": "ok" if ok else "error", "error": error,
            "freed_bytes": freed, "human_freed": human_size(freed),
            "caches": caches, "orphans": orphans, "at": now}


def auto_prune_status(*, now_ts: Optional[float] = None, state_file: Any = None) -> Dict[str, Any]:
    """给面板/CLI 看的一行状态（JSON 可序列化）。"""
    now = float(now_ts if now_ts is not None else time.time())
    state = load_state(state_file)
    hours = auto_prune_hours()
    last = _as_float(state.get("last_auto_prune"))
    freed = int(_as_float(state.get("last_freed_bytes")))
    if not auto_prune_enabled():
        next_text = "已关闭（MAINTENANCE_AUTO_PRUNE_HOURS=0）"
    elif not last:
        next_text = "下一次门铃就会清"
    else:
        remain = max(0.0, hours * 3600.0 - (now - last))
        next_text = "下一次门铃就会清" if remain <= 0 else f"约 {human_duration(remain)} 后"
    return {
        "enabled": auto_prune_enabled(),
        "hours": hours,
        "last_at": last,
        "last_ago_text": human_duration(now - last) + "前" if last else "还没清过",
        "last_freed_bytes": freed,
        "last_freed_text": human_size(freed),
        "last_error": str(state.get("last_error") or ""),
        "next_text": next_text,
    }


def human_duration(seconds: Any) -> str:
    """把秒数说成人话（`3 天` / `5 小时` / `12 分钟`）。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "0 分钟"
    if not math.isfinite(value) or value <= 0:
        return "0 分钟"
    minutes = value / 60.0
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{int(round(minutes))} 分钟"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(round(hours))} 小时"
    return f"{int(round(hours / 24.0))} 天"
