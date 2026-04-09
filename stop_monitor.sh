#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/logs/monitor.pid"

if [[ ! -f "$PID_FILE" ]]; then
	echo "No PID file found. Monitor may already be stopped."
	exit 0
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${PID:-}" ]]; then
	rm -f "$PID_FILE"
	echo "PID file was empty. Cleaned it up."
	exit 0
fi

if kill -0 "$PID" 2>/dev/null; then
	kill "$PID"
	echo "Stopped monitor PID $PID"
else
	echo "Process $PID was not running."
fi

rm -f "$PID_FILE"
