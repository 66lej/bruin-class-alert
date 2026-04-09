#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"
LOG_FILE="$SCRIPT_DIR/logs/alert.log"
PID_FILE="$SCRIPT_DIR/logs/monitor.pid"

if [[ -f "$PID_FILE" ]]; then
	PID="$(cat "$PID_FILE" 2>/dev/null || true)"
	if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
		echo "Monitor is already running with PID $PID"
		exit 0
	fi
	rm -f "$PID_FILE"
fi

nohup zsh -lc "
	cd '$SCRIPT_DIR'
	echo \"[\$(date '+%Y-%m-%d %H:%M:%S')] Starting GEOG 7 monitor in background...\" >> '$LOG_FILE'
	exec python3 bruin_alert.py --config config.json >> '$LOG_FILE' 2>&1
" >/dev/null 2>&1 &

echo $! > "$PID_FILE"
echo "Started GEOG 7 monitor in background. PID $(cat "$PID_FILE")"
echo "Log: $LOG_FILE"
