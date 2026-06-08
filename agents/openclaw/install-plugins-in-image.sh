#!/usr/bin/env bash
# Run inside Docker build (OpenClaw base image already has `openclaw` on PATH).
set -euo pipefail

export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/root/.openclaw}"
export HOME="${HOME:-/root}"
mkdir -p "${OPENCLAW_STATE_DIR}/extensions"

CONFIG_DIR="/tmp/config"
MODELS_FRAGMENT="${CONFIG_DIR}/models.fragment.json"
MODELS_RESOLVED="/tmp/models.fragment.resolved.json"

# 1) Langfuse tracer (repo copy)
cp -r /tmp/langfuse-tracer "${OPENCLAW_STATE_DIR}/extensions/langfuse-tracer"

# 2) Self-evolving plugin (official install script + runtime venv)
cd /tmp
unzip -q self-evolving-plugin-pro.zip
cd self-evolving-plugin-pro
bash scripts/install-openclaw-plugin.sh

# 3) Ensure both plugins enabled
openclaw plugins enable langfuse-tracer 2>/dev/null || true
openclaw plugins enable self-evolving-plugin-pro 2>/dev/null || true

# Ark / custom providers often only support thinking=off (plugin defaults to low).
WORKER_JS="${OPENCLAW_STATE_DIR}/extensions/self-evolving-plugin-pro/src/review/worker.js"
if [[ -f "${WORKER_JS}" ]]; then
  sed -i 's/"--thinking", "low"/"--thinking", "off"/g' "${WORKER_JS}" || true
fi

# 4) Resolve models fragment (inject ARK_API_KEY at build time)
if [[ -z "${ARK_API_KEY:-}" ]]; then
  echo "WARN: ARK_API_KEY is empty; models.provider apiKey will remain placeholder" >&2
  cp "${MODELS_FRAGMENT}" "${MODELS_RESOLVED}"
else
  # Escape sed replacement chars in api key
  esc_key="$(printf '%s' "${ARK_API_KEY}" | sed -e 's/[\/&]/\\&/g')"
  sed "s/__ARK_API_KEY__/${esc_key}/g" "${MODELS_FRAGMENT}" > "${MODELS_RESOLVED}"
fi

TARGET="${OPENCLAW_STATE_DIR}/openclaw.json"

# 5) Merge HACE config fragments (plugins → gateway → agents → models)
node /tmp/merge-openclaw-config.mjs "${TARGET}" /tmp/openclaw.json.template
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/gateway.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/agents.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${MODELS_RESOLVED}"

echo "Plugins installed under ${OPENCLAW_STATE_DIR}/extensions:"
ls -la "${OPENCLAW_STATE_DIR}/extensions" || true

openclaw plugins list 2>/dev/null || true
