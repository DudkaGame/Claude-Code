#!/usr/bin/env bash
# Aeroflot tickets bot — one-shot installer for Ubuntu 22.04 / Debian 12.
# Run as root (or via sudo) on a fresh VPS.
#
# Usage:
#   sudo bash install.sh /path/to/source-dir
# where source-dir is the directory containing check_tickets.py, config.json,
# and the deploy/ folder. If omitted, defaults to the parent of this script.

set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
APP_DIR=/opt/aeroflot-bot
LOG_DIR=/var/log/aeroflot-bot
SERVICE_USER=aeroflot

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo bash install.sh)" >&2
    exit 1
fi

if [[ ! -f "$SRC/check_tickets.py" ]]; then
    echo "check_tickets.py not found in $SRC" >&2
    exit 1
fi

if [[ ! -f "$SRC/config.json" ]]; then
    echo "config.json not found in $SRC — copy config.example.json and fill in the Telegram token/chat_id first" >&2
    exit 1
fi

echo "==> installing OS packages"
apt-get update
apt-get install -y python3 python3-venv python3-pip rsync ca-certificates

echo "==> creating service user $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$SERVICE_USER"
fi

echo "==> creating directories"
mkdir -p "$APP_DIR" "$LOG_DIR"
rsync -a --delete --exclude=.venv --exclude=logs --exclude=state.json \
    "$SRC"/ "$APP_DIR"/
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$LOG_DIR"
chmod 600 "$APP_DIR/config.json"

echo "==> creating Python venv"
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install playwright requests

echo "==> installing chromium + OS deps for Playwright"
"$APP_DIR/.venv/bin/playwright" install-deps chromium
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/playwright" install chromium

echo "==> installing systemd units"
install -m 644 "$APP_DIR/deploy/aeroflot-check.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/aeroflot-check.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aeroflot-check.timer

echo "==> done. Run these to verify:"
echo "    sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/check_tickets.py --test-notify"
echo "    sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/check_tickets.py --dry-run"
echo "    systemctl list-timers aeroflot-check.timer"
echo "    tail -f $LOG_DIR/run.log"
