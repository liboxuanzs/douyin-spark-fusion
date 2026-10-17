"""`tools/record_har.py` 的文件名净化与落盘收敛检查。

为什么专门测这个：录出来的 HAR **含登录 cookie**，而用户名是抖音昵称
（完全不受控的输入）。净化漏一个字符就有两种后果 ——
含 `/` 的名字让工具直接不可用；含 `..` 的名字把凭据文件写到 har_logs/ 之外。
所以这里既测"洗得干净"，也测"落盘前那道锁真的会拦"。

⚠️ 本文件不启动浏览器：`record()` 用打桩的假浏览器跑，
   只关心它算出来的路径，不会真的开 Chromium、也不会连网。
"""
import os
import tempfile
import unittest
from unittest import mock

from tools import record_har as record_har


def _resolved_inside(har_path, root):
    """把路径归一后判断是否落在 root 里（与实现同样的判据）。"""
    return os.path.commonpath(
        [os.path.realpath(root), os.path.realpath(har_path)]
    ) == os.path.realpath(root)


class SafeNameTests(unittest.TestCase):
    """`_safe_name`：产出永远只能是"名字片段"，不能带路径语义。"""

    def test_ordinary_and_cjk_names_are_kept(self):
        """中文昵称必须原样保留 —— 用户的抖音号很可能就是中文。"""
        for name in ("小明", "普通昵称", "示例号_2026", "盖瑞·火花", "abc-123.def"):
            with self.subTest(name=name):
                self.assertEqual(record_har._safe_name(name), name)

    def test_path_separators_are_removed(self):
        self.assertEqual(record_har._safe_name("a/b"), "ab")
        self.assertEqual(record_har._safe_name("a\\b"), "ab")

    def test_unicode_lookalike_separators_are_removed(self):
        """全角 ／ 与同类字符也不能活下来（NFKC 之后仍须过滤）。"""
        for bad in ("／", "∕", "⁄", "⧵", "⧸", "⧹", "﹨", "＼"):
            with self.subTest(ch=bad):
                self.assertNotIn(
                    bad, record_har._safe_name(f"a{bad}b"),
                    f"{bad!r} 活下来了，规范化后它可能就是路径分隔符",
                )

    def test_dot_dot_cannot_survive(self):
        """`..` 一旦活下来，带 cookie 的 HAR 就能被写到 har_logs/ 之外。"""
        for raw in ("..", "...", "....", "../../etc/passwd", "a/../../b",
                    "小明/../../x.har", ".  .", "..\\..\\.."):
            with self.subTest(raw=raw):
                out = record_har._safe_name(raw)
                self.assertNotIn("..", out, f"{raw!r} → {out!r} 里还留着 `..`")
                self.assertTrue(out, "净化结果不能是空串（应有兜底名）")
                self.assertNotIn("/", out)
                self.assertNotIn("\\", out)

    def test_result_never_escapes_when_joined(self):
        """最终判据：拿净化结果拼进 har_logs，落点必须还在 har_logs 里。"""
        for raw in ("../../x", "..", "a/../..", "....//....//etc/passwd", "／..／.."):
            with self.subTest(raw=raw):
                joined = os.path.join(record_har.HAR_LOG_DIR,
                                      record_har._safe_name(raw) + ".har")
                self.assertTrue(
                    _resolved_inside(joined, record_har.HAR_LOG_DIR),
                    f"{raw!r} 拼出来跑到 har_logs 外面去了：{joined}",
                )

    def test_empty_or_whitespace_falls_back(self):
        for raw in ("", "   ", ".", "..", "...", "\t\n", None, "／／", "．"):
            with self.subTest(raw=raw):
                self.assertEqual(record_har._safe_name(raw), record_har._FALLBACK_NAME)

    def test_leading_trailing_space_and_dots_are_trimmed(self):
        """首尾空白在 ls 里看不出来；末尾的点会被 Windows 悄悄吃掉。"""
        self.assertEqual(record_har._safe_name("  小明  "), "小明")
        self.assertEqual(record_har._safe_name("...hidden..."), "hidden")
        self.assertEqual(record_har._safe_name(".ssh"), "ssh")

    def test_windows_illegal_chars_are_removed(self):
        out = record_har._safe_name('a<b>c:d"e|f?g*h')
        self.assertEqual(out, "abcdefgh")
        for ch in record_har._WINDOWS_ILLEGAL:
            self.assertNotIn(ch, out)

    def test_control_chars_are_removed(self):
        """控制字符会让 `ls` 输出错乱，也是文件名里最容易被忽略的一类。"""
        self.assertEqual(record_har._safe_name("a\tb\nc\rd"), "abcd")
        self.assertEqual(record_har._safe_name("a\x00b\x1fc\x7fd"), "abcd")
        self.assertEqual(record_har._safe_name("a\u200bb"), "ab",
                         "零宽字符属于 Cf，也该收掉")

    def test_fullwidth_is_normalized(self):
        """全角字母先 NFKC 收敛成 ASCII，免得同一个名字生成两个不同文件。"""
        self.assertEqual(record_har._safe_name("ｘｙｚ"), "xyz")

    def test_overlong_name_is_clamped(self):
        out = record_har._safe_name("あ" * 500)
        self.assertLessEqual(len(out), record_har._MAX_NAME_LEN)
        self.assertTrue(out)

    def test_clamp_does_not_leave_trailing_dot(self):
        out = record_har._safe_name("a" * record_har._MAX_NAME_LEN + "." * 5)
        self.assertFalse(out.endswith("."), "截断后不能留下尾点")
        self.assertNotEqual(out, "")

    def test_fallback_name_itself_is_safe(self):
        """兜底名自己也得经得起同一套判据（免得改了兜底名反而开了口子）。"""
        self.assertEqual(record_har._safe_name(record_har._FALLBACK_NAME),
                         record_har._FALLBACK_NAME)


class HarPathInsideTests(unittest.TestCase):
    """`_assert_har_path_inside`：落盘前的第二道锁。"""

    def test_path_inside_is_allowed(self):
        record_har._assert_har_path_inside(
            os.path.join(record_har.HAR_LOG_DIR, "x.har"), record_har.HAR_LOG_DIR)

    def test_dot_dot_escape_is_blocked(self):
        with self.assertRaises(ValueError):
            record_har._assert_har_path_inside(
                os.path.join(record_har.HAR_LOG_DIR, "..", "secret.har"),
                record_har.HAR_LOG_DIR)

    def test_sibling_with_same_prefix_is_blocked(self):
        """`/x/har_logs_backup` 不在 `/x/har_logs` 里 —— 别看前缀像就放行。

        这正是实现里用 `commonpath` 而不是 `str.startswith` 的原因。
        """
        with self.assertRaises(ValueError):
            record_har._assert_har_path_inside(
                os.path.join(record_har.HAR_LOG_DIR + "_backup", "x.har"),
                record_har.HAR_LOG_DIR)

    def test_relative_path_inside_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                record_har._assert_har_path_inside("logs/x.har", "logs")
            finally:
                os.chdir(cwd)

    def test_relative_escape_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(ValueError):
                    record_har._assert_har_path_inside("../x.har", "logs")
            finally:
                os.chdir(cwd)


class RecordPathTests(unittest.TestCase):
    """`record()` 里算出来的最终路径（浏览器打桩，不开 Chromium）。"""

    class _FakePage:
        def goto(self, url):
            self.url = url

        def wait_for_timeout(self, ms):
            self.ms = ms

    class _FakeContext:
        def __init__(self):
            self.kwargs = None
            self.closed = False

        def new_page(self):
            return RecordPathTests._FakePage()

        def add_cookies(self, cookies):
            self.cookies = cookies

        def set_default_navigation_timeout(self, value):
            pass

        def set_default_timeout(self, value):
            pass

        def close(self):
            self.closed = True

    class _FakeBrowser:
        def __init__(self):
            self.context = RecordPathTests._FakeContext()
            self.closed = False

        def new_context(self, **kwargs):
            self.context.kwargs = kwargs
            return self.context

        def close(self):
            self.closed = True

    def _record(self, user, out_path=None):
        """只跑 `record()` 的路径计算 + 落盘调用，其余全打桩。"""
        browser = self._FakeBrowser()
        with mock.patch.object(record_har, "get_browser", return_value=browser), \
                mock.patch.object(record_har, "get_config",
                                  return_value={"browserActionTimeout": 1000}), \
                mock.patch.object(record_har, "logger", mock.Mock()):
            path = record_har.record(user, 0, out_path)
        return path, browser

    def test_evil_username_lands_inside_har_logs(self):
        """★ 回归：`../../etc/evil` 这种昵称不能把 HAR 写到 har_logs 外面。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(record_har, "HAR_LOG_DIR", tmp):
                path, browser = self._record(
                    {"username": "../../etc/evil", "cookies": []})
                self.assertTrue(
                    _resolved_inside(path, tmp),
                    f"{path} 跑到 har_logs 外面去了")
                self.assertNotIn("..", os.path.basename(path))
                self.assertEqual(
                    os.path.dirname(
                        os.path.realpath(browser.context.kwargs["record_har_path"])),
                    os.path.dirname(os.path.realpath(path)))
            self.assertEqual(os.listdir(tmp), [], "临时目录里不该留下半成品")

    def test_slash_username_is_usable(self):
        """含 `/` 的昵称以前会让工具直接崩（把文件名当成了一层目录）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(record_har, "HAR_LOG_DIR", tmp):
                path, _ = self._record({"username": "a/b", "cookies": []})
            self.assertTrue(path.startswith(tmp), path)
            self.assertNotIn("/", os.path.basename(path))

    def test_empty_username_uses_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(record_har, "HAR_LOG_DIR", tmp):
                path, _ = self._record({"username": "..", "cookies": []})
            self.assertTrue(
                os.path.basename(path).startswith(record_har._FALLBACK_NAME + "_"),
                os.path.basename(path))

    def test_rename_is_logged_for_reconciliation(self):
        """改写必须留痕：事后要靠日志对账"这个文件是谁的"。"""
        browser = self._FakeBrowser()
        fake_logger = mock.Mock()
        with mock.patch.object(record_har, "get_browser", return_value=browser), \
                mock.patch.object(record_har, "get_config",
                                  return_value={"browserActionTimeout": 1000}), \
                mock.patch.object(record_har, "logger", fake_logger), \
                mock.patch.object(record_har, "HAR_LOG_DIR", tempfile.mkdtemp()):
            record_har.record({"username": "a/b", "cookies": []}, 0)
        self.assertTrue(fake_logger.info.called, "净化改名时没有任何日志")
        self.assertIn("ab", str(fake_logger.info.call_args))

    def test_normal_username_is_not_logged(self):
        """名字本来就安全时别刷日志（正常路径噪音最少）。"""
        browser = self._FakeBrowser()
        fake_logger = mock.Mock()
        with mock.patch.object(record_har, "get_browser", return_value=browser), \
                mock.patch.object(record_har, "get_config",
                                  return_value={"browserActionTimeout": 1000}), \
                mock.patch.object(record_har, "logger", fake_logger), \
                mock.patch.object(record_har, "HAR_LOG_DIR", tempfile.mkdtemp()):
            record_har.record({"username": "小明", "cookies": []}, 0)
        self.assertFalse(fake_logger.info.called)

    def test_out_path_is_used_verbatim_when_it_stays_in_its_directory(self):
        """调用方点名要写哪里就写哪里，不做净化、不改名（否则等于偷偷换落点）。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "sub", "mine.har")
            with mock.patch.object(record_har, "HAR_LOG_DIR", tmp):
                path, browser = self._record({"username": "小明", "cookies": []},
                                             out_path=target)
            self.assertEqual(path, target)
            self.assertEqual(browser.context.kwargs["record_har_path"], target)

    def test_out_path_escaping_the_named_directory_is_blocked(self):
        """基准目录与真实落点分属两地时必须拦下（这里借 symlink 构造）。

        这与 `--out ../x.har` 不同：那种写法的基准目录本身就是 `..`，
        "落点在基准里"成立，属于调用方明确指定 —— 见实现里的注释。
        """
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            inner = os.path.join(tmp, "inner")
            os.makedirs(inner)
            link = os.path.join(inner, "link")        # inner/link → ../outside
            os.symlink(outside, link)
            with mock.patch.object(record_har, "HAR_LOG_DIR", tmp):
                # 基准 = inner/link（真实落点是 outside），文件落点也在 outside
                # → 落点确实在基准里（realpath 之后两边都归一了），允许。
                self._record({"username": "小明", "cookies": []},
                             out_path=os.path.join(link, "ok.har"))
                # 而这里基准是 tmp 下的另一个目录，落点却在 outside → 必须拦
                with self.assertRaises(ValueError):
                    record_har._assert_har_path_inside(
                        os.path.join(outside, "x.har"), inner)

    def test_out_path_inside_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "sub", "mine.har")
            with mock.patch.object(record_har, "HAR_LOG_DIR", tmp):
                path, _ = self._record({"username": "小明", "cookies": []},
                                       out_path=target)
            self.assertEqual(path, target)


if __name__ == "__main__":
    unittest.main()
