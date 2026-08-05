#!/usr/bin/env bash
#
# OARadar hourly scan: discover current Pending items and refresh the latest
# Done-list pages. Only new or changed items enter the downstream queues, so
# running this every hour is safe and idempotent.
#
# Designed to be driven by systemd (see scripts/systemd/oaradar-hourly.*).
# Configure the OA webhook/secret via the environment file, e.g.
#   EnvironmentFile=%h/.config/oaradar/env

set -u

# Resolve the repository root (parent of this script's directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

CONFIG="${OA_CONFIG:-config.yaml}"

pending_status=0
done_status=0

# Scan all current Pending items. Only new or changed matters enqueue work.
uv run oa pending discover \
  --limit 500 \
  --max-pages 50 \
  --config "$CONFIG" \
  || pending_status=$?

# Scan the latest three Done-list pages and create archive-download tasks for
# any newly discovered items.
uv run oa manifest refresh-head \
  --max-pages 3 \
  --config "$CONFIG" \
  || done_status=$?

if (( pending_status != 0 || done_status != 0 )); then
  echo "OARadar hourly scan failed: pending=$pending_status done=$done_status" >&2
  exit 1
fi
