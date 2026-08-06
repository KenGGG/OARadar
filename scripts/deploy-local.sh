#!/usr/bin/env bash
#
# Unified local deployment for OARadar (plan-0806-1 §8).
#
# One command that:
#   1. 检查工作区 (warn on uncommitted changes)
#   2. 安装依赖 (uv sync)
#   3. 数据库升级 (upgrade_database)
#   4. 构建 WebUI (npm ci && npm run build -> src/oa_knowledge/web/static)
#   5. 运行测试
#   6. 安装/更新 systemd 服务
#   7. 重启 Web 与 Worker
#   8. 显示服务状态与 Web 访问地址
#
# First deploy: pass --bootstrap to seed the pending baseline before enabling
# the hourly timer.
#
# Usage:
#   ./scripts/deploy-local.sh --project-root "$PWD" --config "$PWD/config.yaml"
#   ./scripts/deploy-local.sh --project-root "$PWD" --config "$PWD/config.yaml" --bootstrap
#   ./scripts/deploy-local.sh --project-root "$PWD" --config "$PWD/config.yaml" --skip-systemd

set -euo pipefail

PROJECT_ROOT=""
CONFIG=""
TIMEZONE="Asia/Shanghai"
SKIP_TESTS=0
SKIP_SYSTEMD=0
BOOTSTRAP=0

usage() {
  cat <<'EOF'
Usage: deploy-local.sh --project-root DIR --config FILE [--timezone TZ]
                        [--bootstrap] [--skip-tests] [--skip-systemd] [-h]

  --project-root DIR   Repository root (contains src/, webui/, scripts/)
  --config FILE        Path to config.yaml
  --timezone TZ        Timers timezone (default Asia/Shanghai)
  --bootstrap          Seed the pending baseline before enabling hourly timer
  --skip-tests         Do not run the test suite
  --skip-systemd       Do not touch systemd (dev machines without a user manager)
  -h, --help           Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --bootstrap) BOOTSTRAP=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    --skip-systemd) SKIP_SYSTEMD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$PROJECT_ROOT" || -z "$CONFIG" ]]; then
  echo "error: --project-root and --config are required" >&2
  usage >&2
  exit 2
fi

PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"
CONFIG="$(realpath "$CONFIG")"

# uv binary (resolved, with fallback to the common user install location).
if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  UV_BIN="$HOME/.local/bin/uv"
else
  echo "uv not found on PATH or ~/.local/bin/uv" >&2
  exit 1
fi

step() { echo; echo "==> $*"; }

# 1. Workspace check (warn, do not block).
step "1/8 检查工作区"
if [[ -d "$PROJECT_ROOT/.git" ]]; then
  if [[ -n "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null)" ]]; then
    echo "WARNING: 工作区存在未提交改动；部署仍会继续，但建议先提交。" >&2
  else
    echo "工作区干净 ($(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown))."
  fi
else
  echo "（非 git 仓库，跳过脏检查）"
fi

# 2. Install dependencies.
step "2/8 安装依赖"
"$UV_BIN" sync

# 3. Upgrade database.
step "3/8 数据库升级"
"$UV_BIN" run python - <<PY || { echo "数据库升级失败" >&2; exit 1; }
from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
upgrade_database(load_settings("$CONFIG").database_path)
PY

# 4. Build the WebUI (vite writes directly into src/oa_knowledge/web/static).
step "4/8 构建 WebUI"
if command -v npm >/dev/null 2>&1 && [[ -d "$PROJECT_ROOT/webui" ]]; then
  ( cd "$PROJECT_ROOT/webui" && npm ci && npm run build )
  echo "WebUI 已构建至 $PROJECT_ROOT/src/oa_knowledge/web/static"
else
  echo "WARNING: npm 不可用或 webui/ 缺失，静态资源可能过期。" >&2
fi

# 5. Run tests.
if [[ "$SKIP_TESTS" -eq 0 ]]; then
  step "5/8 运行测试"
  "$UV_BIN" run pytest -q || { echo "测试失败，部署中止。" >&2; exit 1; }
else
  step "5/8 运行测试（已跳过）"
fi

# 6-7. systemd install + restart (best-effort without a user manager).
SYSTEMD_AVAILABLE=0
if command -v systemctl >/dev/null 2>&1 && systemctl --user is-active -.scope >/dev/null 2>&1; then
  SYSTEMD_AVAILABLE=1
fi

if [[ "$SKIP_SYSTEMD" -eq 1 ]]; then
  echo "（--skip-systemd：跳过 systemd 安装与重启）"
elif [[ "$SYSTEMD_AVAILABLE" -eq 0 ]]; then
  echo "WARNING: 当前环境无可用 systemd 用户管理器，跳过 systemd 步骤。" >&2
else
  step "6/8 安装/更新 systemd 服务"
  "$PROJECT_ROOT/scripts/install-systemd-user.sh" \
    --project-root "$PROJECT_ROOT" --config "$CONFIG" --timezone "$TIMEZONE"

  step "7/8 重启 Web 与 Worker"
  systemctl --user restart oaradar-web.service oaradar-worker.service oaradar-markdown-worker.service

  if [[ "$BOOTSTRAP" -eq 1 ]]; then
    step "首次部署：执行待办基线扫描"
    "$UV_BIN" run oa schedule bootstrap --config "$CONFIG"
  fi
fi

# 8. Status + access address.
step "8/8 服务状态"
WEB_HOST="$( "$UV_BIN" run python - <<PY
from oa_knowledge.config import load_settings
s = load_settings("$CONFIG")
print(f"{s.web.host}:{s.web.port}")
PY
)"
if [[ "$SYSTEMD_AVAILABLE" -eq 1 && "$SKIP_SYSTEMD" -eq 0 ]]; then
  for unit in oaradar-web.service oaradar-worker.service oaradar-markdown-worker.service oaradar-hourly.timer oaradar-nightly.timer; do
    state="$(systemctl --user is-active "$unit" 2>/dev/null || echo inactive)"
    echo "  $unit : $state"
  done
else
  echo "  （systemd 未启用；使用 'uv run oa web --config $CONFIG' 手工启动 Web）"
fi
echo
echo "部署完成。Web 控制台： http://${WEB_HOST}"
