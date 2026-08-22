#!/usr/bin/env bash
# Prime Agent 镜像分层 —— L2 重量层（耗时 / 稳定）。
#
# 只放 build-arg 无关的耗时动作，让 L4（install-config.sh）修改不 bust 本层。
# 动作：从 Prime Intellect 官方 R2 发布源下载 prime-agent 发布 tarball（含 SHA256
# 校验），再 `npm install -g` 落成全局 bin `prime-agent`。
#
# 为什么不用 `npm install -g prime-agent@latest`：prime-agent 不发布在公共 npm
# registry，而是以 R2 上的 tarball 分发（package.json 的 3 个 @earendil-works/*
# 依赖也指向同源 R2 tarball URL，npm 会自动解析）。因此这里直接喂 tarball，
# 与官方 install.sh 的 download→verify→npm install -g 路径一致。
#
# 依赖：base image 已装好 node / npm（node:22-bookworm-slim 自带）+ curl。
#
# 输入（由 Dockerfile 透传的 ENV）：
#   PRIME_AGENT_VERSION            必填，发布版本号（去 v 前缀，如 0.7.3）
#   PRIME_AGENT_DOWNLOAD_BASE_URL  默认官方 R2（可指向内网镜像）
#   PRIME_AGENT_PACKAGE            默认 prime-agent
#   PRIME_AGENT_VERIFY_CHECKSUM    默认 1（0 关闭 SHA256 校验）
set -euo pipefail

VERSION="${PRIME_AGENT_VERSION:?PRIME_AGENT_VERSION is required (e.g. 0.7.3)}"
VERSION="${VERSION#v}"  # 归一：去掉可能的 v 前缀（与官方 normalize_version 一致）
BASE_URL="${PRIME_AGENT_DOWNLOAD_BASE_URL:-https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev}"
BASE_URL="${BASE_URL%/}"
PACKAGE="${PRIME_AGENT_PACKAGE:-prime-agent}"
VERIFY="${PRIME_AGENT_VERIFY_CHECKSUM:-1}"

TARBALL_NAME="${PACKAGE}-${VERSION}.tgz"
TARBALL_URL="${BASE_URL}/releases/v${VERSION}/${TARBALL_NAME}"
CHECKSUMS_URL="${BASE_URL}/releases/v${VERSION}/SHA256SUMS"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
TARBALL_PATH="${WORKDIR}/${TARBALL_NAME}"

echo "==> [L2] Downloading Prime Agent ${PACKAGE}@${VERSION}"
echo "    ${TARBALL_URL}"
curl -fsSL "${TARBALL_URL}" -o "${TARBALL_PATH}"

if [ "${VERIFY}" = "1" ]; then
  echo "==> Verifying SHA256 against ${CHECKSUMS_URL}"
  CHECKSUMS_PATH="${WORKDIR}/SHA256SUMS"
  if curl -fsSL "${CHECKSUMS_URL}" -o "${CHECKSUMS_PATH}"; then
    EXPECTED="$(grep -E "  ${TARBALL_NAME}\$" "${CHECKSUMS_PATH}" | awk '{print $1}' | head -n1)"
    if [ -z "${EXPECTED}" ]; then
      echo "ERROR: ${TARBALL_NAME} not found in SHA256SUMS" >&2
      exit 1
    fi
    ACTUAL="$(sha256sum "${TARBALL_PATH}" | awk '{print $1}')"
    if [ "${EXPECTED}" != "${ACTUAL}" ]; then
      echo "ERROR: checksum mismatch for ${TARBALL_NAME}" >&2
      echo "  expected ${EXPECTED}" >&2
      echo "  actual   ${ACTUAL}" >&2
      exit 1
    fi
    echo "    OK ${ACTUAL}"
  else
    echo "WARN: could not fetch SHA256SUMS; skipping checksum verification." >&2
  fi
else
  echo "==> Checksum verification disabled (PRIME_AGENT_VERIFY_CHECKSUM=0)."
fi

# 全局安装。跳过 fund/audit 噪声；postinstall 只在 BOOTSTRAP_* 置位时才拉 kernel/
# tools（我们让内核复用系统 python3，工具走 apt 装的 fd/rg，故不置位 → 快且离线安全）。
# npm 会自动解析 package.json 里 3 个同源 R2 tarball 依赖（@earendil-works/*）。
echo "==> [L2] npm install -g ${TARBALL_NAME}"
npm install -g --no-fund --no-audit --loglevel=error --progress=false "${TARBALL_PATH}"

# Sanity：prime-agent 应在 PATH 上且可执行。
if ! command -v prime-agent >/dev/null 2>&1; then
  echo "ERROR: 'prime-agent' not found on PATH after npm install -g." >&2
  echo "       检查 package.json bin 字段（应为 prime-agent）。" >&2
  exit 1
fi

echo "==> prime-agent resolved at: $(command -v prime-agent)"
# --version 在 offline 下应直接可用；失败仅告警不阻断。
PI_SKIP_VERSION_CHECK=1 prime-agent --version 2>/dev/null \
  || echo "WARN: 'prime-agent --version' non-zero (continuing)."

echo "[L2] Prime Agent heavy layer done."
