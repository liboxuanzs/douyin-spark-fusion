# 工具与测试

## 测试

项目用 Python 标准库 `unittest`，不需要额外依赖。但要用**项目自己的 venv**跑：部分用例会 import
浏览器适配层（`core/browser.py` 会尝试 import `cloakbrowser`/`playwright`），系统 Python 里没有这些包。

```bash
cd /opt/douyin-spark-fusion
.venv/bin/python -m unittest discover -s tests -t .        # 全部（2026-10-24 基线：2499 条，约 140 秒）
.venv/bin/python -m unittest tests.test_schedule -v        # 单个文件
.venv/bin/python -m unittest tests.test_tasks_fusion.TaskRunTests.test_something
```

36 个 `tests/test_*.py`，按关注点分：

| 关注点 | 文件 |
| --- | --- |
| 登录判定、扫会话、选中、匹配 | `test_douyin_im.py`、`test_douyin_im_har.py`（基于 HAR 的离线回归） |
| "确认选中/确认收到"的纯函数语义 | `test_strict_verify.py` |
| 派发时间表、确定性抖动 | `test_schedule.py` |
| 一轮的编排、门禁判决、记账、SIGTERM | `test_tasks_fusion.py`、`test_tasks_state.py`、`test_main_cli.py` |
| 运行锁与账本 | `test_run_ledger.py` |
| 扫码登录状态机（离线部分） | `test_qr_login.py` |
| `.env` 原子写、跨进程锁、口令 | `test_envfile.py`、`test_config_env.py`、`test_auth.py` |
| 面板路由、前端契约、静态资源不许外链 | `test_webui.py`、`test_webui_session.py`、`test_webui_static.py`、`test_viewmodel.py` |
| systemd 单元、部署脚本、logrotate、容器入口 | `test_deploy_units.py`、`test_entrypoint_cron.py` |
| cookie、浏览器状态、熔断、静默窗、磁盘维护、消息模板 | `test_cookie_injection.py`、`test_cookie_sync.py`、`test_cookie_utils.py`、`test_browser.py`、`test_browser_state.py`、`test_limit_guard.py`、`test_quiet_window.py`、`test_maintenance.py`、`test_msg_builder.py` |
| 好友列表扫描 | `test_friend_scan.py` |
| 桌面配置工具 | `test_config_tool.py`、`test_config_tool_env.py`、`test_config_login_flow.py`、`test_config_tzset.py`、`test_config_wheel.py`、`test_env_store.py` |
| 杂项 | `test_logger.py`、`test_record_har.py` |

`tests/_purity.py` 是"这个模块 import 期不许拖进重依赖"的检查（面板进程 import 扫码模块必须是安全的）。

- GUI 相关用例在**没有显示环境**时跳过（`skipped`，不算失败）。
- 少数用例会真的走到"启动浏览器"这条路径（`test_qr_login.py` 里"启动失败要给人话"那条）。它
  **不依赖**这台机器装没装 Chromium：把 `BROWSER_BACKEND` 指到一个不存在的后端来构造失败，
  所以有浏览器 / 没浏览器的机器上都稳定通过（2026-10-24 修的正是以前"装了浏览器就必红"的那条）。
- 用例都写临时目录/临时文件，不发消息、不碰线上 `.env` 与真实账本。

## 部署脚本

| 脚本 | 用途 |
| --- | --- |
| `deploy/install.sh` | **首次安装**：同步代码到 `/opt`、建 venv、准备浏览器、装 systemd 单元、装 logrotate |
| `deploy/update.sh` | **日常更新**：git fast-forward + 按改动决定 pip / 单元 / 重启面板（见 `docs/deploy.md` §7） |

## 小工具（都在 `webui/`，`python -m ...` 直接跑）

| 命令 | 用途 |
| --- | --- |
| `python -m webui` | 启动面板（`--host`/`--port`/`--check`） |
| `python -m webui.reset_password` | 面板进不去时应急改口令：写 `.env`、重启服务、等 `/healthz` |
| `python -m webui.hash_password` | 只把口令算成 `WEBUI_PASSWORD_HASH`（自己贴进 `.env`） |

## HAR 录制工具

`tools/record_har.py` 用来打开抖音聊天页录一段流量，供离线分析（比如排查接口结构变化）。

```bash
python tools/record_har.py                 # 第一个账号，录 300 秒
python tools/record_har.py --seconds 60    # 录 60 秒
python tools/record_har.py --account 12345678901
python tools/record_har.py --out har_logs/my.har
```

产出在 `har_logs/` 下，文件名 `<用户名>_<时间戳>.har`。

> ⚠️ HAR 里含登录凭据（Cookie）和好友资料，**绝不提交**。`har_logs/` 已在 `.gitignore`。

## 浏览器自检

`tools/browser_probe.py` 用当前配置开一次上下文、读 UA/时区/语言，成功打印 `BROWSER_PROBE_OK`。
部署后、换代理后、改了 `BROWSER_TIMEZONE` 之后都跑一次：

```bash
cd /opt/douyin-spark-fusion && .venv/bin/python tools/browser_probe.py
```
