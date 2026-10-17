#!/usr/bin/env bash
# 抖音续火花（融合版）安装脚本 —— 目标机：Ubuntu 22.04/24.04，root 运行。
#
# 幂等：重复执行不会覆盖已有 .env、不会重建已有 venv。可安全用于升级。
#
#   sudo bash deploy/install.sh                 # 从源码目录安装到 /opt/douyin-spark-fusion
#   sudo APP_DIR=/opt/xxx bash deploy/install.sh
#   sudo SKIP_BROWSER=1 bash deploy/install.sh  # 只装 Python 侧，不碰浏览器
#   sudo bash deploy/install.sh --dry-run       # 只同步代码/venv/.env，不写 /etc、不重启服务
#
# 升级（重复执行）末尾会对**已在运行**的面板做一次 `systemctl restart`：不重启的话
# 面板进程还跑着上一次装进去的代码，表现为"明明升级了、页面上还是旧行为"。
# 定时器（daily.timer）**不会**被自动 enable，那是"真的会发消息"的动作。
#
# APP_DIR 可以不是默认值：unit 与 logrotate 规则里的绝对路径会在安装时被替换成
# 真实的 APP_DIR（见下面「渲染并安装 systemd 单元」一段）。代价是安装后的文件与
# `deploy/` 里的模板不再逐字相同 —— 以 `/etc/systemd/system/` 下的实际文件为准。
#
# 做的事：venv → pip 安装依赖 → 确保浏览器可用 → 生成/校验 .env → 安装 systemd 单元。
# **不会**自动 enable 定时器：那是"真的会发消息"的动作，留给你显式执行（见文末提示）。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/douyin-spark-fusion}"
#: 三个交付件（web unit / daily unit / logrotate）里写的默认路径。
#: 只在"替换成真实 APP_DIR"时当查找目标用，**产品默认值本身没变**：
#: .env.example 的 OPS_LOG_FILE、docs/ 里的示例都还是这个路径。
DEFAULT_APP_DIR="/opt/douyin-spark-fusion"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3}"
CLOAK_DIR=/opt/cloakbrowser
CLOAK_VER="${CLOAK_CHROMIUM_VERSION:-146.0.7680.177.5}"
CLOAK_SHA="${CLOAK_BINARY_SHA256:-4a12bcde95fa1bb1beef2b41ab5e5c27c36be78e3be3d0dac8c64d705216670e}"
SYSTEM_CHROME="${SYSTEM_CHROME:-/opt/google/chrome/chrome}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# `--dry-run`：只做"拷贝代码 + venv + .env"这些落在 APP_DIR 里的动作，
# 跳过所有写 /etc 的步骤（systemd 单元、logrotate 规则）与最后的面板重启。
# 装 systemd 单元本身要求 root，本脚本也强制 root，所以在容器/CI 里预演时
# 需要一个不碰宿主机配置的开关。
# ⚠️ 这一段必须放在 log/warn/die **之后**：bash 里函数要先定义再调用，
#    放在前面时 `--bogus` 会变成"die: 未找到命令"（127），
#    用户看到的是一句 shell 报错而不是"未知参数"。
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    *) die "未知参数：$arg（只支持 --dry-run）" ;;
  esac
done

[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo bash deploy/install.sh）"
command -v "$PY" >/dev/null || die "找不到 $PY"

# APP_DIR 会被原样拼进 systemd 单元与日志路径：带空格/百分号（systemd 的转义字符）
# 或换行时，下面的 sed 渲染出来的路径必然与真实目录对不上，装出来的 unit 会指向
# 一个不存在的目录（表现为"装成功了但服务起不来"）。宁可在安装前就拒绝。
# `&` 会让 sed 的替换串把匹配到的路径原地重放（路径里出现 /opt/x&y 就会渲染成
# /opt//opt/x&y），`|` 会提前结束 s||| 的分隔符 —— 两个都拒绝，别让渲染悄悄写坏。
case "$APP_DIR" in
  *[[:space:]]*) die "APP_DIR 不能含空白字符：$APP_DIR" ;;
  *%*)           die "APP_DIR 不能含 %（systemd 的转义字符）：$APP_DIR" ;;
  *'&'*)         die "APP_DIR 不能含 &（sed 替换串的元字符）：$APP_DIR" ;;
  *'|'*)         die "APP_DIR 不能含 |（sed 分隔符）：$APP_DIR" ;;
esac
for cmd in install sed; do
  command -v "$cmd" >/dev/null || die "找不到 $cmd（后缀渲染 unit 与 logrotate 规则要用）"
done

# ---------------------------------------------------------------- 1. 拷贝代码
if [ "$(readlink -f "$SRC_DIR")" != "$(readlink -f "$APP_DIR" 2>/dev/null || echo -)" ]; then
  log "同步代码：$SRC_DIR → $APP_DIR"
  mkdir -p "$APP_DIR"
  # 不带 --delete：保留目标机上的 logs/、.env、venv 等运行时产物
  if command -v rsync >/dev/null; then
    rsync -a \
      --exclude '.venv' --exclude 'logs' --exclude '.env' --exclude '.env.tmp_*' \
      --exclude '.tmp_*' \
      --exclude 'chrome' --exclude '__pycache__' --exclude '.git' --exclude 'data' \
      "$SRC_DIR"/ "$APP_DIR"/
  else
    # python-dotenv 的 set_key 是"写临时文件 → os.replace"，中间态里装着**完整的
    # .env**（含全部 COOKIES_）。真实文件名是 `.tmp_xxxxxxxx`（`NamedTemporaryFile
    # (prefix=".tmp_", dir=<.env 所在目录>)`，实测 1.2.4；requirements 钉的是 1.2.2），
    # 早期代码/别处注释里写作 `.env.tmp_*` —— 两套都排除。这种文件平时不存在，但
    # 保存过程被打断就会留在目录里，不能被同步到生产目录（打包/备份/快照也会带走）。
    tar -C "$SRC_DIR" \
      --exclude=.venv --exclude=logs --exclude=.env --exclude='.env.tmp_*' \
      --exclude='.tmp_*' \
      --exclude=chrome --exclude=__pycache__ --exclude=.git --exclude=data \
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
  warn "请编辑 $APP_DIR/.env：填 TASKS / COOKIES_<unique_id> / WEBUI_PASSWORD 后再启动服务"
else
  log "已存在 .env，保持不动：$APP_DIR/.env"
fi

# .env 里装着 COOKIES_<unique_id>（就是抖音 sessionid，等于账号本身），必须 600。
# 这一句以前只写在上面「新装」分支里，而 .env.example 教的是先 `cp .env.example .env`
# 再跑脚本 —— cp 出来的源文件是 644，于是走 else 分支、.env 永远是 644：同机任何用户
# 都能把 sessionid 读走，它还会跟着整目录打包/备份/快照一起外泄。所以挪出分支无条件收紧，
# 升级场景同样管用。文件不存在（从没生成过）就静默跳过；chmod 失败只告警，不中断安装。
if [ -f "$APP_DIR/.env" ]; then
  chmod 600 "$APP_DIR/.env" 2>/dev/null \
    || warn "收紧 $APP_DIR/.env 权限失败，请手动执行：chmod 600 $APP_DIR/.env"
fi

# 扫码登录的资料目录：Chromium 的 Cookies sqlite（sessionid 在里面）与整份登录态，
# 比 .env 还全，一样只给本用户。目录要扫过码才有，不存在就跳过。
# 父目录 logs/ 保持 755 不影响：没有 o+x 就进不去 login-profile，连有哪些账号都列不出来。
if [ -d "$APP_DIR/logs/login-profile" ]; then
  chmod 700 "$APP_DIR/logs/login-profile" 2>/dev/null \
    || warn "收紧登录资料目录失败，请手动执行：chmod 700 $APP_DIR/logs/login-profile"
fi

# ---------------------------------------------------------------- 5. systemd
# 交付件里的绝对路径是**模板默认值**：`deploy/*.service` 与 `deploy/logrotate.conf`
# 里每一处都写 /opt/douyin-spark-fusion。APP_DIR 一旦不是默认值，直接 `install`
# 这两个文件就会装出一套"指错路径的 unit + 一条永远轮转不到日志的规则" ——
# 服务起不来、日志越写越大，而安装脚本自己一声不响。所以这里渲染成真实路径再装。
#
# 为什么选"渲染"而不是"拒绝非默认 APP_DIR"：脚本开头就文档化了 `APP_DIR=/opt/xxx`，
# 这是既有用户能用的功能，删掉它是行为回退；而渲染只多依赖 sed（已在上面校验存在）。
# 代价写在文件头：装到 /etc 下的文件与 deploy/ 里的模板不再逐字相同，排查时以
# `/etc/systemd/system/douyin-spark-fusion-*.service` 的实际内容为准。
#
# systemd 侧只替换单元里的路径即可，不需要动 [Service] 的其他字段；
# logrotate 侧还要把日志目录建出来（没有 o+x 的父目录会让 logrotate 报 missingok 之外
# 的错误），所以两条渲染路径分开处理。
render_unit() {  # render_unit <模板文件> <目标文件>
  if [ "$APP_DIR" = "$DEFAULT_APP_DIR" ]; then
    install -m 644 "$1" "$2"
  else
    sed "s|$DEFAULT_APP_DIR|$APP_DIR|g" "$1" > "$2"
    chmod 644 "$2"
  fi
}

log "安装 systemd 单元"
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] 跳过写 /etc/systemd/system/（web unit / daily unit / daily timer）"
else
  render_unit "$APP_DIR/deploy/douyin-spark-fusion-web.service"   /etc/systemd/system/douyin-spark-fusion-web.service
  render_unit "$APP_DIR/deploy/douyin-spark-fusion-daily.service" /etc/systemd/system/douyin-spark-fusion-daily.service
  render_unit "$APP_DIR/deploy/douyin-spark-fusion-daily.timer"   /etc/systemd/system/douyin-spark-fusion-daily.timer
  systemctl daemon-reload
fi

# 日志轮转（F8）：两个单元都往同一个文件追加，整轮发送的每一步都落盘，
# 不轮转迟早把磁盘写满。日志目录不存在就先建出来，否则 logrotate 会因为
# 通配符没匹配到而报错（missingok 只管 rotate 时文件不在的情况）。
log "安装日志轮转规则"
mkdir -p "$APP_DIR/logs"
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] 跳过写 /etc/logrotate.d/douyin-spark-fusion"
elif [ "$APP_DIR" = "$DEFAULT_APP_DIR" ]; then
  install -m 644 "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/douyin-spark-fusion
else
  sed "s|$DEFAULT_APP_DIR|$APP_DIR|g" "$APP_DIR/deploy/logrotate.conf" > /etc/logrotate.d/douyin-spark-fusion
  chmod 644 /etc/logrotate.d/douyin-spark-fusion
  # 模板注释里还写着默认路径的说明，替换后原样保留 —— 这里只提示一声
  log "logrotate 规则已按 APP_DIR 渲染：$APP_DIR/logs/*.log"
fi
if [ "$DRY_RUN" != "1" ] && command -v logrotate >/dev/null 2>&1; then
  logrotate -d /etc/logrotate.d/douyin-spark-fusion >/dev/null 2>&1 \
    && log "logrotate 规则自测通过（-d 只演练）" \
    || warn "logrotate -d 自测有告警，请手动看：logrotate -d /etc/logrotate.d/douyin-spark-fusion"
elif [ "$DRY_RUN" != "1" ]; then
  warn "没找到 logrotate，规则已放在 /etc/logrotate.d/ 但不生效（apt install logrotate）"
fi

# ---------------------------------------------------------------- 6. 重启面板
# 升级场景：unit 重装 + daemon-reload 之后，**已在运行**的面板进程仍然跑着旧代码。
# 不重启的表现是"代码已经是最新的、页面行为还是旧的"，而且日志里没有任何线索。
#
# 判定条件刻意保守：单元已被 enable（开机自启）**且此刻 active** 才重启。
#  - 没装过 → 什么都不做（提示里已有 enable --now 的手动步骤）；
#  - unit 存在但没 enable，却正在 active：可能是一次性 `systemd-run` 起的，不碰；
#  - enabled 但 failed/stop：管理员刚手工停过，重启会把它的意图顶掉。
# 重启失败不致命（面板起不来不该让"代码已同步"这件事回退），只告警。
log "重启已生效的面板服务（如有）"
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] 跳过 systemctl restart douyin-spark-fusion-web.service"
elif ! command -v systemctl >/dev/null 2>&1; then
  warn "没有 systemctl（容器/非 systemd 环境），跳过重启；用 docker compose up -d --build 或手动重启"
elif [ -f /etc/systemd/system/douyin-spark-fusion-web.service ] \
     && systemctl is-enabled --quiet douyin-spark-fusion-web.service 2>/dev/null \
     && systemctl is-active  --quiet douyin-spark-fusion-web.service 2>/dev/null; then
  if systemctl restart douyin-spark-fusion-web.service; then
    log "面板已重启：systemctl status douyin-spark-fusion-web.service 可看状态"
  else
    warn "面板重启失败 —— 代码已同步，请手动看：systemctl status douyin-spark-fusion-web.service"
  fi
else
  log "面板当前未在运行（或未 enable），跳过重启；启动见下面第 3 步"
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
