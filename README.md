# 抖音续火花控制台

自建服务器上的抖音「续火花」自动化：一个**每 5 分钟敲一次门的派发器**，加一个**网页控制台**。

每个账号可以有自己的消息文本、自己的发送时间、自己的目标好友；发出去之后要拿抖音自己给出的回执核对「这条到底发给了谁」，核对不上就不算发过。

- 版本 `3.2.2` · Linux + Python 3.12 · 依赖全量钉版本（`requirements.txt`）
- 浏览器：Playwright 驱动 cloakbrowser（可固定指纹）
- 部署形态：systemd（web 常驻 + timer 每 5 分钟敲门），**不使用 GitHub Actions**

## 特性

### 发送这条链路

- **账号独立配置**：`TASKS` 里每个账号有自己的 `message`（消息文本）和 `time`（发送时间）；没写时间的账号回落到 `TASK_SCHEDULE_TIMES` 的全局时间。
- **5 分钟敲门派发器**：定时器不判断「今天发没发」，只负责敲门；`core/schedule.py` 决定谁到点、谁今天已经发过、谁要再试。当天没发出去的账号，下一个门铃会再试；试到上限（`SCHEDULE_SKIP_RETRY_MAX`）就停下等第二天。
- **会话被踢不再烧名额**：发送接口回 `{"decision": "KICK"}` 时立刻停手 —— 不重试、不白等回执超时、不占当天名额；账号标「账号阻断」并提示重新扫码，重扫成功后当天自动补发。
- **掉登录不再静默丢一天**：运行期发现登录失效，这一轮记成 `error`（而不是「今天已完成」）；重新扫码后当天自动补发，不用等到明天。
- **强确认 / 弱证据**：只有「HTTP 回执 ok」**且**「发送后当前会话就是目标会话」才算强确认；拿不到收件人身份时降级成弱证据。账本里的 `ok` 表示「这条确实发出去了」，弱证据不会触发当天重发。
- **可信输入**：抖音聊天页的编辑器是 EditorKit（Slate 系）。输入走「全选 + CDP `Input.insertText` 覆盖」，发送前逐行比对读回内容；落地不精确就先重试可信输入，再退回覆盖式重打，两条都不成**拒绝发送**——宁可漏发一条，也不发半条或叠发。
- **dry-run 空跑**：`select` 只跑到「选中会话」，`full` 走完输入与校验但不点发送，适合第一次部署时验证全链路。
- **限流熔断**：命中「操作频繁 / 安全验证」一类文案即中止本轮并进入冷却（`LIMIT_COOLDOWN_MINUTES`），避免火上浇油。

### 运维

- **磁盘维护**：登录资料目录等价于凭据，浏览器缓存会持续增长。`main.py prune` 盘点，面板四个按钮手动清，定时器每周自动清一次（`MAINTENANCE_AUTO_PRUNE_HOURS`，`0` 关闭）。三条安全线：只在自己家目录里动手、在册账号永不删、名单读不到就一个都不删。
- **运行账本**：每轮结果写进 `logs/runs.jsonl`（谁、什么时候、发了几条、失败原因、谁触发的），面板「运行记录」读的就是它；「没有账号到点」这种常态不进账本。
- **幂等安装**：`deploy/install.sh` 可以重复执行用于升级，不会覆盖已有 `.env`、不会重建 venv，装完会重启面板。定时器**不会**被自动 enable——那是「真的会发消息」的动作。

### 界面

- **网页控制台**：账号卡（今天发没发、下一次什么时候、卡在哪一步）、运行明细、日志尾部、手动运行、空跑触发、配置编辑（白名单键）、磁盘维护、扫码登录与二次验证。
- **图形配置工具**：`configTool/`（tkinter）用来生成/修改 `.env`、抓取登录态，不写代码也能上手。

## 快速开始

### 一键安装（Ubuntu 22.04 / 24.04）

```bash
git clone https://github.com/liboxuanzs/douyin-spark-fusion.git
cd douyin-spark-fusion
sudo bash deploy/install.sh --dry-run   # 先看它准备做什么：不写 /etc、不重启服务
sudo bash deploy/install.sh             # 真装：venv → 依赖 → 浏览器 → .env → systemd 单元
```

装完：

```bash
sudo systemctl start douyin-spark-fusion-web.service          # 起面板
sudo systemctl enable --now douyin-spark-fusion-daily.timer   # 打开定时器（这一步之后才会真发消息）
```

面板默认在 `http://<机器>:8787`；健康检查 `GET /healthz` 不需要登录。第一次建议先空跑一轮，确认「选中会话 → 输入 → 读回校验」全绿再打开定时器。

### 手工跑一遍

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env       # 填 COOKIES_<抖音号> / TASKS / WEBUI_PASSWORD
.venv/bin/python main.py dry-run    # 空跑，不发送
.venv/bin/python -m webui           # 起面板
```

容器化部署见 `docker-compose.yml`（容器内由 cron 每 5 分钟敲门）。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `main.py` / `main.py task` | 跑一轮（cron / systemd timer 走这条） |
| `main.py task --force` | 不看时间表，所有账号现在就发（面板「立即运行」同义） |
| `main.py task --only <抖音号或昵称>` | 只处理这一个账号 |
| `main.py dry-run [select\|full]` | 空跑一轮 |
| `main.py prune [--apply] [--caches] [--older-than N] [--json]` | 盘点 / 清理磁盘（默认只预演，不动任何文件） |
| `python -m webui` | 起网页控制台 |

退出码：`0` 整轮跑完（「没有账号到点」也算跑完）、`1` 整轮被跳过或账号全部出错、`2` 参数错误。

## 配置

`.env` 由程序自己按绝对路径读取——**不要**交给 systemd 的 `EnvironmentFile=`，那会吃掉消息模板里的字面 `\n`。常用键：

| 键 | 说明 |
| --- | --- |
| `TASKS` | 账号与目标名单（JSON）：每个账号的 `unique_id`、`message`、`time`、目标列表 |
| `COOKIES_<抖音号>` | 该账号的登录 cookie（面板扫码后自动写入） |
| `TASK_SCHEDULE_TIMES` | 全局发送时间表，第一项是没单独配时间的账号的默认时间 |
| `SCHEDULE_JITTER_MINUTES` | 每个账号在基准时间上的随机抖动上限 |
| `DRY_RUN` | `off` / `select` / `full` |
| `QUIET_WINDOW` | 禁跑时段（窗口结束后的第一个门铃才补发） |
| `LIMIT_GUARD_ENABLED` / `LIMIT_COOLDOWN_MINUTES` | 限流熔断开关与冷却时长 |
| `WEBUI_HOST` / `WEBUI_PORT` | 监听地址与端口（代码默认 `127.0.0.1:8787`，`.env.example` 给的是局域网 `0.0.0.0`） |
| `WEBUI_PASSWORD` / `WEBUI_PASSWORD_HASH` | 面板访问口令（绑外部地址必须有口令） |
| `WEBUI_SESSION_SECRET` / `WEBUI_SESSION_MAX_AGE` | 会话签名密钥与登录保持时长（默认 30 天） |
| `LOGIN_PROFILE_DIR` | 登录资料目录根（等价于凭据，注意权限） |
| `BROWSER_STATE_DIR` | 每账号浏览器状态快照（等价于凭据；删掉不炸，下次以新设备重新登录） |
| `MAINTENANCE_AUTO_PRUNE_HOURS` | 定时器自动清理间隔，默认 168 小时，`0` 关闭 |
| `PROXY_ADDRESS` | 出口代理，留空即直连（多账号共用出口 IP 是已知的风控面） |

完整清单、默认值与注释见 [`.env.example`](.env.example)；面板「配置」页能改其中的白名单键。

## 目录结构

```
main.py              入口：task / dry-run / prune 三种模式
core/                业务核心
  schedule.py        5 分钟敲门派发器：谁到点、谁发过、谁要再试
  tasks.py           一轮的编排：逐账号、逐目标、空跑、写账本
  douyin_im.py       聊天页操作：选会话、可信输入、点发送、收回执
  strict_verify.py   回执核验：强确认 / 弱证据分档
  qr_login.py        扫码登录、登录态复检、资料目录接管
  friend_scan.py     好友列表扫描
  browser.py         浏览器启动 / 资料目录 / keepalive / 强杀
  cookie_utils.py    cookie 清洗与过滤
  maintenance.py     磁盘盘点与清理（手动 + 定时器自动）
  limit_guard.py     限流熔断与冷却
  run_ledger.py      运行账本与运行锁
webui/               网页控制台（FastAPI + 原生前端）
configTool/          图形配置工具（tkinter）
deploy/              systemd 单元、安装脚本、logrotate 规则
docker/ Dockerfile docker-compose.yml   容器化部署
docs/                文档
tests/               unittest 测试套件
tools/               调试工具（浏览器探针、抓 HAR）
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/deploy.md`](docs/deploy.md) | 自建机器部署、systemd、空跑验证、磁盘与资料目录 |
| [`docs/webui.md`](docs/webui.md) | 控制台每一页、每个按钮、每个状态的含义 |
| [`docs/faq/faq.md`](docs/faq/faq.md) | 常见问题（登录态、限流、消息、账号） |
| [`docs/dev/overview.md`](docs/dev/overview.md) | 代码结构与开发约定 |
| [`docs/guide/`](docs/guide/) | 图文上手：部署方式选择、cookie 与出口 IP |
| [`FUSION-DESIGN.md`](FUSION-DESIGN.md) | 本项目的设计取舍与决策记录 |

## 免责声明

- 本项目仅用于**技术学习与个人自用**，请勿用于商业用途、刷量或任何违反抖音平台规则的行为。
- 自动化操作存在被平台限流、验证甚至封禁的风险，**使用风险由使用者自行承担**。
- 请合理控制运行频率与目标数量，只用于维系个人少量好友的火花。
- 使用本项目即表示你已阅读并同意以上内容。

## 许可与来源

本项目以 MIT 协议开源，详见 [`LICENSE`](LICENSE)。

它是 MIT 项目 [2061360308/DouYinSparkFlow](https://github.com/2061360308/DouYinSparkFlow) 的二次开发版本，保留了上游的版权声明；在此基础上重做了调度与记账、收紧了发送与回执的判定、加入了可信输入与磁盘维护，并把部署形态改成自建机器上的 systemd。上游项目的文档、交流渠道与免责条款与本仓库无关。
