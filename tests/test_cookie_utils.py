"""`core.cookie_utils` 的纯函数测试。

这一层是 configTool 与面板扫码登录共用的唯一实现，所以边界行为必须钉死：
最要紧的两条是
  * 空 name 的 cookie 必须丢掉（抖音上真有一条，留着会让 Playwright 整批 add_cookies 失败）；
  * 写 .env 用的 JSON 必须是 escaped 的（utils/config.py 读的时候要过 unicode_escape）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import cookie_utils as cu  # noqa: E402

PNG_1PX = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
).decode()


def cookie(name="sessionid", value="v", domain=".douyin.com", **extra):
    item = {"name": name, "value": value, "domain": domain}
    item.update(extra)
    return item


class CleanCookieTests(unittest.TestCase):
    def test_empty_name_is_dropped(self):
        # 抖音在未登录时就会下发一条 name="" 的 cookie（value 形如 douyin.com）
        self.assertIsNone(cu.clean_cookie(cookie(name="", value="douyin.com")))

    def test_missing_domain_is_dropped(self):
        self.assertIsNone(cu.clean_cookie(cookie(domain="")))

    def test_keeps_only_the_fields_playwright_needs(self):
        got = cu.clean_cookie(
            cookie(
                path="/passport",
                expires=1730000000,
                httpOnly=True,
                secure=False,
                sameSite="Lax",
                session=True,
                size=42,
            )
        )
        self.assertEqual(
            sorted(got.keys()),
            ["domain", "expires", "httpOnly", "name", "path", "secure", "value"],
            "只保留 add_cookies 认的字段（sameSite 由 utils/config.py 再剔一次，这里先不要）",
        )
        self.assertEqual(got["path"], "/passport")
        self.assertEqual(got["expires"], 1730000000)
        self.assertIs(got["httpOnly"], True)
        self.assertIs(got["secure"], False)

    def test_defaults(self):
        got = cu.clean_cookie(cookie())
        self.assertEqual(got["path"], "/")
        self.assertEqual(got["expires"], -1)
        self.assertIs(got["httpOnly"], False)
        self.assertIs(got["secure"], False)

    def test_garbage_input(self):
        self.assertIsNone(cu.clean_cookie(None))
        self.assertIsNone(cu.clean_cookie("sessionid=1"))
        self.assertIsNone(cu.clean_cookie({}))


class MatchesTargetTests(unittest.TestCase):
    def test_hits(self):
        for domain in ("douyin.com", ".douyin.com", "www.douyin.com", ".www.douyin.com"):
            with self.subTest(domain=domain):
                self.assertTrue(cu.matches_target(domain))

    def test_other_family_domains(self):
        for domain in (".bytedance.com", ".snssdk.com", ".iesdouyin.com", ".amemv.com"):
            with self.subTest(domain=domain):
                self.assertTrue(cu.matches_target(domain))

    def test_misses(self):
        for domain in ("", None, "example.com", "notdouyin.com", "douyin.com.evil.com"):
            with self.subTest(domain=domain):
                self.assertFalse(cu.matches_target(domain))


class FilterCookiesTests(unittest.TestCase):
    def test_keeps_target_domain_cookies(self):
        raw = [cookie(name="sessionid"), cookie(name="ttwid"), cookie(domain=".example.com")]
        cookies, stats = cu.filter_cookies(raw)
        self.assertEqual([c["name"] for c in cookies], ["sessionid", "ttwid"])
        self.assertEqual(stats["raw"], 3)
        self.assertEqual(stats["matched"], 2)
        self.assertEqual(stats["invalid"], 0)
        self.assertFalse(stats["fell_back"])

    def test_empty_name_is_counted_as_invalid(self):
        raw = [cookie(name=""), cookie(name="sessionid")]
        cookies, stats = cu.filter_cookies(raw)
        self.assertEqual([c["name"] for c in cookies], ["sessionid"])
        self.assertEqual(stats["invalid"], 1)

    def test_falls_back_to_everything_when_domains_look_wrong(self):
        # 目标域一条都没命中 → 宁可多带，也不能存下一份没用的 cookie
        raw = [cookie(name="sessionid", domain=".example.com")] + [
            cookie(name=f"x{i}", domain=".other.com") for i in range(4)
        ]
        cookies, stats = cu.filter_cookies(raw)
        self.assertTrue(stats["fell_back"])
        self.assertEqual(len(cookies), 5)

    def test_domains_only_false_disables_the_filter(self):
        raw = [cookie(domain=".example.com"), cookie(name="ttwid")]
        cookies, stats = cu.filter_cookies(raw, domains_only=False)
        self.assertEqual(len(cookies), 2)
        self.assertFalse(stats["fell_back"])

    def test_empty_input(self):
        self.assertEqual(cu.filter_cookies([])[0], [])
        self.assertTrue(cu.filter_cookies([])[1]["fell_back"], "一条都没有时也该走兜底")


class SerializationTests(unittest.TestCase):
    def test_escaped_round_trip_through_unicode_escape(self):
        # 复刻 utils/config.py 的读取方式：.encode().decode("unicode_escape") 再 json.loads
        raw = [cookie(value="中文值-ü"), cookie(name="ttwid", value="ok")]
        text = cu.cookies_to_json(raw, escaped=True)
        self.assertNotIn("中文值", text, "escaped 形态里不该有字面非 ASCII")
        decoded = json.loads(text.encode("utf-8").decode("unicode_escape"))
        self.assertEqual(decoded, raw)

    def test_unescaped_is_readable_but_lossy_for_env(self):
        raw = [cookie(value="中文值")]
        text = cu.cookies_to_json(raw, escaped=False)
        self.assertIn("中文值", text)
        decoded = json.loads(text.encode("utf-8").decode("unicode_escape"))
        self.assertNotEqual(decoded, raw, "未转义的写法过不了 .env 那道解码（所以必须 escaped=True）")

    def test_single_line(self):
        self.assertNotIn("\n", cu.cookies_to_json([cookie(), cookie(name="ttwid")]))


class ParseCookiesTests(unittest.TestCase):
    def test_accepts_json_array(self):
        got, error = cu.parse_cookies('[{"name":"sessionid","value":"1"}]')
        self.assertEqual(error, "")
        self.assertEqual(got, [{"name": "sessionid", "value": "1"}])

    def test_rejects_raw_header_form(self):
        got, error = cu.parse_cookies("sessionid=1; ttwid=2")
        self.assertIsNone(got)
        self.assertIn("JSON", error)

    def test_rejects_object_and_empty(self):
        self.assertIsNone(cu.parse_cookies('{"name":"sessionid"}')[0])
        self.assertIsNone(cu.parse_cookies("")[0])
        self.assertEqual(cu.parse_cookies("  ")[1], "Cookies 为空")


class SmallHelpersTests(unittest.TestCase):
    def test_has_login_cookie(self):
        self.assertTrue(cu.has_login_cookie([cookie()]))
        self.assertTrue(cu.has_login_cookie([cookie(name="sid_tt")]))
        self.assertFalse(cu.has_login_cookie([cookie(name="ttwid")]))
        self.assertFalse(cu.has_login_cookie([]))

    def test_cookie_names_dedupes_and_keeps_order(self):
        names = cu.cookie_names([cookie(name="ttwid"), cookie(name="sessionid"), cookie(name="ttwid")])
        self.assertEqual(names, ["ttwid", "sessionid"])

    def test_cookie_names_tolerates_dirty_input(self):
        self.assertEqual(cu.cookie_names([None, {"name": ""}, {"name": "a"}]), ["a"])

    def test_data_uri_bytes(self):
        got = cu.data_uri_bytes(f"data:image/png;base64,{PNG_1PX}")
        self.assertIsNotNone(got)
        self.assertTrue(got.startswith(b"\x89PNG"))

    def test_data_uri_bytes_rejects_other_forms(self):
        self.assertIsNone(cu.data_uri_bytes("https://example.com/a.png"))
        self.assertIsNone(cu.data_uri_bytes("data:image/png,notbase64"))
        self.assertIsNone(cu.data_uri_bytes("data:image/png;base64,"))
        self.assertIsNone(cu.data_uri_bytes(""))

    def test_env_key_for(self):
        self.assertEqual(cu.env_key_for("abc123"), "COOKIES_ABC123")
        self.assertEqual(cu.env_key_for("  a.b-c_1  "), "COOKIES_A.B-C_1")

    def test_env_key_for_rejects_bad_ids(self):
        for bad in ("", "  ", "有中文", "a b", "a=b", "x" * 65):
            with self.subTest(unique_id=bad):
                with self.assertRaises(ValueError):
                    cu.env_key_for(bad)


class ExtractSelfProfileTests(unittest.TestCase):
    def test_reads_nickname_and_unique_id(self):
        got = cu.extract_self_profile(
            {
                "status_code": 0,
                "status_msg": None,
                "user": {
                    "nickname": "示例昵称",
                    "unique_id": "12345678901",
                    "short_id": "987654321",
                    "uid": "10000000000000001",
                    "sec_uid": "MS4wLjABAAAA",
                },
            }
        )
        self.assertEqual(got["nickname"], "示例昵称")
        self.assertEqual(got["unique_id"], "12345678901")
        self.assertEqual(got["id_source"], "unique_id")
        self.assertEqual(got["uid"], "10000000000000001")
        self.assertEqual(got["sec_uid"], "MS4wLjABAAAA")
        self.assertEqual(got["status_code"], 0)

    def test_falls_back_to_short_id(self):
        got = cu.extract_self_profile({"user": {"nickname": "甲", "short_id": "555"}})
        self.assertEqual(got["unique_id"], "555")
        self.assertIn("short_id", got["id_source"])
        self.assertIn("未设自定义抖音号", got["id_source"])

    def test_empty_unique_id_falls_back_but_blank_does_not(self):
        got = cu.extract_self_profile({"user": {"unique_id": "   ", "short_id": "777"}})
        self.assertEqual(got["unique_id"], "777")

    def test_other_nickname_is_used_when_nickname_missing(self):
        self.assertEqual(cu.extract_self_profile({"user": {"other_nickname": "乙"}})["nickname"], "乙")

    def test_logged_out_payload(self):
        got = cu.extract_self_profile({"status_code": 8, "status_msg": "用户未登录"})
        self.assertEqual(got["unique_id"], "")
        self.assertEqual(got["status_code"], 8)
        self.assertEqual(got["status_msg"], "用户未登录")

    def test_garbage_input(self):
        for bad in (None, "x", [], {}):
            with self.subTest(bad=bad):
                got = cu.extract_self_profile(bad)
                self.assertEqual(got["unique_id"], "")
                self.assertEqual(got["nickname"], "")

    def test_page_hooks_are_strings_with_the_expected_markers(self):
        self.assertIn("/user/profile/self", cu.INTERCEPT_JS)
        self.assertIn("__dySelfHook", cu.INTERCEPT_JS)
        self.assertIn("XMLHttpRequest", cu.INTERCEPT_JS)
        # localStorage 兜底是一段独立的求值表达式（不写钩子，只读 key）
        self.assertIn("localStorage", cu.LOCAL_STORAGE_JS)
        self.assertIn("user|profile|account|login", cu.LOCAL_STORAGE_JS)


class PurityTests(unittest.TestCase):
    """这一层要能被面板进程与 configTool 同时 import，不能拖进浏览器依赖。"""

    def test_import_pulls_nothing_heavy(self):
        code = (
            "import sys; import core.cookie_utils; "
            "bad=[m for m in ('playwright','cloakbrowser','tkinter','fastapi') if m in sys.modules]; "
            "print(','.join(bad))"
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "", f"不该 import：{done.stdout.strip()}")


if __name__ == "__main__":
    unittest.main()
