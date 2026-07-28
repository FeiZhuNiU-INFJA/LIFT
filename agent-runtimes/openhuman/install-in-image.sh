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

# LIFT max_tokens 代理:openhuman-core Rust binary 不暴露任何 max_tokens 覆盖入口,
# 我们在容器 127.0.0.1:${LIFT_PROXY_PORT} 起一个透明反向代理,注入 MAX_TOKENS 后
# 再转发到真实上游 (INFERENCE_URL)。config.toml 的 inference_url 改指向 proxy。
# 通过 LIFT_MAX_TOKENS_PROXY_ENABLED=false 关闭代理(此时 inference_url 直连 upstream)。
LIFT_MAX_TOKENS_PROXY_ENABLED="${LIFT_MAX_TOKENS_PROXY_ENABLED:-true}"
LIFT_PROXY_PORT="${LIFT_PROXY_PORT:-7787}"
UPSTREAM_INFERENCE_URL="${INFERENCE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
if [[ "${LIFT_MAX_TOKENS_PROXY_ENABLED}" == "true" || "${LIFT_MAX_TOKENS_PROXY_ENABLED}" == "1" ]]; then
  # inference_url 前面已保留 /v3 前缀作为代理入口约定(proxy 会 strip 后拼到 UPSTREAM)。
  EFFECTIVE_INFERENCE_URL="http://127.0.0.1:${LIFT_PROXY_PORT}/v3"
  echo "==> max_tokens proxy enabled; inference_url=${EFFECTIVE_INFERENCE_URL}, upstream=${UPSTREAM_INFERENCE_URL}"
else
  EFFECTIVE_INFERENCE_URL="${UPSTREAM_INFERENCE_URL}"
  echo "==> max_tokens proxy disabled; inference_url directly points to upstream ${EFFECTIVE_INFERENCE_URL}"
fi

API_KEY_ESC="$(escape_sed "${API_KEY:-}")"
API_URL_ESC="$(escape_sed "${API_URL:-}")"
INFERENCE_URL_ESC="$(escape_sed "${EFFECTIVE_INFERENCE_URL}")"
DEFAULT_MODEL_ESC="$(escape_sed "${DEFAULT_MODEL:-}")"

sed \
  -e "s/__API_KEY__/${API_KEY_ESC}/g" \
  -e "s/__API_URL__/${API_URL_ESC}/g" \
  -e "s/__INFERENCE_URL__/${INFERENCE_URL_ESC}/g" \
  -e "s/__DEFAULT_MODEL__/${DEFAULT_MODEL_ESC}/g" \
  /tmp/config.toml.template > "${OPENHUMAN_HOME}/config.toml"

echo "==> Rendered ${OPENHUMAN_HOME}/config.toml:"
sed -e 's/api_key = ".*"/api_key = "<redacted>"/' "${OPENHUMAN_HOME}/config.toml"

# ── sandbox 边界放行 /workspace/task（关键修复）────────────────────────────
# openhuman-core 除 acting-tool sandbox 的 action_dir 外还有第二层 autonomy 边界：
#   workspace_only=true + forbidden_paths 含 /root/tmp/opt → 即便 OPENHUMAN_ACTION_DIR
#   注入到 /workspace/task，agent 访问 /workspace/task/qN_materials/xxx 仍会被判
#   "Resolved path escapes workspace"。真实边界由 ~/.openhuman/users/local/config.toml
#   的 [autonomy] 段控制，字段与 CLI ``config update_autonomy_settings`` 一一对应。
#
# 直接在 build 期预烘焙 users/local/config.toml，避免运行期每容器 exec 一次的竞态。
# workspace_only=false + trusted_roots=/workspace/task(rw) 打开 LIFT 挂载点；
# forbidden_paths 只保留凭据目录，不再涵盖 /root/tmp/opt（否则会把 agentmemory
# 的 /root/.agentmemory 一起拦掉）。
OPENHUMAN_USER_HOME="${OPENHUMAN_HOME}/users/local"
mkdir -p "${OPENHUMAN_USER_HOME}"
cat > "${OPENHUMAN_USER_HOME}/config.toml" <<'AUTONOMY_EOF'
[autonomy]
workspace_only = false
forbidden_paths = ["/etc", "/root/.ssh", "/root/.gnupg", "/root/.aws", "/root/.config"]

[[autonomy.trusted_roots]]
path = "/workspace/task"
access = "readwrite"

# LIFT 评测场景已预付费到上游 provider，openhuman-core 内建的 daily_limit_usd=10
# 会在长上下文任务里累积到上限后拒 chat（"cost budget exceeded"）。评测无成本内约，
# 直接关掉 cost tracking；无 env 覆盖，只能落 config.toml。
[cost]
enabled = false
AUTONOMY_EOF
echo "==> Wrote ${OPENHUMAN_USER_HOME}/config.toml (autonomy: workspace_only=false, trusted_roots=/workspace/task rw; cost.enabled=false)"

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
