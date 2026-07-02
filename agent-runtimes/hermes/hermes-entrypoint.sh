#!/usr/bin/env bash
# LIFT Hermes container entrypoint.
#
# Runs on every `docker run` (and thus once per LIFT container). It:
#   1. Patches /opt/data/config.yaml model block from env (secrets never baked).
#   2. Best-effort enables the observability/langfuse plugin.
#   3. Execs CMD (default: `tail -f /dev/null`) so the container idles and LIFT
#      can drive Hermes via `docker exec ... hermes_runner.py`.
set -euo pipefail

PATHS_ENV="/opt/evolve-eval/hermes-paths.env"
if [[ -f "$PATHS_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$PATHS_ENV"
fi

export HERMES_HOME="${HERMES_HOME:-/opt/data}"
mkdir -p "$HERMES_HOME"

# Patch config.yaml with the Hermes venv python. That venv is where
# install-in-image.sh installed (and verified) PyYAML, so it is the only
# interpreter guaranteed to merge config.yaml without clobbering other keys.
# We only fall back to system python if venv discovery failed (degraded image);
# in that case patch_hermes_config.py still refuses to overwrite an existing
# config rather than corrupt it.
PY="${HERMES_VENV_PY:-}"
if [[ -z "$PY" || ! -x "$PY" ]]; then
  echo "[hermes-entrypoint] WARN: HERMES_VENV_PY unset/invalid; falling back to system python (PyYAML may be missing)."
  PY="$(command -v python3 || command -v python || true)"
fi

if [[ -n "$PY" ]]; then
  # Fatal on failure: an unpatched config.yaml means Hermes runs with the wrong
  # model/api_key and every task fails in a confusing way. Better to stop here.
  "$PY" /opt/evolve-eval/patch_hermes_config.py
else
  echo "[hermes-entrypoint] FATAL: no python found to patch config.yaml" >&2
  exit 1
fi

# Map LIFT LANGFUSE_* -> Hermes-plugin-expected HERMES_LANGFUSE_* (fail-open).
if [[ -n "${LANGFUSE_PUBLIC_KEY:-}" ]]; then export HERMES_LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY}"; fi
if [[ -n "${LANGFUSE_SECRET_KEY:-}" ]]; then export HERMES_LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY}"; fi
if [[ -n "${LANGFUSE_BASE_URL:-}" ]]; then export HERMES_LANGFUSE_BASE_URL="${LANGFUSE_BASE_URL}"; fi

# Append LIFT-provided credentials into the Hermes .env so `hermes` picks them up
# (the langfuse plugin / API server read from ~/.hermes/.env == $HERMES_HOME/.env).
# - Langfuse vars require the HERMES_ prefix (Hermes requirement).
# - API server vars are written as-is (legacy parity).
# Idempotent: each KEY line is removed then re-appended so container restarts
# don't accumulate duplicates.
HERMES_ENV_FILE="${HERMES_HOME}/.env"
touch "$HERMES_ENV_FILE"

append_hermes_env() {
  local key="$1" value="$2"
  [[ -z "$value" ]] && return 0
  # Drop any existing line for this key, then append the fresh value.
  if [[ -f "$HERMES_ENV_FILE" ]]; then
    grep -v -E "^${key}=" "$HERMES_ENV_FILE" > "${HERMES_ENV_FILE}.tmp" 2>/dev/null || true
    mv "${HERMES_ENV_FILE}.tmp" "$HERMES_ENV_FILE"
  fi
  printf '%s=%s\n' "$key" "$value" >> "$HERMES_ENV_FILE"
}

append_hermes_env "HERMES_LANGFUSE_PUBLIC_KEY" "${LANGFUSE_PUBLIC_KEY:-}"
append_hermes_env "HERMES_LANGFUSE_SECRET_KEY" "${LANGFUSE_SECRET_KEY:-}"
append_hermes_env "HERMES_LANGFUSE_BASE_URL" "${LANGFUSE_BASE_URL:-}"
append_hermes_env "API_SERVER_ENABLED" "${API_SERVER_ENABLED:-}"
append_hermes_env "API_SERVER_KEY" "${API_SERVER_KEY:-}"
echo "[hermes-entrypoint] appended LIFT credentials into ${HERMES_ENV_FILE}"

# Best-effort enable langfuse plugin now that HOME exists.
if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable observability/langfuse >/dev/null 2>&1 \
    && echo "[hermes-entrypoint] observability/langfuse enabled" \
    || echo "[hermes-entrypoint] NOTE: plugin enable skipped/failed (non-fatal)"
fi

exec "$@"
