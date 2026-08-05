#!/usr/bin/env bash
#
# Remove the OARadar user-level systemd units installed by
# install-systemd-user.sh (plan-0805-02 §5). The env file holding secrets is
# left in place on purpose; only the generated units/timers are disabled.

set -euo pipefail

SYSTEMD_DIR="$HOME/.config/systemd/user"

SYSTEMD=(systemctl --user)

"${SYSTEMD[@]}" disable --now \
  oaradar-worker.service \
  oaradar-markdown-worker.service \
  oaradar-hourly.timer \
  oaradar-nightly.timer 2>/dev/null || true

for unit in \
  oaradar-worker.service \
  oaradar-markdown-worker.service \
  oaradar-hourly.service \
  oaradar-hourly.timer \
  oaradar-nightly.service \
  oaradar-nightly.timer; do
  if [[ -f "$SYSTEMD_DIR/$unit" ]]; then
    rm -f "$SYSTEMD_DIR/$unit"
    echo "removed $SYSTEMD_DIR/$unit"
  fi
done

"${SYSTEMD[@]}" daemon-reload

echo "OARadar systemd units removed. Secrets in ~/.config/oaradar/env were left untouched."
