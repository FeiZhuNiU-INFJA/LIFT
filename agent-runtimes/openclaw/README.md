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

Produces `evolve-eval-openclaw-with-evolve:latest` by default (includes `workspace_seed` at `/opt/evolve-eval/workspace_seed`).

变体（不带 self-evolving-plugin-pro 进化插件，对应 LIFT `-r openclaw`）：

```bash
INSTALL_SELF_EVOLVING=false bash agent-runtimes/openclaw/build-image.sh
# 产出 evolve-eval-openclaw-base:latest
```

LIFT copies this seed into each task workspace before mount so agents skip first-run onboarding.

Verify:

```bash
bash agent-runtimes/openclaw/verify-image.sh evolve-eval-openclaw-with-evolve:latest
```

## Environment

Copy [`.env.docker.example`](.env.docker.example) into the repo root `.env`:

本镜像构建时 merge `config/models.fragment.json` 与 `config/agents.fragment.json` 进容器内 `openclaw.json`，并遵守仓库公共契约：[Agent 模型配置契约](../../docs/eval-flow.md#126-agent-模型配置契约lift--容器运行时)（能力在 fragment / 选用在 `.env` `MODEL_NAME`）。

- `ARK_API_KEY` — **required for image build**; injected into `config/models.fragment.json`
- `MODEL_NAME` — runtime model id for LIFT `agents add --model`（须为 fragment 中已登记的 `provider/model_id`）
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — runtime
- `LANGFUSE_BASE_URL` — use `http://host.docker.internal:3000` inside containers
- LIFT `ContainerSession` adds `--add-host=host.docker.internal:host-gateway` (required on Linux for langfuse-tracer ingestion)
- `EVOBENCH_EVAL_RUN_TAG` — set by LIFT to `run_id`; plugin adds it to trace tags (pairs with framework pre-chat `tags.run`)

### Langfuse correlation with LIFT pre-chat

Each chat turn produces two Langfuse traces: framework **`emit_pre_chat_state`** (`work_agent` / `judge_agent`) on the host, and **`langfuse-tracer`** (`openclaw-plugin`) inside the container. Postprocess stitches them by **`session_id`** (same value as `openclaw agent --session-id`).

Plugin requirements: same Langfuse project keys, trace name `openclaw-plugin`, `hooks.allowConversationAccess: true` in `openclaw.json`, and `sessionId` on the trace must match LIFT’s `--session-id`.

Full write / fetch / pairing contract: [docs/eval-flow.md §12.5](../../docs/eval-flow.md#125-trace_backfill观测).

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
