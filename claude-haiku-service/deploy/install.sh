#!/bin/bash
# Usage: sudo ./install.sh
# Pre-requisite: run `claude` once interactively to OAuth-login and create a starting session
#                BEFORE the first cron run (otherwise `claude --continue` has nothing to continue).
set -euo pipefail

INSTALL_DIR="/opt/claude-ping"
LOG_DIR="/var/log/claude-ping"
CRON_LINE="0 */5 * * * $INSTALL_DIR/ping.sh"

echo "==> Installing Node.js and claude CLI..."
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt-get install -y nodejs
fi
npm install -g @anthropic-ai/claude-code 2>&1 | tail -3

CLAUDE_BIN=$(which claude)
echo "==> claude installed at: $CLAUDE_BIN"

echo "==> Ensuring cron is installed..."
if ! command -v crontab &>/dev/null; then
    apt-get install -y cron
fi
systemctl enable --now cron 2>/dev/null || service cron start || true

echo "==> Deploying ping script to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cat > "$INSTALL_DIR/ping.sh" <<EOF
#!/bin/bash
set -euo pipefail
LOG_DIR="$LOG_DIR"
LOG_FILE="\$LOG_DIR/run.log"
mkdir -p "\$LOG_DIR"
TIMESTAMP=\$(date '+%Y-%m-%d %H:%M:%S')
echo "[\$TIMESTAMP] --- ping ---" >> "\$LOG_FILE"
if ! $CLAUDE_BIN --continue --model claude-haiku-4-5 --print "." >> "\$LOG_FILE" 2>&1; then
    echo "[\$TIMESTAMP] ERROR: claude --continue failed. Run 'claude' interactively once to create a starting session." >> "\$LOG_FILE"
    exit 1
fi
echo "[\$TIMESTAMP] --- done ---" >> "\$LOG_FILE"
EOF
chmod 755 "$INSTALL_DIR/ping.sh"

echo "==> Creating log directory..."
mkdir -p "$LOG_DIR"

echo "==> Removing any previous systemd timer (migrating to cron)..."
if systemctl list-unit-files | grep -q '^claude-ping\.'; then
    systemctl disable --now claude-ping.timer 2>/dev/null || true
    rm -f /etc/systemd/system/claude-ping.service /etc/systemd/system/claude-ping.timer
    systemctl daemon-reload
fi

echo "==> Installing cron job (every 5 hours)..."
( crontab -l 2>/dev/null | grep -v -F "$INSTALL_DIR/ping.sh" ; echo "$CRON_LINE" ) | crontab -
echo "Current crontab:"
crontab -l

echo ""
echo "==> Done."
echo ""
echo "NEXT STEPS (run as the user who owns the Claude OAuth session, usually root):"
echo "  1. claude                            # OAuth login in browser + send any first message to create the session"
echo "  2. $INSTALL_DIR/ping.sh              # manual test"
echo "  3. tail $LOG_DIR/run.log             # verify the haiku reply was logged"
echo ""
echo "Cron will fire next at: $(date -d 'now' '+%H:%M')  →  every 5h on the hour (0 */5 * * *)."
