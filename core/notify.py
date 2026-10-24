"""发送结果通知：一轮真发跑完之后，把结论推到手机/IM 上。

## 为什么要有这个

定时器每 5 分钟敲一次门，真正发完的那一刻没有人看着。登录态哪天失效、账号被风控
限流、消息其实一条都没出去 —— 这些事以前只有两种发现方式：用户自己晚上翻面板，
或者好友问"你今天怎么没发火花"。这个模块在**一轮真发跑完之后**把结论推出去，
把"要人主动查"变成"出事有人告诉"。

## 三条硬约束（改这个文件时别破）

1. **绝不影响发送结论。** 调用点在 core/tasks.py 的收尾里，而且必须在
   lock.release() 之后：webhook 最坏会卡满 NOTIFY_TIMEOUT_SECONDS（默认 10 秒），
   不能让推送占着运行锁，也不能因为推送失败把已经发出去的那一轮标成出错。
   notify_run() 自己不向外抛异常。
2. **不把 webhook 地址写进日志。** 地址里带的就是凭据（Server酱的 SCT...、Telegram 的
   bot<token>、钉钉/飞书的 access_token、企业微信的 key），所以日志只打主机名。
3. **一家一种 JSON 形状。** 这几家没有任何字段共识：Server酱是 {"title","desp"}，
   企业微信/钉钉是 {"msgtype":"text","text":{"content"}}，飞书是
   {"msg_type":"text","content":{"text"}}，Telegram 只认 {"text"}。
   认不出来的主机走通用形状 {"title","text","status","run_id","at","accounts"}，
   留给自建网关（n8n / 自己的转发服务）。

## 谁能触发

* 定时器 / 命令行：一轮真发结束 → core/tasks.py 的 runTasks 收尾自动判定要不要推；
* 面板：三个键在「运行配置」里（NOTIFY_WEBHOOK_URL / NOTIFY_LEVEL /
  NOTIFY_TIMEOUT_SECONDS），另有「发送测试通知」按钮（POST /ops/notify/test）；
* 命令行：python main.py notify-test —— 不发送任何消息，只推一条测试消息。

## 为什么直接读 os.getenv 而不走 get_config()

utils.config.get_config() 的那份字典在进程里是**冻结快照**（面板改完 .env 会
reload，但已经 import 的模块级 config 变量不会跟着变）。webhook 地址恰恰是
最需要"改完立刻生效"的一项：地址写错了，用户改完就想点一下测试看通不通。
"""

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.util_time import human_duration, now, parse_iso

__all__ = [
    "DEFAULT_LEVEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "KIND_LABELS",
    "LEVELS",
    "LEVEL_ALWAYS",
    "LEVEL_ERROR",
    "LEVEL_LABELS",
    "LEVEL_OFF",
    "MAX_TIMEOUT_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "REQUEST_KEY",
    "build_payload",
    "config",
    "format_run",
    "has_problem",
    "kind_of",
    "notify_run",
    "panel_url",
    "post",
    "send_test",
]

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

REQUEST_KEY = "NOTIFY_WEBHOOK_URL"
LEVEL_KEY = "NOTIFY_LEVEL"
TIMEOUT_KEY = "NOTIFY_TIMEOUT_SECONDS"

LEVEL_OFF = "off"          # 关掉（连"出错才推"也不要）
LEVEL_ERROR = "error"      # 只有这一轮出事才推（默认）
LEVEL_ALWAYS = "always"    # 每轮真发都推一条

LEVELS = (LEVEL_OFF, LEVEL_ERROR, LEVEL_ALWAYS)
LEVEL_LABELS = {LEVEL_OFF: "不通知", LEVEL_ERROR: "只在出错时", LEVEL_ALWAYS: "每轮都通知"}

DEFAULT_LEVEL = LEVEL_ERROR
#: 默认与范围：手机推送服务 3~10 秒足够；上限 60 秒是给自建网关留的余地，
#: 但再长就会把 systemd 的一轮拖成"看起来卡住"。
DEFAULT_TIMEOUT_SECONDS = 10.0
MIN_TIMEOUT_SECONDS = 3.0
MAX_TIMEOUT_SECONDS = 60.0

#: 主机名 → 形状。用**后缀**匹配（xxx.ftqq.com 这种也认），按顺序取第一个命中的。
HOST_KINDS: Tuple[Tuple[str, str], ...] = (
    ("sctapi.ftqq.com", "serverchan"),
    ("sc.ftqq.com", "serverchan"),
    ("qyapi.weixin.qq.com", "wecom"),
    ("oapi.dingtalk.com", "dingtalk"),
    ("open.feishu.cn", "feishu"),
    ("open.larksuite.com", "feishu"),
    ("api.telegram.org", "telegram"),
)

KIND_LABELS = {
    "serverchan": "Server酱",
    "wecom": "企业微信机器人",
    "dingtalk": "钉钉机器人",
    "feishu": "飞书机器人",
    "telegram": "Telegram",
    "generic": "通用 JSON",
}


def _level_of(raw: Any) -> str:
    """认不出来的值按 error 处理：通知最糟的失效方式是"静默什么都不发"。"""
    text = str(raw or "").strip().lower()
    return text if text in LEVELS else DEFAULT_LEVEL


def _timeout_of(raw: Any) -> float:
    try:
        value = float(str(raw or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, value))


def config(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读三个键。override 只给测试用（不想污染进程环境）。"""
    if override is not None:
        url = str(override.get("url") or "").strip()
        return {
            "url": url,
            "level": _level_of(override.get("level")),
            "timeout": _timeout_of(override.get("timeout")),
        }
    url = (os.getenv(REQUEST_KEY) or "").strip()
    # 有人把面板上打码后的样子整段抄回来（https://host/***）：那不是地址。
    # 照它发请求只会得到一条看不懂的 404，不如当没配。
    if "***" in url:
        url = ""
    return {
        "url": url,
        "level": _level_of(os.getenv(LEVEL_KEY)),
        "timeout": _timeout_of(os.getenv(TIMEOUT_KEY)),
    }


# ---------------------------------------------------------------------------
# 走哪一家
# ---------------------------------------------------------------------------


def kind_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    for suffix, kind in HOST_KINDS:
        if host == suffix or host.endswith("." + suffix):
            return kind
    return "generic"


def build_payload(
    url: str,
    title: str,
    text: str,
    *,
    kind: str = "",
    status: str = "",
    run_id: str = "",
    at: str = "",
    accounts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """按主机名拼 JSON。认不出来的走通用形状（自建网关自己挑字段用）。"""
    kind = kind or kind_of(url)
    if kind == "serverchan":
        # 标题单独一栏，正文 markdown；Server酱标题超过 32 字会被截。
        return {"title": title, "desp": text}
    if kind in ("wecom", "dingtalk"):
        # 两家都是企业机器人：text.content 是纯文本，标题并进正文第一行。
        return {"msgtype": "text", "text": {"content": f"{title}\n\n{text}"}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": f"{title}\n\n{text}"}}
    if kind == "telegram":
        # bot token 在 URL 路径里，chat_id 由用户在地址的 query 里写好（?chat_id=...），
        # 所以这里只管正文；关掉链接预览，否则一条通知会挂一张大图。
        return {"text": f"{title}\n\n{text}", "disable_web_page_preview": True}
    return {
        "title": title,
        "text": text,
        "status": status,
        "run_id": run_id,
        "at": at,
        "accounts": list(accounts or []),
    }


# ---------------------------------------------------------------------------
# 发出去
# ---------------------------------------------------------------------------


def _verdict(code: Any, body: str, kind: str) -> Tuple[bool, str]:
    """HTTP 200 不等于推送成功。

    Server酱 / 企业微信 / 钉钉 / 飞书都是 **200 + 错误码**：token 过期、频率超限、
    机器人被移出群，回的都是 200。通用形状不做这个判断（自建网关的 code 语义
    我们不知道，猜错会把成功当失败），只看 2xx。
    """
    try:
        status = int(code or 0)
    except (TypeError, ValueError):
        status = 0
    if not 200 <= status < 300:
        return False, f"HTTP {status}"
    if kind == "generic":
        return True, "已送达"
    try:
        data = json.loads(body) if (body or "").strip() else {}
    except ValueError:
        return True, "已送达"
    if not isinstance(data, dict):
        return True, "已送达"
    if data.get("ok") is False or data.get("success") is False:
        detail = data.get("description") or data.get("message") or data.get("msg") or ""
        return False, "接口说没成功" + (f"：{detail}" if detail else "")
    for key in ("errcode", "code"):
        if key not in data:
            continue
        value = data.get(key)
        if str(value).strip() in ("0", "200"):
            continue
        detail = data.get("errmsg") or data.get("message") or data.get("msg") or (body or "").strip()
        return False, f"{key}={value}：{detail}"[:300]
    return True, "已送达"


def post(
    url: str,
    payload: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    kind: str = "",
    opener: Optional[Callable[..., Any]] = None,
) -> Tuple[bool, str]:
    """POST 一段 JSON。返回 (成功?, 人话说明)，**从不抛异常**。"""
    kind = kind or kind_of(url)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "douyin-spark-fusion",
        },
    )
    call = opener or urllib.request.urlopen
    try:
        with call(request, timeout=timeout) as response:
            body = (response.read(4000) or b"").decode("utf-8", "replace")
            code = getattr(response, "status", None) or getattr(response, "code", 0)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (exc.read(400) or b"").decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - 读不出响应体也不能改变结论
            detail = ""
        return False, f"HTTP {exc.code}" + (f"：{detail}" if detail else "")
    except Exception as exc:  # noqa: BLE001 - 网络问题一大堆类型，一律当"没发出去"
        return False, f"{type(exc).__name__}: {exc}"
    return _verdict(code, body, kind)


# ---------------------------------------------------------------------------
# 这一轮长什么样
# ---------------------------------------------------------------------------

SOURCE_LABELS = {"timer": "定时器", "webui": "面板", "cli": "命令行", "test": "测试"}


def _accounts(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [item for item in (rec.get("accounts") or []) if isinstance(item, dict)]


def has_problem(rec: Dict[str, Any]) -> bool:
    """这一轮有没有"值得叫人起来看"的事。没跑成的账号算，skipped 不算（它就是没发）。"""
    if rec.get("rate_limited"):
        return True
    return any(str(item.get("status") or "") != "ok" for item in _accounts(rec))


def _clock(raw: Any) -> str:
    stamp = parse_iso(raw)
    if stamp is None:
        text = str(raw or "").strip()
        return text[5:16].replace("T", " ") if len(text) >= 16 else text
    return f"{stamp:%m-%d %H:%M}"


def _source_label(rec: Dict[str, Any]) -> str:
    raw = str(rec.get("source") or "").strip()
    return SOURCE_LABELS.get(raw, raw)


def _duration(rec: Dict[str, Any]) -> str:
    start, end = parse_iso(rec.get("started_at")), parse_iso(rec.get("finished_at"))
    if start is None or end is None:
        return ""
    seconds = int((end - start).total_seconds())
    return human_duration(seconds) if seconds >= 0 else ""


def _reason_text(raw: Any, limit: int = 140) -> str:
    """失败原因压成一行：账本里的 error 可能是整段 traceback。"""
    text = " ".join(str(raw or "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _limit_keyword(rec: Dict[str, Any]) -> str:
    limit = rec.get("limit") if isinstance(rec.get("limit"), dict) else {}
    for key in ("keyword", "reason", "why", "detail"):
        text = str(limit.get(key) or "").strip()
        if text:
            return text
    return ""


def _title(rec: Dict[str, Any]) -> str:
    accounts = _accounts(rec)
    failed = [item for item in accounts if str(item.get("status") or "") != "ok"]
    if rec.get("rate_limited"):
        return "续火花：命中风控熔断，本轮已停"
    if not accounts:
        return "续火花：这一轮没有跑任何账号"
    if failed:
        return f"续火花：{len(failed)}/{len(accounts)} 个账号没发成"
    sent = sum(int(item.get("sent_ok") or 0) for item in accounts)
    return f"续火花：{len(accounts)} 个账号全部送达（{sent} 条）"


def _text(rec: Dict[str, Any]) -> str:
    lines: List[str] = []
    parts = [_clock(rec.get("finished_at") or rec.get("started_at")), _source_label(rec)]
    duration = _duration(rec)
    if duration:
        parts.append(f"用时 {duration}")
    head = " · ".join(part for part in parts if part)
    if head:
        lines.append(head)
    if rec.get("rate_limited"):
        keyword = _limit_keyword(rec)
        lines.append("⛔ 命中限流/风控关键词" + (f"（{keyword}）" if keyword else "") + "，本轮剩余目标已中止")
    accounts = _accounts(rec)
    if accounts:
        lines.append("")
    for item in accounts:
        name = str(item.get("username") or item.get("unique_id") or "?").strip()
        sent_ok = int(item.get("sent_ok") or 0)
        sent_fail = int(item.get("sent_fail") or 0)
        if str(item.get("status") or "") == "ok":
            lines.append(f"✅ {name}：{sent_ok} 条送达")
            continue
        lines.append(f"❌ {name}：{sent_ok} 成功 / {sent_fail} 失败")
        reason = _reason_text(item.get("error"))
        if reason:
            lines.append(f"    {reason}")
    link = panel_url()
    if link:
        lines.append("")
        lines.append(f"面板：{link}")
    return "\n".join(lines)


def format_run(rec: Dict[str, Any]) -> Tuple[str, str]:
    """一轮结果的 (标题, 正文)。"""
    return _title(rec), _text(rec)


def _lan_ip() -> str:
    """猜本机在局域网里的地址：UDP connect 只是查路由表，一个包都不发。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def panel_url() -> str:
    """面板地址。猜不出来就返回空串 —— 给一个打不开的链接比没有更糟。"""
    host = (os.getenv("WEBUI_HOST") or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = _lan_ip()
    if not host or host in ("0.0.0.0", "::", "[::]") or host.startswith("127.") or host == "localhost":
        return ""
    port = (os.getenv("WEBUI_PORT") or "8787").strip() or "8787"
    return f"http://{host}:{port}/ops/runs"


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------


def notify_run(
    rec: Dict[str, Any],
    *,
    logger: Any = None,
    config_override: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """按配置决定要不要推、然后推。返回 (推成功?, 说明)。

    说明为空表示"按配置就是不该推"（没配地址 / 空跑 / 只推出错但这轮没出错），
    调用方据此不要打日志 —— 定时器一天要敲门 288 次，每轮打一行"没通知"就是噪声。
    """
    cfg = config(config_override)
    if not cfg["url"]:
        return False, ""
    if str(rec.get("dry_run") or "off").strip().lower() != "off":
        # 空跑是排练：什么都没发出去，推一条"全部成功"只会误导。
        return False, ""
    if cfg["level"] == LEVEL_OFF:
        return False, ""
    if cfg["level"] == LEVEL_ERROR and not has_problem(rec):
        return False, ""
    kind = kind_of(cfg["url"])
    try:
        title, text = format_run(rec)
        payload = build_payload(
            cfg["url"],
            title,
            text,
            kind=kind,
            status="error" if has_problem(rec) else "ok",
            run_id=str(rec.get("run_id") or ""),
            at=str(rec.get("finished_at") or rec.get("started_at") or ""),
            accounts=_accounts(rec),
        )
        ok, detail = post(cfg["url"], payload, cfg["timeout"], kind=kind)
    except Exception as exc:  # noqa: BLE001 - 通知永远不许把发送结论带偏
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    label = KIND_LABELS.get(kind, kind)
    if ok:
        return True, f"{label} {detail}"
    if logger is not None:
        host = (urllib.parse.urlsplit(cfg["url"]).hostname or "?").lower()
        logger.warning(f"通知没发出去（{label} {host}）：{detail}")
    return False, f"{label} {detail}"


def send_test(*, logger: Any = None, config_override: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """推一条测试消息（不发送任何消息、不碰运行锁）。"""
    cfg = config(config_override)
    if not cfg["url"]:
        return False, f"没有配置 {REQUEST_KEY}：先在「运行配置」里填地址，留空表示不通知"
    kind = kind_of(cfg["url"])
    stamp = str(now())
    record = {
        "run_id": "notify-test",
        "started_at": stamp,
        "finished_at": stamp,
        "dry_run": "off",
        "source": "test",
        "rate_limited": False,
        "accounts": [
            {"username": "样例账号", "status": "ok", "sent_ok": 1, "sent_fail": 0},
        ],
    }
    title, text = format_run(record)
    title = "【测试】" + title
    text += "\n\n这是一条测试通知：收到就说明地址和网络都通了，没有发送任何消息。"
    try:
        payload = build_payload(
            cfg["url"], title, text, kind=kind, status="ok",
            run_id="notify-test", at=stamp, accounts=record["accounts"],
        )
        ok, detail = post(cfg["url"], payload, cfg["timeout"], kind=kind)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    label = KIND_LABELS.get(kind, kind)
    if ok:
        return True, f"{label} 测试通知已送达"
    if logger is not None:
        logger.warning(f"测试通知没发出去（{label}）：{detail}")
    return False, f"{label} 测试通知没发出去：{detail}"
