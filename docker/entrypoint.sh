#!/bin/bash
set -euo pipefail

# 按启动模式分派：
#   cron（默认）→ 服务器 Docker：容器内 cron 到点跑 runTasks
#   fc          → 上游的阿里云函数计算形态，**本融合版仓库不可用**：
#                 它要的 docker/entrypoint-fc.sh 与 fc_server.py 都不在仓库里，
#                 所以这一支只负责明确报错。
LAUNCH_MODE="${LAUNCH_MODE:-cron}"
LAUNCH_MODE="${LAUNCH_MODE,,}"  # 控制台里手打容易填成 FC / Cron，统一转小写

case "${LAUNCH_MODE}" in
  fc|fc_server|serve)
    # 明确失败，而不是 exec 一个不存在的文件：那只会留下一条
    # "No such file or directory"，让人以为是路径写错了。
    # 也**不能**静默退回 cron —— 在函数计算里跑 cron 形态不会有任何东西被触发，
    # 表现出来就是「部署成功了，但一条消息都不发」。
    echo "LAUNCH_MODE=${LAUNCH_MODE}：云函数（FC）形态在本融合版里不可用" >&2
    echo "（缺 docker/entrypoint-fc.sh 与 fc_server.py）请改用 LAUNCH_MODE=cron。" >&2
    exit 1
    ;;
  cron|*)
    exec /app/docker/entrypoint-cron.sh "$@"
    ;;
esac
