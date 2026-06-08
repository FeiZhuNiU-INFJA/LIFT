#!/usr/bin/env bash
set -euo pipefail

export HOME="${HOME:-/home/node}"
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/home/node/.openclaw}"
STATE_DIR="${OPENCLAW_STATE_DIR}"
CONFIG_PATH="${STATE_DIR}/openclaw.json"
GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
CONFIG_DIR="/opt/openclaw-config"
READY_MARKER="${STATE_DIR}/.evolve-eval-entrypoint-ready"

mkdir -p "${STATE_DIR}/workspace"

if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    OPENCLAW_GATEWAY_TOKEN="$(openssl rand -hex 32)"
  else
    OPENCLAW_GATEWAY_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  export OPENCLAW_GATEWAY_TOKEN
fi

write_openclaw_config() {
  local ark_key="${ARK_API_KEY:-}"
  if [[ -z "${ark_key}" ]]; then
    echo "[entrypoint] WARN: ARK_API_KEY is empty; model provider may fail" >&2
  fi

  local base models plugins merged
  base="$(cat "${CONFIG_DIR}/openclaw.base.json")"
  models="$(cat "${CONFIG_DIR}/models.fragment.json")"
  plugins="$(cat "${CONFIG_DIR}/plugins.fragment.json")"

  merged="$(printf '%s\n' "${base}" "${models}" "${plugins}" | jq -s 'reduce .[] as $item ({}; . * $item)')"

  merged="$(printf '%s' "${merged}" | jq \
    --arg token "${OPENCLAW_GATEWAY_TOKEN}" \
    --argjson port "${GATEWAY_PORT}" \
    --arg ark "${ark_key}" \
    '
      .gateway.auth.token = $token
      | .gateway.port = $port
      | .models.providers["custom-ark-cn-beijing-volces-com"].apiKey = $ark
    ')"

  printf '%s\n' "${merged}" > "${CONFIG_PATH}"
}

install_plugins() {
  if ! command -v openclaw >/dev/null 2>&1; then
    echo "[entrypoint] openclaw CLI not on PATH" >&2
    exit 127
  fi

  if [[ ! -d "${STATE_DIR}/extensions/self-evolving-plugin-pro" ]]; then
    echo "[entrypoint] installing self-evolving-plugin-pro..."
    bash /opt/plugins/self-evolving-plugin-pro/scripts/install-openclaw-plugin.sh \
      || true
  fi

  if [[ ! -d "${STATE_DIR}/extensions/langfuse-tracer" ]]; then
    echo "[entrypoint] installing langfuse-tracer..."
    openclaw plugins install --dangerously-force-unsafe-install /opt/plugins/langfuse-tracer \
      || openclaw plugins install /opt/plugins/langfuse-tracer \
      || true
  fi

  openclaw plugins enable langfuse-tracer 2>/dev/null || true
  openclaw plugins enable self-evolving-plugin-pro 2>/dev/null || true
}

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "[entrypoint] writing ${CONFIG_PATH}"
  write_openclaw_config
elif [[ "${OPENCLAW_FORCE_CONFIG_REFRESH:-}" == "1" ]]; then
  echo "[entrypoint] refreshing ${CONFIG_PATH}"
  write_openclaw_config
fi

if [[ ! -f "${READY_MARKER}" ]]; then
  install_plugins
  touch "${READY_MARKER}"
fi

echo "[entrypoint] gateway token (for host CLI): ${OPENCLAW_GATEWAY_TOKEN}"
echo "[entrypoint] starting gateway on port ${GATEWAY_PORT}"

if command -v openclaw >/dev/null 2>&1; then
  exec openclaw gateway --bind lan --port "${GATEWAY_PORT}"
fi

for app_root in /app /usr/local/lib/node_modules/openclaw /opt/openclaw; do
  if [[ -f "${app_root}/dist/index.js" ]]; then
    cd "${app_root}"
    exec node dist/index.js gateway --bind lan --port "${GATEWAY_PORT}"
  fi
done

echo "[entrypoint] cannot find openclaw gateway entrypoint" >&2
exit 1
