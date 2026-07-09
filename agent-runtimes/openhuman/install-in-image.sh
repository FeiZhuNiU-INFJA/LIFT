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

API_KEY_ESC="$(escape_sed "${API_KEY:-}")"
API_URL_ESC="$(escape_sed "${API_URL:-}")"
INFERENCE_URL_ESC="$(escape_sed "${INFERENCE_URL:-https://ark.cn-beijing.volces.com/api/v3}")"
DEFAULT_MODEL_ESC="$(escape_sed "${DEFAULT_MODEL:-}")"

sed \
  -e "s/__API_KEY__/${API_KEY_ESC}/g" \
  -e "s/__API_URL__/${API_URL_ESC}/g" \
  -e "s/__INFERENCE_URL__/${INFERENCE_URL_ESC}/g" \
  -e "s/__DEFAULT_MODEL__/${DEFAULT_MODEL_ESC}/g" \
  /tmp/config.toml.template > "${OPENHUMAN_HOME}/config.toml"

echo "==> Rendered ${OPENHUMAN_HOME}/config.toml:"
sed -e 's/api_key = ".*"/api_key = "<redacted>"/' "${OPENHUMAN_HOME}/config.toml"

# Sanity check：确认 openhuman-core 可执行。--help 不同版本行为不一致，容忍失败。
if command -v openhuman-core >/dev/null 2>&1; then
  openhuman-core --version 2>/dev/null \
    || openhuman-core help >/dev/null 2>&1 \
    || true
else
  echo "WARN: openhuman-core binary not found on PATH; check .deb layout" >&2
fi

echo "OpenHuman baked; config at ${OPENHUMAN_HOME}/config.toml"
