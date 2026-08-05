#!/usr/bin/env bash
#
# OARadar operational health check (plan-0805-02 §5.4).
#
# Verifies the systemd units are running, the scheduled runs recorded
# success, the database is readable, the Markdown queue is not backlogged,
# Feishu is configured, and there is disk headroom. OA login validity is
# inferred from the recency of the last successful hourly run (a stale run
# means the saved browser session has likely expired) rather than performing
# a live OA request.
#
# Exit code is non-zero only when a required check fails; WARN lines are
# informational.

set -uo pipefail

CONFIG="${OA_CONFIG:-config.yaml}"
if [[ ! -f "$CONFIG" ]]; then
  echo "config not found: $CONFIG (set OA_CONFIG or run from the project root)" >&2
  exit 2
fi

SYSTEMD=(systemctl --user)
PASS=0
WARN=0
FAIL=0

emit() {  # level name detail
  local level="$1" name="$2" detail="$3"
  printf '%-5s %-22s %s\n' "$level" "$name" "$detail"
  case "$level" in
    OK) PASS=$((PASS+1)) ;;
    WARN) WARN=$((WARN+1)) ;;
    FAIL) FAIL=$((FAIL+1)) ;;
  esac
}

# --- systemd units ---------------------------------------------------------
for unit in oaradar-worker.service oaradar-markdown-worker.service; do
  if "${SYSTEMD[@]}" is-active --quiet "$unit" 2>/dev/null; then
    emit OK "$unit" "active"
  else
    emit FAIL "$unit" "not active"
  fi
done

timer_next="$("${SYSTEMD[@]}" list-timers 'oaradar-*' --no-legend 2>/dev/null | head -1)"
if [[ -n "$timer_next" ]]; then
  emit OK "timers" "next: $timer_next"
else
  emit FAIL "timers" "no oaradar timer scheduled"
fi

# --- scheduled run records ------------------------------------------------
runs_json="$(uv run oa schedule status --limit 5 --config "$CONFIG" 2>/dev/null || true)"
hourly_ok=""
nightly_ok=""
if [[ -n "$runs_json" ]]; then
  hourly_status="$(printf '%s' "$runs_json" | python3 -c 'import sys,json
try:
    rows=json.load(sys.stdin)
except Exception:
    rows=[]
for r in rows:
    if r.get("stage")=="scheduled_hourly":
        print(r.get("status")); break' 2>/dev/null || true)"
  nightly_status="$(printf '%s' "$runs_json" | python3 -c 'import sys,json
try:
    rows=json.load(sys.stdin)
except Exception:
    rows=[]
for r in rows:
    if r.get("stage")=="scheduled_nightly":
        print(r.get("status")); break' 2>/dev/null || true)"
  if [[ "$hourly_status" == "completed" ]]; then emit OK "hourly-run" "last completed"; else emit WARN "hourly-run" "last=$hourly_status"; fi
  if [[ "$nightly_status" == "completed" ]]; then emit OK "nightly-run" "last completed"; else emit WARN "nightly-run" "last=$nightly_status"; fi
else
  emit WARN "scheduled-runs" "could not read runs table"
fi

# --- database readable -----------------------------------------------------
if uv run oa status --config "$CONFIG" >/dev/null 2>&1; then
  emit OK "database" "readable"
else
  emit FAIL "database" "not readable"
fi

# --- OA login inferred from last hourly run recency -----------------------
if command -v python3 >/dev/null 2>&1 && [[ -n "$runs_json" ]]; then
  age_hours="$(printf '%s' "$runs_json" | python3 -c 'import sys,json,datetime
try:
    rows=json.load(sys.stdin)
except Exception:
    rows=[]
now=datetime.datetime.now(datetime.timezone.utc)
for r in rows:
    if r.get("stage")=="scheduled_hourly" and r.get("finished_at"):
        try:
            fin=datetime.datetime.fromisoformat(r["finished_at"].replace("Z","+00:00"))
            print(round((now-fin).total_seconds()/3600,1)); break
        except Exception:
            pass' 2>/dev/null || true)"
  if [[ -n "$age_hours" ]]; then
    if awk "BEGIN{exit !($age_hours < 12)}"; then emit OK "oa-login" "last hourly ${age_hours}h ago"; else emit WARN "oa-login" "last hourly ${age_hours}h ago — session may have expired"; fi
  else
    emit WARN "oa-login" "no recent hourly run to infer login state"
  fi
else
  emit WARN "oa-login" "could not infer login state"
fi

# --- Markdown queue backlog -------------------------------------------------
handoff="$(uv run oa knowledge audit-handoff --config "$CONFIG" 2>/dev/null || true)"
if [[ -n "$handoff" ]]; then
  pending="$(printf '%s' "$handoff" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("pending",0))
except Exception: print(0)' 2>/dev/null || echo 0)"
  failed="$(printf '%s' "$handoff" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("failed",0))
except Exception: print(0)' 2>/dev/null || echo 0)"
  if awk "BEGIN{exit !($pending < 200)}"; then emit OK "markdown-queue" "pending=$pending failed=$failed"; else emit WARN "markdown-queue" "pending=$pending failed=$failed (backlog)"; fi
else
  emit WARN "markdown-queue" "could not read handoff audit"
fi

# --- Feishu configuration ---------------------------------------------------
feishu_state="$(uv run oa notifications status --config "$CONFIG" 2>/dev/null | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("feishu_state"))
except Exception: print("")' 2>/dev/null || true)"
case "$feishu_state" in
  ready) emit OK "feishu" "ready" ;;
  disabled) emit OK "feishu" "disabled (notifications off)" ;;
  "") emit WARN "feishu" "could not read state" ;;
  *) emit WARN "feishu" "state=$feishu_state (misconfigured)" ;;
esac

# --- disk space -------------------------------------------------------------
data_root="$(python3 - "$CONFIG" <<'PY'
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    print(cfg.get("app", {}).get("data_root", "."))
except Exception:
    print(".")
PY
)"
free_gb="$(df -P "$data_root" 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}' || echo 0)"
if awk "BEGIN{exit !(${free_gb:-0} > 1)}"; then emit OK "disk" "${free_gb}G free on $data_root"; else emit FAIL "disk" "${free_gb}G free on $data_root"; fi

echo "----"
echo "PASS=$PASS WARN=$WARN FAIL=$FAIL"
if [[ "$FAIL" -gt 0 ]]; then exit 1; fi
exit 0
