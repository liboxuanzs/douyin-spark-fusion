# 抖音续火花控制台 · 文档

自建服务器上的抖音「续火花」自动化：每 5 分钟敲一次门的派发器 + 一个网页控制台。

每个账号可以有自己的消息文本、发送时间和目标好友；发出去之后要拿抖音自己给出的回执核对收件人，核对不上就不算发过。

## 从这里开始

1. [项目介绍](intro/01-项目介绍.md) —— 它做什么、由哪几部分组成、和上游项目的关系
2. [源码部署](deploy.md) —— systemd（web 常驻 + timer 每 5 分钟），含空跑验证与磁盘维护
3. [网页控制台](webui.md) —— 每一页、每个按钮、每个状态的含义
4. [常见问题](faq/faq.md) —— 登录态、限流、消息、账号

## 其它

- [选择部署方式](guide/01-选择部署方式.md) · [Cookie 与出口 IP](guide/02-cookie与出口IP.md)
- [仓库结构](dev/overview.md) · [工具与测试](dev/tools.md) · [本地代理调试](dev/local-proxy-debug.md)
- [Docker 部署](deploy/docker.md) · [GitHub Action 部署（上游形态，本仓库不使用）](deploy/action.md)

## 链接

- [GitHub 仓库](https://github.com/liboxuanzs/douyin-spark-fusion)
- [设计取舍（FUSION-DESIGN.md）](https://github.com/liboxuanzs/douyin-spark-fusion/blob/master/FUSION-DESIGN.md)
- [.env.example（全部配置键与默认值）](https://github.com/liboxuanzs/douyin-spark-fusion/blob/master/.env.example)
