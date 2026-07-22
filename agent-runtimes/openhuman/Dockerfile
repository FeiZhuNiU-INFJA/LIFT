# LIFT OpenHuman (https://github.com/tinyhumansai/openhuman) evaluation image.
#
# Build:
#   bash agent-runtimes/openhuman/build-image.sh
#
# Manual:
#   docker build -f agent-runtimes/openhuman/Dockerfile \
#     -t lift-openhuman:latest \
#     agent-runtimes/openhuman
#
# Runtime：容器以 `openhuman-core run --host 0.0.0.0 --port 7788` 启动，
# LIFT adapter 通过 HTTP JSON-RPC `POST /rpc` (`openhuman.agent_chat`) 调用。
# 官方 `.deb` 是 CEF/Chromium 桌面版（400+ MB），headless 场景使用同一 release
# 页发布的 `openhuman-core-<ver>-<triple>.tar.gz`（纯 Rust 二进制）。

ARG OPENHUMAN_BASE_IMAGE=debian:bookworm-slim
FROM ${OPENHUMAN_BASE_IMAGE}

USER root

# APT 源：默认走基础镜像自带（deb.debian.org）；内网构建时通过 --build-arg APT_MIRROR
# 切到内网。codename 从 /etc/os-release 取，避免 base image 升级后写死冲突。
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
      libssl3 \
      libc6 \
      libxdo3 \
      libasound2 \
      python3 \
    && rm -rf /var/lib/apt/lists/*
# python3: /opt/lift/max_tokens_proxy.py 依赖 stdlib(http.server + urllib + json)。
# ⚠️ 不能用 python3-minimal —— Debian 拆包时 json/http.server 都在完整 python3
# 依赖的 python3-stdlib 包里,python3-minimal 仅有内核模块。

# 1) 下载 openhuman-core headless 二进制。
#    官方 .deb 是 CEF/Chromium 桌面 GUI 版（400 MB，Tauri 打包），不包含 CLI；
#    真正的 headless 二进制在同 release 的 `openhuman-core-<ver>-x86_64-unknown-linux-gnu.tar.gz`
#    里，tarball 内只有裸二进制 `openhuman-core`。
#    国内直连 github.com / api.github.com 通常不通；默认走 ghfast.top 反代。
#    build-image.sh 会把宿主上探测到的版本 pin 进来，避免依赖 api.github.com 查
#    latest。外部若已提供 OPENHUMAN_CORE_URL（离线镜像 / 内网 mirror），直接用。
ARG OPENHUMAN_VERSION=0.58.7
ARG OPENHUMAN_CORE_TRIPLE=x86_64-unknown-linux-gnu
ARG OPENHUMAN_GITHUB_MIRROR=https://ghfast.top/
ARG OPENHUMAN_CORE_URL=
RUN set -eux; \
    if [ -z "${OPENHUMAN_CORE_URL}" ]; then \
      REL_PATH="tinyhumansai/openhuman/releases/download/v${OPENHUMAN_VERSION}/openhuman-core-${OPENHUMAN_VERSION}-${OPENHUMAN_CORE_TRIPLE}.tar.gz"; \
      CORE_URL="${OPENHUMAN_GITHUB_MIRROR}https://github.com/${REL_PATH}"; \
    else \
      CORE_URL="${OPENHUMAN_CORE_URL}"; \
    fi; \
    echo "openhuman-core tarball URL: ${CORE_URL}"; \
    curl -fsSL --retry 3 --retry-delay 5 --max-time 600 "${CORE_URL}" -o /tmp/openhuman-core.tar.gz; \
    mkdir -p /opt/openhuman-core; \
    tar xzf /tmp/openhuman-core.tar.gz -C /opt/openhuman-core; \
    rm -f /tmp/openhuman-core.tar.gz; \
    test -x /opt/openhuman-core/openhuman-core; \
    ln -sf /opt/openhuman-core/openhuman-core /usr/local/bin/openhuman-core; \
    /usr/local/bin/openhuman-core --help >/dev/null 2>&1 \
      || /usr/local/bin/openhuman-core help >/dev/null 2>&1 \
      || true

# 2) 创建 openhuman 家目录 + config
#    OpenHuman 默认把配置读自 ``~/.openhuman/config.toml``，workspace_dir 位于
#    ``~/.openhuman/workspace``（含 memory_tree / wiki 等）。以 root 运行方便
#    与 docker exec 语义一致，家目录直接放 /root。若后续切非 root，将 evolve
#    路径同步替换（见 src/lift/adapters/openhuman/adapter.py:evolve_paths）。
ARG API_KEY=
ARG API_URL=
ARG INFERENCE_URL=https://ark.cn-beijing.volces.com/api/v3
ARG DEFAULT_MODEL=
# LIFT max_tokens 代理:openhuman-core 不暴露 max_tokens 覆盖;由 entrypoint 起一个
# 容器内 127.0.0.1:${LIFT_PROXY_PORT} 上的透明代理注入 max_tokens 后再转发到
# 真实上游(LIFT_PROXY_UPSTREAM)。config.toml 的 inference_url 被 install-in-image.sh
# 改指向 http://127.0.0.1:${LIFT_PROXY_PORT}/v3。同 GMI_MAAS_BASE_URL 也改指向代理,
# 保持 AgentBox mode 请求也经过代理。
ARG LIFT_MAX_TOKENS_PROXY_ENABLED=true
ARG LIFT_PROXY_PORT=7787
ARG MAX_TOKENS=51200
ENV ARK_API_KEY=${API_KEY} \
    ARK_BASE_URL=${INFERENCE_URL} \
    MODEL_NAME=${DEFAULT_MODEL} \
    OPENHUMAN_CORE_HOST=0.0.0.0 \
    OPENHUMAN_CORE_PORT=7788 \
    OPENHUMAN_AGENTBOX_MODE=1 \
    GMI_MAAS_BASE_URL=http://127.0.0.1:${LIFT_PROXY_PORT}/v3 \
    GMI_MAAS_API_KEY=${API_KEY} \
    GMI_MODELS=${DEFAULT_MODEL} \
    LIFT_MAX_TOKENS_PROXY_ENABLED=${LIFT_MAX_TOKENS_PROXY_ENABLED} \
    LIFT_PROXY_PORT=${LIFT_PROXY_PORT} \
    LIFT_PROXY_UPSTREAM=${INFERENCE_URL} \
    MAX_TOKENS=${MAX_TOKENS}
# ↑ AgentBox / GMI MaaS: openhuman-core 默认要求 backend OAuth (`app-session` JWT) 才能
#   触发 LLM 推理；未登录时 chat 会立刻返回 `SESSION_EXPIRED: backend session not active`。
#   官方 headless 通路是 AgentBox marketplace：设 `OPENHUMAN_AGENTBOX_MODE=1` +
#   `GMI_MAAS_BASE_URL/API_KEY/MODELS` 时，`chat-factory` 会走 "AgentBox mode ...
#   bypassing app-session gate for custom provider" 分支，直连 OpenAI 兼容端点。
#   我们把它当作 headless custom provider，指到 ARK OpenAI 兼容 URL 与 endpoint id。

# agentmemory backend（官方 wiki config.toml backend 切换）。默认不装；--with-agentmemory
# 时置 true。install-in-image.sh 据此在 config.toml 追加 [memory] backend=agentmemory，并装
# Node>=20 + npm 装 @agentmemory/agentmemory、预热离线引擎/嵌入模型。AGENTMEMORY_ENABLED
# 作为运行期信号：openhuman-agentmemory-entrypoint.sh 在 openhuman-core 启动前先拉起 :3111
# server（OH agentmemory backend 无自动回退 SQLite，daemon 不可达会报错）。
ARG INSTALL_AGENTMEMORY=false
ENV INSTALL_AGENTMEMORY=${INSTALL_AGENTMEMORY} \
    AGENTMEMORY_ENABLED=${INSTALL_AGENTMEMORY}
ARG AGENTMEMORY_GIT_URL=https://github.com/rohitg00/agentmemory.git
ENV AGENTMEMORY_GIT_URL=${AGENTMEMORY_GIT_URL}
ARG AGENTMEMORY_GIT_REF=main
ENV AGENTMEMORY_GIT_REF=${AGENTMEMORY_GIT_REF}
ARG NODE_MAJOR=20
ENV NODE_MAJOR=${NODE_MAJOR}
ARG NPM_CONFIG_REGISTRY=
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}

COPY config.toml.template /tmp/config.toml.template
COPY install-in-image.sh /tmp/install-in-image.sh
COPY scripts/openhuman-agentmemory-entrypoint.sh /opt/lift/openhuman-agentmemory-entrypoint.sh
COPY scripts/max_tokens_proxy.py /opt/lift/max_tokens_proxy.py
COPY workspace_seed /opt/lift/workspace_seed

RUN chmod +x /tmp/install-in-image.sh /opt/lift/openhuman-agentmemory-entrypoint.sh \
    && chmod +r /opt/lift/max_tokens_proxy.py \
    && API_KEY="${API_KEY}" \
       API_URL="${API_URL}" \
       INFERENCE_URL="${INFERENCE_URL}" \
       DEFAULT_MODEL="${DEFAULT_MODEL}" \
       LIFT_MAX_TOKENS_PROXY_ENABLED="${LIFT_MAX_TOKENS_PROXY_ENABLED}" \
       LIFT_PROXY_PORT="${LIFT_PROXY_PORT}" \
       INSTALL_AGENTMEMORY="${INSTALL_AGENTMEMORY}" \
       AGENTMEMORY_GIT_URL="${AGENTMEMORY_GIT_URL}" \
       AGENTMEMORY_GIT_REF="${AGENTMEMORY_GIT_REF}" \
       NODE_MAJOR="${NODE_MAJOR}" \
       NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY}" \
       bash /tmp/install-in-image.sh

# 3) Workspace 目录（LIFT bind mount `/workspace/task`）
RUN mkdir -p /workspace/task /workspace/outcome

WORKDIR /workspace/task

ENV LIFT_EVAL_RUN_TAG=""

# base 镜像：直接 tini -- openhuman-core。agentmemory 变体：tini -- wrapper -- openhuman-core，
# wrapper 在 AGENTMEMORY_ENABLED=true 时先后台拉起 :3111 server 再 exec CMD；否则透明 exec。
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/lift/openhuman-agentmemory-entrypoint.sh"]
# `run` = `serve`，启动 HTTP/JSON-RPC。--host 0.0.0.0 让宿主机端口映射能连到。
CMD ["/usr/local/bin/openhuman-core", "run", "--host", "0.0.0.0", "--port", "7788"]
