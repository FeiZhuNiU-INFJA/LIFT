#!/usr/bin/env bash
# Run inside Docker build - L4 轻量层：config 渲染 + fragment merge + mcp set + plugins enable。
#
# 依赖上游 L2 install-heavy.sh 已把插件文件系统落好；本层只做**秒级**的字符串/JSON 拼接与
# openclaw plugins/mcp CLI 调用。改 MODEL_NAME / WORK_OPENAI_API_KEY / WORK_OPENAI_BASE_URL /
# fragments 内容都只 bust 本层，不重跑 npm/pip/warmup。
set -euo pipefail

export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/root/.openclaw}"
export HOME="${HOME:-/root}"

INSTALL_SELF_EVOLVING="${INSTALL_SELF_EVOLVING:-true}"
INSTALL_OPENSPACE="${INSTALL_OPENSPACE:-false}"
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"

CONFIG_DIR="/tmp/config"
MODELS_FRAGMENT="${CONFIG_DIR}/models.fragment.json"
MODELS_RESOLVED="/tmp/models.fragment.resolved.json"
AGENTS_FRAGMENT="${CONFIG_DIR}/agents.fragment.json"
AGENTS_RESOLVED="/tmp/agents.fragment.resolved.json"

# 1) enable 已安装的插件（L2 已装 langfuse-tracer / self-evolving / firecrawl 到 extensions；
#    enable 是幂等且瞬时的 CLI 调用，放这里方便同 config 一起 bust）。
openclaw plugins enable langfuse-tracer 2>/dev/null || true
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  openclaw plugins enable self-evolving-plugin-pro 2>/dev/null || true
fi
openclaw plugins enable firecrawl 2>/dev/null || true

# 2) self-evolving-plugin-pro 的 review worker 默认 --thinking low → 提到 high。
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  WORKER_JS="${OPENCLAW_STATE_DIR}/extensions/self-evolving-plugin-pro/src/review/worker.js"
  if [[ -f "${WORKER_JS}" ]]; then
    sed -i 's/"--thinking", "low"/"--thinking", "high"/g' "${WORKER_JS}" || true
  fi
fi

# 3) Resolve fragments from build-time env.
MODEL_NAME="${MODEL_NAME:-}"
if [[ "${MODEL_NAME}" != custom/* || "${MODEL_NAME}" == "custom/" ]]; then
  echo "ERROR: MODEL_NAME must be 'custom/model_id' (e.g. custom/ep-xxxx); got '${MODEL_NAME}'" >&2
  exit 1
fi
MODEL_ID="${MODEL_NAME#custom/}"

if [[ -z "${WORK_OPENAI_API_KEY:-}" ]]; then
  echo "WARN: WORK_OPENAI_API_KEY is empty; models.provider apiKey will remain placeholder" >&2
fi

esc_key="$(printf '%s' "${WORK_OPENAI_API_KEY:-}" | sed -e 's/[\/&]/\\&/g')"
esc_model_id="$(printf '%s' "${MODEL_ID}" | sed -e 's/[\/&]/\\&/g')"
esc_model_name="$(printf '%s' "${MODEL_NAME}" | sed -e 's/[\/&]/\\&/g')"

sed -e "s/__WORK_OPENAI_API_KEY__/${esc_key}/g" \
    -e "s/__MODEL_ID__/${esc_model_id}/g" \
    "${MODELS_FRAGMENT}" > "${MODELS_RESOLVED}"
sed -e "s/__MODEL_NAME__/${esc_model_name}/g" \
    "${AGENTS_FRAGMENT}" > "${AGENTS_RESOLVED}"

TARGET="${OPENCLAW_STATE_DIR}/openclaw.json"

# 4) Merge LIFT config fragments (plugins → gateway → agents → skills → models)
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/plugins.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/gateway.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${AGENTS_RESOLVED}"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/skills.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${MODELS_RESOLVED}"

# raw 镜像剥离 self-evolving-plugin-pro 引用。
if [[ "${INSTALL_SELF_EVOLVING}" != "true" ]]; then
  node -e "const fs=require('fs');const p='${TARGET}';const j=JSON.parse(fs.readFileSync(p,'utf8'));if(j.plugins){if(j.plugins.entries){delete j.plugins.entries['self-evolving-plugin-pro'];}if(Array.isArray(j.plugins.allow)){j.plugins.allow=j.plugins.allow.filter(x=>x!=='self-evolving-plugin-pro');}}fs.writeFileSync(p,JSON.stringify(j,null,2)+'\n');"
fi

echo "Plugins installed under ${OPENCLAW_STATE_DIR}/extensions:"
ls -la "${OPENCLAW_STATE_DIR}/extensions" || true

# 5) OpenSpace MCP 注册（L2 已 clone + venv + pip install；这里只做 mcp set / host skills 拷贝）。
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  OPENSPACE_REPO="/opt/OpenSpace"

  OPENSPACE_HOST_SKILLS="${OPENSPACE_REPO}/openspace/host_skills"
  for hs in delegate-task skill-discovery; do
    if [[ -d "${OPENSPACE_HOST_SKILLS}/${hs}" ]]; then
      cp -r "${OPENSPACE_HOST_SKILLS}/${hs}" "${OPENCLAW_STATE_DIR}/skills/${hs}"
    else
      echo "WARN: OpenSpace host skill missing: ${OPENSPACE_HOST_SKILLS}/${hs}" >&2
    fi
  done

  # custom/ → openai/ 重映射（见原脚本详注）
  OPENSPACE_MODEL_VAL="${MODEL_NAME}"
  if [[ "${OPENSPACE_MODEL_VAL}" == custom/* ]]; then
    OPENSPACE_MODEL_VAL="openai/${OPENSPACE_MODEL_VAL#custom/}"
  fi
  if [[ -z "${WORK_OPENAI_BASE_URL:-}" ]]; then
    echo "WARN: WORK_OPENAI_BASE_URL empty; OpenSpace MCP env will omit OPENSPACE_LLM_API_BASE." >&2
  fi
  OPENSPACE_MCP_JSON="$(
    OPENSPACE_REPO="${OPENSPACE_REPO}" \
    OPENSPACE_SKILL_DIRS="${OPENCLAW_STATE_DIR}/skills" \
    OPENSPACE_MODEL_VAL="${OPENSPACE_MODEL_VAL}" \
    OPENSPACE_KEY_VAL="${WORK_OPENAI_API_KEY:-}" \
    OPENSPACE_BASE_VAL="${WORK_OPENAI_BASE_URL:-}" \
    node -e '
const env = {
  OPENSPACE_WORKSPACE: process.env.OPENSPACE_REPO,
  OPENSPACE_HOST_SKILL_DIRS: process.env.OPENSPACE_SKILL_DIRS,
};
if (process.env.OPENSPACE_MODEL_VAL) env.OPENSPACE_MODEL = process.env.OPENSPACE_MODEL_VAL;
if (process.env.OPENSPACE_KEY_VAL) env.OPENSPACE_LLM_API_KEY = process.env.OPENSPACE_KEY_VAL;
if (process.env.OPENSPACE_BASE_VAL) env.OPENSPACE_LLM_API_BASE = process.env.OPENSPACE_BASE_VAL;
process.stdout.write(JSON.stringify({ command: "openspace-mcp", toolTimeout: 600, env }));
'
  )"
  openclaw mcp set openspace "${OPENSPACE_MCP_JSON}" 2>&1 \
    || echo "WARN: 'openclaw mcp set openspace' failed; verify OpenClaw MCP CLI syntax." >&2
fi

# 6) agentmemory fragment merge + MCP register（L2 已 npm 装 + warmup + git clone 到 extensions；
#    这里只做 config 层的 fragment 合并、plugins.allow 并集、mcp set）。
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/agentmemory.fragment.json"
  # plugins.allow 并集（merge 脚本对数组是"替换"语义，用 node 手动做并集）。
  node -e "const fs=require('fs');const p='${TARGET}';const j=JSON.parse(fs.readFileSync(p,'utf8'));j.plugins=j.plugins||{};const a=Array.isArray(j.plugins.allow)?j.plugins.allow:[];if(!a.includes('agentmemory'))a.push('agentmemory');j.plugins.allow=a;fs.writeFileSync(p,JSON.stringify(j,null,2)+'\n');"
  echo "==> agentmemory plugin fragment merged"

  # MCP shim 注册（L2 已 npm -g 装 agentmemory-mcp）。
  AM_MCP_BIN="$(command -v agentmemory-mcp || true)"
  if [[ -z "${AM_MCP_BIN}" ]]; then
    echo "WARN: agentmemory-mcp not on PATH; MCP server will not be registered." >&2
  else
    AM_MCP_JSON="$(node -e 'process.stdout.write(JSON.stringify({command:"agentmemory-mcp",env:{AGENTMEMORY_URL:"http://localhost:3111"}}));')"
    openclaw mcp set agentmemory "${AM_MCP_JSON}" 2>&1 \
      || echo "WARN: 'openclaw mcp set agentmemory' failed; verify OpenClaw MCP CLI syntax." >&2
    echo "==> agentmemory MCP server registered (agentmemory-mcp -> :3111)"
  fi
fi

openclaw plugins list 2>/dev/null || true
