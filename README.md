<div align="center">

# 抖音续火花控制台

自建机器上的多账号「续火花」自动化：**每 5 分钟敲一次门的派发器** + **一个网页控制台**。

每个账号有自己的消息、自己的发送时间、自己的好友名单、自己的出口 IP；
发出去之后要拿抖音自己给的回执核对「这条到底发给了谁」——**核对不上就不算发过**。

[![version](https://img.shields.io/badge/version-3.2.2-7c83ff?style=flat-square)](VERSION)
[![license](https://img.shields.io/badge/license-MIT-2ea44f?style=flat-square)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)](requirements.txt)
[![platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%20%7C%2024.04-e95420?style=flat-square&logo=ubuntu&logoColor=white)](#快速开始)

[快速开始](#快速开始) · [部署手册](docs/deploy.md) · [面板手册](docs/webui.md) · [常见问题](docs/faq/faq.md) · [设计记录](FUSION-DESIGN.md)

</div>

---

## 这是什么

一台自己的机器（NAS、小主机、云服务器都行）上常驻两样东西：

- **派发器**：systemd timer 每 5 分钟敲一次门。它不判断「今天发没发」，只负责敲门；
  谁该发、谁今天已经发过、谁还要再试，由 `core/schedule.py` 决定，面板和派发器共享同一份时间表。
- **网页控制台**：账号卡、运行明细、日志、扫码登录、配置编辑、磁盘维护，一个 8787 端口全包。

为什么不做成云函数 / GitHub Actions：需要**真实浏览器**常驻（登录态要长期保活，扫码和短信二次验证要有人在场）、
需要**固定出口 IP 与时区**（云函数随机出口会把登录顶掉）、失败要**当天补发**而不是等明天的整点。
仓库里唯一的 workflow 只用来手动打包 Windows 配置工具，发送链路完全不上云。

## 它替你解决的四个老问题

| 常见做法 | 这里怎么处理 |
| --- | --- |
| 一天跑一次，失败就丢一天 | 每 5 分钟敲门：当天没发出去的账号继续重试（上限 `SCHEDULE_SKIP_RETRY_MAX`），重新扫码成功当天自动补发 |
| 「点了发送」就记成功 | 回执 + 当前会话双重核对；拿不到证据记 `error`，不记 `ok`（`core/strict_verify.py`） |
| 掉登录还天天显示正常 | 登录失效记 `error` 占当天名额并提示重扫；体检 `doctor` 逐个账号看「最近真的发出去没有」 |
| 触发风控还继续试 | 命中「操作频繁 / 安全验证」立刻停手进冷却，不重试、不叠加 |

## 特性

### 调度与记账

- **每账号独立配置**：`TASKS[]` 里每个账号有自己的 `message`、`time`、`enabled`、`targets`，还能单独配 `proxy`；
  没写 `time` 的回落 `TASK_SCHEDULE_TIMES` 的全局时间。
- **对称抖动**：`SCHEDULE_JITTER_MINUTES` 是在设定时间**前后各**随机偏移 N 分钟（按「账号 + 日期」哈希，同账号同一天恒定），
  不是只往后推；抖动把时刻推出当天边界时会夹回当天，绝不会因为跨零点而整天不发。
- **停用而不删除**：`"enabled": false` 只停自动发送，消息、时间、指纹、cookie、火花记录全都留着，面板上点「暂停 / 恢复」。
- **补发前先探登录态**：在面板上撤掉今天的账（准备补发）之前，先起一次轻量浏览器探登录（3 秒级）；
  登录已死就直接拒绝并引导去扫码，省掉一轮「撤了账、跑起来发现没登录」的白跑。
- **记账原子化**：一轮的结论在放锁**之前**落盘（`logs/schedule_state.json` + `logs/runs.jsonl`）；
  被 SIGTERM 打断也不会丢账，更不会因为丢账在下一个门铃重复发送。

### 发送与核验

- **强确认 / 弱证据**：只有「发送接口回执 ok」**且**「发送后当前会话就是目标会话」才算强确认；
  拿不到收件人身份就降级成弱证据，弱证据不会触发当天重发。
- **真实输入**：聊天编辑器是 EditorKit（Slate 系）。输入走「全选 + CDP `Input.insertText`」，发送前逐行读回比对；
  不精确先重试可信输入，再退回覆盖式重打，两条都不成**拒绝发送**——宁可漏发一条，也不发半条或叠发。
- **一键止损**：发送接口回 `{"decision": "KICK"}`（会话被别处顶掉）时立刻停手：不重试、不白等回执超时、不占当天名额，
  并提示重新扫码。
- **空跑**：`DRY_RUN=select` 只跑到「选中会话」，`full` 走完输入与读回校验但不点发送。
- **模板容错**：`[API]` 取不到一言时整行去掉而不是把 `[error] ...` 当正文发出去；去掉后没内容就不发。

### 像人一点的节奏（都能关）

| 键 | 作用 |
| --- | --- |
| `RUN_START_JITTER_SECONDS` | 门铃到点后再随机等 0~N 秒才开浏览器，打散整点整分 |
| `SEND_INTERVAL_MIN_SECONDS` / `SEND_INTERVAL_MAX_SECONDS` | 同一轮里两个目标之间的基础间隔 |
| `SEND_INTERVAL_LONG_PAUSE_SECONDS` | 约 15% 的概率在基础间隔上再多停 0~N 秒（长尾，不是均匀分布） |
| `TYPING_CHUNK_CHARS` / `TYPING_CHUNK_DELAY_MS` | 分段输入：一次键几个字、块间停多久（默认 6 字 / 110ms） |
| `WARMUP_BROWSE_SECONDS` | 发送前先在会话列表里滚一会儿（默认 0，关闭） |
| `TASKS[].proxy` | 单账号出口；留空回落全局 `PROXY_ADDRESS`，全局也空就是直连 |

### 运维与面板

- **网页控制台**（FastAPI + 原生前端，零外链）：账号卡（今天发没发、下次几点、卡在哪一步、火花连续天数、消息预览、出口）、
  运行明细、日志尾部、立即运行 / 空跑、30 个白名单键的配置表单、磁盘维护、扫码登录 + 短信验证码二次验证、通知测试。
- **运行账本**：每轮写 `logs/runs.jsonl`（谁、什么时候、发了几条、失败原因、谁触发）；「没有账号到点」这种常态不进账本，免得刷屏。
- **静默体检**：`main.py doctor` 看六件事——账本还新吗、定时器还在不在、面板服务还活着吗、账号是不是全停用了、
  每个账号最近有没有真发出去、磁盘。有事退出码 1，并按 `NOTIFY_*` 推一条（每天最多一条）。
  `doctor.timer` 每天 09:00 跑一次，只读、不发消息。
- **结果通知**：Server酱 / 企业微信 / 钉钉 / 飞书 / Telegram 自适应，其它地址按通用 JSON 发；
  **HTTP 200 不算成功**（这几家都是 200 + 错误码），看的是回应体。`main.py notify-test` 可以立刻验证地址通不通。
- **磁盘维护**：登录资料目录等价于凭据，浏览器缓存会持续增长。`main.py prune` 盘点，面板上手动清，
  定时器每周自动清一次（`MAINTENANCE_AUTO_PRUNE_HOURS`）。三条安全线：只在自己家目录里动手、在册账号永不删、名单读不到就一个都不删。
- **扫码登录**：面板出码 → 手机扫 → 首次登录可能要短信验证码（面板会切到二次验证界面）→ 成功后落 cookie 与浏览器快照。
  取消随时可退，按钮不会卡在灰态，终态不留二维码。
- **图形配置工具**：`configTool/`（tkinter）生成 / 修改 `.env`、抓登录态，不写代码也能上手。

## 快速开始

### 一键安装（Ubuntu 22.04 / 24.04，root）

```bash
git clone https://github.com/liboxuanzs/douyin-spark-fusion.git
cd douyin-spark-fusion
sudo bash deploy/install.sh --dry-run   # 先看它准备做什么：只同步代码/venv/.env，不写 /etc、不重启
sudo bash deploy/install.sh             # 真装：venv → 依赖 → 浏览器 → .env → systemd 单元
```

默认装到 `/opt/douyin-spark-fusion`（`APP_DIR=` 可改）。可以重复执行用于升级：不会覆盖已有 `.env`、不会重建 venv。

装完这几步（脚本**不会**替你开定时发送）：

```bash
sudo nano /opt/douyin-spark-fusion/.env          # 填 WEBUI_PASSWORD；账号也可以起面板后扫码加
cd /opt/douyin-spark-fusion && .venv/bin/python main.py dry-run full   # 空跑：不发送任何消息
sudo systemctl enable --now douyin-spark-fusion-web.service           # 面板：http://<机器IP>:8787
sudo systemctl enable --now douyin-spark-fusion-daily.timer           # 确认无误后再开——这一步之后才会真发
```

`GET /healthz` 不需要登录，适合给监控探活。第一次务必先空跑，全绿再开定时器。

### 手工装 / 容器

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python main.py dry-run
.venv/bin/python -m webui
```

容器化见 [`docker-compose.yml`](docker-compose.yml)（容器内由 cron 每 5 分钟敲门）。

## 日常运维

### 升级

线上目录就是 git 工作树，日常升级不用重跑安装脚本：

```bash
sudo bash deploy/update.sh --check   # 只看有没有新提交
sudo bash deploy/update.sh           # ff 更新 → 按改动决定要不要装单元 / 重启面板 → 轮询 /healthz
```

它只允许 fast-forward（历史分叉就停下让人看，绝不 reset），有轮次正在跑就拒绝（`--force` 例外），
`.env` 与 `logs/` 一个字节都不碰。

### 回滚

```bash
git -C /opt/douyin-spark-fusion log --oneline -5     # 挑一个能用的提交
sudo git -C /opt/douyin-spark-fusion reset --hard <sha>
sudo systemctl restart douyin-spark-fusion-web.service
```

### 忘了面板口令

```bash
cd /opt/douyin-spark-fusion && .venv/bin/python -m webui.reset_password
```

### 该备份什么

| 路径 | 内容 |
| --- | --- |
| `.env` | 账号名单、消息模板、cookie、面板口令哈希——**等价于凭据** |
| `logs/runs.jsonl` | 运行账本（面板「运行记录」与火花连续天数都读它） |
| `logs/schedule_state.json` | 今天谁发过、谁要再试 |
| `logs/login-profile/`、`logs/browser-state/` | 登录资料与浏览器快照——同样**等价于凭据**，别外传 |

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `main.py` / `main.py task` | 跑一轮（systemd timer / cron 走这条） |
| `main.py task --force` | 不看时间表：今天还没发成功的账号立刻发（发成功过的不重复发） |
| `main.py task --force-all` | 连「今天已经发成功过」也不管，全部再发一遍 |
| `main.py task --only <抖音号或昵称>` | 只处理这一个账号（是否看时间表仍由 `--force` 决定） |
| `main.py dry-run [select\|full]` | 空跑一轮 |
| `main.py prune [--apply] [--caches] [--older-than N] [--json]` | 盘点 / 清理磁盘（默认只预演，不动任何文件） |
| `main.py notify-test` | 按当前配置推一条测试通知 |
| `main.py doctor [--json] [--no-notify]` | 体检（见上文） |
| `python -m webui` | 起网页控制台；加 `--check` 只校验配置 |
| `python -m webui.reset_password` | 重设面板口令（写完提示重启面板） |

退出码：`0` 整轮跑完（「没有账号到点」也算跑完）、`1` 整轮被跳过或账号全部出错、`2` 参数错误。

## 配置

`.env` 由程序按绝对路径自己读——**不要**交给 systemd 的 `EnvironmentFile=`，那会吃掉消息模板里的字面 `\n`。最常改的几个：

| 键 | 说明 |
| --- | --- |
| `TASKS` | 账号与目标名单（JSON）：`unique_id`、`message`、`time`、`enabled`、`proxy`、`targets` |
| `COOKIES_<抖音号>` | 该账号的登录 cookie（面板扫码后自动写入） |
| `TASK_SCHEDULE_TIMES` | 全局时间表；第一项是没单独配时间的账号的默认时间 |
| `SCHEDULE_JITTER_MINUTES` | 在设定时间前后各随机偏移多少分钟（对称） |
| `DRY_RUN` | `off` / `select` / `full` |
| `QUIET_WINDOW` | 禁跑时段（窗口结束后的第一个门铃才补发） |
| `LIMIT_GUARD_ENABLED` / `LIMIT_COOLDOWN_MINUTES` | 限流熔断开关与冷却时长 |
| `PROXY_ADDRESS` | 全局出口，留空即直连（多账号共用一个出口 IP 是已知的风控面） |
| `WEBUI_HOST` / `WEBUI_PORT` | 监听地址与端口（代码默认 `127.0.0.1:8787`，安装脚本写 `0.0.0.0:8787`） |
| `WEBUI_PASSWORD` / `WEBUI_PASSWORD_HASH` | 面板口令（绑外部地址必须有口令） |
| `WEBUI_SESSION_SECRET` / `WEBUI_SESSION_MAX_AGE` | 会话签名密钥与登录保持时长（默认 30 天） |
| `NOTIFY_WEBHOOK_URL` / `NOTIFY_LEVEL` | 通知地址与时机（`off` / `error` / `always`；空跑永不推） |
| `LOGIN_PROFILE_DIR` / `BROWSER_STATE_DIR` | 登录资料与浏览器快照根目录（等价于凭据） |
| `MAINTENANCE_AUTO_PRUNE_HOURS` | 自动清理间隔（默认 168 小时，`0` 关闭） |

完整清单、默认值与逐条注释见 [`.env.example`](.env.example)；面板「运行配置」能改其中 30 个白名单键：发送与通知相关的下一轮就用新值，监听地址、端口一类的需要重启面板。

## 面板里有什么

| 页面 | 你能做的事 |
| --- | --- |
| 首页 · 账号卡 | 今天发没发、下次几点、卡在哪一步、火花连续天数、消息预览、出口、暂停 / 恢复、补发、移除 |
| 扫码登录 | 出码、手机确认、短信验证码、取消；成功后自动接管资料目录并写入 cookie |
| 运行记录 / 运行日志 | 每一轮的结构化结论、每个目标的证据等级与失败原因；日志尾部实时刷新 |
| 立即运行 / 空跑 | 手动触发一轮，可选空跑档位；与派发器共用运行锁，不会并发抢同一个账号 |
| 运行配置 | 30 个白名单键的表单，带说明与校验 |
| 磁盘维护 | 缓存与孤儿资料目录的盘点、清理，附「只预演」 |

## 目录结构

```
main.py              入口：task / dry-run / prune / notify-test / doctor
core/                业务核心
  schedule.py        敲门派发器：谁到点、谁发过、谁要再试
  tasks.py           一轮的编排：逐账号、逐目标、空跑、记账
  douyin_im.py       聊天页操作：选会话、可信输入、点发送、抓回执
  strict_verify.py   回执核验：强确认 / 弱证据 / 三值判定
  qr_login.py        扫码登录、短信二次验证、资料目录接管
  login_probe.py     轻量登录态探测（补发前先问一句）
  browser.py         浏览器启动 / 资料目录 / keepalive
  maintenance.py     磁盘盘点与清理
  limit_guard.py     限流熔断、冷却、间隔采样
  run_ledger.py      运行账本与运行锁
  notify.py          结果通知（按 webhook 主机名挑形状）
  doctor.py          静默体检
webui/               网页控制台（FastAPI + 原生前端）
configTool/          图形配置工具（tkinter）
deploy/              systemd 单元、install.sh、update.sh、logrotate
docker/ Dockerfile docker-compose.yml   容器化部署
docs/                文档（部署、面板、FAQ、开发）
tests/               unittest（3000+ 条）
tools/               调试工具（浏览器探针、抓 HAR）
```

## 「发出去了吗」是怎么判的

自动化最容易骗自己的地方就是「点了按钮 = 发出去了」。这里的判定链是六步：

1. **门禁**：先判登录态（`READY` / `LOGGED_OUT` / `EXPIRED` / `LOGIN_LOST`），不是 `READY` 就不往下走。
2. **选人**：按昵称在会话列表里找人，选中后核对「当前会话就是目标」（`verify_selection`）。
3. **输入**：把消息分块打进编辑器，再逐行读回比对；读回不一致就重打，仍不一致就不发。
4. **发送**：点发送并抓抖音自己的接口回执，拿到 `message_id` 才算有物证；`decision=KICK` 立刻止损。
5. **核验**：`strict_verify` 把证据分档——强确认（回执 ok **且** 会话是目标）/ 弱证据（回执 ok 但拿不到收件人身份）/ 冲突。
   任何一项明确为假就判失败（fail-closed）。
6. **记账**：`ok` 才算「今天发过了」；`error` 占当天名额并写明原因；`skipped` 不占名额，下个门铃继续试。

所以面板上「今天已发」的数字就是**真发出去**的数量——掉登录、会话被踢、一条都没发出去，全都记 `error` 而不是 ok。

## 已知边界

- **一条账号消息发给它的全部目标**，每个目标只有 `[API]`（一言）那部分不同。要做「每个好友不同的一句话」得改数据模型。
- **不做多机 / 多进程并发**：同一个账号同时只允许一个浏览器（运行锁 + 面板互斥），并发只会更容易被风控。
- **不绕验证码、不绕风控**：命中就停、冷却、通知你，而不是换 IP 继续打。
- **不支持云函数模式**：`main.py fc` 是上游残留，本仓库没有对应实现。
- **依赖抖音网页结构**：改版会让选择器失效——那时账本会记 `error`（而不是假成功），日志里有定位线索。

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/deploy.md`](docs/deploy.md) | 部署、systemd、空跑验证、体检与通知、磁盘与资料目录 |
| [`docs/webui.md`](docs/webui.md) | 控制台每一页、每个按钮、每个状态的含义 |
| [`docs/faq/faq.md`](docs/faq/faq.md) | 常见问题（登录态、限流、消息、账号） |
| [`docs/dev/overview.md`](docs/dev/overview.md) | 代码结构与开发约定 |
| [`docs/dev/tools.md`](docs/dev/tools.md) | 测试与调试工具 |
| [`docs/guide/`](docs/guide/) | 图文上手：部署方式选择、cookie 与出口 IP |
| [`FUSION-DESIGN.md`](FUSION-DESIGN.md) | 设计取舍与逐条决策记录（含每次线上事故的复盘） |

`docs/` 本身是一套 docsify 站点（入口 `docs/index.html`），想在线看就在仓库设置里用 GitHub Pages 指向 `docs/`。

## 免责声明

- 本项目仅用于**技术学习与个人自用**，请勿用于商业用途、刷量或任何违反抖音平台规则的行为。
- 自动化操作存在被平台限流、验证甚至封禁的风险，**使用风险由使用者自行承担**。
- 请合理控制运行频率与目标数量，只用于维系个人少量好友的火花。
- 使用本项目即表示你已阅读并同意以上内容。

## 许可与来源

本项目以 MIT 协议开源，详见 [`LICENSE`](LICENSE)。

它是 MIT 项目 [2061360308/DouYinSparkFlow](https://github.com/2061360308/DouYinSparkFlow) 的二次开发版本，保留了上游的版权声明。
在此基础上：重做了调度与记账、收紧了发送与回执的判定、加入可信输入 / 节奏控制 / 静默体检 / 磁盘维护，
并把部署形态改成自建机器上的 systemd。上游项目的文档、交流渠道与免责条款与本仓库无关。
