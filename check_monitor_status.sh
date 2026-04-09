#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "== Python Monitor =="
pgrep -af "python3 .*bruin_alert.py --config config.json" || echo "Not running"

echo
echo "== PID File =="
if [[ -f "$SCRIPT_DIR/logs/monitor.pid" ]]; then
	cat "$SCRIPT_DIR/logs/monitor.pid"
else
	echo "No PID file"
fi

echo
echo "== Recent Log =="
if [[ -f "$SCRIPT_DIR/logs/alert.log" ]]; then
	tail -n 20 "$SCRIPT_DIR/logs/alert.log"
else
	echo "No log file yet"
fi
