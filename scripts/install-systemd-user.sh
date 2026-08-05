#!/usr/bin/env bash
#
# Install OARadar as a user-level systemd setup: two long-running workers and
# hourly/nightly timers. All machine-specific paths (project root, uv binary,
# config, env file) are detected at install time and substituted into the
# templates under scripts/systemd/templates, so the committed units never
# hard-code a single machine (plan-0805-02 §5).
#
# Usage:
#   ./scripts/install-systemd-user.sh \
#     --project-root "$PWD" \
#     --config "$PWD/config.yaml" \
#     --timezone Asia/Shanghai

set -euo pipefail

PROJECT_ROOT=""
CONFIG=""
TIMEZONE="Asia/Shanghai"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PROJECT_ROOT" || -z "$CONFIG" ]]; then
  echo "Usage: $0 --project-root DIR --config FILE [--timezone TZ]" >&2
  exit 2
fi

PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"
CONFIG="$(realpath "$CONFIG")"
REPO_ROOT="$PROJECT_ROOT"

echo "Detected configuration:"
echo "  project root : $PROJECT_ROOT"
echo "  config       : $CONFIG"
echo "  timezone     : $TIMEZONE"

# uv binary (resolved, with fallback to the common user install location).
if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  UV_BIN="$HOME/.local/bin/uv"
else
  echo "uv not found on PATH or ~/.local/bin/uv" >&2
  exit 1
fi
echo "  uv binary     : $UV_BIN"

# Python environment (informational).
echo "  python       : $(command -v python3 || true)"

# systemd user directory.
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"
echo "  systemd dir  : $SYSTEMD_DIR"

# Environment file for secrets (never committed). Create an empty, private one
# if it does not exist; do not overwrite an existing file.
ENV_FILE="$HOME/.config/oaradar/env"
mkdir -p "$(dirname "$ENV_FILE")"
chmod 700 "$(dirname "$ENV_FILE")"
if [[ ! -f "$ENV_FILE" ]]; then
  cat >"$ENV_FILE" <<'EOF'
# OARadar runtime secrets (chmod 600). Fill these in, then re-run install.
# FEISHU_OA_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/REPLACE
# FEISHU_OA_SECRET=REPLACE
EOF
  chmod 600 "$ENV_FILE"
  echo "  env file     : $ENV_FILE (created empty — edit it to add secrets)"
else
  echo "  env file     : $ENV_FILE (exists, left untouched)"
fi

# data_root (informational, read from the config).
DATA_ROOT="$(python3 - "$CONFIG" <<'PY'
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    print(cfg.get("app", {}).get("data_root", "(default)"))
except Exception:
    print("(unreadable)")
PY
)"
echo "  data_root    : $DATA_ROOT"

# Server timezone sanity check.
if command -v timedatectl >/dev/null 2>&1; then
  SERVER_TZ="$(timedatectl show --property=TimeZone --value 2>/dev/null || true)"
  if [[ -n "$SERVER_TZ" && "$SERVER_TZ" != "$TIMEZONE" ]]; then
    echo "NOTE: server timezone is '$SERVER_TZ'; timers are pinned to $TIMEZONE via OnCalendar=TZ=." >&2
  fi
fi

# Render and write the unit files.
echo "Rendering systemd units..."
uv run python -m oa_knowledge.ops.systemd_render \
  --project-root "$PROJECT_ROOT" \
  --config "$CONFIG" \
  --timezone "$TIMEZONE" \
  --env-file "$ENV_FILE" \
  --uv-bin "$UV_BIN" \
  --output-dir "$SYSTEMD_DIR"

systemctl --user daemon-reload

systemctl --user enable --now \
  oaradar-worker.service \
  oaradar-markdown-worker.service \
  oaradar-hourly.timer \
  oaradar-nightly.timer

echo "Enabled and started OARadar services/timers."
echo "Verify with: systemctl --user status 'oaradar-*'"
