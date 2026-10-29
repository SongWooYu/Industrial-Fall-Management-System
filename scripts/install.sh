#!/bin/sh
# Existing Pi5 + compatible Hailo SDK prerequisite; never upgrades drivers.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
EXPECTED=/home/pi/Industrial-Fall-Management-System
[ "$ROOT" = "$EXPECTED" ] || { echo "Install this Pi5 deployment at $EXPECTED" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo after completing README setup" >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "Create .venv first (README)" >&2; exit 1; }
runuser -u pi -- "$ROOT/.venv/bin/python" "$ROOT/scripts/check_runtime.py"
for unit in smarthome-vision.service smarthome-mediamtx.service; do
  if systemctl is-active --quiet "$unit"; then
    echo "Stop $unit during the approved migration window first." >&2
    exit 1
  fi
done
install -m 644 "$ROOT/systemd/industrial-fall.service" /etc/systemd/system/industrial-fall.service
install -m 644 "$ROOT/systemd/industrial-fall-media.service" /etc/systemd/system/industrial-fall-media.service
systemctl daemon-reload
systemctl enable --now industrial-fall-media.service industrial-fall.service
