# LIFT GenericAgent (https://github.com/lsdefine/GenericAgent) evaluation image.
#
# Build:
#   bash agent-runtimes/genericagent/build-image.sh
#
# Manual:
#   docker build -f agent-runtimes/genericagent/Dockerfile \
#     -t lift-genericagent:latest \
#     agent-runtimes/genericagent

ARG GENERICAGENT_BASE_IMAGE=python:3.12-slim
FROM ${GENERICAGENT_BASE_IMAGE}

USER root

# APT 源：默认保留基础镜像自带（公网 deb.debian.org / security.debian.org）。
# 公网拉取不稳定时通过 --build-arg APT_MIRROR=<scheme://host>。
# codename 从 /etc/os-release 取，避免 base image 升 trixie 后写死 bookworm 的 dpkg 冲突。
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
      . /etc/os-release; \
      printf 'deb %s/debian %s main\ndeb %s/debian %s-updates main\ndeb %s/debian-security %s-security main\n' \
        "$APT_MIRROR" "$VERSION_CODENAME" \
        "$APT_MIRROR" "$VERSION_CODENAME" \
        "$APT_MIRROR" "$VERSION_CODENAME" > /etc/apt/sources.list \
      && rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true ; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
      curl \
      tini \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python 包索引：默认 https://pypi.org/simple/。内网构建时可通过 --build-arg 切换。
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv

# 1) 拉取 GenericAgent 上游
ARG GENERICAGENT_GIT_URL=https://github.com/lsdefine/GenericAgent.git
ARG GENERICAGENT_GIT_REF=main
RUN git clone "${GENERICAGENT_GIT_URL}" /opt/GenericAgent \
    && cd /opt/GenericAgent \
    && git checkout "${GENERICAGENT_GIT_REF}"

# 2) 安装 GenericAgent 依赖（不带可选 [ui] extra）
#    GA 仓库根没有 pyproject.toml，直接用 requirements.txt（如有）+ 必要包
RUN cd /opt/GenericAgent \
    && if [ -f requirements.txt ]; then \
         uv pip install --system --no-cache -r requirements.txt; \
       else \
         echo "No requirements.txt found; installing minimal runtime deps."; \
         uv pip install --system --no-cache \
             requests \
             httpx \
             beautifulsoup4 \
             lxml \
             pillow \
             python-dotenv; \
       fi \
    && uv pip install --system --no-cache langfuse

# 3) 评测任务常用 Python 库（与 OpenClaw 镜像保持一致，避免运行时现场安装）
RUN uv pip install --system --no-cache \
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

# 4) Bake build-time secrets / config into mykey.py via scripts/install-config.sh
ARG WORK_OPENAI_API_KEY=
ARG WORK_OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARG MODEL_NAME=
ARG LANGFUSE_PUBLIC_KEY=
ARG LANGFUSE_SECRET_KEY=
ARG LANGFUSE_HOST=http://host.docker.internal:3000
ARG FIRECRAWL_API_KEY=
ARG REASONING_EFFORT=high
ARG MAX_TOKENS=51200
ENV WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY} \
    WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL} \
    MODEL_NAME=${MODEL_NAME} \
    LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY} \
    LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY} \
    LANGFUSE_HOST=${LANGFUSE_HOST} \
    FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY} \
    REASONING_EFFORT=${REASONING_EFFORT} \
    MAX_TOKENS=${MAX_TOKENS}

COPY mykey.py.template /tmp/mykey.py.template
COPY langfuse_tracing_overlay.py /tmp/langfuse_tracing_overlay.py
COPY firecrawl_plugin.py /tmp/firecrawl_plugin.py
COPY scripts/install-heavy.sh /tmp/install-heavy.sh

# L2 重量层：GA 目前无耗时装配（占位，为分层契约保留入口）
RUN chmod +x /tmp/install-heavy.sh \
    && bash /tmp/install-heavy.sh

# L4 轻量层：mykey.py 渲染 + GA 源码 patch + langfuse overlay + firecrawl 插件注入
COPY scripts/install-config.sh /tmp/install-config.sh
COPY workspace_seed /opt/lift/workspace_seed

RUN chmod +x /tmp/install-config.sh \
    && bash /tmp/install-config.sh

# 5) Workspace 与 GA temp 目录
RUN mkdir -p /workspace/task /opt/GenericAgent/temp /opt/GenericAgent/memory

WORKDIR /workspace/task

ENV LIFT_EVAL_RUN_TAG="" \
    LIFT_GA_SESSION_ID=""

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]
