#!/usr/bin/env bash
# deploy/update.sh —— 从 git 更新**已经在跑**的部署（日常更新；首次安装用 deploy/install.sh）
#
# 为什么单开一个脚本：install.sh 是"从零装一台"的重量级入口（rsync 同步、建 venv、
# 下浏览器、装单元、必要时生成 .env）。线上 /opt/douyin-spark-fusion 自 2026-10-24 起
# 是 **git 工作树**，日常升级只需要把 HEAD 往前挪、再按需重启面板。重跑 install.sh 反而
# 会多出几件有副作用的事（rsync 不带 --delete、会把没提交的改动一起搬进 /opt）。
#
# 用法（在服务器上、root）：
#   sudo bash deploy/update.sh                    # 从当前分支的上游 ff（线上是 origin/github）
#   sudo bash deploy/update.sh /root/dsf master     # 从服务器内的开发目录 ff（本地开发流）
#   sudo bash deploy/update.sh origin master      # 显式指定远程与分支
#   sudo bash deploy/update.sh --check            # 只看有没有新提交，不动任何文件
#   sudo bash deploy/update.sh --no-restart       # 更新代码/单元，但不重启面板
#   sudo bash deploy/update.sh --dry-run          # 打印将要做什么，不执行
#   sudo bash deploy/update.sh --force            # 有轮次在跑也照更新（默认拒绝）
#
# 它只做四件事：
#   1. 检查有没有一轮正在跑（RUN_LOCK_FILE 的 pid 还活着）—— 在跑就拒绝（--force 除外）；
#   2. git fetch，并且**只允许 fast-forward**：历史分叉就停下让人自己看，绝不 reset/强合；
#   3. 按改动的文件决定副作用：requirements.txt 变了才 pip install；deploy/ 下的单元或
#      logrotate 规则变了才重装 + daemon-reload；只有代码变了才重启面板；
#   4. 轮询 /healthz 确认面板真的起来了，并告诉你有多少提交还没到 github（本脚本不 push）。
#
# .env 与 logs/ 不在 git 里（见 .gitignore），本脚本一个字节都不碰它们。
#
# 退出码：0 = 已是最新或更新成功；1 = 拒绝/失败；2 = 用法错误。

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/douyin-spark-fusion}"
DEFAULT_APP_DIR="/opt/douyin-spark-fusion"
WEB_UNIT="douyin-spark-fusion-web.service"
UNITS=(
  "douyin-spark-fusion-web.service"
  "douyin-spark-fusion-daily.service"
  "douyin-spark-fusion-daily.timer"
)

CHECK_ONLY=0
DO_RESTART=1
DRY_RUN=0
FORCE=0
SOURCE=""
BRANCH=""

say()  { printf '%s\n' "$*"; }
log()  { printf '[update] %s\n' "$*"; }
warn() { printf '[update] 警告：%s\n' "$*" >&2; }
die()  { printf '[update] 失败：%s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^$/p' "$0" | sed -e 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)      CHECK_ONLY=1 ;;
    --no-restart) DO_RESTART=0 ;;
    --dry-run)    DRY_RUN=1; DO_RESTART=1 ;;
    --force)      FORCE=1 ;;
    -h|--help)    usage; exit 0 ;;
    -*)           usage >&2; die "认不出来的开关：$1" ;;
    *)
      if [ -z "$SOURCE" ]; then SOURCE="$1"
      elif [ -z "$BRANCH" ]; then BRANCH="$1"
      else die "多余的参数：$1"
      fi
      ;;
  esac
  shift
done

[ -d "$APP_DIR" ] || die "目录不存在：$APP_DIR（首次部署用 deploy/install.sh）"
git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1 \
  || die "$APP_DIR 不是 git 工作树（首次部署用 deploy/install.sh；或先在根上 git init/clone）"
TOP="$(git -C "$APP_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
[ "$TOP" = "$APP_DIR" ] || die "$APP_DIR 只是子目录（工作树根是 ${TOP:-?}）：改设 APP_DIR=$TOP"

# 跑一轮的进程还活着时**不要**换代码：模块已经 import 进内存，换文件不会立刻生效，
# 却会让这一轮和下一轮跑在两份不同的代码上（排查时最容易误判的就是这种）。
env_file="$APP_DIR/.env"

# 从 .env 读一个键的原样值（去掉两端引号与 \r）。只用 sed/tr，不做 `source`：
# `.env` 里的口令、Cookie、消息模板都是数据，不是 shell 代码。
env_get() {
  [ -f "$env_file" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$env_file" \
    | tail -n 1 | tr -d '\r' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

lock_rel="$(env_get RUN_LOCK_FILE)"
lock_rel="${lock_rel:-logs/run.lock}"
case "$lock_rel" in
  /*) lock_path="$lock_rel" ;;
  *)  lock_path="$APP_DIR/$lock_rel" ;;
esac
if [ -f "$lock_path" ] && [ "$FORCE" -eq 0 ]; then
  pid="$(grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]\+' "$lock_path" 2>/dev/null | grep -o '[0-9]\+$' | head -n 1 || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    die "有一轮正在跑（pid=$pid，$lock_path）。等它跑完再更新；确认可以打断时加 --force"
  fi
fi

looks_like_path() {
  case "$1" in
    /*|./*|../*) return 0 ;;
  esac
  [ -d "$1" ]
}

cd "$APP_DIR"

# ---------------------------------------------------------------- 1. 取到目标提交
if [ -n "$SOURCE" ] && looks_like_path "$SOURCE"; then
  ref="${BRANCH:-HEAD}"
  log "从本地仓库取：$SOURCE（$ref）"
  git fetch --quiet --no-tags "$SOURCE" "$ref"
  TARGET="FETCH_HEAD"
  SRC_LABEL="$SOURCE"
elif [ -n "$SOURCE" ]; then
  log "从远程取：$SOURCE${BRANCH:+ $BRANCH}"
  if [ -n "$BRANCH" ]; then
    git fetch --quiet --no-tags "$SOURCE" "$BRANCH"
    TARGET="$SOURCE/$BRANCH"
  else
    git fetch --quiet --no-tags "$SOURCE"
    default_branch="$(git symbolic-ref --quiet --short "refs/remotes/$SOURCE/HEAD" 2>/dev/null \
      | sed -e "s|^$SOURCE/||")"
    [ -n "$default_branch" ] \
      || die "不知道 $SOURCE 的默认分支，请显式给：deploy/update.sh $SOURCE <分支>"
    TARGET="$SOURCE/$default_branch"
  fi
  SRC_LABEL="$SOURCE"
else
  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  [ -n "$upstream" ] \
    || die "当前分支没有上游（git branch --set-upstream-to）。显式给来源：deploy/update.sh origin master"
  log "从上游取：$upstream"
  git fetch --quiet --no-tags
  TARGET="$upstream"
  SRC_LABEL="$upstream"
fi

OLD="$(git rev-parse HEAD)"
NEW="$(git rev-parse "$TARGET")"
OLD_SHORT="$(git rev-parse --short "$OLD")"
NEW_SHORT="$(git rev-parse --short "$NEW")"

report_unpushed() {
  # 哪个 remote 指向 github 就拿哪个（/opt 上是 origin，/root/dsf 上叫 github），
  # 拿不到就闭嘴 —— 这里的唯一用途是提醒"服务器上还有提交没推到 github"，不是门禁。
  gh_remote="$(git config --get-regexp '^remote\..*\.url$' 2>/dev/null \
    | grep 'github\.com' | head -n 1 | sed -e 's/^remote\.//' -e 's/\.url[[:space:]].*$//')"
  [ -n "$gh_remote" ] || return 0
  gh_head="$(git symbolic-ref --quiet --short "refs/remotes/$gh_remote/HEAD" 2>/dev/null \
    || printf '%s/master' "$gh_remote")"
  git rev-parse --verify --quiet "$gh_head" >/dev/null 2>&1 || return 0
  pending="$(git rev-list --count "$gh_head..HEAD" 2>/dev/null || echo 0)"
  if [ "${pending:-0}" -gt 0 ]; then
    say ""
    log "本地领先 $gh_head ${pending:-0} 个提交 —— 这些还没到 github（本脚本不会 push，也不该由服务器 push）"
    git --no-pager log --oneline --no-decorate "$gh_head..HEAD" | sed 's/^/    /'
  fi
}

if [ "$OLD" = "$NEW" ]; then
  log "已经是最新：HEAD=$OLD_SHORT（$SRC_LABEL）"
  report_unpushed
  exit 0
fi

ahead="$(git rev-list --count "$NEW..$OLD")"
if [ "$ahead" -gt 0 ]; then
  die "不能 fast-forward：本地有 $ahead 个提交不在 $SRC_LABEL 里（历史已分叉）。先自己看：\
git -C $APP_DIR log --oneline $NEW_SHORT..$OLD_SHORT"
fi

count="$(git rev-list --count "$OLD..$NEW")"
log "将前进 $count 个提交：$OLD_SHORT → $NEW_SHORT"
git --no-pager log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/    /'

if [ "$CHECK_ONLY" -eq 1 ]; then
  say ""
  log "--check：没有改动任何文件。去掉 --check 才会真的更新"
  exit 0
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  die "$APP_DIR 的工作树有未提交的改动，拒绝 ff 合并（先 git -C $APP_DIR stash 或提交）"
fi

# ---------------------------------------------------------------- 2. ff 合并
if [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] 跳过 git merge --ff-only $NEW_SHORT"
else
  git merge --ff-only --no-edit "$NEW" >/dev/null
  log "已到 $NEW_SHORT"
fi

CHANGED="$(git diff --name-only "$OLD" "$NEW")"
has() { printf '%s\n' "$CHANGED" | grep -qE "$1"; }

# ---------------------------------------------------------------- 3. 依赖
if has '^requirements\.txt$'; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] requirements.txt 变了：跳过 pip install"
  elif [ -x "$APP_DIR/.venv/bin/pip" ]; then
    log "requirements.txt 变了：更新 venv 依赖"
    "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" \
      || die "pip install 失败 —— 代码已经是新的，但依赖没装上，先别重启面板"
  else
    warn "requirements.txt 变了，但 $APP_DIR/.venv/bin/pip 不存在：跳过（首次部署请用 install.sh）"
  fi
fi

# ---------------------------------------------------------------- 4. systemd 单元 / logrotate
units_changed=0
for unit in "${UNITS[@]}"; do
  if has "^deploy/$unit\$"; then units_changed=1; fi
done
logrotate_changed=0
if has '^deploy/logrotate\.conf$'; then logrotate_changed=1; fi

if [ "$units_changed" -eq 1 ] || [ "$logrotate_changed" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    log "[dry-run] 跳过重装 systemd 单元 / logrotate 规则"
  elif ! command -v systemctl >/dev/null 2>&1; then
    warn "没有 systemctl（容器/开发机）：跳过单元安装"
  else
    if [ "$units_changed" -eq 1 ]; then
      for unit in "${UNITS[@]}"; do
        if [ "$APP_DIR" = "$DEFAULT_APP_DIR" ]; then
          install -m 644 "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
        else
          sed "s|$DEFAULT_APP_DIR|$APP_DIR|g" "$APP_DIR/deploy/$unit" > "/etc/systemd/system/$unit"
          chmod 644 "/etc/systemd/system/$unit"
        fi
      done
      log "systemd 单元已重装"
    fi
    if [ "$logrotate_changed" -eq 1 ]; then
      if [ "$APP_DIR" = "$DEFAULT_APP_DIR" ]; then
        install -m 644 "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/douyin-spark-fusion
      else
        sed "s|$DEFAULT_APP_DIR|$APP_DIR|g" "$APP_DIR/deploy/logrotate.conf" \
          > /etc/logrotate.d/douyin-spark-fusion
        chmod 644 /etc/logrotate.d/douyin-spark-fusion
      fi
      log "logrotate 规则已重装"
    fi
    systemctl daemon-reload
    log "已 daemon-reload"
  fi
fi

# ---------------------------------------------------------------- 5. 重启面板
# 只有"面板进程真的会用到的东西"变了才重启：纯文档/测试/部署脚本的提交不该打断别人正在看的页面。
needs_restart=0
if [ "$units_changed" -eq 1 ]; then needs_restart=1; fi
if has '^(webui|core|utils|main\.py|run_configtool\.py|VERSION)(/|$)'; then needs_restart=1; fi

healthz_port() {
  port="$(env_get WEBUI_PORT)"
  case "$port" in ''|*[!0-9]*) port=8787 ;; esac
  printf '%s' "$port"
}

wait_healthz() {
  port="$(healthz_port)"
  url="http://127.0.0.1:$port/healthz"
  if ! command -v curl >/dev/null 2>&1; then
    warn "没有 curl，跳过健康检查；面板地址 http://127.0.0.1:$port/"
    return 0
  fi
  i=0
  while [ "$i" -lt 20 ]; do
    if curl -fsS -m 2 "$url" >/dev/null 2>&1; then
      log "面板已就绪：$url"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done
  warn "重启后 10 秒内 $url 没响应 —— 面板可能没起来：systemctl status $WEB_UNIT；journalctl -u $WEB_UNIT -n 50"
  return 1
}

if [ "$needs_restart" -eq 0 ]; then
  log "改动只涉及文档/测试/部署脚本：不需要重启面板"
elif [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] 会重启 $WEB_UNIT"
elif [ "$DO_RESTART" -eq 0 ]; then
  log "按 --no-restart 跳过重启；新代码要手动 systemctl restart $WEB_UNIT 才生效"
elif ! command -v systemctl >/dev/null 2>&1; then
  warn "没有 systemctl：跳过重启（容器里用 docker compose up -d --build）"
else
  log "重启面板：$WEB_UNIT"
  if systemctl restart "$WEB_UNIT"; then
    wait_healthz || exit 1
  else
    warn "重启失败 —— 代码已是新的，面板没起来：systemctl status $WEB_UNIT"
    exit 1
  fi
fi

if has '^\.env\.example$'; then
  say ""
  warn "「.env.example」变了：可能有新配置键，看一眼并按需补进 $env_file（本脚本不会改 .env）"
fi

report_unpushed
say ""
log "完成。当前 HEAD=$(git rev-parse --short HEAD)；.env 与 logs/ 没有被改动"
