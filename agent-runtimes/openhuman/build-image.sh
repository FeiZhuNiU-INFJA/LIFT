#!/usr/bin/env bash
# Build lift-openhuman image from agent-runtimes/openhuman build context.
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

# 内网自动探测：未显式设 APT_MIRROR 时，如果能解析 mirrors.byted.org 就默认切内网。
# LIFT_INTRANET_AUTODETECT=0 关闭。
if [[ "${LIFT_INTRANET_AUTODETECT:-1}" != "0" ]] \
   && [[ -z "${APT_MIRROR:-}" ]] \
   && getent hosts mirrors.byted.org >/dev/null 2>&1 \
   && curl -fsSL --max-time 3 -o /dev/null "http://mirrors.byted.org/debian/" 2>/dev/null; then
  : "${APT_MIRROR:=http://mirrors.byted.org}"
  export APT_MIRROR
  echo "==> Intranet detected; defaulting APT_MIRROR to ${APT_MIRROR} (LIFT_INTRANET_AUTODETECT=0 to disable)"
fi

usage() {
  cat <<EOF
Usage: $(basename "$0") [-h|--help]

Build lift-openhuman:latest by downloading the upstream
openhuman-core headless binary tarball (same-release sibling of the CEF
GUI .deb) into a slim Debian image, then baking ARK credentials from
repo root .env into ~/.openhuman/config.toml.

Override via env:
  OPENHUMAN_IMAGE           默认 lift-openhuman:latest
  OPENHUMAN_VERSION         指定 upstream release 版本（如 0.58.7）；留空则宿主探测 latest
  OPENHUMAN_CORE_TRIPLE     Rust target triple（默认 x86_64-unknown-linux-gnu；arm64 host 用 aarch64-unknown-linux-gnu）
  OPENHUMAN_CORE_URL        显式指向 openhuman-core-<ver>-<triple>.tar.gz 下载 URL，跳过 mirror 拼接
  OPENHUMAN_GITHUB_MIRROR   github 反代前缀（默认 https://ghfast.top/；设 "" 走直连）
  APT_MIRROR                内网构建时切上游（脚本会自动探测字节内网）
  LIFT_INTRANET_AUTODETECT  设 0 关闭内网自动探测
EOF
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

TAG="${OPENHUMAN_IMAGE:-lift-openhuman:latest}"
VERSION="${OPENHUMAN_VERSION:-}"
CORE_TRIPLE="${OPENHUMAN_CORE_TRIPLE:-x86_64-unknown-linux-gnu}"
CORE_URL="${OPENHUMAN_CORE_URL:-}"
# 默认走 ghfast.top 反代 github；国内直连 github.com / api.github.com 常超时。
# 设 OPENHUMAN_GITHUB_MIRROR="" 可关闭反代（走直连），或指向自建 mirror。
GITHUB_MIRROR="${OPENHUMAN_GITHUB_MIRROR-https://ghfast.top/}"

# 未 pin 版本时，先在宿主机探测 latest（走宿主代理 / ghfast），把结果 pin 进
# --build-arg 传给 Docker，让 build 容器直接命中 releases/download/vX 的稳定
# URL，避开容器内 api.github.com 不通。
if [[ -z "${VERSION}" && -z "${CORE_URL}" ]]; then
  api_url="https://api.github.com/repos/tinyhumansai/openhuman/releases/latest"
  latest_json="$(curl -fsSL --max-time 15 "${api_url}" 2>/dev/null || true)"
  if [[ -n "${latest_json}" ]]; then
    VERSION="$(printf '%s' "${latest_json}" \
      | grep -oE '"tag_name"[[:space:]]*:[[:space:]]*"v[^"]+"' \
      | head -n1 \
      | sed -E 's/.*"v([^"]+)".*/\1/')"
    [[ -n "${VERSION}" ]] && echo "==> Detected latest OpenHuman version: v${VERSION}"
  fi
  if [[ -z "${VERSION}" ]]; then
    echo "WARN: could not resolve latest version from api.github.com; falling back to Dockerfile default." >&2
  fi
fi

# LIFT 约定所有 agent 用同一个 seed 模型（ARK doubao-seed-2-0-pro-260215）；
# 与 GA 类似，OpenHuman 走 ``inference_url`` + ``api_key`` + ``default_model``
# 直连 ARK OpenAI-兼容端点。允许通过 OPENHUMAN_MODEL_NAME 覆盖，避免污染共享 MODEL_NAME。
#
# API key 解析优先级：``WORK_OPENAI_API_KEY``（.env / .env.example 唯一约定的共享 key）
# 优先；``ARK_API_KEY`` 作为老变量向后兼容 fallback；``OPENHUMAN_API_KEY`` 允许临时
# 单独覆盖 OpenHuman 而不影响其它 runtime。
OPENHUMAN_API_KEY_RESOLVED="${OPENHUMAN_API_KEY:-${WORK_OPENAI_API_KEY:-${ARK_API_KEY:-}}}"
ARK_BASE_URL="${OPENHUMAN_BASE_URL:-${WORK_OPENAI_BASE_URL:-${ARK_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}}}"
MODEL_NAME_RESOLVED="${OPENHUMAN_MODEL_NAME:-${MODEL_NAME:-}}"

if [[ -z "${OPENHUMAN_API_KEY_RESOLVED}" ]]; then
  echo "WARN: no API key found (WORK_OPENAI_API_KEY / ARK_API_KEY / OPENHUMAN_API_KEY); image will bake config.toml with empty api_key." >&2
  echo "      Set WORK_OPENAI_API_KEY in repo root .env before build." >&2
fi
if [[ -z "${MODEL_NAME_RESOLVED}" ]]; then
  echo "WARN: MODEL_NAME is not set; config.toml default_model field will be empty." >&2
fi

# 处理 provider 前缀：OpenHuman 的 default_model 直接吃裸模型 id（例如
# ``doubao-seed-2-0-pro-260215``），不接受 GA/OpenClaw 的
# ``custom-ark-cn-beijing-volces-com/doubao-seed-...`` 复合格式。
# 若拿到复合形式，剥掉 ``/`` 前的 provider 前缀。
if [[ "${MODEL_NAME_RESOLVED}" == */* ]]; then
  MODEL_NAME_RESOLVED="${MODEL_NAME_RESOLVED##*/}"
  echo "==> Stripped provider prefix; using default_model=${MODEL_NAME_RESOLVED}"
fi

BUILD_ARGS=(
  --build-arg "API_KEY=${OPENHUMAN_API_KEY_RESOLVED}"
  --build-arg "INFERENCE_URL=${ARK_BASE_URL}"
  --build-arg "DEFAULT_MODEL=${MODEL_NAME_RESOLVED}"
  --build-arg "OPENHUMAN_CORE_TRIPLE=${CORE_TRIPLE}"
  --build-arg "OPENHUMAN_GITHUB_MIRROR=${GITHUB_MIRROR}"
)

if [[ -n "${GITHUB_MIRROR}" ]]; then
  echo "==> Using GitHub mirror: ${GITHUB_MIRROR}"
fi

if [[ -n "${VERSION}" ]]; then
  BUILD_ARGS+=(--build-arg "OPENHUMAN_VERSION=${VERSION}")
  echo "==> Pinning OpenHuman version: ${VERSION}"
fi
if [[ -n "${CORE_URL}" ]]; then
  BUILD_ARGS+=(--build-arg "OPENHUMAN_CORE_URL=${CORE_URL}")
  echo "==> Using explicit openhuman-core tarball URL: ${CORE_URL}"
fi
if [[ -n "${APT_MIRROR:-}" ]]; then
  BUILD_ARGS+=(--build-arg "APT_MIRROR=${APT_MIRROR}")
  echo "==> Using APT mirror: ${APT_MIRROR}"
fi

echo "==> Building ${TAG} (context: ${AGENT_DIR}, triple: ${CORE_TRIPLE})"
docker build -f "${AGENT_DIR}/Dockerfile" \
  "${BUILD_ARGS[@]}" \
  -t "${TAG}" \
  "${AGENT_DIR}"

echo ""
echo "Built: ${TAG}"
echo "Verify openhuman-core binary:"
echo "  docker run --rm ${TAG} /usr/local/bin/openhuman-core help"
