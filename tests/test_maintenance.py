"""`core.maintenance` —— 资料目录盘点与清理的测试。

清理会**真删文件**，所以这里的重点不是"能不能删掉"，而是"什么时候绝不该删"：

* 登录态的载体（Cookies / Local Storage / Preferences）必须原样留着 ——
  删错的代价是账号得重新扫码，而用户往往正是因为"昨天没发出去"才来看的；
* 不在册的目录要删，但**刚出现的**（宽限期内）与**正被浏览器占着的**不能删；
* 账号名单读不到时（`TASKS` 写坏 / 都缺 `COOKIES_*`）**一个都不许删** ——
  那种情况下每个目录都会被算成孤儿，是最危险的一种"空名单"；
* 资料根目录里塞一个指向外部的符号链接，也不能把外面删了。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import maintenance as mt  # noqa: E402


def _write(path: Path, size: int = 16) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def make_profile(root: Path, name: str, *, cache_mb: int = 0, with_login: bool = True) -> Path:
    """造一个像模像样的资料目录：登录态 + 可再生的缓存。"""
    profile = root / name
    if with_login:
        _write(profile / "Default" / "Cookies", 32)
        _write(profile / "Default" / "Local Storage" / "leveldb" / "000003.log", 32)
        _write(profile / "Local State", 32)
    if cache_mb:
        _write(profile / "Default" / "Cache" / "Cache_Data" / "f_000001", cache_mb * 1024)
        _write(profile / "Default" / "Code Cache" / "js" / "index", 64)
        _write(profile / "ShaderCache" / "data_0", 64)
        _write(profile / "BrowserMetrics-spare.pma", 64)
    return profile


class HumanSizeTests(unittest.TestCase):
    def test_units_and_rounding(self):
        self.assertEqual(mt.human_size(0), "0 B")
        self.assertEqual(mt.human_size(999), "999 B")
        self.assertEqual(mt.human_size(1024), "1.0 KB")
        self.assertEqual(mt.human_size(1536), "1.5 KB")
        self.assertEqual(mt.human_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(mt.human_size(3 * 1024 ** 3), "3.0 GB")

    def test_garbage_is_zero_not_an_exception(self):
        for bad in (None, "", "abc", -5, float("nan")):
            self.assertTrue(mt.human_size(bad).endswith(("B", "KB", "MB", "GB", "TB")),
                            msg=f"{bad!r} 不该炸")


class DirSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsf-maint-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_counts_nested_files(self):
        _write(self.tmp / "a" / "b" / "c.bin", 100)
        _write(self.tmp / "a" / "d.bin", 50)
        self.assertEqual(mt.dir_size(self.tmp), 150)

    def test_missing_dir_is_zero(self):
        self.assertEqual(mt.dir_size(self.tmp / "nope"), 0)

    def test_symlink_is_not_followed(self):
        outside = self.tmp / "outside"
        _write(outside / "big.bin", 4096)
        inside = self.tmp / "inside"
        inside.mkdir()
        _write(inside / "small.bin", 10)
        os.symlink(outside, inside / "link")
        self.assertEqual(mt.dir_size(inside), 10)

    def test_within_rejects_targets_outside_the_root(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        self.assertFalse(mt._within(self.tmp / "inside", outside))
        self.assertFalse(mt._within(self.tmp, self.tmp))          # 根目录自己不算
        self.assertTrue(mt._within(self.tmp, self.tmp / "inside"))


class CacheTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsf-maint-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profile = make_profile(self.tmp, "11223344556", cache_mb=2)

    def test_cache_size_covers_the_usual_suspects(self):
        # Default/Cache(2KB) + Default/Code Cache(64) + ShaderCache(64) + .pma(64)
        self.assertEqual(mt.cache_size(self.profile), 2 * 1024 + 64 * 3)

    def test_login_state_is_not_cache(self):
        names = {str(p) for _, p in mt._iter_cache_targets(self.profile)}
        self.assertFalse([n for n in names if n.endswith("Cookies")])
        self.assertFalse([n for n in names if "Local Storage" in n])
        self.assertFalse([n for n in names if n.endswith("Local State")])

    def test_prune_caches_dry_run_touches_nothing(self):
        before = mt.dir_size(self.profile)
        result = mt.prune_caches(self.tmp, ids=["11223344556"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["freed_bytes"], 2 * 1024 + 64 * 3)
        self.assertEqual(mt.dir_size(self.profile), before, "预演不许动文件")

    def test_prune_caches_keeps_login_state(self):
        result = mt.prune_caches(self.tmp, ids=["11223344556"], apply=True)
        self.assertTrue(result["ok"])
        self.assertFalse((self.profile / "Default" / "Cache").exists())
        self.assertFalse((self.profile / "ShaderCache").exists())
        self.assertFalse((self.profile / "BrowserMetrics-spare.pma").exists())
        # ★ 这四样必须还在，否则账号要重新扫码
        self.assertTrue((self.profile / "Default" / "Cookies").exists())
        self.assertTrue((self.profile / "Default" / "Local Storage" / "leveldb" / "000003.log").exists())
        self.assertTrue((self.profile / "Local State").exists())

    def test_prune_caches_skips_a_running_browser(self):
        (self.profile / mt.SINGLETON_LOCK).write_text("")
        result = mt.prune_caches(self.tmp, ids=["11223344556"], apply=True)
        self.assertEqual(result["removed"], [])
        self.assertIn("11223344556", result["skipped_in_use"])
        self.assertTrue((self.profile / "Default" / "Cache").exists())


class PruneOrphanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsf-maint-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = make_profile(self.tmp, "11223344556", cache_mb=1)
        self.orphan = make_profile(self.tmp, "28619454585", cache_mb=1)
        self.scratch = make_profile(self.tmp, mt.SCRATCH_PROFILE_NAME, with_login=True)

    def _age(self, path: Path, days: float) -> None:
        when = time.time() - days * 86400
        os.utime(path, (when, when))

    def test_keeps_accounts_in_the_roster(self):
        result = mt.prune_orphans(self.tmp, ids=["11223344556"], apply=True, grace_days=0)
        self.assertTrue(self.live.is_dir(), "在册账号永远不能删")
        self.assertIn("11223344556", result["kept"])

    def test_keeps_the_scratch_profile(self):
        # 正在扫码的会话可能正用着它（登录成功才会搬到账号名下）
        result = mt.prune_orphans(self.tmp, ids=["11223344556"], apply=True, grace_days=0)
        self.assertTrue(self.scratch.is_dir(), "扫码用的临时资料目录不能删")
        self.assertIn(mt.SCRATCH_PROFILE_NAME, result["kept"])

    def test_fresh_orphan_is_protected_by_the_grace_period(self):
        result = mt.prune_orphans(self.tmp, ids=["11223344556"], grace_days=7, apply=True)
        self.assertTrue(self.orphan.is_dir(), "刚出现的目录可能是还没写进 TASKS 的新账号")
        self.assertIn("28619454585", result["skipped_recent"])

    def test_old_orphan_is_removed_only_when_applying(self):
        self._age(self.orphan, 30)
        preview = mt.prune_orphans(self.tmp, ids=["11223344556"], apply=False, grace_days=7)
        self.assertEqual([row["name"] for row in preview["removed"]], ["28619454585"])
        self.assertTrue(self.orphan.is_dir(), "预演不许动文件")
        self.assertTrue(preview["freed_bytes"] > 0)

        real = mt.prune_orphans(self.tmp, ids=["11223344556"], apply=True, grace_days=7)
        self.assertEqual(real["human_freed"], preview["human_freed"])
        self.assertFalse(self.orphan.exists())
        self.assertTrue(self.live.is_dir())

    def test_running_orphan_is_skipped(self):
        self._age(self.orphan, 30)
        (self.orphan / mt.SINGLETON_LOCK).write_text("")
        result = mt.prune_orphans(self.tmp, ids=["11223344556"], apply=True, grace_days=7)
        self.assertEqual(result["removed"], [])
        self.assertIn("28619454585", result["skipped_in_use"])
        self.assertTrue(self.orphan.is_dir())

    def test_symlinked_orphan_is_not_deleted_outside_the_root(self):
        outside = Path(tempfile.mkdtemp(prefix="dsf-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        _write(outside / "keepme.bin", 64)
        os.symlink(outside, self.tmp / "99999999999")
        result = mt.prune_orphans(self.tmp, ids=["11223344556"], apply=True, grace_days=0)
        self.assertTrue((outside / "keepme.bin").exists(), "符号链接指到外面也不能跟着删")
        self.assertIn("99999999999", result["skipped_symlink"])

    def test_unreadable_roster_deletes_nothing(self):
        self._age(self.orphan, 30)
        with mock.patch.object(mt, "known_ids", return_value=([], "账号名单读不到")):
            result = mt.prune_orphans(self.tmp, apply=True, grace_days=0)
        self.assertFalse(result["ok"])
        self.assertIn("账号名单", result["error"])
        self.assertTrue(self.orphan.is_dir(), "名单不可信时一个都不许删")
        self.assertEqual(result["removed"], [])

    def test_known_ids_reports_an_empty_roster_as_an_error(self):
        with mock.patch.dict(os.environ, {"TASKS": "[]"}, clear=False):
            with mock.patch("utils.config.userData", None):
                ids, error = mt.known_ids()
        self.assertEqual(ids, [])
        self.assertTrue(error, "空名单必须带错误说明，否则上层会当成'没有账号'")

    def test_known_ids_reads_the_real_roster(self):
        tasks = json.dumps([{"username": "甲", "unique_id": "11223344556",
                             "targets": ["乙"]}])
        env = {"TASKS": tasks, "COOKIES_11223344556": json.dumps([{"name": "sessionid"}])}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("utils.config.userData", None):
                ids, error = mt.known_ids()
        self.assertEqual(error, "")
        self.assertEqual(ids, ["11223344556"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsf-maint-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        make_profile(self.tmp, "11223344556", cache_mb=1)
        make_profile(self.tmp, "28619454585", cache_mb=1)

    def test_report_is_json_serialisable_and_counts_orphans(self):
        report = mt.profile_report(self.tmp, ids=["11223344556"])
        json.dumps(report, ensure_ascii=False)  # 面板要直接透出，必须能序列化
        self.assertEqual([row["name"] for row in report["profiles"]], ["11223344556", "28619454585"])
        names = {row["name"]: row for row in report["profiles"]}
        self.assertFalse(names["11223344556"]["is_orphan"])
        self.assertTrue(names["28619454585"]["is_orphan"])
        self.assertTrue(names["11223344556"]["in_use"] is False)
        self.assertEqual(report["orphan_bytes"], names["28619454585"]["bytes"])
        self.assertEqual(report["total_bytes"],
                         names["11223344556"]["bytes"] + names["28619454585"]["bytes"])

    def test_missing_root_is_reported_not_raised(self):
        report = mt.profile_report(self.tmp / "nope", ids=["11223344556"])
        self.assertFalse(report["exists"])
        self.assertEqual(report["profiles"], [])
        self.assertTrue(any("不存在" in line for line in mt.report_lines(report)))

    def test_lines_are_human_readable(self):
        lines = mt.report_lines(mt.profile_report(self.tmp, ids=["11223344556"]))
        text = "\n".join(lines)
        self.assertIn(self.tmp.name, text)
        self.assertIn("孤儿", text)

    def test_prune_lines_explain_a_refused_run(self):
        lines = mt.prune_lines({"ok": False, "error": "名单读不到"}, what="孤儿资料目录")
        self.assertIn("没动任何文件", "\n".join(lines))


class AutoPruneTests(unittest.TestCase):
    """定时器顺带做的那次维护（`maybe_auto_prune`）。

    这一层的价值全在"什么时候**不**动手"和"出错时不许把发送带崩"：

    * 没到点 → 一次状态文件读，什么都不做；
    * `MAINTENANCE_AUTO_PRUNE_HOURS=0` → 关闭；
    * 账号名单读不到 → 一个都不删，**且不推进计时器**（否则要等一周才再试），
      但也要有"至少隔 1 小时"的闸，免得每 5 分钟刷一行错误；
    * 清理本身炸了 → 吞掉、记进状态、返回 `reason="error"`，绝不抛。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsf-autoprune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "login-profile"
        self.root.mkdir()
        self.state = self.tmp / "maintenance_state.json"
        self.env = mock.patch.dict(os.environ, {"MAINTENANCE_AUTO_PRUNE_HOURS": "168"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 1_800_000_000.0

    def _run(self, **kwargs):
        kwargs.setdefault("root", self.root)
        kwargs.setdefault("state_file", self.state)
        kwargs.setdefault("now_ts", self.now)
        return mt.maybe_auto_prune(**kwargs)

    # -- 开关与间隔 ---------------------------------------------------------

    def test_default_interval_is_a_week_and_enabled(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAINTENANCE_AUTO_PRUNE_HOURS", None)
            self.assertEqual(mt.auto_prune_hours(), mt.DEFAULT_AUTO_PRUNE_HOURS)
            self.assertEqual(mt.DEFAULT_AUTO_PRUNE_HOURS, 168.0)
            self.assertTrue(mt.auto_prune_enabled())

    def test_zero_hours_switches_it_off(self):
        with mock.patch.dict(os.environ, {"MAINTENANCE_AUTO_PRUNE_HOURS": "0"}):
            self.assertFalse(mt.auto_prune_enabled())
            result = self._run()
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "disabled")
        self.assertFalse(self.state.exists(), "关掉时不该写状态文件")

    def test_garbage_and_extremes_fall_back_or_clamp(self):
        cases = {"": 168.0, "abc": 168.0, "-5": 168.0, "1e999": 168.0,
                 "0.5": 1.0, "8761": mt.MAX_AUTO_PRUNE_HOURS, "2": 2.0}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"MAINTENANCE_AUTO_PRUNE_HOURS": raw}):
                    self.assertEqual(mt.auto_prune_hours(), want)

    def test_first_run_prunes_caches_and_old_orphans(self):
        make_profile(self.root, "11223344556", cache_mb=512)
        make_profile(self.root, "old-orphan", cache_mb=256)
        old = self.now - 30 * 86400
        os.utime(self.root / "old-orphan", (old, old))
        result = self._run(ids=["11223344556"])
        self.assertTrue(result["ran"])
        self.assertEqual(result["reason"], "ok")
        self.assertGreater(result["freed_bytes"], 512 * 1024)
        self.assertFalse((self.root / "old-orphan").exists(), "过宽限期的孤儿该被删")
        self.assertTrue((self.root / "11223344556").exists(), "在册账号的目录永远留着")
        self.assertTrue((self.root / "11223344556" / "Default" / "Cookies").exists(),
                        "登录态一个字节都不能动")
        self.assertFalse((self.root / "11223344556" / "Default" / "Cache").exists(),
                         "可再生缓存该被清掉")
        state = mt.load_state(self.state)
        self.assertEqual(state["last_auto_prune"], self.now)
        self.assertEqual(state["runs"], 1)
        self.assertEqual(state["last_error"], "")

    def test_within_the_interval_it_does_nothing(self):
        make_profile(self.root, "11223344556", cache_mb=64)
        self._run(ids=["11223344556"])
        make_profile(self.root, "11223344556", cache_mb=64)  # 缓存又长回来了
        again = self._run(ids=["11223344556"], now_ts=self.now + 3600)
        self.assertFalse(again["ran"])
        self.assertEqual(again["reason"], "not-due")
        self.assertTrue((self.root / "11223344556" / "Default" / "Cache").exists(),
                        "没到点不许动手")

    def test_after_the_interval_it_runs_again(self):
        make_profile(self.root, "11223344556", cache_mb=64)
        self._run(ids=["11223344556"])
        make_profile(self.root, "11223344556", cache_mb=64)
        later = self._run(ids=["11223344556"], now_ts=self.now + 168 * 3600 + 60)
        self.assertTrue(later["ran"])
        self.assertEqual(mt.load_state(self.state)["runs"], 2)

    def test_force_ignores_both_the_switch_and_the_interval(self):
        with mock.patch.dict(os.environ, {"MAINTENANCE_AUTO_PRUNE_HOURS": "0"}):
            make_profile(self.root, "11223344556", cache_mb=64)
            result = self._run(ids=["11223344556"], force=True)
        self.assertTrue(result["ran"])
        self.assertTrue(result["reason"] in ("ok", "error"))

    # -- 失败路径 -----------------------------------------------------------

    def test_unreadable_roster_prunes_nothing_and_does_not_advance(self):
        make_profile(self.root, "11223344556", cache_mb=64)
        with mock.patch.object(mt, "known_ids",
                               return_value=([], "读不到账号名单（RuntimeError: boom）")):
            result = self._run()
        self.assertTrue(result["ran"])
        self.assertEqual(result["reason"], "error")
        self.assertIn("读不到账号名单", result["error"])
        self.assertTrue((self.root / "11223344556" / "Default" / "Cache").exists(),
                        "名单不可信时一个字节都不许删")
        state = mt.load_state(self.state)
        self.assertNotIn("last_auto_prune", state, "失败不许推进计时器")
        self.assertIn("读不到账号名单", state["last_error"])
        # 1 小时之内不再重试（否则每 5 分钟一行错误）
        again = self._run(now_ts=self.now + 60)
        self.assertFalse(again["ran"])
        self.assertEqual(again["reason"], "retry-wait")
        # 过了重试闸就再试一次
        third = self._run(now_ts=self.now + mt.MIN_AUTO_PRUNE_RETRY_SECONDS + 1)
        self.assertTrue(third["ran"])

    def test_a_crash_inside_the_prune_is_swallowed(self):
        with mock.patch.object(mt, "prune_caches", side_effect=RuntimeError("boom")):
            result = self._run(ids=["11223344556"])
        # `ran=True` + `reason="error"`：调用方要把它打进日志，不许静默失败。
        self.assertTrue(result["ran"])
        self.assertEqual(result["reason"], "error")
        self.assertIn("boom", result["error"])
        self.assertIn("boom", mt.load_state(self.state)["last_error"])

    def test_broken_state_file_is_treated_as_never_pruned(self):
        self.state.write_text("{ 这不是 JSON", encoding="utf-8")
        make_profile(self.root, "11223344556", cache_mb=64)
        self.assertEqual(mt.load_state(self.state), {})
        result = self._run(ids=["11223344556"])
        self.assertTrue(result["ran"])
        self.assertEqual(mt.load_state(self.state)["runs"], 1)

    def test_state_is_written_atomically(self):
        make_profile(self.root, "11223344556", cache_mb=64)
        self._run(ids=["11223344556"])
        leftovers = list(self.tmp.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"留下了临时文件：{leftovers}")

    # -- 面板/CLI 看的那行状态 ----------------------------------------------

    def test_status_before_and_after_the_first_run(self):
        before = mt.auto_prune_status(now_ts=self.now, state_file=self.state)
        self.assertTrue(before["enabled"])
        self.assertEqual(before["hours"], 168.0)
        self.assertEqual(before["last_ago_text"], "还没清过")
        self.assertIn("下一次定时检查", before["next_text"])
        make_profile(self.root, "11223344556", cache_mb=64)
        self._run(ids=["11223344556"])
        after = mt.auto_prune_status(now_ts=self.now + 86400, state_file=self.state)
        self.assertEqual(after["last_ago_text"], "1 天前")
        self.assertIn("后", after["next_text"])

    def test_status_says_off_when_disabled(self):
        with mock.patch.dict(os.environ, {"MAINTENANCE_AUTO_PRUNE_HOURS": "0"}):
            status = mt.auto_prune_status(now_ts=self.now, state_file=self.state)
        self.assertFalse(status["enabled"])
        self.assertIn("关闭", status["next_text"])

    def test_human_duration(self):
        self.assertEqual(mt.human_duration(0), "0 分钟")
        self.assertEqual(mt.human_duration(None), "0 分钟")
        self.assertEqual(mt.human_duration(30), "不到 1 分钟")
        self.assertEqual(mt.human_duration(120), "2 分钟")
        self.assertEqual(mt.human_duration(7200), "2 小时")
        self.assertEqual(mt.human_duration(3 * 86400), "3 天")


class ProfileRootTests(unittest.TestCase):
    """资料根目录的口径：`LOGIN_PROFILE_DIR` 优先，否则**按仓库根**解析。"""

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"LOGIN_PROFILE_DIR": "/tmp/whatever"}):
            self.assertEqual(mt.profile_root(), Path("/tmp/whatever"))

    def test_default_is_anchored_to_the_repo_root(self):
        repo = Path(mt.__file__).resolve().parents[1]
        cwd = os.getcwd()
        os.chdir(tempfile.gettempdir())
        try:
            os.environ.pop("LOGIN_PROFILE_DIR", None)
            self.assertEqual(mt.profile_root(), repo / "logs" / "login-profile")
            self.assertEqual(mt.state_path(), repo / "logs" / mt.STATE_FILE_NAME)
        finally:
            os.chdir(cwd)
class SaveStateConcurrencyTests(unittest.TestCase):
    """状态文件原子写：临时文件名必须唯一（GitHub issue #8）。"""

    def test_并发写不会留下半截_JSON(self) -> None:
        """★ 回归：以前临时文件名是固定的 `target + ".tmp"`，两个写者
        （定时器的自动清理 + 面板的按钮）会同时用 "w" 打开同一个 inode，
        一个把另一个截断，`os.replace` 搬走的可能是半截 JSON ——
        下次 load_state 解析失败按「没清过」处理，计时全部重置。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "maintenance_state.json")
            errors = []

            def writer(n):
                try:
                    for i in range(40):
                        mt.save_state({"writer": n, "i": i}, path)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(n,))
                       for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)          # 半截 JSON 会在这里抛
            self.assertIn("writer", data)
            leftovers = [n for n in os.listdir(tmp)
                         if n != "maintenance_state.json"]
            self.assertEqual(leftovers, [])       # 临时文件都搬走了


if __name__ == "__main__":
    unittest.main()
