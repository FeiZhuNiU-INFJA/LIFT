#!/bin/bash
set -euo pipefail

STATE_DIR="$HOME/.openclaw/evolution-runtime"
DB_FILE="$STATE_DIR/evolution-pro.db"
RUNTIME_STATE_FILE="$STATE_DIR/runtime-state.json"
RUNTIME_READY_FILE="$STATE_DIR/runtime-ready.json"
PLUGIN_DIR="$HOME/.openclaw/extensions/self-evolving-plugin-pro"

extract_port_pid() {
    local line
    line="$(ss -nltp 2>/dev/null | awk '$4 ~ /:18090$/ { print; exit }')"
    if [ -z "$line" ]; then
        return 1
    fi
    printf '%s\n' "$line" | sed -n 's/.*pid=\([0-9]\+\).*/\1/p'
}

echo "Resetting evolution runtime without backup..."

PORT_PID="$(extract_port_pid || true)"
if [ -n "$PORT_PID" ]; then
    echo "Stopping port 18090 process PID: $PORT_PID"
    kill "$PORT_PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$PORT_PID" 2>/dev/null; then
        echo "PID $PORT_PID is still running, forcing kill..."
        kill -9 "$PORT_PID" 2>/dev/null || true
    fi
else
    echo "No process found on port 18090"
fi

rm -f "$DB_FILE"
rm -f "$RUNTIME_STATE_FILE"
rm -f "$RUNTIME_READY_FILE"

echo "Deleted previous evolution runtime state"

cd "$PLUGIN_DIR"
./scripts/setup-runtime.sh >/dev/null 2>&1

echo "Rebuilt evolution runtime"

sleep 5
