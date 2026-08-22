# LIFT Prime Agent (https://github.com/PrimeIntellect-ai/prime-agent) evaluation image.
#
# Prime Agent 是 Node/TS CLI（发布包 ``prime-agent``，bin ``prime-agent``），运行时
# 会拉起一个常驻 IPython 内核执行工具调用——因此镜像同时需要 Node + Python。
# 自进化状态（Continual Harness / skills / sessions）落在 ``PRIME_AGENT_STATE_DIR``
# （由 ENV ``PRIME_AGENT_CODING_AGENT_DIR`` 钉死），global harness 在
# ``${DIR}/harness/harness_state.json``；warmup 期写入、docker commit 带入 delta。
#
# 分发方式：prime-agent 不在公共 npm registry，而是以 Prime Intellect 官方 R2 上的
# 发布 tarball 分发（package.json 的 3 个 @earendil-works/* 依赖也指向同源 R2 tarball
# URL）。故 L2 层用 download→verify→`npm install -g <tarball>`（对齐官方 install.sh），
# 而非 `npm i -g prime-agent`。
#
# LIFT 在通用 node base 上叠加：
#   - Python3 + ipykernel（Prime Agent 常驻内核依赖；用 PRIME_AGENT_KERNEL_PYTHON
#     指向系统 python3，跳过运行时 kernel-venv bootstrap）
#   - fd/ripgrep（prime-agent 的 fast search 工具，走系统 PATH 命中即不联网下载）
#   - 全局安装 prime-agent CLI（L2 重量层：install-heavy.sh）
#   - build-time secret bake（WORK_OPENAI_* / MODEL_NAME → models.json / settings.json；
#     L4：install-config.sh）
#   - Langfuse env 落地（容器内不打 plugin trace，仅 host 侧 span，见 install-config.sh）
#   - workspace_seed（可选：注入 LIFT 特定 project context）
#
# Build:
#   bash agent-runtimes/prime_agent/build-image.sh
#
# Manual:
#   docker build -f agent-runtimes/prime_agent/Dockerfile \
#     --build-arg PRIME_AGENT_VERSION=0.7.3 \
#     -t lift-prime-agent:latest \
#     agent-runtimes/prime_agent

ARG PRIME_AGENT_BASE_IMAGE=node:22-bookworm-slim
FROM ${PRIME_AGENT_BASE_IMAGE}

USER root

# APT 源：默认保留基础镜像自带（公网 deb.debian.org）。内网构建时 --build-arg APT_MIRROR。
# codename 从 /etc/os-release 取，避免 base image 升版本后写死 codename 的 dpkg 冲突。
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
      . /etc/os-release; \
      printf 'deb %s/debian %s main\ndeb %s/debian %s-updates main\ndeb %s/debian-security %s-security main\n' \
        "$APT_MIRROR" "$VERSION_CODENAME" \
        "$APT_MIRROR" "$VERSION_CODENAME" \
        "$APT_MIRROR" "$VERSION_CODENAME" > /etc/apt/sources.list \
      && rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true ; \
    fi

# Prime Agent 常驻 IPython 内核 → 需要 python3 + pip + venv；git/curl/tini 为通用依赖。
# fd-find(fdfind) / ripgrep(rg)：prime-agent tools-manager 优先查系统 PATH，命中即
# 不再联网下载二进制（build/holdout 更稳、可离线）。
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
      curl \
      tini \
      python3 \
      python3-pip \
      python3-venv \
      build-essential \
      fd-find \
      ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Debian 下 fd 的二进制名是 fdfind；prime-agent 的 systemBinaryNames 含 fd/fdfind，
# 但补个 fd 软链更稳妥。
RUN if command -v fdfind >/dev/null 2>&1 && ! command -v fd >/dev/null 2>&1; then \
      ln -s "$(command -v fdfind)" /usr/local/bin/fd ; \
    fi

ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 1) Prime Agent 常驻内核依赖 + 评测任务常用 Python 库（与其它 runtime 保持一致）
#    ipykernel 装到系统 python3；下方 PRIME_AGENT_KERNEL_PYTHON 指向 python3，
#    prime-agent 直接复用之，跳过 ~/.prime/agent/kernel-venv 的运行时 bootstrap。
#
#    ⚠️ kernel-ready 硬校验：设了 PRIME_AGENT_KERNEL_PYTHON 后，prime-agent 启动会
#    校验该 python 是否具备 ipykernel + prime-agent-runtime(rlm) + 一组默认包
#    （DEFAULT_RLM_EXTRA_PACKAGES：requests/httpx/pyyaml/tomli/python-dotenv/
#    pandas/numpy/scipy/bs4/lxml/pydantic/tyro），缺任一即 throw，kernel 代码无法执行。
#    rlm 本体不在 PyPI，随 npm 包分发（dist/prime-agent-runtime），故在 install-heavy
#    之后单独 pip 安装（见下方步骤 2.1）；这里先把纯 PyPI 的默认包 + mcp 一次装齐。
#    mcp：MCP 集成（如 firecrawl_mcp skill 的 McpIntegration）在 kernel 内经官方
#    `mcp` SDK 建连，必须预装。
RUN pip3 install --no-cache-dir \
        ipykernel \
        jupyter-client \
        mcp \
        httpx \
        pyyaml \
        tomli \
        python-dotenv \
        scipy \
        pydantic \
        tyro \
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

COPY scripts/install-heavy.sh /tmp/install-heavy.sh

# 2) L2 重量层：从官方 R2 下载+校验+全局安装 prime-agent。secrets / config 类
#    build-arg 变动不 bust 本层，rebuild 直接命中缓存。
#    PRIME_AGENT_VERSION 由 build-image.sh 传入（可 override）；DOWNLOAD_BASE_URL
#    可指向内网镜像。offline 安全：postinstall 不置 BOOTSTRAP_* → 不联网拉 kernel/工具。
ARG PRIME_AGENT_VERSION=0.7.3
ARG PRIME_AGENT_DOWNLOAD_BASE_URL=https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev
ARG PRIME_AGENT_PACKAGE=prime-agent
ARG PRIME_AGENT_VERIFY_CHECKSUM=1
RUN chmod +x /tmp/install-heavy.sh \
    && PRIME_AGENT_VERSION="${PRIME_AGENT_VERSION}" \
       PRIME_AGENT_DOWNLOAD_BASE_URL="${PRIME_AGENT_DOWNLOAD_BASE_URL}" \
       PRIME_AGENT_PACKAGE="${PRIME_AGENT_PACKAGE}" \
       PRIME_AGENT_VERIFY_CHECKSUM="${PRIME_AGENT_VERIFY_CHECKSUM}" \
       bash /tmp/install-heavy.sh

# 2.1) 把 prime-agent-runtime(rlm) 装进 kernel python。rlm 不在 PyPI，随 npm 包
#      分发在 <node_modules>/prime-agent/dist/prime-agent-runtime（含 pyproject）。
#      装上后 kernel-ready 校验（RUNTIME_READY_CHECK: import rlm / McpIntegration /
#      HarnessEntry + 一组 harness CRUD 断言）才能通过；否则任何 kernel 代码执行都会
#      因 "prime-agent-runtime is not installed in this IPython kernel" 报错。
#      用 npm root -g 动态定位，避免写死 node 版本路径。
RUN RT_DIR="$(npm root -g)/prime-agent/dist/prime-agent-runtime" \
    && test -f "${RT_DIR}/pyproject.toml" \
       || { echo "ERROR: prime-agent-runtime not found at ${RT_DIR}" >&2; exit 1; } \
    && pip3 install --no-cache-dir "${RT_DIR}" \
    && /usr/bin/python3 -c "import rlm; from rlm import McpIntegration; from rlm.harness import HarnessEntry; print('rlm ready:', rlm.__file__)"

# 2.2) Firecrawl 远程 MCP skill（路线一）。两个独立需求：
#      (a) skill 发现：SKILL.md 落到 ${AGENT_DIR}/skills/firecrawl（prime-agent 扫描
#          skills 位置提取到 system prompt）。
#      (b) kernel import：因 PRIME_AGENT_KERNEL_PYTHON 已钉死，prime-agent 不会自动把
#          Python skill 装进 kernel（"does not install packages into that environment"），
#          缺失即 import 失败被 disable。故手动把 firecrawl 包装进 system python3。
#      --no-deps：pyproject 依赖 mcp/httpx（步骤 1 已装）+ prime-agent-runtime（步骤 2.1
#      本地装，不在 PyPI），不加 --no-deps 会让 pip 去 PyPI 找 prime-agent-runtime 而失败。
#      -e 指向 skills 目录：SKILL.md 发现与 kernel import 共用同一份源，避免双份漂移。
COPY skills /opt/lift/prime-agent-skills
RUN mkdir -p /root/.prime/agent/skills \
    && cp -a /opt/lift/prime-agent-skills/firecrawl /root/.prime/agent/skills/firecrawl \
    && pip3 install --no-cache-dir --no-deps -e /root/.prime/agent/skills/firecrawl \
    && /usr/bin/python3 -c "import firecrawl; assert firecrawl.firecrawl.server == 'firecrawl'; assert firecrawl.firecrawl.bearer_token_env == 'FIRECRAWL_API_KEY'; print('firecrawl skill import ok:', firecrawl.firecrawl.url)"

# 3) Bake build-time secrets 到 Prime Agent provider 配置（models.json）
#    Prime Agent 走 OpenAI 兼容后端；Work LLM 从 WORK_OPENAI_* + MODEL_NAME 渲染。
ARG WORK_OPENAI_API_KEY=
ARG WORK_OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARG MODEL_NAME=
ARG LANGFUSE_PUBLIC_KEY=
ARG LANGFUSE_SECRET_KEY=
ARG LANGFUSE_HOST=http://host.docker.internal:3000
ARG REASONING_EFFORT=high
# FIRECRAWL_API_KEY：可选联网抓取能力。settings.json 的 mcpServers.firecrawl 用
# bearerTokenEnvVar 引用本 env（不写进 JSON 明文）；置空则 firecrawl_mcp skill 仍加载，
# 但调用时抛 NotEnabled（agent 可回退 kernel + requests）。对齐 genericagent 的透传约定。
ARG FIRECRAWL_API_KEY=
ENV WORK_OPENAI_API_KEY=${WORK_OPENAI_API_KEY} \
    WORK_OPENAI_BASE_URL=${WORK_OPENAI_BASE_URL} \
    MODEL_NAME=${MODEL_NAME} \
    LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY} \
    LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY} \
    LANGFUSE_HOST=${LANGFUSE_HOST} \
    REASONING_EFFORT=${REASONING_EFFORT} \
    FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}

# 关键：钉死 Prime Agent 状态根目录。getAgentDir() 优先读 PRIME_AGENT_CODING_AGENT_DIR，
# global harness 落在 ${PRIME_AGENT_CODING_AGENT_DIR}/harness/harness_state.json，
# 供 docker commit 稳定捕获（与 src/paths.py PRIME_AGENT_STATE_DIR 一致）。
# PRIME_AGENT_KERNEL_PYTHON 指向系统 python3（已装 ipykernel）→ 复用之，免 kernel-venv bootstrap。
# PI_SKIP_VERSION_CHECK=1：评测环境跳过启动时的版本更新检查（免联网/免噪声）。
ENV PRIME_AGENT_CODING_AGENT_DIR=/root/.prime/agent \
    PRIME_AGENT_KERNEL_PYTHON=/usr/bin/python3 \
    PI_SKIP_VERSION_CHECK=1

# 4) L4 轻量层：provider 配置渲染（models.json + settings.json）+ Langfuse env 落地
#    （依赖 build-arg，改动频繁但秒级）
COPY scripts/install-config.sh /tmp/install-config.sh
COPY workspace_seed /opt/lift/workspace_seed

RUN chmod +x /tmp/install-config.sh \
    && bash /tmp/install-config.sh

# 5) Prime Agent 状态目录（须落在 container FS 层以进 delta 镜像）+ 评测 workspace
#    ${PRIME_AGENT_CODING_AGENT_DIR}/harness = Continual Harness global scope；
#    skills/sessions 亦在此目录下。
RUN mkdir -p /root/.prime/agent/harness \
             /root/.prime/agent/skills \
             /root/.prime/agent/sessions \
    && mkdir -p /workspace/task

WORKDIR /workspace/task

ENV LIFT_EVAL_RUN_TAG="" \
    LIFT_PRIME_AGENT_SESSION_ID=""

# 容器空转，LIFT 通过 docker exec 起 `prime-agent -p ...`（chat）/
# `prime-agent -p "/refine --global"`（evolve）单次调用。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]
