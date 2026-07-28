# LIFT Hermes (https://hermes-agent.nousresearch.com) evaluation image.
#
# Build:
#   bash agent-runtimes/hermes/build-image.sh
#
# Manual:
#   docker build -f agent-runtimes/hermes/Dockerfile \
#     --build-arg HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.5.16 \
#     -t lift-hermes:latest \
#     agent-runtimes/hermes
#
# Design (see .trae/documents/hermes_runtime_integration_plan.md):
#   - Container idles (no `gateway run`); LIFT drives it via
#     `docker exec ... <hermes-venv-python> /opt/lift/hermes_runner.py`.
#   - Hermes state root is relocated to /opt/hermes-state (a plain image dir).
#     The upstream base declares VOLUME /opt/data, so runtime writes there are
#     dropped by `docker commit`; relocating lets warmup memory / skills /
#     sessions get captured into the delta image. Initialized once at build via
#     hermes-bootstrap.sh (the entrypoint no longer seeds, so committed deltas
#     are never re-seeded / clobbered).
#   - config.yaml is patched at container startup from env (never baked), so
#     WORK_OPENAI_API_KEY etc. are not frozen into image layers.

ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.5.16
FROM ${HERMES_BASE_IMAGE}

USER root

# Python 包索引：默认 https://pypi.org/simple/。内网构建时通过 --build-arg 切换。
# uv 不认 PIP_INDEX_URL，必须显式设 UV_INDEX_URL / UV_DEFAULT_INDEX（对齐
# genericagent/Dockerfile 的成功做法），否则 scripts/install-heavy.sh 的 `uv pip
# install` 会忽略镜像源、回落到 pypi.org 并在受限网络下 DNS 超时。
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# L2 重量层：仅 COPY 触发 install-heavy 的入口；改 langfuse-hermes / patch_hermes_config.py
# 等配置类文件不 bust 本层（它们的 COPY 在 L2 RUN 之后）。
COPY scripts/install-heavy.sh /tmp/install-heavy.sh

# Firecrawl API key: only injected when non-empty (build-image.sh passes it only
# then). Baked as runtime ENV so the Hermes agent can use firecrawl; also drives
# the build-time `firecrawl-cli init --all` in install-heavy.sh.
ARG FIRECRAWL_API_KEY=
ENV FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}

# OpenSpace（基于 MCP 的 quality-first skill hub）。默认不装；--with-openspace 时置 true。
# install-heavy.sh 据此 git clone OpenSpace、装进独立 Python 3.12 venv、软链
# openspace-mcp；install-config.sh 拷 host skills 到 Hermes 状态根 skills 目录。
# OPENSPACE_ENABLED 作为运行期信号，让 patch_hermes_config.py 在 entrypoint 阶段把
# mcp_servers.openspace upsert 进 config.yaml（构建期无可靠 runtime HOME，走 config
# patch 而非 hermes mcp add）。
ARG INSTALL_OPENSPACE=false
ENV INSTALL_OPENSPACE=${INSTALL_OPENSPACE} \
    OPENSPACE_ENABLED=${INSTALL_OPENSPACE}
ARG OPENSPACE_GIT_URL=https://github.com/HKUDS/OpenSpace.git
ENV OPENSPACE_GIT_URL=${OPENSPACE_GIT_URL}
ARG OPENSPACE_GIT_REF=main
ENV OPENSPACE_GIT_REF=${OPENSPACE_GIT_REF}
# uv 会去 python-build-standalone GitHub releases 拉 cpython；受限网络下常挂死。
# 通过 UV_PYTHON_INSTALL_MIRROR 让下载走 GitHub 反代（默认 gh-proxy.com）。
ARG UV_PYTHON_INSTALL_MIRROR=https://gh-proxy.com/https://github.com/astral-sh/python-build-standalone/releases/download
ENV UV_PYTHON_INSTALL_MIRROR=${UV_PYTHON_INSTALL_MIRROR}

# agentmemory memory provider plugin（README「Option 2: Memory provider plugin」深度集成）。
# 默认不装；--with-agentmemory 时置 true。install-heavy.sh 装 Node>=20 + npm 装
# @agentmemory/agentmemory、预热离线引擎/嵌入模型、git clone integrations/hermes；
# install-config.sh 再把 plugin 拷进 $HERMES_HOME/plugins/agentmemory。
# AGENTMEMORY_ENABLED 作为运行期信号：
#   - patch_hermes_config.py 在 entrypoint 阶段把 memory.provider=agentmemory upsert 进 config.yaml；
#   - hermes-entrypoint.sh 在 exec 前后台拉起 :3111 server（离线本地嵌入）。
ARG INSTALL_AGENTMEMORY=false
ENV INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY} \
    AGENTMEMORY_ENABLED=${INSTALL_AGENTMEMORY}
ARG AGENTMEMORY_GIT_URL=https://github.com/rohitg00/agentmemory.git
ENV AGENTMEMORY_GIT_URL=${AGENTMEMORY_GIT_URL}
ARG AGENTMEMORY_GIT_REF=main
ENV AGENTMEMORY_GIT_REF=${AGENTMEMORY_GIT_REF}
ARG NPM_CONFIG_REGISTRY=
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}

# 1) L2 heavy: 只做 pip / uv / nvm / npm / git clone / warmup 等分钟级依赖安装。
#    install-heavy.sh 会把发现的 HERMES_VENV_PY / _SRC_DIR / _PLUGINS_DIR 持久化到
#    /opt/lift/hermes-paths.env，供 L4 install-config.sh 复用。
ENV HERMES_HOME=/opt/hermes-state
RUN chmod +x /tmp/install-heavy.sh \
    && mkdir -p /opt/hermes-state /workspace/task \
    && bash /tmp/install-heavy.sh

# 2) L4 config: langfuse overlay / run_agent.py patch / hermes plugins enable /
#    OpenSpace host skills 拷贝 / agentmemory plugin cp。所有 COPY 放在 L2 之后，
#    改这些资产只 bust 本层，不重跑 pip/nvm/npm/warmup。
COPY langfuse-hermes /tmp/langfuse-hermes
COPY hermes-helper/hermes_runner.py /opt/lift/hermes_runner.py
COPY scripts/install-config.sh /tmp/install-config.sh
COPY hermes-bootstrap.sh /opt/lift/hermes-bootstrap.sh
COPY hermes-entrypoint.sh /opt/lift/hermes-entrypoint.sh
COPY patch_hermes_config.py /opt/lift/patch_hermes_config.py

RUN chmod +x /tmp/install-config.sh \
        /opt/lift/hermes-bootstrap.sh \
        /opt/lift/hermes-entrypoint.sh \
    && bash /tmp/install-config.sh \
    && bash /opt/lift/hermes-bootstrap.sh

# 2) Ensure Hermes data root + eval task dir exist.
RUN mkdir -p /opt/data /workspace/task

WORKDIR /workspace/task

# Injected by LIFT at `docker run` (see src/lift/adapters/hermes/session.py).
ENV LIFT_EVAL_RUN_TAG=""

# Container idles; LIFT drives Hermes via docker exec runner. The entrypoint
# patches /opt/hermes-state/config.yaml from env, then keeps the container alive.
ENTRYPOINT ["/opt/lift/hermes-entrypoint.sh"]
CMD ["tail", "-f", "/dev/null"]
