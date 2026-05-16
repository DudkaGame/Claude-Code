#!/bin/bash
# Usage: sudo ./install.sh <ANTHROPIC_API_KEY>
set -euo pipefail

API_KEY="${1:-}"
if [[ -z "$API_KEY" ]]; then
    echo "Usage: sudo $0 <ANTHROPIC_API_KEY>" >&2
    exit 1
fi

INSTALL_DIR="/opt/claude-ping"
ENV_DIR="/etc/claude-ping"
LOG_DIR="/var/log/claude-ping"
SERVICE_USER="claude-ping"

echo "==> Creating system user '$SERVICE_USER'..."
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Installing Node.js and claude CLI..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi
npm install -g @anthropic-ai/claude-code 2>&1 | tail -3

echo "==> Deploying ping script..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/../ping.sh" "$INSTALL_DIR/ping.sh"
chmod 755 "$INSTALL_DIR/ping.sh"
chown root:root "$INSTALL_DIR/ping.sh"

echo "==> Writing API key to $ENV_DIR/env..."
mkdir -p "$ENV_DIR"
cat > "$ENV_DIR/env" <<EOF
ANTHROPIC_API_KEY=$API_KEY
HOME=/tmp
EOF
chmod 600 "$ENV_DIR/env"
chown root:root "$ENV_DIR/env"

echo "==> Creating log directory..."
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$LOG_DIR"

echo "==> Installing systemd units..."
cp "$(dirname "$0")/claude-ping.service" /etc/systemd/system/
cp "$(dirname "$0")/claude-ping.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-ping.timer

echo ""
echo "Done! Timer status:"
systemctl status claude-ping.timer --no-pager
echo ""
echo "Next run: $(systemctl show claude-ping.timer -p NextElapseUSecRealtime --value | xargs -I{} date -d @$(echo {} | cut -d= -f2 2>/dev/null) 2>/dev/null || echo 'see: systemctl list-timers')"
echo "Logs: tail -f $LOG_DIR/run.log"
