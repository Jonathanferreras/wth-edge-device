#!/usr/bin/env bash
# Remove the wth-edge services. Config (/etc/wth) and data (/var/lib/wth) stay unless --purge.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run as root: sudo $0 $*" >&2; exit 1; }

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

for unit in wth-update.timer wth-update.service wth-display.service wth-edge.service; do
  if [[ -f "/etc/systemd/system/$unit" ]]; then
    systemctl disable --now "$unit" || true
    rm -f "/etc/systemd/system/$unit"
    echo "removed $unit"
  fi
done
systemctl daemon-reload
systemctl reset-failed || true
rm -f /usr/local/bin/wth-edge

if [[ $PURGE -eq 1 ]]; then
  rm -rf /etc/wth /var/lib/wth /run/wth
  userdel wth 2>/dev/null || true
  echo "purged config, data and the wth user"
else
  echo "kept /etc/wth and /var/lib/wth (use --purge to delete them)"
fi
