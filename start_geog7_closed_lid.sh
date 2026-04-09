#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"
LOG_FILE="$SCRIPT_DIR/logs/alert.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting GEOG 7 monitor in foreground..." | tee -a "$LOG_FILE"

exec python3 bruin_alert.py --config config.json 2>&1 | tee -a "$LOG_FILE"
