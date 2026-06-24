# OpenClaw agent (`agent-runtimes/openclaw`)

Gateway image with **self-evolving-plugin-pro** and **langfuse-tracer** pre-installed. LIFT (`src`) runs agents via **`docker exec … openclaw`** inside ephemeral per-task containers.

## Layout

```
agent-runtimes/openclaw/
├── .dockerignore          # build context excludes (context = this directory)
├── Dockerfile
├── build-image.sh
├── config/                # openclaw.json fragments
├── workspace_seed/        # pre-filled IDENTITY/USER/SOUL (no BOOTSTRAP.md)
├── plugins/
│   ├── langfuse-tracer/
│   └── self-evolving-plugin-pro-2026.4.23.zip
└── scripts/
    └── openclaw-instance.sh
```

## Build (recommended)

From the **repository root**:

```bash
bash agent-runtimes/openclaw/build-image.sh
```

默认产出 `evolve-eval-openclaw-base:latest`（不带 self-evolving-plugin-pro 进化插件，对应 LIFT `-r openclaw`，包含 `workspace_seed` 于 `/opt/evolve-eval/workspace_seed`）。

变体（带 self-evolving-plugin-pro 进化插件）：

```bash
bash agent-runtimes/openclaw/build-image.sh --with-evolve
# 产出 evolve-eval-openclaw-with-evolve:latest
```

LIFT copies this seed into each task workspace before mount so agents skip first-run onboarding.

### 内网/外网构建（base image / APT / PyPI 镜像源）

构建脚本默认走公网官方源（`ghcr.io/openclaw/openclaw:latest` + `deb.debian.org` +
`https://pypi.org/simple/`）；公网拉取不稳定时（如字节内部环境），通过三个环境变量
切换镜像源即可，**Dockerfile 与脚本本身无需改动**。

| 环境 | 命令 |
|------|------|
| 公网（默认） | `bash agent-runtimes/openclaw/build-image.sh` |
| 字节内网 | `OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \`<br>`APT_MIRROR=http://mirrors.byted.org \`<br>`PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \`<br>`  bash agent-runtimes/openclaw/build-image.sh` |
| 其它内网/自建源 | 把 `OPENCLAW_BASE_IMAGE` 指向已镜像同步过 `ghcr.io/openclaw/openclaw` 的内网 registry；`APT_MIRROR` 指向布局与官方一致的 Debian 镜像（`<APT_MIRROR>/debian` + `<APT_MIRROR>/debian-security`）；`PIP_INDEX_URL` 指向 PEP 503 兼容的 simple 索引 |

构建两个变体（base 和 with-evolve）的完整命令：

```bash
# 字节内网
OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/openclaw/build-image.sh                # → evolve-eval-openclaw-base:latest

OPENCLAW_BASE_IMAGE=ghcr.milu.moe/openclaw/openclaw:latest \
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
  bash agent-runtimes/openclaw/build-image.sh --with-evolve  # → evolve-eval-openclaw-with-evolve:latest
```

注意：
- `OPENCLAW_BASE_IMAGE` 决定 `FROM` 这一步去哪里拉基础镜像。**Docker daemon 的
  `registry-mirrors` 只代理 `docker.io`，不代理 `ghcr.io`**，所以仅在 daemon.json
  里加加速器对 base image pull 无效，必须在这里显式切源。首次拉完之后本地有缓存，
  后续 build 即便 `docker pull` 失败也会自动 fallback 到本地镜像（脚本里 `|| echo
  WARN` 兜底）。
- `APT_MIRROR` 仅在构建期生效（用于 `apt-get install` 系统包）。
- `PIP_INDEX_URL` 既影响构建期（uv / pip 装 self-evolving plugin 等），也写入运行
  期 ENV——容器内 plugin 装包时也走这里。
- 同时切换的还有仓库根 `requirements.txt` 顶部的 `--extra-index-url`（用于宿主机
  conda 环境装 `bytedtos` 等内部包）；纯外网用户若不需要 `bytedtos` 可直接删那行。

Verify:

```bash
bash agent-runtimes/openclaw/verify-image.sh evolve-eval-openclaw-with-evolve:latest
```

## Environment

Copy [`.env.docker.example`](.env.docker.example) into the repo root `.env`:

本镜像构建时 merge `config/plugins.fragment.json`、`config/gateway.fragment.json`、`config/agents.fragment.json`、`config/skills.fragment.json` 与 `config/models.fragment.json` 进容器内 `openclaw.json`，并遵守仓库公共契约：[Agent 模型配置契约](../../docs/eval-flow.md#126-agent-模型配置契约lift--容器运行时)（能力在 fragment / 选用在 `.env` `MODEL_NAME`）。

- `ARK_API_KEY` — **required for image build**; injected into `config/models.fragment.json`
- `MODEL_NAME` — runtime model id for LIFT `agents add --model`（须为 fragment 中已登记的 `provider/model_id`）
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — runtime
- `LANGFUSE_BASE_URL` — use `http://host.docker.internal:3000` inside containers
- LIFT `ContainerSession` adds `--add-host=host.docker.internal:host-gateway` (required on Linux for langfuse-tracer ingestion)
- `LIFT_EVAL_RUN_TAG` — set by LIFT to `run_id`; plugin adds it to trace tags (pairs with framework pre-chat `tags.run`)

### Langfuse correlation with LIFT pre-chat

Each chat turn produces two Langfuse traces: framework **`emit_pre_chat_state`** (`work_agent` / `judge_agent`) on the host, and **`langfuse-tracer`** (`openclaw-plugin`) inside the container. Postprocess stitches them by **`session_id`** (same value as `openclaw agent --session-id`).

Plugin requirements: same Langfuse project keys, trace name `openclaw-plugin`, `hooks.allowConversationAccess: true` in `openclaw.json`, and `sessionId` on the trace must match LIFT’s `--session-id`.

Full write / fetch / pairing contract: [docs/eval-flow.md §12.5](../../docs/eval-flow.md#125-trace_backfill观测).

### Task extra skills

Benchmark tasks may set `requirements.extra_skills_dir`. For OpenClaw containers, LIFT mounts that directory at `/workspace/task/skills`, matching the configured agent workspace `/workspace/task` and OpenClaw's `<workspace>/skills` loading convention.

The image config also sets `skills.load.extraDirs` to `/workspace/task/skills` as an explicit fallback. At container startup LIFT copies the mounted skills into `${OPENCLAW_STATE_DIR:-/root/.openclaw}/skills` so warmup `docker commit` captures task skills in evolved delta images; the bind-mounted `/workspace/task/skills` content itself is not captured by Docker commit.

## LIFT integration (`src`)

```bash
bash agent-runtimes/openclaw/build-image.sh
python -m src.cli.lift_main -r openclaw_with_evolve --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only
python -m src.cli.lift_main -r openclaw_with_evolve --benchmark_dir assets/benchmarks_demo --suite hello.json
```

Default images:

- `-r openclaw` → `evolve-eval-openclaw-base:latest`（不带进化插件，常量 `OPENCLAW_BASE_DOCKER_IMAGE`）
- `-r openclaw_with_evolve` → `evolve-eval-openclaw-with-evolve:latest`（带 self-evolving-plugin-pro，常量 `OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE`）

均定义于 `src/paths.py`。

## Instance lifecycle (manual debugging)

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh create --id run-a
eval "$(./agent-runtimes/openclaw/scripts/openclaw-instance.sh env run-a)"
./agent-runtimes/openclaw/scripts/openclaw-instance.sh destroy run-a
```

Commit warmup container to delta image:

```bash
./agent-runtimes/openclaw/scripts/openclaw-instance.sh commit run-a --tag evolve-eval-delta:my-run-r0-suite
```

## Compose (optional)

```bash
docker compose -f agent-runtimes/openclaw/compose.openclaw.yml up -d --build
```
