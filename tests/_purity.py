"""跨进程的"导入纯净性"探针：验证某个模块不会顺带把浏览器栈拉进来。

为什么要用**子进程**（issue #3 的机制一）
----------------------------------------
`sys.modules` 是**进程级**状态，而 pytest / unittest 都在同一个进程里跑完一整个文件。
本仓库有一批用例会"真把面板跑一遍"（登录、点按钮、渲染页面），它们顺手就会把
`core.tasks`、`cloakbrowser` 装进 `sys.modules`。之后再断言

    self.assertNotIn("cloakbrowser", sys.modules)

测到的其实是"这条用例排在第几个"——而顺序由测试框架决定（pytest 对 `unittest.TestCase`
按方法名字典序排，ASCII 名排在中文名之前），不由被测代码决定。于是同一个文件里
"单跑 1 passed / 整文件跑 60 failed"。

把断言搬进**全新子进程**后，`sys.modules` 里只可能有 `import webui.app` 自己造成的变化，
断言才重新变成"真的能失败、真的能通过"：

* 干净 → 空集合；
* 哪天有人给 `webui/app.py` 加了一句模块级 `import core.tasks` → 子进程立刻报出来。

为什么结果可以缓存
------------------
子进程是与本进程隔离的：它的结论只取决于仓库里的源码，与本进程此刻的 `sys.modules`
无关（这正是把它放子进程的全部意义）。所以同一轮测试里 `probe_import()` 的结果是稳定的，
用 `lru_cache` 把它记住，14 个 `WebUITestCase` 子类就只会真的起一次解释器，
不会为了修顺序依赖而给测试套件塞进十几秒的启动开销。

跑不起来了怎么办
----------------
子进程起不来（解释器缺失 / 超时 / 脚本自己报错）**既不跳过也不通过**，
而是抛 `PurityProbeFailure`（`AssertionError` 的子类，所以 unittest 记成 failure）。
理由同上：一个"环境不对就自动变绿"的纯净性断言等于没有断言。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, Iterable, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# webui 是薄层：这两个是真正的"浏览器依赖"，出现即违规。
BROWSER_MODULES: Tuple[str, ...] = ("cloakbrowser", "playwright")

# `core.tasks` 在模块级就 `import cloakbrowser`，所以"没碰 core.tasks"同样属于导入纯净性。
CORE_MODULES: Tuple[str, ...] = ("core.tasks",)

# `import webui.app` 允许出现的最大集合：一个都不许有。
IMPORT_STACK: Tuple[str, ...] = BROWSER_MODULES + CORE_MODULES

# 手动触发路径允许出现的最大集合：`core.tasks` 由调用方注入，剩下的一个都不许有。
TRIGGER_STACK: Tuple[str, ...] = BROWSER_MODULES

DEFAULT_TIMEOUT = 120.0

# 子进程把结论按这个前缀写在 stdout 的最后一行；其余输出（warnings 等）走 stderr。
_RESULT_PREFIX = "PURITY_PROBE "

_CHILD_SCRIPT = """
import json
import sys
import types

config = json.loads(sys.argv[1])
trigger = config["trigger"]
targets = config["targets"]

if trigger:
    # 与 tests/test_webui.py 里的做法一致：调用方显式注入 core.tasks，
    # 这样被测的只有 webui 自己那条"惰性导入"路径（load_run_tasks 拿现成的模块）。
    stub = types.ModuleType("core.tasks")

    def runTasks(*args, **kwargs):
        return {"stub": True}

    stub.runTasks = runTasks
    sys.modules["core.tasks"] = stub

import webui.app as webui_app  # noqa: E402  （被测对象，必须放在 stub 之后）

if trigger:
    runner = webui_app.load_run_tasks()
    if getattr(runner, "__name__", "") != "runTasks":
        raise SystemExit("load_run_tasks 没拿到 stub 的 runTasks：%r" % (runner,))

payload = {
    "imported": "webui.app" in sys.modules,
    "pulled": sorted(name for name in targets if name in sys.modules),
}
sys.stdout.write(config["prefix"] + json.dumps(payload) + "\\n")
"""


class PurityProbeFailure(AssertionError):
    """探针本身没跑起来（不是"被测代码不干净"，而是"这句话没法判"）。"""


class PurityProbe:
    """一次子进程探针的结论。`pulled` 是**仍然出现在 sys.modules 里**的违规模块名。"""

    def __init__(self, *, pulled: FrozenSet[str], targets: Tuple[str, ...], trigger: bool,
                 stdout: str, stderr: str) -> None:
        self.pulled = pulled
        self.targets = targets
        self.trigger = trigger
        self.stdout = stdout
        self.stderr = stderr

    @property
    def what(self) -> str:
        return "手动触发路径" if self.trigger else "import webui.app"

    def assert_clean(self, *, note: str = "") -> None:
        """断言子进程里一个违规模块都没被拉进来；带了就报出是哪几个。"""
        if not self.pulled:
            return
        names = " / ".join(sorted(self.pulled))
        raise AssertionError(
            f"{self.what}不该把 {names} 拉进 sys.modules（子进程实测：{sorted(self.pulled)}）"
            + (f"；{note}" if note else "")
        )


def _child_env() -> dict:
    """子进程环境：显式带上仓库根，别指望调用方的 cwd / 已经装好的 PYTHONPATH。

    原样保留调用方已有的 PYTHONPATH（`PYTHONPATH=/home/lbx/localshim` 那种垫片
    仍然要生效），只在最前面追加仓库根。
    """
    env = dict(os.environ)
    parts = [str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONIOENCODING"] = "utf-8"
    # 别让探针往仓库里丢 __pycache__
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _parse_child_stdout(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            try:
                return json.loads(line[len(_RESULT_PREFIX):])
            except ValueError as exc:  # pragma: no cover - 只有在子进程被污染时才可能
                raise PurityProbeFailure(f"探针输出的结论不是 JSON：{line!r}（{exc}）") from exc
    raise PurityProbeFailure(f"探针没有输出结论行，stdout={stdout!r}")


@lru_cache(maxsize=None)
def _probe_cached(trigger: bool, targets: Tuple[str, ...], timeout: float) -> PurityProbe:
    config = json.dumps({"trigger": trigger, "targets": list(targets),
                         "prefix": _RESULT_PREFIX})
    try:
        done = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT, config],
            cwd=str(REPO_ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PurityProbeFailure(
            f"纯净性探针超过 {timeout:g}s 没跑完（解释器 {sys.executable}）：{exc}"
        ) from exc
    except OSError as exc:  # 解释器起不来
        raise PurityProbeFailure(f"纯净性探针起不了子进程（解释器 {sys.executable}）：{exc}") from exc

    if done.returncode != 0:
        raise PurityProbeFailure(
            f"纯净性探针在子进程里失败（exit={done.returncode}）：\n"
            f"stdout={done.stdout}\nstderr={done.stderr}"
        )
    payload = _parse_child_stdout(done.stdout)
    if not payload.get("imported"):
        # 万一日后 `-c` 的 sys.path 规则变了，别让"没导入成功"伪装成"导入很干净"。
        raise PurityProbeFailure(
            f"探针子进程没能 import webui.app（stdout={done.stdout!r} stderr={done.stderr!r}）"
        )
    return PurityProbe(
        pulled=frozenset(payload.get("pulled", ())),
        targets=targets,
        trigger=trigger,
        stdout=done.stdout,
        stderr=done.stderr,
    )


def probe_import(*, trigger: bool = False, targets: Iterable[str] = IMPORT_STACK,
                 timeout: float = DEFAULT_TIMEOUT) -> PurityProbe:
    """在全新子进程里跑 `import webui.app`（可选再走一遍触发路径），返回违规模块集合。

    trigger=False：只 `import webui.app`，对照 `IMPORT_STACK`。
    trigger=True ：先注入 stub `core.tasks` 再 `import webui.app`，然后调
                   `webui_app.load_run_tasks()` —— 那正是"立即运行"那条路径唯一的
                   惰性导入点。对照 `TRIGGER_STACK`（`core.tasks` 是注入的，不算违规）。
    """
    ordered = tuple(dict.fromkeys(targets))  # 去重且保序，好让缓存键稳定
    return _probe_cached(bool(trigger), ordered, float(timeout))


def assert_imports_clean(*, trigger: bool = False, targets: Iterable[str] = IMPORT_STACK,
                         note: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
    """`probe_import(...).assert_clean()` 的简写。"""
    probe_import(trigger=trigger, targets=targets, timeout=timeout).assert_clean(note=note)
