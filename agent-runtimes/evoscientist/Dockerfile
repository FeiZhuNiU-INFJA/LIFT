# LIFT EvoScientist (https://github.com/EvoScientist/EvoScientist) evaluation image.
#
# Base: 官方 ghcr.io/evoscientist/evoscientist:latest（Python 3.11+，EvoSci CLI 预装）。
# LIFT 在其上叠加：
#   - Langfuse tracing overlay（通过 EvoScientist stream events hook 打 root span）
#   - 评测常用 Python 库（pandas/numpy/openpyxl 等）
#   - workspace_seed（可选：注入 LIFT 特定的 project context）
#   - build-time secret bake（WORK_OPENAI_* / LANGFUSE_* → EvoScientist config.yaml）
#
# Build:
#   bash agent-runtimes/evoscientist/build-image.sh
#
# Manual:
#   docker build -f agent-runtimes/evoscientist/Dockerfile \
#     -t lift-evoscientist:latest \
#     agent-runtimes/evoscientist

ARG EVOSCIENTIST_BASE_IMAGE=ghcr.io/evoscientist/evoscientist:latest
FROM ${EVOSCIENTIST_BASE_IMAGE}

USER root

# APT 镜像切换（内网构建）——沿用 GenericAgent 一致策略；官方镜像基于 debian slim。
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
      ca-certificates \
      curl \
      tini \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_INDEX_URL=${PIP_INDEX_URL} \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 1) Langfuse SDK + 评测任务常用库（与其它 runtime 保持一致，避免运行期缺依赖）
#    官方镜像的 EvoScientist 装在 /opt/venv 里但 venv 没有 pip；用 uv pip 显式
#    指向 venv Python 才能装进正确位置。系统 pip（/usr/local/bin/pip）会把包
#    装到 /usr/lib/python3/dist-packages，而 /opt/venv 的 sys.path 看不到那里。
RUN uv pip install --system --python /opt/venv/bin/python3 --no-cache \
        langfuse \
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

# 2) Bake build-time secrets 到 EvoScientist config.yaml
#    EvoScientist 走 custom-openai provider：任意 OpenAI 兼容后端 + 自定义 base_url + model id。
#    Work LLM 走 WORK_OPENAI_* → custom_openai_api_key/base_url + provider=custom-openai。
ARG WORK_OPENAI_API_KEY=
ARG WORK_OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARG MODEL_NAME=
ARG LANGFUSE_PUBLIC_KEY=
ARG LANGFUSE_SECRET_KEY=
ARG LANGFUSE_HOST=http://host.docker.internal:3000
ARG REASONING_EFFORT=high
ENV WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY} \
    WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL} \
    MODEL_NAME=${MODEL_NAME} \
    LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY} \
    LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY} \
    LANGFUSE_HOST=${LANGFUSE_HOST} \
    REASONING_EFFORT=${REASONING_EFFORT}

COPY langfuse_tracing_overlay.py /opt/lift/evoscientist_langfuse_overlay.py
COPY install-in-image.sh /tmp/install-in-image.sh
COPY workspace_seed /opt/lift/workspace_seed

RUN chmod +x /tmp/install-in-image.sh \
    && bash /tmp/install-in-image.sh

# 3) EvoScientist 运行时目录（须落在 container FS 层以进 delta 镜像）
#    ~/.evoscientist/{sessions.db,memories/,skills/} = 跨会话持久数据；
#    warmup 期 EvoScientist 会写入 memories/ + 自动蒸馏 skills 到 GLOBAL_SKILLS_DIR。
RUN mkdir -p /root/.evoscientist/memories /root/.evoscientist/skills \
    && mkdir -p /workspace/task

WORKDIR /workspace/task

ENV LIFT_EVAL_RUN_TAG="" \
    LIFT_EVOSCI_SESSION_ID="" \
    EVOSCIENTIST_DATA_DIR=/root/.evoscientist

# 容器空转，LIFT 通过 docker exec 起 EvoSci -p ... 单次调用
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]
