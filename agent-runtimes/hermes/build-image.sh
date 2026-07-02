#!/usr/bin/env bash
# Build evolve-eval-hermes image from agent-runtimes/hermes build context.
#
# The build context is self-contained: legacy/hermes-helper and
# legacy/langfuse-hermes have been copied in-tree under this directory
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

Build evolve-eval-hermes:latest from the upstream Hermes image.

Override via env:
  HERMES_IMAGE            产物 tag，默认 evolve-eval-hermes:latest
  HERMES_BASE_IMAGE_REPO  上游镜像仓库，默认 nousresearch/hermes-agent
  HERMES_BASE_IMAGE_TAG   上游镜像 tag，默认 v2026.5.16（对齐 legacy 版本）
  HERMES_BASE_IMAGE       直接指定完整上游镜像（优先于 REPO:TAG 拼接）
  PIP_INDEX_URL           内网构建时切换 PyPI 源
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

TAG="${HERMES_IMAGE:-evolve-eval-hermes:latest}"
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

echo "==> Building ${TAG} (context: ${AGENT_DIR}, base: ${BASE_IMAGE})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify discovered Hermes paths:"
echo "  docker run --rm ${TAG} cat /opt/evolve-eval/hermes-paths.env"
echo "Verify langfuse + pyyaml installed in Hermes venv:"
echo "  docker run --rm ${TAG} sh -lc '. /opt/evolve-eval/hermes-paths.env; \"\$HERMES_VENV_PY\" -m pip show langfuse pyyaml'"
