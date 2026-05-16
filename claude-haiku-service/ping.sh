#!/bin/bash
set -euo pipefail

LOG_DIR="/var/log/claude-ping"
LOG_FILE="$LOG_DIR/run.log"
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] --- ping ---" >> "$LOG_FILE"
echo "." | claude --model claude-haiku-4-5 --print >> "$LOG_FILE" 2>&1
echo "[$TIMESTAMP] --- done ---" >> "$LOG_FILE"
