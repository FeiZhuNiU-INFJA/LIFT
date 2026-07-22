#!/usr/bin/env bash
# OpenHuman agentmemory 变体的 ENTRYPOINT 包装脚本。
#
# 镜像 ENTRYPOINT 为 `tini -- <本脚本> <CMD>`（CMD 默认 openhuman-core run --host ... --port 7788）。
# 当 AGENTMEMORY_ENABLED=true 时：先在容器内后台拉起 agentmemory server（:3111，纯本地
# all-MiniLM-L6-v2 嵌入 + BM25，离线、零 API Key），等 :3111 就绪后再 exec openhuman-core。
#
# 为什么必须先起 server：OpenHuman 的 agentmemory backend **无自动回退 SQLite**——openhuman-core
# 启动时若 daemon 不可达，memory trait 调用会直接报错。因此本脚本保证"server 先起 + 健康等待"。
# base 镜像（AGENTMEMORY_ENABLED 非 true）时本脚本对 CMD 完全透明（直接 exec）。
set -euo pipefail

if [[ "${AGENTMEMORY_ENABLED:-false}" == "true" ]]; then
  export CI=1
  export HOME="${HOME:-/root}"
  _am_log="/workspace/task/agentmemory-server.log"
  mkdir -p /workspace/task 2>/dev/null || true
  if command -v agentmemory >/dev/null 2>&1; then
    echo "[openhuman-agentmemory-entrypoint] starting agentmemory server on :3111 ..."
    ( agentmemory >"${_am_log}" 2>&1 & ) || echo "[openhuman-agentmemory-entrypoint] WARN: failed to spawn agentmemory server" >&2
    _ready="false"
    for _i in $(seq 1 60); do
      if curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1 \
         || curl -fsS http://localhost:3111/agentmemory/health >/dev/null 2>&1; then
        _ready="true"; break
      fi
      sleep 1
    done
    if [[ "${_ready}" == "true" ]]; then
      echo "[openhuman-agentmemory-entrypoint] agentmemory server ready on :3111"
    else
      # backend 无回退，server 未就绪则 openhuman-core 的 memory 调用会失败。仍继续 exec，
      # 让 openhuman-core 自身的错误信息暴露问题，而不是在 wrapper 里静默悬挂。
      echo "[openhuman-agentmemory-entrypoint] WARN: agentmemory server not ready within timeout; continuing (OpenHuman memory ops may fail)." >&2
      cat "${_am_log}" 2>/dev/null || true
    fi
  else
    echo "[openhuman-agentmemory-entrypoint] WARN: AGENTMEMORY_ENABLED=true but 'agentmemory' not on PATH; skipping server start." >&2
  fi
fi

exec "$@"
