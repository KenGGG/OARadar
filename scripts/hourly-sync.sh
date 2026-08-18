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

# Only enqueue the durable operation. The single OA Worker owns the persistent
# browser profile and performs the real read-only scan, preventing Chromium
# profile corruption when an online verification batch is active.
hourly_status=0
uv run oa schedule enqueue hourly \
  --config "$CONFIG" \
  || hourly_status=$?

if (( hourly_status != 0 )); then
  echo "OARadar hourly scan failed: hourly=$hourly_status" >&2
  exit 1
fi
