# LIFT / src OpenClaw evaluation image (plugins baked at build time).
#
# Build:
#   bash agent-runtimes/openclaw/build-image.sh
#
# Or:
#   docker build -f agent-runtimes/openclaw/Dockerfile -t evolve-eval-openclaw-with-evolve:latest agent-runtimes/openclaw
#   # 不带进化插件的 base 变体（推荐通过 build-image.sh 走 INSTALL_SELF_EVOLVING=false）：
#   docker build --build-arg INSTALL_SELF_EVOLVING=false -f agent-runtimes/openclaw/Dockerfile -t evolve-eval-openclaw-base:latest agent-runtimes/openclaw

ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_BASE_IMAGE}

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip \
    curl \
    ca-certificates \
    libsqlite3-dev \
    python3 \
    python3-venv \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

ENV OPENCLAW_STATE_DIR=/root/.openclaw \
    HOME=/root \
    PATH="/root/.openclaw/uv-bin:/root/.openclaw/uv-bin/bin:${PATH}"

RUN mkdir -p /root/.openclaw/extensions /root/.openclaw/skills /workspace

# Firecrawl 通过 OpenClaw 自带的 stock plugin (`stock:firecrawl/index.js`) 提供
# search/scrape/browser 能力；运行时只需在容器 env 注入 FIRECRAWL_API_KEY 鉴权。
# 启用动作放在 install-plugins-in-image.sh，allowlist 在 openclaw.base.json。

COPY plugins/langfuse-tracer /tmp/langfuse-tracer
COPY plugins/self-evolving-plugin-pro-2026.4.23.zip /tmp/self-evolving-plugin-pro.zip
COPY config /tmp/config
COPY openclaw.json.template /tmp/openclaw.json.template
COPY merge-openclaw-config.mjs /tmp/merge-openclaw-config.mjs
COPY install-plugins-in-image.sh /tmp/install-plugins-in-image.sh
COPY workspace_seed /opt/evolve-eval/workspace_seed

ARG ARK_API_KEY=
ENV ARK_API_KEY=${ARK_API_KEY}

# 是否安装并启用 self-evolving-plugin-pro 进化插件。默认 true（带进化插件的标准镜像）；
# 设为 false 构建不带进化插件的 raw 镜像（仍保留 langfuse-tracer / firecrawl）。
ARG INSTALL_SELF_EVOLVING=true
ENV INSTALL_SELF_EVOLVING=${INSTALL_SELF_EVOLVING}

RUN chmod +x /tmp/install-plugins-in-image.sh \
    && bash /tmp/install-plugins-in-image.sh

WORKDIR /workspace

ENV LANGFUSE_PUBLIC_KEY="" \
    LANGFUSE_SECRET_KEY="" \
    LANGFUSE_BASE_URL="" \
    LANGFUSE_TRACER_DEBUG_MESSAGES="0"

EXPOSE 18789 18090

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD test -x "$(command -v openclaw)" && openclaw plugins list >/dev/null 2>&1 || exit 1

CMD ["openclaw", "gateway", "run"]
