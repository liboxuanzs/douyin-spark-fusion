"""结果通知（core/notify.py）的行为契约。

这些用例围绕四件"错了会很难受"的事：

* **形状**：Server酱 / 企业微信 / 钉钉 / 飞书 / Telegram 的字段名没有任何共识，
  拼错了对方只回 200 或干脆 400，而用户看到的只是"没收到通知"。
* **200 不等于成功**：这几家都是 HTTP 200 + JSON 里的错误码（token 过期、被移出群、
  频率超限）。只认 HTTP 状态的话，推送失败会被记成"已送达"。
* **门禁**：空跑永远不推；error 档只在真出事时推；没配地址时一声不响 ——
  定时器一天敲 288 次门，多推一条就是骚扰。
* **不许影响发送**：这一层无论怎么炸，都不能让那一轮的结论变形（调用点在放锁之后，
  core/tasks.py 的 _notify_after_run 兜住所有异常）。

另外钉住面板那一侧的"打码即未改"：地址里的 token 在路径/查询里，面板显示打码值，
保存时必须认得出"用户没动过这一栏"，否则会把 https://host/*** 写回 .env。
"""

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from core import notify, tasks
from webui import settings, store

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVERCHAN = "https://sctapi.ftqq.com/SCTsecret.send"
WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
DINGTALK = "https://oapi.dingtalk.com/robot/send?access_token=abc"
FEISHU = "https://open.feishu.cn/open-apis/bot/v2/hook/xyz"
TELEGRAM = "https://api.telegram.org/bot123:SECRET/sendMessage?chat_id=42"


def _rec(**over):
    base = {
        "run_id": "20261024-172000",
        "started_at": "2026-10-24T17:20:00+08:00",
        "finished_at": "2026-10-24T17:21:40+08:00",
        "dry_run": "off",
        "source": "timer",
        "rate_limited": False,
        "accounts": [
            {"username": "LBX", "status": "ok", "sent_ok": 3, "sent_fail": 0},
            {"username": "wheat", "status": "ok", "sent_ok": 2, "sent_fail": 0},
        ],
    }
    base.update(over)
    return base


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, _size=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(status: int = 200, body: bytes = b'{"errcode":0}', captured=None):
    def call(request, timeout=None):
        if captured is not None:
            captured.append((request, timeout))
        return _FakeResponse(status, body)

    return call


class KindTests(unittest.TestCase):
    def test_recognizes_the_five_supported_hosts(self):
        self.assertEqual(notify.kind_of(SERVERCHAN), "serverchan")
        self.assertEqual(notify.kind_of(WECOM), "wecom")
        self.assertEqual(notify.kind_of(DINGTALK), "dingtalk")
        self.assertEqual(notify.kind_of(FEISHU), "feishu")
        self.assertEqual(notify.kind_of(TELEGRAM), "telegram")

    def test_unknown_host_falls_back_to_generic(self):
        self.assertEqual(notify.kind_of("https://example.com/hook/abc"), "generic")
        self.assertEqual(notify.kind_of(""), "generic")
        self.assertEqual(notify.kind_of("not a url"), "generic")

    def test_matches_by_suffix_not_by_equality(self):
        self.assertEqual(notify.kind_of("https://x.sctapi.ftqq.com/SCT1.send"), "serverchan")


class PayloadTests(unittest.TestCase):
    def test_serverchan_uses_title_and_desp(self):
        payload = notify.build_payload(SERVERCHAN, "标题", "正文")
        self.assertEqual(payload, {"title": "标题", "desp": "正文"})

    def test_wecom_and_dingtalk_share_the_text_shape(self):
        for url in (WECOM, DINGTALK):
            payload = notify.build_payload(url, "标题", "正文")
            self.assertEqual(payload["msgtype"], "text")
            self.assertIn("标题", payload["text"]["content"])
            self.assertIn("正文", payload["text"]["content"])

    def test_feishu_uses_msg_type(self):
        payload = notify.build_payload(FEISHU, "标题", "正文")
        self.assertEqual(payload["msg_type"], "text")
        self.assertIn("正文", payload["content"]["text"])

    def test_telegram_only_carries_text(self):
        payload = notify.build_payload(TELEGRAM, "标题", "正文")
        self.assertEqual(payload["text"], "标题\n\n正文")
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertNotIn("chat_id", payload)

    def test_generic_shape_keeps_the_structured_bits(self):
        payload = notify.build_payload(
            "https://example.com/hook", "标题", "正文",
            status="error", run_id="20261024-172000", at="2026-10-24T17:21:40+08:00",
            accounts=[{"username": "a"}],
        )
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["run_id"], "20261024-172000")
        self.assertEqual(payload["accounts"], [{"username": "a"}])


class PostTests(unittest.TestCase):
    def test_posts_json_with_utf8_and_returns_delivered(self):
        captured = []
        ok, detail = notify.post(TELEGRAM, {"text": "你好"}, opener=_opener(captured=captured))
        self.assertTrue(ok)
        self.assertEqual(detail, "已送达")
        request = captured[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("application/json", request.headers["Content-type"])
        self.assertEqual(captured[0][1], notify.DEFAULT_TIMEOUT_SECONDS)

    def test_http_error_becomes_a_readable_failure(self):
        import urllib.error

        def boom(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        ok, detail = notify.post(SERVERCHAN, {}, opener=boom)
        self.assertFalse(ok)
        self.assertIn("HTTP 404", detail)

    def test_network_error_never_raises(self):
        def boom(request, timeout=None):
            raise TimeoutError("timed out")

        ok, detail = notify.post(SERVERCHAN, {}, opener=boom)
        self.assertFalse(ok)
        self.assertIn("TimeoutError", detail)

    def test_200_with_an_error_code_is_a_failure(self):
        ok, detail = notify.post(
            WECOM, {"msgtype": "text"}, opener=_opener(200, b'{"errcode":93000,"errmsg":"invalid webhook url"}')
        )
        self.assertFalse(ok)
        self.assertIn("93000", detail)

    def test_200_with_ok_false_is_a_failure(self):
        ok, detail = notify.post(
            TELEGRAM, {"text": "x"}, opener=_opener(200, b'{"ok":false,"description":"chat not found"}')
        )
        self.assertFalse(ok)
        self.assertIn("chat not found", detail)

    def test_non_2xx_is_a_failure_even_with_a_happy_body(self):
        ok, detail = notify.post(SERVERCHAN, {}, opener=_opener(500, b'{"code":0}'))
        self.assertFalse(ok)
        self.assertIn("HTTP 500", detail)

    def test_generic_gateway_is_judged_by_http_status_only(self):
        ok, _ = notify.post(
            "https://example.com/hook", {}, opener=_opener(200, b'{"code":500,"msg":"whatever"}')
        )
        self.assertTrue(ok)


class ConfigTests(unittest.TestCase):
    def test_level_unknown_falls_back_to_error(self):
        self.assertEqual(notify.config({"url": "", "level": "sometimes"})["level"], "error")
        self.assertEqual(notify.config({"url": "", "level": "ALWAYS"})["level"], "always")

    def test_timeout_is_clamped_into_range(self):
        self.assertEqual(notify.config({"url": "", "timeout": "999"})["timeout"], 60.0)
        self.assertEqual(notify.config({"url": "", "timeout": "0"})["timeout"], 3.0)
        self.assertEqual(notify.config({"url": "", "timeout": "abc"})["timeout"], 10.0)

    def test_masked_url_is_treated_as_not_configured(self):
        with mock.patch.dict(os.environ, {notify.REQUEST_KEY: "https://sctapi.ftqq.com/***"}, clear=False):
            self.assertEqual(notify.config()["url"], "")

    def test_env_is_read_every_call(self):
        with mock.patch.dict(os.environ, {notify.REQUEST_KEY: SERVERCHAN, notify.LEVEL_KEY: "always"}, clear=False):
            cfg = notify.config()
        self.assertEqual(cfg["url"], SERVERCHAN)
        self.assertEqual(cfg["level"], "always")


class GatingTests(unittest.TestCase):
    def test_no_url_means_silence(self):
        with mock.patch.object(notify, "post") as post:
            self.assertEqual(notify.notify_run(_rec(), config_override={"url": ""}), (False, ""))
        post.assert_not_called()

    def test_dry_run_is_never_pushed(self):
        with mock.patch.object(notify, "post") as post:
            result = notify.notify_run(
                _rec(dry_run="full"), config_override={"url": SERVERCHAN, "level": "always"}
            )
        self.assertEqual(result, (False, ""))
        post.assert_not_called()

    def test_off_never_pushes(self):
        with mock.patch.object(notify, "post") as post:
            result = notify.notify_run(
                _rec(accounts=[{"username": "a", "status": "error"}]),
                config_override={"url": SERVERCHAN, "level": "off"},
            )
        self.assertEqual(result, (False, ""))
        post.assert_not_called()

    def test_error_level_stays_silent_on_a_clean_round(self):
        with mock.patch.object(notify, "post") as post:
            result = notify.notify_run(_rec(), config_override={"url": SERVERCHAN})
        self.assertEqual(result, (False, ""))
        post.assert_not_called()

    def test_error_level_pushes_when_an_account_failed(self):
        rec = _rec(accounts=[
            {"username": "LBX", "status": "ok", "sent_ok": 3, "sent_fail": 0},
            {"username": "wheat", "status": "error", "sent_ok": 0, "sent_fail": 2,
             "error": "login_lost: 登录态失效，需要重新扫码"},
        ])
        captured = {}
        with mock.patch.object(notify, "post", return_value=(True, "已送达")):
            ok, detail = notify.notify_run(rec, config_override={"url": SERVERCHAN})
        self.assertTrue(ok)
        self.assertIn("Server酱", detail)

    def test_rate_limit_counts_as_a_problem(self):
        rec = _rec(rate_limited=True, limit={"keyword": "操作频繁"},
                   accounts=[{"username": "a", "status": "ok", "sent_ok": 1}])
        with mock.patch.object(notify, "post", return_value=(True, "已送达")) as post:
            ok, _ = notify.notify_run(rec, config_override={"url": SERVERCHAN})
        self.assertTrue(ok)
        payload = post.call_args[0][1]
        self.assertIn("操作频繁", payload["desp"])

    def test_always_pushes_even_a_clean_round(self):
        with mock.patch.object(notify, "post", return_value=(True, "已送达")) as post:
            ok, _ = notify.notify_run(_rec(), config_override={"url": SERVERCHAN, "level": "always"})
        self.assertTrue(ok)
        text = post.call_args[0][1]["desp"]
        self.assertIn("LBX", text)
        self.assertIn("3 条送达", text)

    def test_failed_push_returns_false_instead_of_raising(self):
        with mock.patch.object(notify, "post", return_value=(False, "HTTP 502")):
            ok, detail = notify.notify_run(_rec(), config_override={"url": SERVERCHAN, "level": "always"})
        self.assertFalse(ok)
        self.assertIn("HTTP 502", detail)

    def test_push_with_a_broken_config_does_not_raise(self):
        with mock.patch.object(notify, "format_run", side_effect=RuntimeError("boom")):
            ok, detail = notify.notify_run(_rec(), config_override={"url": SERVERCHAN, "level": "always"})
        self.assertFalse(ok)
        self.assertIn("boom", detail)

    def test_token_never_appears_in_the_returned_hint(self):
        with mock.patch.object(notify, "post", return_value=(False, "HTTP 400")):
            _, detail = notify.notify_run(
                _rec(accounts=[{"username": "a", "status": "error"}]),
                config_override={"url": TELEGRAM},
            )
        self.assertNotIn("SECRET", detail)


class FormatTests(unittest.TestCase):
    def test_title_counts_delivered_messages(self):
        title, _ = notify.format_run(_rec())
        self.assertIn("2 个账号全部送达", title)
        self.assertIn("5 条", title)

    def test_title_counts_failed_accounts(self):
        rec = _rec(accounts=[
            {"username": "a", "status": "ok", "sent_ok": 1},
            {"username": "b", "status": "error", "sent_ok": 0, "sent_fail": 1},
        ])
        self.assertIn("1/2 个账号没发成", notify.format_run(rec)[0])

    def test_rate_limit_title_says_so(self):
        self.assertIn("风控熔断", notify.format_run(_rec(rate_limited=True))[0])

    def test_text_lists_each_account_and_the_reason(self):
        rec = _rec(accounts=[
            {"username": "LBX", "status": "ok", "sent_ok": 3, "sent_fail": 0},
            {"username": "wheat", "status": "error", "sent_ok": 0, "sent_fail": 2,
             "error": "login_lost: 登录态失效"},
        ])
        _, text = notify.format_run(rec)
        self.assertIn("✅ LBX：3 条送达", text)
        self.assertIn("❌ wheat：0 成功 / 2 失败", text)
        self.assertIn("login_lost: 登录态失效", text)
        self.assertIn("定时器", text)
        self.assertIn("用时 1 分 40 秒", text)

    def test_reason_is_squeezed_and_trimmed(self):
        rec = _rec(accounts=[{
            "username": "a", "status": "error", "sent_ok": 0, "sent_fail": 1,
            "error": "Traceback (most recent call last):\n  File \"x.py\", line 1\n" + "很长的原因" * 60,
        }])
        _, text = notify.format_run(rec)
        self.assertNotIn("Traceback (most recent call last):\n", text)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 160)

    def test_missing_username_falls_back_to_the_uid(self):
        rec = _rec(accounts=[{"unique_id": "10050yi", "status": "ok", "sent_ok": 1}])
        self.assertIn("10050yi", notify.format_run(rec)[1])

    def test_empty_run_does_not_crash(self):
        title, text = notify.format_run({"accounts": [], "dry_run": "off"})
        self.assertIn("没有跑任何账号", title)
        self.assertIsInstance(text, str)


class PanelUrlTests(unittest.TestCase):
    def test_loopback_host_yields_no_link(self):
        with mock.patch.dict(os.environ, {"WEBUI_HOST": "127.0.0.1"}, clear=False):
            self.assertEqual(notify.panel_url(), "")

    def test_wildcard_host_uses_the_lan_address(self):
        with mock.patch.dict(os.environ, {"WEBUI_HOST": "0.0.0.0", "WEBUI_PORT": "8787"}, clear=False), \
                mock.patch.object(notify, "_lan_ip", return_value="192.168.15.172"):
            self.assertEqual(notify.panel_url(), "http://192.168.15.172:8787/ops/runs")

    def test_no_address_means_no_link(self):
        with mock.patch.dict(os.environ, {"WEBUI_HOST": "0.0.0.0"}, clear=False), \
                mock.patch.object(notify, "_lan_ip", return_value=""):
            self.assertEqual(notify.panel_url(), "")


class SendTestTests(unittest.TestCase):
    def test_without_a_url_it_explains_what_to_do(self):
        ok, detail = notify.send_test(config_override={"url": ""})
        self.assertFalse(ok)
        self.assertIn("NOTIFY_WEBHOOK_URL", detail)

    def test_test_message_is_marked_as_a_test(self):
        captured = []

        def fake_post(url, payload, timeout=None, **kwargs):
            captured.append(payload)
            return True, "已送达"

        with mock.patch.object(notify, "post", side_effect=fake_post):
            ok, detail = notify.send_test(config_override={"url": SERVERCHAN})
        self.assertTrue(ok)
        self.assertIn("测试", detail)
        self.assertTrue(captured[0]["title"].startswith("【测试】"))
        self.assertIn("没有发送任何消息", captured[0]["desp"])

    def test_test_message_reports_an_interface_error(self):
        with mock.patch.object(notify, "post", return_value=(False, "errcode=93000：invalid")):
            ok, detail = notify.send_test(config_override={"url": WECOM})
        self.assertFalse(ok)
        self.assertIn("93000", detail)


class TasksWiringTests(unittest.TestCase):
    def test_a_raising_notifier_never_escapes(self):
        with mock.patch.object(tasks.notify, "notify_run", side_effect=RuntimeError("boom")):
            tasks._notify_after_run({"accounts": []})  # 不抛就是通过

    def test_a_successful_push_is_logged(self):
        with mock.patch.object(tasks.notify, "notify_run", return_value=(True, "Server酱 已送达")), \
                mock.patch.object(tasks, "logger") as logger:
            tasks._notify_after_run({"accounts": []})
        self.assertTrue(logger.info.called)

    def test_nothing_is_logged_when_the_config_says_do_not_push(self):
        with mock.patch.object(tasks.notify, "notify_run", return_value=(False, "")), \
                mock.patch.object(tasks, "logger") as logger:
            tasks._notify_after_run({"accounts": []})
        self.assertFalse(logger.info.called)
        self.assertFalse(logger.warning.called)


class SettingsTests(unittest.TestCase):
    def test_mask_keeps_only_scheme_and_host(self):
        masked = store.mask_webhook_url(TELEGRAM)
        self.assertEqual(masked, "https://api.telegram.org/***")
        self.assertNotIn("SECRET", masked)

    def test_mask_of_an_empty_value_is_empty(self):
        self.assertEqual(store.mask_webhook_url(""), "")
        self.assertEqual(store.mask_webhook_url(None), "")

    def test_mask_of_garbage_is_not_echoed(self):
        self.assertEqual(store.mask_webhook_url("nonsense"), "***")

    def test_untouched_masked_value_is_not_written_back(self):
        env_map = {"NOTIFY_WEBHOOK_URL": SERVERCHAN}
        submitted = {"NOTIFY_WEBHOOK_URL": store.mask_webhook_url(SERVERCHAN)}
        self.assertEqual(settings.collect(submitted, env_map), {})

    def test_a_changed_url_is_written_through(self):
        env_map = {"NOTIFY_WEBHOOK_URL": SERVERCHAN}
        fresh = "https://sctapi.ftqq.com/SCTnew.send"
        self.assertEqual(
            settings.collect({"NOTIFY_WEBHOOK_URL": fresh}, env_map),
            {"NOTIFY_WEBHOOK_URL": fresh},
        )

    def test_clearing_the_url_is_allowed(self):
        env_map = {"NOTIFY_WEBHOOK_URL": SERVERCHAN}
        self.assertEqual(settings.collect({"NOTIFY_WEBHOOK_URL": ""}, env_map), {"NOTIFY_WEBHOOK_URL": ""})

    def test_masked_text_is_rejected_when_somebody_types_it(self):
        item = settings.BY_KEY["NOTIFY_WEBHOOK_URL"]
        ok, _, error = settings.parse(item, "https://sctapi.ftqq.com/***")
        self.assertFalse(ok)
        self.assertIn("***", error)

    def test_url_must_be_http(self):
        item = settings.BY_KEY["NOTIFY_WEBHOOK_URL"]
        self.assertFalse(settings.parse(item, "sctapi.ftqq.com/SCT1.send")[0])
        self.assertTrue(settings.parse(item, SERVERCHAN)[0])

    def test_spaces_are_rejected(self):
        item = settings.BY_KEY["NOTIFY_WEBHOOK_URL"]
        ok, _, error = settings.parse(item, "https://sctapi.ftqq.com/a b.send")
        self.assertFalse(ok)
        self.assertIn("空格", error)

    def test_empty_value_is_allowed(self):
        item = settings.BY_KEY["NOTIFY_WEBHOOK_URL"]
        ok, value, _ = settings.parse(item, "")
        self.assertTrue(ok)
        self.assertEqual(value, "")

    def test_three_rows_reach_the_panel(self):
        keys = [row["key"] for row in settings.form_state({})]
        for key in ("NOTIFY_WEBHOOK_URL", "NOTIFY_LEVEL", "NOTIFY_TIMEOUT_SECONDS"):
            self.assertIn(key, keys)

    def test_level_options_match_the_module(self):
        item = settings.BY_KEY["NOTIFY_LEVEL"]
        self.assertEqual(tuple(item.options), notify.LEVELS)

    def test_dotenv_example_documents_the_same_defaults(self):
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("NOTIFY_LEVEL=error", text)
        self.assertIn("NOTIFY_TIMEOUT_SECONDS=10", text)
        self.assertIn("NOTIFY_WEBHOOK_URL=", text)


class PanelWiringTests(unittest.TestCase):
    def test_webui_doc_counts_the_same_number_of_editable_keys(self):
        # 文档里"面板能改 N 个键"是给维护者看的清单口径；加了键却忘了改这一句，
        # 下一个人就会照着文档去数一个并不存在的键盘位。
        doc = (REPO_ROOT / "docs" / "webui.md").read_text(encoding="utf-8")
        match = re.search(r"能改 (\d+) 个键", doc)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), len(settings.SETTINGS))

    def test_deploy_doc_lists_the_three_keys(self):
        doc = (REPO_ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
        for key in ("NOTIFY_WEBHOOK_URL", "NOTIFY_LEVEL", "NOTIFY_TIMEOUT_SECONDS"):
            self.assertIn(key, doc)
        self.assertIn("main.py notify-test", doc)

    def test_dashboard_offers_the_test_button(self):
        html = (REPO_ROOT / "webui" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('action="/ops/notify/test"', html)

    def test_app_registers_the_test_route(self):
        source = (REPO_ROOT / "webui" / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/ops/notify/test")', source)

    def test_main_exposes_the_notify_test_mode(self):
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("notify-test", source)
        self.assertIn('"notify-test", "notify", "test-notify"', source)


if __name__ == "__main__":
    unittest.main()
