#!/usr/bin/env bash
# LIFT Hermes container entrypoint.
#
# Runs on every `docker run` (and thus once per LIFT container). The Hermes state
# root ($HERMES_HOME) is initialized ONCE at image build (see hermes-bootstrap.sh
# invoked from the Dockerfile), NOT here — so a holdout container restarted from a
# committed delta image never re-seeds and can never clobber the evolved
# memory/skills we are measuring. This entrypoint only does per-run injection:
#   1. Patches $HERMES_HOME/config.yaml model block from env (secrets never baked).
#   2. Maps LIFT LANGFUSE_* / API server creds into env + $HERMES_HOME/.env.
#   3. Best-effort enables the observability/langfuse plugin.
#   4. Execs CMD (default: `tail -f /dev/null`) so the container idles and LIFT
#      can drive Hermes via `docker exec ... hermes_runner.py`.
set -euo pipefail

PATHS_ENV="/opt/lift/hermes-paths.env"
if [[ -f "$PATHS_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$PATHS_ENV"
fi

export HERMES_HOME="${HERMES_HOME:-/opt/hermes-state}"
mkdir -p "$HERMES_HOME"

# Patch config.yaml with the Hermes venv python. That venv is where
# scripts/install-heavy.sh installed (and verified) PyYAML, so it is the only
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
  "$PY" /opt/lift/patch_hermes_config.py
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
# - API server vars are written as-is for Hermes compatibility.
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
# The `hermes` CLI lives in the same venv bin dir as HERMES_VENV_PY (sourced from
# hermes-paths.env above). `docker exec` / a stripped entrypoint PATH may not
# include that dir, so resolve the CLI by absolute path first and prepend its dir
# to PATH; fall back to a bare `hermes` only if discovery failed.
HERMES_CLI=""
if [[ -n "${HERMES_VENV_PY:-}" && -x "$HERMES_VENV_PY" ]]; then
  HERMES_BIN_DIR="$(dirname "$HERMES_VENV_PY")"
  export PATH="$HERMES_BIN_DIR:$PATH"
  if [[ -x "$HERMES_BIN_DIR/hermes" ]]; then
    HERMES_CLI="$HERMES_BIN_DIR/hermes"
  fi
fi
if [[ -z "$HERMES_CLI" ]] && command -v hermes >/dev/null 2>&1; then
  HERMES_CLI="hermes"
fi

if [[ -n "$HERMES_CLI" ]]; then
  # Keep the command's own stdout/stderr in the container log so failures are
  # diagnosable (previously silenced by >/dev/null 2>&1). Wrapped in `if` so a
  # non-zero exit stays non-fatal under `set -e`.
  echo "[hermes-entrypoint] enabling observability/langfuse via ${HERMES_CLI} ..."
  if "$HERMES_CLI" plugins enable observability/langfuse 2>&1; then
    echo "[hermes-entrypoint] observability/langfuse enabled"
  else
    echo "[hermes-entrypoint] NOTE: plugin enable skipped/failed (non-fatal)"
  fi
else
  echo "[hermes-entrypoint] WARN: hermes CLI not found (HERMES_VENV_PY='${HERMES_VENV_PY:-}'); langfuse plugin enable skipped." >&2
fi

# agentmemory 变体：在容器空转前后台拉起 agentmemory server（:3111，离线本地嵌入）。
# chat 走 docker exec hermes_runner.py（同容器同网络命名空间），runner 内的 AIAgent 通过
# localhost:3111 访问该 server。CI=1 跳过所有交互 prompt；日志落 bind mount 便于诊断。
if [[ "${AGENTMEMORY_ENABLED:-false}" == "true" ]]; then
  export CI=1
  export HOME="${HOME:-/root}"
  _am_log="/workspace/task/agentmemory-server.log"
  mkdir -p /workspace/task 2>/dev/null || true
  if command -v agentmemory >/dev/null 2>&1; then
    echo "[hermes-entrypoint] starting agentmemory server on :3111 ..."
    ( agentmemory >"${_am_log}" 2>&1 & ) || echo "[hermes-entrypoint] WARN: failed to spawn agentmemory server" >&2
    for _i in $(seq 1 30); do
      if curl -fsS http://localhost:3111/agentmemory/livez >/dev/null 2>&1 \
         || curl -fsS http://localhost:3111/agentmemory/health >/dev/null 2>&1; then
        echo "[hermes-entrypoint] agentmemory server ready on :3111"
        break
      fi
      sleep 1
    done
  else
    echo "[hermes-entrypoint] WARN: AGENTMEMORY_ENABLED=true but 'agentmemory' not on PATH; skipping server start." >&2
  fi
fi

exec "$@"
