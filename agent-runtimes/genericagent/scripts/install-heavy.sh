#!/usr/bin/env bash
# GenericAgent 镜像分层 —— L2 重量层。
#
# 当前 GA 的所有 image 装配（mykey 渲染、GA 源码 patch、firecrawl 插件注入、
# tools_schema 追加）都是秒级动作，全部归为 L4（install-config.sh）。GA 的
# python 依赖装载在 Dockerfile 里通过 `uv pip install` 单独 RUN 完成，不走本脚本。
#
# 本文件作为 "分层结构占位" 存在，保证 GA 与其他 runtime 遵循同一 L2/L4 契约；
# 未来若引入需分钟级下载 / 编译的资源（如 embedding model / 大 npm 包），加进
# 这里即可，不用重排 Dockerfile。
set -euo pipefail

echo "==> [L2] GenericAgent has no heavy build actions right now (placeholder)."
