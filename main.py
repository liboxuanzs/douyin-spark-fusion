"""程序入口 —— 按启动模式分派。

一个镜像要同时支持两种定时部署形态，靠这里分流：

    python main.py              跑一轮任务（= task）。服务器 cron / systemd timer 都走这条。
    python main.py task         同上，显式写法。
    python main.py task --force 不看时间表：所有账号现在就发（面板"立即运行"同义）。
    python main.py task --only 11223344556
                                只处理这一个账号（抖音号或昵称）；它只缩小候选范围，
                                要不要看时间表仍由 --force 决定。
    python main.py dry-run      空跑一轮（不点发送）—— 等价于 DRY_RUN=full。
    python main.py dry-run select
                                只跑到"选中"那一步（不输入、不发送）。
    python main.py prune        盘一下磁盘：每个登录资料目录多大、多少是可再生缓存、
                                哪些目录已经不属于任何在册账号（默认**只预演**，不删）。
    python main.py prune --apply [--caches] [--older-than 7] [--json]
                                真的清理：--caches 连可再生缓存一起清，
                                --older-than 只删闲置超过 N 天的孤儿目录。
    python main.py notify-test  推一条测试通知（确认 webhook 地址通不通），
                                不发送任何消息、不碰运行锁。
    python main.py fc           云函数模式（上游保留；本融合版仓库没有 fc_server.py，
                                这条分支不可用）。

也支持环境变量：RUN_MODE 指定模式、DRY_RUN 指定空跑档位（命令行参数优先）、
RUN_SOURCE 标记本轮是谁触发的（cli / timer，只该由定时器的 unit 设置，
面板"触发来源"那一列读的就是它）。
退出码：0=整轮跑完（个别目标失败也算跑完），**"没有账号到点"同样是 0**；
        1=整轮被跳过或账号全部出错；2=参数错误。
        "没到点"必须算成功：定时器现在是每 5 分钟敲一次门的派发器
        （见 core/schedule.py），把这种常态记成 failed 会让 systemd 一天红 288 次，
        真正的故障反而淹了。
账本（logs/runs.jsonl）里有一轮的结构化结果 —— 但"没到点"的敲门**不进账本**。

`.env` 由本文件自己读（绝对路径、文件优先），**不要**把它交给 systemd 的
`EnvironmentFile=` —— 那会吃掉消息模板里的字面 `\\n`，换行就没了（见 `_load_env`）。
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

#: `python main.py prune` 自己的开关（`_parse_maintenance` 解析，`_parse_flags`
#: 只负责别把它们当成"认不出来的开关"）。
MAINTENANCE_SWITCHES = ("--apply", "--caches", "--json")


def _load_env():
    """把仓库根的 `.env` 读进环境变量 —— **文件是配置的唯一真相**。

    绝对路径（不依赖 CWD），且文件覆盖进程环境里的同名值。为什么非这样不可：

    * systemd 的 `EnvironmentFile=` 按**转义序列**解析值，`.env` 里消息模板写的
      字面 `\\n`（规则："用 \\n 表示换行"）会被吃成裸 `n` —— 发出去的消息变成
      一整行、行尾还挂着 n。2026-10-02 20:38 那一轮的真实故障就是这个。
    * 面板"消息模板预览"读的是这个文件。文件不赢，就会出现
      "面板显示两行、实际发出去一行"，也就是用户最反感的"面板一套、实际一套"。
    * 所以两个 unit 里都**故意不写** `EnvironmentFile=`，改由这里读；
      万一别处仍注入了不同的值，下面这条 WARNING 会把痕迹留在日志里。

    `RUN_MODE` / `DRY_RUN` / `RUN_SOURCE` 例外：那是"这一轮怎么跑"，由启动方决定。
    """
    from utils.config import drift_report, load_env_file

    drift = load_env_file(_ROOT / ".env")
    if not drift:
        return
    from utils.logger import setup_logger

    setup_logger(level=os.getenv("LOG_LEVEL", "Info")).warning(
        "进程环境里的值跟 .env 不一样，已按 .env 为准（检查启动器/unit 是否注入了旧值）："
        + drift_report(drift)
    )


_load_env()


def _install_sigterm_handler():
    """把 systemd 的超时信号变成"能收尾的"退出。

    `deploy/douyin-spark-fusion-daily.service` 里写着 `TimeoutStartSec=3600`：
    一轮跑到一小时，systemd 会先发 SIGTERM。Python 的默认动作是**立刻死掉** ——
    `core/tasks.py` 收尾那段 `finally`（记账 + 写时间表）一行都不会跑，于是
    "已经发出去的消息"没有记账；下一个门铃读到的是"今天还没发"，原样再发一遍，
    好友收到第二条（本项目最刺眼的线上事故）。

    改成抛 `SystemExit` 之后，栈会正常展开：浏览器句柄照关，已经跑完的账号
    照记，锁照放，然后进程带着 128+15 退出。systemd 那一侧仍会把这一轮记成
    超时失败（这是对的：它确实没跑完），但重复发送被挡住了。
    """
    import signal

    def _stop(signum, _frame):
        raise SystemExit(128 + int(signum))

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError, AttributeError):
        # 非主线程 / 平台没有 SIGTERM：装不上就算了，不能因此起不来。
        pass


_install_sigterm_handler()


def _parse_flags(args):
    """把 `--force` / `--only X` 从命令行里挑出来，其余原样返回。

    手写而不用 `argparse`：位置参数既有"子命令"又有"空跑档位"，
    而 `--only` 的值又可能长得跟档位一样（`python main.py dry-run --only select`），
    argparse 的位置参数会在这种组合上给出很难解释的报错。
    这几个开关都只有一层、且互不影响，手写反而更好读。

    `--force` 与 `--force-all` 的区别见 `core.tasks.runTasks`：前者只无视
    「到没到点」（面板「立即运行」同义），后者连「今天已经发过」也无视。

    `--only` 后面没跟值时**不在这里退**，只记下来交给 `main()` 报错退 2 ——
    这个模块在别处会被 import（测试、面板探测依赖），import 期 `sys.exit` 会
    把"命令行写错了"变成"导入这个文件就死"。
    """
    force, force_all, only, rest, error = False, False, "", [], ""
    index = 0
    while index < len(args):
        item = args[index]
        if item in ("--force", "-f"):
            force = True
        elif item == "--force-all":
            force_all = True
        elif item in MAINTENANCE_SWITCHES or item.startswith("--older-than="):
            # `prune` 子命令自己的开关（见 `_parse_maintenance`）。放在这里只是
            # 为了让它们**不落进**下面那条"以 - 开头就报错"的分支：否则
            # `python main.py prune --apply` 会被判成"认不出来的开关：--apply"。
            #
            # ★ 它们**不进 rest**（`pass` 就是丢掉），所以 `_prune()` 不能读
            #   `_ARGS` —— 那样 `--apply` 会被静默吞掉，`prune --apply` 退化成
            #   纯预演（用户以为删了，其实一个文件都没动，而且没有任何提示）。
            #   `_prune()` 因此直接解析 `sys.argv[1:]`。
            pass
        elif item.startswith("--only="):
            only = item.split("=", 1)[1].strip()
        elif item in ("--only", "-o"):
            if index + 1 >= len(args):
                error = "--only 后面要跟抖音号或昵称"
            else:
                index += 1
                only = str(args[index]).strip()
        elif item.startswith("-") and item != "-":
            # 认不出来的开关**不能**塞进 rest：它会变成"子命令"，而 rest 里
            # 只要有一个合法位置参数，它就被静默忽略 —— `task --froce` 等于 `task`，
            # 用户以为限制了什么，其实没有。
            #
            # ★ 这一条必须放在**所有**合法开关之后：它按"以 - 开头"通吃，
            #   排在前面会把 `--only` / `-o` / `--only=x` 全部吞掉（线上真的发生过：
            #   2026-10-09 想用它只跑一个账号，得到的却是「认不出来的开关：--only」，
            #   而报错里给出的示例偏偏就是这个用法）。
            error = error or f"认不出来的开关：{item}"
        else:
            rest.append(item)
        index += 1
    if not error and not only and any(a in ("--only", "-o", "--only=") for a in args):
        error = "--only 后面要跟抖音号或昵称"
    return ("all" if force_all else force), only, rest, error


def _parse_maintenance(args):
    """解析 `python main.py prune […]` 的开关。

    只在这个子命令里用，所以没有塞进 `_parse_flags` 的返回值（那会动到一堆
    既有测试与调用方）。认不出来的写法**不静默忽略**：返回 error 让 main()
    报错退 2 —— 清理是删文件的动作，"`--appli` 少了半个字母"绝不能变成
    "什么都没干但看起来成功了"，更不能变成"按默认宽限期删了"。
    """
    info = {"apply": False, "caches": False, "json": False, "grace_days": None, "error": ""}
    index = 0
    while index < len(args):
        item = args[index]
        if item in ("prune", "clean", "cleanup"):
            pass
        elif item == "--apply":
            info["apply"] = True
        elif item == "--caches":
            info["caches"] = True
        elif item == "--json":
            info["json"] = True
        elif item.startswith("--older-than="):
            info["grace_days"] = item.split("=", 1)[1]
        elif item == "--older-than":
            if index + 1 >= len(args):
                info["error"] = "--older-than 后面要跟天数（例如 --older-than 7）"
            else:
                index += 1
                info["grace_days"] = args[index]
        else:
            info["error"] = info["error"] or f"认不出来的开关：{item}"
        # ★ 这一行不能少：少了它 `while` 会在同一个元素上原地打转，
        #   表现是 `main.py prune` 占满一个核、永远不返回（2026-10-14 实测，
        #   在真机上跑了 7 分钟 100% CPU 才发现）。解析循环必须每轮前进。
        index += 1

    raw = info["grace_days"]
    info["grace_days"] = None
    if raw is not None and not info["error"]:
        try:
            days = float(raw)
        except (TypeError, ValueError):
            info["error"] = f"--older-than 要一个天数，给的是 {raw!r}"
        else:
            if days < 0:
                info["error"] = "--older-than 不能是负数"
            else:
                info["grace_days"] = days
    return info


def _prune(argv=None):
    """盘一下磁盘并（可选）清理。默认只预演：不传 `--apply` 一个文件都不删。

    `argv` 默认取 `sys.argv[1:]`（**不是** `_ARGS`）：`_parse_flags` 会把
    `--apply`/`--caches`/`--json` 这类 prune 自己的开关丢掉（见那里的注释），
    拿 `_ARGS` 解析等于"开关全被吞掉"—— 实测表现是 `main.py prune --caches`
    不打缓存段、`--apply` 静默退化成预演。传 `argv` 只为测试留缝。
    """
    import json as _json

    from core import maintenance

    opts = _parse_maintenance(list(sys.argv[1:] if argv is None else argv))
    if opts["error"]:
        print(f"{opts['error']}（例：python main.py prune --apply --caches）", file=sys.stderr)
        return 2

    report = maintenance.profile_report()
    result = {"report": report}
    if opts["caches"]:
        result["caches"] = maintenance.prune_caches(apply=opts["apply"])
    result["orphans"] = maintenance.prune_orphans(
        apply=opts["apply"],
        grace_days=(maintenance.ORPHAN_GRACE_DAYS if opts["grace_days"] is None
                    else opts["grace_days"]),
    )

    if opts["json"]:
        print(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for line in maintenance.report_lines(report):
            print(line)
        if "caches" in result:
            print()
            for line in maintenance.prune_lines(result["caches"], what="浏览器缓存"):
                print(line)
        print()
        for line in maintenance.prune_lines(result["orphans"], what="孤儿资料目录"):
            print(line)
        if not opts["apply"]:
            print("（以上是预演。真要删：python main.py prune --apply）")

    # 退出码：名单读不到（会有 error）时给 1，让定时/人工能发现"盘点本身没做成"。
    if not report.get("exists") or report.get("error"):
        return 1 if report.get("error") else 0
    return 0


FORCE, ONLY, _ARGS, FLAG_ERROR = _parse_flags(list(sys.argv[1:]))

# 优先级：argv 子命令 > RUN_MODE 环境变量 > 默认 task
MODE = (_ARGS[0] if _ARGS else os.getenv("RUN_MODE", "task")).strip().lower()


def dry_run_notice(record):
    """空跑提示文案（真跑时返回空串）。

    ★ 以**账本里最终生效的档位**为准，不要看 `_run` 的入参：`main.py task`
    传进来的是 `None`（表示"让 .env 的 DRY_RUN 说了算"），拿它跟 `off` 比
    永远不相等 —— 于是真发的那一轮也会在日志里留下
    "[空跑] 档位=None —— 一条消息都没有真的发出去"，
    面板的日志页正好会把这行显示给用户看。
    """
    from core.util_time import DRY_RUN_OFF

    level = (record or {}).get("dry_run") or DRY_RUN_OFF
    if level == DRY_RUN_OFF:
        return ""
    return f"[空跑] 档位={level} —— 一条消息都没有真的发出去"


def _idle_notice(record):
    """"没有账号到点"的人话说明。要看得出下一个到点的是谁。"""
    brief = (record or {}).get("schedule") or {}
    waiting = brief.get("waiting") or []
    if not waiting:
        return "本轮没有账号到点：今天该发的都发过了"
    soon = sorted(waiting, key=lambda row: str(row.get("due_at") or ""))[:3]
    return ("本轮没有账号到点，最近的一批：" + "、".join(
        f"{row.get('username')}@{row.get('due_at')}" for row in soon))


def _auto_prune_notice():
    """定时器轮次里顺带做一次磁盘维护（默认开；`MAINTENANCE_AUTO_PRUNE_HOURS=0` 关）。

    缓存只涨不落（实测约 180 MB/天/账号），而 `prune` 与面板按钮都要人记得点；
    磁盘满 = 发送直接失败。这里按 `MAINTENANCE_AUTO_PRUNE_HOURS`（默认一周）
    到点清一次，**绝大多数轮次只是一次状态文件读**（约 1 ms）。

    维护永远不能影响发送：这个函数自己不抛，`core.maintenance` 那边也不抛。
    """
    try:
        from core import maintenance

        result = maintenance.maybe_auto_prune()
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        print(f"[维护] 自动清理跳过（不影响发送）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return
    if not result.get("ran"):
        return
    caches = (result.get("caches") or {}).get("human_freed") or "0 B"
    orphans = (result.get("orphans") or {}).get("human_freed") or "0 B"
    print(f"[维护] 自动清理：回收 {result.get('human_freed')}"
          f"（浏览器缓存 {caches} / 孤儿资料目录 {orphans}）")
    if result.get("reason") != "ok":
        print(f"[维护] ⚠️ 这次只清了一部分：{result.get('error')}", file=sys.stderr)


def _notify_test():
    """推一条测试通知。成功 0、失败 1，说明文字里带原因（HTTP 码/接口报错）。"""
    from core import notify

    ok, detail = notify.send_test()
    print(("✅ " if ok else "❌ ") + detail, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def _run(dry_run, source=None, force=None, only=None):
    from core.run_ledger import SOURCE_TIMER, run_source
    from core.tasks import runTasks

    # 不给 source 就从 RUN_SOURCE 读（定时器的 unit 会设成 timer），
    # 都没有就是命令行 —— 面板上"触发来源"那一列靠它区分。
    source = source or run_source()
    if dry_run is None:
        # 只有真轮次才顺带维护：空跑（dry-run）是"人正在盯着看"，不该动文件。
        _auto_prune_notice()
    rec = runTasks(
        dry_run=dry_run, source=source,
        force=FORCE if force is None else force,
        only=ONLY if only is None else only,
    )
    notice = dry_run_notice(rec)
    if notice:
        print(notice)
    if rec.get("nothing_due"):
        # 定时器每 5 分钟敲一次门，绝大多数敲门都是这个结果，它是**正常**的。
        print(_idle_notice(rec))
        return 0
    if rec.get("skipped"):
        reason = rec["skipped"].get("reason")
        # 定时器来源的跳过一律算成功：门铃响的时候"正有一轮在跑""还在冷却"
        # 都是常态，记成 failed 会让 systemd 一天红 288 次。
        if source == SOURCE_TIMER:
            print(f"本轮跳过：{reason}（定时器常态，不算失败）")
            return 0
        print(f"本轮被跳过：{reason}", file=sys.stderr)
        return 1
    accounts = rec.get("accounts") or []
    if accounts and all(a.get("status") == "error" for a in accounts):
        return 1
    return 0


def main():
    if FLAG_ERROR:
        print(FLAG_ERROR + "（例：python main.py task --only 11223344556）", file=sys.stderr)
        sys.exit(2)

    if MODE in {"fc", "serve"}:
        # 上游的阿里云函数计算形态需要 fc_server.py + docker/entrypoint-fc.sh，
        # 本融合版仓库里这两样都没有（见模块 docstring）。以前这里直接
        # `from fc_server import serve`，用户看到的是一段 ImportError 堆栈；
        # 现在明确说清「这个形态不可用 + 该用什么」。
        print(
            "云函数（FC）模式在本融合版里不可用：仓库没有 fc_server.py 与 "
            "docker/entrypoint-fc.sh。请用 `python main.py task`（cron / systemd timer）。",
            file=sys.stderr,
        )
        sys.exit(2)

    if MODE in {"dry-run", "dryrun", "dry"}:
        # `python main.py dry-run [select|full|off]`，默认 full（真输入进编辑器再清空）
        from core.util_time import DRY_RUN_LEVELS

        level = (_ARGS[1] if len(_ARGS) > 1 else "full").strip().lower()
        # ★ 认不出来的档位必须**报错退出**，绝不能静默当成 off（= 真发）。
        # `norm_dry_run` 是有意回落 off 的（DRY_RUN 环境变量写错时，宁可发出去
        # 也别静默不发），但命令行是人在现打：`dry-run of` 少一个字母，
        # 一批消息就真发出去了，收不回来。
        if level not in DRY_RUN_LEVELS:
            print(
                f"空跑档位不认：{level!r}（可选：{'、'.join(DRY_RUN_LEVELS)}）",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(_run(level))

    if MODE in {"prune", "clean", "cleanup"}:
        # 磁盘维护：盘点资料目录体积、清理孤儿账号目录与可再生缓存。
        # 默认**只预演**，要删必须显式 --apply（见 core/maintenance.py）。
        sys.exit(_prune())

    if MODE in {"notify-test", "notify", "test-notify"}:
        # 只是"这条地址通不通"：不发送、不占运行锁、不写时间表。
        sys.exit(_notify_test())

    if MODE in {"task", "run", "cli", ""}:
        sys.exit(_run(None))

    print(f"未知启动模式: {MODE}（可选：task / dry-run / prune / fc）", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
