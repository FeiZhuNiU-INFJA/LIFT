# LIFT / src_new OpenClaw evaluation image (plugins baked at build time).
#
# Build:
#   bash agents/openclaw/build-image.sh
#
# Or:
#   docker build -f agents/openclaw/Dockerfile -t evolve-eval-openclaw:latest agents/openclaw

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

RUN mkdir -p /root/.openclaw/extensions /workspace

COPY plugins/langfuse-tracer /tmp/langfuse-tracer
COPY plugins/self-evolving-plugin-pro-2026.4.23.zip /tmp/self-evolving-plugin-pro.zip
COPY config /tmp/config
COPY openclaw.json.template /tmp/openclaw.json.template
COPY merge-openclaw-config.mjs /tmp/merge-openclaw-config.mjs
COPY install-plugins-in-image.sh /tmp/install-plugins-in-image.sh
COPY workspace_seed /opt/evolve-eval/workspace_seed

ARG ARK_API_KEY=
ENV ARK_API_KEY=${ARK_API_KEY}

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
