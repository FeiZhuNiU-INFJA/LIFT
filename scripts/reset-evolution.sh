#!/bin/bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <run_id> <category> <repeat_index>" >&2
    exit 1
fi

RUN_ID="$1"
CATEGORY="$2"
REPEAT_INDEX="$3"

STATE_DIR="$HOME/.openclaw/evolution-runtime"
BACKUP_ROOT="$HOME/.openclaw/evo-db-backup"
BACKUP_DIR="$BACKUP_ROOT/$RUN_ID/run-$REPEAT_INDEX/$CATEGORY"
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

echo "🧹 正在重置 Self-Evolving Plugin Pro 进化数据..."
echo "📦 备份目录: $BACKUP_DIR"

PORT_PID="$(extract_port_pid || true)"
if [ -n "$PORT_PID" ]; then
    echo "🛑 发现 18090 端口进程 PID: $PORT_PID，正在停止..."
    kill "$PORT_PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$PORT_PID" 2>/dev/null; then
        echo "⚠️ PID $PORT_PID 仍在运行，尝试强制结束..."
        kill -9 "$PORT_PID" 2>/dev/null || true
    fi
else
    echo "ℹ️ 未找到 18090 端口关联进程"
fi

mkdir -p "$BACKUP_DIR"

if [ -f "$DB_FILE" ]; then
    cp "$DB_FILE" "$BACKUP_DIR/evolution-pro.db"
    echo "💾 已备份数据库到: $BACKUP_DIR/evolution-pro.db"
else
    echo "ℹ️ 数据库不存在，跳过数据库备份"
fi

if [ -f "$RUNTIME_STATE_FILE" ]; then
    cp "$RUNTIME_STATE_FILE" "$BACKUP_DIR/runtime-state.json"
    echo "💾 已备份运行时状态到: $BACKUP_DIR/runtime-state.json"
else
    echo "ℹ️ runtime-state.json 不存在，跳过备份"
fi

if [ -f "$RUNTIME_READY_FILE" ]; then
    cp "$RUNTIME_READY_FILE" "$BACKUP_DIR/runtime-ready.json"
    echo "💾 已备份运行时就绪文件到: $BACKUP_DIR/runtime-ready.json"
else
    echo "ℹ️ runtime-ready.json 不存在，跳过备份"
fi

rm -f "$DB_FILE"
rm -f "$RUNTIME_STATE_FILE"
rm -f "$RUNTIME_READY_FILE"

echo "🗑️ 已删除旧数据库和运行时状态文件"

cd "$PLUGIN_DIR"
./scripts/setup-runtime.sh >/dev/null 2>&1

echo "🔧 已重建数据库和运行时"

sleep 5
