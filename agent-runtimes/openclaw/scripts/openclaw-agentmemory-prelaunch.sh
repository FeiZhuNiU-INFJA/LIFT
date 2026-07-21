#!/usr/bin/env bash
# OpenClaw agentmemory 变体的 gateway prelaunch 包装脚本。
#
# LIFT 的 OpenClawWithAgentMemoryAdapter 把容器 entrypoint 前置成本脚本 + 原 gateway
# 命令（openclaw gateway run --bind lan）。作用：在 gateway 启动前，先在容器内后台拉起
# 一个 agentmemory server（:3111，纯本地 all-MiniLM-L6-v2 嵌入 + BM25，离线、零 API Key），
# 等 :3111 就绪（best-effort，最多 ~30s）后 exec 原命令。
#
# 该 server 只被容器内 localhost:3111 访问（OpenClaw memory 插件 base_url）；配合 adapter
# 强制 bridge 网络，避免同一宿主并发容器抢同一宿主端口冲突。CI=1 跳过所有交互 prompt。
set -euo pipefail

export CI=1
export HOME="${HOME:-/root}"

# 后台起 agentmemory server；日志落 bind mount 便于事后诊断（/workspace/task 由 LIFT 挂载）。
_am_log="/workspace/task/agentmemory-server.log"
mkdir -p /workspace/task 2>/dev/null || true
( agentmemory >"${_am_log}" 2>&1 & ) || echo "[agentmemory-prelaunch] WARN: failed to spawn agentmemory server" >&2

# best-effort 等 :3111 就绪（引擎首次冷启可能拉二进制/模型，稍慢）。失败也不阻断——
# 插件侧 fallback_on_error=true 会在 server 未就绪时回退 OpenClaw 默认记忆，不炸主流程。
for _i in $(seq 1 30); do
  if curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1 \
     || curl -fsS http://localhost:3111/agentmemory/health >/dev/null 2>&1; then
    echo "[agentmemory-prelaunch] agentmemory server ready on :3111"
    break
  fi
  sleep 1
done

exec "$@"
