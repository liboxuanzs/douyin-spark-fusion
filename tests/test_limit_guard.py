"""core.limit_guard 的单测：关键词命中、两个来源的优先级、冷却状态机。

不碰浏览器、不碰网络，全部用临时文件与注入的时间。
"""
import json
import os
import tempfile
import unittest
from datetime import timedelta

from core import limit_guard as lg
from core.util_time import iso, now


class DetectTextTests(unittest.TestCase):
    def test_rate_limit_keywords(self):
        for kw in ("操作频繁", "请稍后重试", "发送过快"):
            hit = lg.detect_texts([f"发送失败，{kw}"])
            self.assertIsNotNone(hit, kw)
            self.assertEqual(hit["category"], "rate_limit")
            self.assertEqual(hit["source"], "dom")

    def test_captcha_keywords(self):
        hit = lg.detect_texts(["请完成安全验证后继续"])
        self.assertEqual(hit["category"], "captcha")

    def test_account_risk_keywords(self):
        hit = lg.detect_texts(["您的账号存在风险"])
        self.assertEqual(hit["category"], "account_risk")

    def test_english_keywords_case_insensitive(self):
        self.assertEqual(lg.detect_texts(["Too Many Requests"])["keyword"], "too many requests")
        self.assertEqual(lg.detect_texts(["CAPTCHA required"])["category"], "captcha")

    def test_clean_text_is_not_a_hit(self):
        # 聊天内容里出现"验证""频繁"这类词太正常，没命中关键词就不能熔断
        self.assertIsNone(lg.detect_texts(["今天聊得很频繁呀", "晚安"]))
        self.assertIsNone(lg.detect_texts([]))
        self.assertIsNone(lg.detect_texts(None))

    def test_long_text_is_truncated_in_evidence(self):
        hit = lg.detect_texts(["操作频繁" + "x" * 500])
        self.assertLessEqual(len(hit["text"]), 200)


class DetectReceiptTests(unittest.TestCase):
    def test_status_keyword_hits(self):
        hit = lg.detect_receipt({"code": 1001, "status": "操作频繁，请稍后再试",
                                 "message_id": None})
        self.assertEqual(hit["source"], "http")
        self.assertEqual(hit["category"], "rate_limit")
        self.assertIn("code=1001", hit["text"])

    def test_plain_failure_is_not_a_hit(self):
        # 普通失败（网络抖动/非风控错误码）不能升级成熔断，否则会白停一天
        self.assertIsNone(lg.detect_receipt({"code": 1001, "status": "internal error"}))
        self.assertIsNone(lg.detect_receipt({"code": None, "status": None}))
        self.assertIsNone(lg.detect_receipt(None))

    def test_code_8_is_not_rate_limit(self):
        """code==8 是抖音官方的「掉登录」码（见 douyin_im._watch_login_lost）。

        把它当限流会让"登录失效"被误报成"被限流" —— 这里钉住这个边界。
        """
        self.assertIsNone(lg.detect_receipt({"code": 8, "status": "ok"}))
        self.assertIsNone(lg.detect_receipt({"code": 8, "status": ""}))

    def test_receipt_wins_over_dom(self):
        hit = lg.detect(receipts=[{"code": 0, "status": "操作频繁"}],
                        dom_texts=["请完成安全验证"])
        self.assertEqual(hit["source"], "http")

    def test_dom_used_when_receipt_clean(self):
        hit = lg.detect(receipts=[{"code": 0, "status": "ok"}],
                        dom_texts=["请完成安全验证"])
        self.assertEqual(hit["source"], "dom")
        self.assertIsNone(lg.detect(receipts=[{"code": 0, "status": "ok"}], dom_texts=[]))


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "rate_limit.json")

    def test_no_file_means_no_cooldown(self):
        g = lg.LimitGuard(path=self.path)
        self.assertIsNone(g.read())
        self.assertIsNone(g.in_cooldown())
        self.assertFalse(g.status()["tripped"])

    def test_trip_then_cooldown_then_expire(self):
        g = lg.LimitGuard(path=self.path, cooldown_seconds=1800)
        # 冷却按整秒计（`trip` 会把微秒归零，因为落盘的时间戳是秒级精度），
        # 所以基准时间也取整秒，否则断言会随"这一秒里的第几微秒"漂移。
        t0 = now().replace(microsecond=0)
        rec = g.trip({"keyword": "操作频繁", "category": "rate_limit",
                      "source": "dom", "text": "操作频繁"}, target="甲同学",
                     run_id="20261001-093000", at=t0)
        self.assertEqual(rec["cooldown_until"], iso(t0 + timedelta(seconds=1800)))

        active = g.in_cooldown(at=t0 + timedelta(minutes=5))
        self.assertEqual(active["remaining_seconds"], 1500)
        self.assertEqual(active["keyword"], "操作频繁")
        self.assertEqual(active["target"], "甲同学")

        self.assertIsNone(g.in_cooldown(at=t0 + timedelta(minutes=31)))
        st = g.status(at=t0 + timedelta(minutes=31))
        self.assertFalse(st["tripped"])
        self.assertTrue(st["expired"])

    def test_status_reports_remaining(self):
        g = lg.LimitGuard(path=self.path, cooldown_seconds=600)
        t0 = now().replace(microsecond=0)
        g.trip({"keyword": "验证码"}, at=t0)
        st = g.status(at=t0 + timedelta(seconds=100))
        self.assertTrue(st["tripped"])
        self.assertEqual(st["remaining_seconds"], 500)

    def test_remaining_seconds_ignores_subsecond_now(self):
        """`now()` 带微秒时倒计时不许少一秒（仪表盘会看到 1799 而不是 1800）。"""
        g = lg.LimitGuard(path=self.path, cooldown_seconds=1800)
        t0 = now().replace(microsecond=0)
        g.trip({"keyword": "操作频繁"}, at=t0)
        for frac_ms in (1, 400, 999):
            st = g.status(at=t0 + timedelta(milliseconds=frac_ms))
            self.assertEqual(st["remaining_seconds"], 1800, f"frac={frac_ms}ms")
            self.assertEqual(g.in_cooldown(at=t0 + timedelta(milliseconds=frac_ms))
                             ["remaining_seconds"], 1800, f"frac={frac_ms}ms")

    def test_clear_removes_file(self):
        g = lg.LimitGuard(path=self.path)
        g.trip({"keyword": "操作频繁"})
        self.assertTrue(g.clear())
        self.assertIsNone(g.in_cooldown())
        self.assertFalse(g.clear())     # 再删一次返回 False，不抛

    def test_broken_file_is_tolerated_and_not_deleted(self):
        g = lg.LimitGuard(path=self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{不是 JSON")
        self.assertIsNone(g.read())
        self.assertIsNone(g.in_cooldown())
        self.assertTrue(os.path.exists(self.path))   # 宁可放过一轮，也不要永久停机

    def test_unparseable_deadline_is_tolerated(self):
        g = lg.LimitGuard(path=self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"cooldown_until": "明天"}, f)
        self.assertIsNone(g.in_cooldown())

    def test_write_is_atomic_via_tmp(self):
        g = lg.LimitGuard(path=self.path)
        g.trip({"keyword": "操作频繁"})
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["keyword"], "操作频繁")

    def test_creates_missing_directory(self):
        deep = os.path.join(self._dir.name, "logs", "sub", "rate_limit.json")
        lg.LimitGuard(path=deep).trip({"keyword": "操作频繁"})
        self.assertTrue(os.path.exists(deep))


class SleepBoundsTests(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(lg.sleep_jitter_bounds(8, 20), (8.0, 20.0))

    def test_reversed_and_negative(self):
        self.assertEqual(lg.sleep_jitter_bounds(20, 8), (20.0, 20.0))
        self.assertEqual(lg.sleep_jitter_bounds(-5, 3), (0.0, 3.0))
        self.assertEqual(lg.sleep_jitter_bounds(None, None), (0.0, 0.0))
        self.assertEqual(lg.sleep_jitter_bounds("", ""), (0.0, 0.0))


class KeywordCoverageTests(unittest.TestCase):
    def test_categories_are_present(self):
        self.assertEqual(set(lg.CATEGORIES), {"rate_limit", "captcha", "account_risk"})

    def test_a_slider_captcha_phrase_is_covered(self):
        # A 的原始清单里最典型的一条：滑块验证
        for kw in ("滑动验证", "拖动滑块"):
            self.assertIsNotNone(lg.detect_texts([kw]), kw)

    def test_keywords_are_lowercase_insensitive_ready(self):
        # 全部小写化后可比较；中文不受影响
        for kw in lg.KEYWORDS:
            self.assertEqual(kw, kw.lower())


if __name__ == "__main__":
    unittest.main()
