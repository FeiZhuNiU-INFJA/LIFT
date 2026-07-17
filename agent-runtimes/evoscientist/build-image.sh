#!/usr/bin/env bash
# Build lift-evoscientist image based on official ghcr.io/evoscientist/evoscientist:latest.
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

# 内网自动探测：与 GenericAgent build-image.sh 对齐，默认走公网 pypi.org，检出到
# 字节内网环境时切到 bytedpypi。ghcr.io 前缀的 base image 依赖 dockerd 层配置的
# HTTP_PROXY（``/etc/systemd/system/docker.service.d/http-proxy.conf`` → sys-proxy）；
# 若 sys-proxy 不通可手动 export EVOSCIENTIST_BASE_IMAGE=ghcr.milu.moe/... 备用代理。
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

Build lift-evoscientist:latest by layering LIFT tracing overlay + build-time secrets
onto the official ghcr.io/evoscientist/evoscientist:latest image.

Override via env:
  EVOSCIENTIST_IMAGE            默认 lift-evoscientist:latest
  EVOSCIENTIST_BASE_IMAGE       默认 ghcr.io/evoscientist/evoscientist:latest
  APT_MIRROR / PIP_INDEX_URL    内网构建时切换上游（脚本会自动探测字节内网）
  LIFT_INTRANET_AUTODETECT=0    关闭内网自动探测
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

TAG="${EVOSCIENTIST_IMAGE:-lift-evoscientist:latest}"
BASE_IMAGE="${EVOSCIENTIST_BASE_IMAGE:-ghcr.io/evoscientist/evoscientist:latest}"
WORK_OPENAI_API_KEY="${WORK_OPENAI_API_KEY:-}"
WORK_OPENAI_BASE_URL="${WORK_OPENAI_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"

# LIFT 约定：MODEL_NAME=custom/<model_id>；剥掉 custom/ 前缀作为 provider-native id。
RAW_MODEL_NAME="${MODEL_NAME:-}"
if [[ -n "${RAW_MODEL_NAME}" && ( "${RAW_MODEL_NAME}" != custom/* || "${RAW_MODEL_NAME}" == "custom/" ) ]]; then
  echo "WARN: MODEL_NAME must be 'custom/model_id' (e.g. custom/ep-xxxx); got '${RAW_MODEL_NAME}'." >&2
fi
if [[ -n "${EVOSCIENTIST_MODEL_NAME:-}" ]]; then
  MODEL_NAME="${EVOSCIENTIST_MODEL_NAME}"
elif [[ "${RAW_MODEL_NAME}" == custom/* && "${RAW_MODEL_NAME}" != "custom/" ]]; then
  MODEL_NAME="${RAW_MODEL_NAME#custom/}"
else
  MODEL_NAME="${RAW_MODEL_NAME}"
fi

LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_BASE_URL:-http://host.docker.internal:3000}}"
# 宿主 .env 常配 localhost:PORT / 127.0.0.1:PORT；容器 loopback 不通宿主。
# 同 GenericAgent/openclaw 的策略：host 段改写为 host.docker.internal，端口/协议/路径保留。
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

if [[ -z "${WORK_OPENAI_API_KEY}" ]]; then
  echo "WARN: WORK_OPENAI_API_KEY is not set; image will be built with empty api key." >&2
fi
if [[ -z "${MODEL_NAME}" ]]; then
  echo "WARN: MODEL_NAME is not set; EvoScientist config.yaml model field will be empty." >&2
fi

BUILD_ARGS=(
  --build-arg "EVOSCIENTIST_BASE_IMAGE=${BASE_IMAGE}"
  --build-arg "WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY}"
  --build-arg "WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL}"
  --build-arg "MODEL_NAME=${MODEL_NAME}"
  --build-arg "LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY}"
  --build-arg "LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY}"
  --build-arg "LANGFUSE_HOST=${LANGFUSE_HOST}"
  --build-arg "REASONING_EFFORT=${REASONING_EFFORT}"
)

if [[ -n "${APT_MIRROR:-}" ]]; then
  BUILD_ARGS+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
  echo "==> Using APT mirror: ${APT_MIRROR}"
fi
if [[ -n "${PIP_INDEX_URL:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}")
  echo "==> Using pip index URL: ${PIP_INDEX_URL}"
fi

echo "==> Building ${TAG} (context: ${AGENT_DIR}, base: ${BASE_IMAGE})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify EvoSci CLI:"
echo "  docker run --rm ${TAG} EvoSci --version"
