#!/usr/bin/env bash
# 抖音续火花（融合版）安装脚本 —— 目标机：Ubuntu 22.04/24.04，root 运行。
#
# 幂等：重复执行不会覆盖已有 .env、不会重建已有 venv。可安全用于升级。
#
#   sudo bash deploy/install.sh                 # 从源码目录安装到 /opt/douyin-spark-fusion
#   sudo APP_DIR=/opt/xxx bash deploy/install.sh
#   sudo SKIP_BROWSER=1 bash deploy/install.sh  # 只装 Python 侧，不碰浏览器
#
# 做的事：venv → pip 安装依赖 → 确保浏览器可用 → 生成/校验 .env → 安装 systemd 单元。
# **不会**自动 enable 定时器：那是"真的会发消息"的动作，留给你显式执行（见文末提示）。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/douyin-spark-fusion}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3}"
CLOAK_DIR=/opt/cloakbrowser
CLOAK_VER="${CLOAK_CHROMIUM_VERSION:-146.0.7680.177.5}"
CLOAK_SHA="${CLOAK_BINARY_SHA256:-4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e}"
SYSTEM_CHROME="${SYSTEM_CHROME:-/opt/google/chrome/chrome}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash deploy/install.sh）"
command -v "$PY" >/dev/null || die "找不到 $PY"

# ---------------------------------------------------------------- 1. 拷贝代码
if [ "$(readlink -f "$SRC_DIR")" != "$(readlink -f "$APP_DIR" 2>/dev/null || echo -)" ]; then
  log "同步代码：$SRC_DIR → $APP_DIR"
  mkdir -p "$APP_DIR"
  # 不带 --delete：保留目标机上的 logs/、.env、venv 等运行时产物
  if command -v rsync >/dev/null; then
    rsync -a \
      --exclude '.venv' --exclude 'logs' --exclude '.env' --exclude 'chrome' \
      --exclude '__pycache__' --exclude '.git' --exclude 'data' \
      "$SRC_DIR"/ "$APP_DIR"/
  else
    tar -C "$SRC_DIR" \
      --exclude=.venv --exclude=logs --exclude=.env --exclude=chrome \
      --exclude=__pycache__ --exclude=.git --exclude=data \
      -cf - . | tar -C "$APP_DIR" -xf -
  fi
else
  log "已在目标目录，跳过拷贝：$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p logs

# ---------------------------------------------------------------- 2. venv + 依赖
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  log "创建 venv：$APP_DIR/.venv"
  "$PY" -m venv "$APP_DIR/.venv"
fi
log "安装 Python 依赖"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------- 3. 浏览器
if [ "${SKIP_BROWSER:-0}" != "1" ]; then
  if [ -x "$CLOAK_DIR/chrome" ]; then
    log "已存在 cloakbrowser 浏览器：$CLOAK_DIR/chrome"
  else
    log "尝试下载 cloakbrowser 浏览器（chromium-v$CLOAK_VER）"
    tmp="$(mktemp -d)"
    url="https://github.com/CloakHQ/CloakBrowser/releases/download/chromium-v${CLOAK_VER}/cloakbrowser-linux-x64.tar.gz"
    if curl -fL --connect-timeout 15 --max-time 900 -o "$tmp/cb.tar.gz" "$url" \
       && echo "$CLOAK_SHA  $tmp/cb.tar.gz" | sha256sum -c - >/dev/null 2>&1; then
      mkdir -p "$tmp/x" && tar -C "$tmp/x" -xzf "$tmp/cb.tar.gz"
      chrome_bin="$(find "$tmp/x" -maxdepth 6 -type f -name chrome -perm -u+x | head -n1)"
      if [ -n "$chrome_bin" ]; then
        mkdir -p "$CLOAK_DIR"
        cp -a "$(dirname "$chrome_bin")/." "$CLOAK_DIR/"     # 整目录：单拷 chrome 会缺 ICU 数据
        chmod +x "$CLOAK_DIR/chrome"
        log "浏览器就位：$CLOAK_DIR/chrome"
      else
        warn "压缩包里找不到 chrome 二进制，跳过"
      fi
    else
      warn "下载或校验失败（$url）"
    fi
    rm -rf "$tmp"
  fi

  # .env 里告诉程序浏览器在哪：优先 cloakbrowser，其次系统 Chrome + Playwright 回落
  ENV_FILE="$APP_DIR/.env"
  if [ -f "$ENV_FILE" ]; then
    if [ -x "$CLOAK_DIR/chrome" ]; then
      grep -q '^CLOAKBROWSER_BINARY_PATH=' "$ENV_FILE" || \
        echo "CLOAKBROWSER_BINARY_PATH=$CLOAK_DIR/chrome" >> "$ENV_FILE"
    elif [ -x "$SYSTEM_CHROME" ]; then
      warn "没有 cloakbrowser 浏览器，回落到系统 Chrome：$SYSTEM_CHROME"
      grep -q '^PLAYWRIGHT_CHROMIUM_EXECUTABLE=' "$ENV_FILE" || {
        {
          echo "# 没有 cloakbrowser 浏览器时的回落（见 core/browser.py）"
          echo "PLAYWRIGHT_CHROMIUM_EXECUTABLE=$SYSTEM_CHROME"
        } >> "$ENV_FILE"
      }
    fi
  fi

  log "浏览器探针"
  if "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/browser_probe.py"; then
    log "浏览器可用"
  else
    warn "浏览器探针失败 —— 先看上面的报错；也可以试 BROWSER_BACKEND=playwright 再跑一次"
  fi
fi

# ---------------------------------------------------------------- 4. .env
if [ ! -f "$APP_DIR/.env" ]; then
  log "生成 .env（从 .env.example）"
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  secret="$("$APP_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(32))')"
  # 端口/密钥/监听地址给个能直接用的默认值；口令留空 → 绑 0.0.0.0 时程序会拒绝启动（故意的）
  sed -i "s|^WEBUI_SESSION_SECRET=.*|WEBUI_SESSION_SECRET=$secret|" "$APP_DIR/.env"
  sed -i "s|^WEBUI_HOST=.*|WEBUI_HOST=0.0.0.0|" "$APP_DIR/.env"
  sed -i "s|^WEBUI_PORT=.*|WEBUI_PORT=8787|" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  warn "请编辑 $APP_DIR/.env：填 TASKS / COOKIES_<unique_id> / WEBUI_PASSWORD 后再启动服务"
else
  log "已存在 .env，保持不动：$APP_DIR/.env"
fi

# ---------------------------------------------------------------- 5. systemd
log "安装 systemd 单元"
install -m 644 "$APP_DIR/deploy/douyin-spark-fusion-web.service"  /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/douyin-spark-fusion-daily.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/douyin-spark-fusion-daily.timer"   /etc/systemd/system/
systemctl daemon-reload

# 日志轮转（F8）：生产默认 LOG_LEVEL=Debug，两个单元都往同一个文件追加，
# 不轮转迟早把磁盘写满。日志目录不存在就先建出来，否则 logrotate 会因为
# 通配符没匹配到而报错（missingok 只管 rotate 时文件不在的情况）。
log "安装日志轮转规则"
mkdir -p "$APP_DIR/logs"
install -m 644 "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/douyin-spark-fusion
if command -v logrotate >/dev/null 2>&1; then
  logrotate -d /etc/logrotate.d/douyin-spark-fusion >/dev/null 2>&1 \
    && log "logrotate 规则自测通过（-d 只演练）" \
    || warn "logrotate -d 自测有告警，请手动看：logrotate -d /etc/logrotate.d/douyin-spark-fusion"
else
  warn "没找到 logrotate，规则已放在 /etc/logrotate.d/ 但不生效（apt install logrotate）"
fi

log "完成。接下来（**都没自动做**）："
cat <<'EOF'
  1) 编辑 /opt/douyin-spark-fusion/.env：TASKS、COOKIES_<unique_id>、WEBUI_PASSWORD（或 WEBUI_PASSWORD_HASH）
  2) 空跑验证（不发送任何消息）：
       cd /opt/douyin-spark-fusion && .venv/bin/python main.py dry-run full
  3) 启动面板：
       systemctl enable --now douyin-spark-fusion-web.service
       # 面板：http://<本机IP>:8787
  4) 确认无误后再开定时发送：
       systemctl enable --now douyin-spark-fusion-daily.timer
       systemctl list-timers douyin-spark-fusion-daily.timer
EOF
