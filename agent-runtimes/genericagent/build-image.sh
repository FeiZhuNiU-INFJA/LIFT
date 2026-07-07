#!/usr/bin/env bash
# Build evolve-eval-genericagent image from agent-runtimes/genericagent build context.
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

usage() {
  cat <<EOF
Usage: $(basename "$0") [-h|--help]

Build evolve-eval-genericagent:latest by cloning lsdefine/GenericAgent and
baking WORK_OPENAI / Langfuse credentials from repo root .env into the image.

Override via env:
  GENERICAGENT_IMAGE       默认 evolve-eval-genericagent:latest
  GENERICAGENT_GIT_URL     默认 https://github.com/lsdefine/GenericAgent.git
  GENERICAGENT_GIT_REF     默认 main
  APT_MIRROR / PIP_INDEX_URL  内网构建时切换上游（脚本会自动探测字节内网）
  LIFT_INTRANET_AUTODETECT=0  关闭内网自动探测
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

TAG="${GENERICAGENT_IMAGE:-evolve-eval-genericagent:latest}"
GIT_URL="${GENERICAGENT_GIT_URL:-https://ghfast.top/https://github.com/lsdefine/GenericAgent.git}"
GIT_REF="${GENERICAGENT_GIT_REF:-main}"
WORK_OPENAI_API_KEY="${WORK_OPENAI_API_KEY:-}"
WORK_OPENAI_BASE_URL="${WORK_OPENAI_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
# GA 直连 work LLM，模型名必须是 provider 真实 endpoint id（如 ep-2025xxxx-xxxxx），不是
# OpenClaw 内部 gateway 命名空间。``GENERICAGENT_MODEL_NAME`` 优先于共享的 ``MODEL_NAME``，
# 避免污染 OpenClaw 镜像构建期的 MODEL_NAME。
MODEL_NAME="${GENERICAGENT_MODEL_NAME:-${MODEL_NAME:-}}"
LANGFUSE_PUBLIC_KEY="${LANGFUSE_PUBLIC_KEY:-}"
LANGFUSE_SECRET_KEY="${LANGFUSE_SECRET_KEY:-}"
LANGFUSE_HOST="${LANGFUSE_HOST:-${LANGFUSE_BASE_URL:-http://host.docker.internal:3000}}"
# 宿主机 .env 通常配 http://localhost:PORT / http://127.0.0.1:PORT；容器内无法解析
# loopback，必须改写为 host.docker.internal 才能连回宿主机 Langfuse。保留原端口 /
# 协议 / 路径，与 src/lift/adapters/openclaw/session.py 的 normalize 语义一致。
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
echo "==> LANGFUSE_HOST baked into mykey.py: ${LANGFUSE_HOST}"
FIRECRAWL_API_KEY="${FIRECRAWL_API_KEY:-}"

if [[ -z "${WORK_OPENAI_API_KEY}" ]]; then
  echo "WARN: WORK_OPENAI_API_KEY is not set; image will bake mykey.py with empty apikey." >&2
  echo "      Set WORK_OPENAI_API_KEY in repo root .env before build." >&2
fi
if [[ -z "${MODEL_NAME}" ]]; then
  echo "WARN: MODEL_NAME is not set; mykey.py model field will be empty." >&2
fi

BUILD_ARGS=(
  --build-arg "GENERICAGENT_GIT_URL=${GIT_URL}"
  --build-arg "GENERICAGENT_GIT_REF=${GIT_REF}"
  --build-arg "WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY}"
  --build-arg "WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL}"
  --build-arg "MODEL_NAME=${MODEL_NAME}"
  --build-arg "LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY}"
  --build-arg "LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY}"
  --build-arg "LANGFUSE_HOST=${LANGFUSE_HOST}"
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

echo "==> Building ${TAG} (context: ${AGENT_DIR}, GA ref: ${GIT_REF})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify GA imports:"
echo "  docker run --rm ${TAG} python -c 'import sys; sys.path.insert(0, \"/opt/GenericAgent\"); import agentmain'"
