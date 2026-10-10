# 仪表盘（`webui/`）说明

融合版自带一个**只读为主 + 一个手动触发按钮**的网页仪表盘：看运行结果、看登录门禁、
看限流冷却、看日志尾部，并能在面板上直接空跑或真跑一轮。部署步骤见 [deploy.md](deploy.md)。

界面**直接来自 B（`halfwaystudent/douyin-sparkflow`）的原始 `webui/` 代码**：模板骨架
（`base.html` / `dashboard.html` / `send_console.html` → `runs.html` / `logs.html` / `login.html`）、
`static/app.css`、`static/app.js`（前 378 行：主题、移动端导航、确认框、分段控件、总览轮询）
与 vendored 的 `static/lucide.min.js` 都是原件，没有另起一套样式。移植时只做了两件事：

- **砍掉 C 没有的能力**：B 的交互式登录桌面（noVNC）、好友选择器、账号增删改、代理重启、
  调度编辑、用户管理、改密码、补发/全部重发 —— 这些按钮在 C 里没有对应实现，全部删除，
  不留"点了没用"的装饰件。
- **补上 C 自己的东西**：空跑三档选择器、冷却倒计时、禁跑时间窗横幅、触发来源、逐目标的
  强确认/弱证据/需处理/未尝试四档明细（`webui/viewmodel.py` 把账本翻译成 B 的模板契约）。

判断准则：**页面上的每个按钮都必须对应一条真实路由与一个真实能力**，有测试钉住
（`tests/test_webui.py::test_每个表单动作都指向真实路由`）。

## 1. 它不做什么（设计边界）

- **不碰浏览器依赖**：`webui/` 任何模块都不 import `playwright` / `cloakbrowser`。
  只有真正点"立即运行"时，才在**请求处理函数内部**惰性 `from core.tasks import runTasks`；
  依赖缺失时返回 **503 + 中文说明**，进程不崩（`webui/app.py:171` `missing_browser_modules()`）。
- **不写 `.env`、不改配置**：面板只读展示配置；改配置请编辑 `.env` 后
  `systemctl restart douyin-spark-fusion-web.service`（配置在进程内只读一次）。
- **不落 cookie 值**：账号区只显示"已配置 N 项 cookie"，页面与 JSON 里都不会出现 cookie 内容。
- **零外网资源**：模板与静态文件里没有任何 `http(s)://` 引用，完全离线可用。

## 2. 启动

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python -m webui                 # 读取仓库根 .env 后启动
.venv/bin/python -m webui --check         # 只校验配置，不启动（退出码 0/2）
.venv/bin/python -m webui --no-env-file   # 忽略 .env，只用当前环境变量
```

常用参数：`--host`、`--port`、`--log-level`、`--reload`（开发用）。

**绑非回环地址且没配口令时会拒绝启动**（退出码 `2`，stderr 有中文原因）——这是故意的，
不允许把能触发真实发送的页面裸奔在局域网上。

## 3. 环境变量

| 键 | 默认 | 说明 |
|---|---|---|
| `WEBUI_HOST` | `127.0.0.1` | 监听地址；`0.0.0.0` = 局域网可访问（需配口令） |
| `WEBUI_PORT` | `8787` | 监听端口 |
| `WEBUI_PASSWORD` | 空 | 明文口令（方便临时用；不建议长期留）。**登录后在首页「访问口令」里就能改** |
| `WEBUI_PASSWORD_HASH` | 空 | `pbkdf2_sha256$<轮数>$<salt>$<digest>`，**优先于明文**；面板里也能改成存这种形式 |
| `WEBUI_SESSION_SECRET` | 随机 | 会话 cookie 的**签名密钥**（不是登录口令，也不要在登录框里填它）；缺省时随机生成 + 警告，**重启后会话失效** |
| `WEBUI_SESSION_MAX_AGE` | `28800` | 会话有效期（秒） |
| `WEBUI_DEFAULT_DRY_RUN` | `off` | "立即运行"默认选中的档位（`off`/`select`/`full`）；只是默认值，点按钮前仍可在面板上改，非法值回落 `off` |
| `LOGIN_PROFILE_DIR` | `logs/login-profile` | 扫码登录用的浏览器资料目录（等于凭据，别提交/共享） |
| `QR_LOGIN_TTL_SECONDS` | `300` | 二维码有效期；超时作废，需重新开始一次 |
| `FRIEND_SCAN_TIMEOUT_SECONDS` | `300` | "读取好友列表"单次扫描超时（秒）；超时标成读取失败，已读到的部分仍可用 |
| `WEBUI_COOKIE_NAME` | `dsf_webui_session` | 会话 cookie 名 |
| `WEBUI_AUTH` | `pass` | 设成 `off` 关闭认证；**只在回环地址上被接受** |
| `WEBUI_TZ` | `Asia/Shanghai` | 页面上时间戳的显示时区 |
| `TASK_SCHEDULE_TIMES`（别名 `WEBUI_TASK_SCHEDULE`） | `20:30` | 只是用来在页面上算"下次定时"，不控制真实调度；**要和 timer 的 `OnCalendar` 保持一致** |
| `RUN_LEDGER_FILE` | `logs/runs.jsonl` | 运行账本（面板读它画结果） |
| `RUN_LOCK_FILE` | `logs/run.lock` | 运行锁（判断"正在运行"） |
| `RATE_LIMIT_FILE` | `logs/rate_limit.json` | 熔断冷却状态 |
| `OPS_LOG_FILE` | `logs/app.log` | "日志"页读的文件；systemd 部署下指向 `logs/douyin-spark-fusion.log` |

生成口令哈希：

```bash
.venv/bin/python -m webui.hash_password            # 交互输入
.venv/bin/python -m webui.hash_password '我的口令'  # 直接给
.venv/bin/python -m webui.hash_password --random   # 生成随机口令并打印
echo -n '我的口令' | .venv/bin/python -m webui.hash_password --stdin
```

输出一行 `pbkdf2_sha256$480000$<salt>$<digest>`（PBKDF2-HMAC-SHA256，轮数写在串里，
所以以后调轮数不会让旧哈希失效；早期 3 段格式仍能校验）。

## 4. 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 总览页（首页，需登录）：状态横幅、今日发送进度、KPI、账号状态、立即运行、运行配置（可改） |
| GET | `/ops/runs` | 运行记录页：历史轮次表 + 每个账号的四档逐目标明细（`?run=<run_id>` 选中某一轮） |
| GET/POST | `/login` | 登录页 / 提交口令（带 CSRF 校验） |
| POST | `/logout` | 登出（303 回登录页） |
| GET | `/api/ops/overview` | **B 的 `app.js` 每 10 秒轮询的 JSON**（未登录 401） |
| GET | `/api/overview` | 旧版完整快照 JSON（字段更多，给脚本/排障用） |
| GET | `/api/runs/{run_id}` | 单轮明细（按需取） |
| GET | `/ops/logs` | 日志页（带过滤与自动刷新） |
| GET | `/ops/log-tail` | 日志片段（供前端轮询替换正文） |
| GET | `/ops/logs/download` | 下载完整日志（`Cache-Control: no-store`，文件不存在 404） |
| POST | `/ops/run-now` | 表单 `dry_run=off\|select\|full`，后台线程跑一轮。**双语义**：浏览器表单（`Accept: text/html`）成功 303 回首页并显示提示、失败也 303 带原因（含禁跑窗口/已有锁）；`curl` 等 JSON 调用维持状态码语义（409 / 503 / 202），带 `?format=json` 可强制 JSON |
| POST | `/ops/login/start` | 表单 `unique_id`/`username`/`fingerprint`，起一次无头扫码登录（202；已有会话在跑则 409）。**不收目标好友** —— 目标一律在好友选择器里勾选 |
| GET | `/api/ops/login` | 扫码进度快照（面板每 2 秒轮询）：状态、提示、二维码序号、账号信息、写盘结果 |
| GET | `/ops/login/qr.png` | 当前二维码 PNG（`Cache-Control: no-store`）；还没出码时 404 |
| GET | `/ops/login/shot.png` | 二次验证页的截图（风控账号扫码后抖音会甩一道「身份验证」）；没有截图时 404 |
| POST | `/ops/login/code` | 表单 `code`：把用户收到的**短信验证码**填进正在等它的扫码会话（200；没在等验证码则 409） |
| POST | `/ops/login/cancel` | 取消当前扫码登录（关掉浏览器） |
| POST | `/ops/friends/scan` | 表单 `unique_id`/`fingerprint`，起一次"读取好友列表"（202；已有读取在跑则 409） |
| GET | `/api/ops/friends` | 好友列表读取进度（面板每 2 秒轮询）：状态、已读条数、是否扫到底。**不返回会话明细**（明细随页面重新渲染下发） |
| POST | `/ops/friends/cancel` | 停止当前读取（200；没有在跑的则 409） |
| POST | `/ops/account/targets` | 表单 `unique_id` + **`picked`（可重复，每个值是一小段 JSON `{"name","uid","conv_id"}`）**，保存勾选结果到 `.env` 的 `TASKS`；`picked` 为空时回落到旧的 `targets` 文本字段（每行/逗号分隔的名字，给命令行与老配置用） |
| POST | `/ops/account/remove` | 移除账号（连 `COOKIES_<抖音号>` 一起删） |
| POST | `/ops/config/save` | 保存「运行配置」表单：只认 `webui/settings.py` 白名单里的键，写前备份 `.env`；`?format=json` 走 JSON 报错 |
| POST | `/ops/password` | 改面板自己的访问口令：字段 `current`/`new`/`confirm`/`store`（`plain`\|`hash`）。`WEBUI_PASSWORD` 与 `WEBUI_PASSWORD_HASH` **两个键一起写**（不用的那个写成空行），成功即**所有旧会话立刻失效**（303 回登录页）；`?format=json` 走 JSON |
| GET | `/healthz` | 免登录健康检查 `{"ok":true}` |
| — | `/static/*` | 样式与脚本（本地文件） |

## 5. 面板上能看到什么

**总览页 `/`**

- **状态横幅**（按紧急程度排序）：风控冷却中、禁跑时间窗内、缺少浏览器依赖、正在运行、
  运行锁未释放（只在锁文件还在、进程已退出时提示）、上一轮整轮跳过、账号被阻断。
- **今日发送进度环**：强确认段 + 弱证据段（两段百分比由服务端算好，前端只填色），
  旁边是"已发送 N/M"、最近强确认时间、需要处理数、下次自动触发时间与定时说明。
- **KPI 四卡**：确认成功（强确认）、弱证据、失败或阻断、未尝试（待发送）。
  C 没有 B 的"仅回执"这一档，所以那张卡不渲染（不是隐藏，是压根不存在）。
  "本轮已发送"不是独立一张卡，而是进度环旁边的那个数字。
- **账号与目标好友**：每个账号一张卡片（不是表格），卡片头是名字 + 抖音号/指纹/cookie 项数 +
  状态标签，右侧是「明细 / 重新登录 / 移除」；下面一条进度条与三个数字（强确认 / 需处理 /
  待发送），异常原因直接列在卡片里。同一个账号**只渲染一次** —— 总览数据与扫码登录数据在
  `webui/app.py::merge_account_rows()` 里按 `unique_id` 合并成一张卡。点进运行记录页看明细。
- **立即运行**：**整页唯一的动作按钮**。三档空跑用分段控件选，按钮文字与配色随档位变化
  （真实发送 = 红色，空跑 = 柔和色）；被阻断时按钮禁用并在上方列出原因，不会给一个按了没反应的按钮。
- **运行配置**（折叠）：**可以直接改**。表单里每一项的当前值取自 `.env` **原文**（不是
  `store.config_snapshot()` 那种加工过的展示值 —— 那里超时是"120000 毫秒"，写回去就废了）。
  改完点「保存到 .env」：先备份（`.env.bak-<时间戳>`，留最近 5 份）→ 就地改行 → 立刻重读，
  定时任务与「立即运行」下一轮就用新值。只写白名单里的键（`webui/settings.py` 的 `SETTINGS`）：
  注释、变量顺序、`TASKS` 与 `COOKIES_*` **一个字节都不动**；没改过的项也一个字节都不写。
  任何一项不合法就**整批不写**，原因显示在那一行；`.env` 里现在就写坏的值会在表单上方用红框
  列出来（否则工具下一轮读配置直接抛异常）。账号与口令不在这个表单里（走扫码登录）。
  下面还有只读的定时时间（以 systemd 单元为准）、账本/锁/冷却/日志路径与消息模板预览。
- **访问口令**（折叠）：**面板自己的登录口令在这里改**，不用去服务器上碰 `.env`。左侧栏
  「管理 → 访问口令」也能直接跳进来。表单要填当前口令 + 新口令（两遍）+ 保存方式：
  - 保存方式 `明文`（默认）＝ 写 `WEBUI_PASSWORD`，`.env` 里能直接读到，忘了可以用眼睛看回来；
  - 保存方式 `哈希` ＝ 写 `WEBUI_PASSWORD_HASH`（`pbkdf2_sha256`），文件里看不到原口令。
  - **两个键永远一起写**：选了哈希就把明文那行清成空。否则"哈希优先"会让面板里改了的口令
    登不上（老明文还在）。改完写前备份 `.env`，并**立刻让所有旧会话失效**（包括当前这个），
    所以提交后会跳到登录页让你用新口令登一次 —— 其它设备上开着的页面也一样下线。
  - 页面上**从不回显口令或哈希**（连 cookie 都只显示"56 项"这种计数），只说明"现在存的是
    哪种形式 / 来自 `.env` 还是进程环境 / 会话能撑多久"。
  - 口令忘了、面板进不去：在服务器上跑 `bash netinv/reset_webui_password.sh`（不带参数就
    生成一个随机口令并打印），它会走同一个写入口 + 重启 `douyin-spark-fusion-web.service`。
- **扫码登录**（折叠）：面板里唯一会写配置的地方（只写 `TASKS` 与 `COOKIES_<抖音号>`，
  见下面第 5.1 节）。
- **好友选择器**（每张账号卡里）：**不再手打目标好友**，改成"读取好友列表 → 勾选 → 保存"，
  见第 5.2 节。

扫码登录、运行配置与访问口令平时收起成一行（右侧显示状态标签 / 配置项数 / 已设置），要用时点开；
从别处的锚点（`/#login-panel`、`/#config-panel`、`/#password-panel`、`/#ops-panel`）跳进来时
`app.js` 会自动展开对应的折叠段。

### 5.1 扫码登录（换 cookie 的正规做法）

抖音的登录二维码是页面里的**内联 base64 PNG**，所以面板不需要 noVNC、也不需要截图，
直接把那张图转发给浏览器即可：

1. 面板「扫码登录」→ 填（或留空）账号 → 点**开始扫码登录**；
2. 服务端起一个无头 Chromium（用该账号固定的 `fingerprint`，代理与发送链路一致），
   打开 `douyin.com`、必要时点一次页面右上角的「登录」、把二维码读出来；
3. 面板每 2 秒轮询一次进度，二维码刷新时自动换图（靠 `qr_seq` 变化，不是无脑重下）；
4. 你用抖音 App 扫码并在手机上确认之后，服务端会**验证服务端是否真的认这个登录态**
   （不是只看本地有没有 `sessionid` —— 那正是 C 一开始的假登录漏洞）。
   这一步**绝不在扫码页上做**：服务端另开一个标签页去问「现在算登录了吗」，扫码页原封不动 ——
   否则页面一被导航走，你手上那张码就当场作废（2026-10-10 的现场：码出来 3 毫秒就被刷掉了）。
   资料目录里如果还留着上一次登录的旧 cookie，那它**不算**「登录成功」的证据：只有 cookie
   值换了、而且服务端真点了头，才继续往下走；旧的那份会被判掉并写成
   「本地那份登录态服务端不认了，请重新扫码」（第一步的状态叫 `checking`「正在确认已有登录态」，
   与扫完码之后的 `verifying`「已扫码，正在确认登录态」分开）；
4.5 **风控账号的二次验证**：有些账号扫完码不会直接登录，抖音会甩一道「身份验证」，
   让它在「接收短信验证码」和「发送指定短信到指定号码」里二选一。本项目**固定走收验证码**：
   会话进入 `waiting_code`（面板状态标签「需要短信验证码」），自动点掉「接收短信验证码」那一项，
   把页面截图摆到面板上，并亮出一个**独立的验证码输入框** —— 你把短信里的数字填进去点「提交验证码」，
   会话负责填进页面、点提交、等抖音放行。默认给你 5 分钟（`core/qr_login.py` 的 `CODE_WAIT_SECONDS`），
   验证码最多连着填错 3 次（`CODE_MAX_ATTEMPTS`），超时或次数用完就把这次登录明确置成失败并说原因；
   **判「这一页是不是身份验证页」只认正向证据**：页面正文出现「身份验证 / 安全验证 / 短信验证码」
   这类字样，并且页面上**没有二维码、也没有手机号输入框**。新版登录页把「扫码登录 / 验证码登录 /
   密码登录」三个 tab 渲染在同一页，它自带一个「请输入验证码」输入框 —— 那不是二次验证，
   不会被认（`_detect_code_page`），也不会去点登录页上的「获取验证码」；

5. 通过之后：读到昵称与抖音号 → 把这份 cookie（转义过的单行 JSON）写进 `.env` 的
   `COOKIES_<抖音号>` → 缺账号就补进 `TASKS`（顺手分配一个 `fingerprint`）→ 重载配置；
6. 面板整页刷新一次，账号表里那份 cookie 变成"已配置"。

写盘纪律（`webui/envfile.py`，与 `configTool/env_store.py` 同一口径）：

- **就地更新**：只改自己管的键，注释和别人手写的变量原样保留；
- 每次写之前先备份成 `.env.bak-<时间戳>`，只保留最近 5 份；POSIX 上把权限收回 `600`；
- 写之前先校验：抖音号必须能当变量名、cookie 必须是 JSON 数组、**必须含 `sessionid`**，
  任何一条不满足就整笔拒绝（原来那份 cookie 一个字都不动，也不留备份）。

失败时面板会直接说原因，不会假装成功：抖音号没读到、二维码超时（默认 300 秒）、
浏览器起不来、登录态服务端不认 —— 各有一条不同的提示。

### 5.2 目标好友：主动读取 + 勾选（不再手打名字）

**为什么改**：手打名字是"被动匹配" —— 主程序拿这个名字到会话列表里比对，而
`DouyinIM._match()` 最后有一条**子串兜底**（目标「小明」会命中「小明明」）。
名字会改、会重名，打错一个字就可能把火花发给另一个人。

**现在的做法**（借 A 的"先读列表再勾"的交互，用 C 自己更强的会话枚举能力实现）：

1. 账号表里点**读取好友列表** → 服务端起一个无头 Chromium（该账号的持久化 profile，
   与发送用的是同一个登录态），`DouyinIM.iter_conversations()` 从头滚到尾，
   把**全部会话**读出来（不是只读 DOM 里当前可见的那几条）；
2. 面板每 2 秒轮询进度，读完后整页刷新，列表出现在该账号的勾选区里
   （搜索框可以按名字/抖音号过滤，已保存的默认勾上，新读到的标「新读到」）；
3. 你勾选要续火花的人 → **保存勾选** → 面板把结果写进 `.env` 的
   `TASKS[].targets`。带身份的目标写成对象：

   ```json
   {"name": "好友甲", "uid": "1234...", "conv_id": "0:1:2:3"}
   ```

   `uid`/`conv_id` 都是抖音自己算的，**勾选的那一刻就拿到**；
4. 发送时 `_match()` 先按 `conv_id`、再按 `uid`/`sec_uid` 精确匹配，
   带身份的目标**禁用子串兜底**（名字一模一样但 id 不同 = 不是同一个人，宁可报"没找到"）。

老配置里的纯名字目标**继续可用**（走的还是原来的名字链路），所以升级不需要改 `.env`；
但只有名字时就没有这层保护 —— 勾一次选，之后就都是 id 匹配了。

手工维护（命令行、脚本）仍然可以：`POST /ops/account/targets` 的 `targets` 文本字段
（每行一个名字）保留着，只有名字时就写成字符串。

**运行记录页 `/ops/runs`**

- 本轮结论 + KPI 四卡（**这一页讲的是"选中的那一轮"，挂点是 `data-run-value`，
  不会被总览轮询（10 秒一次、永远最新一轮）改写**）。
- **历史轮次表**：开始时间、档位、来源（命令行/面板触发/定时任务）、结果、账号数、
  成功·失败、耗时；整轮被跳过的那几轮标出跳过原因；点一行看那一轮。
- **每个账号的明细卡**：门禁、凭据状态、目标数、进度条，以及四档分段控件 ——
  **成功**（强确认）、**弱证据**（只拿到页面回显）、**需处理**（失败/选中失败/会话列表里没出现）、
  **未尝试**（这一轮没走到）。每条明细写清判据（level、via、message_id、原因），
  空跑轮次标"空跑（没有真的发送）"。

**日志页 `/ops/logs`**

- 尾部 N 行（50/100/300/800/2000，默认 300，从文件尾部 seek，不整文件读入）、
  自动刷新开关、关键词过滤、下载完整日志。
- 文件不存在/读失败/被截断都会显示成状态标签，不会静默空白。
- 本站**不提供**清空或删除日志的入口（日志由 logrotate 管，见 [deploy.md](deploy.md)）。


## 6. 安全设计

- 口令比对用 `hmac.compare_digest`；会话是 `itsdangerous` 签名 cookie（`httponly`、`samesite=lax`，
  HTTPS 时 `secure`）。
- 所有写操作（登录、登出、立即运行）都要过 **CSRF** 令牌。
- 服务端 Jinja2 autoescape；客户端渲染**只用 `textContent`**，全仓（含静态 JS）不允许 `innerHTML`。
- 日志下载的路径**只来自配置**（`OPS_LOG_FILE`），不接受请求参数，因此不存在路径穿越。
- `webui/` 不写入任何凭据，日志页也不会把 `.env` 内容读出来。
- 「运行配置」的写入口只认 `webui/settings.py` 白名单里的键：伪造提交 `cfg_TASKS` /
  `cfg_COOKIES_*` / `cfg_WEBUI_PASSWORD` 会**被直接丢掉**（`settings.collect()` 只认白名单）。
  代理地址回显时打码（`store.mask_proxy`），把打码文本原样提交回去不会被当成新值覆盖真口令。
- 「访问口令」的写入口（`POST /ops/password`）要**当前口令** + 两次新口令，走
  `envfile.set_webui_password()`：`WEBUI_PASSWORD` 与 `WEBUI_PASSWORD_HASH` **一起写**（不用的
  那个写成空行，顺手把进程环境里可能残留的旧值清掉），校验（8–128 字符、不能有换行、不能有
  英文单引号 `'`）不通过就**一个字节都不写**；成功后备份 `.env`，并且因为会话 token 里带着
  `config.credential_fingerprint()`，**所有旧会话当场失效**。页面与 JSON 响应里从不出现口令
  或它的哈希（`PasswordPanelWebUITestCase::test_首页有口令面板且不回显任何凭据` 钉住）。

## 8. "面板说的"和"工具真做的"必须一致

面板是**双写**结构：服务端渲染一次，前端每 **10 秒**（`webui/static/app.js` 的总览轮询）再覆盖一次。两处只要有一处说的不是同一个事实，
用户就会看到"打开时对、十秒后错"。已经栽过并修掉的几类：

| 症状 | 真因 | 现在的做法 |
|---|---|---|
| 点「移除」账号还在 | 账号已从 `TASKS` 删掉，但上一轮的运行记录（`logs/runs.jsonl`）里还有它，`store.account_snapshots()` 会把它重新变成一张卡；对着这张幽灵卡点移除必然失败 | 幽灵卡带 `orphan=True`，界面标「已不在配置里」、**不给**「移除/重新登录」按钮，也不计入"共 N 个目标/账号被阻断"；`summarize()` 只对非幽灵卡求和 |
| 页面看着错位（同一份 HTML，CSS 却是旧的） | `StaticFiles` 只发 `ETag` 不发 `Cache-Control`，浏览器按"上次修改时间的 10%"启发式缓存旧 `app.css` | 静态文件加 `Cache-Control: no-cache`；模板里的 `?v=` 改成 `asset_version()`（取 `app.css`/`app.js` 的 mtime，**必须写括号**，不然渲染成函数对象） |
| 「下次自动触发 12:30」其实不会跑 | 那个时间读的是 `.env` 的 `TASK_SCHEDULE_TIMES`（"打算几点跑"），而真正说了算的是 systemd 定时器；2026-10-01 之前 `.172` 上它是 **disabled**，那天的 12:30 从来没跑过 | `store.timer_state()` 只读 `/etc/systemd/system/**` 判断是否启用、从单元文件读真实 `OnCalendar`；没启用就画红框「不会自动跑」并给出 `systemctl enable --now …`；对不上时以单元时间为准并注明 `.env` 的值 |
| 定时器到点了却一条都没发 | 禁跑时间窗（`QUIET_WINDOW`）把定时器那一刻盖住了 —— 定时任务整轮跳过，账本只有一行 `quiet_window` | `viewmodel._quiet_clash()` 拿 timer 的 `OnCalendar` 逐点对窗口，命中就在首页画红框「禁跑时间窗把定时器的时间盖住了」；默认窗已改成 `off` |
| 「本轮结论」的数字是今天的合计 | 运行记录页复用了首页的 `data-overview-value` 挂点，而前端总览轮询按这个选择器全局覆盖 | 运行记录页改用 `data-run-value`（只由服务端渲染），`data-overview-value` 只留给首页 |
| 空跑的"确认成功"看着像真发出去 | 空跑（`DRY_RUN=select/full`）也会走选中/回执流程 | 首页进度处标"空跑"角标 + 「这是空跑的数字，一条都没真发出去」，账号行追加"上一轮是空跑（…）" |
| 三个空跑档位点了没反应（400） | 分段控件的挂点是 `dry-off`/`dry-select`/`dry-full`，前端把这个**带前缀**的值原样提交，服务端只认 `off/select/full` | 前端 `raw.replace(/^dry-/, "")`；服务端 `normalize_dry_run()` 再兜一层（老页面被缓存住也不会白点）；`tests/test_webui.py` 里 `test_空跑档位的规范化` + `test_带前缀的空跑档位真的能跑起来` |
| 面板替 core 补记的失败轮次显示成绿色「成功」 | `normalize_run()` 的失败只看每个账号的结果，而"这一轮没跑起来"的记录 `accounts` 是空的 | 账本自己写的 `status="error"` 直接算失败；账本里的 `note`/`error` 优先展示 |
| 账号卡的数字莫名变成"今天" | 运行记录页的账号行用过首页的 `data-account-overview` 挂点 | 运行记录页改用 `data-run-account`，`data-account-overview` 只留给首页 |
| 同一个浏览器资料目录被两个功能抢 | 扫码登录的"资料搬迁"发生在浏览器关掉之后，中间窗口只比 `profile_dir` 会判成空闲；好友读取取消时又立刻放行 | `_profile_in_use()` 同时认 `profile_dir` 与 `adopt_dir`（`QrLoginSession.snapshot()` 新增该字段）；`core/friend_scan.py::cancel()` 不再立刻置终态，等浏览器真的关掉 |
| 卡住的会话让账号永远用不了 | 会话只回收"非 active"的，卡在"进行中"的没人管 | `SESSION_STUCK_SECONDS = 20 分钟` + `_reap_stuck_sessions()`：先发取消信号再从注册表清掉，发起新会话前也会清一次 |
| 清掉一个账号之后，历史轮次开始"猜"原因 | 删掉账本里的账号条目后，那些轮次就变成"没有任何账号结果"，页面会用兜底文案猜"账号凭据可能未配置" | 那条猜原因的兜底文案已删；这类轮次改成明确的判决「没有账号结果」（`status="empty"`）；清理脚本还要给**这些**轮次补一句真话 —— `netinv/annotate_cleaned_rows.sh` 以清理前的备份为准，只给当初真的带着该账号、且已经空掉、又没有别的解释的轮次写 `note`；账本里的 `note` 永远优先 |
| 账号全被门禁拦下（登录失效/掉登录/超时），那一轮却显示绿色「成功」 | 轮次判决只看"有没有失败"，而每个账号都是 `skipped` 时既没成功也没失败 | 新增判决「账号全被跳过」（`status="blocked"`，`totals["entries"] == 0` 时才算）；只要有一个账号真跑了就还是「成功」，别把话说反 |
| `.env` 里没有的下拉项被"顺手"写死 | 浏览器遇到"当前值不在选项里"会默认选中**第一项**，用户不动它直接点保存，就把一个从来没配过的键写成第一项的值（例如凭空写出 `DRY_RUN=off`） | 缺项时下拉框最前面插一个空值占位项「（.env 里没有，工具用默认值 X）」—— 它与 `.env` 原文一致，`collect()` 据此判定"没动过"，一个字节都不写；认不出的当前值同样插在最前面，不会被第一项顶掉 |
| 正在编辑的配置被 10 秒轮询改回旧值 | 表单字段若复用 `data-overview-value` 挂点，总览轮询会把输入框里的内容换成 `.env` 里的旧值 | 表单用 `data-config-input`，`app.js` **完全不认识**这个挂点（`FrontendAgreementTests::test_配置表单不会被总览轮询改写` 钉住）；口令表单同理用 `data-password-*` |
| 在面板里改了口令，登出后老口令照样能进 | 只写了 `WEBUI_PASSWORD`，而 `.env` 里还留着更早的 `WEBUI_PASSWORD_HASH` —— 校验是**哈希优先** | 两个键一起写（不用的那个清成空行）；再给 token 加 `credential_fingerprint()`，改完口令所有旧会话立刻失效 |
| 以为"登录要用密钥" | `WEBUI_SESSION_SECRET` 的注释写成"会话签名密钥"，看着像是登录凭据 | 文档与 `.env` 注释都点明它**只是 cookie 的签名密钥**，登录框只认 `WEBUI_PASSWORD` / `WEBUI_PASSWORD_HASH`；口令能在面板「访问口令」里自己改 |
| 面板预览是三行，真发出去是一行、行尾还挂个 `n` | 模板里写的字面 `\n` 在**配置层**就被吃掉了：单元文件用了 systemd 的 `EnvironmentFile=`（它按转义序列解析值），而 `main.py` 当年 `load_dotenv(".env")` 的 `override` 默认 `False`，被弄坏的进程环境反而赢了文件 | 两份 unit 都不写 `EnvironmentFile=`；`.env` 只由程序读（`utils/config.load_env_file`，绝对路径、**文件优先**，`DRY_RUN`/`RUN_MODE`/`RUN_SOURCE` 仍归启动方）；进程值与文件值不一致时打 WARNING 报出两边；面板预览读的就是这份文件，不存在"两套值" |
| `dry-run=full` 读回了文本，却查不出换行丢了 | `_readback_matches()` 两边都 `re.sub(r"\s+","")` 再比字符 —— `"甲\n乙"` 与 `"甲乙"` 归一化后相等，换行没了也判 ✅ | 改成**按行**比（`line_list()`），并返回读回原文；读回改用 `innerText`（`textContent` 看不见 `<br>`/块边界）；合成输入的换行改成**自校验 + 三级退化**（keydown → `insertLineBreak` → `insertText`），结果写进 `brk`，三种都不行就打 WARNING「换行没插进去：这条消息会挤成一行」 |
| 空跑里"清空输入框"看着成功了，第二次输入却把上一条又打了一遍 | 清空只做了"全选 + `execCommand('delete')`"：它**只抹 DOM、不动 Draft.js 的 EditorState**，当场判空是空的，下一次输入 React 重渲染时旧文本又回来 | `JS_EDITOR_CLEAR` 改成**先走 Draft 自己的键盘路径**（全选 + 合成 Backspace，与软换行同一条路），再退到 `execCommand('delete')`，每条路都用删掉零宽字符后的可见文本自校验并回报 `how`；`_clear_editor()` 清完等 250ms 复查、最多重试 `tries` 轮，且"上一次打过字"时会先插一个字符逼 Draft 重渲染（`distrust_empty`）把藏在 state 里的残留逼出来 |
| 运行详情里冒出一串英文码（`editor_text_mismatch` 之类） | `core` 每新增一个判定原因码，面板的 `REASON_LABELS` 就得跟着加一条；漏了就回退成"原样显示码" | 补上中文标签（输入框相关的四个码都要点名"输入框"，否则用户看不出是哪儿的问题）；`tests/test_viewmodel.py::ReasonLabelCoverageTests` 直接从 `core/douyin_im.py`/`core/strict_verify.py` 源码里抓 `reason=` 码，漏一个就红，顺带钉住"标签不重复、不为空" |
| 面板把证据说强了：写「仅 HTTP 回执」，像是"另一侧看过、不认" | `_wait_receipt()` 拿到 HTTP 回执后**最多再等 1 秒**就收工（`if http and time.time() - t0 > 1.0: break`），所以 `via="http"` 只是"HTTP 先到了、会话回显没等到"，不是"回显里没有这条" | `VIA_LABELS["http"]` 改成「HTTP 回执（会话回显没等到）」；被拦下没点发送的那两条走 `via="none"`，也要有中文（「没发送（先拦下了）」），不能只显示 `none`；`tests/test_viewmodel.py::ViaLabelCoverageTests` 只从 `type_and_send()` 体内抓 `via` 取值（`_wait_list_ready()` 的 `"dom-stable"` 另有含义，不算），并禁止 `http` 那条再出现「仅」 |

排查手段（都在 `netinv/`，只读）：

- `consistency_audit.py`：把同一条事实的多条解析路径并排打出来（`TASKS` 的四条读法、cookie、
  配置、定时器状态、门禁标签与判定原因、core 里 `dry_run`/`quiet_window`/`limit`/`lock` 的出现次数）。
- `after_poll_probe.py`／`runs_poll_probe.py`：读一次页面 → 等 6 秒（≥3 个轮询周期）→ 再读一次，
  确认前端没把服务端的话改掉。
- `layout_probe.py`：8 种宽度 × 三个页面量溢出元素。
- `js_overwrite_audit.py`：列出 JS 会覆盖哪些节点、三个页面各有哪些挂点。
- `audit-panel-core.md`／`audit-error-paths.md`：两轮只读审计（面板语义 vs core、错误路径/并发/安全）
  的原始报告，含复现探针与"查过没问题"的清单。
- `inspect_ghost.sh`／`drop_ghost.sh`／`annotate_cleaned_rows.sh`：清一个已删账号在账本里的残留 ——
  先看清它在哪几轮、哪些字段（只读），再**备份后**删条目，最后给空掉的轮次补一句真话。
  三个脚本都只碰 `logs/runs.jsonl`，`.env` 与资料目录一个字节都不动（`logs/login-profile/` 里
  该账号的资料目录要么本来就没有，要么得单独确认后再删）。
- `probe_systemd_env.sh`：**四段对照**证明"谁把模板里的 `\n` 吃了" —— 同一份 `.env` 分别交给
  `systemd-run --property=EnvironmentFile=…`、`bash source`、`python-dotenv`，把读到的值 `repr` 出来比。
- `probe_template.py`：在部署机上把配置层 → `build_message()` → `split_message_lines()` 三段
  并排打出来（字面 `\n` 应当变成 3 行）。
- `inspect_template.sh`／`inspect_send_log.sh`：只读翻真机 —— 前者把日志里的"消息模板"行、`.env`
  与备份里的模板一起列出来；后者按时间列出全部 `[SEND]`/`[SEL]`/`[DRY]` 行
  （`[SEND] 待发送文本 … → N 行` 就是"这一轮到底按几行发的"的实锤）。
- `probe_reason_labels.py`：拿真机账本里出现过的每个原因码过一遍面板的 `_reason_text()`，
  列出"码 ×次数 → 显示文案"，末尾一句结论。运行记录里的"原因"该显示中文，就是靠它复验的
  （纯标注库调用，不写文件、不改状态）。
- `probe_panel_view.py`：**不发 HTTP、不需要口令** —— 在部署机上直接调
  `webui.app.build_context()`（`lines=` 取日志尾行数、`selected_run_id=""`），把首页视图模型里
  的关键值打出来：横幅/阻塞项/能不能跑、今日汇总、**消息模板原文 + 行数 + 渲染后的样子**、
  定时器状态与下次触发、静默窗与冷却、最近一轮与账号卡。用途是"面板说的"和"实际做的"要能
  逐项对账（模板三行、来源、状态文案），而口令改过之后 HTTP 探针很容易先卡在 401 上。

新增前端文案时的硬约定：**同一句话必须同时出现在模板和 `app.js` 里**（`tests/test_webui.py`
的 `FrontendAgreementTests` 会把两边钉在一起），并且前端配色用 `stateCss` 而不是 `state`。

## 9. 排障

| 现象 | 处理 |
|---|---|
| 起不来，退出码 2 | 绑了 `0.0.0.0` 却没配 `WEBUI_PASSWORD`/`WEBUI_PASSWORD_HASH`（故意的）；设口令或改回 `127.0.0.1` |
| 登录后立刻又跳回登录页 | `WEBUI_SESSION_SECRET` 每次重启都变 ⇒ 在 `.env` 里固定一个值；**改过口令也会这样**（旧的会话 cookie 一律作废，这是设计） |
| 忘了面板口令，进不去 | 在服务器上 `cd /opt/douyin-spark-fusion && bash netinv/reset_webui_password.sh`（不带参数就随机生成一个并打印出来；它会重启 web 服务）。手工等价做法：`python -m webui.hash_password '新口令'` 写回 `WEBUI_PASSWORD_HASH`（或直接写 `WEBUI_PASSWORD='新口令'`）后重启 `douyin-spark-fusion-web.service` |
| 首页账号/cookie 是旧的 | 配置在进程内缓存，`systemctl restart douyin-spark-fusion-web.service` |
| 点"立即运行"返回 503 | 这台机器上没装 `cloakbrowser`/`playwright`（面板本身不需要它们，跑任务才需要） |
| 点"立即运行"返回 409 | 两种原因：①已有一轮在跑（`logs/run.lock` 里的 pid 还活着）；等它结束，或确认进程已死后删掉锁文件。②当前处于禁跑时间窗（`QUIET_WINDOW`，默认 `off`；设了才会拦）；在「运行配置」里改窗口，或临时设 `QUIET_WINDOW_OVERRIDE=1` |
| 日志页显示"文件不存在" | `OPS_LOG_FILE` 指向的文件还没产生；systemd 部署下应指向 `logs/douyin-spark-fusion.log` |
| 消息发出去没有换行、行尾多个 `n` | 模板里的字面 `\n` 在配置层被吃了。①`.env` 里消息模板必须写**两个字符** `\n`（不是真回车，真回车会把 `.env` 拆成两行）②`grep -n '^EnvironmentFile' /etc/systemd/system/douyin-spark-fusion-*.service` **必须没有输出**（systemd 按转义序列解析 environment file），改完 `systemctl daemon-reload` 并重启 web 单元。确认：`.venv/bin/python main.py dry-run full`，日志里 `[SEND] 待发送文本 '…\\n…' → 3 行` 才对；面板「运行配置」的模板预览读的就是同一个文件 |
| `dry-run=full` 里 `已清空=False`（连着重试一次，读回变成两遍文本） | 清空输入框只用了 `execCommand('delete')`：Draft.js 的内容真相在它自己的 EditorState 里，这样**只抹 DOM**。当场判空是空的，可下一次输入 React 重渲染又把旧文本带回来，于是重试时两条消息叠着发。现在 `JS_EDITOR_CLEAR` 先走 Draft 自己的键盘路径（全选 + 合成 Backspace），`_clear_editor()` 清完等 250ms 复查、最多重试 3 轮，还会插一个字符逼它重渲染把藏在 state 里的残留逼出来。日志看 `已清空=True（清空方式=key 或 cmd）`—— 现场两种都出现过，`cmd` 表示"键盘那条路没清干净、靠 `execCommand('delete')` 收尾"，只要 `已清空=True` 且没有 `重试一次` 就是干净的 |
| 运行记录里"原因"一栏显示英文 | `core` 新增了判定码，而 `webui/viewmodel.py` 的 `REASON_LABELS` 没跟上（`_reason_text()` 找不到就原样显示） | 在 `REASON_LABELS` 里补一条中文（**别在模板里写死**，否则运行记录页和首页会各说各话）；`ReasonLabelCoverageTests` 会替你盯住这件事 |
| "证据"一栏写着"HTTP 回执（会话回显没等到）"，是不是另一侧没确认？ | 不是。HTTP 回执（抖音自己给的 `code=0` + `message_id`）先到，代码就再等最多 1 秒收工，**没等到**会话回显 ≠ 回显里没有。强/弱由 `strict_verify.verify_receipt()` 合并两侧证据后给（`强确认` 就是它判的） | 想看到两侧都确认，就等一轮"气泡数也涨了"的发送（`via="http+dom"`）；要查某一轮到底怎么判的，看日志里 `[SEND] ✅ HTTP 回执 …` 与 `[SEND] ✅ DOM 确认：消息数 …` 两行是否都出现 |
