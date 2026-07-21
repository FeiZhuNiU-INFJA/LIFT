#!/usr/bin/env bash
# Run inside Docker build (after .deb 安装).
# 1) 渲染 config.toml.template -> /root/.openhuman/config.toml
# 2) （占位）后续如需 patch openhuman-core 二进制或额外配置，可在此追加
set -euo pipefail

OPENHUMAN_HOME="/root/.openhuman"
mkdir -p "${OPENHUMAN_HOME}"

escape_sed() {
  printf '%s' "${1:-}" | sed -e 's/[\/&]/\\&/g' -e ':a;N;$!ba;s/\n/\\n/g'
}

API_KEY_ESC="$(escape_sed "${API_KEY:-}")"
API_URL_ESC="$(escape_sed "${API_URL:-}")"
INFERENCE_URL_ESC="$(escape_sed "${INFERENCE_URL:-https://ark.cn-beijing.volces.com/api/v3}")"
DEFAULT_MODEL_ESC="$(escape_sed "${DEFAULT_MODEL:-}")"

sed \
  -e "s/__API_KEY__/${API_KEY_ESC}/g" \
  -e "s/__API_URL__/${API_URL_ESC}/g" \
  -e "s/__INFERENCE_URL__/${INFERENCE_URL_ESC}/g" \
  -e "s/__DEFAULT_MODEL__/${DEFAULT_MODEL_ESC}/g" \
  /tmp/config.toml.template > "${OPENHUMAN_HOME}/config.toml"

echo "==> Rendered ${OPENHUMAN_HOME}/config.toml:"
sed -e 's/api_key = ".*"/api_key = "<redacted>"/' "${OPENHUMAN_HOME}/config.toml"

# agentmemory backend（官方 wiki config.toml backend 切换）。默认不装（INSTALL_AGENTMEMORY=false）。
# 装的话：
#   - 在 config.toml 追加 [memory] backend=agentmemory（openhuman-core 旁路自家 SQLite，
#     把 Memory trait 调用代理到容器内 :3111 server）。
#   - 装 Node >= 20 + npm 装 @agentmemory/agentmemory；预热离线引擎/嵌入模型，然后清空
#     记忆状态（保留 bin/ 与模型缓存）。运行期 :3111 server 由 openhuman-agentmemory-entrypoint.sh
#     在 openhuman-core 启动前拉起。
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  AGENTMEMORY_GIT_URL="${AGENTMEMORY_GIT_URL:-https://github.com/rohitg00/agentmemory.git}"
  AGENTMEMORY_GIT_REF="${AGENTMEMORY_GIT_REF:-main}"
  NODE_MAJOR="${NODE_MAJOR:-20}"
  export HOME="${HOME:-/root}"
  [[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && export NPM_CONFIG_REGISTRY

  echo "==> Enabling agentmemory backend (offline local embeddings)"

  # 1) config.toml 追加 [memory] backend=agentmemory（幂等：已存在则跳过）。
  if ! grep -q '^\[memory\]' "${OPENHUMAN_HOME}/config.toml" 2>/dev/null; then
    {
      echo ""
      echo "[memory]"
      echo 'backend = "agentmemory"'
      echo 'agentmemory_url = "http://localhost:3111"'
    } >> "${OPENHUMAN_HOME}/config.toml"
    echo "==> Appended [memory] backend=agentmemory to config.toml"
  else
    echo "==> [memory] block already present in config.toml; skip append"
  fi

  # 2) 装 Node >= 20（Debian bookworm-slim 无 Node；走 NodeSource apt 仓库）。
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

  # 3) 装 agentmemory server + CLI。
  npm install -g @agentmemory/agentmemory

  # 4) 构建期预热引擎 + 本地嵌入模型，然后清空记忆状态（保留引擎/模型缓存）。
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
  echo "==> agentmemory backend enabled; :3111 server started at runtime by entrypoint wrapper"
else
  echo "INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY}: skip agentmemory backend"
fi

# Sanity check：确认 openhuman-core 可执行。--help 不同版本行为不一致，容忍失败。
if command -v openhuman-core >/dev/null 2>&1; then
  openhuman-core --version 2>/dev/null \
    || openhuman-core help >/dev/null 2>&1 \
    || true
else
  echo "WARN: openhuman-core binary not found on PATH; check .deb layout" >&2
fi

echo "OpenHuman baked; config at ${OPENHUMAN_HOME}/config.toml"
