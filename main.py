"""程序入口 —— 按启动模式分派。

一个镜像要同时支持两种定时部署形态，靠这里分流：

    python main.py              跑一轮任务（= task）。服务器 cron / systemd timer 都走这条。
    python main.py task         同上，显式写法。
    python main.py task --force 不看时间表：所有账号现在就发（面板"立即运行"同义）。
    python main.py task --only 10050yi
                                只处理这一个账号（抖音号或昵称）；它只缩小候选范围，
                                要不要看时间表仍由 --force 决定。
    python main.py dry-run      空跑一轮（不点发送）—— 等价于 DRY_RUN=full。
    python main.py dry-run select
                                只跑到"选中"那一步（不输入、不发送）。
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
    from utils.config import load_env_file

    drift = load_env_file(_ROOT / ".env")
    if not drift:
        return
    from utils.logger import setup_logger

    setup_logger(level=os.getenv("LOG_LEVEL", "Info")).warning(
        "进程环境里的值跟 .env 不一样，已按 .env 为准（检查启动器/unit 是否注入了旧值）："
        + "；".join(f"{k} 进程={a!r} → 文件={b!r}" for k, (a, b) in sorted(drift.items()))
    )


_load_env()


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
        elif item.startswith("-") and item != "-":
            # 认不出来的开关**不能**塞进 rest：它会变成"子命令"，而 rest 里
            # 只要有一个合法位置参数，它就被静默忽略 —— `task --froce` 等于 `task`，
            # 用户以为限制了什么，其实没有。
            error = error or f"认不出来的开关：{item}"
        elif item.startswith("--only="):
            only = item.split("=", 1)[1].strip()
        elif item in ("--only", "-o"):
            if index + 1 >= len(args):
                error = "--only 后面要跟抖音号或昵称"
            else:
                index += 1
                only = str(args[index]).strip()
        else:
            rest.append(item)
        index += 1
    if not error and not only and any(a in ("--only", "-o", "--only=") for a in args):
        error = "--only 后面要跟抖音号或昵称"
    return ("all" if force_all else force), only, rest, error


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


def _run(dry_run, source=None, force=None, only=None):
    from core.run_ledger import SOURCE_TIMER, run_source
    from core.tasks import runTasks

    # 不给 source 就从 RUN_SOURCE 读（定时器的 unit 会设成 timer），
    # 都没有就是命令行 —— 面板上"触发来源"那一列靠它区分。
    source = source or run_source()
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
        print(FLAG_ERROR + "（例：python main.py task --only 10050yi）", file=sys.stderr)
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

    if MODE in {"task", "run", "cli", ""}:
        sys.exit(_run(None))

    print(f"未知启动模式: {MODE}（可选：task / dry-run / fc）", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
