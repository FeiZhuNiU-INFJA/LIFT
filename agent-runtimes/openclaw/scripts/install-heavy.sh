#!/usr/bin/env bash
# Run inside Docker build - L2 重量层：只做插件的 **二进制/依赖安装** 与预热。
#
# 排除 config 渲染 / fragment merge / mcp set 等秒级轻活（那些放 install-config.sh /
# install-plugins-config.sh 里，避免每次改 MODEL_NAME/WORK_OPENAI_API_KEY 都重跑几分
# 钟的 npm/pip/warmup）。
#
# 依赖:
#   - self-evolving-plugin-pro-*.zip 已 COPY 到 /tmp/self-evolving-plugin-pro.zip
#   - INSTALL_SELF_EVOLVING / INSTALL_OPENSPACE / INSTALL_AGENTMEMORY 三选一（Dockerfile
#     互斥守卫在前面已做）。
set -euo pipefail

export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/root/.openclaw}"
export HOME="${HOME:-/root}"
mkdir -p "${OPENCLAW_STATE_DIR}/extensions"

INSTALL_SELF_EVOLVING="${INSTALL_SELF_EVOLVING:-true}"
INSTALL_OPENSPACE="${INSTALL_OPENSPACE:-false}"
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"

# 1) Langfuse tracer (repo copy) — 静态资源拷贝，轻量但只有一个动作，放这里避免再多拆一层。
cp -r /tmp/langfuse-tracer "${OPENCLAW_STATE_DIR}/extensions/langfuse-tracer"

# 2) Self-evolving plugin (official install script + runtime venv)
# 注意：repo_root 不能落在 /tmp，因为 LIFT 启容器时会 ``-v /tmp:/tmp`` 把宿主机
# /tmp 整个挂进来屏蔽镜像里的 /tmp/self-evolving-plugin-pro。
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  mkdir -p /opt
  cd /opt
  unzip -q /tmp/self-evolving-plugin-pro.zip
  cd /opt/self-evolving-plugin-pro
  bash scripts/install-openclaw-plugin.sh
else
  echo "INSTALL_SELF_EVOLVING=${INSTALL_SELF_EVOLVING}: skip self-evolving-plugin-pro install (raw image)"
fi

# 3) Firecrawl external plugin（npm 装，2026.6.10+ 必装才有 web_search provider）。
openclaw plugins install @openclaw/firecrawl-plugin

# 4) OpenSpace（quality-first skill hub）依赖:git clone + Python 3.12 venv + pip install。
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  OPENSPACE_GIT_URL="${OPENSPACE_GIT_URL:-https://github.com/HKUDS/OpenSpace.git}"
  OPENSPACE_GIT_REF="${OPENSPACE_GIT_REF:-main}"
  OPENSPACE_REPO="/opt/OpenSpace"
  OPENSPACE_VENV="/opt/openspace-venv"

  echo "==> Installing OpenSpace from ${OPENSPACE_GIT_URL}@${OPENSPACE_GIT_REF}"
  git clone --filter=blob:none --sparse "${OPENSPACE_GIT_URL}" "${OPENSPACE_REPO}"
  git -C "${OPENSPACE_REPO}" sparse-checkout set --no-cone '/*' '!/assets/'
  git -C "${OPENSPACE_REPO}" checkout "${OPENSPACE_GIT_REF}" || true

  # 确保 uv 可用：OpenClaw 镜像里的 uv 由 self-evolving 插件安装脚本带进来
  # （PATH 中的 /root/.openclaw/uv-bin）。只传 --with-openspace 时该步被跳过，uv 不存在，
  # 故这里缺失就用 pip3 补装（base 镜像已装 python3-pip，PIP_BREAK_SYSTEM_PACKAGES=1）。
  if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv not on PATH; installing uv via pip3"
    pip3 install --no-cache-dir uv || python3 -m pip install --no-cache-dir uv
  fi

  uv venv --python 3.12 "${OPENSPACE_VENV}"
  uv pip install --python "${OPENSPACE_VENV}/bin/python" -e "${OPENSPACE_REPO}"

  # 暴露 CLI 到 PATH（openspace-mcp 为 MCP server 入口；其余 cloud CLI 可选）。
  ln -sf "${OPENSPACE_VENV}/bin/openspace-mcp" /usr/local/bin/openspace-mcp
  for extra in openspace-cloud-auth openspace-download-skill openspace-upload-skill; do
    [[ -x "${OPENSPACE_VENV}/bin/${extra}" ]] && ln -sf "${OPENSPACE_VENV}/bin/${extra}" "/usr/local/bin/${extra}" || true
  done

  echo "==> Verifying openspace-mcp:"
  openspace-mcp --help >/dev/null 2>&1 \
    && echo "OK: openspace-mcp importable" \
    || echo "WARN: 'openspace-mcp --help' failed; check OpenSpace install." >&2
fi

# 5) agentmemory plugin 依赖:Node>=20 + npm install + engine warmup + git clone。
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  AGENTMEMORY_GIT_URL="${AGENTMEMORY_GIT_URL:-https://github.com/rohitg00/agentmemory.git}"
  AGENTMEMORY_GIT_REF="${AGENTMEMORY_GIT_REF:-main}"
  AGENTMEMORY_SRC="/opt/agentmemory-src"
  [[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && export NPM_CONFIG_REGISTRY

  echo "==> Installing agentmemory memory plugin (offline local embeddings)"

  if ! command -v node >/dev/null 2>&1; then
    echo "ERROR: node not found on PATH; agentmemory requires Node.js >= 20." >&2
    exit 1
  fi
  NODE_MAJOR_VER="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [[ "${NODE_MAJOR_VER}" -lt 20 ]]; then
    echo "ERROR: Node.js >= 20 required for agentmemory; got $(node -v 2>/dev/null)." >&2
    exit 1
  fi
  echo "==> Node $(node -v) OK (>= 20)"

  npm install -g @agentmemory/agentmemory

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

  # 落插件：git clone，sparse 仅取 integrations/openclaw，拷进 extensions/agentmemory。
  git clone --filter=blob:none --sparse "${AGENTMEMORY_GIT_URL}" "${AGENTMEMORY_SRC}"
  git -C "${AGENTMEMORY_SRC}" sparse-checkout set --no-cone '/integrations/openclaw'
  git -C "${AGENTMEMORY_SRC}" checkout "${AGENTMEMORY_GIT_REF}" || true
  AM_PLUGIN_SRC="${AGENTMEMORY_SRC}/integrations/openclaw"
  if [[ ! -d "${AM_PLUGIN_SRC}" ]]; then
    echo "ERROR: agentmemory integrations/openclaw not found after clone (${AM_PLUGIN_SRC})." >&2
    exit 1
  fi
  rm -rf "${OPENCLAW_STATE_DIR}/extensions/agentmemory"
  cp -r "${AM_PLUGIN_SRC}" "${OPENCLAW_STATE_DIR}/extensions/agentmemory"
  for f in package.json openclaw.plugin.json plugin.mjs; do
    if [[ ! -f "${OPENCLAW_STATE_DIR}/extensions/agentmemory/${f}" ]]; then
      echo "WARN: agentmemory plugin missing ${f}; plugin may fail to load." >&2
    fi
  done

  # agentmemory MCP server（README「Option 1: MCP server」，与 Option 2 plugin 叠加）。
  echo "==> Installing agentmemory MCP server (@agentmemory/mcp, stdio)"
  npm install -g @agentmemory/mcp
fi

echo "==> L2 heavy install completed."
