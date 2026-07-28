#!/usr/bin/env bash
# 构建期第 2 层（重量 · 缓存友好）：Node + npm + agentmemory engine/model warmup。
# 独立成脚本以便与 config.toml 渲染解耦：只有本文件、Dockerfile 中相应 ARG、或
# @agentmemory/agentmemory 上游包变化时才会 bust 这一层的 layer 缓存（约 200s）。
#
# ⚠️ 只在 INSTALL_AGENTMEMORY=true 时被 Dockerfile 调起。否则整层跳过。
set -euo pipefail

INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" != "true" ]]; then
  echo "INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY}: skip agentmemory backend (no-op)"
  exit 0
fi

NODE_MAJOR="${NODE_MAJOR:-20}"
export HOME="${HOME:-/root}"
[[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && export NPM_CONFIG_REGISTRY

echo "==> Enabling agentmemory backend (offline local embeddings)"

# 1) 装 Node >= 20（Debian bookworm-slim 无 Node；走 NodeSource apt 仓库）。
if command -v node >/dev/null 2>&1 && [[ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge 20 ]]; then
  echo "==> Node $(node -v) already >= 20"
else
  echo "==> Installing Node ${NODE_MAJOR} via NodeSource"
  apt-get update
  apt-get install -y --no-install-recommends ca-certificates curl gnupg git
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y --no-install-recommends nodejs
  rm -rf /var/lib/apt/lists/*
  echo "==> Node $(node -v), npm $(npm -v)"
fi

# 2) 装 agentmemory server + CLI。
npm install -g @agentmemory/agentmemory

# 3) 构建期预热引擎 + 本地嵌入模型，然后清空记忆状态（保留引擎/模型缓存）。
echo "==> Warming up agentmemory engine + local embedding model (build-time, networked)"
export CI=1
( agentmemory >/tmp/agentmemory-warmup.log 2>&1 & )
_am_ready="false"
for _i in $(seq 1 60); do
  if curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1 \
     || curl -fsS http://localhost:3111/agentmemory/health >/dev/null 2>&1; then
    _am_ready="true"; break
  fi
  sleep 1
done
if [[ "${_am_ready}" == "true" ]]; then
  echo "==> agentmemory warmup server ready; triggering demo to fetch engine/model, then reset"
  agentmemory demo >/tmp/agentmemory-demo.log 2>&1 || echo "WARN: agentmemory demo returned non-zero (non-fatal)" >&2
else
  echo "WARN: agentmemory warmup server not ready in time; engine/model may fetch at first runtime start." >&2
  cat /tmp/agentmemory-warmup.log 2>/dev/null || true
fi
pkill -f agentmemory 2>/dev/null || true
sleep 2
if [[ -d "${HOME}/.agentmemory" ]]; then
  find "${HOME}/.agentmemory" -maxdepth 1 -mindepth 1 \
    ! -name bin \
    ! -name models \
    ! -name model-cache \
    ! -name '.cache' \
    -exec rm -rf {} + 2>/dev/null || true
  echo "==> Reset agentmemory memory state (kept engine binary + model cache)"
fi
echo "==> agentmemory backend installed; :3111 server will be started at runtime by entrypoint wrapper"
