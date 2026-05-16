#!/bin/bash
# Usage: sudo ./install.sh
# Requires: claude auth login to be run once before this script
set -euo pipefail

INSTALL_DIR="/opt/claude-ping"
LOG_DIR="/var/log/claude-ping"

echo "==> Installing Node.js and claude CLI..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi
npm install -g @anthropic-ai/claude-code 2>&1 | tail -3

CLAUDE_BIN=$(which claude)
echo "==> claude installed at: $CLAUDE_BIN"

echo "==> Deploying ping script to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cat > "$INSTALL_DIR/ping.sh" <<EOF
#!/bin/bash
set -euo pipefail
LOG_FILE="$LOG_DIR/run.log"
mkdir -p "$LOG_DIR"
TIMESTAMP=\$(date '+%Y-%m-%d %H:%M:%S')
echo "[\$TIMESTAMP] --- ping ---" >> "\$LOG_FILE"
echo "." | $CLAUDE_BIN --model claude-haiku-4-5 --print >> "\$LOG_FILE" 2>&1
echo "[\$TIMESTAMP] --- done ---" >> "\$LOG_FILE"
EOF
chmod 755 "$INSTALL_DIR/ping.sh"

echo "==> Creating log directory..."
mkdir -p "$LOG_DIR"

echo "==> Installing systemd units..."
cat > /etc/systemd/system/claude-ping.service <<'EOF'
[Unit]
Description=Claude Haiku Keepalive Ping
After=network.target

[Service]
Type=oneshot
User=root
ExecStart=/opt/claude-ping/ping.sh
StandardOutput=append:/var/log/claude-ping/run.log
StandardError=append:/var/log/claude-ping/run.log
EOF

cat > /etc/systemd/system/claude-ping.timer <<'EOF'
[Unit]
Description=Claude Haiku Ping every 5 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=5h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now claude-ping.timer

echo ""
echo "==> Testing one ping..."
bash "$INSTALL_DIR/ping.sh" && echo "==> OK" || echo "==> FAILED — run 'claude auth login' first"

echo ""
echo "Done! Timer status:"
systemctl status claude-ping.timer --no-pager
echo ""
echo "Logs: tail -f $LOG_DIR/run.log"
