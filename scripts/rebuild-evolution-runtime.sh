#!/bin/bash
# 重建 Self-Evolving Plugin Pro 的本地进化运行时（无备份）。
#
# 用途：
#   - 评测环境初始化时由 OpenClawAgent.initialize_environment() 调用
#   - 与 scripts/reset-evolution.sh 逻辑类似，但不备份 DB/状态文件
#
# 流程概要：
#   1. 停止监听 18090 端口的进化运行时进程
#   2. 删除 ~/.openclaw/evolution-runtime 下的数据库与状态文件
#   3. 在插件目录执行 setup-runtime.sh 重新初始化
#   4. 等待 5 秒，给新运行时留出启动时间
#
# 依赖：
#   - OpenClaw 插件已安装于 ~/.openclaw/extensions/self-evolving-plugin-pro
#   - ss（iproute2）用于查找占用 18090 的进程

set -euo pipefail

# OpenClaw 进化运行时状态目录（SQLite DB + JSON 状态）
STATE_DIR="$HOME/.openclaw/evolution-runtime"
DB_FILE="$STATE_DIR/evolution-pro.db"
RUNTIME_STATE_FILE="$STATE_DIR/runtime-state.json"
RUNTIME_READY_FILE="$STATE_DIR/runtime-ready.json"

# Self-Evolving Plugin Pro 安装路径（内含 scripts/setup-runtime.sh）
PLUGIN_DIR="$HOME/.openclaw/extensions/self-evolving-plugin-pro"

# 从 ss 输出中解析监听 18090 端口的进程 PID。
# 18090 为进化插件本地 HTTP 服务端口（评测/工具会访问 http://127.0.0.1:18090）。
# 无监听进程时返回非 0，调用方需容忍失败。
extract_port_pid() {
    local line
    line="$(ss -nltp 2>/dev/null | awk '$4 ~ /:18090$/ { print; exit }')"
    if [ -z "$line" ]; then
        return 1
    fi
    printf '%s\n' "$line" | sed -n 's/.*pid=\([0-9]\+\).*/\1/p'
}

echo "Resetting evolution runtime without backup..."

# --- 1. 停止旧运行时，避免删除 DB 时文件仍被占用 ---
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

# --- 2. 清除旧状态（不备份；评测跑次间重置请用 reset-evolution.sh）---
rm -f "$DB_FILE"
rm -f "$RUNTIME_STATE_FILE"
rm -f "$RUNTIME_READY_FILE"

echo "Deleted previous evolution runtime state"

# --- 3. 调用插件脚本重建 DB 与 runtime-ready 等初始文件 ---
cd "$PLUGIN_DIR"
./scripts/setup-runtime.sh >/dev/null 2>&1

echo "Rebuilt evolution runtime"

# --- 4. 等待新运行时就绪（setup 可能异步拉起 18090 服务）---
sleep 5
