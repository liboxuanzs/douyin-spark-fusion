# 融合项目设计说明

> 派生自 [2061360308/DouYinSparkFlow](https://github.com/2061360308/DouYinSparkFlow)（下称 **C**，MIT），
> 叠加 **A** 的限流熔断思路与 **B** 的仪表盘形态，并按本机家庭网络环境（`本机`）做本地化部署改造。

## 1. 需求

用户原话（m01808）：

> 现在请你基于C的基础上进行修改整合成一个融合型项目：在C的基础上解决其"乐观通过"的漏洞，去掉不用的Actions，
> 添加dry-run，添加A类似的限流关键词熔断，将B的仪表盘融入此融合项目。整合好后检测漏洞并修复，
> 确保不存在融合后兼容问题后，部署到本机并开启局域网监听。

拆成六项可验收的改动：

| # | 改动 | 验收方式 |
|---|------|----------|
| 1 | 修复 C 的"乐观通过"漏洞 | 新增单测：DOM 无选中态 / convId 不匹配 / 仅网络信号 → 一律判失败 |
| 2 | 去掉不用的 Actions | `.github/workflows/` 只剩一份手动触发的工作流；`schedule.yml` / `docker-publish.yml` 删除并断开引用 |
| 3 | 添加 dry-run | 全链路可空跑：不点发送、不落盘、返回结构化结果 |
| 4 | 添加 A 式限流关键词熔断 | 关键词命中 → 当轮中止 + 冷却 + 结构化上报 |
| 5 | 融入 B 的仪表盘 | webui 可启动、可登录、能看到运行状态，且**不 import playwright** |
| 6 | 部署到 `本机` 并开局域网监听 | `0.0.0.0` 监听 + systemd 常驻 + 从 `.224` 能打开页面 |

## 2. 基线（改动前实测）

- C 的 `core/douyin_im.py` 1982 行；`python -m pytest tests -q` = **99 passed, 15 skipped, 30 subtests passed**。
- 15 个 skip 里 13 个是 `tests/test_douyin_im_har.py`（要本机真实 HAR），1 个是 `tests/test_config_tool.py`（本机 Tk 缺字体，纯环境噪声）。
- 本机 Python 3.12.10；缺 `fastapi` / `uvicorn` / `jinja2` / `python-multipart` / `itsdangerous` / `playwright` / `cloakbrowser`。
  → **核心逻辑可离线测**（`tests/test_douyin_im.py` 用 `_Stub(DouyinIM)` 绕过 `__init__`），浏览器与 web 层必须另装依赖。

## 3. "乐观通过"漏洞的精确位置

`core/douyin_im.py` 的 `_select_and_verify()`（1559-1644），三处**都会在证据不足时返回成功**：

```
1612: if cur and (not want or not cur.get("convId") or cur["convId"] == want):
1613:     break                       # ← cur.get("convId") 为 None（fiber 取不到 id）也算"确认"
1619: if cur and (not want or not cur.get("convId") or cur["convId"] == want):
1620-1622: logger.warning("[SEL] ✅ 成功 …"); return True
1623: if net:                          # ← 只有"出现过含 conv_id 的响应"就算成功
1624-1625: logger.warning("[SEL] ✅ 成功（仅网络信号，DOM 无选中态）…"); return True
```

失败后果：

1. **发错人**：选中态仍停在上一个会话，而 `type_and_send` 已经往"以为已选中"的会话里打字。
2. **静默丢消息**：调用方以为成功，实际没发出去。

### 3.1 第二处隐藏缺陷（改动前必须一起修）

`JS_MSG_STATE`（365-373）统计的是**全局** `[data-e2e="msg-item-content"]` 数量：

```
count: items.length,      // 整个页面所有消息气泡，不分会话
```

而 `_wait_receipt()`（1923-1947）用它做 DOM 回执：

```
1935: if (st.get("count", 0) > msg_before.get("count", 0)
1936:         and st.get("lastFromMe")):
```

**换个会话就归零** → `count` 变小 → `st.count > msg_before.count` 不成立 → 明明发出去了却判失败。
所以这个缺陷是**双向**的：既可能误判成功，也会误判失败。修复口径：把消息计数与"当前选中会话"绑定。

## 4. 修复设计

### 4.1 严格确认（fail-closed）

新增 `core/strict_verify.py`，把判定从 `_select_and_verify` 里抽出来做成纯函数，便于离线单测：

- **身份要用强标识**：优先 `conv_id`（可由 `self_uid` + `peer_uid` 复算，见 `assemble_conv_id`），
  其次"DOM 标题精确等于目标显示名（归一化后）"。
- `fuzzy`（子串命中）**不算**强标识 —— 只有弱证据时必须补第二个信号。
- DOM 明确回 `convId == want` → `strong`。
- DOM 回 `convId is None` → **不再视为成功**；降级为 `weak`，需要标题等第二信号才能过。
- 只有网络信号（`get_by_conversation` 响应里出现过 conv_id）→ `weak`，**单独不足以通过**。
- 全部缺失 → `fail`。

返回值是结构化对象（`ok / level / reason / evidence`），直接进 dry-run 与仪表盘的运行结果。

### 4.2 消息数按会话绑定

`JS_MSG_STATE` 增加 `convId`（复用 `JS_CURRENT_CONV` 的 fiber 走法），
`_wait_receipt` 的 DOM 分支要求 `st["convId"] == 目标 conv_id`，否则不采信。

### 4.3 发送前复核

`type_and_send` 在点发送前再读一次 `_current_conv()`，若已不是目标会话 → 中止并返回
`reason="selection_lost"`，**不进输入**。发送后再复核一次（防前端自动切回上一次会话）。

## 5. dry-run 设计

dry-run **不是"只跳过发送"**，否则验证不到真实链路。分级：

| 级别 | 做什么 | 不做什么 |
|------|--------|----------|
| `off` | 正常发送 | — |
| `select` | 扫描 + 严格确认 + 走到"输入前的最后一步" | 不点发送 |
| `full` | 扫描 + 严格确认 + 真的把文本输入 Draft.js 编辑器并读回内容校验 | 不点发送按钮；输入完清空编辑器 |

`full` 的价值：能发现"编辑器找不到 / 合成输入缺行 / 换行处理错"这类只在真实输入时才暴露的问题。

dry-run 的判定同样走严格确认，所以 **dry-run 结果 = 真跑结果（除发送本身）**。

## 6. 限流熔断（移植 A 的思路）

A 的做法（`core/automation.py:27-40`）是 12 个中文关键词 + `bounding_box()` 判可见 + 发送前后各查一次。
融合版做成独立模块 `core/limit_guard.py`：

- 关键词表（中文 + 英文）覆盖：操作频繁 / 发送过于频繁 / 请勿频繁 / 安全验证 / 滑动验证 / 验证码 / 人机验证 / 网络异常 / 账号异常 / 登录失效 等。
- 采样来源有两个，都要看：
  1. **DOM 文案**（页面上出现的提示条 / 弹窗 / toast）；
  2. **接口返回**（`_handle_send` 的 `code` / `status`）。
- 命中 → `tripped`：**立刻停止本轮剩余目标**，写冷却截止时间（默认 30 分钟），并在结构化结果里标 `rate_limited`。
- 冷却期内不启动新的一轮（cron 触发也直接跳过并记日志），避免"重试加剧风控"。
- **记账口径：熔断 ≠ 已发送**。门禁处命中（一条都还没发）与发送中途命中且 `sent_ok == 0`，
  都必须记 `skipped` —— 当天名额留着，冷却结束 / 重新登录后照常补发；只有真送达过
  （`sent_ok > 0`）的账号才记 `ok`。从前"发送中熔断"这一格不写状态，被 `_mark_status()`
  兜成 `ok`：面板写"今天已发"、当天名额被吃掉，而消息一条都没送到（GitHub issue #14）。

### 6.1 会话被踢（`{"decision": "KICK"}`）

2026-10-20 的真实事故：两个账号到点时，首条发送的响应体是一句 `{"decision": "KICK"}`（不是 protobuf 信封）。
`decode_send_resp()` 当时只解出 `code=None status=""`，于是被当成"这一条没发出去"：每个目标重试一次、
每次白等 20 秒回执超时，整天一条都没送出去；而账号级状态还是 `ok` —— 调度认定"今天已发"、不会补发，
面板上一切正常。判定拆成四层，缺一层就又退回旧行为：

1. **解码**：`decision_of(buf)` 只认 JSON 判决（body 以 `{` 开头才试），`decode_send_resp()` 返回
   `kicked = decision == "KICK"`；`ok` 仍是 `False` —— 它永远不是一次成功发送。
2. **监视器**：`ImMonitor._handle_send` 见到 kicked 立刻置 `_login_dead = True`、
   `_login_dead_reason = "KICK:KICK"`，并触发掉登录回调（`_emit_login_lost` 的 `head` 参数给这条自己的说明）。
3. **不白等**：`_wait_receipt()` 一看到 kicked 就 break；`type_and_send()` 直接返回 `reason="session_kicked"`，
   **不复检登录态、不重试同一个死会话**（复检在这里是反作用的：页面还长着 IM 的样子，复检也可能说"登录着"）。
4. **记账**：`core/tasks.py` 记 `session_kicked`（`status="error"`，不是 `ok`）→ 当天名额不被吃掉；
   重新扫码后 `core/schedule.requeue_after_relogin()` 照常撤回这条账；`viewmodel.build_account` 把它翻成
   「账号阻断 · 重扫一次即可，登录成功后自动补发」。

判断口径：**KICK 属于"账号级、今天别再试"，不属于"这一条失败了"**。前者要停手并等人扫码，
后者才轮到重试预算（见上一节的熔断口径，两者都落在"error/skipped ≠ 已发送"这一条上）。
### 6.2 登录态只活了九天：每账号的浏览器状态快照（2026-10-21）

10-20 那次 KICK 修完之后接着查"两个账号同一天掉登录"。两份会话的存活期是 9.3 天和 9.9 天，
而同一批账号在用户自己的浏览器里挂了 15 天以上没掉。四条实测差异：**WebGL 全无**（Chrome 启动参数里有
`--disable-gpu`，`WEBGL_debug_renderer_info` 两个字段都是 null）、**`x-web-secsdk-uid` 每轮都换**、
`localStorage` / CacheStorage 每轮清零、`.env` 里的 cookie 从扫码那一刻起**再没被回写过**。
合起来是一句话：每轮发送都是一个"全新的陌生设备"在复用一张九天前的旧票。

抖音的会话是设备绑定的：新上下文会重新生成 secsdk 设备号并下发新 cookie，服务端据此认为换了设备，
旧会话被降级甚至判为异常。三处一起改，缺一处都还会继续掉：

1. **P1 状态快照（主因）**：每个账号一份 `logs/browser-state/<抖音号>.json`（Playwright `storage_state`），
   下一轮 `new_context(storage_state=…)`，跑完再 `storage_state()` 存回来。
   **不用 `launch_persistent_context(LOGIN_PROFILE_DIR/<uid>)`**：那份资料目录同时被面板扫码和"读取好友列表"
   占用（SingletonLock + `kill_profile_processes`），发送路径去抢锁会失败；快照是无锁的单文件。
2. **P2 cookie 回写**：服务端新签发的 cookie 写回 `.env` 的 `COOKIES_<抖音号>`，复用 `webui/envfile.save_cookie()`
   （自带备份、0600、dotenv 转义），`core/` 侧**惰性 import**，拿不到写入器就安静跳过。
   不回写的话 `.env` 里的会话永远停在扫码那一刻，面板"是否登录"、发送前复检都在拿旧值判断。
3. **P3 指纹**：`BASE_CHROME_ARGS` 去掉 `--disable-gpu`，让浏览器自报 GPU/WebGL，
   没有 GPU 的机器由 `--enable-unsafe-swiftshader` 兜底（SwiftShader 软渲染，WebGL 照样可用）。
4. **P4 登录设备统一（2026-10-22）**：扫码成功后把持久化上下文的整份 `storage_state` 也写进
   `logs/browser-state/<抖音号>.json`（`core/qr_login._save_device_snapshot`）。此前**登录是一台设备、
   发送是另一台**：实测登录资料目录的 `web_runtime_security_uid` 是 `283d8355…`，发送快照是
   `21d1e1d3…`，`__msuuid__` / `a11y_device_id` 也各不相同 —— "同一个账号天天换机器"本身就是信号。
   登录成功时以登录这台为准覆盖快照，之后每轮发送沿用；写快照失败只记 warning，不影响登录结果。
   P4 上线前就已经登录的账号，要等**下一次重新扫码**才会统一（之前没有快照，首轮发送仍会新开一台）；
   也可以在停机窗口里做一次性回填（2026-10-22 对线上两个账号做过）：用各自的登录资料目录开一次持久化上下文、
   取 `storage_state()` 写进 `logs/browser-state/<抖音号>.json`（`core/browser_state.write`，目录 0700 / 文件 0600）。
   回填后**不碰 `.env`**：两者是同一份会话就只信快照，不同（例如 `.env` 是手动粘的浏览器 cookie）就仍以 `.env` 的票为准、
   只沿用快照里的设备号 —— 换票不换设备。

三条判定，都是"宁可少做不可做错"：

- **同一份会话只信快照**：快照与 `.env` 的 `login_cookie_signature` 相同 → 不再 `add_cookies` 注入 `.env` 的旧值
  （快照更全，含服务端后来签发的）；不同（重新扫码 / 手动换过 Cookie）→ `.env` 赢、照旧注入，但快照里的
  `localStorage` / 设备号仍然挂上去 —— 换票不该换设备。
- **状态层只降级不报错**：读不出按全新上下文、写不进只记 warning，绝不改变本轮结果；
  `write()` / `sync()` 外面还包一层 try —— 收尾阶段的任何意外都不许把一次成功发送改判成失败。
- **被踢 / 掉登录 / 门禁判死时只存设备状态、不回写 `.env`**：别把已经死掉的会话盖到可能还好的票上。
  门禁（`LOGGED_OUT` / `EXPIRED` / `LOGIN_LOST`）一条都没发出去时同样算死会话 —— 页面可能已经把它换成一份
  **匿名** `sessionid`（名字还叫 `sessionid`、过期时间在未来，`has_login_cookie` 照样认），写回去就把真凭据
  盖掉了；设备号（`localStorage`）仍然值得留下，所以快照照写。

验收口径：真机上 `logs/browser-state/<uid>.json` 生成且只有抖音域的 cookie；两轮之间
`x-web-secsdk-uid` 不变；跑完 `.env` 的 `COOKIES_<uid>` 有刷新（设备号那几个值会变）。
这两份状态文件**等价于凭据**（`0600` / 目录 `0700`），别进备份、别提交。

### 6.3 时区、语言与出口 IP 对齐（2026-10-22）

浏览器会自报时区（`Intl.DateTimeFormat().resolvedOptions().timeZone`、`Date` 偏移）和语言
（`navigator.language`、`Accept-Language`）。此前 `core/browser.py` 只传指纹种子，这两项跟着
服务器走：系统时区 `Asia/Shanghai`，而实测出口 IP 是香港 HGC 住宅宽带（`223.19.25.170`），
UA 又自称 Windows —— **时区和 IP 不在同一地**，是风控里最便宜的一条对照。

改法：cloakbrowser 的 `launch()` / `launch_persistent_context()` 本来就收 `timezone` 与 `locale`
（分别落到 `--fingerprint-timezone` 与 `--lang`），`core/browser.py` 现在把这两个参数透传下去：

- `browser_locale()`：读 `BROWSER_LOCALE`，空 = `zh-CN`；
- `browser_timezone()`：读 `BROWSER_TIMEZONE`（IANA 名），空 = 不传、让浏览器用系统时区；
  写错名字用 `zoneinfo.ZoneInfo` 校验失败只记一条 warning、退回系统时区，**不许拖垮发送**；
- `launch_options()` / `context_options()`：前者给 cloakbrowser（参数名 `timezone`），后者给
  Playwright 的 `new_context()` / `launch_persistent_context()`（参数名 `timezone_id`）；
- `core/tasks._new_context()` 开每个任务的上下文时也带上，否则页面里的时区/语言又回到默认值。

为什么不自动按出口 IP 推（cloakbrowser 有 `geoip=True`）：它要额外装 `geoip2`、首次下载 ~70MB
GeoLite2 库，解析失败还会抛异常/加延迟；我们的出口是固定的，显式配置更稳、可审计。

实测（同一指纹种子、Windows 平台）：不传时 `Intl` 是 `Asia/Shanghai`；传
`timezone=Asia/Hong_Kong, locale=zh-HK` 后 `Intl` 变成 `Asia/Hong_Kong`、`navigator.language`
变成 `zh-HK`、偏移 −480，UA 仍是 Windows Chrome/146 —— 参数确实生效。生产只改时区、语言保持
`zh-CN`：香港 IP 上简体中文完全正常，抖音的主要语言就是简体。

**字体**：浏览器把 Windows 字体名都报告为"可用"，但真实字宽会漏出来（72px、同一串测试文本）。
2026-10-22 在服务器上补了度量兼容字体：`fonts-crosextra-carlito`（Calibri）、
`fonts-crosextra-caladea`（Cambria）、`fonts-liberation2`（Arial/Times），以及从微软官方
[Selawik 发布页](https://github.com/microsoft/Selawik) 装的 Selawik + fontconfig 别名
（`Segoe UI` → `Selawik`）—— 它是微软自己的 Segoe UI 度量兼容替代。装完实测：Calibri
1436.77 → 1380.94、Cambria 1436.77 → 1423.09、Segoe UI 1436.77 → 1467.08（不再落在同一个
"通用兜底"宽度上）。**只剩 CJK 一条没修完**：`SimSun` 仍等于 `Noto Serif CJK SC`（1690.92）、
`SimHei` 仍等于 `Noto Sans CJK SC`（1496.89）—— 没有可自由分发的宋体/黑体度量兼容字体，
这属于服务器固定资产、不是本项目代码能改的，留在"已知差异"里。

验收口径：真机页面里 `Intl.DateTimeFormat().resolvedOptions().timeZone` 与 `navigator.language`
等于 `.env` 里配置的两个值；请求头 `Accept-Language` 跟着变；连续两轮不变。
## 7. 仪表盘（借鉴 B 的形态）

B 的 `webui/` 不能直接搬：它和 noVNC / 账号库 / 主机 cron 注入强耦合。融合版**重写一层薄 webui**，
只保留 B 里被验证好用的形态：

- 单页仪表盘：账号卡（登录态 / 上次运行 / 成功失败计数 / 火花天数）、最近运行明细、日志尾部、手动触发、dry-run 开关。
- 认证：单口令 + 签名 cookie（`itsdangerous`），**默认关闭**但绑 `0.0.0.0` 时**强制要求设口令**（拒绝裸奔启动）。
- 关键约束：**`webui/` 不得 import `playwright` / `cloakbrowser`**，也不得在 import 期读 `.env` 里的 `TASKS`
  （C 的 `core.tasks` 是模块级快照 `config = get_config()`，import 即求值 —— 仪表盘必须绕开它）。

### 两套皮肤（2026-10-18 起）

样式表有两套**完整**实现，切换只是换一个 `<link>`，不刷新页面也不重建 DOM（HTML 与 `app.js` 始终只有一份）：
`static/app.css`「扁平控制台」与 `static/app-aurora.css`「极光棱镜」（玻璃层 + 动态极光光斑 + 光谱描边）。
2026-10-19 起**默认就是 aurora**：没挑过皮肤的浏览器（含隐私模式读不到 `localStorage`）打开即极光棱镜，挑过 classic 的留在 classic。
两套的**版式**也不同（2026-10-19 起）：aurora 在桌面端把侧栏改成顶部指挥条、仪表盘改成 12 栏栅格、运行记录改成卡片列表、
日志页改成通栏终端、登录页改成居中单列，classic 仍是左侧竖栏 + 单列流水。版式差异同样只能靠 CSS 重排**同一份 DOM** 实现 ——
模板一个字都不改。栅格的行高按该行最高的卡算，所以**矮卡旁边会留死白**（aurora 仪表盘在 1363px 宽下左栏空了 190px），
桌面版式得靠 `grid-row: span 2` 之类的跨行规则把短卡拼上去，而不是让卡片各占一行。皮肤只决定 css 挂点长什么样，不决定 DOM 结构；运行时类挂点（`.poll-stale`、`.maintenance-*`、
`overview-value--short|--prose`、`status-chip` 四色、`.dot.live` …）**两套皮肤都要各自覆盖**，漏一个就是"换了皮肤少一块"。
主题（深/浅，`localStorage["sparkflow-theme"]`）与皮肤（`sparkflow-skin`）是**两个正交维度**，四种组合都要成立 —— 也就是说同一套皮肤里，
每个"文字压在色块上"的地方都得跟着主题换字色（aurora 用 `--accent-ink`：深色主题光谱浅、配近黑字，浅色主题光谱深、配白字；
2026-10-19 之前那里写死近黑，浅色下主按钮只有 3.1:1）。次要文字（`--text-3`/`--text-4`）也按主题分开定档，
浅色那一套压深过一档，保证在最白的面板上仍有 4:1 以上。
单个控件内部的层级也一样：**有边框或内边距的容器套一层同族圆角时，内层半径要由外层半径减去退让量算出来**，
写死就会出现"内外两个圆角矩形相交"（aurora 的分段控件曾把理想值 9px 写成 10px，经典皮肤 8px 容器配 6px 药丸直接交叉）。
但半径只差 1px 时人眼看不出来（实测两条弧的最小点距修前 3.2–3.4px、修后 3.0px）：容器套药丸这类控件还得让**导轨和药丸有色差**，
否则两条圆角在屏幕上糊成一片，用户照样报"相交" —— 浅色主题下把导轨压暗到 `rgba(21, 28, 56, 0.10)` 再补一道内阴影凹槽才看清。
反过来说，**一条视觉边界只该由一个元素画**：外层卡片和内层折叠块各自画边框和底，即便圆角算得再准，也会出现
"两个圆角矩形套在一起"的观感（仪表盘那四张只有一个折叠块的卡就是这样，后来改成外层不画、折叠块即卡片）。
**质感是一层可以后补的皮**：卡片同时有顶部天光（`--card-sheen`）、上下两道内描边（`--inner-top` / `--edge-bottom`）和外投影，
卡才有厚度，不然浅色下就是一张贴纸；极光背景再叠一层内联 SVG 噪声（`feTurbulence`）当胶片颗粒，三层光斑随指针做几十像素的视差。
这些都只改 `background-image` / `box-shadow` / `translate`，不碰尺寸，所以加质感不需要重排版式；**但任何新加的层都要重算对比度** ——
浅色卡面从 66% 白提到 80% 白就是这一步的直接结果：原来粉紫极光整块透上来，白卡染成淡紫、字灰到 4:1 边缘。
另有一类坑与"主题 × 皮肤"正交无关：版式层用 `background: none` 抹装饰时**特异性必须够**，否则浅色主题那条 (0, 2, 0) 的渐变会压过
版式层的 (0, 1, 0)，小图上肉眼看不出、浅色下就是一块没有圆角的硬边白矩形（登录页品牌区）。
页面之间也不再硬切：两套样式表与 `base.html` / `login.html` 的 `<head>` 都用跨文档 View Transition（`@view-transition { navigation: auto; }`）把顶栏留在原地，
只让页头与页体淡出、上浮；全屏极光背景**不能**单独命名，否则它会画在 root 快照上面把正文盖住 —— 这条是实测出来的
（转场中只剩顶栏和背景）。减少动效时要单独停 `::view-transition-*` 的动画，通配 `*` 管不到这些伪元素。
这条 opt-in 必须内联在 `<head>`：外链 CSS 在新文档里可能还没解析完，Chrome 会报
`ViewTransition opt-in disabled` 并硬切；外链那份只负责 `::view-transition-*` 与命名。
**“支持”不能只看 `CSS.supports("view-transition-name")`，也不能只看 `CSSStyleSheet` 解析**：Firefox 157 的
supports 为 true、`startViewTransition` 也是 function，却会把 `@view-transition` 整条丢掉；部分 Chromium 分支还会
「解析得出、实际不播」。现在按结果判断：同源左键导航统一写 `sessionStorage` 标记后 `location.assign`，新页首绘前
先挂入场类，`pagereveal` 带 `viewTransition` 就把类摘掉（只留原生），没带就留下类播 280ms 入场；**不做旧页离场** ——
旧页先淡到全透明再换文档，中间会空出一截背景，只剩新页从半透明浮上来更连续。中键、新标签、后退仍走原生。
切换机制、层叠约定与两条仓规（不许字符串拼 DOM、不许任何外链）见 [docs/webui.md](docs/webui.md) 第 7 节。

## 8. 部署形态（`本机`）

- 目录 `/opt/douyin-spark-fusion`；`.venv` 独立虚拟环境（不污染系统 Python）。
- systemd 两个 unit：`douyin-spark-fusion-web.service`（webui 常驻，`0.0.0.0:8xxx`）+
  `douyin-spark-fusion-task.timer`（每日定时跑一轮，替代被删掉的 `schedule.yml`）。
- 浏览器复用 C 的方式：本地 `chrome/` 目录或 `CLOAKBROWSER_BINARY_PATH`。
- 每账号的浏览器状态快照落在 `BROWSER_STATE_DIR`（默认 `logs/browser-state`，见 6.2），**等价于凭据**；
  删掉它不影响发送，只是下一个账号首次运行会以全新设备身份重新登录一次。
- 与既有 `douyin-spark.service`（A 的部署，8000 端口）**并存**，端口不冲突。

**2026-10-24 修订（以这段为准；上面保留为上线当天的记录）**

- 机器换了：现在是 `192.168.15.172`（文中 `.224` / `192.168.1.100` 是上线当天的开发机，已退役）。
- 单元名字与节奏都变了：`douyin-spark-fusion-web.service`（面板，`0.0.0.0:8787`）+
  `douyin-spark-fusion-daily.timer` → 同名 `.service`，`OnCalendar=*:0/5` —— **每 5 分钟敲一次门**，
  这一轮到底发不发由 `core/schedule.py` 决策（到点 / 今天已发 / jitter），不再是"一天跑一次"。
  oneshot 每次都是新进程，所以任务侧改代码不需要重启任何东西。
- 两份 unit 都**故意不写 `EnvironmentFile=`**：systemd 会按转义序列解析值，把消息模板里字面的
  `\n` 吃成裸 `n`（消息挤成一行）。`.env` 只由程序用 python-dotenv 读。这是持久约束，不是临时绕过。
- `/opt/douyin-spark-fusion` 自 2026-10-24 起是 **git 工作树**（HEAD 跟 github
  `liboxuanzs/douyin-spark-fusion`）。日常升级 `deploy/update.sh`（fast-forward + 按改动决定
  pip / 单元 / 重启），首次安装才用 `deploy/install.sh`；详见 [docs/deploy.md](docs/deploy.md) 第 7 节。
- 文中引用的 `netinv/` 脚本（审查报告、各类探针、清理脚本）**从未进过版本库**，只存在于当年那台开发机上。
  想复现它们描述的动作，走仓库里真实存在的入口：`logs/runs.jsonl`、`python -m webui --check`、
  `main.py dry-run select|full`、`main.py prune`。

## 9. 验收记录（2026-10-01，实测）

| # | 需求 | 状态 | 证据 |
|---|---|---|---|
| 1 | 修"乐观通过" | ✅ | 会话选择与登录态两处都改成 fail-closed；`tests/test_strict_verify.py`（31）与 `tests/test_douyin_im.py::SelectionFailClosedTests` / `LoginVerdictVetoTests`（8）钉住；`本机` 实测匿名访客 id 不再被判成"已登录"（整轮从 2 分 2 秒降到 3 秒） |
| 2 | 去掉不用的 Actions | ✅ | `.github/workflows/` 只剩 `build-configtool.yml` 且只有 `workflow_dispatch`；`tests/test_deploy_units.py::WorkflowTriggerTests` 钉住"打 tag 不会自动发布" |
| 3 | dry-run | ✅ | 三档 `off/select/full`；`本机` 上三档都真机跑过，账本里 `dry_run` 字段可查；`full` 会真输入并读回校验再清空编辑器 |
| 4 | 限流关键词熔断 | ✅ | `core/limit_guard.py` + `core/quiet_window.py`；`本机` 真机演练过"命中 → 冷却 30 分钟 → 下一轮跳过 → 到期恢复"；空跑不落冷却 |
| 5 | 融入 B 的仪表盘 | ✅ | `webui/`（FastAPI + 手写 CSS/JS，零外网资源）；48 条单测；`.224` 实测 `http://192.168.1.100:8787` 可登录、可手动触发、可看冷却与日志 |
| 6 | 部署 `本机` + 局域网监听 | ✅ | `douyin-spark-fusion-web.service` enabled+active 监听 `0.0.0.0:8787`，与 8000 上的既有 `douyin-spark.service` 并存互不影响 |

自检方式：`netinv/review-report-fusion.md` 里的对抗式审查（F1–F13）。（`netinv/` 里的脚本与报告从未入库，见第 8 节 2026-10-24 修订。）高危/中危已全部修完
（空跑落冷却、冷却时长死配置、活进程被夺锁、Windows 判活、20:30–20:50 撞车、面板误报成功），
低危里 F8（日志轮转）与 F9（删除 `webui/store.py::write_lock` 死代码）也已处理。

**唯一尚未闭环的是"真的发出去"**（F1）：`.env` 里仍是示例账号与占位 cookie，真机上每一轮都停在
`登录已失效（有 sessionid 但服务端不认）`，`sent_ok` 全为 0。要用 `configTool/` 或面板的扫码登录
（见 9.1）重新登录取新 cookie 填进 `COOKIES_<unique_id>`，空跑确认后再
`systemctl enable --now douyin-spark-fusion-daily.timer`。在此之前定时器**刻意保持 disabled**。

### 9.1 面板扫码登录（2026-10-01 追加）

面板现在自己就能换 cookie。抖音的登录二维码是页面里的**内联 base64 PNG**，
所以不需要 B 的 noVNC 桌面、也不需要截图：`core/qr_login.py` 起一个无头 Chromium
（用该账号固定的 `fingerprint`、代理与发送链路一致），把那张图读出来转发给面板，
扫码确认后再用 `core.douyin_im.check_login()` **验证服务端认不认这个登录态**
（不是只看本地有没有 `sessionid` —— 那正是第 1 项要修的漏洞）。

`本机` 实测：`POST /ops/login/start` → 约 7 秒后 `waiting_scan`、二维码 512×512 真 PNG
（`image/png` + `Cache-Control: no-store`），面板里肉眼可扫；取消后 `state=cancelled`，
浏览器控制台零报错。写盘用 `webui/envfile.py`（与 `configTool/env_store.py` 同一口径），
在 `.env` **副本**上实测：85 行注释一字未改、原有变量全在、只新增 `COOKIES_<抖音号>`、
权限 `600`、`utils.config.get_userData()` 能把 cookie 解析回来，真实 `.env` 的 sha256 前后一致；
缺 `sessionid` 或抖音号非法时整笔拒绝且不留备份。

`tests/test_cookie_utils.py`（34）、`tests/test_envfile.py`（34）、`tests/test_qr_login.py`（16）、
`tests/test_webui.py::QrLoginWebUITestCase`（11 条 + 继承来的父类用例）钉住。


### 9.2 目标好友改成"主动读取 + 勾选"（2026-10-01 追加）

**问题**：原来目标好友靠手打名字（`TASKS[].targets` 是字符串数组），发送时靠
`DouyinIM._match()` 去会话列表里比对 —— 那条链最后有一层**子串兜底**
（目标「小明」会命中「小明明」，`how="fuzzy"`）。名字会改、会重名，打错一个字
就可能把火花发给另一个人。用户要求改成 A 的做法：先读列表，再由人勾。

**做法**（借 A 的交互，用 C 自己更强的会话枚举能力实现）：

1. `core/friend_scan.py`（新增，只读、不写 `.env`）：拿该账号的持久化 profile
   起一个无头 Chromium，走 `DouyinIM.wait_ready()` 门禁 → `iter_conversations()`
   从头滚到尾，把**全部**会话读出来（每项带抖音自己算的 `uid` / `sec_uid` / `conv_id`）；
   状态机 `starting/opening/ready/scanning/done/error/cancelled`，可取消、可超时。
2. 面板账号表里的目标好友列换成好友选择器：**读取好友列表** → 轮询进度 → 读完整页刷新 →
   搜索框过滤 → 勾选 → **保存勾选**。写盘仍走 `webui/envfile.py`（就地更新 + 备份 + `600`）。
3. `TASKS[].targets` 支持对象形态 `{"name","uid","conv_id"}`；只有名字时仍写成字符串
   （老配置一字不改继续可用）。
4. `_match(self, item, pending, by_conv=None, by_uid=None)` 改为
   **conv_id → uid/sec_uid → 名字精确 → 子串兜底**，并且
   **带身份（key 以 `conv:`/`uid:` 开头）的目标禁用子串兜底** —— 名字一样但 id 不同
   就是另一个人，宁可报"没找到"（进 `missing`，面板显示"没在会话列表里出现"）。

**为什么不再依赖名字**：`uid`/`conv_id` 是抖音自己算的，勾选那一刻就拿到；
发送链路用同一份 id 回查，就不存在"匹配到同名的人"这种可能。

`tests/test_douyin_im.py`（`TargetRefTests` 8 + `IdentityMatchTests` 7 +
`IdentitySelectionTests` 6）、`tests/test_friend_scan.py`（23）、
`tests/test_envfile.py` 目标往返 7 条、`tests/test_webui.py::FriendPickerWebUITestCase`（11 +
继承来的父类用例）钉住；全量 641 passed / 15 skipped。

**真机曾抓到的 bug**：`FriendScanSession.__init__` 里 `self._started = time.time()`
覆盖了 `threading.Thread` 自己的 `_started`（一个 `Event`），`Thread.start()` 直接抛
`AttributeError: 'float' object has no attribute 'is_set'`，面板点按钮 500 —— 单测没拦住
（没人真的 `start()` 过），已改成 `self._t0` 并补回归测试。

**9.2.1 资料目录错位（真机第二次踩到，已从代码层修掉）**

新账号是"先扫码、后知道抖音号"，于是扫码时的 profile 落在
`logs/login-profile/default/`，而按账号读列表用的是
`logs/login-profile/<抖音号>/` —— 目录对不上，读列表时打开一个空目录，
门禁直接 `gate=LOGGED_OUT`（面板提示"这个账号还没登录"，可 cookie 明明在）。

修法：`QrLoginSession` 新增 `adopt_profile` / `_adopt_profile()`，登录成功后
（`on_success` 返回 `adopt_profile=<账号目录>` 时）**等浏览器关闭再 `shutil.move`
把源目录挂到账号名下**；目标目录已存在就**拒绝覆盖**（换设备重扫时不毁已有登录态），
结果记进 `persist["profile_moved"]`。`tests/test_qr_login.py::AdoptProfileTests`（6 条）钉住。

**9.2.2 "未覆盖折叠组/陌生人组"曾是一句空话**

`_finish_scan()` 原来只要 `scanned_all` 就无条件写 `note="未覆盖折叠组/陌生人组"`。
真机取证（`netinv\fold_probe.py`）：这个账号 `fold_groups()` 返回 `{'fold': 0, 'stranger': 0}`，
两个容器数都是 0，而 API 一轮就报 `messages=23 has_more=False`、扫描正好收齐 23 条 ——
**覆盖 23/23，那句备注纯属吓人**。现改为数据驱动：容器数都为 0 → `note` 留空（面板不显示）；
有值才写"主列表已读完；另有折叠组/陌生人组未包含（N 个分组）"；探不到（页面挂了）仍保守提示。

**真机验收（2026-10-01）**：`netinv\friend_scan_check.py`（在 `本机` 上跑）——
`opening(0s) → ready(3s) → scanning(6s/9s) → done(12s)`，**读到 23 个会话、
`scanned_all=True`、其中群聊 4 个、备注为空**；首页渲染 **23 个好友选项**、
**0 个默认勾选**（还没保存过目标）、**23 个值都带 `uid`/`conv_id`**。

### 9.3 面板一致性返工（2026-10-01，用户实测反馈后）

用户反馈两件事：「点了移除但未生效」「我的浏览器里错位严重」，并要求**审查全部代码，
确保面板和实际工具一致，不得面板一套实际一套**。逐条查出来的真因：

| 反馈 | 真因（实测） | 修法 |
|---|---|---|
| 移除不生效 | 账号确实已从 `TASKS` 里删掉了（用户那次操作是成功的），但 `logs/runs.jsonl` 里还有它 → `store.account_snapshots()` 第 2 段把"上次运行里出现、配置里没有的账号"又变回一张卡；对着这张**幽灵卡**点移除，`envfile.remove_account()` 只会回 `TASKS 里没有 <uid>` | `AccountSnapshot.orphan`；幽灵卡标「已不在配置里」、不给「移除/重新登录」、不计入汇总；`remove_account` 的失败文案说明"它可能只是上一轮运行记录里的账号" |
| 错位严重 | 服务端 `StaticFiles` 不发 `Cache-Control`，浏览器按启发式缓存策略**继续用 9 月 19 日的旧 `app.css` 配新 HTML**（新 HTML 用了 `.account-card` 这些新类）。8 种宽度实测 `scrollWidth` 全部等于视口宽、越界元素 0 —— 布局本身没问题 | 静态文件加 `Cache-Control: no-cache`；模板的 `?v=` 换成 `asset_version()`（`app.css`/`app.js` 的 mtime，必须写成 `{{ asset_version() }}`） |
| 「下次自动触发 12:30」 | 这个数字读 `.env` 的 `TASK_SCHEDULE_TIMES`，而 `本机` 上 `douyin-spark-fusion-daily.timer` 是 **disabled**（`list-timers` 空、`NextElapseUSecRealtime` 空）—— 到点什么都不会发生 | `store.timer_state()` 读 `/etc/systemd/system/**`（启用与否看 `timers.target.wants/` 软链）与单元文件的真实 `OnCalendar`/`RandomizedDelaySec`；没启用画红框「不会自动跑」并给出 enable 命令；时间不一致以单元为准并注明 `.env` 的值 |
| 「本轮结论」数字会变 | 运行记录页复用了首页的 `data-overview-value` 挂点，前端总览轮询按该选择器**全局**覆盖，于是"选中的那一轮"被换成今天合计 | 运行记录页改用 `data-run-value`；`FrontendAgreementTests` 钉住"`data-overview-value` 只出现在首页" |
| 空跑数字像真发 | `DRY_RUN=select/full` 也走选中/回执流程，"确认成功"照样涨 | 首页标"空跑"角标与说明、账号行追加"上一轮是空跑（…）"，前端文案同步 |

排查脚本（`netinv/`，只读）：`consistency_audit.py`（同一条事实的多条解析路径并排）、
`after_poll_probe.py` / `runs_poll_probe.py`（读 → 等 6 秒 → 再读，抓轮询改写）、
`layout_probe.py`（8 宽度 × 3 页面量溢出）、`js_overwrite_audit.py`（JS 覆盖目标 vs 页面挂点）。
`consistency_audit.py` 的结论是**没发现别的不一致**：账号集合/目标/匹配键（`target_refs` 的
`conv:`/`uid:` 键）、cookie 项数与 `sessionid`、配置 15 行、门禁六个 `STATUS_*` 的标签、
core 里 `dry_run`/`quiet_window`/`limit`/`lock` 的支持面，逐条对齐。

### 9.4 第二轮审计与修复（2026-10-01，用户"继续寻找可能的问题并修复"）

用两个**只读**子代理做对抗式审查（面板语义 vs core、错误路径/并发/安全），报告留在
`netinv/audit-panel-core.md` 与 `netinv/audit-error-paths.md`。查出来并修掉的：

| 症状 | 真因 | 修法 |
|---|---|---|
| 三个空跑档位点了**没有任何反应**（400） | 分段控件挂点是 `dry-off`/`dry-select`/`dry-full`，`app.js` 把这个带前缀的值原样提交，而服务端只认 `off/select/full`；旧测试全都直接提交 `"off"` 所以一直是绿的。附带：`const real = value === "off"` 恒假 → 真实发送档位下按钮是柔和色 + 写着「开始空跑」 | 前端 `raw.replace(/^dry-/, "")`；服务端 `normalize_dry_run()` 兜底（老页面被缓存也不会白点）；真机复验 `dry-select` → 202 + 账本 `dry_run=select/source=webui` |
| 面板替 core 补记的失败轮次显示成绿色「成功」 | `normalize_run()` 的失败判定只看每个账号的结果，而"这一轮没跑起来"的记录 `accounts` 是空的；那句兜底文案还会把它说成"账号凭据可能未配置" | 账本自己写的 `status="error"` 直接算失败；账本的 `note`/`error` 优先展示 |
| 不认识的账号状态被静默当成成功 | `status not in STATUS_LABELS → "ok"` —— 最骗人的兜底值 | 原样保留（页面按原码显示 + 黄色 chip） |
| 后台线程崩了却回 202「已开始」 | `start_run_thread` 起线程后直接应答，`run_once` 的返回值被丢掉，账本 0 条记录 —— 用户以为跑了，其实什么都没发生 | `run_in_thread()`：兜 `BaseException`，core 没写账本时补一条 `status="error"` 的记录（含异常文本），结果存进 `_RUN_RESULTS`（留最近 8 条） |
| 扫码登录"搬迁途中"资料目录被判空闲 | `_profile_in_use` 只比会话当时的 `profile_dir`，而搬迁是在浏览器关掉之后才 `shutil.move` 的 | `snapshot()` 增 `adopt_dir`，占用判定同时认它；`core/friend_scan.py::cancel()` 不再立刻置终态，等浏览器真的关掉 |
| 卡在"进行中"的会话让账号永久用不了 | 只回收非 active 的会话 | `SESSION_STUCK_SECONDS = 20 分钟` + `_reap_stuck_sessions()`（先发取消信号再清出，发起新会话前也清一次） |
| CSRF：`same-site` / `Origin: null` / 伪造 Host 都放行 | 只拒 `cross-site`，且 `_expected_origins()` 以**请求自带的 Host** 为基准 | 拒 `same-site` 与 `null`；Host 必须命中本机名字（回环/私网 IP 字面量/机器名/绑定地址/`WEBUI_TRUSTED_HOSTS`），**带点的域名一律不认**（DNS rebinding 必须用域名），短名照常 |
| `/ops/login/qr.png` 未登录回 303 | 浏览器把跳转后的登录页当图片解码，静默失败看不出是没登录 | 与同组接口统一成 401 JSON |
| 运行记录页账号行的数字会变成"今天" | 账号行用了首页挂点 `data-account-overview` | 改用 `data-run-account` |
| 「已发送 380/2」这种 >100% | 配置里的目标数可能比账本少（上一轮发完把 `TASKS` 改小了） | `total = max(total, confirmed + weak + failed)`，四档相加恰好等于 total |
| 弱证据点亮「最近强确认」 | `lastConfirmedAt` 由 strong **或** weak 触发，与说明文字"会话选择 + 回执双确认"不符 | 拆成 `lastConfirmedAt` / `lastWeakAt`，说明随值切换，前端同一句话双写 |

同一批还修了 `configTool`（tkinter 配置器）与面板共用 `.env` 时暴露的两个真 bug：取值闭包被
局部变量 `text` 遮蔽（TASKS 里有手写纯名字目标就 `TypeError`）、同名目标的身份互相覆盖
（`target_meta` 由 `name → 身份` 改成 `name → [身份, …]`，按出现顺序取用）。
`webui/envfile.py` 的 6 个写函数加 `_WRITE_LOCK`/`@_serialized`：40 线程并发写不再丢数据（旧代码丢 39/40）。

**经验**：面板最容易骗人的地方不是"算错"，而是**默认值太乐观**（不认识的 status 当成功、
线程崩了当已开始、disabled 的定时器画个时间）。审计要专门盯这类兜底分支。

### 9.5 清掉一个已删账号的残留（2026-10-01，用户「清掉12345678905吧」）

用户界面上的幽灵卡（「已不在配置里」）来源是**上一轮的运行记录**：账号早已从 `TASKS` 删掉，
但 `logs/runs.jsonl` 里 10 轮的 `accounts[0]` 还留着它，`store.account_snapshots()` 就把它
又变回一张卡。清理按"先看清、再备份、再动、最后补一句真话"四步走，脚本都在 `netinv/`：

1. `inspect_ghost.sh`（只读）：它在 15 轮里的 10 轮、全部位于 `accounts[0]`，形态固定
   （`status=skipped`、`gate=EXPIRED`，07:18 那轮 `TIMEOUT`，`targets=["好友a","好友b"]`）；
   `.env` 里只剩注释行提到它，没有生效配置；`本机` 上也没有它的资料目录。
2. `drop_ghost.sh`：先 `cp -a` 备份，再逐行丢掉引用该 uid 的账号条目与 `missing`/`select_failed` 项，
   其余字段原样写回（15 行不动，10 条账号条目消失）。
3. `annotate_cleaned_rows.sh`：以**清理前的备份**为准，只给当初真的带着它、且已经空掉、
   又没有别的解释的轮次写 `note`：「这一轮的账号条目已经从账本里清理掉了（那个账号已不在
   .env 的 TASKS 里）」—— 否则页面会拿兜底文案去猜"账号凭据可能未配置"。

顺带改掉两处**同类骗人判决**（`webui/store.py::normalize_run()`）：

| 症状 | 真因 | 现在 |
|---|---|---|
| 跑完一轮却没有任何账号结果，页面显示绿色「成功」 | 轮次判决只看"有没有失败"，空账号列表既不是失败也不是跳过 | 新增判决「没有账号结果」（`status="empty"`，黄色 chip）；那条猜原因的兜底文案整条删掉 |
| 每个账号都被门禁拦下（登录失效/掉登录/超时），页面还是绿色「成功」 | 账号级 `skipped` 不影响轮次判决 | 新增判决「账号全被跳过」（`status="blocked"`，要求 `totals["entries"] == 0`）；只要有一个账号真跑了就仍算「成功」 |

真机复验（`本机`）：首页只剩 1 张账号卡（`12345678905`），`12345678905`/「已不在配置里」0 次；
运行记录页 10 行显示「没有账号结果」+ 那句真话，绿色「成功」只剩真正跑过的那一轮
（15:48:27，`dry_run=select`，`source=webui`）。账本备份留在
`logs/runs.jsonl.bak-20261001-155909`（原始）与 `.bak-20261001-160018`（删条目后、补注前）。

### 9.6 「运行配置」可以在面板上改（2026-10-01，用户「我希望运行配置能在webui改动」）

改一个超时原先必须 ssh 上服务器编辑 `.env` 再重启。这次没有新造一套"设置系统"，而是把运行参数
接进面板**早就有的 `.env` 写入口**（扫码存 cookie、勾选目标、移除账号本来就在写 `.env`）。

写入口的三条硬规矩（`webui/settings.py`）：

1. **只写白名单**：`SETTINGS` 里 17 项（空跑档位、消息模板、一言类型、发送间隔、限流熔断、
   禁跑时间窗、代理、浏览器/IM 各类超时与步数、重试次数、日志级别、面板时区）。提交里混进
   `cfg_TASKS` / `cfg_COOKIES_*` / `cfg_WEBUI_PASSWORD` 一律**丢掉**（`collect()` 只遍历白名单）。
2. **值取自 `.env` 原文**：不能用 `store.config_snapshot()` 的加工值（那里超时是 `"120000 毫秒"`，
   写回去就废了）。代理回显走 `store.mask_proxy()`，把打码文本原样提交回来**不算改动**，不会
   把真口令换成字面的 `***`。
3. **不合法就整批不写**：`utils/config.py` 的 `get_config()` 里有 `json.loads(HITOKOTO_TYPES)`、
   `int(float(...))` 一串，写进一个坏值下一轮读配置直接抛异常。所以每项都按 `configTool/models.py`
   的范围校验，跨字段还要 `上限 ≥ 下限`；`.env` 里**现在就坏**的值会在表单上方红框列出。

写盘用 `envfile.update_settings()`：先比出改过的键（**没变的键一个字节都不写**）→ `backup()`
（`.env.bak-<时间戳>`，留最近 5 份）→ 逐个 `_set_key()` 就地改行（注释、空行、其它变量顺序原样）
→ `_lock_down()` → 立刻 `envfile.reload()` + `store.refresh_env()`，下一轮任务就用新值。

| 症状 | 真因 | 现在 |
|---|---|---|
| `.env` 里没有的下拉项被"顺手"写死 | 浏览器遇到"当前值不在选项里"默认选中**第一项**，用户不动它直接保存就凭空写出 `DRY_RUN=off` | 缺项时下拉框最前面插空值占位项「（.env 里没有，工具用默认值 X）」，它与 `.env` 原文一致 → 判定"没动过"，一个字节都不写；认不出的当前值同样插在最前，不会被第一项顶掉 |
| 正在编辑的配置被 10 秒轮询改回旧值 | 表单字段若复用 `data-overview-value` 挂点，总览轮询会往输入框里写 `.env` 的旧值 | 表单用 `data-config-input`，`app.js` **完全不认识**它；`FrontendAgreementTests::test_配置表单不会被总览轮询改写` 钉住 |
| 保存后页面还是旧值 | 常驻进程里 `utils.config.get_config()` 是**首次调用即冻结**的模块级对象 | 保存成功后 `envfile.reload()` + `store.refresh_env()`；`/ops/account/targets` 早就是这么做的 |

### 9.7 「今日发送进度」那一格重做（2026-10-01，用户「截图处的ui明显不对」）

用户截的是**浅色主题**下的首页第一格：一个 68px 的 conic-gradient 圆环，环心里塞两行字
（`100%` + `已发送`），右边再写一遍"今日发送进度 / 1/1 / 强确认 1…"。量过七档宽度（1440→820）
与四种缩放，圆环尺寸与溢出都正常（`scrollW == clientW`），所以不是"错位"，是**这一格本身设计得不对**：

* 同一个数字说两遍（环里 `100%`、旁边 `1/1`），而"已发送"三个字被挤在 68px 的环心里；
* 浅色主题下圆环是"橙圈 + 白心 + 黑字"，同一行右边却是白底 KPI 卡，看上去像告警块；
* 同一页别处的"进度"都是**数字 + 进度条**（账号卡就是这么画的），只有它是个环。

改法与 KPI 卡同构：标签行（`今日发送进度` + 空跑角标）→ 大数字 `1/1` → 进度条（橙段=强确认、
黄段=弱证据，宽度由服务端 `summary.strong_pct`/`weak_pct` 渲染，前端只按同一份百分比改
`width`）→ 帮助行。圆环 `span.progress-badge` 与其全部 CSS 删除，`app.js` 里写
`--strong-pct/--weak-pct/--ring-gap` 三个 CSS 变量的代码换成写两个 `<i>` 的宽度
（`data-overview-bar-strong` / `data-overview-bar-echo`），`test_首页今日进度条前后端都画`
把两边的挂点钉在一起。

### 9.9 面板口令可以在面板里自己改（2026-10-02，用户「我不希望是用密钥登录…改成用密码登录…密码我可以在 webui 自行设定」）

先把话说清：**登录框从来没要过"密钥"**。`WEBUI_SESSION_SECRET` 是会话 cookie 的签名密钥，
`.env.example` 与 `docs/webui.md` 却把它写成"会话签名密钥"，看着就像登录凭据 —— 用户的抱怨
多半就是从这行注释来的。登录只认 `WEBUI_PASSWORD`（明文）或 `WEBUI_PASSWORD_HASH`（哈希优先）。

真正的缺口是"**口令只能在服务器上改**"（`python -m webui.hash_password` 写完还得重启）。
现在面板多了一个写入口 `POST /ops/password`，首页「访问口令」折叠段 + 侧栏「管理 → 访问口令」：

1. **两个键永远一起写**。选了哈希就把 `WEBUI_PASSWORD` 清成空行，反之亦然。只写一个键的话，
   另一个键的旧值还躺在 `.env` 里，而校验是"哈希优先" —— 用户会得到"改了口令却登不上"。
   写空的 `KEY=` 不是留白：`load_dotenv(override=True)` 会顺带把进程环境里残留的旧值清成空串。
2. **改完所有旧会话立刻失效**。会话 token 里多装一个 `config.credential_fingerprint()`
   （哈希/明文的 SHA256 前 16 位），`auth.read_token()` 对不上就返回 None —— 无服务端状态，
   也不用维护"已登出列表"。代价是**自己也掉线**，所以成功后就跳登录页让用户用新口令登一次。
3. **口径不许含糊**。默认存明文（用户明说不要"密钥"，且 `.env` 本来就是 600、里面还躺着
   Cookie）；想存哈希就在表单的下拉里选。校验（8–128 字符、无换行、无英文单引号 `'`）不过
   就一个字节都不写；页面与 JSON 里**从不回显口令或哈希**，只说"存的是哪种形式、来自 `.env`
   还是进程环境、会话能撑多久"。
4. 忘了口令进不去：服务器上 `.venv/bin/python -m webui.reset_password`（走同一个写入口再重启服务）。

| 症状 | 真因 | 现在 |
|---|---|---|
| 以为"登录要用密钥" | `WEBUI_SESSION_SECRET` 的注释与文档把它叫"会话签名密钥" | 文档/`.env.example` 都点明它只是 cookie 签名密钥，登录只认 `WEBUI_PASSWORD(_HASH)` |
| 面板改了口令，老口令还能登 | 只写了一个键，另一个键的旧值还在，而校验哈希优先 | `set_webui_password()` 两键同写；`credential_fingerprint()` 进 token，旧会话当场失效 |
| 口令里有 `#`、空格、`$` 被截断 | dotenv 默认写法会把 `#` 当注释、把值截到第一个空白 | 口令专用 `_set_key_quoted()`（`quote_mode="always"`）写成 `KEY='值'`，因此**值里不许有英文单引号**（校验里挡掉）；`test_口令里的井号空格美元符号都要原样活下来` 钉住 |
| 口令表单被 10 秒轮询改回旧值 | 与「运行配置」同一个坑 | 表单挂点用 `data-password-*`，`app.js` 完全不认识（`test_口令表单不会被总览轮询改写` 钉住） |

### 9.10 消息模板的换行在真机上被吃掉了（2026-10-03，用户「原本的发送规则…在实际发送中部分失效，如换行」）

`[API]` 替换是好的，坏的只有换行。查下来不是"漏了实现"，而是**值在到达代码之前就被改过**：

1. `MESSAGE_TEMPLATE` 里的换行规则是"写**字面两个字符** `\n`"，`split_message_lines()`
   在发送前把它还原成真换行（`core/douyin_im.py:887`，9 组样例钉在 `tests/test_douyin_im.py`）。
2. 单元文件里写着 `EnvironmentFile=-/opt/douyin-spark-fusion/.env`。systemd 解析 environment
   file 时按**转义序列**处理值，于是 `\n` 被吃成裸 `n`。
   `netinv/probe_systemd_env.sh` 用四段对照把这件事钉死：
   `systemd-run --property=EnvironmentFile=…` → `[加一]n——`；
   `bash -c 'set -a; . ./.env'` 与 `python-dotenv` → `[加一]\\n——`（原样）。
3. `main.py` 当时是 `if os.path.exists(".env"): load_dotenv(".env")` —— **`override` 默认
   `False`**，所以 systemd 那份坏值赢过文件。（`utils/config.reload_env()` 用的是
   `override=True`，但任务路径上在 `get_config()` 之前没人调它。）
4. 真机实证（`logs/douyin-spark-fusion.log`，2026-10-02 20:38 那一轮）：
   `消息模板: [盖瑞]今日火花[加一]n—— …——n[API]`，`[SEND] 合成输入=[{'grew': 64, …}]`
   只有一个 dict（逐行输入会每行一条、彼此间隔 250ms）→ 整条消息**作为一行**发出去，
   行尾还挂着 `n`。三条消息都 `via=http` 成功，只是格式错 —— 这类故障不会报错，只会难看。
5. 顺带挖出第二个洞：`dry-run=full` 存在的意义是"证明文本真的进了编辑器"，
   但 `_readback_matches()` 两边都 `re.sub(r"\s+","")` 再比字符 —— `"甲\n乙"` 与 `"甲乙"`
   归一化后相等，**换行丢了也判 ✅**。所以这条规则"验证过"其实从来没被验证。

现在的口径：**`.env` 是配置的唯一真相**（面板预览读它、人改它、程序也读它）。

* `.env` 只由程序自己读：`utils/config.load_env_file(path, *, override=True, keep=PROCESS_WINS_KEYS)`
  —— 绝对路径、文件优先、返回"被文件纠正过的键"（`{KEY: (进程原值, 文件值)}`）供调用方告警；
  `main.py` 与 `python -m webui` 都走它。两份 unit 都不再写 `EnvironmentFile=`（`RUN_SOURCE=timer`
  这类"这一轮怎么跑"的值仍由启动方给，`keep` 里的键读完原样还回去）。
* 读回校验**按行**比（`line_list()`），并返回读回原文；读回改用 `innerText`
  （Draft.js 的软换行是 `<br>`、硬换行是兄弟块，`textContent` 两种都不补换行符）。
* 合成输入的换行改成**自校验 + 三级退化**：派发 `keydown/keyup` → `execCommand('insertLineBreak')`
  → `execCommand('insertText', '\n')`，每级用行数确认，结果记在 `brk`；
  三种都不行时打 WARNING「换行没插进去：这条消息会挤成一行」，**但不抛异常** ——
  宁可发一行也不能漏续火花，前提是绝不说假话。
* 日志把模板用 `!r` 打出来（`'…\\n…'` 与裸 `n` 一眼可辨），发送前把
  `待发送文本 … → N 行` 打出来 —— 下一回再出这种事，看日志就够了。

| 症状 | 真因 | 现在 |
|---|---|---|
| 面板预览三行，发出去一行、行尾挂 `n` | unit 用 systemd `EnvironmentFile=` 解析 `.env`，反斜杠被吃；`load_dotenv` 又不覆盖进程环境 | 两份 unit 不写 `EnvironmentFile=`；`.env` 只由 `utils/config.load_env_file` 读（文件优先 + drift 告警）；`test_env_is_not_parsed_by_systemd` 钉住"谁都不许加回来" |
| `dry-run=full` 说"文本对上了"，其实换行丢了 | 归一化把空白全抹掉再比字符 | `_readback_matches` 按行比并返回原文；`ReadbackMatchesTests` 里"换行丢失必须不匹配"是核心回归 |
| 合成 keydown 派发完就当插好了 | `softBreak()` 只发 keydown/keyup，Draft 认不认没人验证 | 行数自校验 + 三级退化 + `brk` 上报；`InputVerificationContractTests` 钉住这三条 |
| 面板「运行配置」的模板预览和真发的不一样 | 预览读文件、任务读进程环境（可能已被 systemd 弄坏） | 只有一个来源（文件）；不一致时启动就打 WARNING 报出两边 |

### 9.11 "清空输入框"只清了 DOM：Draft.js 的 EditorState 没动（2026-10-03，同一轮排查的第二个洞）

换行修好之后，真机 `dry-run=full` 立刻把下一个洞露了出来。它长这样：

1. 输入成功、读回**完全正确**（`读回=74字/3行 期望=3行 匹配=True`），
   但同一行日志后面写着 `已清空=False`。多出来的 5 个字是每行行尾各一个
   `\u200b`（Draft 在软换行处塞的零宽空格，`str.strip()`/JS `trim()` 都删不掉）。
   这一层已经修掉（`ZERO_WIDTH_CHARS` + `_strip_invisible`，`JS_EDITOR_EMPTY`/
   `JS_EDITOR_TEXT`/`lineCount` 三处都先删零宽字符）。
2. 真正的问题在清空：`JS_EDITOR_CLEAR` 只做"全选 Range + `document.execCommand('delete')`"。
   **Draft.js 的内容真相在它自己的 `EditorState` 里**，这条命令只抹 DOM —— 当场判空确实
   是空的，可下一次输入时 React 重渲染又把旧文本带回来。真机证据：判定失败触发
   `core/tasks.py:163` 的"重试一次"，第二次输入后读回变成 **5 行 148 字**：
   `['[盖瑞]今日火花[加一]', …, '…一言' + '[盖瑞]今日火花[加一]', …]` —— 上一遍的三行
   原封不动地在中间又出现了一遍。三个目标全一样。
3. 危险的不止空跑：真发那一路"发送失败 → 重试一次"（`core/tasks.py:160-177`）也靠
   "编辑器空不空"决定要不要清残留。判错就会把同一条消息**叠着发两遍**给好友。

现在的口径：**"清空"必须走 Draft 自己认的路，并且验证过才算清空**。

* `JS_EDITOR_CLEAR` 逐级试：① 全选 + 合成 **Backspace**（和软换行同一条路 —— React 的
  `onKeyDown` 认合成事件，实测 `brk='key'`）→ ② 全选 + `execCommand('delete')`（兜底）；
  每级都用**删掉零宽字符后的可见文本**自校验，返回 `{ok, how, left, tries}` ——
  调用方必须看 `ok`，而不是"我调用过清空"。
* `DouyinIM._clear_editor(tries, settle_ms, distrust_empty)`：清完等 250ms 再复查，
  读到的不是空就再清一遍；`how in ('key','cmd')` 却复查失败会打 WARNING 说明
  "Draft 的 state 没跟着变"。**`distrust_empty`**：上一轮判定失败留下的残留，
  "看着是空的"不能信（正是上面那个坑）—— 先插一个字符逼 Draft 走一次状态更新，
  藏在 state 里的残留会跟着重渲染现形，下一轮一起清掉。
* 本进程记账 `self._editor_dirty`：打过字就记上，只有"验证通过的清空"或"回执说发出去了"
  才勾掉。发送前若 `dirty` 或 DOM 判空为假 → 先清空；**清不掉就这一条不发**
  （`reason='editor_not_cleared'`）—— 叠发两条比漏发一条更糟。
* 真跑前也读回一次（与 `dry-run=full` 同一套判据），但**只在有强证据时拦**：
  `_residue_echo()` 判"同一条消息出现了两遍"（首行压成一条后数出现次数 ≥2，首行短于
  4 字不算），命中就 `reason='editor_residue'` 不点发送并顺手清残留。
  刻意**不拿"读回 ≠ 期望"当拦阻条件** —— 排版差异误判的代价是整天不发（比叠发更亏）。
* 四个编辑器 JS（读回/判空/清空/输入）共用同一组选择器（`JS_EDITOR_SELECTORS`，
  5 个、同一个顺序）。老实现里判空/清空只认 2 个、读回/输入认 4 个 —— 列表一分叉就会
  "读到的是 A、清的是 B"，而且从日志上几乎看不出来。

| 症状 | 真因 | 现在 |
|---|---|---|
| 空跑里"清空成功"了，下一次输入却把上一条又打了一遍（读回 5 行 148 字） | `execCommand('delete')` 只抹 DOM，Draft 的 `EditorState` 没变，重渲染时旧文本回来 | 清空先走 Draft 的键盘路径（全选 + 合成 Backspace）再退到 `execCommand`；`_clear_editor` 复查 + 重试 + `distrust_empty` 逼重渲染；`ClearEditorTests` 用假页面钉住"JS 说 ok 但读回还有字 → 不算清空" |
| 重试时可能把同一条消息叠着发两遍 | 清空的成功与否只看"我调用过"，没人验证 | `_editor_dirty` 记账 + 清不掉就不发（`editor_not_cleared`）+ 发送前 `_residue_echo` 拦"同一条消息两遍"（`editor_residue`），两条都有单测 |
| `dry-run=full` 明明读回对了却判"不一致" | Draft 在软换行处塞 `\u200b`，`strip()`/`trim()` 都删不掉 | `ZERO_WIDTH_CHARS` 统一先删；`test_zero_width_filler_never_counts_as_content` 钉住三处 JS |
| 读到的是 A、清的是 B | 判空/清空 2 个选择器、读回/输入 4 个 | 共用 `JS_EDITOR_SELECTORS`；`test_all_editor_js_share_the_same_selectors` 钉住"数量和顺序一致" |

## 10. 上线后的缺陷修复（2026-10-24）

按"这一条会不会把话发错 / 发丢"重新审了调度、登录、回执三块，修掉七处。
共同点都是**错的方向**：宁可多等一轮、多报一次错，也不能把"不确定"报成"已发送"。

| # | 位置 | 症状（线上可见） | 现在 |
|---|------|------------------|------|
| 1 | `core/tasks.py` `runTasks` 的 `force` 分支 | 面板「立即运行」(`force=True`) 重跑时，只要今天有 error 记录（登录失效 / 一条没发）就回「今天都已经发过了」，一条都不重试 | 只有 `status=ok` 才算"今天已发"；error 与试满上限的 skipped 会被真正重跑。真成功记录照旧跳过（防重复发送这条不能破） |
| 2 | `core/strict_verify.py` `verify_receipt` | 服务端回执明确失败，但页面先画出气泡 → 判 `ok=True/medium`，调用方 `sent_ok += 1`，当天不再补发 | `http_ok is False` 先返回 `receipt_failed` / `conflict`；DOM 气泡只在**完全没有 HTTP 回执**（可能走 WS）时兜底 |
| 3 | `core/douyin_im.py` `_walk` 的 `finally` | 调用方"目标全找到"写进 `_walk_stat` 的 `all-found` 被覆盖成 `caller-break` → `scanned_all` 永远 False，账本和面板整天说"扫描未完成，missing 不代表不存在" | 保留 `all-found`；只有真被外部打断才记 `caller-break` |
| 4 | `core/qr_login.py` `_watch` | 超过"二维码 TTL + 420s"就强杀：第 290 秒才扫码的人刚进验证码页就被判"会话卡住"，`CODE_MAX_ATTEMPTS` 的三次机会一次都轮不上 | 人工阶段（`WAITING_CODE`/`VERIFYING`）另给**有界**预算：`3 ×（等码 300s + 等放行 60s）+ 宽限`，从第一次进入人工阶段起算；机器阶段照旧按 TTL 收尾 |
| 5 | `core/qr_login.py` `_wait_for_code` | `_code_event.clear()` 排在解锁之后：恰好落进"解锁 → clear"窗口里提交的验证码被抹掉，这一轮之后只等事件、不再读值，白等 300s 报 `code_timeout` | clear 移进锁内且先于读值 |
| 6 | `core/tasks.py` `do_user_task` 收尾 | `context.close()` 抛错（浏览器已崩 / CDP 超时）→ 异常冒到 `runTasks`，本轮已发成功的记录连同 `sent_ok`/`entries` 一起丢，记成 `error / 0 条`；用户照提示重新扫码后当天补发一遍，好友收到两条 | close 也包 `try/except`：收尾**永远**不许改变本轮结果 |
| 7 | `core/limit_guard.py` `_write` | 固定 `.tmp` 名，timer 与面板两个进程写同一个 inode，可能发布半截 JSON；`read()` 解析失败静默返回 None → 冷却被当成"没有"，熔断失效还接着发 | `tempfile.mkstemp` 随机名 + `os.replace`，失败清理残渣 |
| 8 | `webui/envfile.py` `dotenv_hazard` | 值以引号开头时面板说"保存成功"，dotenv 却整条键读不到（模板回落内置默认值）；成对引号则被悄悄吃掉 | 首字符是 `"` / `'` 直接拒收 |

每处各配一条回归测试：`test_force_retries_an_error_record_instead_of_calling_it_sent`、
`test_failed_receipt_beats_a_dom_bubble`、`test_all_found_by_the_caller_stays_all_found`、
`test_the_human_code_stage_gets_its_own_budget` / `test_the_human_stage_is_still_bounded`、
`test_wait_for_code_clears_the_event_under_the_lock`、
`test_关闭上下文抛错也不许吃掉这一轮的成绩`、`test_write_is_atomic_via_tmp`（加验"无残渣"）、
`test_a_leading_quote_is_refused`。

**故意没改**：`TASK_RETRY_TIMES` 的重试对"20s 内没拿到可信回执（`unknown`/`no_evidence`）"
也会重发整段 —— `core/douyin_im.py` 自己写着"没回执 ≠ 失败（可能走 WS）"，但重发确实可能让
好友收到两遍。这是**产品取舍**（漏发 vs 叠发），默认保持现状；要改成不重试，在
`core/tasks.py` 的重试条件里排除 `unknown`/`no_evidence` 即可，别顺手改。

## 11. 抖动改成前后对称（2026-10-24，用户要求）

用户反馈："真实发送时间全部都是比我设置的要往后，我更希望在设置时间左右偏移。"

原因不是 bug，是设计：`jitter_minutes()` 只返回 `0..span`，`due_at = 设定时间 + offset`，
再叠上门铃粒度（抖动 15 → 每 5 分钟敲一次），实际落地永远在 `[设定, 设定+20]`。
而 `window_text()` 一直写着"抖动 ±15 分钟"——文案和实现本来就不一致。

**改动**：

- `core/schedule.py: jitter_minutes()`：取模窗口 `span + 1` → `2 * span + 1`，再减 `span`，
  返回 `-span..+span`。仍是 `sha256(账号|日期|span)` 纯函数，同一天同一账号恒定。
- `core/schedule.py: plan_for()`：`due` 夹进当天 `[00:00, 23:55]`。以前只夹上界
  （防推过零点导致整天不发），现在下界也必须夹：设定 00:05 + 负偏移会落到昨天，
  `select()` 拿今天的日期比昨天的时刻，当天就永远"没到点"。
- 文案对齐：`webui/settings.py`（原写"到点后再随机推迟 0~N 分钟"）、`docs/deploy.md`、
  `README.md`、`dashboard.html`、`configTool/models.py` 注释。

**效果**：设定 17:20、抖动 15 → 落地窗口 `[17:05, 17:40]`（门铃粒度最多再晚 5 分钟），
期望值约 17:22；设定 20:30 → `[20:15, 20:50]`。门铃间隔 `tick_minutes() = 抖动/3`
（15 → 5 分钟）没变。

**回归测试**：`tests/test_schedule.py` 的 `test_offset_is_symmetric_around_the_set_time`、
`test_due_at_can_land_before_the_configured_time`、
`test_jitter_never_pulls_the_send_into_the_previous_day`。
