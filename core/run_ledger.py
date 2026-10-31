"""运行账本 + 运行锁 —— 纯 stdlib，可离线单测。

## 为什么单独一个模块

`core.tasks` 是模块级快照（import 即 `get_config()`、`core.browser` 又会
import cloakbrowser），所以**仪表盘不能 import 它**。这个模块反过来：
只依赖 stdlib，`webui` 可以放心 import 它来读账本，两边共用一套 schema，
不会出现"写的人改了字段、读的人还在按老字段解析"的漂移。

## 账本格式（一行一轮，追加写）

`logs/runs.jsonl`，每行一个 JSON：

    {"run_id": "20261001-093000", "started_at": "...", "finished_at": "...",
     "dry_run": "off|select|full", "rate_limited": false, "limit": null,
     "skipped": null, "accounts": [ ... ]}

* `skipped` 非空 = 整轮被冷却跳过：`{"reason": "cooldown", "until": "..."}`
* `limit` 非空    = 本轮命中熔断：`{"keyword", "category", "source", "target", ...}`

**追加写 + 坏行容忍**：账本是"事实记录"，宁可留一行废数据，也不要因为
一次写坏就把整段历史丢掉。
"""
import json
import os
import time
import uuid

from core.util_time import iso, make_run_id, norm_dry_run, now, parse_iso, resolve_dry_run  # noqa: F401

# 锁超过这么久就认为是**残留**（进程被 kill -9、机器断电都会留下锁文件）。
# 2 小时的上限比最长一轮运行还宽裕：宁可多等一会儿，也不要误判成"没人在跑"
# 然后两个进程同时开浏览器 —— 那才是真的会把账号打进去。
STALE_LOCK_SECONDS = 2 * 60 * 60

#: 锁文件**读不出来**（0 字节 / 半截 JSON）时，多久之后才敢当残留删掉。
#: acquire() 是「先建空文件、再写内容」，所以刚出现的空文件是正常的中间态；
#: 真正在写的进程几毫秒就写完了，30 秒足够区分「正在写」和「死在那里」。
CORRUPT_LOCK_GRACE_SECONDS = 30.0

#: 「持锁进程还活着」但锁已经这么老 → 按**卡死**处理（当作残留锁夺过来）。
#:
#: 正常一轮有明确上界：定时器轮由 systemd 的 `TimeoutStartSec=3600` 掐掉，
#: 面板轮就算多账号 × 多目标，也不过"目标数 ×（发送间隔 + 单步超时）"，
#: 一小时出头。远超过这个上界还活着，几乎只有两种可能：
#:   * webui 那条 daemon 线程卡在某个没有单步超时的调用上再也不动（面板没有
#:     "取消这一轮"的入口，锁于是永不释放）；
#:   * 锁里的 pid 被别的长命进程复用了（小机器上 Chromium 起落频繁）。
#: 两者的表现完全一样：此后每次敲门都被 RunLocked 拒掉，而 **timer 源被拒时
#: 连账本都不写**（见 core/tasks._skip），面板"最近运行"一片正常，实际整天
#: 一个好友都没发 —— 只能靠人工登机器 rm 锁。给一个远大于合法轮次的上限，
#: 让下一个门铃自愈。
STUCK_LOCK_SECONDS = 6 * 60 * 60


def ledger_path():
    """账本路径。相对路径按**仓库根**解析（与 .env.example 的口径一致）。

    按 CWD 解析的代价：`WorkingDirectory=`、`cd /app`、以及"在别处执行
    `python /opt/douyin-spark-fusion/main.py task`"三种起法会各自写一份账本，
    面板"最近运行"就会漏掉命令行跑的那几轮。
    """
    from utils.config import resolve_path  # 函数内 import：避免 import 期耦合

    return resolve_path(os.getenv("RUN_LEDGER_FILE", os.path.join("logs", "runs.jsonl")))


def lock_path():
    """运行锁路径。与 `ledger_path()` 同订：相对路径按仓库根解析。

    ★ 这条是**正确性**问题，不只是"文件散落"：锁落到别处等于没有锁，
    定时任务与手动触发可能同时开两个浏览器发同一个账号（重复发送）。
    """
    from utils.config import resolve_path

    return resolve_path(os.getenv("RUN_LOCK_FILE", os.path.join("logs", "run.lock")))


# ---- 触发来源 ---------------------------------------------------------------

#: 账本/锁里的 `source` 是谁把这一轮跑起来的：
#: `cli`   = 人敲的命令行（缺省；直接跑 main.py 就是它）
#: `timer` = systemd timer / cron —— 由 unit 里的 `Environment=RUN_SOURCE=timer` 注入
#: `webui` = 面板"立即运行"（webui 直接给 runTasks 传参，不走这个函数）
SOURCE_CLI = "cli"
SOURCE_TIMER = "timer"
SOURCE_WEBUI = "webui"


def run_source(default=SOURCE_CLI, env=None):
    """解析本轮触发来源（`RUN_SOURCE` 环境变量，认不出来就原样返回小写）。

    **不要在 `.env` 里设它**：`.env` 是三个入口共用的，一旦写死 `timer`，
    手动跑的和面板跑的都会被标成定时任务，面板上就再也分不清谁触发的了。
    它只该由 `deploy/douyin-spark-fusion-daily.service` 这一个 unit 注入。
    """
    raw = (env if env is not None else os.getenv("RUN_SOURCE", "")) or ""
    return raw.strip().lower() or default


def _ensure_dir(path):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)


# ---- 写 -------------------------------------------------------------------

def _tail_is_newline(f) -> bool:
    """文件当前最后一个字节是不是 `\\n`（空文件 / 读不到 = True）。"""
    try:
        if f.seek(0, os.SEEK_END) <= 0:
            return True
        f.seek(-1, os.SEEK_END)
        return f.read(1) == b"\n"
    except OSError:  # pragma: no cover - 读不动就当作"是"，别挡住追加
        return True


def append_run(record, path=None):
    """追加一轮记录。写失败**不抛**：账本写不进去不能反过来让整轮任务失败。"""
    path = path or ledger_path()
    try:
        _ensure_dir(path)
        with open(path, "a+b") as f:
            # 先确保**最后一行以 \n 结尾**再追加：上一轮如果被 SIGKILL / 断电
            # 砍在半行上，直接 append 会把两条记录拼成一行 —— 那一行 JSON 解析
            # 失败，于是**两条**历史一起变成坏行被 read_runs 丢掉（丢的不只是
            # 那半条）。先补一个换行，半条留在原地当坏行，新记录仍是好记录。
            if not _tail_is_newline(f):
                f.write(b"\n")
            # default=str：万一条目里混进 datetime/time 这类不可序列化的东西，
            # 降级成字符串写下去，而不是抛 TypeError 把整轮任务的收尾炸掉
            # （2026-10-01 真机验证时正撞上：禁跑窗口的记录里带了个 time 元组，
            #  任务已经跑完了，却因为写账本失败把记录整个丢掉）。
            f.write((json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            # flush + fsync：账本只在**每轮收尾**写一次，代价可以忽略；不落盘的话
            # 机器掉电/被硬关时这一轮的记录会随页缓存一起消失，面板的"最近运行"
            # 与限流冷却的追溯就断档了。fsync 失败不当作写失败（数据已进内核）。
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:  # pragma: no cover - 某些文件系统不支持 fsync
                pass
        return True
    except (OSError, ValueError, TypeError):
        # ValueError：路径里带 NUL 之类（Windows 上还会碰见非法字符）时
        # open() 抛的是 ValueError 而不是 OSError。契约是"账本写不进去
        # 不能让整轮任务失败"，所以三类都得吞。
        return False


# ---- 读 -------------------------------------------------------------------

def read_runs(limit=20, path=None):
    """读最近 `limit` 轮，**最新的在前**。坏行跳过，文件不存在返回 []。"""
    path = path or ledger_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue          # 坏行跳过，不影响其余历史
        if isinstance(rec, dict):
            out.append(rec)
        if len(out) >= max(1, int(limit)):
            break
    return out


def last_run(path=None):
    runs = read_runs(limit=1, path=path)
    return runs[0] if runs else None


# ---- 运行锁 ---------------------------------------------------------------

def _pid_alive_windows(pid):
    """Windows 上判进程存活 —— `os.kill(pid, 0)` 在这里不可用。

    实测（Python 3.12 / Windows）：`os.kill` 对自己的 pid 会抛 `OSError [WinError 87]`
    （参数错误），对已经死掉的 pid 抛 `[WinError 11]` —— 错误码与"进程是否存在"对不上，
    所以走 `OpenProcess` + `GetExitCodeProcess`（退出码 `STILL_ACTIVE`=259 表示还活着）。
    `ERROR_ACCESS_DENIED`（5）说明进程存在但不是我们的权限 → 保守当作活着。
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_ACCESS_DENIED:
            return True      # 进程存在，只是不给我们查询权限 → 保守当作活着
        return False         # ERROR_INVALID_PARAMETER(87) 等 → 没有这个 pid
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid):
    """→ True / False / None（判断不了）。

    POSIX 用 `os.kill(pid, 0)`；Windows 走 `_pid_alive_windows`（见它的 docstring，
    `os.kill` 在那边判不出来）。两边都**允许返回 None**，由调用方回落到"按时间判陈旧"。
    绝不把"判断不了"当成"没人在跑"。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在但不是我的 → 活着
    except (OSError, AttributeError, ValueError):
        return None


class RunLocked(RuntimeError):
    """已有运行在跑。`info` 是占用者的信息，供 webui 直接展示。"""

    def __init__(self, info):
        self.info = info or {}
        who = self.info.get("source") or "?"
        since = self.info.get("started_at") or "?"
        super().__init__(f"已有一轮任务在运行（来源={who} 开始于 {since}）")


def _lock_age(path) -> float:
    """锁文件有多大年纪（秒）。文件不在就返回 0。"""
    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return 0.0


def lock_info(now_dt=None, path=None):
    """读锁状态 → dict 或 None（没有锁）。`stale` 表示这是残留锁。"""
    path = path or lock_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    at = now_dt or now()
    started = parse_iso(rec.get("started_at"))
    age = (at - started).total_seconds() if started else None
    alive = _pid_alive(rec.get("pid"))
    reason = None
    if alive is False:
        stale, reason = True, "dead_pid"
    elif alive is True and age is not None and age > STUCK_LOCK_SECONDS:
        # 活着的进程**通常**不该被判残留（见下面那一支的注释），但"还活着"也有
        # 两种：真在跑，以及 pid 被复用 / 面板那条线程卡死了。超过 STUCK_LOCK_SECONDS
        # 就归到后者 —— 否则整套发送会静默停摆到有人登机器 rm 锁为止。
        stale, reason = True, "alive_too_long"
    elif alive is True:
        # 进程确实活着 —— 跑了多久都不是残留。**这一支必须排在按时间判断之前**：
        # 一场多账号 × 多目标、间隔 8~20 秒的真跑超过 2 小时很现实，
        # 若按时间判成残留，第二个进程会把锁删掉并夺锁 —— 同账号两个 Chromium
        # 同时登录会互踢，正是本项目最怕的事故。（上限见 STUCK_LOCK_SECONDS）
        stale, reason = False, "alive"
    elif age is None:
        stale, reason = False, "unknown_age"   # 时间读不出来 → 保守当作"还在跑"
    elif age > STUCK_LOCK_SECONDS:
        # 走到这里说明 pid **探不出来**（`_pid_alive` 回了 None：Windows 上 OpenProcess
        # 因权限以外的原因失败、或 os.kill 抛了预料之外的 OSError）。判不了就当还在跑 ——
        # 按 STALE_LOCK_SECONDS(2h) 判残留会删掉一个可能真在跑的锁，两个 Chromium 同时
        # 登录同一个账号会互踢，那是本项目最怕的事故。但也不能无限期占着：超过
        # STUCK_LOCK_SECONDS(6h) 就与"活着但卡死"同等处理，否则发送会静默停摆到
        # 有人登机器 rm 锁为止。
        stale, reason = True, "unknown_pid_too_long"
    else:
        stale, reason = False, "unknown_pid"
    out = dict(rec)
    out.update({"age_seconds": int(age) if age is not None else None,
                "pid_alive": alive, "stale": bool(stale), "stale_reason": reason})
    return out


def is_running(now_dt=None, path=None):
    info = lock_info(now_dt=now_dt, path=path)
    return bool(info and not info.get("stale"))


class RunLock:
    """跨进程互斥：`O_CREAT|O_EXCL` 建文件 + 内容里带 token。

    释放时**核对 token 才删**：否则 A 进程超时退出后会把 B 进程的锁删掉，
    两个浏览器就同时开起来了。
    """

    def __init__(self, source="cli", dry_run="off", path=None, run_id=None,
                 accounts=None):
        self.path = path or lock_path()
        self.source = source
        self.dry_run = dry_run
        self.run_id = run_id
        #: 这一轮要处理哪些账号。**只是给人看的元信息，不参与互斥** —— 互斥永远只靠
        #  O_EXCL + token；面板读它来判断"派发器正在动这个账号"，见 `webui/app.py` 的
        #  `_dispatcher_accounts()`（2026-10-17 审计：面板登录/扫描与派发器之间原本
        #  没有账号级互斥，同一个账号能被两只 Chromium 同时打开）。
        self.accounts = [str(a).strip() for a in (accounts or []) if str(a or "").strip()]
        self.token = uuid.uuid4().hex
        self.acquired = False
        self.record = None

    def acquire(self):
        _ensure_dir(self.path)
        rec = {"pid": os.getpid(), "token": self.token,
               "started_at": iso(now()), "started_ts": time.time(),
               "source": self.source, "dry_run": self.dry_run,
               # run_id 只是给人看的：仪表盘拿它跟账本对上；锁的互斥性只靠 O_EXCL + token
               "run_id": self.run_id,
               "accounts": self.accounts}
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = lock_info(path=self.path)
                if attempt == 1 and (info is None or info.get("stale")):
                    # 残留锁：删掉重试一次。**只删一次**，避免和刚抢到的进程打架
                    #
                    # info is None = 锁文件在、但读不出来（0 字节 / 半截 JSON）。
                    # acquire() 是先 O_CREAT|O_EXCL 建文件、再 json.dump 写内容，
                    # 中间被 kill -9 / 断电就会留下这么一个文件。以前它既不判 stale
                    # 也不清，于是**此后每一次运行都被拒**，而面板还显示「空闲」
                    # （lock_info 同样读不出来）—— 只能人工登机器 rm。
                    # 但「刚建出来的空文件」是合法的中间态，所以给它一个宽限期：
                    # 真在跑的进程几毫秒内就写完了。
                    if info is not None or _lock_age(self.path) > CORRUPT_LOCK_GRACE_SECONDS:
                        try:
                            os.remove(self.path)
                        except OSError:
                            pass
                        continue
                raise RunLocked(info)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
            self.record = rec
            self.acquired = True
            return rec
        raise RunLocked(lock_info(path=self.path))

    def release(self):
        if not self.acquired:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = None
        if isinstance(cur, dict) and cur.get("token") not in (None, self.token):
            return False         # 已经不是我的锁了，别动
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.acquired = False
        return True

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ---- 给仪表盘用的汇总 ------------------------------------------------------

def summarize(record):
    """把一轮记录压成仪表盘要的几个数。缺字段一律按 0/None 处理。"""
    if not record:
        return {"run_id": None, "accounts": 0, "sent_ok": 0, "sent_fail": 0,
                "dry_run": None, "rate_limited": False, "started_at": None,
                "finished_at": None, "skipped": None, "limit": None, "duration": None}
    accts = record.get("accounts") or []
    sent_ok = sum(int(a.get("sent_ok") or 0) for a in accts)
    sent_fail = sum(int(a.get("sent_fail") or 0) for a in accts)
    dur = None
    s, e = parse_iso(record.get("started_at")), parse_iso(record.get("finished_at"))
    if s and e:
        dur = int((e - s).total_seconds())
    return {
        "run_id": record.get("run_id"),
        "accounts": len(accts),
        "sent_ok": sent_ok,
        "sent_fail": sent_fail,
        "dry_run": record.get("dry_run"),
        "rate_limited": bool(record.get("rate_limited")),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "skipped": record.get("skipped"),
        "limit": record.get("limit"),
        "duration": dur,
    }


def account_entries(record, unique_id=None, username=None):
    """取某账号的逐条发送明细（仪表盘展开用）。"""
    for a in (record or {}).get("accounts") or []:
        if unique_id is not None and str(a.get("unique_id")) == str(unique_id):
            return a.get("entries") or []
        if username is not None and a.get("username") == username:
            return a.get("entries") or []
    return []
