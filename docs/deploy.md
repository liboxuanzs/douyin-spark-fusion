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
cd /opt/douyin-spark-fusion && bash netinv/reset_webui_password.sh        # 随机生成并打印
cd /opt/douyin-spark-fusion && bash netinv/reset_webui_password.sh '我的新口令'
```

其它可选键（默认值见 `.env.example`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `DRY_RUN` | `off` | 空跑档位：`off` / `select` / `full`。**认不出来的值一律回落 `off`（真发）**，不会悄悄变空跑 |
| `LIMIT_GUARD_ENABLED` | `true` | 风控熔断开关 |
| `LIMIT_COOLDOWN_MINUTES` | `30` | 命中后冷却多少分钟不再发 |
| `QUIET_WINDOW` | `off` | 禁跑时间窗（`HH:MM-HH:MM`，跨零点也认）。这段**绝对不发**，用来避免两个程序抢同一个账号；`off`（默认）关闭。盖住定时器时间会让那一轮整轮跳过，面板会报红 |
| `QUIET_WINDOW_OVERRIDE` | 空 | 设为 `1` 时**临时无视** `QUIET_WINDOW` 放行一轮（默认窗口内面板"立即运行"会返回 409） |
| `SEND_INTERVAL_MIN_SECONDS` / `MAX` | `8` / `20` | 每两个目标之间的额外间隔（0/0 = 不额外等待） |
| `RUN_LEDGER_FILE` | `logs/runs.jsonl` | 运行账本（面板读它画图表） |
| `RUN_LOCK_FILE` | `logs/run.lock` | 运行锁（防止两个任务同时开浏览器） |
| `RATE_LIMIT_FILE` | `logs/rate_limit.json` | 冷却状态 |
| `OPS_LOG_FILE` | `logs/app.log` | 面板"日志"页读的文件（默认读 Python 日志 `logs/app.log`）。`deploy/` 里的两个 systemd 单元把标准输出追加到 `logs/douyin-spark-fusion.log`，`.env.example` 给的也是后者 —— 想让面板同时看到单元输出，就显式把它设成 `logs/douyin-spark-fusion.log` |
| `BROWSER_BACKEND` | `auto` | `auto` / `cloakbrowser` / `playwright` |
| `TASK_SCHEDULE_TIMES` | `20:30` | **每个账号的发送时间**：当天的门铃第一次到点之后才发。面板上单独给某个账号设了时间就以账号的为准，这里是没单独设的账号的默认值 |
| `SCHEDULE_JITTER_MINUTES` | `15` | 发送时间上的随机抖动（分钟）。按「账号 + 日期」**确定性**取值 —— 同一天同一账号每次算出来一样，不会每 5 分钟漂一次；`0` = 准点发 |
| `SCHEDULE_STATE_FILE` | `logs/schedule_state.json` | 发送时间表：哪个账号哪天发过了。删掉它等于「今天全都没发过」，当天还会补发 |
| `WEBUI_DEFAULT_DRY_RUN` | `off` | 面板"立即运行"默认选中的档位（`off`/`select`/`full`）；只是默认值，点按钮前仍能改 |
| `LOGIN_PROFILE_DIR` | `logs/login-profile` | 面板扫码登录用的浏览器资料目录；**等价于凭据**，别进备份/别共享 |
| `BROWSER_STATE_DIR` | `logs/browser-state` | 每个账号的浏览器状态快照（`<抖音号>.json`，发送时挂上去，让每轮都还是同一台设备）；**等价于凭据**。删掉不炸，只是下一个账号首次运行会以新设备身份重新登录一次 |
| `QR_LOGIN_TTL_SECONDS` | `300` | 面板扫码登录的二维码有效期（秒） |
| `FRIEND_SCAN_TIMEOUT_SECONDS` | `300` | 面板"读取好友列表"单次扫描的超时（秒）；超时标成读取失败，已读到的部分仍可用 |

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

---

## 4. 启动面板

```bash
systemctl enable --now douyin-spark-fusion-web.service
systemctl status douyin-spark-fusion-web.service
```

- 面板：`http://<本机IP>:8787/`（`本机` → http://192.168.1.100:8787/ ）
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

---

## 7. 升级

```bash
cd <新代码目录> && sudo bash deploy/install.sh     # 会保留 .env 与 logs/
sudo systemctl restart douyin-spark-fusion-web.service
```

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
