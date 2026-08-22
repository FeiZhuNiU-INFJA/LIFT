#!/usr/bin/env bash
# Build lift-prime-agent image (PrimeIntellect-ai/prime-agent, bin `prime-agent`).
#
# 从通用 node base（含 python3 常驻内核依赖）装起 —— 与 genericagent 的
# "install upstream + bake secrets" 模式对齐。prime-agent 以 Prime Intellect 官方
# R2 发布 tarball 分发（非公共 npm registry），故用 download→verify→npm install -g。
# 自进化状态落在容器内 ${PRIME_AGENT_CODING_AGENT_DIR}（=/root/.prime/agent），
# global harness 在 ${DIR}/harness/harness_state.json，由 adapter 侧 docker commit
# 带入 delta。
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${AGENT_DIR}/../.." && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found on PATH" >&2
  exit 127
fi

if ! docker info >/dev/null 2>&1; then
  echo "Cannot connect to Docker daemon. Add user to docker group or run with sudo." >&2
  exit 1
fi

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT}/.env"
  set +a
fi

# 内网自动探测：与其它 build-image.sh 对齐，默认走公网，检出字节内网时切镜像源。
# LIFT_INTRANET_AUTODETECT=0 关闭。
if [[ "${LIFT_INTRANET_AUTODETECT:-1}" != "0" ]] \
   && [[ -z "${APT_MIRROR:-}" || -z "${PIP_INDEX_URL:-}" ]] \
   && getent hosts mirrors.byted.org >/dev/null 2>&1 \
   && curl -fsSL --max-time 3 -o /dev/null "https://bytedpypi.byted.org/simple/pip/" 2>/dev/null; then
  : "${APT_MIRROR:=http://mirrors.byted.org}"
  : "${PIP_INDEX_URL:=https://bytedpypi.byted.org/simple/}"
  export APT_MIRROR PIP_INDEX_URL
  echo "==> Intranet detected; defaulting APT_MIRROR / PIP_INDEX_URL to ByteDance mirrors (LIFT_INTRANET_AUTODETECT=0 to disable)"
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") [-h|--help]

Build lift-prime-agent:latest by downloading + verifying the Prime Agent release
tarball (bin \`prime-agent\`) from Prime Intellect's R2 and \`npm install -g\`-ing it
onto a node base image, then baking WORK_OPENAI / Langfuse credentials from repo
root .env into the image (models.json / settings.json).

Override via env:
  PRIME_AGENT_IMAGE               默认 lift-prime-agent:latest
  PRIME_AGENT_BASE_IMAGE          默认 node:22-bookworm-slim
  PRIME_AGENT_VERSION             默认 0.7.3（发布版本号，去 v 前缀）
  PRIME_AGENT_DOWNLOAD_BASE_URL   默认官方 R2（可指向内网镜像）
  PRIME_AGENT_PACKAGE             默认 prime-agent
  PRIME_AGENT_VERIFY_CHECKSUM     默认 1（0 关闭 SHA256 校验）
  PRIME_AGENT_MODEL_NAME          override provider-native model id（否则取 MODEL_NAME 去 custom/ 前缀）
  FIRECRAWL_API_KEY               可选：Firecrawl 远程 MCP 静态 bearer token（联网抓取/搜索；空则 skill 加载但调用抛 NotEnabled）
  APT_MIRROR / PIP_INDEX_URL      内网构建时切换上游（脚本会自动探测字节内网）
  LIFT_INTRANET_AUTODETECT=0      关闭内网自动探测
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

TAG="${PRIME_AGENT_IMAGE:-lift-prime-agent:latest}"
BASE_IMAGE="${PRIME_AGENT_BASE_IMAGE:-node:22-bookworm-slim}"
PA_VERSION="${PRIME_AGENT_VERSION:-0.7.3}"
PA_DOWNLOAD_BASE_URL="${PRIME_AGENT_DOWNLOAD_BASE_URL:-https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev}"
PA_PACKAGE="${PRIME_AGENT_PACKAGE:-prime-agent}"
PA_VERIFY_CHECKSUM="${PRIME_AGENT_VERIFY_CHECKSUM:-1}"
WORK_OPENAI_API_KEY="${WORK_OPENAI_API_KEY:-}"
WORK_OPENAI_BASE_URL="${WORK_OPENAI_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"

# LIFT 约定：MODEL_NAME=custom/<model_id>；剥掉 custom/ 前缀作为 provider-native id。
# PRIME_AGENT_MODEL_NAME 可显式覆盖为 provider-native id。
RAW_MODEL_NAME="${MODEL_NAME:-}"
if [[ -n "${RAW_MODEL_NAME}" && ( "${RAW_MODEL_NAME}" != custom/* || "${RAW_MODEL_NAME}" == "custom/" ) ]]; then
  echo "WARN: MODEL_NAME must be 'custom/model_id' (e.g. custom/ep-xxxx); got '${RAW_MODEL_NAME}'." >&2
fi
if [[ -n "${PRIME_AGENT_MODEL_NAME:-}" ]]; then
  MODEL_NAME="${PRIME_AGENT_MODEL_NAME}"
elif [[ "${RAW_MODEL_NAME}" == custom/* && "${RAW_MODEL_NAME}" != "custom/" ]]; then
  MODEL_NAME="${RAW_MODEL_NAME#custom/}"
else
  MODEL_NAME="${RAW_MODEL_NAME}"
fi

LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_BASE_URL:-http://host.docker.internal:3000}}"
# 宿主 .env 常配 localhost:PORT / 127.0.0.1:PORT；容器 loopback 不通宿主。
# host 段改写为 host.docker.internal，端口/协议/路径保留（同其它 runtime）。
LANGFUSE_HOST="$(python3 - <<PY
from urllib.parse import urlsplit, urlunsplit
raw = "${LANGFUSE_HOST}".strip()
if not raw:
    print("http://host.docker.internal:3000")
else:
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host in {"localhost", "127.0.0.1"}:
        port = parts.port
        netloc = "host.docker.internal" if port is None else f"host.docker.internal:{port}"
        print(urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, parts.fragment)))
    else:
        print(raw)
PY
)"
echo "==> LANGFUSE_HOST baked: ${LANGFUSE_HOST}"

REASONING_EFFORT="${REASONING_EFFORT:-high}"

# Firecrawl 远程 MCP（路线一）：holdout 容器允许出公网时，透传 FIRECRAWL_API_KEY 让
# kernel 内的 firecrawl skill 能联网抓取/搜索（对齐 genericagent 的 build-arg 透传约定）。
# 置空则 skill 仍加载，但调用抛 NotEnabled；不阻断构建。
FIRECRAWL_API_KEY="${FIRECRAWL_API_KEY:-}"

if [[ -z "${WORK_OPENAI_API_KEY}" ]]; then
  echo "WARN: WORK_OPENAI_API_KEY is not set; image will be built with empty api key." >&2
fi
if [[ -z "${MODEL_NAME}" ]]; then
  echo "WARN: MODEL_NAME is not set; Prime Agent provider config model field will be empty." >&2
fi
if [[ -z "${FIRECRAWL_API_KEY}" ]]; then
  echo "WARN: FIRECRAWL_API_KEY is not set; firecrawl MCP skill will load but raise NotEnabled on call (no web access)." >&2
fi

BUILD_ARGS=(
  --build-arg "PRIME_AGENT_BASE_IMAGE=${BASE_IMAGE}"
  --build-arg "PRIME_AGENT_VERSION=${PA_VERSION}"
  --build-arg "PRIME_AGENT_DOWNLOAD_BASE_URL=${PA_DOWNLOAD_BASE_URL}"
  --build-arg "PRIME_AGENT_PACKAGE=${PA_PACKAGE}"
  --build-arg "PRIME_AGENT_VERIFY_CHECKSUM=${PA_VERIFY_CHECKSUM}"
  --build-arg "WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY}"
  --build-arg "WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL}"
  --build-arg "MODEL_NAME=${MODEL_NAME}"
  --build-arg "LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY}"
  --build-arg "LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY}"
  --build-arg "LANGFUSE_HOST=${LANGFUSE_HOST}"
  --build-arg "REASONING_EFFORT=${REASONING_EFFORT}"
  --build-arg "FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}"
)

if [[ -n "${APT_MIRROR:-}" ]]; then
  BUILD_ARGS+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
  echo "==> Using APT mirror: ${APT_MIRROR}"
fi
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  echo "==> Using pip index URL: ${PIP_INDEX_URL}"
fi

# 宿主代理透传：内网构建时 npm/curl 需走公司代理才能访问公网 R2 / GitHub。
# Docker 对 build-arg 名 http_proxy/https_proxy/no_proxy（含大写）有内建支持。
# 设 LIFT_BUILD_PROXY_AUTODETECT=0 关闭。
if [[ "${LIFT_BUILD_PROXY_AUTODETECT:-1}" != "0" ]]; then
  _proxy_http="${http_proxy:-${HTTP_PROXY:-}}"
  _proxy_https="${https_proxy:-${HTTPS_PROXY:-}}"
  _proxy_no="${no_proxy:-${NO_PROXY:-}}"
  if [[ -n "${_proxy_http}" || -n "${_proxy_https}" ]]; then
    [[ -n "${_proxy_http}" ]]  && BUILD_ARGS+=(--build-arg "http_proxy=${_proxy_http}"  --build-arg "HTTP_PROXY=${_proxy_http}")
    [[ -n "${_proxy_https}" ]] && BUILD_ARGS+=(--build-arg "https_proxy=${_proxy_https}" --build-arg "HTTPS_PROXY=${_proxy_https}")
    [[ -n "${_proxy_no}" ]]    && BUILD_ARGS+=(--build-arg "no_proxy=${_proxy_no}"    --build-arg "NO_PROXY=${_proxy_no}")
    echo "==> Forwarding host proxy to docker build (http=${_proxy_http:-<unset>} https=${_proxy_https:-<unset>} no=${_proxy_no:-<unset>})"
  fi
fi

echo "==> Building ${TAG} (context: ${AGENT_DIR}, base: ${BASE_IMAGE}, pkg: ${PA_PACKAGE}@${PA_VERSION})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify prime-agent CLI:"
echo "  docker run --rm ${TAG} prime-agent --version"
