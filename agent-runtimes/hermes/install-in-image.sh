#!/usr/bin/env bash
# Run inside Docker build for the LIFT Hermes image.
#
# Responsibilities (see .trae/documents/hermes_runtime_integration_plan.md §A.3):
#   1. Install langfuse SDK into Hermes' own venv.
#   2. Overlay Hermes' bundled observability/langfuse plugin with the LIFT
#      version maintained in-tree at agent-runtimes/hermes/langfuse-hermes.
#   3. Enable observability/langfuse (best-effort; falls back to entrypoint).
#   4. Leave runner in place (already COPYed by Dockerfile) and record the
#      discovered Hermes venv python path for the entrypoint / adapter.
#
# The Hermes image layout differs across builds, so paths are DISCOVERED here
# rather than hardcoded, then persisted to /opt/lift/hermes-paths.env.
set -euo pipefail

OUT_ENV="/opt/lift/hermes-paths.env"
mkdir -p /opt/lift

log() { echo "[hermes-install] $*"; }

# ---------------------------------------------------------------------------
# 1) Discover Hermes venv python
# ---------------------------------------------------------------------------
# Known/likely locations first, then a filesystem search as fallback.
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
  # Fall back to resolving the `hermes` CLI shebang, then a bounded find.
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

# ---------------------------------------------------------------------------
# 4) Install langfuse SDK + PyYAML into Hermes venv
# ---------------------------------------------------------------------------
# PyYAML is required by patch_hermes_config.py at container startup to safely
# merge the model block into config.yaml WITHOUT clobbering other keys. We
# install it into the SAME venv the entrypoint uses (HERMES_VENV_PY), then
# assert both import cleanly so a missing dep fails the build (not the run).
#
# Hermes' venv is created by `uv` and ships WITHOUT pip, so `python -m pip`
# fails with "No module named pip". Try installers in order of likelihood:
#   1) uv   — Hermes' own package manager; targets the venv via --python.
#   2) ensurepip — bootstrap pip into the venv, then use it.
#   3) python -m pip — only if pip somehow already exists.
PIP_IDX="${PIP_INDEX_URL:-https://pypi.org/simple/}"
HERMES_VENV_DIR="$(dirname "$(dirname "$HERMES_VENV_PY")")"

install_deps() {
  # 1) uv (preferred): install straight into the discovered venv.
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

  # 2) ensurepip: bootstrap pip into the venv, then install.
  if "$HERMES_VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    log "Bootstrapped pip via ensurepip; installing langfuse + pyyaml ..."
    "$HERMES_VENV_PY" -m pip install --no-cache-dir --index-url "$PIP_IDX" langfuse pyyaml \
      && return 0
  fi

  # 3) last resort: pip may already be present.
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
    sys.stderr.write(f"[hermes-install] FATAL: required dep import failed: {exc!r}\n")
    sys.exit(1)
print("[hermes-install] OK: yaml + langfuse importable in Hermes venv")
PYEOF

# ---------------------------------------------------------------------------
# 5) Overlay observability/langfuse plugin with LIFT version
# ---------------------------------------------------------------------------
if [[ -n "$HERMES_PLUGINS_DIR" ]]; then
  DEST="$HERMES_PLUGINS_DIR/observability/langfuse"
  mkdir -p "$DEST"
  if [[ -f "$DEST/__init__.py" ]]; then
    cp -a "$DEST/__init__.py" "$DEST/__init__.py.upstream.bak" || true
  fi
  cp -a /tmp/langfuse-hermes/. "$DEST/"
  log "Overlaid LIFT langfuse plugin into $DEST"
else
  log "WARN: Hermes plugins dir not found; langfuse plugin overlay skipped. Set it up at runtime." >&2
fi

# ---------------------------------------------------------------------------
# 5b) Patch _supports_reasoning_extra_body 白名单加入 ARK (volces.com)。
#     Hermes upstream 只对 OpenRouter / GitHub / LMStudio / Nous Portal 放开
#     ``reasoning`` extra_body，ARK doubao-seed 端点默认走 "不支持" 分支 →
#     ``reasoning_config`` 会被静默丢弃。ARK 已实测接受 ``reasoning_effort=medium``
#     以及嵌套 ``reasoning={enabled,effort}``；这里用 Hermes 自带 python 在 nousresearch.com
#     分支后插入一条 volces.com 分支，保留 8-space 缩进。幂等：已插入过就 skip。
if [[ -n "${HERMES_SRC_DIR:-}" && -f "${HERMES_SRC_DIR}/run_agent.py" ]]; then
  RUN_AGENT_PY="${HERMES_SRC_DIR}/run_agent.py"
  log "Patching _supports_reasoning_extra_body to whitelist volces.com in $RUN_AGENT_PY"
  "$HERMES_VENV_PY" - "$RUN_AGENT_PY" <<'PYEOF'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
marker = "# LIFT: allow ARK volces.com"
if marker in text:
    print(f"[hermes-install] volces.com whitelist already patched in {path}; skipping.")
    sys.exit(0)

anchor_lines = (
    '        if base_url_host_matches(self._base_url_lower, "nousresearch.com"):\n'
    '            return True\n'
)
insert_lines = (
    '        if base_url_host_matches(self._base_url_lower, "volces.com"):  '
    + marker + '\n'
    '            return True\n'
)
if anchor_lines not in text:
    sys.stderr.write(
        f"[hermes-install] FATAL: anchor for reasoning whitelist not found in {path}. "
        "Upstream Hermes may have refactored _supports_reasoning_extra_body.\n"
    )
    sys.exit(1)
patched = text.replace(anchor_lines, anchor_lines + insert_lines, 1)
path.write_text(patched, encoding="utf-8")
print(f"[hermes-install] Patched volces.com whitelist into {path}.")
PYEOF
else
  log "WARN: HERMES_SRC_DIR/run_agent.py missing; skip _supports_reasoning_extra_body patch." >&2
fi

# ---------------------------------------------------------------------------
# 6) Best-effort enable the plugin (may require HOME/profile; entrypoint retries)
# ---------------------------------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  HERMES_HOME=/opt/hermes-state hermes plugins enable observability/langfuse >/dev/null 2>&1 \
    && log "Enabled observability/langfuse" \
    || log "NOTE: 'hermes plugins enable' deferred to entrypoint (needs runtime HOME)."
fi

# ---------------------------------------------------------------------------
# 6b) Firecrawl: only when a non-empty API key was baked in. Initialize the
#     firecrawl CLI so the Hermes agent's web search/scrape works at runtime.
#
#     firecrawl-cli needs a modern Node. The upstream Hermes image ships an
#     old Node that npx rejects, so we bootstrap Node via nvm FIRST (only when
#     firecrawl is actually requested — no key means no Node churn).
# ---------------------------------------------------------------------------
if [[ -n "${FIRECRAWL_API_KEY:-}" ]]; then
  # GitHub reverse proxy prefix (repo convention: ghfast.top). Override with
  # GITHUB_PROXY_PREFIX= (empty) for direct GitHub, or another mirror.
  GH_PREFIX="${GITHUB_PROXY_PREFIX-https://ghfast.top/}"
  NVM_VERSION="${NVM_VERSION:-v0.40.5}"
  NODE_MAJOR="${NODE_MAJOR:-26}"
  NVM_INSTALL_URL="${GH_PREFIX}https://raw.githubusercontent.com/nvm-sh/nvm/${NVM_VERSION}/install.sh"

  log "Upgrading Node via nvm ${NVM_VERSION} (target Node ${NODE_MAJOR}) for firecrawl-cli..."
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if curl -fsSL -o- "$NVM_INSTALL_URL" | bash; then
    # Load nvm into this shell (in lieu of restarting it), then install Node.
    # shellcheck disable=SC1091
    \. "$NVM_DIR/nvm.sh"
    if nvm install "$NODE_MAJOR"; then
      nvm use "$NODE_MAJOR" >/dev/null 2>&1 || true
      nvm alias default "$NODE_MAJOR" >/dev/null 2>&1 || true
      # Put the freshly installed Node/npm/npx at the front of PATH so the
      # firecrawl step below (and later builders) pick the new binaries.
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
# 6c) OpenSpace（基于 MCP 的 quality-first skill hub，README「Path A: For Your Agent」）。
#     默认不装（INSTALL_OPENSPACE=false）。装的话：
#       - git clone 到 /opt/OpenSpace（sparse 跳过 assets/）。
#       - 独立 Python 3.12 venv（Hermes venv 由 uv 造、无 pip 且很可能 <3.12，不能复用）。
#       - openspace-mcp 软链到 /usr/local/bin。
#       - 拷 host skills 到 Hermes 状态根 skills 目录（随 docker commit 落 delta）。
#     mcp_servers.openspace 的 config.yaml 注册在 entrypoint 阶段由 patch_hermes_config.py
#     完成（OPENSPACE_ENABLED=true 触发），这里只负责装好 openspace-mcp。
# ---------------------------------------------------------------------------
INSTALL_OPENSPACE="${INSTALL_OPENSPACE:-false}"
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  OPENSPACE_GIT_URL="${OPENSPACE_GIT_URL:-https://github.com/HKUDS/OpenSpace.git}"
  OPENSPACE_GIT_REF="${OPENSPACE_GIT_REF:-main}"
  OPENSPACE_REPO="/opt/OpenSpace"
  OPENSPACE_VENV="/opt/openspace-venv"
  OS_HERMES_HOME="${HERMES_HOME:-/opt/hermes-state}"
  PIP_IDX="${PIP_INDEX_URL:-https://pypi.org/simple/}"

  log "Installing OpenSpace from ${OPENSPACE_GIT_URL}@${OPENSPACE_GIT_REF}"
  if command -v git >/dev/null 2>&1; then
    git clone --filter=blob:none --sparse "${OPENSPACE_GIT_URL}" "${OPENSPACE_REPO}"
    git -C "${OPENSPACE_REPO}" sparse-checkout set --no-cone '/*' '!/assets/'
    git -C "${OPENSPACE_REPO}" checkout "${OPENSPACE_GIT_REF}" || true
  else
    log "ERROR: git not found; cannot clone OpenSpace." >&2
    exit 1
  fi

  # 独立 uv 3.12 venv（uv 已在 Hermes 构建可用，见上文 install_deps 的探测）。
  if command -v uv >/dev/null 2>&1; then
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

  # 拷 host skills（bootstrap.sh 会再 mkdir skills，这里先建保证顺序无关）。
  mkdir -p "${OS_HERMES_HOME}/skills"
  OPENSPACE_HOST_SKILLS="${OPENSPACE_REPO}/openspace/host_skills"
  for hs in delegate-task skill-discovery; do
    if [[ -d "${OPENSPACE_HOST_SKILLS}/${hs}" ]]; then
      cp -r "${OPENSPACE_HOST_SKILLS}/${hs}" "${OS_HERMES_HOME}/skills/${hs}"
    else
      log "WARN: OpenSpace host skill missing: ${OPENSPACE_HOST_SKILLS}/${hs}" >&2
    fi
  done

  log "Verifying openspace-mcp..."
  if openspace-mcp --help >/dev/null 2>&1; then
    log "OK: openspace-mcp importable"
  else
    log "WARN: 'openspace-mcp --help' failed; check OpenSpace install." >&2
  fi
else
  log "INSTALL_OPENSPACE=${INSTALL_OPENSPACE}: skip OpenSpace MCP plugin install"
fi

# ---------------------------------------------------------------------------
# 6d) agentmemory memory provider plugin（README「Option 2: Memory provider plugin」）。
#     默认不装（INSTALL_AGENTMEMORY=false）。装的话：
#       - 装 Node >= 20（Hermes 基镜像自带 Node 偏旧，用 nvm 升级；与 firecrawl 段同款）。
#       - npm -g 装 @agentmemory/agentmemory。
#       - 构建期预热离线引擎 + 本地嵌入模型，然后清空记忆状态（保留 bin/ 与模型缓存）。
#       - git clone agentmemory，把 integrations/hermes 拷进 $HERMES_HOME/plugins/agentmemory
#         （随 docker commit 落 delta）。
#     config.yaml 的 memory.provider=agentmemory 由 entrypoint 阶段 patch_hermes_config.py
#     写入（AGENTMEMORY_ENABLED=true 触发）；:3111 server 由 hermes-entrypoint.sh 后台拉起。
# ---------------------------------------------------------------------------
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  AGENTMEMORY_GIT_URL="${AGENTMEMORY_GIT_URL:-https://github.com/rohitg00/agentmemory.git}"
  AGENTMEMORY_GIT_REF="${AGENTMEMORY_GIT_REF:-main}"
  AGENTMEMORY_SRC="/opt/agentmemory-src"
  AM_HERMES_HOME="${HERMES_HOME:-/opt/hermes-state}"
  export HOME="${HOME:-/root}"
  [[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && export NPM_CONFIG_REGISTRY

  log "Installing agentmemory memory provider plugin (offline local embeddings)"

  # 6d-1) Node >= 20：若当前 node 缺失或 <20，用 nvm 升级（与 firecrawl 段同款反代逻辑）。
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

  # 6d-2) 装 agentmemory server + CLI。
  npm install -g @agentmemory/agentmemory

  # 6d-2b) 把 agentmemory / node 软链到 /usr/local/bin（始终在 PATH 上）。
  # nvm 装的 bin 只在 login shell 的 PATH 里；容器 entrypoint（非 login，set -e）与
  # docker exec 的 hermes_runner 看不到，导致运行期起不了 :3111 server。软链固定暴露。
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

  # 6d-3) 构建期预热引擎 + 本地嵌入模型，然后清空记忆状态（保留引擎/模型缓存）。
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

  # 6d-4) 落插件：git clone，sparse 仅取 integrations/hermes，拷进 $HERMES_HOME/plugins/agentmemory。
  if command -v git >/dev/null 2>&1; then
    git clone --filter=blob:none --sparse "${AGENTMEMORY_GIT_URL}" "${AGENTMEMORY_SRC}"
    git -C "${AGENTMEMORY_SRC}" sparse-checkout set --no-cone '/integrations/hermes'
    git -C "${AGENTMEMORY_SRC}" checkout "${AGENTMEMORY_GIT_REF}" || true
  else
    log "ERROR: git not found; cannot clone agentmemory." >&2
    exit 1
  fi
  AM_PLUGIN_SRC="${AGENTMEMORY_SRC}/integrations/hermes"
  if [[ ! -d "${AM_PLUGIN_SRC}" ]]; then
    log "ERROR: agentmemory integrations/hermes not found after clone (${AM_PLUGIN_SRC})." >&2
    exit 1
  fi
  mkdir -p "${AM_HERMES_HOME}/plugins"
  rm -rf "${AM_HERMES_HOME}/plugins/agentmemory"
  cp -r "${AM_PLUGIN_SRC}" "${AM_HERMES_HOME}/plugins/agentmemory"
  log "agentmemory plugin installed under ${AM_HERMES_HOME}/plugins/agentmemory"
else
  log "INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY}: skip agentmemory memory provider plugin install"
fi

# ---------------------------------------------------------------------------
# 7) Persist discovered paths for entrypoint / adapter
# ---------------------------------------------------------------------------
{
  echo "HERMES_VENV_PY=$HERMES_VENV_PY"
  echo "HERMES_SRC_DIR=${HERMES_SRC_DIR:-}"
  echo "HERMES_PLUGINS_DIR=${HERMES_PLUGINS_DIR:-}"
} > "$OUT_ENV"
log "Wrote discovered paths to $OUT_ENV:"
cat "$OUT_ENV"

log "Hermes image install complete."
