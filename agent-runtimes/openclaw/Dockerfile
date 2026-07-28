# LIFT / src OpenClaw evaluation image (plugins baked at build time).
#
# Build:
#   bash agent-runtimes/openclaw/build-image.sh
#
# Or:
#   docker build -f agent-runtimes/openclaw/Dockerfile -t lift-openclaw-with-evolve:latest agent-runtimes/openclaw
#   # 不带进化插件的 base 变体（推荐通过 build-image.sh 走 INSTALL_SELF_EVOLVING=false）：
#   docker build --build-arg INSTALL_SELF_EVOLVING=false -f agent-runtimes/openclaw/Dockerfile -t lift-openclaw-base:latest agent-runtimes/openclaw

ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_BASE_IMAGE}

USER root

# APT 源：默认保留基础镜像自带（公网 deb.debian.org / security.debian.org）。
# 在公网拉取不稳定时，可通过 --build-arg APT_MIRROR=<scheme://host> 指定 Debian 镜像
# 仓库（要求布局与官方一致：<APT_MIRROR>/debian、<APT_MIRROR>/debian-security）。
# 例：字节内网：APT_MIRROR=http://mirrors.byted.org
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
      printf 'deb %s/debian bookworm main\ndeb %s/debian bookworm-updates main\ndeb %s/debian-security bookworm-security main\n' \
        "$APT_MIRROR" "$APT_MIRROR" "$APT_MIRROR" > /etc/apt/sources.list \
      && rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true ; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip \
    curl \
    ca-certificates \
    git \
    libsqlite3-dev \
    python3 \
    python3-pip \
    python3-venv \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

ENV OPENCLAW_STATE_DIR=/root/.openclaw \
    HOME=/root \
    PATH="/root/.openclaw/uv-bin:/root/.openclaw/uv-bin/bin:${PATH}"

RUN mkdir -p /root/.openclaw/extensions /root/.openclaw/skills /workspace/task

# Firecrawl 自 OpenClaw 2026.6.10 起从 stock 剥离为外置 npm 插件
# (@openclaw/firecrawl-plugin)，提供 web_search/scrape/browser 能力；安装与启用
# 由 install-heavy.sh（`openclaw plugins install`）+ install-config.sh（`plugins enable`）完成，
# 运行时只需在容器 env 注入 FIRECRAWL_API_KEY
# 鉴权。allowlist 在 config/plugins.fragment.json。

COPY plugins/langfuse-tracer /tmp/langfuse-tracer
COPY plugins/self-evolving-plugin-pro-2026.4.23.zip /tmp/self-evolving-plugin-pro.zip
COPY scripts/install-heavy.sh /tmp/install-heavy.sh
COPY scripts/openclaw-agentmemory-prelaunch.sh /opt/lift/openclaw-agentmemory-prelaunch.sh
COPY workspace_seed /root/.openclaw/workspace

ARG WORK_OPENAI_API_KEY=
ENV WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY}

# OpenSpace LLM base URL（对应本项目 WORK_OPENAI_BASE_URL）。仅 OpenSpace MCP env 块使用
# （install-config.sh 写入 OPENSPACE_LLM_API_BASE）；OpenClaw 自身模型的 baseUrl
# 仍由 models.fragment.json 决定，二者互不影响。默认空串时 OpenSpace 走其默认解析。
ARG WORK_OPENAI_BASE_URL=
ENV WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL}

# 运行模型选用（provider/model_id 格式，provider 约定固定为 custom）。install-config.sh
# 据此渲染 models.fragment（斜杠后作 model.id）与 agents.fragment（整串作 primary/models key）。
ARG MODEL_NAME=
ENV MODEL_NAME=${MODEL_NAME}

# 是否安装并启用 self-evolving-plugin-pro 进化插件。默认 true（带进化插件的标准镜像）；
# 设为 false 构建不带进化插件的 raw 镜像（仍保留 langfuse-tracer / firecrawl）。
ARG INSTALL_SELF_EVOLVING=true
ENV INSTALL_SELF_EVOLVING=${INSTALL_SELF_EVOLVING}

# 是否安装 OpenSpace（基于 MCP 的 quality-first skill hub）。默认 false；设为 true 时
# install-heavy.sh 会 git clone OpenSpace、在独立 Python 3.12 venv 里
# pip install -e，注册名为 openspace 的 stdio MCP server，并拷入 host skills。
# OPENSPACE_GIT_URL / OPENSPACE_GIT_REF 允许切换上游（内网可指镜像/反代）。
ARG INSTALL_OPENSPACE=false
ENV INSTALL_OPENSPACE=${INSTALL_OPENSPACE}
ARG OPENSPACE_GIT_URL=https://github.com/HKUDS/OpenSpace.git
ENV OPENSPACE_GIT_URL=${OPENSPACE_GIT_URL}
ARG OPENSPACE_GIT_REF=main
ENV OPENSPACE_GIT_REF=${OPENSPACE_GIT_REF}

# 是否安装 agentmemory memory plugin（README「Option 2: OpenClaw memory plugin」深度集成）。
# 默认 false；设为 true 时 install-heavy.sh 会 npm 装 @agentmemory/agentmemory、
# 预热离线引擎/嵌入模型、git clone integrations/openclaw 到 extensions/agentmemory，并 merge
# agentmemory.fragment.json（claim plugins.slots.memory = agentmemory）。运行期由
# openclaw-agentmemory-prelaunch.sh 在 gateway 启动前后台拉起 :3111 server（离线本地嵌入）。
# AGENTMEMORY_ENABLED 作为运行期信号（当前 prelaunch 脚本无条件起 server，仅由 adapter 决定
# 是否把它设为 entrypoint；保留 ENV 便于诊断与未来条件化）。
ARG INSTALL_AGENTMEMORY=false
ENV INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY} \
    AGENTMEMORY_ENABLED=${INSTALL_AGENTMEMORY}
ARG AGENTMEMORY_GIT_URL=https://github.com/rohitg00/agentmemory.git
ENV AGENTMEMORY_GIT_URL=${AGENTMEMORY_GIT_URL}
ARG AGENTMEMORY_GIT_REF=main
ENV AGENTMEMORY_GIT_REF=${AGENTMEMORY_GIT_REF}
# npm registry（内网可覆盖；默认公网）。install-heavy.sh 读取后 export 给 npm。
ARG NPM_CONFIG_REGISTRY=
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}

# 互斥保证：self-evolving-plugin-pro / OpenSpace / agentmemory 三种插件只能三选一，不允许叠加。
# build-image.sh 已在脚本层拦截；这里再加一道 Dockerfile 层守卫，防止有人直接
# docker build --build-arg 同时置多个 true 绕过脚本。
RUN _n=0; \
    [ "${INSTALL_SELF_EVOLVING}" = "true" ] && _n=$((_n+1)); \
    [ "${INSTALL_OPENSPACE}" = "true" ] && _n=$((_n+1)); \
    [ "${INSTALL_AGENTMEMORY}" = "true" ] && _n=$((_n+1)); \
    if [ "${_n}" -gt 1 ]; then \
      echo "ERROR: INSTALL_SELF_EVOLVING / INSTALL_OPENSPACE / INSTALL_AGENTMEMORY 三方互斥，只能三选一。" >&2; \
      exit 2; \
    fi
# Python 包索引：默认 https://pypi.org/simple/（公网官方源）。在公网拉取不稳定时，可通过
# --build-arg PIP_INDEX_URL=<url> 指定镜像源（uv / pip 均生效）。
# 例：字节内网：PIP_INDEX_URL=https://bytedpypi.byted.org/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_BREAK_SYSTEM_PACKAGES=1

# 预装评测任务常用的 Python 库，避免 Agent 在容器内现场安装（Bookworm 默认 PEP 668
# 受限，且常因网络/源问题失败）。如需新增库，统一加在这里。
RUN pip3 install --no-cache-dir \
        pandas \
        numpy \
        openpyxl \
        xlrd \
        xlsxwriter \
        python-pptx \
        python-docx \
        pypdf \
        beautifulsoup4 \
        lxml \
        requests \
        markdown \
        tabulate \
        Pillow

RUN chmod +x /tmp/install-heavy.sh /opt/lift/openclaw-agentmemory-prelaunch.sh \
    && bash /tmp/install-heavy.sh

# ── L4 config 层 COPY 放在 install-heavy 之后：改 fragment / merge 脚本 / install-config.sh
#    只 bust 本层，不重跑 self-evolving unzip / OpenSpace pip / agentmemory npm+warmup。
COPY config /tmp/config
COPY merge-openclaw-config.mjs /tmp/merge-openclaw-config.mjs
COPY scripts/install-config.sh /tmp/install-config.sh

RUN chmod +x /tmp/install-config.sh \
    && bash /tmp/install-config.sh

WORKDIR /workspace

ENV LANGFUSE_PUBLIC_KEY="" \
    LANGFUSE_SECRET_KEY="" \
    LANGFUSE_BASE_URL="" \
    LANGFUSE_TRACER_DEBUG_MESSAGES="0"

EXPOSE 18789 18090

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD test -x "$(command -v openclaw)" && openclaw plugins list >/dev/null 2>&1 || exit 1

CMD ["openclaw", "gateway", "run"]
