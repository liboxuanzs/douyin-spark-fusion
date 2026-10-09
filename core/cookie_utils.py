"""Cookie 与账号资料的纯函数层（不碰浏览器、不碰 GUI、不碰网络）。

这个模块是**单一实现**：`configTool/browser_login.py` 与 `core/qr_login.py`
（面板扫码登录）都从这里取 cookie 清洗 / 序列化 / 域白名单 / 账号资料拦截，
避免两处各写一份、各自漂移。

放这里的另一个原因是它必须能被**没有装浏览器依赖**的地方 import：
面板进程启动时不会导入 playwright / cloakbrowser，纯 stdlib 才安全。

关键约定（踩过坑才写下来的）：

- `COOKIES_<抖音号>` 的值必须是**转义过的**单行 JSON（`ensure_ascii=True`），
  因为 `utils/config.py` 读的时候会做 `.encode("utf-8").decode("unicode_escape")`，
  字面中文会被这步毁掉。
- cookie 里的 `name` 或 `domain` 为空时必须丢掉：抖音确实会发一条
  `value="douyin.com"` 的空名 cookie，带进 Playwright 的 `add_cookies`
  会让**整批**注入失败（表现为"配置里有 cookie 却登不上"）。
- `sameSite` 一律去掉，与 `utils/config.py::sanitize_cookies` 的口径一致。
"""
from __future__ import annotations

import base64
import json
import re

# 只保留这些域下的 Cookie，避免把无关站点的 Cookie 灌进任务
TARGET_DOMAINS = ("douyin.com", "bytedance.com", "snssdk.com", "iesdouyin.com", "amemv.com")

# 出现任意一个即可判定为「浏览器里存着登录态」
LOGIN_COOKIE_NAMES = ("sessionid", "sessionid_ss", "sid_tt")

# 账号信息接口。抖音自己会在页面加载时发这个请求，我们从响应里取昵称与抖音号。
# 注意：不要在页面里主动 fetch 它 —— 实测缺 a_bogus 签名时服务端直接回
# {"status_code": 8, "status_msg": "用户未登录"}，只有抖音自己的请求才是有效的。
SELF_PROFILE_API = "/aweme/v1/web/user/profile/self"

# 抖音号 / unique_id 允许出现在环境变量名里（`COOKIES_<它>`），所以先卡一道字符集
UNIQUE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------
def matches_target(domain: str) -> bool:
    domain = (domain or "").lower().lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in TARGET_DOMAINS)


def clean_cookie(cookie: dict):
    """把一条 cookie 裁剪成 Playwright add_cookies 需要的字段。

    返回 None 表示这条**不可用**，调用方应丢弃：
      - name 为空：站点确实会产生这种条目（实测抖音上就有一条 value="douyin.com"
        的空名 cookie），而 add_cookies 遇到空 name 会让整批注入失败；
      - domain 为空：没有归属域，Playwright 同样不接受。

    去掉 sameSite 与主项目 utils/config.py::sanitize_cookies 的口径保持一致。
    """
    if not isinstance(cookie, dict):
        # 上游给什么都要扛住：storage_state() 里混进 null、或调用方直接喂字符串
        return None
    name = str(cookie.get("name") or "").strip()
    domain = str(cookie.get("domain") or "").strip()
    if not name or not domain:
        return None
    return {
        "name": name,
        "value": cookie.get("value", ""),
        "domain": domain,
        "path": cookie.get("path") or "/",
        "expires": cookie.get("expires", -1),
        "httpOnly": bool(cookie.get("httpOnly", False)),
        "secure": bool(cookie.get("secure", False)),
    }


def filter_cookies(raw, *, domains_only: bool = True):
    """按域过滤 + 清洗，返回 (cookies, 统计)。

    `domains_only=False` 时不过滤域（调用方已知要全量保存）。

    统计里的 `fell_back` 表示「目标域里没剩几条，于是改成全量保存」——
    这是刻意留的兜底：少一条登录态 cookie 就登不上，多带几条无关域只是配置大一点。
    """
    raw = list(raw or [])
    picked = [c for c in raw if matches_target(c.get("domain", ""))] if domains_only else raw
    cookies = [c for c in (clean_cookie(item) for item in picked) if c]
    stats = {
        "raw": len(raw),
        "matched": len(picked),
        "invalid": len(picked) - len(cookies),
        "fell_back": False,
    }
    if domains_only and (not cookies or len(cookies) * 2 < len(raw)):
        cookies = [c for c in (clean_cookie(item) for item in raw) if c]
        stats["fell_back"] = True
    return cookies, stats


def cookies_to_json(cookies: list, *, escaped: bool = True) -> str:
    """序列化为单行 JSON。

    escaped=True 必须用于写进 .env 的场景：utils/config.py 读取时会做
    `.encode("utf-8").decode("unicode_escape")`，字面中文会被这步毁成乱码，
    转成 \\uXXXX 才能安全往返。
    """
    if escaped:
        return json.dumps(cookies, ensure_ascii=True, separators=(",", ":"))
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def parse_cookies(text: str):
    """把 cookies 文本解析成列表；非法返回 (None, 错误信息)。"""
    text = (text or "").strip()
    if not text:
        return None, "Cookies 为空"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"不是合法 JSON：{exc.msg}"
    if not isinstance(data, list):
        return None, "必须是 JSON 数组（形如 [{...}]）"
    return data, ""


def has_login_cookie(cookies) -> bool:
    """本地这份 cookie 里有没有登录态条目（决定存下来有没有用）。"""
    return any(str((c or {}).get("name") or "") in LOGIN_COOKIE_NAMES for c in (cookies or []))


def cookie_names(cookies) -> list:
    """按出现顺序去重后的 cookie 名（只报名字，不落值）。"""
    out = []
    for item in cookies or []:
        name = str((item or {}).get("name") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def data_uri_bytes(uri: str):
    """把 `data:image/png;base64,...` 解成 bytes；不是这种形式则返回 None。

    抖音的登录二维码就是这么给的（实测 178×178 的 PNG），所以面板不用截图，
    直接把这段 base64 转发给浏览器即可 —— 无头模式下这比截图可靠得多。
    """
    text = str(uri or "")
    if not text.startswith("data:"):
        return None
    head, _, payload = text.partition(",")
    if "base64" not in head or not payload:
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001 —— 坏数据只该让这次取码失败
        return None


def env_key_for(unique_id: str) -> str:
    """`COOKIES_<抖音号>`；抖音号不合法时抛 ValueError（宁可不写，也别写出个怪键）。"""
    uid = str(unique_id or "").strip()
    if not UNIQUE_ID_RE.match(uid):
        raise ValueError(f"抖音号不合法，无法作为环境变量名：{uid!r}")
    return f"COOKIES_{uid.upper()}"


# ---------------------------------------------------------------------------
# 账号资料：拦截 SELF_PROFILE_API 的响应，取 nickname / unique_id
# ---------------------------------------------------------------------------
# 注入到页面世界的钩子。它只做「记录」，不主动发请求，也不改动请求参数。
#
# 为什么不用 page.route 改写请求：抖音的请求带 a_bogus 签名，重放风险高。
# 为什么同时还要 Python 侧 page.on("response")：两条路互补 ——
#   页面钩子不依赖 Python 事件泵，但注入痕迹留在页面世界里；
#   Python 侧监听不注入任何代码，但读到 body 的时机受事件派发影响。
# 两条都挂上，谁先拿到就用谁。
INTERCEPT_JS = r"""
(() => {
  if (window.__dySelfHook) return;
  const hook = { profile: null, status: null, hits: [] };
  window.__dySelfHook = hook;

  const match = (url) => String(url || "").indexOf("/user/profile/self") >= 0;

  const record = (text, via) => {
    if (!text || text.charAt(0) !== "{") return;
    let data;
    try { data = JSON.parse(text); } catch (e) { return; }
    if (!data || typeof data !== "object") return;

    // 无论有没有 user 都记一份状态，方便上层区分「没截到」和「截到了但未登录」
    hook.status = {
      code: data.status_code,
      msg: data.status_msg != null ? String(data.status_msg) : "",
      via: via,
      at: Date.now()
    };

    const u = data.user;
    if (!u || typeof u !== "object") return;
    if (!u.nickname && !u.unique_id && !u.short_id) return;

    hook.profile = data;
    hook.hits.push(via + "@" + Date.now());
    if (hook.hits.length > 20) hook.hits.shift();
  };

  const _fetch = window.fetch;
  if (typeof _fetch === "function") {
    window.fetch = function (...args) {
      let url = "";
      try { url = (args[0] && args[0].url) || String(args[0] || ""); } catch (e) {}
      const promise = _fetch.apply(this, args);
      if (match(url)) {
        try {
          promise.then((r) => r.clone().text()).then((t) => record(t, "fetch")).catch(() => {});
        } catch (e) {}
      }
      return promise;
    };
  }

  const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (proto) {
    const _open = proto.open;
    const _send = proto.send;
    proto.open = function (method, url, ...rest) {
      try { this.__dyUrl = String(url || ""); } catch (e) {}
      return _open.call(this, method, url, ...rest);
    };
    proto.send = function (...args) {
      try {
        if (match(this.__dyUrl)) {
          this.addEventListener("load", () => {
            try { record(this.responseText, "xhr"); } catch (e) {}
          });
        }
      } catch (e) {}
      return _send.apply(this, args);
    };
  }
})();
"""

# 最后兜底：扫 localStorage 里可能存在的用户信息（接口完全没截到时才用）
LOCAL_STORAGE_JS = r"""
() => {
  const pick = (u) => {
    if (!u || typeof u !== "object") return null;
    const uid = u.unique_id || u.short_id;
    const nick = u.nickname || u.nick_name || u.name;
    if (!nick && !uid) return null;
    return { nickname: nick ? String(nick) : "", unique_id: uid ? String(uid) : "" };
  };
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !/user|profile|account|login/i.test(key)) continue;
      const raw = localStorage.getItem(key);
      if (!raw || (raw.charAt(0) !== "{" && raw.charAt(0) !== "[")) continue;
      let data;
      try { data = JSON.parse(raw); } catch (e) { continue; }
      for (const cand of [data, data && data.user, data && data.userInfo,
                          data && data.user_info, data && data.data, data && data.profile]) {
        const got = pick(cand);
        if (got && (got.nickname || got.unique_id)) {
          return Object.assign(got, { source: "localStorage:" + key });
        }
      }
    }
  } catch (e) {}
  return null;
}
"""


def extract_self_profile(data) -> dict:
    """从 self profile 响应里取出账号信息（纯函数，便于单测）。

    真实响应结构（2026-09 实测）::

        {
          "status_code": 0,
          "status_msg": null,
          "user": {"nickname": "示例昵称", "unique_id": "12345678901",
                   "short_id": "12345678901", "uid": "10000000000000001", ...}
        }

    nickname  -> 用户名
    unique_id -> 抖音号
    """
    out = {
        "nickname": "",
        "unique_id": "",
        "short_id": "",
        "uid": "",
        "sec_uid": "",
        "id_source": "",
        "status_code": None,
        "status_msg": "",
        "source": "",
    }
    if not isinstance(data, dict):
        return out

    out["status_code"] = data.get("status_code")
    out["status_msg"] = str(data.get("status_msg") or "")

    user = data.get("user")
    if not isinstance(user, dict):
        return out

    def text(key: str) -> str:
        value = user.get(key)
        return "" if value is None else str(value).strip()

    out["nickname"] = text("nickname") or text("other_nickname")
    out["short_id"] = text("short_id")
    out["uid"] = text("uid")
    out["sec_uid"] = text("sec_uid")

    unique_id = text("unique_id")
    if unique_id:
        out["unique_id"] = unique_id
        out["id_source"] = "unique_id"
    elif out["short_id"]:
        # 用户没设自定义抖音号时，抖音界面展示的「抖音号」就是 short_id
        out["unique_id"] = out["short_id"]
        out["id_source"] = "short_id（该账号未设自定义抖音号）"

    return out


def read_page_hook(page) -> dict:
    """读取页面侧钩子的原始记录：{'profile': 响应, 'status': {...}, 'hits': [...]}。"""
    if page is None:
        return {}
    try:
        got = page.evaluate("() => (window.__dySelfHook || null)")
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def detect_account_info(page) -> dict:
    """仅从页面侧钩子读取并归一化（保留给单独使用页面的场景）。"""
    hook = read_page_hook(page)
    return extract_self_profile(hook.get("profile"))
