# LIFT Hermes (https://hermes-agent.nousresearch.com) evaluation image.
#
# Build:
#   bash agent-runtimes/hermes/build-image.sh
#
# Manual:
#   docker build -f agent-runtimes/hermes/Dockerfile \
#     --build-arg HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.5.16 \
#     -t evolve-eval-hermes:latest \
#     agent-runtimes/hermes
#
# Design (see .trae/documents/hermes_runtime_integration_plan.md):
#   - Container idles (no `gateway run`); LIFT drives it via
#     `docker exec ... <hermes-venv-python> /opt/evolve-eval/hermes_runner.py`.
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
# genericagent/Dockerfile 的成功做法），否则 install-in-image.sh 的 `uv pip
# install` 会忽略镜像源、回落到 pypi.org 并在受限网络下 DNS 超时。
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Copy the self-contained build context (runner/plugin assets maintained in-tree).
COPY langfuse-hermes /tmp/langfuse-hermes
COPY hermes-helper/hermes_runner.py /opt/evolve-eval/hermes_runner.py
COPY install-in-image.sh /tmp/install-in-image.sh
COPY hermes-bootstrap.sh /opt/evolve-eval/hermes-bootstrap.sh
COPY hermes-entrypoint.sh /opt/evolve-eval/hermes-entrypoint.sh
COPY patch_hermes_config.py /opt/evolve-eval/patch_hermes_config.py

# Firecrawl API key: only injected when non-empty (build-image.sh passes it only
# then). Baked as runtime ENV so the Hermes agent can use firecrawl; also drives
# the build-time `firecrawl-cli init --all` in install-in-image.sh.
ARG FIRECRAWL_API_KEY=
ENV FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}

# 1) Install langfuse + overlay plugin + firecrawl, then init the Hermes state
#    root at /opt/hermes-state in ONE RUN layer (VOLUME /opt/data writes would be
#    dropped by docker commit). Bootstrap seeds dirs + bundled skills at build.
#    install-in-image.sh auto-discovers the Hermes venv / source / plugins dir
#    (paths differ across Hermes image builds; discovered at build time).
ENV HERMES_HOME=/opt/hermes-state
RUN chmod +x /tmp/install-in-image.sh \
        /opt/evolve-eval/hermes-bootstrap.sh \
        /opt/evolve-eval/hermes-entrypoint.sh \
    && mkdir -p /opt/hermes-state /workspace/task \
    && bash /tmp/install-in-image.sh \
    && bash /opt/evolve-eval/hermes-bootstrap.sh

# 2) Ensure Hermes data root + eval task dir exist.
RUN mkdir -p /opt/data /workspace/task

WORKDIR /workspace/task

# Injected by LIFT at `docker run` (see src/lift/adapters/hermes/session.py).
ENV LIFT_EVAL_RUN_TAG=""

# Container idles; LIFT drives Hermes via docker exec runner. The entrypoint
# patches /opt/hermes-state/config.yaml from env, then keeps the container alive.
ENTRYPOINT ["/opt/evolve-eval/hermes-entrypoint.sh"]
CMD ["tail", "-f", "/dev/null"]
