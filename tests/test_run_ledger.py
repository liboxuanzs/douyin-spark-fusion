"""core.run_ledger 的单测：账本追加/读取/坏行容忍 + 运行锁的抢锁与残留判定。

全部走临时目录，不碰仓库里的 logs/。
"""
import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from core import run_ledger as rl
from core.util_time import (
    cooldown_human, human_duration, iso, make_run_id, norm_dry_run, now,
    resolve_dry_run,
)


def _rec(run_id="20261001-093000", started=None, finished=None, accounts=None):
    return {
        "run_id": run_id,
        "started_at": started or iso(now()),
        "finished_at": finished,
        "dry_run": "off",
        "rate_limited": False,
        "limit": None,
        "skipped": None,
        "accounts": accounts or [],
    }


class _TempFiles(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.ledger = os.path.join(self._dir.name, "runs.jsonl")
        self.lock = os.path.join(self._dir.name, "run.lock")

    def _write_lines(self, *objs):
        with open(self.ledger, "w", encoding="utf-8") as f:
            for o in objs:
                f.write((o if isinstance(o, str) else json.dumps(o)) + "\n")


class LedgerReadWriteTests(_TempFiles):
    def test_append_then_read_newest_first(self):
        rl.append_run(_rec(run_id="A"), path=self.ledger)
        rl.append_run(_rec(run_id="B"), path=self.ledger)
        runs = rl.read_runs(path=self.ledger)
        self.assertEqual([r["run_id"] for r in runs], ["B", "A"])
        self.assertEqual(rl.last_run(path=self.ledger)["run_id"], "B")

    def test_missing_file_is_empty_not_error(self):
        self.assertEqual(rl.read_runs(path=os.path.join(self._dir.name, "nope.jsonl")), [])
        self.assertIsNone(rl.last_run(path=os.path.join(self._dir.name, "nope.jsonl")))

    def test_bad_lines_are_skipped(self):
        # 坏行 + 非对象行 + 空行都不能影响其余历史
        self._write_lines(_rec(run_id="A"), "{不是 JSON", "", "[1,2,3]", _rec(run_id="B"))
        runs = rl.read_runs(path=self.ledger)
        self.assertEqual([r["run_id"] for r in runs], ["B", "A"])

    def test_limit_caps_result(self):
        for i in range(5):
            rl.append_run(_rec(run_id=f"r{i}"), path=self.ledger)
        self.assertEqual(len(rl.read_runs(limit=2, path=self.ledger)), 2)

    def test_append_run_creates_missing_directory(self):
        deep = os.path.join(self._dir.name, "a", "b", "runs.jsonl")
        self.assertTrue(rl.append_run(_rec(), path=deep))
        self.assertTrue(os.path.exists(deep))

    def test_append_run_does_not_raise_on_bad_path(self):
        # 账本写不进去不能反过来让整轮任务失败
        self.assertFalse(rl.append_run(_rec(), path=os.path.join(self._dir.name, "x", "\0bad")))


class SummarizeTests(unittest.TestCase):
    def test_empty_record(self):
        s = rl.summarize(None)
        self.assertEqual(s["accounts"], 0)
        self.assertIsNone(s["run_id"])

    def test_totals_and_duration(self):
        t0 = now()
        rec = _rec(
            started=iso(t0), finished=iso(t0 + timedelta(seconds=95)),
            accounts=[
                {"username": "a", "sent_ok": 3, "sent_fail": 1, "entries": []},
                {"username": "b", "sent_ok": 2, "sent_fail": 0, "entries": []},
            ],
        )
        s = rl.summarize(rec)
        self.assertEqual((s["accounts"], s["sent_ok"], s["sent_fail"]), (2, 5, 1))
        self.assertEqual(s["duration"], 95)

    def test_missing_counts_are_zero(self):
        s = rl.summarize(_rec(accounts=[{"username": "a"}]))
        self.assertEqual((s["sent_ok"], s["sent_fail"]), (0, 0))

    def test_account_entries_lookup(self):
        rec = _rec(accounts=[
            {"username": "a", "unique_id": "111", "entries": [{"target": "x"}]},
            {"username": "b", "unique_id": "222", "entries": [{"target": "y"}]},
        ])
        self.assertEqual(rl.account_entries(rec, unique_id="222")[0]["target"], "y")
        self.assertEqual(rl.account_entries(rec, username="a")[0]["target"], "x")
        self.assertEqual(rl.account_entries(rec, username="不存在"), [])


class LockTests(_TempFiles):
    def test_acquire_writes_pid_and_release_removes(self):
        lk = rl.RunLock(source="webui", dry_run="full", path=self.lock)
        rec = lk.acquire()
        self.assertEqual(rec["pid"], os.getpid())
        self.assertTrue(os.path.exists(self.lock))
        # 文件内容确实是可读 JSON，仪表盘要直接展示它
        with open(self.lock, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["source"], "webui")
        self.assertTrue(lk.release())
        self.assertFalse(os.path.exists(self.lock))

    def test_lock_record_carries_run_id_for_dashboard(self):
        """锁里要带 run_id：仪表盘靠它把"正在跑的这一轮"对到账本的那一行。"""
        lk = rl.RunLock(source="webui", dry_run="select", path=self.lock,
                        run_id="20261001-093000")
        rec = lk.acquire()
        try:
            self.assertEqual(rec["run_id"], "20261001-093000")
            self.assertEqual(rl.lock_info(path=self.lock)["run_id"], "20261001-093000")
        finally:
            lk.release()

    def test_lock_record_run_id_may_be_absent(self):
        """老调用方不传 run_id 也不能炸（键在、值为 None）。"""
        lk = rl.RunLock(path=self.lock)
        rec = lk.acquire()
        try:
            self.assertIn("run_id", rec)
            self.assertIsNone(rec["run_id"])
        finally:
            lk.release()

    def test_second_acquire_is_refused_while_running(self):
        a = rl.RunLock(source="timer", path=self.lock)
        a.acquire()
        b = rl.RunLock(source="webui", path=self.lock)
        with self.assertRaises(rl.RunLocked) as cm:
            b.acquire()
        self.assertEqual(cm.exception.info["source"], "timer")
        self.assertTrue(rl.is_running(path=self.lock))
        a.release()

    def test_release_only_removes_own_lock(self):
        a = rl.RunLock(path=self.lock)
        a.acquire()
        # 模拟"A 的锁被 B 抢走/替换"：token 变了
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "token": "别人的", "started_at": iso(now())}, f)
        self.assertFalse(a.release())
        self.assertTrue(os.path.exists(self.lock))

    def test_stale_lock_is_taken_over(self):
        # 3 小时前 + 不存在的 pid → 残留锁，应该能抢过来
        old = now() - timedelta(hours=3)
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999999, "token": "dead", "started_at": iso(old)}, f)
        info = rl.lock_info(path=self.lock)
        self.assertTrue(info["stale"])
        self.assertFalse(rl.is_running(path=self.lock))
        lk = rl.RunLock(path=self.lock)
        lk.acquire()
        self.assertEqual(rl.lock_info(path=self.lock)["token"], lk.token)
        lk.release()

    def test_fresh_lock_with_unreadable_time_is_treated_as_running(self):
        # 时间读不出来 → 保守当"还在跑"，绝不因为字段坏掉就放第二个进程进来
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "token": "t", "started_at": ""}, f)
        info = rl.lock_info(path=self.lock)
        self.assertIsNone(info["age_seconds"])
        self.assertFalse(info["stale"])
        self.assertTrue(rl.is_running(path=self.lock))

    def test_dead_pid_but_recent_start_is_stale(self):
        """写锁的进程已经不在了 → 即便 started_at 很新，也是残留锁。

        `_pid_alive` 在 Windows 上可能返回 None（"判断不了"），所以这里把它
        钉死成 False 来验证判定分支本身 —— 真实平台上"判断不了"的回落行为
        由下面那条用例单独钉。
        """
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999999, "token": "t", "started_at": iso(now())}, f)
        with mock.patch.object(rl, "_pid_alive", return_value=False):
            info = rl.lock_info(path=self.lock)
        self.assertFalse(info["pid_alive"])
        self.assertTrue(info["stale"])
        with mock.patch.object(rl, "_pid_alive", return_value=False):
            self.assertFalse(rl.is_running(path=self.lock))
            lk = rl.RunLock(path=self.lock)
            lk.acquire()                    # 残留锁应当能被接管
            self.assertEqual(rl.lock_info(path=self.lock)["token"], lk.token)
            lk.release()

    def test_alive_but_stuck_lock_is_taken_over(self):
        """持锁进程"还活着"，但已经超过 STUCK_LOCK_SECONDS → 当卡死处理，夺锁。

        没有这一支时的真实后果：面板某条线程卡住（或 pid 被复用）→ 之后每次敲门
        都被 RunLocked 拒掉，而 **timer 源被拒时不写账本** → 面板"最近运行"一片
        正常，实际整天一个好友都没发，只能人工登机器 rm 锁。
        """
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "token": "stuck",
                       "started_at": iso(now() - timedelta(hours=7))}, f)
        info = rl.lock_info(path=self.lock)
        self.assertTrue(info["pid_alive"])
        self.assertTrue(info["stale"])
        self.assertEqual(info["stale_reason"], "alive_too_long")
        lk = rl.RunLock(path=self.lock)
        lk.acquire()
        self.assertEqual(rl.lock_info(path=self.lock)["token"], lk.token)
        lk.release()

    def test_alive_and_busy_lock_is_respected(self):
        """真跑得久（多账号 × 多目标）不算残留 —— 上限之内一律放行。"""
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "token": "busy",
                       "started_at": iso(now() - timedelta(hours=5))}, f)
        info = rl.lock_info(path=self.lock)
        self.assertFalse(info["stale"])
        self.assertEqual(info["stale_reason"], "alive")
        self.assertFalse(rl.lock_info(path=self.lock)["stale"])
        with self.assertRaises(rl.RunLocked):
            rl.RunLock(path=self.lock).acquire()

    def test_undeterminable_pid_falls_back_to_age_rule(self):
        """`os.kill(pid, 0)` 判断不了时必须保守：只有超时才敢当残留。"""
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999999, "token": "t", "started_at": iso(now())}, f)
        self.assertIn(rl._pid_alive(999999999), (False, None))   # Linux=False / Windows=None
        with mock.patch.object(rl, "_pid_alive", return_value=None):
            self.assertFalse(rl.lock_info(path=self.lock)["stale"])
            self.assertTrue(rl.is_running(path=self.lock))
            with self.assertRaises(rl.RunLocked):
                rl.RunLock(path=self.lock).acquire()

    def test_lock_info_on_missing_or_broken_file(self):
        self.assertIsNone(rl.lock_info(path=self.lock))
        with open(self.lock, "w", encoding="utf-8") as f:
            f.write("{不是 JSON")
        self.assertIsNone(rl.lock_info(path=self.lock))
        self.assertFalse(rl.is_running(path=self.lock))

    def test_context_manager_releases_on_exception(self):
        try:
            with rl.RunLock(path=self.lock):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(os.path.exists(self.lock))

    def test_pid_alive_maps_exceptions(self):
        """`os.kill(pid, 0)` 的三种结局必须映射成三种语义（不能混为一谈）。

        这是锁判定的地基：把"判断不了"当成"没人在跑"，就会放第二个进程
        进来同时开两个浏览器打同一个账号。

        这里显式把 `os.name` 压成 `posix`：Windows 上判活已经改走 ctypes
        （`_pid_alive_windows`），`os.kill` 分支只在 POSIX 上生效 —— 不压的话
        这条用例在 Windows 上测的是另一条路径，等于什么都没测。
        """
        with mock.patch.object(rl.os, "name", "posix"):
            with mock.patch("core.run_ledger.os.kill") as k:
                k.return_value = None
                self.assertIs(rl._pid_alive(1234), True)

                k.side_effect = ProcessLookupError()
                self.assertIs(rl._pid_alive(1234), False)      # 进程真的不在了
                k.side_effect = PermissionError()
                self.assertIs(rl._pid_alive(1234), True)       # 存在但不是我的
                k.side_effect = OSError("别的错")
                self.assertIsNone(rl._pid_alive(1234))         # 判断不了

    def test_pid_alive_rejects_garbage(self):
        self.assertIsNone(rl._pid_alive("不是数字"))
        self.assertIsNone(rl._pid_alive(None))
        self.assertIsNone(rl._pid_alive(0))
        self.assertIsNone(rl._pid_alive(-1))

    def test_pid_alive_self_is_never_dead(self):
        """自己一定活着：Linux 上返回 True，Windows 某些环境下判断不了返回 None。

        两种都不是 False —— 断言"绝不误判自己已死"才是这条用例的意义。
        Windows 上这条曾经真的会踩空：`os.kill(pid, 0)` 对自己的 pid 抛
        `OSError [WinError 87]`，于是"活着"被判成"判断不了"，残留锁规则接手。
        """
        self.assertIsNot(rl._pid_alive(os.getpid()), False)

    def test_alive_writer_is_never_stale_however_long_it_ran(self):
        """**F4 回归**：持锁进程还活着 → 不论跑了多久都不是残留锁。

        修复前 `lock_info()` 只判"死了"或"超时 7200 秒"，活着的长任务被当成残留
        并被第二个进程夺锁 —— 真跑多账号很容易超过两小时，结果是同一个账号被两个
        Chromium 同时操作（互踢/风控）。跑得久只能说明它慢，不能说明它死了。
        """
        old = iso(now() - timedelta(hours=5))
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "token": "t", "started_at": old}, f)
        with mock.patch.object(rl, "_pid_alive", return_value=True):
            info = rl.lock_info(path=self.lock)
            self.assertTrue(info["pid_alive"])
            self.assertFalse(info["stale"])
            self.assertTrue(rl.is_running(path=self.lock))
            with self.assertRaises(rl.RunLocked):
                rl.RunLock(path=self.lock).acquire()

    def test_windows_pid_check_uses_openprocess_not_os_kill(self):
        """**F5 回归**：Windows 上判活必须走 ctypes，而不是 `os.kill(pid, 0)`。

        实测 Windows 的 `os.kill(pid, 0)`：对自己的 pid 抛 `[WinError 87]`、
        对已死进程抛 `[WinError 11]` —— 错误码与"进程是否存在"对不上，于是
        `core/run_ledger`（判不了 → 最多堵 2 小时）与 `webui/store`（ctypes 判得准）
        在**危险方向上相反**。现在两侧都以 ctypes 为准。
        """
        self.assertTrue(hasattr(rl, "_pid_alive_windows"))
        with mock.patch.object(rl.os, "name", "nt"):
            with mock.patch.object(rl, "_pid_alive_windows", return_value=True) as w:
                self.assertIs(rl._pid_alive(os.getpid()), True)
                w.assert_called_once()
            with mock.patch.object(rl, "_pid_alive_windows", return_value=False):
                self.assertIs(rl._pid_alive(999999999), False)
        # 真实平台上"绝不误判自己已死"这条底线仍然成立
        self.assertIsNot(rl._pid_alive(os.getpid()), False)


class DryRunValueTests(unittest.TestCase):
    """档位解析是"共用一件东西"的典型：命令行、环境变量、webui 表单三处都要一致。"""

    def test_aliases(self):
        self.assertEqual(norm_dry_run("OFF"), "off")
        self.assertEqual(norm_dry_run("select"), "select")
        self.assertEqual(norm_dry_run("TRUE"), "full")
        self.assertEqual(norm_dry_run("1"), "full")

    def test_unknown_falls_back_to_off(self):
        # 拼错档位 → 按默认（真发），不悄悄变成空跑
        for bad in ("yes please", "slect", "2", "空跑"):
            self.assertEqual(norm_dry_run(bad), "off", bad)
        self.assertEqual(norm_dry_run(None), "off")

    def test_priority_cli_over_env(self):
        self.assertEqual(resolve_dry_run("select", "full"), "select")
        self.assertEqual(resolve_dry_run(None, "full"), "full")
        self.assertEqual(resolve_dry_run("", "full"), "full")
        self.assertEqual(resolve_dry_run(None, None), "off")

    def test_run_id_shape(self):
        self.assertRegex(make_run_id(), r"^\d{8}-\d{6}$")

    def test_human_helpers(self):
        self.assertEqual(cooldown_human(0), "已结束")
        self.assertEqual(cooldown_human(45), "45 秒")
        self.assertEqual(cooldown_human(600), "10 分钟")
        self.assertEqual(cooldown_human(3660), "1 小时1 分")
        self.assertEqual(human_duration(95), "1 分35 秒")
        self.assertEqual(human_duration(7200), "2 小时")


class RunSourceTests(unittest.TestCase):
    """触发来源：面板"触发来源"列分不清谁跑的，这一列就白加了。

    三个入口各有各的来源 —— 命令行缺省 cli、面板自己传 webui、
    定时器由 unit 注入 RUN_SOURCE=timer。这里钉住缺省与解析口径。
    """

    def test_default_is_cli(self):
        self.assertEqual(rl.run_source(env=""), "cli")
        self.assertEqual(rl.run_source(env=None), "cli")
        self.assertEqual(rl.run_source(env="   "), "cli")

    def test_timer_and_case_and_space(self):
        self.assertEqual(rl.run_source(env="timer"), "timer")
        self.assertEqual(rl.run_source(env=" TIMER "), "timer")
        self.assertEqual(rl.run_source(env="Timer"), "timer")

    def test_unknown_value_is_passed_through(self):
        # 认不出来就原样给小写（面板 source_text() 也原样显示），
        # 不要悄悄归成 cli —— 否则用户分不清"这是谁跑的"和"这是个新来源"
        self.assertEqual(rl.run_source(env="cron"), "cron")

    def test_reads_env_var_when_not_passed(self):
        with mock.patch.dict(os.environ, {"RUN_SOURCE": "timer"}):
            self.assertEqual(rl.run_source(), "timer")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rl.run_source(), "cli")

    def test_custom_default(self):
        self.assertEqual(rl.run_source(default="webui", env=""), "webui")

    def test_constant_names_match_webui_labels(self):
        """webui 的 SOURCE_LABELS 用的是同一批字面量，改了这里要一起改。"""
        self.assertEqual(
            (rl.SOURCE_CLI, rl.SOURCE_TIMER, rl.SOURCE_WEBUI), ("cli", "timer", "webui")
        )


class LedgerAppendDurabilityTests(_TempFiles):
    """append_run 的两条写盘契约：写前补尾换行、写完 fsync（审计项：低）。

    没有这两条时的真实后果：
    - 不补尾换行：崩溃/断电留下半行后，下一次 append 会**接在断行后面**，两条记录
      粘成一行 → read_runs 按坏行跳过，两条一起丢。表现为"这一天明明发过、账本上
      没有"，而面板的"最近运行"和冷却判定都读这个文件。
    - 不 fsync：那条记录可能还躺在页缓存里，断电同样表现成"发过了但账本没有"。
    """

    def test_partial_tail_line_does_not_glue_the_next_record(self):
        # 模拟"上一次写到一半就挂了"：是合法 JSON，但没有结尾换行
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write(json.dumps(_rec(run_id="A")))
        self.assertTrue(rl.append_run(_rec(run_id="B"), path=self.ledger))
        self.assertEqual(
            [r["run_id"] for r in rl.read_runs(path=self.ledger)], ["B", "A"]
        )

    def test_normal_append_does_not_add_blank_lines(self):
        rl.append_run(_rec(run_id="A"), path=self.ledger)
        rl.append_run(_rec(run_id="B"), path=self.ledger)
        with open(self.ledger, "rb") as f:
            raw = f.read()
        self.assertEqual(raw.count(b"\n"), 2, raw)
        self.assertNotIn(b"\n\n", raw)

    def test_tail_is_newline_helper(self):
        with open(self.ledger, "wb") as f:
            f.write(b"")                      # 空文件：没什么可补的
        with open(self.ledger, "rb") as f:
            self.assertTrue(rl._tail_is_newline(f))
        with open(self.ledger, "wb") as f:
            f.write(b"x")
        with open(self.ledger, "rb") as f:
            self.assertFalse(rl._tail_is_newline(f))
        with open(self.ledger, "wb") as f:
            f.write(b"x\n")
        with open(self.ledger, "rb") as f:
            self.assertTrue(rl._tail_is_newline(f))

    def test_append_run_fsyncs_the_record(self):
        real_fsync = os.fsync
        seen = []

        def spy(fd):
            seen.append(fd)
            return real_fsync(fd)

        with mock.patch.object(os, "fsync", spy):
            self.assertTrue(rl.append_run(_rec(run_id="S"), path=self.ledger))
        self.assertTrue(seen, "append_run 必须 flush + fsync：不落盘的话断电就丢这一条")
        self.assertEqual([r["run_id"] for r in rl.read_runs(path=self.ledger)], ["S"])


class DefaultPathTests(unittest.TestCase):
    """账本 / 运行锁的默认路径必须锚到**仓库根**（审计项：中）。

    默认值以前是相对 CWD 的 "logs/runs.jsonl"：从别的目录执行
    `python /opt/douyin-spark-fusion/main.py task` 时锁会落到那个目录里，等于
    **没有锁** —— 定时任务与手动触发可能同时开两个浏览器发同一个账号（重复发送）。
    `.env.example` 里写的承诺就是"相对路径按仓库根解析"。
    """

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(rl.__file__)))

    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("RUN_LEDGER_FILE", "RUN_LOCK_FILE")}
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    def test_defaults_are_absolute_under_the_repo_root(self):
        with self._env():
            self.assertEqual(rl.ledger_path(),
                             os.path.join(self.root, "logs", "runs.jsonl"))
            self.assertEqual(rl.lock_path(),
                             os.path.join(self.root, "logs", "run.lock"))

    def test_relative_env_value_is_resolved_against_the_repo_root(self):
        with self._env(RUN_LEDGER_FILE="logs/x.jsonl", RUN_LOCK_FILE="tmp/run.lock"):
            self.assertEqual(rl.ledger_path(), os.path.join(self.root, "logs", "x.jsonl"))
            self.assertEqual(rl.lock_path(), os.path.join(self.root, "tmp", "run.lock"))

    def test_absolute_env_value_is_kept(self):
        with self._env(RUN_LEDGER_FILE="/var/log/x.jsonl"):
            self.assertEqual(rl.ledger_path(), "/var/log/x.jsonl")


if __name__ == "__main__":
    unittest.main()
