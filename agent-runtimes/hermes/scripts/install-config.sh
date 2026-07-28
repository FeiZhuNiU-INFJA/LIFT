#!/usr/bin/env bash
# Run inside Docker build - L4 轻量层：langfuse-hermes overlay / run_agent.py patch /
# hermes plugins enable / host skills 拷贝 / agentmemory plugin 落地。所有动作都是**秒级**
# 的文件拷贝与字符串替换；改 langfuse-hermes/、patch_hermes_config.py 等只 bust 本层。
#
# 依赖 L2 install-heavy.sh 的路径发现结果（/opt/lift/hermes-paths.env）。
set -euo pipefail

if [[ ! -f /opt/lift/hermes-paths.env ]]; then
  echo "[hermes-install-config] FATAL: /opt/lift/hermes-paths.env missing (L2 install-heavy.sh not run?)." >&2
  exit 1
fi
# shellcheck disable=SC1091
source /opt/lift/hermes-paths.env

log() { echo "[hermes-install-config] $*"; }

log "Reusing discovered paths: HERMES_VENV_PY=$HERMES_VENV_PY  HERMES_SRC_DIR=${HERMES_SRC_DIR:-}  HERMES_PLUGINS_DIR=${HERMES_PLUGINS_DIR:-}"

# ---------------------------------------------------------------------------
# 1) Overlay observability/langfuse plugin with LIFT version
# ---------------------------------------------------------------------------
if [[ -n "${HERMES_PLUGINS_DIR:-}" ]]; then
  DEST="$HERMES_PLUGINS_DIR/observability/langfuse"
  mkdir -p "$DEST"
  if [[ -f "$DEST/__init__.py" && ! -f "$DEST/__init__.py.upstream.bak" ]]; then
    cp -a "$DEST/__init__.py" "$DEST/__init__.py.upstream.bak" || true
  fi
  cp -a /tmp/langfuse-hermes/. "$DEST/"
  log "Overlaid LIFT langfuse plugin into $DEST"
else
  log "WARN: Hermes plugins dir not found; langfuse plugin overlay skipped." >&2
fi

# ---------------------------------------------------------------------------
# 2) Patch _supports_reasoning_extra_body 白名单加入 ARK (volces.com)。幂等：已插入过则 skip。
# ---------------------------------------------------------------------------
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
    print(f"[hermes-install-config] volces.com whitelist already patched in {path}; skipping.")
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
        f"[hermes-install-config] FATAL: anchor for reasoning whitelist not found in {path}. "
        "Upstream Hermes may have refactored _supports_reasoning_extra_body.\n"
    )
    sys.exit(1)
patched = text.replace(anchor_lines, anchor_lines + insert_lines, 1)
path.write_text(patched, encoding="utf-8")
print(f"[hermes-install-config] Patched volces.com whitelist into {path}.")
PYEOF
else
  log "WARN: HERMES_SRC_DIR/run_agent.py missing; skip _supports_reasoning_extra_body patch." >&2
fi

# ---------------------------------------------------------------------------
# 3) Best-effort enable observability/langfuse
# ---------------------------------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  HERMES_HOME=/opt/hermes-state hermes plugins enable observability/langfuse >/dev/null 2>&1 \
    && log "Enabled observability/langfuse" \
    || log "NOTE: 'hermes plugins enable' deferred to entrypoint (needs runtime HOME)."
fi

# ---------------------------------------------------------------------------
# 4) OpenSpace host skills 拷贝（L2 已 git clone + venv + pip 装好 openspace-mcp）。
# ---------------------------------------------------------------------------
INSTALL_OPENSPACE="${INSTALL_OPENSPACE:-false}"
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  OPENSPACE_REPO="/opt/OpenSpace"
  OS_HERMES_HOME="${HERMES_HOME:-/opt/hermes-state}"

  mkdir -p "${OS_HERMES_HOME}/skills"
  OPENSPACE_HOST_SKILLS="${OPENSPACE_REPO}/openspace/host_skills"
  for hs in delegate-task skill-discovery; do
    if [[ -d "${OPENSPACE_HOST_SKILLS}/${hs}" ]]; then
      cp -r "${OPENSPACE_HOST_SKILLS}/${hs}" "${OS_HERMES_HOME}/skills/${hs}"
    else
      log "WARN: OpenSpace host skill missing: ${OPENSPACE_HOST_SKILLS}/${hs}" >&2
    fi
  done
  log "OpenSpace host skills copied into ${OS_HERMES_HOME}/skills"
fi

# ---------------------------------------------------------------------------
# 5) agentmemory plugin：把 clone 好的 integrations/hermes 拷进 $HERMES_HOME/plugins/agentmemory
# ---------------------------------------------------------------------------
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  AGENTMEMORY_SRC="/opt/agentmemory-src"
  AM_HERMES_HOME="${HERMES_HOME:-/opt/hermes-state}"
  AM_PLUGIN_SRC="${AGENTMEMORY_SRC}/integrations/hermes"

  if [[ ! -d "${AM_PLUGIN_SRC}" ]]; then
    log "ERROR: agentmemory integrations/hermes not found at ${AM_PLUGIN_SRC} (L2 install-heavy.sh should have cloned it)." >&2
    exit 1
  fi
  mkdir -p "${AM_HERMES_HOME}/plugins"
  rm -rf "${AM_HERMES_HOME}/plugins/agentmemory"
  cp -r "${AM_PLUGIN_SRC}" "${AM_HERMES_HOME}/plugins/agentmemory"
  log "agentmemory plugin installed under ${AM_HERMES_HOME}/plugins/agentmemory"
fi

log "Hermes L4 config install complete."
