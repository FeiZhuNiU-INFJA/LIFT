#!/usr/bin/env bash
# Build evolve-eval-openclaw image (base 或 with-evolve) from agent-runtimes/openclaw build context.
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
# 是否安装并启用 self-evolving-plugin-pro。默认 true（with-evolve 镜像）；设 false 构建 base 镜像（不带进化插件）
INSTALL_SELF_EVOLVING="${INSTALL_SELF_EVOLVING:-true}"
if [[ "${INSTALL_SELF_EVOLVING}" == "true" ]]; then
  TAG="${OPENCLAW_IMAGE:-evolve-eval-openclaw-with-evolve:latest}"
else
  TAG="${OPENCLAW_IMAGE:-evolve-eval-openclaw-base:latest}"
fi
ARK_API_KEY="${ARK_API_KEY:-}"

if [[ -z "${ARK_API_KEY}" ]]; then
  echo "WARN: ARK_API_KEY is not set; image will bake models fragment without a real apiKey." >&2
  echo "      Set ARK_API_KEY in repo root .env before build." >&2
fi

echo "==> Pulling base image (if needed): ${BASE_IMAGE}"
docker pull "${BASE_IMAGE}" || echo "WARN: docker pull failed; using local image if available"

BUILD_ARGS=(--build-arg "OPENCLAW_BASE_IMAGE=${BASE_IMAGE}")
BUILD_ARGS+=(--build-arg "INSTALL_SELF_EVOLVING=${INSTALL_SELF_EVOLVING}")
if [[ -n "${ARK_API_KEY}" ]]; then
  BUILD_ARGS+=(--build-arg "ARK_API_KEY=${ARK_API_KEY}")
  echo "==> Baking Ark provider (apiKey from ARK_API_KEY) into image"
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
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify plugins:"
echo "  docker run --rm ${TAG} openclaw plugins list"
