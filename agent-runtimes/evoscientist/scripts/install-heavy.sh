#!/usr/bin/env bash
# EvoScientist 镜像分层 —— L2 重量层（耗时 / 稳定）。
#
# 只放 build-arg 无关的耗时动作，让 L4（install-config.sh）修改不 bust 本层。
# 当前唯一动作：pre-warm firecrawl-mcp 的 npm 包缓存到 image 层，避免首次
# 联网任务被 `npx -y firecrawl-mcp` 的下载拖到 3-10s。
#
# 依赖：base image 已装好 node / npm / npx（`ghcr.io/evoscientist/evoscientist:latest`
# 官方镜像自带）。
set -euo pipefail

echo "==> [L2] Pre-warming firecrawl-mcp npm package"
FIRECRAWL_API_KEY=stub timeout 60 npx -y firecrawl-mcp --help >/dev/null 2>&1 || \
  echo "WARN: firecrawl-mcp pre-warm skipped (offline?)"

echo "[L2] EvoScientist heavy layer done."
