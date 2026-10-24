# 仪表盘（`webui/`）说明

融合版自带一个**只读为主 + 一个手动触发按钮**的网页仪表盘：看运行结果、看登录门禁、
看限流冷却、看日志尾部，并能在面板上直接空跑或真跑一轮。部署步骤见 [deploy.md](deploy.md)。

界面骨架**来自 B（`halfwaystudent/douyin-sparkflow`）的原始 `webui/` 代码**：模板结构
（`base.html` / `dashboard.html` / `send_console.html` → `runs.html` / `logs.html` / `login.html`）、
`static/app.js`（前 378 行：主题、移动端导航、确认框、分段控件、总览轮询）与 vendored 的
`static/lucide.min.js` 都还是原件。**样式表是我们自己维护的**：现在是两套可选皮肤 ——
`static/app.css`「扁平控制台」与 `static/app-aurora.css`「极光棱镜」（2026-10-19 起为新访客的默认皮肤），见 §7。移植时只做了两件事：

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
- **只改白名单里的键**：面板的配置面板能改 28 个键（发送时间、抖动、超时、
  日志级别等），保存时**原子替换** `.env` 并留 5 份备份，改完立即生效。白名单之外
  （`COOKIES_*` / `TASKS` / 口令 / 各种 `*_FILE`）只能手工编辑 `.env`，改完
  `systemctl restart douyin-spark-fusion-web.service`。
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
| `WEBUI_SESSION_MAX_AGE` | `2592000`（30 天） | 登录保持时长（秒），上限 400 天（浏览器对 cookie 有效期的硬上限）。**读的时候才生效**：调大后已经登录的浏览器立刻受益，调小会把超过新时长的会话踢下线；改口令永远会作废所有会话。面板首页「访问口令」里也能改 |
| `WEBUI_DEFAULT_DRY_RUN` | `off` | "立即运行"默认选中的档位（`off`/`select`/`full`）；只是默认值，点按钮前仍可在面板上改，非法值回落 `off` |
| `LOGIN_PROFILE_DIR` | `logs/login-profile` | 扫码登录用的浏览器资料目录（等于凭据，别提交/共享） |
| `QR_LOGIN_TTL_SECONDS` | `300` | 二维码有效期；超时作废，需重新开始一次 |
| `FRIEND_SCAN_TIMEOUT_SECONDS` | `300` | "读取好友列表"单次扫描超时（秒）；超时标成读取失败，已读到的部分仍可用 |
| `WEBUI_COOKIE_NAME` | `dsf_webui_session` | 会话 cookie 名 |
| `WEBUI_AUTH` | `pass` | 设成 `off` 关闭认证；**只在回环地址上被接受** |
| `WEBUI_TZ` | `Asia/Shanghai` | 页面上时间戳的显示时区 |
| `TASK_SCHEDULE_TIMES`（别名 `WEBUI_TASK_SCHEDULE`） | `20:30` | **真实调度**：每个账号自己的发送时间（账号单独设了时间就以账号的为准）。派发器每 5 分钟敲一次门，到点才发；页面上算的"下次定时"就是它 |
| `RUN_LEDGER_FILE` | `logs/runs.jsonl` | 运行账本（面板读它画结果） |
| `RUN_LOCK_FILE` | `logs/run.lock` | 运行锁（判断"正在运行"） |
| `RATE_LIMIT_FILE` | `logs/rate_limit.json` | 熔断冷却状态 |
| `OPS_LOG_FILE` | `logs/app.log` | "日志"页读的文件；systemd 部署下指向 `logs/douyin-spark-fusion.log` |
| `NOTIFY_WEBHOOK_URL` | 空 | 结果通知地址；面板上**打码显示**（只留 `scheme://host/***`），不动这一栏就不会覆写。留空 = 不通知 |
| `NOTIFY_LEVEL` | `error` | `off` / `error` / `always`；空跑（`DRY_RUN` 不是 `off`）无论哪一档都不推 |
| `NOTIFY_TIMEOUT_SECONDS` | `10` | 单次推送超时（3–60 秒） |

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
| POST | `/ops/account/requeue` | 表单 `unique_id`：**撤回今天的记账**，让当天剩下的门铃把它补发一次（issue #12 的手动出口）。**今天已经成功发过的账号会被拒绝**（"已经发过就不撤"的判定在 `core/schedule.requeue_after_relogin()`），所以这个按钮造不出"一天发两遍"；`?format=json` 走 JSON |
| POST | `/ops/config/save` | 保存「运行配置」表单：只认 `webui/settings.py` 白名单里的键，写前备份 `.env`；`?format=json` 走 JSON 报错 |
| POST | `/ops/notify/test` | 按当前配置推一条**测试**通知（200 成功 / 400 失败并在 `detail` 里说原因）。同步执行，最坏等 `NOTIFY_TIMEOUT_SECONDS` 秒；`?format=json` 走 JSON |
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
  状态标签，右侧是「明细 / 重新登录 / 补发今天 / 移除」；下面一条进度条与三个数字（强确认 / 需处理 /
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
  表单外面还有一个独立的**发送测试通知**按钮，走 `POST /ops/notify/test` —— 地址配错时
  不用等出事故就知道；测试消息带【测试】前缀，不会被当成真发送。
- **访问口令**（折叠）：**面板自己的登录口令在这里改**，不用去服务器上碰 `.env`。左侧栏
  「管理 → 访问口令」也能直接跳进来。表单要填当前口令 + 新口令（两遍）+ 保存方式：
  - 保存方式 `明文`（默认）＝ 写 `WEBUI_PASSWORD`，`.env` 里能直接读到，忘了可以用眼睛看回来；
  - 保存方式 `哈希` ＝ 写 `WEBUI_PASSWORD_HASH`（`pbkdf2_sha256`），文件里看不到原口令。
  - **两个键永远一起写**：选了哈希就把明文那行清成空。否则"哈希优先"会让面板里改了的口令
    登不上（老明文还在）。改完写前备份 `.env`，并**立刻让所有旧会话失效**（包括当前这个），
    所以提交后会跳到登录页让你用新口令登一次 —— 其它设备上开着的页面也一样下线。
  - 页面上**从不回显口令或哈希**（连 cookie 都只显示"56 项"这种计数），只说明"现在存的是
    哪种形式 / 来自 `.env` 还是进程环境 / 会话能撑多久"。
  - 口令忘了、面板进不去：在服务器上跑 `.venv/bin/python -m webui.reset_password`（不带参数就
    生成一个随机口令并打印一次），它走同一个写入口（先备份、两键同写）+ 重启
    `douyin-spark-fusion-web.service` 并轮询 `/healthz`；`--hash` 存哈希，`--no-restart` 只写文件。
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
   与扫完码之后的 `verifying`「已扫码，正在确认登录态」分开；服务端一旦明说「没登录 / 这份
   sessionid 不认」，这一步**立刻**结束，不再干等满 `VERIFY_TIMEOUT_SECONDS` —— 旧 profile 上
   实测要等 60 秒才见到二维码，没人受得了）；
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
   `COOKIES_<抖音号>` → **把"登录这一台设备"的整份浏览器状态存成 `logs/browser-state/<抖音号>.json`**
   （`core/qr_login._save_device_snapshot`，之后的发送沿用同一台设备，见 `FUSION-DESIGN.md` 6.2 P4）→
   缺账号就补进 `TASKS`（顺手分配一个 `fingerprint`）→ 重载配置；
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

## 7. 两套皮肤（classic / aurora）

同一份模板、同一个 `app.js`，配两套**完整**样式表。切换只换 `<link>`：不刷新页面、不重建 DOM，
`html[data-theme]`（深/浅）与 `html[data-skin]`（classic/aurora）是两个正交维度，四种组合都成立。

| | classic「扁平控制台」 | aurora「极光棱镜」（默认） |
|---|---|---|
| 样式表 | `static/app.css` | `static/app-aurora.css` |
| 观感 | 扁平控制台：1px 发丝线、单一强调色、信息密度高 | 玻璃层 + 动态极光光斑 + 光谱描边与走光高光 |
| 版式（桌面 ≥1081px） | 左侧竖栏 + 单列流水，内容 1280px 定宽居中 | 顶部指挥条 + 刊头 + 12 栏非对称栅格；运行记录是卡片列表、日志页是通栏终端、登录页是居中单列 |
| 装饰 | 无（只有静态背景色） | `app.js` 往 `body` 最前面插一层 `.aurora-scene`（网格 + 三个光斑 + 暗角），切回 classic 就删掉 |
| 交互点缀 | 没有位移与光效 | 卡片鼠标跟随柔光、进度条跑光、进入动画 |

怎么切、为什么不会闪、以及它撞上的两条仓规：

- **开关**：页头（窄屏在顶栏）的调色板按钮 `[data-skin-toggle]`；选择记在 `localStorage["sparkflow-skin"]`，
  与主题的 `sparkflow-theme` 分开。图标自带两枚（`skin-icon-classic` / `skin-icon-aurora`），按当前皮肤留一个。
- **默认皮肤是 aurora**（2026-10-19 起）：头脚本与 `app.js::storedSkin()` 的口径都是「`localStorage` 里没有
  `sparkflow-skin`，或者读不出来（隐私模式）→ aurora；只有明确存过 `classic` 的浏览器才留在 classic」。
  要整体退回 classic，把 `base.html` / `login.html` 头脚本里的初始值与 catch 兜底、以及 `storedSkin()` 的默认值
  改回 `"classic"` 即可（`SkinWebUITestCase::test_默认皮肤是极光棱镜` 钉住这一条，改的时候测试会红）。
- **不闪第一帧**：`base.html` 与 `login.html`（登录页不继承 base，必须各写一份）的 `<head>` 里有一段**同步**内联脚本，
  在样式表开始加载前就把 `document.documentElement.dataset.skin` 定下来、aurora 就当场换掉 `href`；
  换成异步执行的话，第一帧画的会是 classic。
- **换表不空白**：`app.js` 的 `swapSheet()` 走**双缓冲** —— clone 一个新 `<link>` 追加进 `<head>`，
  等它 `load` 之后才删旧表、把 `id="app-stylesheet"` 挪过去；`error` 时把新表撤掉、退回旧表。
  直接改旧表的 `href` 会先摘掉样式、再等 79KB 下载完，中间那一瞬是裸 HTML。
  连续快速切换时只认**最新一次**的目标：脚本用 `pending` 记住正在下载的那张，新切换先撤掉上一张，
  旧表的 `load` 回调发现自己已经不是 `pending` 就直接返回 —— 最终 `<head>` 里永远只有一张 `app-*.css`
  （`SkinWebUITestCase::test_连续切换皮肤不会留下两张样式表` 钉住）。
- **层叠约定**：`.app-shell` / `.main-area` 是 `position: relative; z-index: 1`，装饰层是 `z-index: 0` + `pointer-events: none`，
  所以极光永远在内容之下、也不吃点击。登录页没有 `.app-shell`，用 `.login-page > *:not(.aurora-scene) { z-index: 1 }` 把内容抬起来 ——
  那个 `:not()` **不能省**，否则会把装饰层的 `position: fixed` 顶掉、整层消失。
- **仓规一：不许字符串拼 DOM**（`tests/test_webui.py::test_webui_源码里没有_innerHTML`）。所以极光装饰层是
  `createElement` 拼出来的一棵树；连注释里出现那个词都会判违规（它是纯子串扫描）。
- **仓规二：零外链**（`test_模板与静态文件没有外网引用`）。所以自绘下拉箭头是两段 `linear-gradient` 拼的 V 形，
  不是 SVG data URI —— data URI 里的 `xmlns='http://www.w3.org/2000/svg'` 一样会踩线。
- **浅色主题要单独收一遍渐变文字**：用 `background-clip: text` + `color: transparent` 画的标题（`.brand-title`、`.page-heading h1`），
  深色主题下用亮色渐变，浅色底上就等于把字藏了 —— 两处都另配了 `html[data-theme="light"] …` 的深色渐变，
  以后新增这种标题要照着加（`SkinWebUITestCase::test_浅色下渐变文字换成深色渐变` 钉住）。
- **光谱底上的字色是令牌 `--accent-ink`，不写死**：`--spectrum` 既是主按钮、头像、勾选框的底色，又是登录页标题的渐变字。
  深色主题的光谱是浅色（青→粉），上面要近黑的字；浅色主题的光谱中间调偏深，上面要纯白 ——
  2026-10-19 修的就是浅色下「蓝底 + 近黑字」，主按钮只有 3.1:1。改法：两套主题各给一个 `--accent-ink`，
  浅色主题另配一套压深一档的光谱（700 号色阶），白字与「画在白卡上的渐变字」两边都 ≥ 4.5:1
  （`SkinWebUITestCase::test_光谱底上的字色跟着主题走`、`::test_浅色主题的光谱白字与渐变字都够看` 钉住）。
- **浅色主题的次要文字压深一档**：`--text-3` / `--text-4` 画的是 KPI 标签、表头、字段名、hint，
  浅色底上原本最淡只有 2.4:1；现在最淡那一档也在 4:1 以上，层级（text-2 > text-3 > text-4）不变
  （`::test_浅色主题的次要文字不再淡到看不清` 钉住）。
- **浅色主题的状态色也压深一档**：`--success` / `--danger` / `--info` 既当文字（状态胶囊、指标数字、「移除」「立即发送」）
  又当 12% 淡底上的字。原稿的绿字在淡绿底上只有 3.0:1、红字 3.7-4.4:1、蓝字 4.0:1，现在四档语义色在白底上都 ≥ 4.5:1，
  浅色主题的 `--*-soft` 还得是**写死的不透明淡色**：面板底色是一条斜向渐变，半透明的淡绿会被底下的蓝染灰，
  实测「在册」胶囊只剩 4.27:1，换成不透明的 `#eaf6f1` 一类之后是 4.95:1 ——
  连淡底一起被 `::test_浅色主题的状态色在白底和淡底上都够看` 钉住。
- **运行配置表不靠单元格内容撑列宽**：`table-layout: auto` 下 `th` 的宽度只是"建议值"，卡片一窄（1081~1400px 这段最明显）
  中文标签会被压成一字一行的竖排、hint 被裁，整张表还顶出卡片外 —— 页面**不产生横向滚动条**，所以是"看不见"而不是"能滑"（用户截图报的就是这个）。
  现在配置表是 `table-layout: fixed` + `th` 固定列宽，输入框从 330px 硬宽改成 `flex: 1 1 260px`（可伸可缩、上限不变），
  窄卡片里 hint 自己折到下一行；`@media (max-width: 900px)` 的堆叠规则同步加 `.config-form .config-cell` 前缀
  （`::test_运行配置的行不随卡片变窄塌掉` 钉住）。另外 **1081-1279 这一段仪表盘并回单列**：
  两栏 7/5 时这张卡只剩 364-447px，比 `<=1080` 的单列（1024 下它有 948px）还窄，越过断点反而更难用。
   **回到两栏（`>=1280`）时行高对齐又会留死白**：第一行左卡「扫码登录」只有 112px，右边「立即运行」280px，
   栅格行高按最高的那张算，左栏中间就空出 190px（1363px 宽实测，用户第二张截图报的就是这个）。
   后来让「立即运行」`grid-row: span 2`，左栏补上「磁盘与资料目录」，但第三张卡「运行配置」又落到下一行：
   这 280px 被摊到两行上，左栏每张卡上下各多出约 48px，卡片之间看着有 70px 的空隙（用户再次截图报的就是这个）。
   现在让它 `span 3`：左栏三张卡自然高度 72 + 91 + 73，加两个 22px 间距正好 280，行高与卡高严丝合缝；
   访问口令改到右栏第三行下面（`grid-column: 8 / -1`），不再和运行配置并排。1363px 实测三张卡之间都是 22px。
- **嵌套圆角写成"同心"关系，不写死内层半径**：分段控件（空跑档位那组 `role="tablist"`）是「1px 边框 + 3px 内边距」的容器套一个药丸按钮，
  内层半径必须等于外层半径减去（边框 + 内边距）：16 号那次改版把它写成死数 10px，理想值是 `13 - 1 - 3 = 9`（差 1px）；
  经典皮肤同款问题更狠：8px 的容器配 6px 的药丸，理想值只有 4px。
  现在两套皮肤都在容器上定义 `--seg-inset`（= 边框 + 内边距），按钮写 `border-radius: calc(<外层半径> - var(--seg-inset))`，
  `SkinWebUITestCase::test_分段控件的内外圆角是同心圆` 从 CSS 文本里量着钉住这条关系。
- **半径对齐了还不够，滑轨和胶囊得有色差**：沿两条 90° 圆弧各采样 24 点量最小点距，
  修前 3.2–3.4px、修后 3.0px —— 差不到半个像素，光改半径用户根本看不出区别（"好像并无修复"就是这么来的）。
  真正让两条圆角糊成一团的是滑轨与胶囊几乎同色：极光的滑轨原来写 `rgba(255, 255, 255, 0.035)`，
  浅色主题等于往白卡上又刷一层白，白胶囊压上去分不出边界。现在滑轨走 `--seg-track` / `--seg-track-inner` 两个令牌
  （浅色 `rgba(21, 28, 56, 0.10)` 压暗、深色 `rgba(255, 255, 255, 0.085)` 提亮），再加一道 `inset 0 1px 2px` 的凹槽；
  `SkinWebUITestCase::test_分段控件的滑轨和胶囊分得开` 钉住"浅色滑轨必须是压暗的蓝灰"。
   这三条也一并被 `::test_极光皮肤在版式上也与经典不同` 钉住。
- **「只有折叠块」的卡片只留一层圆角**：「扫码登录 / 运行配置 / 磁盘与资料目录 / 修改密码」这四张卡里只有一个
  `<details class="collapsible-section">`。极光原来把外层也画成卡片（1px 边框 + `--glass` 底 + `padding: 16px 22px 22px`），
  再用 `.dashboard-section > .section-collapse { margin-inline: -22px }` 让折叠块抵消左右内边距贴到卡片边线上：
  内层因此离边框左右各 1px、上方 17px、下方 22px，外 22px 内 16px 两道弧在四角挤在一起 ——
  用户报的「两个圆角矩形重叠」就是这个。经典皮肤一直是另一套写法：
  `.dashboard-section.section-flush { padding: 0; border: 0; background: transparent; }`，折叠块自己就是卡片。
  极光现在照这个来，并把卡片圆角收进 `--panel-radius` 令牌（560px 以下换成 `--radius`），折叠块写
  `border-radius: var(--panel-radius)`，整张卡只剩一层圆角；`SkinWebUITestCase::test_四张折叠卡只有一层圆角`
  钉住负边距不再出现、外层不再画卡片、内层圆角跟着令牌换。
- **版式也换了，不只是配色**：`app-aurora.css` 末尾是一层 `/* == 21 版式重排 … */`，整段包在
  `@media (min-width: 1081px)` 里，把七块重排了一遍 —— 外壳（`.app-shell` 退出栅格、`.sidebar` 变 sticky 顶部指挥条）、
  主区（`.main-area::before` 刊头柔光带 + `.page-header` 大字标题 + 1360px 宽画布）、仪表盘（`.dashboard-stack` 12 栏栅格，
  扫码登录、立即运行 `span 7` / `span 5` 成对同行）、概览条（主卡独占一行、主数字 clamp 到 76px）、
  KPI（2×2）、运行记录（`#run-history` 表格改卡片列表，`td::before` 用 `nth-child` 硬编码七列中文小标签）、
  日志页（`.panel` 去壳、`.logs-code` 通栏终端）、登录页（居中单列 + 三列特性 + 560px 卡片）。
  **必须留在 `min-width: 1081px` 里**：part6 的窄屏是抽屉，压掉就废；窄屏另有一段补丁（`.mobile-topbar` 贴顶、
  `.mobile-brand` 靠左把主题/皮肤按钮推到右侧）。顶部指挥条会挡住锚点跳转的落点，所以另配了
  `html { scroll-padding-top: 104px } `（1081–1399 的折行档 150px）与各 section 的 `scroll-margin-top`。
  `SkinWebUITestCase::test_极光皮肤在版式上也与经典不同` 钉住这些骨架选择，也钉住 `body.nav-open .sidebar` 没被压掉。
- **材质是三层，不是一张半透明底**：卡片现在同时有 `--card-sheen`（上亮下暗的天光）、`--inner-top` + `--edge-bottom`
  （上下各一道内描边）和 `--shadow-2`（外投影）。只靠 `background-color` 时圆角处没有厚度，浅色下更像一张贴纸。
  材质只改 `background-image` / `box-shadow`，不动尺寸，所以断点和版式都不用跟着改（`::test_材质令牌两套主题各一份`）。
- **浅色卡面是 80% 白**：66% 时粉紫极光整块透上来，白卡被染成淡紫、字跟着发灰；88% 又太实、丢了底色。
  80% 是两档之间试出来的（`::test_浅色卡面是磨砂白而不是彩色玻璃` 钉住 0.72–0.9 这个区间）。
- **胶片颗粒是内联 SVG，`xmlns` 必须百分号编码**：`.aurora-scene::after` 用 `feTurbulence` 现画 180×180 噪声，
  浓度走 `--grain-opacity`（深色 0.055 / 浅色 0.04）、`mix-blend-mode: overlay`。仓规二扫的是裸 `http://` 子串，
  所以写 `http%3A//www.w3.org/2000/svg` —— 直接写链接会被判外链（`::test_颗粒层是内联的_svg_不引外链`）。
- **KPI 的语义色改成「一道渐隐的光」**：原来 42px 等宽实心条 + `0 0 16px` 外发光，浅色下像一根浮在卡外的荧光棒；
  现在四档只换 `--metric-tone` / `--metric-wash`，色条 76px、右端渐隐、无发光，左上角另有一层同色 radial 渗光
  （`::test_KPI_色条是渐隐的光而不是浮着的实心条`）。
- **「真发」自己一条 `button-send`**：它原来与「移除」共用 `button-danger`（淡红底红字），看着就是 disabled。
  `button-danger` 留给破坏性动作（移除、确认清空），`button-send` 是实心红 + 白字 + 红色投影，
  模板按 `view.dry_run.default == "off"` 选类，**app.js 的档位切换又按同一个档位重画一次类** ——
  两处必须写同一个类名，只改模板的话首屏会被脚本改回 `button-danger`
  （`::test_真发按钮是实心红而不是淡红边框` 把模板与脚本一起钉住）。
- **背景层跟指针做视差**：三层光斑各给一个系数（`PARALLAX_LAYERS`：-26/-18、20/14、14/-10 px），
  `paintGlow` 里按 `clientX / innerWidth - .5` 算成 px 写进 `--tx` / `--ty`，`.aurora-blob` 用
  `translate: var(--tx, 0px) var(--ty, 0px)` + 700ms 过渡吃掉抖动，`pointerleave` 归零。
  背景层是 `setScene()` 动态注入的，节点不能只查一次（`::test_背景层跟着指针做轻微视差`）。
- **减少动效要连 `animation-delay` 一起压**：原来只压 `animation-duration`，KPI 卡仍会先空白 40/90/140/190ms
  再出现；现在 `prefers-reduced-motion` 里把 delay 也归零、`translate` 清掉（`::test_减少动效时连入场延迟也一起停`）。
- **登录页品牌区的浅色白矩形是特异性问题**：版式层 `.login-brand-panel { background: none }` 是 (0,1,0)，
  压不过浅色那套 (0,2,0) 的渐变，浅色下品牌区露出一块没有圆角的硬边白矩形；补一条同特异性的
  `html[data-theme="light"] .login-brand-panel { background: none; }`（`::test_登录页品牌区在浅色下也不画矩形`）。
- **跨页导航是淡出上浮，不是硬切**：首页 / 运行记录 / 运行日志是三份独立文档，点导航会真的换文档。
  两套样式表与 `base.html` / `login.html` 的 `<head>` 都打开了跨文档 View Transition（`@view-transition { navigation: auto; }`）：`.sidebar` 与
  `.mobile-topbar` 用 `view-transition-name` 命名后在转场中原样留在原地；只有 `root` 快照换，旧页 160ms 快退
  （上移 10px）、新页 360ms 慢入（从下 14px 浮上来），两段同时开始、交叉约 160ms。**极光那层全屏
  `.aurora-scene` 不能单独命名** —— 它会画在 root 快照上面，把页头与页体整块盖住（实测只剩顶栏和背景）。
  减少动效时还要单独停 `::view-transition-*` 与 JS 兜底类的动画：通配 `*` 管不到这些伪元素
  （`::test_跨页导航有淡出淡入转场` 钉住）。
  这条 opt-in 必须在模板 `<head>` 里**内联**一份，不能只写在外链 CSS 里：换文档时新页的样式表
  可能还没解析完，Chrome 会以 `ViewTransition opt-in disabled` 中止转场（实测 `simple -> /ops/runs`
  5/5 失败，内联后 8/8 成功）。外链那份保留，负责 `::view-transition-*` 与 `view-transition-name`。
  但 **“支持”不能靠 `CSS.supports("view-transition-name: none")` 或 `CSSStyleSheet.replaceSync` 猜**：
  Firefox 157 的 supports 是 true、`document.startViewTransition` 也是 function，却把 `@view-transition` 整条丢掉；
  部分 Chromium 分支也会“解析得出、实际不播”。现在按结果判断：所有同源普通左键导航都由 `app.js` 把目标 URL 写进
  `sessionStorage` 后 `location.assign`（浏览器的原生跨文档转场对脚本导航同样生效，实测 `pagereveal` 带 `viewTransition`）；
  新页在 `<head>` 里同步读标记、在第一次绘制前挂 `dsf-page-enter`，同时监听 `pagereveal` —— 真原生转场会在首帧前带
  `viewTransition` 触发，把入场类摘掉，只留原生；没带就把类留下，由 CSS 播 280ms 入场（从 82% 不透明、上移 10px
  浮上来，避免整页一闪成白），再在 `animationend` 后摘掉。**兜底不做旧页离场**：旧页先淡到全透明再换文档，
  屏幕会空出一截背景（实测约 3 帧），只剩新页入场反而更连续。只接管无修饰键的左键、同源、非 `target=_blank`、
  非 `download`、非 `[data-confirm]`、非纯 hash 的链接；中键 / 新标签 / 后退仍是原生行为
  （`::test_不支持跨文档转场的浏览器也有转场` 钉住）。
- **版本串**：`asset_version()` 的 mtime 取自 `app.css` + `app-aurora.css` + `app.js`，改哪一套都会换 `?v=`。
- **测试**：`SkinWebUITestCase` 钉住「两套都完整（>30KB，不是薄薄一层覆盖）、模板里用到的类两套都有样式、
  每张页面都自己同步挑皮肤、皮肤开关桌面与手机各一个、`/static/app-aurora.css` 走静态路由且带 no-cache、
  浅色主题的对比度（光谱底上的字色、次要文字）、配置表列宽不随卡片塌、嵌套分段控件的内外圆角是同心圆、
  分段控件的滑轨与胶囊有色差、四张折叠卡只留一层圆角、材质令牌与颗粒层、KPI 渐隐色条、实心红真发按钮、
  经典皮肤也补了真发按钮与皮肤图标、登录页浅色覆盖、减少动效时连延迟一起停、背景层视差、
  跨页导航的淡出上浮转场（含内联 opt-in、`@view-transition` 被丢弃时的 JS 兜底）、连续切皮肤不留第二张表（加载失败还要回滚）、浅色首屏不闪深色」；
  上面两条仓规对新皮肤同样生效。

## 8. "面板说的"和"工具真做的"必须一致

面板是**双写**结构：服务端渲染一次，前端每 **10 秒**（`webui/static/app.js` 的总览轮询）再覆盖一次。两处只要有一处说的不是同一个事实，
用户就会看到"打开时对、十秒后错"。已经栽过并修掉的几类：

| 症状 | 真因 | 现在的做法 |
|---|---|---|
| 点「移除」账号还在 | 账号已从 `TASKS` 删掉，但上一轮的运行记录（`logs/runs.jsonl`）里还有它，`store.account_snapshots()` 会把它重新变成一张卡；对着这张幽灵卡点移除必然失败 | 幽灵卡带 `orphan=True`，界面标「已不在配置里」、**不给**「移除/重新登录」按钮，也不计入"共 N 个目标/账号被阻断"；`summarize()` 只对非幽灵卡求和 |
| 页面看着错位（同一份 HTML，CSS 却是旧的） | `StaticFiles` 只发 `ETag` 不发 `Cache-Control`，浏览器按"上次修改时间的 10%"启发式缓存旧 `app.css` | 静态文件加 `Cache-Control: no-cache`；模板里的 `?v=` 改成 `asset_version()`（取 `app.css`/`app-aurora.css`/`app.js` 的 mtime，**必须写括号**，不然渲染成函数对象） |
| 「下次自动触发 12:30」其实不会跑 | 那个时间读的是 `.env` 的 `TASK_SCHEDULE_TIMES`（"打算几点跑"），而真正说了算的是 systemd 定时器；2026-10-01 之前 `本机` 上它是 **disabled**，那天的 12:30 从来没跑过 | `store.timer_state()` 只读 `/etc/systemd/system/**` 判断是否启用、从单元文件读真实 `OnCalendar`；没启用就画红框「不会自动跑」并给出 `systemctl enable --now …`；对不上时以单元时间为准并注明 `.env` 的值 |
| 定时器到点了却一条都没发 | 禁跑时间窗（`QUIET_WINDOW`）把定时器那一刻盖住了 —— 定时任务整轮跳过，账本只有一行 `quiet_window` | `viewmodel._quiet_clash()` 拿 timer 的 `OnCalendar` 逐点对窗口，命中就在首页画红框「禁跑时间窗把定时器的时间盖住了」；默认窗已改成 `off` |
| 「本轮结论」的数字是今天的合计 | 运行记录页复用了首页的 `data-overview-value` 挂点，而前端总览轮询按这个选择器全局覆盖 | 运行记录页改用 `data-run-value`（只由服务端渲染），`data-overview-value` 只留给首页 |
| 空跑的"确认成功"看着像真发出去 | 空跑（`DRY_RUN=select/full`）也会走选中/回执流程 | 首页进度处标"空跑"角标 + 「这是空跑的数字，一条都没真发出去」，账号行追加"上一轮是空跑（…）" |
| 三个空跑档位点了没反应（400） | 分段控件的挂点是 `dry-off`/`dry-select`/`dry-full`，前端把这个**带前缀**的值原样提交，服务端只认 `off/select/full` | 前端 `raw.replace(/^dry-/, "")`；服务端 `normalize_dry_run()` 再兜一层（老页面被缓存住也不会白点）；`tests/test_webui.py` 里 `test_空跑档位的规范化` + `test_带前缀的空跑档位真的能跑起来` |
| 面板替 core 补记的失败轮次显示成绿色「成功」 | `normalize_run()` 的失败只看每个账号的结果，而"这一轮没跑起来"的记录 `accounts` 是空的 | 账本自己写的 `status="error"` 直接算失败；账本里的 `note`/`error` 优先展示 |
| 账号卡的数字莫名变成"今天" | 运行记录页的账号行用过首页的 `data-account-overview` 挂点 | 运行记录页改用 `data-run-account`，`data-account-overview` 只留给首页 |
| 同一个浏览器资料目录被两个功能抢 | 扫码登录的"资料搬迁"发生在浏览器关掉之后，中间窗口只比 `profile_dir` 会判成空闲；好友读取取消时又立刻放行 | `_profile_in_use()` 同时认 `profile_dir` 与 `adopt_dir`（`QrLoginSession.snapshot()` 新增该字段）；`core/friend_scan.py::cancel()` 不再立刻置终态，等浏览器真的关掉 |
| 卡住的会话让账号永远用不了 | 会话只回收"非 active"的，卡在"进行中"的没人管 | `SESSION_STUCK_SECONDS = 20 分钟` + `_reap_stuck_sessions()`：先发取消信号再从注册表清掉，发起新会话前也会清一次 |
| 清掉一个账号之后，历史轮次开始"猜"原因 | 删掉账本里的账号条目后，那些轮次就变成"没有任何账号结果"，页面会用兜底文案猜"账号凭据可能未配置" | 那条猜原因的兜底文案已删；这类轮次改成明确的判决「没有账号结果」（`status="empty"`）；真要补一句真话就手写在那条轮次的 `note` 里（账本的 `note` **永远优先**于页面兜底文案），别回头改 `status`/`totals` |
| 账号全被门禁拦下（登录失效/掉登录/超时），那一轮却显示绿色「成功」 | 轮次判决只看"有没有失败"，而每个账号都是 `skipped` 时既没成功也没失败 | 新增判决「账号全被跳过」（`status="blocked"`，`totals["entries"] == 0` 时才算）；只要有一个账号真跑了就还是「成功」，别把话说反 |
| `.env` 里没有的下拉项被"顺手"写死 | 浏览器遇到"当前值不在选项里"会默认选中**第一项**，用户不动它直接点保存，就把一个从来没配过的键写成第一项的值（例如凭空写出 `DRY_RUN=off`） | 缺项时下拉框最前面插一个空值占位项「（.env 里没有，工具用默认值 X）」—— 它与 `.env` 原文一致，`collect()` 据此判定"没动过"，一个字节都不写；认不出的当前值同样插在最前面，不会被第一项顶掉 |
| 正在编辑的配置被 10 秒轮询改回旧值 | 表单字段若复用 `data-overview-value` 挂点，总览轮询会把输入框里的内容换成 `.env` 里的旧值 | 表单用 `data-config-input`，`app.js` **完全不认识**这个挂点（`FrontendAgreementTests::test_配置表单不会被总览轮询改写` 钉住）；口令表单同理用 `data-password-*` |
| 空跑跑了一轮，页面安安静静地把它讲成发送 | 空跑药丸与那两句说明由模板的 `{% if summary.dry_run %}` 决定在不在页面上 —— 首屏打开时档位是 `off`，之后切成空跑，页面上根本没有可写的节点（2026-10-18 issue #9） | 两块都改成**常驻元素 + `hidden`**，两种说明（各账号档位不一致 / 整页一个档位）各自常驻、档位文字单独挂一个槽；`/api/ops/overview` 的 `summary` 新增 `dryRun`/`dryRunText`；`FrontendAgreementTests::test_空跑药丸与两种说明都能被轮询重画` 钉住 |
| 跑到一半命中风控、跨进禁跑窗，页面顶部什么都不说 | 顶部状态横幅只有服务端渲染那一刻算一次：`/api/ops/overview` 的 payload 里**没有 `banners`**，前端也就没有可用来重画它的数据，那条倒计时永远等不到它的横幅（2026-10-18 issue #10） | 横幅常驻一个容器（`data-status-banner-list`）+ 模板母版（`data-status-banner-tpl`），payload 新增 `banners`（文案仍然只有 `viewmodel._banners()` 一处，前端只切显隐与文字）；倒计时改成**每秒重新取节点**，新插进来的横幅也有倒计时 |
| 运行期间还能点「移除 / 重新登录」 | `updateActions()` 认的 `data-action-count-source`/`data-action-count`/`data-disable-while-running` 三个挂点在 `templates/` 下命中数是 0 —— 它自 `9c7a3a8` 起就是死代码，承诺的"运行期间禁用"从未发生（2026-10-18 issue #11） | 三个计数挂点连同 `updateActions()` 一起删掉；"运行期间不许动账号"改由 `data-disable-while-running` 接管，轮询按 `task.running` 写 `disabled`，服务端首屏同样渲染；「重新登录」另有一路禁用理由（扫码会话开着），也渲染成 `data-login-active` 发给轮询，避免 10 秒后把服务端禁用的按钮重新点亮 |
| 账号看着还登录着（页面正常、cookie 也没过期），到点却一条都发不出去，账本还写着"今天已发" | 服务端把这个会话**踢下线**了：发送接口的响应体是一句 `{"decision": "KICK"}`，不是 protobuf 信封。旧代码只解出 `code=None status=""`，按普通发送失败处理 —— 重试 1/1 又发一遍、之后每个目标白等 20 秒超时，账号级状态仍是 `ok`（2026-10-20 两个账号整天没发出去就是这个形状） | `decode_send_resp()` 多解出 `decision`/`kicked`；`ImMonitor._handle_send` 见到 KICK 就置"登录已死"并触发掉登录回调；`_wait_receipt()` 立刻返回（不再白等到超时）；`type_and_send()` 给出 `reason="session_kicked"`，**不复检、不重试**；`core/tasks.py` 记 `session_kicked`、本轮立即停手、**不占当天名额**（error 而不是 ok）；账号卡标「账号阻断」并提示去「扫码登录」重扫，重登成功后当天自动补发 |
| 扫码会话取消/结束了，「重新登录」按钮却一直是灰的 | 那条禁用理由（"有名会话开着"）只写在服务端首屏的 `data-login-active` 上，轮询只读不写 —— 属性停在 `"1"`，按钮就永不亮回来，只能手动刷新页面（2026-10-20 的投诉） | `/api/ops/overview` 新增 `login.active`；`app.js` 每轮先同步 `data-login-active` 再算 `disabled`（只影响带这个属性的按钮，`task.running` 那一路不变）——会话结束后一个轮询周期内自己解锁 |
| 同一份新 cookie：面板扫码会当天补发，configTool 扫码不会 | "登录成功 → 撤今天的记账"这套判定原来写在 `webui/app.py` 里，只有面板扫码那条路调得到；configTool 那条路只写 `.env`，于是用户从它登录完，当天的火花静默丢失（2026-10-09 事故换个入口重演，2026-10-18 issue #12） | 判定搬到 `core/schedule.requeue_after_relogin()` **只有一份**，面板扫码与 configTool 登录成功都调它；另加「补发今天」按钮（`POST /ops/account/requeue`）作为手动出口 —— 哪条路都没走到时，用户不必去手改 `logs/schedule_state.json` |
| 在面板里改了口令，登出后老口令照样能进 | 只写了 `WEBUI_PASSWORD`，而 `.env` 里还留着更早的 `WEBUI_PASSWORD_HASH` —— 校验是**哈希优先** | 两个键一起写（不用的那个清成空行）；再给 token 加 `credential_fingerprint()`，改完口令所有旧会话立刻失效 |
| 以为"登录要用密钥" | `WEBUI_SESSION_SECRET` 的注释写成"会话签名密钥"，看着像是登录凭据 | 文档与 `.env` 注释都点明它**只是 cookie 的签名密钥**，登录框只认 `WEBUI_PASSWORD` / `WEBUI_PASSWORD_HASH`；口令能在面板「访问口令」里自己改 |
| 面板预览是三行，真发出去是一行、行尾还挂个 `n` | 模板里写的字面 `\n` 在**配置层**就被吃掉了：单元文件用了 systemd 的 `EnvironmentFile=`（它按转义序列解析值），而 `main.py` 当年 `load_dotenv(".env")` 的 `override` 默认 `False`，被弄坏的进程环境反而赢了文件 | 两份 unit 都不写 `EnvironmentFile=`；`.env` 只由程序读（`utils/config.load_env_file`，绝对路径、**文件优先**，`DRY_RUN`/`RUN_MODE`/`RUN_SOURCE` 仍归启动方）；进程值与文件值不一致时打 WARNING 报出两边；面板预览读的就是这份文件，不存在"两套值" |
| `dry-run=full` 读回了文本，却查不出换行丢了 | `_readback_matches()` 两边都 `re.sub(r"\s+","")` 再比字符 —— `"甲\n乙"` 与 `"甲乙"` 归一化后相等，换行没了也判 ✅ | 改成**按行**比（`line_list()`），并返回读回原文；读回改用 `innerText`（`textContent` 看不见 `<br>`/块边界）；合成输入的换行改成**自校验 + 三级退化**（keydown → `insertLineBreak` → `insertText`），结果写进 `brk`，三种都不行就打 WARNING「换行没插进去：这条消息会挤成一行」 |
| 空跑里"清空输入框"看着成功了，第二次输入却把上一条又打了一遍 | 清空只做了"全选 + `execCommand('delete')`"：它**只抹 DOM、不动 Draft.js 的 EditorState**，当场判空是空的，下一次输入 React 重渲染时旧文本又回来 | `JS_EDITOR_CLEAR` 改成**先走 Draft 自己的键盘路径**（全选 + 合成 Backspace，与软换行同一条路），再退到 `execCommand('delete')`，每条路都用删掉零宽字符后的可见文本自校验并回报 `how`；`_clear_editor()` 清完等 250ms 复查、最多重试 `tries` 轮，且"上一次打过字"时会先插一个字符逼 Draft 重渲染（`distrust_empty`）把藏在 state 里的残留逼出来 |
| 运行详情里冒出一串英文码（`editor_text_mismatch` 之类） | `core` 每新增一个判定原因码，面板的 `REASON_LABELS` 就得跟着加一条；漏了就回退成"原样显示码" | 补上中文标签（输入框相关的四个码都要点名"输入框"，否则用户看不出是哪儿的问题）；`tests/test_viewmodel.py::ReasonLabelCoverageTests` 直接从 `core/douyin_im.py`/`core/strict_verify.py` 源码里抓 `reason=` 码，漏一个就红，顺带钉住"标签不重复、不为空" |
| 面板把证据说强了：写「仅 HTTP 回执」，像是"另一侧看过、不认" | `_wait_receipt()` 拿到 HTTP 回执后**最多再等 1 秒**就收工（`if http and time.time() - t0 > 1.0: break`），所以 `via="http"` 只是"HTTP 先到了、会话回显没等到"，不是"回显里没有这条" | `VIA_LABELS["http"]` 改成「HTTP 回执（会话回显没等到）」；被拦下没点发送的那两条走 `via="none"`，也要有中文（「没发送（先拦下了）」），不能只显示 `none`；`tests/test_viewmodel.py::ViaLabelCoverageTests` 只从 `type_and_send()` 体内抓 `via` 取值（`_wait_list_ready()` 的 `"dom-stable"` 另有含义，不算），并禁止 `http` 那条再出现「仅」 |

排查手段（都在仓库里，只读）：

- `logs/runs.jsonl`：账本，页面上的原因/证据/结论最终都来自它（`tail -n 1 logs/runs.jsonl | python -m json.tool`）。
  对不上页面时先看这里，再看 `logs/schedule_state.json`（"今天已发"只认它）。
- 面板首页横幅与账号卡 = `/api/ops/overview` 的同一份 viewmodel；`/ops/runs` 是运行记录，
  `/ops/log-tail` 是日志尾。三处与账本逐项对账，就能分清是"core 记错了"还是"面板画错了"。
- `.venv/bin/python -m webui --check`：只校验配置不启动（`.env` 能否解析、口令配没配、绑定地址）。
- `.venv/bin/python main.py dry-run select`：不真发，把"这一轮到不到点、该找谁、门禁什么状态"打出来。
- `.venv/bin/python main.py dry-run full`：真输入 + 读回校验 + 清空编辑器；日志里
  `[SEND] 待发送文本 '…' → N 行` 是"这一轮到底按几行发的"的实锤。
- `.venv/bin/python main.py prune`：只报告登录资料目录/缓存占用（加 `--apply` 才删）。


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
| 忘了面板口令，进不去 | 在服务器上 `cd /opt/douyin-spark-fusion && .venv/bin/python -m webui.reset_password`（不带参数就随机生成一个并打印一次，写进 `.env` 后重启 web 服务并轮询 `/healthz`）。手工等价做法：`python -m webui.hash_password '新口令'` 写回 `WEBUI_PASSWORD_HASH`（或直接写 `WEBUI_PASSWORD='新口令'`）后重启 `douyin-spark-fusion-web.service` |
| 首页账号/cookie 是旧的 | 配置在进程内缓存，`systemctl restart douyin-spark-fusion-web.service` |
| 点"立即运行"返回 503 | 这台机器上没装 `cloakbrowser`/`playwright`（面板本身不需要它们，跑任务才需要） |
| 点"立即运行"返回 409 | 两种原因：①已有一轮在跑（`logs/run.lock` 里的 pid 还活着）；等它结束，或确认进程已死后删掉锁文件。②当前处于禁跑时间窗（`QUIET_WINDOW`，默认 `off`；设了才会拦）；在「运行配置」里改窗口，或临时设 `QUIET_WINDOW_OVERRIDE=1` |
| 日志页显示"文件不存在" | `OPS_LOG_FILE` 指向的文件还没产生；systemd 部署下应指向 `logs/douyin-spark-fusion.log` |
| 消息发出去没有换行、行尾多个 `n` | 模板里的字面 `\n` 在配置层被吃了。①`.env` 里消息模板必须写**两个字符** `\n`（不是真回车，真回车会把 `.env` 拆成两行）②`grep -n '^EnvironmentFile' /etc/systemd/system/douyin-spark-fusion-*.service` **必须没有输出**（systemd 按转义序列解析 environment file），改完 `systemctl daemon-reload` 并重启 web 单元。确认：`.venv/bin/python main.py dry-run full`，日志里 `[SEND] 待发送文本 '…\\n…' → 3 行` 才对；面板「运行配置」的模板预览读的就是同一个文件 |
| `dry-run=full` 里 `已清空=False`（连着重试一次，读回变成两遍文本） | 清空输入框只用了 `execCommand('delete')`：Draft.js 的内容真相在它自己的 EditorState 里，这样**只抹 DOM**。当场判空是空的，可下一次输入 React 重渲染又把旧文本带回来，于是重试时两条消息叠着发。现在 `JS_EDITOR_CLEAR` 先走 Draft 自己的键盘路径（全选 + 合成 Backspace），`_clear_editor()` 清完等 250ms 复查、最多重试 3 轮，还会插一个字符逼它重渲染把藏在 state 里的残留逼出来。日志看 `已清空=True（清空方式=key 或 cmd）`—— 现场两种都出现过，`cmd` 表示"键盘那条路没清干净、靠 `execCommand('delete')` 收尾"，只要 `已清空=True` 且没有 `重试一次` 就是干净的 |
| 日志里发送响应只有一句 `{"decision": "KICK"}`，之后全是"回执超时" | 服务端把这个会话踢下线了（不是普通失败，也不是 cookie 过期）。现在会被识别成 `session_kicked`：本轮立即停手、不重试、不占当天名额，账号卡提示「账号阻断 · 重新扫码登录」。处理方式只有一条 —— 去「扫码登录」重扫该账号，登录成功后当天会自动补发；**别反复点「立即运行」**，那只会把同一个死会话再喂一遍 |
| 扫码登录明明结束了，「重新登录」还是灰的 | 2026-10-20 之前要先手动刷新页面：按钮的第二路禁用理由（扫码会话开着）原来只写在首屏 HTML 上，轮询不更新。现在 `/api/ops/overview` 每轮下发 `login.active`，一个轮询周期内会自己亮回来；如果 30 秒还不亮，`systemctl restart douyin-spark-fusion-web.service` 后看 `logs/app.log` 里 `/api/ops/overview` 是否还报错 |
| 运行记录里"原因"一栏显示英文 | `core` 新增了判定码，而 `webui/viewmodel.py` 的 `REASON_LABELS` 没跟上（`_reason_text()` 找不到就原样显示） | 在 `REASON_LABELS` 里补一条中文（**别在模板里写死**，否则运行记录页和首页会各说各话）；`ReasonLabelCoverageTests` 会替你盯住这件事 |
| "证据"一栏写着"HTTP 回执（无法确认收件人）"，是不是没发出去？ | 回执是抖音自己给的（`code=0` + `message_id`），它证明**服务端收下了这一条**，但回执里**没有**收件人字段，所以还要靠页面说"现在停在哪个会话"来点名收件人。**强确认** = 回执 ok **且** 发送后当前会话就是目标会话；点不出收件人（会话 id 读不到）或只见到气泡没有回执，都算**弱证据**（账本里 `ok` 仍是 True —— 照旧算发过、当天不重发，只是面板不把它当"铁证"）。刻意**不要求**气泡数上涨：虚拟列表不重渲染时气泡数不涨是常态，拿它当必要条件会把每一次发送都判成弱证据。**"点不出"和"点错了"是两件事**：发送后停留的会话**不是**目标会话（`receipt_wrong_conv`）判**失败** —— 消息送到了别人那里，账本 `ok=False`、面板写「可能发错人」，当天不算已发并会重试 | 想让强确认变多，别在发送期间切走那个聊天窗口（代码按会话 id 核收件人，切走就核不上）；要查某一轮到底怎么判的，看日志里 `[SEND] ✅ HTTP 回执 …`、`[SEND] ✅ DOM 确认：消息数 …` 以及 `[SEND]` 后面那句降级原因 |
