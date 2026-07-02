#!/usr/bin/env bash
# Run inside Docker build for the LIFT Hermes image.
#
# Responsibilities (see .trae/documents/hermes_runtime_integration_plan.md §A.3):
#   1. Install langfuse SDK into Hermes' own venv.
#   2. Overlay Hermes' bundled observability/langfuse plugin with the LIFT
#      version copied from legacy/langfuse-hermes (now in-tree at
#      agent-runtimes/hermes/langfuse-hermes).
#   3. Enable observability/langfuse (best-effort; falls back to entrypoint).
#   4. Leave runner in place (already COPYed by Dockerfile) and record the
#      discovered Hermes venv python path for the entrypoint / adapter.
#
# The Hermes image layout differs across builds, so paths are DISCOVERED here
# rather than hardcoded, then persisted to /opt/evolve-eval/hermes-paths.env.
set -euo pipefail

OUT_ENV="/opt/evolve-eval/hermes-paths.env"
mkdir -p /opt/evolve-eval

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
log "Installing langfuse + pyyaml into Hermes venv..."
"$HERMES_VENV_PY" -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL:-https://pypi.org/simple/}" langfuse pyyaml \
  || "$HERMES_VENV_PY" -m pip install --no-cache-dir langfuse pyyaml

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
# 6) Best-effort enable the plugin (may require HOME/profile; entrypoint retries)
# ---------------------------------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
  HERMES_HOME=/opt/data hermes plugins enable observability/langfuse >/dev/null 2>&1 \
    && log "Enabled observability/langfuse" \
    || log "NOTE: 'hermes plugins enable' deferred to entrypoint (needs runtime HOME)."
fi

# ---------------------------------------------------------------------------
# 6b) Firecrawl: only when a non-empty API key was baked in. Initialize the
#     firecrawl CLI so the Hermes agent's web search/scrape works at runtime.
# ---------------------------------------------------------------------------
if [[ -n "${FIRECRAWL_API_KEY:-}" ]]; then
  log "FIRECRAWL_API_KEY present; running 'npx -y firecrawl-cli init --all'"
  if command -v npx >/dev/null 2>&1; then
    FIRECRAWL_API_KEY="${FIRECRAWL_API_KEY}" npx -y firecrawl-cli init --all \
      || log "WARN: firecrawl-cli init failed (non-fatal); check runtime FIRECRAWL_API_KEY." >&2
  else
    log "WARN: npx not found in image; skipping firecrawl-cli init." >&2
  fi
else
  log "FIRECRAWL_API_KEY empty; skipping firecrawl-cli init."
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
