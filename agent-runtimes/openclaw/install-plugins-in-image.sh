#!/usr/bin/env bash
# Run inside Docker build (OpenClaw base image already has `openclaw` on PATH).
set -euo pipefail

export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/root/.openclaw}"
export HOME="${HOME:-/root}"
mkdir -p "${OPENCLAW_STATE_DIR}/extensions"

# 是否安装并启用 self-evolving-plugin-pro。默认 true；raw 镜像传 false 跳过安装/启用并删除 entry。
INSTALL_SELF_EVOLVING="${INSTALL_SELF_EVOLVING:-true}"

CONFIG_DIR="/tmp/config"
MODELS_FRAGMENT="${CONFIG_DIR}/models.fragment.json"
MODELS_RESOLVED="/tmp/models.fragment.resolved.json"

# 1) Langfuse tracer (repo copy)
cp -r /tmp/langfuse-tracer "${OPENCLAW_STATE_DIR}/extensions/langfuse-tracer"

# 2) Self-evolving plugin (official install script + runtime venv)
# 注意：repo_root 不能落在 /tmp，因为 LIFT 启容器时会 ``-v /tmp:/tmp`` 把宿主机
# /tmp 整个挂进来屏蔽镜像里的 /tmp/self-evolving-plugin-pro。
# install 脚本基于 cwd 推 repo_root 写入 runtime-ready.json，所以解到 /opt 下。
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  mkdir -p /opt
  cd /opt
  unzip -q /tmp/self-evolving-plugin-pro.zip
  cd /opt/self-evolving-plugin-pro
  bash scripts/install-openclaw-plugin.sh
else
  echo "INSTALL_SELF_EVOLVING=${INSTALL_SELF_EVOLVING}: skip self-evolving-plugin-pro install (raw image)"
fi

# 3) Ensure required plugins enabled (含 OpenClaw 自带 stock firecrawl，运行时读 FIRECRAWL_API_KEY)
openclaw plugins enable langfuse-tracer 2>/dev/null || true
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  openclaw plugins enable self-evolving-plugin-pro 2>/dev/null || true
fi
openclaw plugins enable firecrawl 2>/dev/null || true

# Ark / custom providers often only support thinking=off (plugin defaults to low).
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  WORKER_JS="${OPENCLAW_STATE_DIR}/extensions/self-evolving-plugin-pro/src/review/worker.js"
  if [[ -f "${WORKER_JS}" ]]; then
    sed -i 's/"--thinking", "low"/"--thinking", "off"/g' "${WORKER_JS}" || true
  fi
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

# 5) Merge LIFT config fragments (plugins → gateway → agents → skills → models)
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/plugins.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/gateway.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/agents.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/skills.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${MODELS_RESOLVED}"

# raw 镜像：plugins.fragment.json 把 self-evolving-plugin-pro 同时写进 entries 与 allow，
# 这里同时从两处剥掉，避免 gateway 启动时加载缺失扩展或在 allowlist 中保留无效 id。
if [[ "${INSTALL_SELF_EVOLVING}" != "true" ]]; then
  node -e "const fs=require('fs');const p='${TARGET}';const j=JSON.parse(fs.readFileSync(p,'utf8'));if(j.plugins){if(j.plugins.entries){delete j.plugins.entries['self-evolving-plugin-pro'];}if(Array.isArray(j.plugins.allow)){j.plugins.allow=j.plugins.allow.filter(x=>x!=='self-evolving-plugin-pro');}}fs.writeFileSync(p,JSON.stringify(j,null,2)+'\n');"
fi

echo "Plugins installed under ${OPENCLAW_STATE_DIR}/extensions:"
ls -la "${OPENCLAW_STATE_DIR}/extensions" || true

openclaw plugins list 2>/dev/null || true
