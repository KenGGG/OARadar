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

WEB_ENDPOINT="$(python3 - "$CONFIG" <<'PY'
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    web = cfg.get("web", {})
    host = web.get("host", "127.0.0.1")
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"
    print(f"http://{host}:{int(web.get('port', 8000))}")
except Exception:
    print("http://127.0.0.1:8000")
PY
)"

# Server timezone sanity check.
if command -v timedatectl >/dev/null 2>&1; then
  SERVER_TZ="$(timedatectl show --property=TimeZone --value 2>/dev/null || true)"
  if [[ -n "$SERVER_TZ" && "$SERVER_TZ" != "$TIMEZONE" ]]; then
    echo "NOTE: server timezone is '$SERVER_TZ'; timers are pinned to $TIMEZONE via OnCalendar=TZ=." >&2
  fi
fi

# Current git commit (for deploy provenance / reproducibility).
GIT_COMMIT="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "  git commit   : $GIT_COMMIT"

# Build the WebUI so the served static assets are current (plan-0806-1 §5.1).
if command -v npm >/dev/null 2>&1 && [[ -d "$PROJECT_ROOT/webui" ]]; then
  echo "Building WebUI (npm ci && npm run build)..."
  ( cd "$PROJECT_ROOT/webui" && npm ci && npm run build )
  echo "WebUI built into $PROJECT_ROOT/src/oa_knowledge/web/static"
else
  echo "WARNING: npm not available or webui/ missing; the Web console will serve" >&2
  echo "stale or missing static assets. Run 'npm ci && npm run build' in webui/." >&2
fi

# Verify Python dependencies are importable.
echo "Verifying Python dependencies..."
"$UV_BIN" run python -c "import oa_knowledge, uvicorn, fastapi" \
  || { echo "Python dependency check failed" >&2; exit 1; }

# Verify / upgrade the database.
echo "Verifying database schema..."
"$UV_BIN" run python - <<PY || { echo "Database verification failed" >&2; exit 1; }
from oa_knowledge.config import load_settings
from oa_knowledge.db.migrate import upgrade_database
from pathlib import Path
upgrade_database(load_settings(Path("$CONFIG")).database_path)
PY

# Render and write the unit files.
echo "Rendering systemd units..."
"$UV_BIN" run python -m oa_knowledge.ops.systemd_render \
  --project-root "$PROJECT_ROOT" \
  --config "$CONFIG" \
  --timezone "$TIMEZONE" \
  --env-file "$ENV_FILE" \
  --uv-bin "$UV_BIN" \
  --output-dir "$SYSTEMD_DIR"

systemctl --user daemon-reload

systemctl --user enable --now \
  oaradar-web.service \
  oaradar-worker.service \
  oaradar-markdown-worker.service \
  oaradar-hourly.timer \
  oaradar-nightly.timer

echo "Enabled and started OARadar services/timers."

# Post-install verification: every check must pass or the script fails (§5.3).
echo "Verifying installed units..."
verify_failed=0
for unit in oaradar-web.service oaradar-worker.service oaradar-markdown-worker.service; do
  if ! systemctl --user is-active --quiet "$unit"; then
    echo "FAILED: $unit is not active" >&2
    verify_failed=1
  fi
done
for unit in oaradar-hourly.timer oaradar-nightly.timer; do
  if ! systemctl --user is-enabled --quiet "$unit"; then
    echo "FAILED: $unit is not enabled" >&2
    verify_failed=1
  fi
done
systemctl --user list-timers 'oaradar-*' || verify_failed=1

if [[ "$verify_failed" -ne 0 ]]; then
  echo "Post-install verification failed; see errors above." >&2
  exit 1
fi
echo "All services active, timers enabled. Install complete."
echo "Web console: $WEB_ENDPOINT"
echo "Next run (hourly): $("$UV_BIN" run python -m oa_knowledge.ops.systemd_render --help >/dev/null 2>&1; systemctl --user list-timers --no-pager 'oaradar-hourly.timer' | awk 'NR==2{print $1" "$2" "$3}')"
