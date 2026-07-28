#!/usr/bin/env bash
# Run inside Docker build - L2 重量层：仅做 pip / nvm / npm / git clone / warmup 等**分钟级**
# 依赖安装。改配置类文件（langfuse-hermes/、patch_hermes_config.py、hermes-bootstrap.sh、
# hermes-entrypoint.sh、hermes_runner.py、install-config.sh）不会 bust 本层。
#
# 路径发现在此完成并 dump 到 /opt/lift/hermes-paths.env，L4 install-config.sh 直接 source。
# 依据 .trae/documents/hermes_runtime_integration_plan.md §A.3。
set -euo pipefail

OUT_ENV="/opt/lift/hermes-paths.env"
mkdir -p /opt/lift

log() { echo "[hermes-install-heavy] $*"; }

# ---------------------------------------------------------------------------
# 1) Discover Hermes venv python
# ---------------------------------------------------------------------------
HERMES_VENV_PY=""
for cand in \
    /opt/hermes/.venv/bin/python \
    /opt/hermes/venv/bin/python \
    /opt/hermes-agent/.venv/bin/python \
    /opt/hermes-agent/venv/bin/python \
    /root/.hermes/hermes-agent/venv/bin/python ; do
  if [[ -x "$cand" ]]; then HERMES_VENV_PY="$cand"; break; fi
done
if [[ -z "$HERMES_VENV_PY" ]]; then
  if command -v hermes >/dev/null 2>&1; then
    shebang="$(head -n1 "$(command -v hermes)" 2>/dev/null | sed 's/^#!//; s/[[:space:]].*$//')"
    if [[ -x "$shebang" ]]; then HERMES_VENV_PY="$shebang"; fi
  fi
fi
if [[ -z "$HERMES_VENV_PY" ]]; then
  HERMES_VENV_PY="$(find /opt /root -maxdepth 6 -type f -path '*/bin/python' 2>/dev/null \
      | grep -Ei 'venv|\.venv' | grep -i hermes | head -n1 || true)"
fi
if [[ -z "$HERMES_VENV_PY" ]]; then
  log "ERROR: could not locate Hermes venv python. Inspect the base image layout." >&2
  exit 1
fi
log "Hermes venv python: $HERMES_VENV_PY"

# ---------------------------------------------------------------------------
# 2) Discover Hermes source dir (importable run_agent / agent package)
# ---------------------------------------------------------------------------
HERMES_SRC_DIR=""
for cand in \
    /opt/hermes \
    /opt/hermes-agent \
    /opt/hermes/hermes-agent \
    /root/.hermes/hermes-agent ; do
  if [[ -f "$cand/run_agent.py" ]] || [[ -d "$cand/agent" ]]; then
    HERMES_SRC_DIR="$cand"; break
  fi
done
if [[ -z "$HERMES_SRC_DIR" ]]; then
  HERMES_SRC_DIR="$(find /opt /root -maxdepth 6 -type f -name 'run_agent.py' 2>/dev/null \
      | head -n1 | xargs -r dirname || true)"
fi
if [[ -z "$HERMES_SRC_DIR" ]]; then
  log "WARN: could not locate Hermes source dir (run_agent.py). Runner --hermes-agent-dir must be set manually." >&2
fi
log "Hermes source dir: ${HERMES_SRC_DIR:-<unknown>}"

# ---------------------------------------------------------------------------
# 3) Discover Hermes plugins dir (contains observability/)
# ---------------------------------------------------------------------------
HERMES_PLUGINS_DIR=""
for cand in \
    /opt/hermes/plugins \
    /opt/hermes-agent/plugins \
    "${HERMES_SRC_DIR:-/nonexistent}/plugins" \
    /root/.hermes/hermes-agent/plugins ; do
  if [[ -d "$cand" ]]; then HERMES_PLUGINS_DIR="$cand"; break; fi
done
if [[ -z "$HERMES_PLUGINS_DIR" ]]; then
  HERMES_PLUGINS_DIR="$(find /opt /root -maxdepth 7 -type d -path '*/plugins/observability' 2>/dev/null \
      | head -n1 | xargs -r dirname || true)"
fi
log "Hermes plugins dir: ${HERMES_PLUGINS_DIR:-<unknown>}"

# 早持久化：即使后续 heavy step 失败，L4 也能看到 path env（当前 build 会 abort，故仅作
# 稳定性保险）。L4 会再次 source 本文件。
{
  echo "HERMES_VENV_PY=$HERMES_VENV_PY"
  echo "HERMES_SRC_DIR=${HERMES_SRC_DIR:-}"
  echo "HERMES_PLUGINS_DIR=${HERMES_PLUGINS_DIR:-}"
} > "$OUT_ENV"

# ---------------------------------------------------------------------------
# 4) Install langfuse SDK + PyYAML into Hermes venv
# ---------------------------------------------------------------------------
PIP_IDX="${PIP_INDEX_URL:-https://pypi.org/simple/}"
HERMES_VENV_DIR="$(dirname "$(dirname "$HERMES_VENV_PY")")"

install_deps() {
  if command -v uv >/dev/null 2>&1; then
    log "Installing langfuse + pyyaml via uv into $HERMES_VENV_DIR ..."
    if VIRTUAL_ENV="$HERMES_VENV_DIR" uv pip install --python "$HERMES_VENV_PY" \
         --index-url "$PIP_IDX" langfuse pyyaml; then
      return 0
    fi
    log "WARN: 'uv pip install' failed; trying ensurepip/pip fallback." >&2
  else
    log "uv not on PATH; trying ensurepip/pip fallback."
  fi

  if "$HERMES_VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    log "Bootstrapped pip via ensurepip; installing langfuse + pyyaml ..."
    "$HERMES_VENV_PY" -m pip install --no-cache-dir --index-url "$PIP_IDX" langfuse pyyaml \
      && return 0
  fi

  log "Trying 'python -m pip' directly ..."
  "$HERMES_VENV_PY" -m pip install --no-cache-dir --index-url "$PIP_IDX" langfuse pyyaml \
    || "$HERMES_VENV_PY" -m pip install --no-cache-dir langfuse pyyaml
}

if ! install_deps; then
  log "FATAL: could not install langfuse + pyyaml into Hermes venv ($HERMES_VENV_PY)." >&2
  exit 1
fi

log "Verifying langfuse + yaml import in Hermes venv..."
"$HERMES_VENV_PY" - <<'PYEOF'
import sys
try:
    import yaml  # noqa: F401
    import langfuse  # noqa: F401
except Exception as exc:  # noqa: BLE001
    sys.stderr.write(f"[hermes-install-heavy] FATAL: required dep import failed: {exc!r}\n")
    sys.exit(1)
print("[hermes-install-heavy] OK: yaml + langfuse importable in Hermes venv")
PYEOF

# ---------------------------------------------------------------------------
# 5) Firecrawl (only if FIRECRAWL_API_KEY was baked). Bootstraps Node via nvm.
# ---------------------------------------------------------------------------
if [[ -n "${FIRECRAWL_API_KEY:-}" ]]; then
  GH_PREFIX="${GITHUB_PROXY_PREFIX-https://ghfast.top/}"
  NVM_VERSION="${NVM_VERSION:-v0.40.5}"
  NODE_MAJOR="${NODE_MAJOR:-26}"
  NVM_INSTALL_URL="${GH_PREFIX}https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh"

  log "Upgrading Node via nvm ${NVM_VERSION} (target Node ${NODE_MAJOR}) for firecrawl-cli..."
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  NVM_DIRECT_URL="https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh"
  NVM_INSTALLED=0
  for src in "$NVM_INSTALL_URL" "$NVM_DIRECT_URL"; do
    [[ -z "$src" ]] && continue
    log "  trying nvm source: $src"
    if curl --max-time 60 --connect-timeout 10 -fsSL -o /tmp/nvm-install.sh "$src" && bash /tmp/nvm-install.sh; then
      NVM_INSTALLED=1
      break
    fi
    log "  nvm source failed (curl or bash): $src" >&2
  done
  if [[ "$NVM_INSTALLED" -eq 1 ]]; then
    # shellcheck disable=SC1091
    \. "$NVM_DIR/nvm.sh"
    if nvm install "$NODE_MAJOR"; then
      nvm use "$NODE_MAJOR" >/dev/null 2>&1 || true
      nvm alias default "$NODE_MAJOR" >/dev/null 2>&1 || true
      NODE_BIN_DIR="$(dirname "$(nvm which "$NODE_MAJOR" 2>/dev/null || command -v node)")"
      export PATH="$NODE_BIN_DIR:$PATH"
      log "Node upgraded: $(node -v 2>/dev/null || echo '?'), npm $(npm -v 2>/dev/null || echo '?') (dir: $NODE_BIN_DIR)"
    else
      log "WARN: 'nvm install ${NODE_MAJOR}' failed; firecrawl-cli may fail on old Node." >&2
    fi
  else
    log "WARN: nvm install failed (network? ${NVM_INSTALL_URL}); firecrawl-cli may fail on old Node." >&2
  fi

  log "FIRECRAWL_API_KEY present; running 'npx -y firecrawl-cli init --all'"
  if command -v npx >/dev/null 2>&1; then
    FIRECRAWL_API_KEY="${FIRECRAWL_API_KEY}" npx -y firecrawl-cli init --all \
      || log "WARN: firecrawl-cli init failed (non-fatal); check runtime FIRECRAWL_API_KEY." >&2
  else
    log "WARN: npx not found in image; skipping firecrawl-cli init." >&2
  fi
else
  log "FIRECRAWL_API_KEY empty; skipping firecrawl-cli init (Node upgrade also skipped)."
fi

# ---------------------------------------------------------------------------
# 6) OpenSpace（quality-first skill hub）：git clone + uv 独立 3.12 venv + pip install -e。
#    host skills 拷贝 与 mcp_servers.openspace 注册留给 L4 / entrypoint。
# ---------------------------------------------------------------------------
INSTALL_OPENSPACE="${INSTALL_OPENSPACE:-false}"
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  OPENSPACE_GIT_URL="${OPENSPACE_GIT_URL:-https://github.com/HKUDS/OpenSpace.git}"
  OPENSPACE_GIT_REF="${OPENSPACE_GIT_REF:-main}"
  OPENSPACE_REPO="/opt/OpenSpace"
  OPENSPACE_VENV="/opt/openspace-venv"

  log "Installing OpenSpace from ${OPENSPACE_GIT_URL}@${OPENSPACE_GIT_REF}"
  if command -v git >/dev/null 2>&1; then
    git clone --filter=blob:none --sparse "${OPENSPACE_GIT_URL}" "${OPENSPACE_REPO}"
    git -C "${OPENSPACE_REPO}" sparse-checkout set --no-cone '/*' '!/assets/'
    git -C "${OPENSPACE_REPO}" checkout "${OPENSPACE_GIT_REF}" || true
  else
    log "ERROR: git not found; cannot clone OpenSpace." >&2
    exit 1
  fi

  if command -v uv >/dev/null 2>&1; then
    export UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download}"
    log "Using UV_PYTHON_INSTALL_MIRROR=${UV_PYTHON_INSTALL_MIRROR}"
    uv venv --python 3.12 "${OPENSPACE_VENV}"
    uv pip install --python "${OPENSPACE_VENV}/bin/python" --index-url "${PIP_IDX}" -e "${OPENSPACE_REPO}"
  else
    log "ERROR: uv not on PATH; cannot create OpenSpace 3.12 venv." >&2
    exit 1
  fi

  ln -sf "${OPENSPACE_VENV}/bin/openspace-mcp" /usr/local/bin/openspace-mcp
  for extra in openspace-cloud-auth openspace-download-skill openspace-upload-skill; do
    [[ -x "${OPENSPACE_VENV}/bin/${extra}" ]] && ln -sf "${OPENSPACE_VENV}/bin/${extra}" "/usr/local/bin/${extra}" || true
  done

  if openspace-mcp --help >/dev/null 2>&1; then
    log "OK: openspace-mcp importable"
  else
    log "WARN: 'openspace-mcp --help' failed; check OpenSpace install." >&2
  fi
else
  log "INSTALL_OPENSPACE=${INSTALL_OPENSPACE}: skip OpenSpace MCP plugin install"
fi

# ---------------------------------------------------------------------------
# 7) agentmemory memory provider plugin：Node>=20 + npm + engine warmup + git clone。
#    plugin cp 到 $HERMES_HOME/plugins/agentmemory 留给 L4 install-config.sh。
# ---------------------------------------------------------------------------
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  AGENTMEMORY_GIT_URL="${AGENTMEMORY_GIT_URL:-https://github.com/rohitg00/agentmemory.git}"
  AGENTMEMORY_GIT_REF="${AGENTMEMORY_GIT_REF:-main}"
  AGENTMEMORY_SRC="/opt/agentmemory-src"
  export HOME="${HOME:-/root}"
  [[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && export NPM_CONFIG_REGISTRY

  log "Installing agentmemory memory provider plugin (offline local embeddings)"

  _need_node="true"
  if command -v node >/dev/null 2>&1; then
    _cur_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    [[ "${_cur_major}" -ge 20 ]] && _need_node="false"
  fi
  if [[ "${_need_node}" == "true" ]]; then
    GH_PREFIX="${GITHUB_PROXY_PREFIX-https://ghfast.top/}"
    NVM_VERSION="${NVM_VERSION:-v0.40.5}"
    NODE_MAJOR="${NODE_MAJOR:-20}"
    NVM_INSTALL_URL="${GH_PREFIX}https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh"
    log "Installing Node ${NODE_MAJOR} via nvm ${NVM_VERSION} for agentmemory..."
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if curl -fsSL -o- "$NVM_INSTALL_URL" | bash; then
      # shellcheck disable=SC1091
      \. "$NVM_DIR/nvm.sh"
      if nvm install "$NODE_MAJOR"; then
        nvm use "$NODE_MAJOR" >/dev/null 2>&1 || true
        nvm alias default "$NODE_MAJOR" >/dev/null 2>&1 || true
        NODE_BIN_DIR="$(dirname "$(nvm which "$NODE_MAJOR" 2>/dev/null || command -v node)")"
        export PATH="$NODE_BIN_DIR:$PATH"
        log "Node upgraded: $(node -v 2>/dev/null || echo '?'), npm $(npm -v 2>/dev/null || echo '?')"
      else
        log "ERROR: 'nvm install ${NODE_MAJOR}' failed; cannot install agentmemory." >&2
        exit 1
      fi
    else
      log "ERROR: nvm install failed (${NVM_INSTALL_URL}); cannot install agentmemory." >&2
      exit 1
    fi
  else
    log "Node $(node -v) already >= 20; skipping nvm upgrade."
  fi

  npm install -g @agentmemory/agentmemory

  AM_BIN="$(command -v agentmemory || true)"
  if [[ -z "${AM_BIN}" ]]; then
    AM_BIN="$(ls /root/.nvm/versions/node/*/bin/agentmemory 2>/dev/null | head -1 || true)"
  fi
  if [[ -n "${AM_BIN}" ]]; then
    ln -sf "${AM_BIN}" /usr/local/bin/agentmemory
    log "Symlinked agentmemory -> /usr/local/bin/agentmemory (source: ${AM_BIN})"
  else
    log "ERROR: agentmemory CLI not found after npm install; cannot expose on PATH." >&2
    exit 1
  fi
  NODE_BIN="$(command -v node || true)"
  if [[ -n "${NODE_BIN}" && ! -x /usr/local/bin/node ]]; then
    ln -sf "${NODE_BIN}" /usr/local/bin/node || true
  fi

  npm install -g @agentmemory/mcp
  AM_MCP_BIN="$(command -v agentmemory-mcp || true)"
  if [[ -z "${AM_MCP_BIN}" ]]; then
    AM_MCP_BIN="$(ls /root/.nvm/versions/node/*/bin/agentmemory-mcp 2>/dev/null | head -1 || true)"
  fi
  if [[ -n "${AM_MCP_BIN}" ]]; then
    ln -sf "${AM_MCP_BIN}" /usr/local/bin/agentmemory-mcp
    log "Symlinked agentmemory-mcp -> /usr/local/bin/agentmemory-mcp (source: ${AM_MCP_BIN})"
  else
    log "WARN: agentmemory-mcp not found after npm install; MCP server will not be registered." >&2
  fi

  # Warmup engine + local embedding model, then reset memory state (keep engine/model cache).
  log "Warming up agentmemory engine + local embedding model (build-time, networked)"
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
    log "agentmemory warmup server ready; triggering demo to fetch engine/model, then reset"
    agentmemory demo >/tmp/agentmemory-demo.log 2>&1 || log "WARN: agentmemory demo returned non-zero (non-fatal)"
  else
    log "WARN: agentmemory warmup server not ready in time; engine/model may fetch at first runtime start."
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
    log "Reset agentmemory memory state (kept engine binary + model cache)"
  fi

  # git clone agentmemory 到 /opt/agentmemory-src；plugin cp 由 L4 完成。
  if command -v git >/dev/null 2>&1; then
    git clone --filter=blob:none --sparse "${AGENTMEMORY_GIT_URL}" "${AGENTMEMORY_SRC}"
    git -C "${AGENTMEMORY_SRC}" sparse-checkout set --no-cone '/integrations/hermes'
    git -C "${AGENTMEMORY_SRC}" checkout "${AGENTMEMORY_GIT_REF}" || true
  else
    log "ERROR: git not found; cannot clone agentmemory." >&2
    exit 1
  fi
  if [[ ! -d "${AGENTMEMORY_SRC}/integrations/hermes" ]]; then
    log "ERROR: agentmemory integrations/hermes not found after clone." >&2
    exit 1
  fi
else
  log "INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY}: skip agentmemory memory provider plugin install"
fi

log "Hermes L2 heavy install complete."
