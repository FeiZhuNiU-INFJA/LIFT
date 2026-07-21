#!/usr/bin/env bash
# Build lift-openclaw image (base 或 with-evolve) from agent-runtimes/openclaw build context.
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

# 内网自动探测：未显式设 APT_MIRROR / PIP_INDEX_URL 时，如果能解析 mirrors.byted.org
# 就默认切到字节内网源，避免手动 export。用 LIFT_INTRANET_AUTODETECT=0 关闭。
if [[ "${LIFT_INTRANET_AUTODETECT:-1}" != "0" ]] \
   && [[ -z "${APT_MIRROR:-}" || -z "${PIP_INDEX_URL:-}" ]] \
   && getent hosts mirrors.byted.org >/dev/null 2>&1 \
   && curl -fsSL --max-time 3 -o /dev/null "https://bytedpypi.byted.org/simple/pip/" 2>/dev/null; then
  : "${APT_MIRROR:=http://mirrors.byted.org}"
  : "${PIP_INDEX_URL:=https://bytedpypi.byted.org/simple/}"
  export APT_MIRROR PIP_INDEX_URL
  echo "==> Intranet detected; defaulting APT_MIRROR / PIP_INDEX_URL to ByteDance mirrors (LIFT_INTRANET_AUTODETECT=0 to disable)"
fi

# 是否安装并启用 self-evolving-plugin-pro。默认 false（base 镜像）；传 --with-evolve 构建 with-evolve 镜像
INSTALL_SELF_EVOLVING="false"
# 是否安装 OpenSpace（基于 MCP 的 skill hub）。默认 false；传 --with-openspace 启用。
# 与 --with-evolve 互斥（两种进化插件二选一，不可叠加）。
INSTALL_OPENSPACE="false"
usage() {
  cat <<EOF
Usage: $(basename "$0") [--with-evolve | --with-openspace] [-h|--help]

Options:
  --with-evolve     构建带 self-evolving-plugin-pro 的镜像（默认不带）
  --with-openspace  构建带 OpenSpace MCP 插件的镜像（默认不带）
  -h, --help        显示本帮助

注意：--with-evolve 与 --with-openspace 互斥，只能二选一（不能同时传）。

Tag 规则（OPENCLAW_IMAGE 覆盖优先）：
  (无)              → lift-openclaw-base:latest
  --with-evolve     → lift-openclaw-with-evolve:latest
  --with-openspace  → lift-openclaw-with-openspace:latest

OpenSpace 源可用 env 覆盖：OPENSPACE_GIT_URL（默认 https://github.com/HKUDS/OpenSpace.git）、
OPENSPACE_GIT_REF（默认 main）。
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-evolve)
      INSTALL_SELF_EVOLVING="true"
      shift
      ;;
    --with-openspace)
      INSTALL_OPENSPACE="true"
      shift
      ;;
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

# --with-evolve 与 --with-openspace 互斥：两种进化插件二选一，不允许叠加。
if [[ "${INSTALL_SELF_EVOLVING}" == "true" && "${INSTALL_OPENSPACE}" == "true" ]]; then
  echo "ERROR: --with-evolve 与 --with-openspace 互斥，只能二选一（不能同时传）。" >&2
  usage >&2
  exit 2
fi

# 默认走官方 ghcr.io；国内拉取慢时可设 OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest 切到加速源
BASE_IMAGE="${OPENCLAW_BASE_IMAGE:-ghcr.io/openclaw/openclaw:latest}"
# tag 由二选一的插件决定；OPENCLAW_IMAGE 显式覆盖优先。
if [[ -n "${OPENCLAW_IMAGE:-}" ]]; then
  TAG="${OPENCLAW_IMAGE}"
elif [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  TAG="lift-openclaw-with-evolve:latest"
elif [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  TAG="lift-openclaw-with-openspace:latest"
else
  TAG="lift-openclaw-base:latest"
fi
WORK_OPENAI_API_KEY="${WORK_OPENAI_API_KEY:-}"
WORK_OPENAI_BASE_URL="${WORK_OPENAI_BASE_URL:-}"
MODEL_NAME="${MODEL_NAME:-}"

if [[ -z "${WORK_OPENAI_API_KEY}" ]]; then
  echo "WARN: WORK_OPENAI_API_KEY is not set; image will bake models fragment without a real apiKey." >&2
  echo "      Set WORK_OPENAI_API_KEY in repo root .env before build." >&2
fi
if [[ "${MODEL_NAME}" != custom/* || "${MODEL_NAME}" == "custom/" ]]; then
  echo "ERROR: MODEL_NAME must be 'custom/model_id' (e.g. custom/ep-xxxx); got '${MODEL_NAME}'." >&2
  echo "       Set MODEL_NAME in repo root .env before build." >&2
  exit 1
fi

echo "==> Pulling base image (if needed): ${BASE_IMAGE}"
docker pull "${BASE_IMAGE}" || echo "WARN: docker pull failed; using local image if available"

BUILD_ARGS=(--build-arg "OPENCLAW_BASE_IMAGE=${BASE_IMAGE}")
BUILD_ARGS+=(--build-arg "INSTALL_SELF_EVOLVING=${INSTALL_SELF_EVOLVING}")
BUILD_ARGS+=(--build-arg "INSTALL_OPENSPACE=${INSTALL_OPENSPACE}")
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  [[ -n "${OPENSPACE_GIT_URL:-}" ]] && BUILD_ARGS+=(--build-arg "OPENSPACE_GIT_URL=${OPENSPACE_GIT_URL}")
  [[ -n "${OPENSPACE_GIT_REF:-}" ]] && BUILD_ARGS+=(--build-arg "OPENSPACE_GIT_REF=${OPENSPACE_GIT_REF}")
  echo "==> OpenSpace MCP plugin enabled (git ${OPENSPACE_GIT_URL:-default}@${OPENSPACE_GIT_REF:-main})"
fi
BUILD_ARGS+=(--build-arg "MODEL_NAME=${MODEL_NAME}")
if [[ -n "${WORK_OPENAI_API_KEY}" ]]; then
  BUILD_ARGS+=(--build-arg "WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY}")
  echo "==> Baking model provider (apiKey from WORK_OPENAI_API_KEY) into image"
fi
# OpenSpace 的 LLM base URL（OPENSPACE_LLM_API_BASE）由构建期 WORK_OPENAI_BASE_URL bake 进
# openspace MCP env 块（install-plugins-in-image.sh）。仅 --with-openspace 时相关；空值时
# OpenSpace 走其默认解析，故非致命，只在启用 OpenSpace 且为空时提醒。
if [[ -n "${WORK_OPENAI_BASE_URL}" ]]; then
  BUILD_ARGS+=(--build-arg "WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL}")
  [[ "${INSTALL_OPENSPACE}" == "true" ]] && echo "==> Baking OpenSpace LLM base URL (from WORK_OPENAI_BASE_URL) into MCP env"
elif [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  echo "WARN: WORK_OPENAI_BASE_URL is empty; OpenSpace MCP env will omit OPENSPACE_LLM_API_BASE." >&2
  echo "      Set WORK_OPENAI_BASE_URL in repo root .env before build for OpenSpace to reach your endpoint." >&2
fi
# APT_MIRROR / PIP_INDEX_URL 默认走公网；内网构建时通过环境变量传入即可（详见 README）。
if [[ -n "${APT_MIRROR:-}" ]]; then
  BUILD_ARGS+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
  echo "==> Using APT mirror: ${APT_MIRROR}"
fi
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  echo "==> Using pip index URL: ${PIP_INDEX_URL}"
fi

echo "==> Building ${TAG} (context: ${AGENT_DIR})"
# Build-time network mode. BuildKit 沙箱默认不继承宿主 DNS/路由，内网环境下
# uv 拉 python-build-standalone（github）等公网资源易卡死/超时。默认走 host 网络，
# 让 RUN 步骤复用宿主 DNS/路由/代理（与 hermes/build-image.sh 一致）。仅影响构建期
# RUN，不改最终镜像与运行期网络。设 DOCKER_BUILD_NETWORK= (空) 可回退 Docker 默认 bridge。
BUILD_NETWORK="${DOCKER_BUILD_NETWORK-host}"
NETWORK_ARGS=()
if [[ -n "${BUILD_NETWORK}" ]]; then
  NETWORK_ARGS+=(--network "${BUILD_NETWORK}")
  echo "==> Using docker build network: ${BUILD_NETWORK}"
fi
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${NETWORK_ARGS[@]}" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify plugins:"
echo "  docker run --rm ${TAG} openclaw plugins list"
