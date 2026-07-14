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
Usage: $(basename "$0") [-h|--help]

Build lift-hermes:latest from the upstream Hermes image.

Override via env:
  HERMES_IMAGE            产物 tag，默认 lift-hermes:latest
  HERMES_BASE_IMAGE_REPO  上游镜像仓库，默认 nousresearch/hermes-agent
  HERMES_BASE_IMAGE_TAG   上游镜像 tag，默认 v2026.5.16
  HERMES_BASE_IMAGE       直接指定完整上游镜像（优先于 REPO:TAG 拼接）
  PIP_INDEX_URL           内网构建时切换 PyPI 源
  DOCKER_BUILD_NETWORK    docker build 网络模式，默认 host（构建期复用宿主机
                          DNS/路由/代理，规避 BuildKit 沙箱 DNS 解析失败）；
                          设为空可回退 Docker 默认 bridge
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

TAG="${HERMES_IMAGE:-lift-hermes:latest}"
BASE_IMAGE_REPO="${HERMES_BASE_IMAGE_REPO:-nousresearch/hermes-agent}"
BASE_IMAGE_TAG="${HERMES_BASE_IMAGE_TAG:-v2026.5.16}"
BASE_IMAGE="${HERMES_BASE_IMAGE:-${BASE_IMAGE_REPO}:${BASE_IMAGE_TAG}}"

echo "==> Pulling base image (if needed): ${BASE_IMAGE}"
docker pull "${BASE_IMAGE}" || echo "WARN: docker pull failed; using local image if available"

BUILD_ARGS=(--build-arg "HERMES_BASE_IMAGE=${BASE_IMAGE}")
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  echo "==> Using pip index URL: ${PIP_INDEX_URL}"
fi
# Firecrawl：仅当 .env 提供非空 FIRECRAWL_API_KEY 时才注入镜像并在构建期 init。
if [[ -n "${FIRECRAWL_API_KEY:-}" ]]; then
  BUILD_ARGS+=(--build-arg "FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}")
  echo "==> Baking FIRECRAWL_API_KEY into image + running firecrawl-cli init"
fi

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
