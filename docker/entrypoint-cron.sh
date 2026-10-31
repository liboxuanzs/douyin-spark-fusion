#!/bin/bash
set -euo pipefail

CONFIG_ENV_PATH="/app/.env"

if [[ ! -f "$CONFIG_ENV_PATH" ]]; then
  echo "Config file not found: $CONFIG_ENV_PATH" >&2
  exit 1
fi

#: 每几分钟敲一次门，单位分钟。
#:
#: ★ 为什么不是"一天敲一次"：发送时刻由应用自己按账号算
#:   （`core/schedule.py`：`TASKS[].time` + 一个 [0, SCHEDULE_JITTER_MINUTES]
#:   内的**非负**抖动），门铃只是"来问一句到点了没"。以前这里写的是
#:   `CRON_MINUTE CRON_HOUR * * *`（一天一次），于是抖动窗口（默认 15 分钟）里
#:   只有恰好落在那一分钟的账号（约 1/16）当天才发得出去，其余全部被
#:   "还没到点"挡回去，且第二天不会补 —— 表现出来就是"大部分账号一条都没发"。
#:
#: ★ 为什么是 5 而不是"按抖动算"：这个数与 `core/schedule.py::TICK_MINUTES = 5`
#:   （systemd 那套 `deploy/*.timer` 用的 `OnCalendar=*:0/5`）对齐。
#:   `schedule.tick_minutes(jitter)` 算出来的建议值随 jitter 变（0 分钟抖动 → 1），
#:   而容器里只有一个 cron 表达式、每账号抖动又各不相同，按哪个账号算都是错的；
#:   跟 systemd 侧保持一致才是可解释的。要覆盖就设 CRON_TICK_MINUTES 或
#:   CRON_SCHEDULE（容器环境变量与挂载的 .env 都认，环境变量优先，见下）。
CRON_TICK_MINUTES="${CRON_TICK_MINUTES:-}"

# 合并 .env 与容器环境变量 → /etc/douyin-spark-flow.env，供 run-task.sh source。
# 这份文件里有 COOKIES_<抖音号>（一整份登录态）。`open(..., 'w')` 跟着 umask 走，
# 默认 022 就是 0644 —— 容器里任何用户/进程都能读到。先按 0600 建出来再写。
python - <<'PY'
import os
import shlex
from dotenv import dotenv_values

config_env_path = "/app/.env"
file_vars = {k: v for k, v in dotenv_values(config_env_path).items() if v is not None}
merged_vars = dict(os.environ)
merged_vars.update(file_vars)

fd = os.open('/etc/douyin-spark-flow.env', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as f:
    for key, value in merged_vars.items():
        f.write(f'export {key}={shlex.quote(value)}\n')
os.chmod('/etc/douyin-spark-flow.env', 0o600)   # 老文件已存在时 O_CREAT 不改权限

with open('/tmp/douyin-spark-flow.tz', 'w', encoding='utf-8') as f:
    f.write(file_vars.get('TZ', os.environ.get('TZ', 'UTC')))
PY

# 这里以前还会写一个 /tmp/douyin-spark-flow.cron（内容 = CRON_SCHEDULE），
# 全仓（含 Dockerfile / run-task.sh / docs）**没有任何消费者** —— 真正的 cron
# 装载在下面那段 heredoc 里。删掉，免得让人以为改那个文件能影响调度。

# CRON_HOUR / CRON_MINUTE / CRON_SECOND 的默认值。
# ★ 刻意**不**把 configTool 写的 9/0（.env.example 的示例值）当"用户显式设置"：
#   那两个键现在由 `configTool/models.py` 按运行时间恒定写进 .env，值一直是
#   9 和 0，一旦把它们当成显式设置，下面这段兼容分支就永远生效 = 抖动缺陷原样保留。
#   所以只有"真的不是示例默认值"才走老行为。
CRON_HOUR="9"
CRON_MINUTE="0"
CRON_SECOND="0"
LEGACY_ONCE_A_DAY=""
LEGACY_RAW="$(python - <<'PY'
from dotenv import dotenv_values

values = dotenv_values('/app/.env')


def plain(key, default):
    """把 .env 里的值拍成一行纯文本：None / 空串都按旧行为回落。"""
    raw = values.get(key)
    if raw is None:
        return default
    text = str(raw).strip()
    return text or default


hour = plain('CRON_HOUR', '9')
minute = plain('CRON_MINUTE', '0')
# 9/0 是 .env.example 与 configTool 的示例默认值 → 不算"显式设置"
if (hour, minute) not in {('9', '0'), ('09', '00'), ('09', '0'), ('9', '00')}:
    print(f'{hour} {minute}')
PY
)"
if [[ -n "$LEGACY_RAW" ]]; then
  read -r CRON_HOUR CRON_MINUTE <<<"$LEGACY_RAW"
  LEGACY_ONCE_A_DAY=1
fi

CRON_SECOND="$(python - <<'PY'
from dotenv import dotenv_values

raw = dotenv_values('/app/.env').get('CRON_SECOND', '0')
print(str(raw).strip() if raw is not None and str(raw).strip() else '0')
PY
)"

TZ="$(cat /tmp/douyin-spark-flow.tz)"
export TZ

# CRON_TICK_MINUTES 与 CRON_HOUR/MINUTE/SECOND 一样，允许写在挂载进来的 .env 里
# （容器环境变量优先）。只认容器环境的话，"我在 .env 里设了它却没反应"是最难查的
# 一类问题 —— 这个 .env 本来就是这几个 CRON_* 键的家。
if [[ -z "$CRON_TICK_MINUTES" ]]; then
  CRON_TICK_MINUTES="$(python - <<'PY'
from dotenv import dotenv_values

raw = dotenv_values('/app/.env').get('CRON_TICK_MINUTES', '')
print(str(raw).strip() if raw is not None else '')
PY
)"
fi
CRON_TICK_MINUTES="${CRON_TICK_MINUTES:-5}"

if [[ -n "${CRON_SCHEDULE:-}" ]]; then
  # 显式给了完整 cron 表达式 → 一字不改照用（老用户的自定义写法）
  echo "[docker] 警告：CRON_SCHEDULE 为显式设置，按原样使用；若它不是每 5 分钟一次，" >&2
  echo "[docker]       各账号的发送抖动（SCHEDULE_JITTER_MINUTES）将无法生效。" >&2
elif [[ -n "$LEGACY_ONCE_A_DAY" ]]; then
  # 兼容老行为：CRON_HOUR/CRON_MINUTE 被显式改成非示例值时，仍按"一天敲一次"跑
  CRON_SCHEDULE="${CRON_MINUTE} ${CRON_HOUR} * * *"
  echo "[docker] 警告：检测到显式设置的 CRON_HOUR=${CRON_HOUR} / CRON_MINUTE=${CRON_MINUTE}，" >&2
  echo "[docker]       按老行为一天只敲一次门：各账号的发送抖动将被跳过，" >&2
  echo "[docker]       一天里只有恰好落在 ${CRON_HOUR}:${CRON_MINUTE} 的账号发得出去。" >&2
  echo "[docker]       想按每个账号的 TASKS[].time 发：删掉这两个键（或设成 9/0）后重启容器。" >&2
else
  if [[ ! "$CRON_TICK_MINUTES" =~ ^[0-9]+$ || "$CRON_TICK_MINUTES" -lt 1 || "$CRON_TICK_MINUTES" -gt 59 ]]; then
    echo "[docker] CRON_TICK_MINUTES=${CRON_TICK_MINUTES} 非法（应为 1-59 的整数），回落到 5" >&2
    CRON_TICK_MINUTES=5
  fi
  CRON_SCHEDULE="*/${CRON_TICK_MINUTES} * * * *"
fi

# CRON_SECOND 只在"一天敲一次"的老模式里还有意义（run-task.sh 睡 CRON_SECOND 秒后开跑，
# 用来对齐整点）。每 5 分钟敲门的新模式里，到点时刻本来就由应用算，秒级偏移没有意义 ——
# 这里保留读取与提示，避免"我设了它却没反应"的困惑。
if [[ "${CRON_SECOND}" != "0" && -n "$LEGACY_ONCE_A_DAY" ]]; then
  echo "[docker] 提示：CRON_SECOND=${CRON_SECOND} 只对一天一次的 cron 生效（见 docker/run-task.sh）"
fi

cat > /etc/cron.d/douyin-spark-flow <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TZ=${TZ}
# 派发节奏要传给 cron 里的 python：应用侧靠它算“当天最后一次派发”在哪一分钟
# （core/schedule.py 的 dispatch_minutes_env）。少了这一行，容器环境变量设成
# CRON_TICK_MINUTES=10 时，应用仍旧按 5 分钟去夹取，夹出一个永不响的 23:55 ——
# 落在那之后的账号当天一条都发不出去（issue #19）。
CRON_TICK_MINUTES=${CRON_TICK_MINUTES}
${CRON_SCHEDULE} root /app/docker/run-task.sh >> /proc/1/fd/1 2>> /proc/1/fd/2
EOF

chmod 0644 /etc/cron.d/douyin-spark-flow

echo "[docker] timezone: ${TZ:-UTC}"
echo "[docker] cron schedule: ${CRON_SCHEDULE} (+${CRON_SECOND}s)"
echo "[docker] container started, waiting for scheduled runs"

exec cron -f
