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
AGENTS_FRAGMENT="${CONFIG_DIR}/agents.fragment.json"
AGENTS_RESOLVED="/tmp/agents.fragment.resolved.json"

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

# 3) Ensure required plugins enabled
#    - langfuse-tracer / self-evolving-plugin-pro: 上面已 cp/install 到 extensions
#    - firecrawl: 从 2026.6.10 起从 stock 剥离为 npm 外置插件 @openclaw/firecrawl-plugin，
#      必须 `openclaw plugins install` 才有 web_search provider；运行时只读 FIRECRAWL_API_KEY 鉴权
openclaw plugins enable langfuse-tracer 2>/dev/null || true
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  openclaw plugins enable self-evolving-plugin-pro 2>/dev/null || true
fi
openclaw plugins install @openclaw/firecrawl-plugin
openclaw plugins enable firecrawl 2>/dev/null || true

# self-evolving-plugin-pro 的 review worker 默认 ``--thinking low``；LIFT 统一把
# seed 模型跑成 ``thinking high``（与 work/judge 保持一致），evolve 阶段也升到
# high 以匹配 ARK ``doubao-seed-2-0-pro-260215`` 端点的 ``reasoning_effort`` 语义。
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  WORKER_JS="${OPENCLAW_STATE_DIR}/extensions/self-evolving-plugin-pro/src/review/worker.js"
  if [[ -f "${WORKER_JS}" ]]; then
    sed -i 's/"--thinking", "low"/"--thinking", "high"/g' "${WORKER_JS}" || true
  fi
fi

# 4) Resolve fragments from build-time env.
#    - MODEL_NAME 必须是 provider/model_id 格式（provider 约定固定为 custom）。
#      models.fragment 用斜杠后的 model_id 作为 model.id；agents.fragment 用整串
#      MODEL_NAME 作为 primary / models key（OpenClaw agents add --model 需 provider/model_id）。
#    - WORK_OPENAI_API_KEY 注入 models.provider.apiKey。
MODEL_NAME="${MODEL_NAME:-}"
if [[ "${MODEL_NAME}" != custom/* || "${MODEL_NAME}" == "custom/" ]]; then
  echo "ERROR: MODEL_NAME must be 'custom/model_id' (e.g. custom/ep-xxxx); got '${MODEL_NAME}'" >&2
  exit 1
fi
MODEL_ID="${MODEL_NAME#custom/}"  # custom/ 后的部分作为 model.id

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

# 5) Merge LIFT config fragments (plugins → gateway → agents → skills → models)
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/plugins.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/gateway.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${AGENTS_RESOLVED}"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/skills.fragment.json"
node /tmp/merge-openclaw-config.mjs "${TARGET}" "${MODELS_RESOLVED}"

# raw 镜像：plugins.fragment.json 把 self-evolving-plugin-pro 同时写进 entries 与 allow，
# 这里同时从两处剥掉，避免 gateway 启动时加载缺失扩展或在 allowlist 中保留无效 id。
if [[ "${INSTALL_SELF_EVOLVING}" != "true" ]]; then
  node -e "const fs=require('fs');const p='${TARGET}';const j=JSON.parse(fs.readFileSync(p,'utf8'));if(j.plugins){if(j.plugins.entries){delete j.plugins.entries['self-evolving-plugin-pro'];}if(Array.isArray(j.plugins.allow)){j.plugins.allow=j.plugins.allow.filter(x=>x!=='self-evolving-plugin-pro');}}fs.writeFileSync(p,JSON.stringify(j,null,2)+'\n');"
fi

echo "Plugins installed under ${OPENCLAW_STATE_DIR}/extensions:"
ls -la "${OPENCLAW_STATE_DIR}/extensions" || true

# 6) OpenSpace（基于 MCP 的 quality-first skill hub，README「Path A: For Your Agent」）。
#    默认不装（INSTALL_OPENSPACE=false）。装的话：
#      - git clone 到 /opt（不能落 /tmp：运行期 -v /tmp:/tmp 会遮蔽），sparse 跳过 assets/。
#      - 独立 Python 3.12 venv（镜像系统 python3 是 Debian bookworm 3.11，不满足 OpenSpace >=3.12）。
#      - openspace-mcp 软链到 /usr/local/bin，注册名为 openspace 的 stdio MCP server。
#      - 拷 host skills（delegate-task / skill-discovery）进 agent skills 目录，随 docker commit 落 delta。
INSTALL_OPENSPACE="${INSTALL_OPENSPACE:-false}"
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  OPENSPACE_GIT_URL="${OPENSPACE_GIT_URL:-https://github.com/HKUDS/OpenSpace.git}"
  OPENSPACE_GIT_REF="${OPENSPACE_GIT_REF:-main}"
  OPENSPACE_REPO="/opt/OpenSpace"
  OPENSPACE_VENV="/opt/openspace-venv"

  echo "==> Installing OpenSpace from ${OPENSPACE_GIT_URL}@${OPENSPACE_GIT_REF}"
  # sparse-checkout：跳过 assets/（~50MB 图片），只留代码 + 打包所需资源。
  git clone --filter=blob:none --sparse "${OPENSPACE_GIT_URL}" "${OPENSPACE_REPO}"
  git -C "${OPENSPACE_REPO}" sparse-checkout set --no-cone '/*' '!/assets/'
  git -C "${OPENSPACE_REPO}" checkout "${OPENSPACE_GIT_REF}" || true

  # 确保 uv 可用：OpenClaw 镜像里的 uv 由 self-evolving 插件安装脚本带进来
  # （PATH 中的 /root/.openclaw/uv-bin）。只传 --with-openspace 时该步被跳过，uv 不存在，
  # 故这里缺失就用 pip3 补装（base 镜像已装 python3-pip，PIP_BREAK_SYSTEM_PACKAGES=1）。
  if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv not on PATH; installing uv via pip3"
    pip3 install --no-cache-dir uv || python3 -m pip install --no-cache-dir uv
  fi

  # 独立 uv 管理的 Python 3.12 venv（uv 会按需下载 3.12 解释器）。
  uv venv --python 3.12 "${OPENSPACE_VENV}"
  uv pip install --python "${OPENSPACE_VENV}/bin/python" -e "${OPENSPACE_REPO}"

  # 暴露 CLI 到 PATH（openspace-mcp 为 MCP server 入口；其余 cloud CLI 可选）。
  ln -sf "${OPENSPACE_VENV}/bin/openspace-mcp" /usr/local/bin/openspace-mcp
  for extra in openspace-cloud-auth openspace-download-skill openspace-upload-skill; do
    [[ -x "${OPENSPACE_VENV}/bin/${extra}" ]] && ln -sf "${OPENSPACE_VENV}/bin/${extra}" "/usr/local/bin/${extra}" || true
  done

  # 拷 host skills（教 agent 何时/如何调用 OpenSpace）。skills 目录随 docker commit 落 delta。
  OPENSPACE_HOST_SKILLS="${OPENSPACE_REPO}/openspace/host_skills"
  for hs in delegate-task skill-discovery; do
    if [[ -d "${OPENSPACE_HOST_SKILLS}/${hs}" ]]; then
      cp -r "${OPENSPACE_HOST_SKILLS}/${hs}" "${OPENCLAW_STATE_DIR}/skills/${hs}"
    else
      echo "WARN: OpenSpace host skill missing: ${OPENSPACE_HOST_SKILLS}/${hs}" >&2
    fi
  done

  # 注册 stdio MCP server（README §Setup for openclaw / Path A）。toolTimeout=600 是
  # execute_task 长任务的硬性要求。OPENSPACE_WORKSPACE 指 repo 根；OPENSPACE_HOST_SKILL_DIRS
  # 指 agent skills 目录（OpenSpace 从这里 discover 本地 skill）。
  #
  # LLM 凭据必须进 MCP server 的 env 块：mcporter/MCP TS SDK 只把 PATH/HOME 等安全白名单
  # 合并进 env 块后传给 stdio 子进程，不继承容器任意环境变量，故仅靠 docker run -e 注入的
  # OPENSPACE_* 到不了 openspace-mcp 进程。这里从构建期 env bake 进 env 块（与
  # models.fragment.json 已 bake WORK_OPENAI_API_KEY 的做法一致）：
  #   - OPENSPACE_MODEL       ← MODEL_NAME，但把 custom/ 前缀重映射为 openai/（见下）
  #   - OPENSPACE_LLM_API_KEY ← WORK_OPENAI_API_KEY
  #   - OPENSPACE_LLM_API_BASE← WORK_OPENAI_BASE_URL
  #
  # 为何 custom/ → openai/：OpenSpace 用 litellm 路由模型，litellm 不认 custom 这个
  # provider（实测 model=custom/ep-xxx 会 AuthenticationError: API key/AK/SK missing
  # or invalid，进而误触发 OpenSpace cloud 登录）。本项目 custom/ 约定 = "OpenAI 兼容
  # 自定义端点"，对 litellm 等价于 openai/<model_id> + 显式 api_base + api_key。OpenClaw
  # 自身仍用 custom/（models.fragment.json 注册了 custom provider），二者互不影响。
  # 仅注入非空值；用 node JSON.stringify 安全转义（key/url 可能含特殊字符）。当前 OpenClaw
  # 版本若不认 `openclaw mcp set` 语法，容错打印诊断，不阻断构建。
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

  echo "==> Verifying openspace-mcp:"
  openspace-mcp --help >/dev/null 2>&1 \
    && echo "OK: openspace-mcp importable" \
    || echo "WARN: 'openspace-mcp --help' failed; check OpenSpace install." >&2
else
  echo "INSTALL_OPENSPACE=${INSTALL_OPENSPACE}: skip OpenSpace MCP plugin install"
fi

# 7) agentmemory memory plugin（README「Option 2: OpenClaw memory plugin，deeper integration」）。
#    默认不装（INSTALL_AGENTMEMORY=false）。装的话：
#      - 校验 Node >= 20（OpenClaw 基镜像本身是 Node 应用，通常已满足；加断言防版本漂移）。
#      - npm -g 装 @agentmemory/agentmemory（server + CLI）。
#      - 构建期预热：后台起 server 把 iii-engine 二进制拉进 ~/.agentmemory/bin、把本地嵌入
#        模型 all-MiniLM-L6-v2 拉进缓存，随后清空记忆状态（保留 bin/ 与模型缓存），保证运行期
#        离线可用且 baseline 从空记忆开始（不污染评测）。
#      - git clone agentmemory，把 integrations/openclaw 拷进 extensions/agentmemory。
#      - merge agentmemory.fragment.json（claim plugins.slots.memory = agentmemory）。
INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY:-false}"
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  AGENTMEMORY_GIT_URL="${AGENTMEMORY_GIT_URL:-https://github.com/rohitg00/agentmemory.git}"
  AGENTMEMORY_GIT_REF="${AGENTMEMORY_GIT_REF:-main}"
  AGENTMEMORY_SRC="/opt/agentmemory-src"
  [[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && export NPM_CONFIG_REGISTRY

  echo "==> Installing agentmemory memory plugin (offline local embeddings)"

  # 7a) Node >= 20 断言。
  if ! command -v node >/dev/null 2>&1; then
    echo "ERROR: node not found on PATH; agentmemory requires Node.js >= 20." >&2
    exit 1
  fi
  NODE_MAJOR_VER="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [[ "${NODE_MAJOR_VER}" -lt 20 ]]; then
    echo "ERROR: Node.js >= 20 required for agentmemory; got $(node -v 2>/dev/null)." >&2
    exit 1
  fi
  echo "==> Node $(node -v) OK (>= 20)"

  # 7b) 装 agentmemory server + CLI。
  npm install -g @agentmemory/agentmemory

  # 7c) 构建期预热引擎 + 本地嵌入模型，然后清空记忆状态（保留引擎/模型缓存）。
  echo "==> Warming up agentmemory engine + local embedding model (build-time, networked)"
  export CI=1
  export HOME="${HOME:-/root}"
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
    echo "==> agentmemory warmup server ready; triggering demo to fetch engine/model, then reset"
    # demo 会拉起完整栈并做一次真实检索，确保 iii-engine 二进制 + 嵌入模型落地缓存。
    agentmemory demo >/tmp/agentmemory-demo.log 2>&1 || echo "WARN: agentmemory demo returned non-zero (non-fatal)" >&2
  else
    echo "WARN: agentmemory warmup server not ready in time; engine/model may fetch at first runtime start." >&2
    cat /tmp/agentmemory-warmup.log 2>/dev/null || true
  fi
  # 停掉后台 server（best-effort），随后清记忆状态。
  pkill -f agentmemory 2>/dev/null || true
  sleep 2
  # 清空记忆数据但保留引擎二进制（bin/）与模型缓存：删除 ~/.agentmemory 下除 bin/ 与 models
  # 缓存以外的状态（DB / observations / logs）。精确目录随版本可能变化，采用保守白名单删法。
  if [[ -d "${HOME}/.agentmemory" ]]; then
    find "${HOME}/.agentmemory" -maxdepth 1 -mindepth 1 \
      ! -name bin \
      ! -name models \
      ! -name model-cache \
      ! -name '.cache' \
      -exec rm -rf {} + 2>/dev/null || true
    echo "==> Reset agentmemory memory state (kept engine binary + model cache)"
  fi

  # 7d) 落插件：git clone，sparse 仅取 integrations/openclaw，拷进 extensions/agentmemory。
  git clone --filter=blob:none --sparse "${AGENTMEMORY_GIT_URL}" "${AGENTMEMORY_SRC}"
  git -C "${AGENTMEMORY_SRC}" sparse-checkout set --no-cone '/integrations/openclaw'
  git -C "${AGENTMEMORY_SRC}" checkout "${AGENTMEMORY_GIT_REF}" || true
  AM_PLUGIN_SRC="${AGENTMEMORY_SRC}/integrations/openclaw"
  if [[ ! -d "${AM_PLUGIN_SRC}" ]]; then
    echo "ERROR: agentmemory integrations/openclaw not found after clone (${AM_PLUGIN_SRC})." >&2
    exit 1
  fi
  rm -rf "${OPENCLAW_STATE_DIR}/extensions/agentmemory"
  cp -r "${AM_PLUGIN_SRC}" "${OPENCLAW_STATE_DIR}/extensions/agentmemory"
  # 校验插件必需文件。
  for f in package.json openclaw.plugin.json plugin.mjs; do
    if [[ ! -f "${OPENCLAW_STATE_DIR}/extensions/agentmemory/${f}" ]]; then
      echo "WARN: agentmemory plugin missing ${f}; plugin may fail to load." >&2
    fi
  done

  # 7e) merge fragment：claim plugins.slots.memory = agentmemory（在 models fragment 之后）。
  node /tmp/merge-openclaw-config.mjs "${TARGET}" "${CONFIG_DIR}/agentmemory.fragment.json"
  # 把 agentmemory 并入 plugins.allow（merge 脚本对数组是"替换"语义，故用 node 做并集，
  # 避免覆盖既有 langfuse-tracer / firecrawl 等 allowlist 项）。
  node -e "const fs=require('fs');const p='${TARGET}';const j=JSON.parse(fs.readFileSync(p,'utf8'));j.plugins=j.plugins||{};const a=Array.isArray(j.plugins.allow)?j.plugins.allow:[];if(!a.includes('agentmemory'))a.push('agentmemory');j.plugins.allow=a;fs.writeFileSync(p,JSON.stringify(j,null,2)+'\n');"
  echo "==> agentmemory plugin installed under ${OPENCLAW_STATE_DIR}/extensions/agentmemory"
else
  echo "INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY}: skip agentmemory memory plugin install"
fi

openclaw plugins list 2>/dev/null || true
