#!/usr/bin/env bash
# Build lift-hermes image from agent-runtimes/hermes build context.
#
# The build context is self-contained: runner and plugin assets are maintained
# in-tree under agent-runtimes/hermes.
# (see .trae/documents/hermes_runtime_integration_plan.md §A).
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

usage() {
  cat <<EOF
Usage: $(basename "$0") [--with-openspace | --with-agentmemory] [-h|--help]

Build lift-hermes:latest from the upstream Hermes image.

Options:
  --with-openspace    构建带 OpenSpace MCP 插件的镜像（默认不带；产物 lift-hermes-with-openspace:latest）
  --with-agentmemory  构建带 agentmemory memory provider plugin 的镜像（默认不带；产物 lift-hermes-with-agentmemory:latest；离线本地嵌入）
  -h, --help          显示本帮助

注意：--with-openspace 与 --with-agentmemory 互斥，只能二选一（不能同时传）。

Override via env:
  HERMES_IMAGE            产物 tag，默认 lift-hermes:latest（--with-openspace → lift-hermes-with-openspace:latest；--with-agentmemory → lift-hermes-with-agentmemory:latest）
  HERMES_BASE_IMAGE_REPO  上游镜像仓库，默认 nousresearch/hermes-agent
  HERMES_BASE_IMAGE_TAG   上游镜像 tag，默认 v2026.5.16
  HERMES_BASE_IMAGE       直接指定完整上游镜像（优先于 REPO:TAG 拼接）
  PIP_INDEX_URL           内网构建时切换 PyPI 源
  OPENSPACE_GIT_URL       OpenSpace 源（默认 https://github.com/HKUDS/OpenSpace.git）
  OPENSPACE_GIT_REF       OpenSpace ref（默认 main）
  UV_PYTHON_INSTALL_MIRROR
                          uv 拉 python-build-standalone cpython 的 GitHub releases
                          反代（默认 https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download，
                          可指向内网镜像；受限网络下必需，否则 uv venv 会静默挂死）
  AGENTMEMORY_GIT_URL     agentmemory 源（默认 https://github.com/rohitg00/agentmemory.git）
  AGENTMEMORY_GIT_REF     agentmemory ref（默认 main）
  NPM_CONFIG_REGISTRY     npm registry（内网可覆盖；默认公网）
  DOCKER_BUILD_NETWORK    docker build 网络模式，默认 host（构建期复用宿主机
                          DNS/路由/代理，规避 BuildKit 沙箱 DNS 解析失败）；
                          设为空可回退 Docker 默认 bridge
EOF
}
INSTALL_OPENSPACE="false"
INSTALL_AGENTMEMORY="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-openspace) INSTALL_OPENSPACE="true"; shift ;;
    --with-agentmemory) INSTALL_AGENTMEMORY="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# --with-openspace 与 --with-agentmemory 互斥。
if [[ "${INSTALL_OPENSPACE}" == "true" && "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  echo "ERROR: --with-openspace 与 --with-agentmemory 互斥，只能二选一（不能同时传）。" >&2
  usage >&2
  exit 2
fi

if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  TAG="${HERMES_IMAGE:-lift-hermes-with-openspace:latest}"
elif [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  TAG="${HERMES_IMAGE:-lift-hermes-with-agentmemory:latest}"
else
  TAG="${HERMES_IMAGE:-lift-hermes:latest}"
fi
BASE_IMAGE_REPO="${HERMES_BASE_IMAGE_REPO:-nousresearch/hermes-agent}"
BASE_IMAGE_TAG="${HERMES_BASE_IMAGE_TAG:-v2026.5.16}"
BASE_IMAGE="${HERMES_BASE_IMAGE:-${BASE_IMAGE_REPO}:${BASE_IMAGE_TAG}}"

echo "==> Pulling base image (if needed): ${BASE_IMAGE}"
docker pull "${BASE_IMAGE}" || echo "WARN: docker pull failed; using local image if available"

BUILD_ARGS=(--build-arg "HERMES_BASE_IMAGE=${BASE_IMAGE}")
BUILD_ARGS+=(--build-arg "INSTALL_OPENSPACE=${INSTALL_OPENSPACE}")
if [[ "${INSTALL_OPENSPACE}" == "true" ]]; then
  [[ -n "${OPENSPACE_GIT_URL:-}" ]] && BUILD_ARGS+=(--build-arg "OPENSPACE_GIT_URL=${OPENSPACE_GIT_URL}")
  [[ -n "${OPENSPACE_GIT_REF:-}" ]] && BUILD_ARGS+=(--build-arg "OPENSPACE_GIT_REF=${OPENSPACE_GIT_REF}")
  [[ -n "${UV_PYTHON_INSTALL_MIRROR:-}" ]] && BUILD_ARGS+=(--build-arg "UV_PYTHON_INSTALL_MIRROR=${UV_PYTHON_INSTALL_MIRROR}")
  echo "==> OpenSpace MCP plugin enabled (git ${OPENSPACE_GIT_URL:-default}@${OPENSPACE_GIT_REF:-main})"
fi
BUILD_ARGS+=(--build-arg "INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY}")
if [[ "${INSTALL_AGENTMEMORY}" == "true" ]]; then
  [[ -n "${AGENTMEMORY_GIT_URL:-}" ]] && BUILD_ARGS+=(--build-arg "AGENTMEMORY_GIT_URL=${AGENTMEMORY_GIT_URL}")
  [[ -n "${AGENTMEMORY_GIT_REF:-}" ]] && BUILD_ARGS+=(--build-arg "AGENTMEMORY_GIT_REF=${AGENTMEMORY_GIT_REF}")
  [[ -n "${NPM_CONFIG_REGISTRY:-}" ]] && BUILD_ARGS+=(--build-arg "NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}")
  echo "==> agentmemory memory provider plugin enabled (git ${AGENTMEMORY_GIT_URL:-default}@${AGENTMEMORY_GIT_REF:-main}; offline local embeddings)"
fi
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  echo "==> Using pip index URL: ${PIP_INDEX_URL}"
fi
# Firecrawl：仅当 .env 提供非空 FIRECRAWL_API_KEY 时才注入镜像并在构建期 init。
if [[ -n "${FIRECRAWL_API_KEY:-}" ]]; then
  BUILD_ARGS+=(--build-arg "FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}")
  echo "==> Baking FIRECRAWL_API_KEY into image + running firecrawl-cli init"
fi

# Propagate host HTTP(S) proxy into build ARGs. BuildKit RUN sandboxes do NOT
# inherit host env vars, so `git clone github.com` etc. can hang on restricted
# networks even under --network host. Injecting http_proxy / https_proxy /
# no_proxy as --build-arg makes them visible as env inside every RUN step.
for var in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
  val="${!var:-}"
  if [[ -n "${val}" ]]; then
    BUILD_ARGS+=(--build-arg "${var}=${val}")
    echo "==> Forwarding ${var} to build args"
  fi
done

# Build-time network mode. The BuildKit sandbox does NOT inherit host DNS by
# default, so uv/git/npx can fail with "dns error / failed to lookup address"
# even when the host resolves fine (restricted networks / corporate proxy).
# Default to host networking so the build reuses the host's DNS/route/proxy.
# NOTE: affects the RUN steps only — the final image and runtime container
# networking are unchanged. Under host net, `localhost` inside RUN means the
# HOST (we don't rely on that). Override with DOCKER_BUILD_NETWORK= (empty) to
# fall back to Docker's default bridge (e.g. if the daemon forbids host net).
BUILD_NETWORK="${DOCKER_BUILD_NETWORK-host}"
NETWORK_ARGS=()
if [[ -n "${BUILD_NETWORK}" ]]; then
  NETWORK_ARGS+=(--network "${BUILD_NETWORK}")
  echo "==> Using docker build network: ${BUILD_NETWORK}"
fi

echo "==> Building ${TAG} (context: ${AGENT_DIR}, base: ${BASE_IMAGE})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${NETWORK_ARGS[@]}" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify discovered Hermes paths:"
echo "  docker run --rm ${TAG} cat /opt/lift/hermes-paths.env"
echo "Verify langfuse + pyyaml installed in Hermes venv:"
echo "  docker run --rm ${TAG} sh -lc '. /opt/lift/hermes-paths.env; \"\$HERMES_VENV_PY\" -m pip show langfuse pyyaml'"
