# 部署到自有服务器（systemd）

融合版**不依赖 GitHub Actions、不依赖 Docker**：一台能跑 Chromium 的 Linux 机器 +
systemd 就够了。下面以本机实际的 `本机`（Ubuntu 24.04，root）为例。

> 面板与定时任务是**两个独立单元**：面板常驻（随时看状态、手动触发、跑空跑），
> 定时器负责每天真正发一轮。可以先只开面板，确认无误再开定时器。

---

## 0. 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| 系统 | Ubuntu 22.04/24.04（其它发行版同理，只要有 systemd） | `systemctl --version` |
| Python | 3.10+（`本机` 上是 3.12.3） | `python3 -V` |
| venv | `python3-venv` | `python3 -m venv --help >/dev/null && echo ok` |
| 网络 | 能直连 `pypi.org`；能访问 `github.com`（只在需要下载浏览器时） | `curl -sS -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/` |
| 浏览器 | 系统 Chrome，或 cloakbrowser 自带的 Chromium，或 Playwright 的缓存 | `ls -l /opt/google/chrome/chrome` |
| 端口 | 默认 **8787**（避开既有服务的 8000 / 8080） | `ss -lntp \| grep 8787` |

**root 运行必须带 `--no-sandbox`** —— Ubuntu 24.04 的 `kernel.apparmor_restrict_unprivileged_userns=1`
让 Chromium 的沙箱起不来。`core/browser.py` 检测到 root 会自动追加 `--no-sandbox --disable-setuid-sandbox`
（可用 `CHROME_NO_SANDBOX=0/1` 覆盖）。

---

## 1. 装代码与依赖

把仓库传到目标机（`git clone` 或 `scp -r`），然后在**仓库根**执行：

```bash
sudo bash deploy/install.sh
```

它做五件事（幂等，可重复执行）：

1. 同步代码到 `/opt/douyin-spark-fusion`（`APP_DIR=` 可改；不带 `--delete`，**不会删掉你已有的
   `.env` / `logs/` / `.venv`**）；
2. 建 `.venv` 并 `pip install -r requirements.txt`；
3. 确保浏览器可用：优先下载 cloakbrowser 的 Chromium 到 `/opt/cloakbrowser/`（带 sha256 校验），
   失败则回落到系统 Chrome 并写 `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/google/chrome/chrome`；
   最后跑一次 `tools/browser_probe.py`，成功会打印 `BROWSER_PROBE_OK`；
4. `.env` 不存在才从 `.env.example` 生成，并填好随机 `WEBUI_SESSION_SECRET`、`WEBUI_HOST=0.0.0.0`、
   `WEBUI_PORT=8787`（`chmod 600`）；
5. 安装三个 systemd 单元并 `daemon-reload`（**不 enable**）。

> **单元里故意不写 `EnvironmentFile=`**：systemd 解析 environment file 时按"转义序列"处理值，
> `.env` 里消息模板写的字面 `\n`（规则："用 `\n` 表示换行"）会被吃成裸 `n`，
> 发出去的消息就挤成一行、行尾挂个 n。`.env` 一律由程序自己用 python-dotenv 读
> （`main.py` / `python -m webui`，绝对路径、**文件优先**）。详见 `docs/webui.md` §8。

只想装 Python 侧、不碰浏览器：`sudo SKIP_BROWSER=1 bash deploy/install.sh`。

---

## 2. 填 `.env`

```bash
sudo nano /opt/douyin-spark-fusion/.env
```

至少要填三样：

```dotenv
# 1) 账号与目标（JSON 必须写成单行！）
TASKS=[{"username":"我的号","unique_id":"12345678901234567","targets":["好友A","好友B"]}]
COOKIES_12345678901234567=[{"name":"sessionid","value":"...","domain":".douyin.com","path":"/"}]

# 2) 面板口令（绑 0.0.0.0 时必填，否则拒绝启动）
WEBUI_PASSWORD=一个只有你知道的口令
#   或者用哈希（更安全，见下）
# WEBUI_PASSWORD_HASH=pbkdf2_sha256$<salt>$<digest>

# 3) 面板监听
WEBUI_HOST=0.0.0.0
WEBUI_PORT=8787
```

生成口令哈希（不用把明文留在 `.env` 里）：

```bash
cd /opt/douyin-spark-fusion && .venv/bin/python -m webui.hash_password
```

**口令平时不用碰服务器**：登录面板后首页「访问口令」（侧栏「管理 → 访问口令」）里就能改，
明文/哈希两种存法随选，改完所有旧会话立刻失效。忘了口令时在服务器上：

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python -m webui.reset_password               # 生成一个随机口令，打印一次，写进 .env 并重启面板
.venv/bin/python -m webui.reset_password '我的新口令'    # 直接用你给的这串
```

它走的是和面板同一个写入口（`webui/envfile.py::set_webui_password`：先备份、拿跨进程文件锁、
明文与哈希两个键一起写），写完重启 `douyin-spark-fusion-web.service` 并轮询 `/healthz`。
`--hash` 存哈希，`--no-restart` 只写文件，`--dry-run` 只说会写哪个文件。

其它可选键（默认值见 `.env.example`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `DRY_RUN` | `off` | 空跑档位：`off` / `select` / `full`。**认不出来的值一律回落 `off`（真发）**，不会悄悄变空跑 |
| `LIMIT_GUARD_ENABLED` | `true` | 风控熔断开关 |
| `LIMIT_COOLDOWN_MINUTES` | `30` | 命中后冷却多少分钟不再发 |
| `QUIET_WINDOW` | `off` | 禁跑时间窗（`HH:MM-HH:MM`，跨零点也认）。这段**绝对不发**，用来避免两个程序抢同一个账号；`off`（默认）关闭。盖住定时器时间会让那一轮整轮跳过，面板会报红 |
| `QUIET_WINDOW_OVERRIDE` | 空 | 设为 `1` 时**临时无视** `QUIET_WINDOW` 放行一轮（默认窗口内面板"立即运行"会返回 409） |
| `SEND_INTERVAL_MIN_SECONDS` / `MAX` | `8` / `20` | 每两个目标之间的额外间隔（0/0 = 不额外等待） |
| `RUN_START_JITTER_SECONDS` | `90` | 定时器到点后先随机等 0~N 秒再开浏览器（磨掉每 5 分钟整分敲门的边缘）。只影响定时触发；面板/命令行立即运行与空跑都不等 |
| `SEND_INTERVAL_LONG_PAUSE_SECONDS` | `75` | 长尾停顿：约 15% 的概率在正常间隔之外再多停 0~N 秒；`0` = 回到纯均匀分布 |
| `WARMUP_BROWSE_SECONDS` | `0` | 暖场浏览：门禁通过后先在会话列表里滚 N 秒再发（只滚不点，失败不影响发送）；`0` = 关闭 |
| `TYPING_CHUNK_CHARS` / `TYPING_CHUNK_DELAY_MS` | `6` / `110` | 分段输入：按 N 字切块、块间停 M 毫秒；任一为 `0` = 一次性打完整段 |
| `RUN_LEDGER_FILE` | `logs/runs.jsonl` | 运行账本（面板读它画图表） |
| `RUN_LOCK_FILE` | `logs/run.lock` | 运行锁（防止两个任务同时开浏览器） |
| `RATE_LIMIT_FILE` | `logs/rate_limit.json` | 冷却状态 |
| `OPS_LOG_FILE` | `logs/app.log` | 面板"日志"页读的文件（默认读 Python 日志 `logs/app.log`）。`deploy/` 里的两个 systemd 单元把标准输出追加到 `logs/douyin-spark-fusion.log`，`.env.example` 给的也是后者 —— 想让面板同时看到单元输出，就显式把它设成 `logs/douyin-spark-fusion.log` |
| `BROWSER_BACKEND` | `auto` | `auto` / `cloakbrowser` / `playwright` |
| `TASK_SCHEDULE_TIMES` | `20:30` | **每个账号的发送时间**：当天的门铃第一次到点之后才发。面板上单独给某个账号设了时间就以账号的为准，这里是没单独设的账号的默认值 |
| `SCHEDULE_JITTER_MINUTES` | `15` | 发送时间上的随机抖动（分钟），**前后对称**：设定 20:30、抖动 15 就是在 20:15–20:45 里挑一个时刻。按「账号 + 日期」**确定性**取值 —— 同一天同一账号每次算出来一样，不会每 5 分钟漂一次；`0` = 准点发 |
| `SCHEDULE_STATE_FILE` | `logs/schedule_state.json` | 发送时间表：哪个账号哪天发过了。删掉它等于「今天全都没发过」，当天还会补发 |
| `NOTIFY_WEBHOOK_URL` | 空 | 结果通知地址（Server酱 / 企业微信 / 钉钉 / 飞书 / Telegram，或任意收 JSON 的地址）。留空 = 不通知 |
| `NOTIFY_LEVEL` | `error` | `off` / `error` / `always`。`error` 只在有账号失败或命中熔断时推；空跑永远不推 |
| `NOTIFY_TIMEOUT_SECONDS` | `10` | 推送超时（3–60 秒） |
| `WEBUI_DEFAULT_DRY_RUN` | `off` | 面板"立即运行"默认选中的档位（`off`/`select`/`full`）；只是默认值，点按钮前仍能改 |
| `LOGIN_PROFILE_DIR` | `logs/login-profile` | 面板扫码登录用的浏览器资料目录；**等价于凭据**，别进备份/别共享 |
| `BROWSER_STATE_DIR` | `logs/browser-state` | 每个账号的浏览器状态快照（`<抖音号>.json`，发送时挂上去，让每轮都还是同一台设备）；**等价于凭据**。删掉不炸，只是下一个账号首次运行会以新设备身份重新登录一次 |
| `BROWSER_TIMEZONE` | 空 | 浏览器自报的时区（IANA 名）。**留空 = 跟服务器系统时区走**；要和出口 IP 对得上 —— 这台系统时区 `Asia/Shanghai`、出口在香港，所以生产写 `Asia/Hong_Kong`。写错只告警、退回系统时区，不影响发送 |
| `BROWSER_LOCALE` | `zh-CN` | 浏览器自报的语言（BCP 47），落到 `--lang` 与每个上下文的 `locale` |
| `QR_LOGIN_TTL_SECONDS` | `300` | 面板扫码登录的二维码有效期（秒） |
| `FRIEND_SCAN_TIMEOUT_SECONDS` | `300` | 面板"读取好友列表"单次扫描的超时（秒）；超时标成读取失败，已读到的部分仍可用 |

### 结果通知推到哪里

配了 `NOTIFY_WEBHOOK_URL` 就会在出问题时收到推送；不配则这一层完全不工作 ——
定时器一天敲 288 次门，只有真出事才值得打扰。地址是谁家的靠主机名认，不用手选格式：

```dotenv
# Server酱（微信）
NOTIFY_WEBHOOK_URL=https://sctapi.ftqq.com/SCTxxxxxxxx.send
# 企业微信群机器人
# NOTIFY_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
# 钉钉群机器人
# NOTIFY_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxxxxxxx
# 飞书群机器人
# NOTIFY_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
# Telegram
# NOTIFY_WEBHOOK_URL=https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>
NOTIFY_LEVEL=error
NOTIFY_TIMEOUT_SECONDS=10
```

认不出来的主机名按通用 JSON（`title` / `text` / `status` / `run_id` / `accounts`）发，
接自己的机器人或转发服务都行。

配完先验一次，不用等出事故：

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python main.py notify-test        # 成功退出码 0，失败 1 并打印对方原始回应
```

面板首页「运行配置」最下面也有一个**发送测试通知**按钮，走同一条路。

### 静默体检（`main.py doctor`）

通知只在「真发完一轮」之后推。可定时器被 `systemctl disable`、派发器每 5 分钟照常敲门却
没账号到点（按设计不写账本）、TASKS 里唯一账号掉登录 —— 这三种故障**一轮都不会产生**，
面板和通知全都安静。所以另有一个只读体检：

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python main.py doctor              # 人话几行；有事退出码 1
.venv/bin/python main.py doctor --json       # 同上，机器可读
.venv/bin/python main.py doctor --no-notify  # 只检查，不推送
```

一共看六件事：账本还新吗（`NOTIFY_SILENCE_HOURS`，默认 30 小时；0 关闭）、定时器还在不在、
面板服务（`douyin-spark-fusion-web.service`）还活着吗、账号是不是全停用、以及**每个账号最近
有没有真发出去**。**全停用不算故障** —— 那是你自己关的；面板读不到状态也不算故障（不知道 ≠
坏了），只有它真到了 `failed` / `inactive` 才是 ⛔。

最后一条补的是一个很能骗人的盲区：账号掉登录之后每天照样跑、照样写一条 `error` 记录，账本
永远是新的、定时器永远 active —— 只看「账本新不新」的体检于是天天说一切正常，而实际上一条消息
都没发出去。现在：最后一轮没发出去就 ⚠️ 提醒，连着三轮没发出去直接 ⛔ 算故障，并且点名是哪个
账号、最后一次是什么错、该去「扫码登录」重扫。已停用的账号不参与判定（那是你关的）。
`deploy/install.sh` 会装一个 `douyin-spark-fusion-doctor.timer`（每天 09:00）并自动启用；
它只读、不发消息，所以和必须人点头的 `daily.timer` 不同。不想跑就
`systemctl disable --now douyin-spark-fusion-doctor.timer`。

几点要知道的：

- **HTTP 200 不代表推送成功**：这几家都是 200 + JSON 里的错误码（token 过期、被移出群、
  频率超限），所以看的是回应体；失败只写一行 `warning`，绝不改这一轮的结论。
- **推送在放锁之后**：不影响时间表，也不占运行锁；最坏让这一轮多花
  `NOTIFY_TIMEOUT_SECONDS` 秒。
- **空跑永远不推**：`DRY_RUN` 不是 `off` 的时候一条都不发，免得预演变成骚扰。
- **日志只留 host**：地址通常带 token，日志与页面只显示 `scheme://host/***`。
- **改完立刻生效**：这一层每次运行都重新读 `.env`，不用重启服务。

### 消息模板里的换行：写两个字符 `\n`

`.env` 是"面板预览看到的"和"实际发出去的"**同一份真相**：程序读它、面板也读它。
模板里换行必须写成**字面两个字符** `\n`（反斜杠 + n），运行时由 `split_message_lines()`
还原成真换行；直接敲回车会让 `.env` 变成两行、值被截断：

```dotenv
MESSAGE_TEMPLATE=[盖瑞]今日火花[加一]\n—— [右边] 每日一言 [左边] ——\n[API]
```

面板「运行配置 → 消息模板」里也是同一个口径（输入框下方写着"用 `\n` 表示换行"），
存盘后下面「消息模板预览」按实际发送的样子分行显示。改完想确认真的分行，
用 `main.py dry-run full` 跑一轮：它会读回编辑器里的文本并**按行**比对，
换行没插进去会明确报出来（只比字符的老实现查不出这个，见 `docs/webui.md` §8）。

> `RUN_SOURCE`（触发来源）**不写在 `.env` 里**：`.env` 是三个入口共用的，
> 写死会让面板分不清"谁跑的"。它由命令行（默认 `cli`）、
> `douyin-spark-fusion-daily.service` 里的 `Environment=RUN_SOURCE=timer`、
> 面板"立即运行"（`webui`）三处各自决定 —— 面板运行明细里的"来源"列就是它。
> 临时用别的词（例如 `RUN_SOURCE=manual` 手动跑一轮验收）也行：认不出来的值原样进账本，
> 面板只对登记过的几个给中文（`webui/store.py` 的 `SOURCE_LABELS`），
> 所以**常用词要顺手补一条标签**，别让用户在页面上看见英文。
>
> `QUIET_WINDOW` **默认 `off`**：这段绝对不发，用来避免两个程序抢同一个抖音账号。
> 2026-10-01 之前默认是 `20:20-21:00`，为了避开当时还在跑的 `douyin-spark.service`
> （每天 20:30 起加 0~20 分钟抖动真的在发私信，同一个账号被两台 Chromium 同时登录会互踢）；
> 那套服务已经停用禁用，融合版是唯一的发送方，所以默认关掉了。真要用（比如夜里不想发）
> 就在面板「运行配置」里写 `HH:MM-HH:MM`。
> **注意**：窗口盖住定时器时间的那一轮会整轮跳过（账本写"整轮跳过（quiet_window）"），
> 面板「禁跑时间窗把定时器的时间盖住了」红框会提前报出来 —— 别让它静默吃掉一轮。
> 面板点"立即运行"落在窗口内会拿到 409 并说明原因。

---

## 3. 先空跑（不发任何消息）

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python main.py dry-run full      # 真输入到编辑器并读回校验，但不点发送
.venv/bin/python main.py dry-run select    # 只做"扫描 + 严格确认会话"，连输入都不做
```

- `full`：扫描 → 严格确认 → **真的把消息输入编辑器 → 读回文字校验 → 清空编辑器** → 不发送。
  能暴露"编辑器找不到 / 换行处理错 / 合成输入没进去"这类只有真输入才暴露的问题。
- `select`：只验证"目标会话被正确选中"，最快。
- **空跑与真跑走同一条严格校验链路**，所以空跑通过 ≈ 真跑也能通过（差别只在最后那一次点击）。

退出码：`0` = 正常跑完；`1` = 整轮被跳过（冷却中/已有任务在跑）或有账号报错；`2` = 参数错。

> ⚠️ **改完 `.env` 要重启面板服务**：配置与账号数据在进程内只读一次（`utils/config.py` 缓存），
> 命令行与定时器每次都是新进程、自动生效，但常驻的面板进程会一直用旧值：
> `systemctl restart douyin-spark-fusion-web.service`。填好真实 `TASKS` / `COOKIES_<uid>` 后务必重启一次。
>
> `.env` 有多个写入者（面板改设置、扫码登录、`core/cookie_sync` 回写 cookie），它们用同目录下
> 的 `<env 文件名>.lock` 排队（`flock`；空文件、`0600`，默认就是 `.env.lock`），不会互相覆盖掉
> 对方刚写的键。锁文件删掉不影响：下一次写会重建。`configTool` 是独立程序，不参与这把锁 ——
> 别在它保存的同时让面板也在写同一个文件。

---

## 4. 启动面板

```bash
systemctl enable --now douyin-spark-fusion-web.service
systemctl status douyin-spark-fusion-web.service
```

- 面板：`http://<本机IP>:8787/`（当前这台 → http://192.168.15.172:8787/；换机器就换 IP 或挂域名/反代）
- **绑非回环且没配口令时服务会拒绝启动**（退出码 2，`journalctl -u douyin-spark-fusion-web -n 20` 有中文原因）。
- 日志：`logs/douyin-spark-fusion.log`，同时 `journalctl -u douyin-spark-fusion-web -f`。
- 只校验配置不启动：`.venv/bin/python -m webui --check`。

面板能做什么：看运行状态与上次结果、看每个账号的登录态/成功失败/未找到的目标、
看限流冷却倒计时、**选空跑档位后手动触发一轮**、看日志尾部并下载完整日志。

---

## 5. 最后才开定时发送

```bash
systemctl enable --now douyin-spark-fusion-daily.timer
systemctl list-timers douyin-spark-fusion-daily.timer
```

- **本项目的 timer 是「门铃」，不是「发送时刻」**：`OnCalendar=*:0/5` + `Persistent=true`，每 5 分钟敲一次门；
  应用醒来后自己判断「这个账号今天到点没有、发过没有」（时间表落在 `SCHEDULE_STATE_FILE`，默认 `logs/schedule_state.json`）。
  所以要改发送时间，改的是 `.env` 里的 `TASK_SCHEDULE_TIMES`（默认 `20:30`），或者在面板上给单个账号设「发送时间」——
  **不要**去改 timer 的 `OnCalendar`：把它改回「一天一次」，那一刻以外的门铃就全没了，账号会整天一条都不发。
- 抖动也在应用里算（`SCHEDULE_JITTER_MINUTES`，默认 15 分钟），timer 上的 `RandomizedDelaySec` 已经移除 ——
  以前它按轮次随机，现在按「账号 + 日期」确定性随机，同一天每个账号的发送时刻是稳定的。
- **同机共存提醒**：如果这台机器上还跑着别的续火花项目、**且用的是同一个抖音账号**，
  两个 Chromium 同时登录会互踢并触发风控 —— 这时才需要 `QUIET_WINDOW`（错开时段），
  或者把两套的时间岔开。2026-10-01 本项目部署机上的 `douyin-spark.service` 已经停用禁用，
  不再需要这个窗口；
- 手动跑一次定时任务单元（不影响 timer）：`systemctl start douyin-spark-fusion-daily.service`。
- 任务日志：`logs/douyin-spark-fusion.log` / `journalctl -u douyin-spark-fusion-daily`。

---

## 6. 运行时文件

| 文件 | 内容 | 谁写 |
|---|---|---|
| `logs/runs.jsonl` | 每行一轮运行的结构化结果（账号、每个目标的结果、是否熔断/跳过） | 任务 |
| `logs/run.lock` | 运行锁（`pid` + `started_at` + `token`） | 任务 |
| `logs/rate_limit.json` | 熔断冷却状态（命中关键词、来源、冷却截止） | 任务 |
| `logs/douyin-spark-fusion.log` | web 服务与每日任务**两个单元共用**的标准输出（面板"日志"页读它） | systemd |

三个文件路径都能用环境变量改（见上表）。**删掉 `run.lock` 是安全的**：进程不在时它会被自动
判定为残留锁并接管；进程判断不了时按"2 小时未更新"才敢当残留。

### 日志轮转

`deploy/install.sh` 会把 `deploy/logrotate.conf` 装成
`/etc/logrotate.d/douyin-spark-fusion`：`logs/*.log` 每天或涨到 32M 时轮转一次，留 14 份、压缩。

**规则里必须保留 `copytruncate`**：进程是 systemd 拉起来的、不会重开日志文件，用默认的
`create` 会导致轮转后进程继续往被改名走的旧 inode 写，新文件永远空着 —— 现象是"面板日志页
突然不再更新"。自测（`-d` 只演练不真做）：

```bash
logrotate -d /etc/logrotate.d/douyin-spark-fusion
logrotate -f /etc/logrotate.d/douyin-spark-fusion   # 强制轮转一次，然后去面板看日志页还有没有内容
```

### 磁盘与登录资料目录

`logs/login-profile/<抖音号>/` 是扫码登录留下的浏览器资料目录（**等价于凭据**）。Chrome 在里面
写的缓存（`Cache` / `Code Cache` / `GPUCache` / `ShaderCache` …）**默认没有上限**，实测一个账号
一天能长 180MB 左右；账号删了但目录还在的"孤儿目录"也不会自己消失。盘满的表现是发送直接失败，
所以要么定期盘一次：

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python main.py prune                      # 只盘点：每个目录多大、多少是可再生的缓存
.venv/bin/python main.py prune --apply --caches     # 清缓存（保留 Cookies / Local Storage / Local State）
.venv/bin/python main.py prune --apply --older-than 30   # 删闲置超过 30 天的孤儿目录
```

面板上也有同一套：首页「磁盘与资料目录」区块（展开时才盘点），四个按钮分别是"预演/执行 ×
清缓存/清孤儿"。三条安全线在 `core/maintenance.py` 里：**只删白名单里的缓存目录**、**在册账号
永不删**（孤儿还要过 7 天宽限期、且没在跑）、**账号名单读不到就一个都不删**。清理必须以
`root` 跑（资料目录是 `700`）；面板服务本身就是 root，手工跑就 `sudo`。

**也可以让它自己清**：定时器每 5 分钟敲一次门，`main.py task` 会顺带检查一次维护计时器，
到点（默认 `MAINTENANCE_AUTO_PRUNE_HOURS=168`，一周）就自动做一遍上面那两步，日志里会多一行
`[维护] 自动清理：回收 …`。没到点只是一次状态文件读取（约 1 ms），不建浏览器、不碰登录态。
要关掉就把这个键写成 `0`；维护失败（例如 `TASKS` 写坏导致名单读不到）**不会**影响发送，
也**不会**推进计时器，但至少隔 1 小时才重试一次，免得每 5 分钟刷一行错误。
两个边界：**空跑（dry-run）不碰磁盘**（人正盯着屏幕时不该有东西被删）；面板的「立即运行」
走的是进程内调用、也**不做**自动清理 —— 只用面板、不开定时器的话，要清就在上面那个区块里点。

### 浏览器状态快照

`logs/browser-state/<抖音号>.json` 是 Playwright 的 `storage_state`：设备号、localStorage 和那一批
服务端签发的 cookie。发送时用它开上下文、跑完再存回来，让每轮发送都是**同一台设备**。
没有它的时候每轮都是新设备，抖音会把会话当异常，实测登录态只活 9 天左右（见 `FUSION-DESIGN.md` 6.2）。

- 文件 `0600`、目录 `0700`，**等价于凭据**，不进备份、不进 git。
- 删掉/清掉不会报错：下一次运行按全新设备走一遍，不影响当天发送。
- 它和 `.env` 里的 `COOKIES_<抖音号>` 是同一份登录会话：**重新扫码后 `.env` 赢**（新票），
  快照里的设备号照旧沿用（换票不换设备）；平时服务端新签发的 cookie 由 `core/cookie_sync.py`
  自动回写 `.env`，值没变就一个字节都不写。
- 写 `.env` 时会在旁边留一把空的 `<env>.lock`（`0600`）：面板和定时任务可能同时回写，
  两边靠 `flock` 排队，不会互相盖掉刚写的键。锁文件删掉没影响；它只是个门闩，
  里面不存任何内容。别把 `.env.lock` 建成目录（会退化成不加锁）。
- 快照文件坏了（截断、`origins` 结构不对）会被当成"没有快照"，本轮按全新上下文跑，
  跑完用新的一份覆盖掉 —— 不会让这个账号每轮都卡在同一个坏文件上。
- **扫码登录成功时也会写这份快照**（2026-10-22 起）：以"登录这一台"为准覆盖，之后发送沿用同一设备
  （`core/qr_login._save_device_snapshot`）。在此之前登录的账号没有这份快照，首轮发送仍会新开一台，
  下次重新扫码才会统一；也可以在没人用面板时一次性回填（先确认没有"扫码登录"/"读取好友列表"在跑，
  这两个功能会占着同一个资料目录）：

  ```python
  # 在 /opt/douyin-spark-fusion 下用项目 venv 跑
  from core import browser, browser_state
  ctx = browser.launch_persistent_context("logs/login-profile/<抖音号>", headless=True)
  state = ctx.storage_state(); ctx.close()
  browser_state.write("<抖音号>", state)   # → logs/browser-state/<抖音号>.json（0600）
  ```

  回填只写快照、不动 `.env`：同一份会话（`same_session` 为真）下一轮直接沿用；不同则仍注入 `.env` 的票、
  设备号取快照里的。2026-10-22 线上按这个办法回填了两个账号，并用真实浏览器各跑一遍门禁：都是
  `READY`，`web_runtime_security_uid` 与登录资料目录一致。

### 浏览器时区与语言

浏览器会自报时区（`Intl.DateTimeFormat().resolvedOptions().timeZone`、`Date` 偏移）和语言
（`navigator.language`、请求头 `Accept-Language`）。这台服务器的系统时区是 `Asia/Shanghai`，
但实测出口 IP 在香港（HGC 住宅宽带，`223.19.25.170`），UA 又自称 Windows —— **时区和 IP 不在同一地**，
是风控里最便宜的一条对照。所以生产 `.env` 里显式写：

```dotenv
BROWSER_TIMEZONE=Asia/Hong_Kong
BROWSER_LOCALE=zh-CN
```

- `BROWSER_TIMEZONE` 留空 = 跟服务器系统时区走；写错名字只记一条 warning、退回系统时区，
  不会让发送失败（`core/browser.py::browser_timezone`）。
- `BROWSER_LOCALE` 留空 = `zh-CN`。语言不必跟着 IP 归属地改：香港 IP 上简体中文完全正常，
  抖音的主要语言就是简体。
- 换代理、搬家、出口 IP 变了以后**要一起改时区**，否则又会自相矛盾。
- 这两个键**界面里没有输入框**，只能编辑 `.env`；configTool 已登记它们，GUI 保存不会抹掉。
- 验收：真机页面里 `Intl.DateTimeFormat().resolvedOptions().timeZone` 等于配置值、
  `navigator.language` 等于 `BROWSER_LOCALE`；连续两轮不变。

### 字体（可选，但推荐）

Windows UA 的浏览器如果拿不到 `Segoe UI` / `Calibri` / `Cambria`，fontconfig 会回落到 Noto / DejaVu，
同一段文字量出来的宽度就和真 Windows 不同（canvas / 文字测量都能探到）。2026-10-22 在 192.168.15.172
上做了三件事，都是**操作系统级**的，不在本仓库里：

```bash
sudo apt-get install -y fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation2

# Selawik：微软官方的 Segoe UI 度量兼容替代（OFL）
curl -sSLo /tmp/selawik.zip https://github.com/microsoft/Selawik/releases/download/1.01/Selawik_Release.zip
sudo mkdir -p /tmp/selawik /usr/share/fonts/truetype/selawik
sudo unzip -oq /tmp/selawik.zip -d /tmp/selawik
sudo find /tmp/selawik -iname "*.ttf" -exec cp -f {} /usr/share/fonts/truetype/selawik/ \;
sudo tee /etc/fonts/conf.d/60-selawik.conf >/dev/null <<'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <alias binding="same"><family>Segoe UI</family><prefer><family>Selawik</family></prefer></alias>
</fontconfig>
EOF
sudo fc-cache -f
fc-match "Segoe UI"      # → selawk.ttf: "Selawik" "Regular"
```

验收：真机页面里用 72px 量 `mmmmmmmmmmWWWWWWWWWWiiiiiiiiii`，Calibri 1380.94、Cambria 1423.09、
Segoe UI 1467.08 —— 三者不再等于同一个"通用兜底"宽度（1436.77）。
CJK 仍有一条已知差异：`SimSun` 等于 `Noto Serif CJK SC`（1690.92）、`SimHei` 等于
`Noto Sans CJK SC`（1496.89），没有可自由分发的宋体/黑体度量兼容字体。

---

## 7. 升级

线上 `/opt/douyin-spark-fusion` 自 2026-10-24 起是 **git 工作树**（HEAD 跟着 github
`liboxuanzs/douyin-spark-fusion` 走）。日常升级用 `deploy/update.sh`，它只做四件事：
查有没有一轮正在跑 → `git` 只做 fast-forward → 按改动的文件决定要不要 `pip install` /
重装单元 / `daemon-reload` → 只有代码变了才重启面板并轮询 `/healthz`。
`.env` 与 `logs/` 不在 git 里，脚本一个字节都不碰。

```bash
cd /opt/douyin-spark-fusion
sudo bash deploy/update.sh             # ff 到当前分支的上游（线上是 origin/master）
sudo bash deploy/update.sh --check     # 只看有没有新提交，不动任何文件
```

在服务器上自己开发时，把提交从开发目录送过来（**别指望服务器 push github**，它没有凭据）：

```bash
cd /root/dsf && git add -A && git commit -m '...'                  # 开发目录里改完提交
sudo bash /opt/douyin-spark-fusion/deploy/update.sh /root/dsf master  # 线上 ff 到这个本地仓库
```

- 有一轮正在跑（`RUN_LOCK_FILE`，默认 `logs/run.lock`，里面的 pid 还活着）时脚本会**拒绝**：
  模块已经 import 进内存，换文件不会立刻生效，却会让这一轮和下一轮跑在两份不同的代码上。
  确认可以打断才加 `--force`。
- 历史分叉（本地有提交不在来源里）也会拒绝：只做 fast-forward，绝不 `reset --hard` 或强合。
- 回滚：`sudo git -C /opt/douyin-spark-fusion reset --hard <旧提交>` 后重启 web 单元；
  如果那段提交动过 `requirements.txt`，把 `.venv` 的依赖也退回去。
- 任务那一侧不用重启（oneshot，每次运行都是新进程）。纯文档/测试的提交不会重启面板。
- `deploy/install.sh` 是"从零装一台"的入口（rsync 同步、必要时重建 venv、装单元），日常更新别用它。

**旧部署升级时必须换掉单元文件**：2026-10-03 之前的 `deploy/*.service` 里写着
`EnvironmentFile=-/opt/douyin-spark-fusion/.env`，它会把消息模板里的字面 `\n` 吃成裸 `n`
（消息挤成一行）。`deploy/install.sh` 会覆盖 `/etc/systemd/system/` 下的单元，
升级后确认一下并重载：

```bash
grep -n '^EnvironmentFile' /etc/systemd/system/douyin-spark-fusion-*.service   # 应当*没有*输出
sudo systemctl daemon-reload
sudo systemctl restart douyin-spark-fusion-web.service
```

任务那一侧不用重启（oneshot，每次运行都是新进程）。

---

## 8. 排障

| 现象 | 原因 / 处理 |
|---|---|
| 服务起不来，退出码 2 | 绑了 `0.0.0.0` 但没配 `WEBUI_PASSWORD`/`WEBUI_PASSWORD_HASH` —— 这是故意的。设口令，或改 `WEBUI_HOST=127.0.0.1` |
| `BROWSER_PROBE_FAILED … Executable doesn't exist` | 浏览器没装好。设 `BROWSER_BACKEND=playwright` + `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/opt/google/chrome/chrome`，或 `PLAYWRIGHT_CHANNEL=chrome` |
| 浏览器启动报 sandbox / `Failed to move to new namespace` | 加了 `--no-sandbox` 没生效？确认以 root 运行，或显式 `CHROME_NO_SANDBOX=1` |
| 一轮跑完一个目标都没成功 | 先 `main.py dry-run select` 看是"没登录"还是"没找到会话"；面板的账号卡会显示门禁状态（`LOGGED_OUT` / `EXPIRED` / `LOGIN_LOST`） |
| 面板显示"冷却中" | 上一轮命中了风控关键词，冷却期内定时器会跳过。确认无风险后可删 `logs/rate_limit.json` 提前解除 |
| 面板里账号/cookie 还是旧的 | 配置在进程内缓存，`systemctl restart douyin-spark-fusion-web.service` 后才是新值 |
| 发出去的消息没有换行、行尾多出一个 `n` | 模板里的字面 `\n` 在配置层就被吃掉了。两条路都查：①`.env` 里必须写**两个字符** `\n`（不是真回车）②单元文件里**不许**有 `EnvironmentFile=`（systemd 会按转义序列解析值），改完 `systemctl daemon-reload`。确认办法：`main.py dry-run full` 会按行读回校验，日志里 `消息模板: '...\\n...'` 带引号才是对的（裸 `n` 就是坏了） |
| `dial tcp 198.18.x.x: i/o timeout` | 代理链路问题（fake-IP 走错网关），与本项目无关，去查旁路网关 |
| 日志页空白 | 任务还没跑过，`OPS_LOG_FILE` 指向的文件尚未生成 |
