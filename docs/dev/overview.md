# 仓库结构

> **2026-10-24 重写。** 这份文档以前写的是上游的云函数形态（`python main.py fc`、
> `aliyun-fc-ros-template.yaml`、`utils/export_github_env.py`）—— 那些东西融合版里早就没了。
> 现在只有一种部署形态：**一台常驻 Linux 机器 + systemd 每 5 分钟派发 + 常驻面板**。
> 下面的内容按线上 `/opt/douyin-spark-fusion`（v3.2.2，158 个受版本控制的文件、约 5.1 万行 Python）写。

## 一次派发是怎么跑起来的

```
douyin-spark-fusion-daily.timer      OnCalendar=*:0/5 —— 这是**门铃**，不是发送时刻
  └─ douyin-spark-fusion-daily.service     python main.py task
       └─ core/tasks.py::runTasks()
            ├─ core/limit_guard.py            上一轮命中风控？冷却中直接整轮跳过
            ├─ core/quiet_window.py           禁跑时间窗（默认 off）
            ├─ core/schedule.py::select()     今天谁到点了（今天发过 > 到点 > 等）
            ├─ core/run_ledger.py::RunLock    抢运行锁（另一个轮次/面板在跑就让开）
            ├─ 逐账号 core/tasks.py::do_user_task
            │    ├─ core/browser_state.py     这个账号的浏览器状态快照（同一台设备）
            │    ├─ core/cookie_utils.py      .env 里的 COOKIES_<抖音号>
            │    ├─ core/douyin_im.py         门禁 → 扫会话 → 选中 → 真输入 → 发送 → 回执
            │    ├─ core/strict_verify.py     选中/回执的三值校验（命中/不命中/未知，fail-closed）
            │    └─ 收尾：写回 browser_state、core/cookie_sync.py 回写 .env
            ├─ 记账：logs/schedule_state.json（今天发过没有）+ logs/runs.jsonl（这一轮的结果）
            └─ 放锁（**记账必须在放锁之前**）
```

面板是**另一个进程**（`python -m webui`，常驻）：只读 `logs/runs.jsonl`、`logs/schedule_state.json`、`.env`；
要真跑的时候在**进程内**调 `core.tasks.runTasks()`，靠同一把 `logs/run.lock` 与定时器互斥，
并且再按账号互斥一次（`webui/app.py` 的 `_dispatcher_accounts/_dispatcher_blocks/_panel_busy_reason`）——
两个功能不会同时开同一个账号的浏览器。

## 目录

```
main.py                 # 任务入口：task / dry-run / prune（fc 分支上游留的，不可用）
run_configtool.py       # 桌面配置生成器入口（tkinter）
core/                   # 业务核心（17 个模块，约 1.4 万行）
  douyin_im.py          #   抖音 IM：登录判定、扫会话、选中、真输入、发送、回执（3467 行）
  tasks.py              #   跑一轮：编排、逐账号、门禁判决、记账（1121 行）
  schedule.py           #   派发决策：到点/已发/等待 + 确定性抖动（875 行）
  qr_login.py           #   扫码登录状态机 + 二次验证 + 守夜线程（1516 行）
  strict_verify.py      #   "选中/收到"的三值校验纯函数（465 行）
  run_ledger.py         #   账本追加 + 运行锁 RunLock（435 行）
  browser.py            #   启动浏览器（cloakbrowser / playwright 两条后端）
  browser_state.py      #   每账号 storage_state 快照读写
  cookie_sync.py        #   服务端新签发的 cookie 回写 .env
  cookie_utils.py       #   cookie 过滤/规范化/.env 键名
  maintenance.py        #   磁盘与登录资料目录清理（白名单 + 在册账号保护）
  limit_guard.py        #   风控关键词熔断与冷却
  quiet_window.py       #   禁跑时间窗
  friend_scan.py        #   面板"读取好友列表"
  msg_builder.py        #   消息模板渲染（把字面 \n 还原成换行）
  util_time.py          #   时间口径（本地时间、跨零点）
webui/                  # 常驻面板：FastAPI + 手写 CSS/JS（零外网资源）
  app.py                #   路由（约 2900 行）
  viewmodel.py          #   页面数据整形、原因码/来源码的中文标签
  store.py              #   账本读取与运行状态
  envfile.py            #   .env 的原子保存 + 跨进程写锁（.env.lock）+ 口令哈希写入
  config.py             #   启动前校验（绑 0.0.0.0 必须配口令）
  auth.py / password.py #   会话 cookie 与 pbkdf2 口令
  logtail.py            #   日志页读文件
  reset_password.py     #   应急改口令 CLI（python -m webui.reset_password）
  hash_password.py      #   只要哈希串的 CLI（python -m webui.hash_password）
  __main__.py           #   入口：python -m webui [--host --port --check]
configTool/             # 桌面配置生成器（tkinter + 扫码登录 + gost 隧道，13 个模块）
utils/                  # config.py（读 .env）、logger.py、hitokoto.py、节日彩蛋
tools/                  # browser_probe.py（浏览器自检）、record_har.py（录 HAR）
deploy/                 # 3 个 systemd 单元 + install.sh（首次安装）+ update.sh（日常更新）+ logrotate.conf
tests/                  # 39 个 test_*.py，纯 unittest
docs/                   # 本套文档（docsify）；docs/dev/ 是维护者文档
docker/ + Dockerfile    # 容器形态（备用部署方式，线上没用）
.github/workflows/      # 只有 build-configtool.yml（手动触发，打 Windows 配置工具包）
VERSION                 # 3.2.2，面板页脚与 pip 包都用它
```

## 入口

| 入口 | 用途 |
| --- | --- |
| `python main.py task` | 跑一轮（timer 每 5 分钟调它；没账号到点就什么都不做，退出码 0） |
| `python main.py dry-run [select\|full]` | 空跑一轮：不点发送、不写时间表 |
| `python main.py prune [--apply --caches --older-than N]` | 盘/清登录资料目录 |
| `python -m webui [--host --port --check]` | 常驻面板（默认 `127.0.0.1:8787`，线上 `0.0.0.0:8787`） |
| `python run_configtool.py` | 桌面配置生成器 |
| `python -m webui.reset_password` | 面板进不去时应急改口令（写 `.env` + 重启服务） |

## 改代码时先知道这几条（都是踩过的坑）

1. **配置的唯一真相是 `.env`**，由程序自己用 python-dotenv 读；两个 unit 里**故意不写**
   `EnvironmentFile=`（systemd 会把消息模板里字面的 `\n` 吃成裸 `n`）。别加回去。
2. **记账发生在 `lock.release()` 之前**。挪到放锁之后会丢别人的改动、让 `skipped.attempts` 翻倍
   （历史上真出现过"重复发送"）。
3. **判定原因码新增要同步中文标签**：`webui/viewmodel.py` 的 `REASON_LABELS`；漏了
   `tests/test_viewmodel.py::ReasonLabelCoverageTests` 会直接从 `core` 源码里抓码而变红。
4. **timer 的 `OnCalendar=*:0/5` 不要改成"一天一次"**：它是门铃，改成一天一次就等于只有那一刻在判断，
   其余时间账号不会被考虑。发送时间在 `.env`（`TASK_SCHEDULE_TIMES` / 账号的 `time`）里改。
5. **状态码集中在 `core/douyin_im.py`**（`STATUS_READY/LOGGED_OUT/EXPIRED/LOGIN_LOST/TIMEOUT/ERROR`，约 1653 行）；
   哪些算"当天不再重试"看 `core/tasks.py` 的 `GATE_DEAD_STATUSES`。
6. **线上 `/opt/douyin-spark-fusion` 是 git 工作树**（2026-10-24 起）：改东西走 commit，
   手改不提交会让 `deploy/update.sh` 拒绝合并。日常更新见 `docs/deploy.md` §7。
7. `logs/login-profile/` 与 `logs/browser-state/` **等价于凭据**：不进 git、不进备份、不贴到聊天里。

## 想知道某件事在哪写

| 想知道 | 看 |
| --- | --- |
| 一轮怎么编排、什么算"今天发过了" | `core/tasks.py`、`core/schedule.py` |
| 抖音页面怎么点、状态码什么含义 | `core/douyin_im.py` |
| "确认选中/确认收到"的判定 | `core/strict_verify.py`（三值，fail-closed） |
| 面板有哪些路由、页面数据从哪来 | `webui/app.py`、`webui/viewmodel.py`、`webui/store.py` |
| `.env` 怎么写才不丢键 | `webui/envfile.py`（原子写 + flock） |
| 扫码登录的状态机 | `core/qr_login.py`（`QrLoginSession`） |
| 运行锁与账本 | `core/run_ledger.py` |
| 磁盘清理的安全线 | `core/maintenance.py` |
| 配置键名/范围/默认值 | `configTool/models.py` 是登记表，`.env.example` 是面向使用者的说明 |
| 为什么是现在这样 | `FUSION-DESIGN.md`（决策记录，按时间追加） |
