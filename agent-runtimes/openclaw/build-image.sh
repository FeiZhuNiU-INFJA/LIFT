#!/usr/bin/env bash
# Build evolve-eval-openclaw image from agent-runtimes/openclaw build context.
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

# 默认走官方 ghcr.io；国内拉取慢时可设 OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest 切到加速源
BASE_IMAGE="${OPENCLAW_BASE_IMAGE:-ghcr.io/openclaw/openclaw:latest}"
TAG="${OPENCLAW_IMAGE:-evolve-eval-openclaw:latest}"
ARK_API_KEY="${ARK_API_KEY:-}"

if [[ -z "${ARK_API_KEY}" ]]; then
  echo "WARN: ARK_API_KEY is not set; image will bake models fragment without a real apiKey." >&2
  echo "      Set ARK_API_KEY in repo root .env before build." >&2
fi

echo "==> Pulling base image (if needed): ${BASE_IMAGE}"
docker pull "${BASE_IMAGE}" || echo "WARN: docker pull failed; using local image if available"

BUILD_ARGS=(--build-arg "OPENCLAW_BASE_IMAGE=${BASE_IMAGE}")
if [[ -n "${ARK_API_KEY}" ]]; then
  BUILD_ARGS+=(--build-arg "ARK_API_KEY=${ARK_API_KEY}")
  echo "==> Baking Ark provider (apiKey from ARK_API_KEY) into image"
fi

echo "==> Building ${TAG} (context: ${AGENT_DIR})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify plugins:"
echo "  docker run --rm ${TAG} openclaw plugins list"
