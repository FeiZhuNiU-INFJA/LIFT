# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LIFT (Loaded Impact on Final Task) is an evaluation framework for **agent self-evolution**. It does not measure an agent's out-of-the-box ability — it measures whether an agent actually improves, and by how much, on holdout tasks after completing warmup tasks and triggering one evolve step.

**Core paradigm**: every holdout task is run twice — once with a clean baseline image, and once with a delta image committed after warmup + evolve. The diff measures improvement.

Agents are hosted in Docker containers (OpenClaw and other runtimes); the pipeline is a single-process asyncio orchestrator. Container fan-out is `max_parallel_suites × per-cell task parallelism × 2 phases`; with defaults (3 parallel cells, moderate holdout task counts) peak is on the order of a few dozen concurrent containers, and can climb into the hundreds when `--max-parallel-suites` is raised.

## Common Commands

> **Python 环境约定**：所有 `python` / `pytest` 命令默认在 `lift` conda 环境执行。非交互调用直接用绝对路径 `/root/miniconda3/envs/lift/bin/python`；交互终端可先 `conda activate lift`。系统 `/usr/bin/python` **未安装项目依赖**（缺 pydantic 等），会立即报错。

```bash
# Build the OpenClaw evaluation image (rebuild required after runtime changes)
# Default builds the base image; pass --with-evolve to include the evolution plugin
bash agent-runtimes/openclaw/build-image.sh                # → lift-openclaw-base:latest
bash agent-runtimes/openclaw/build-image.sh --with-evolve  # → lift-openclaw-with-evolve:latest
# OpenSpace MCP plugin (quality-first skill hub); mutually exclusive with --with-evolve (pick one)
bash agent-runtimes/openclaw/build-image.sh --with-openspace                # → lift-openclaw-with-openspace:latest
# Hermes OpenSpace variant
bash agent-runtimes/hermes/build-image.sh --with-openspace  # → lift-hermes-with-openspace:latest

# ByteDance intranet build (defaults go through public mirrors; switch via env vars)
APT_MIRROR=http://mirrors.byted.org \
PIP_INDEX_URL=https://bytedpypi.byted.org/simple/ \
bash agent-runtimes/openclaw/build-image.sh --with-evolve

# Smoke test (warmup + delta only, skips holdout)
python -m src.cli.lift_main -r openclaw \
  --benchmark_dir assets/benchmarks_demo --suite hello.json \
  --run_id smoke-test --warmup-only

# Full LIFT evaluation (with terminal TUI + browser dashboard)
python -m src.cli.lift_main -r openclaw \
  --benchmark_dir assets/benchmarks_demo --suite hello.json \
  --run_id my-run --tui --dashboard 8080

# Post-process only (rebuild dashboard / metrics from existing report.json)
python -m src.cli.lift_main -r openclaw --evaluate-only --run_id my-run

# Pull benchmark markdowns from TOS / HuggingFace / ModelScope and convert to suite JSON
python -m src.cli.preprocess

# Unit tests
python -m pytest src/lift/tests -q
```

## Architecture

### Three-Layer Design

```
CLI / Pipeline (src/cli, src/lift/pipeline)
    ↓ orchestrates repeat × suite × phase, writes report, mounts status dashboard
AgentRuntimeAdapter (src/lift/adapters)
    ↓ runtime-specific: container lifecycle, evolve hooks, chat interface, delta materialization
lift/eval (src/lift/eval)
    ↓ evaluation kernel: per-task work↔judge multi-turn loop (runtime-agnostic)
```

### Key Components

| Component | Path | Purpose |
|-----------|------|---------|
| CLI entry | `src/cli/lift_main.py` | Argument parsing; dispatches full run vs. `--evaluate-only` |
| Pipeline | `src/lift/pipeline/lift_pipeline.py` | Flattens repeat × suite into cells; cell-level concurrency + failure retry |
| Adapter base | `src/lift/adapters/base.py` | `AgentRuntimeAdapter` template: `produce_delta` / `run_before_load` / `run_after_load` |
| Container adapter | `src/lift/adapters/container/` | Generic Docker lifecycle, `docker commit` to materialize delta |
| OpenClaw base | `src/lift/adapters/openclaw/` | OpenClaw container, chat, `agents add --model`, trajectory.jsonl parsing |
| OpenClaw with evolve | `src/lift/adapters/openclaw_with_evolve/` | Runs `openclaw learn review` after warmup, plus signal pipeline |
| Group memory mixin | `src/lift/adapters/group_memory/` + `openclaw_multi_user/` | Multi-container warmup, evolve writes to external memory service, delta reuses base image |
| GenericAgent | `src/lift/adapters/genericagent[_active_evolve]/` | File-I/O-style agent; `_active_evolve` variant adds active reflection per task / suite |
| Task evaluation | `src/lift/eval/run_task.py` | work↔judge multi-turn + provider retry + judge JSON parse retry |
| Hermes | `src/lift/adapters/hermes/` | Docker exec runner; review-driven implicit evolution under `/opt/hermes-state` |
| OpenHuman | `src/lift/adapters/openhuman/` | Rust core JSON-RPC runtime; transcript push to Langfuse |
| EvoScientist | `src/lift/adapters/evoscientist[_active_evolve]/` | `EvoSci -p ... --output-format stream-json`; `_active_evolve` runs EvoMemory AutoSkills after warmup |
| Data models | `src/models.py` | `Suite` / `EvalReport` / `PhaseRun` / `CustomTags` / Langfuse trace schema |
| Status bus | `src/lift/status/` | Event bus + state aggregator + TUI / HTTP dashboard + replay |
| Post-processing | `src/postprocess/run_post_process.py` | trace backfill, metric extraction, trajectory judge, CSV / HTML reports |

Supported runtimes (`-r` values, see `src/lift/adapters/registry.py`):
- `openclaw` — base image with no explicit evolve; OpenClaw's natural skill/memory changes during warmup are carried into the delta via `docker commit`
- `openclaw_with_evolve` — evolution plugin variant; runs `openclaw learn review` after warmup
- `openclaw_with_openspace` — OpenClaw + OpenSpace MCP plugin (skill hub); reuses base warmup/evolve/commit flow (`INSTALL_OPENSPACE=true`, image `lift-openclaw-with-openspace`)
- `multi_user_openclaw` — OpenClaw + group memory mixin; multi-container warmup (`parallel_multi`), evolve writes to external memory service
- `genericagent` / `genericagent_active_evolve` — file-I/O-style agent; the latter performs an extra active-reflection pass
- `hermes` — Hermes runner via `docker exec`; warmup **must** use `serial_single` (concurrent writes to `/opt/hermes-state` race the review process)
- `hermes_with_openspace` — Hermes + OpenSpace MCP plugin (registered as `mcp_servers.openspace` in `config.yaml`); reuses Hermes review/commit flow (image `lift-hermes-with-openspace`)
- `openhuman` — Rust core `serve` with JSON-RPC over HTTP; chat goes through `agent.chat`; `reasoning_tokens` is folded into `output_tokens` by upstream schema and reported as `null(counted_into_output)`
- `evoscientist` — EvoScientist CLI headless runtime (`EvoSci -p ... --output-format stream-json`); runtime conversation continuity uses captured `--resume <thread_id>`, not LIFT observability `session_id`
- `evoscientist_active_evolve` — EvoScientist + explicit EvoMemory AutoSkills hook after warmup; reuses `lift-evoscientist:latest`, waits for LangGraph run completion, then commits `/root/.evoscientist`

> MCP support: OpenSpace is only added to MCP-capable runtimes (OpenClaw, Hermes). GenericAgent (fixed atomic tools) and OpenHuman (no `mcp_servers`) are not MCP clients, so they get no OpenSpace variant.

### Evaluation Flow

1. **Warmup**: warmup tasks within a suite run in a shared container (or multiple containers); state accumulates in the container layer
2. **Evolve**: `evolve_after_task` (per-task hook, no-op by default) + `evolve_after_warmup` (post-batch hook; OpenClaw = `learn review`)
3. **Delta materialization**: `docker commit` to a temporary image `lift-delta:{run_id}-r{repeat}-{suite_name}`; non-image deltas are flagged via `DeltaRef.owned=False`
4. **Holdout**: each holdout task starts 2 containers for baseline / evolved — baseline from the base image, evolved from the delta image (or same image + `load_state` injection)
5. **Report**: writes `results/{run_id}/report.json` with `success` / `content_score` / `turns` / `tool_calls` / session id, etc.
6. **Post-process** (default, `-e`): pulls Langfuse traces for backfill, extracts metrics, emits CSV / HTML / static dashboard snapshot

## Data Model

```
EvalReport
  └── runs[]            (EvalRepeat: one iteration of --repeat)
        └── suites[]    (SuiteRun: one *.json)
              └── tasks[] (TaskRun: holdout tasks only)
                    ├── baseline: PhaseRun (success / content_score / turns / tool_calls / langfuse / workspace_dir)
                    └── evolved:  PhaseRun
```

- **Suite JSON**: `Suite` (`src/models.py`) explicitly separates `warmup_tasks` from `holdout_tasks`
- **Demo suite**: `assets/benchmarks_demo/hello.json` (shipped with the repo, used for smoke tests)
- **Full benchmarks**: `assets/benchmarks/*.json` (generated by preprocess, not in git)
- **Suite display name**: `suite_name` on dashboards / container labels is forced to read from the JSON `name` field, decoupled from the file name
- **Two-phase report fill**: success / score / session id are written during execution; `PhaseRun.langfuse` is backfilled by the post-process `trace_backfill` step

## Token Accounting (5 fields)

LIFT enforces a fixed 5-field token schema across all runtimes (see `LangfuseTraceTokens` in `src/models.py`):

- `input_tokens` — fresh input, **excludes** cache
- `cache_write_tokens` — first-time cache writes (Anthropic-style; OpenAI-family is always 0)
- `cache_read_tokens` — cache hits
- `output_tokens` — total completion tokens; **includes** `reasoning_tokens` as a subset
- `reasoning_tokens` — extended-thinking / o-series reasoning, `⊂ output_tokens`

`total_tokens = input + cache_write + cache_read + output` — do **not** add `reasoning` again (double counts). Per-runtime status matrix and known gotchas live in `skill/lift-integrate-agent-runtime/docs/token-observability.md`; the full narrative of the cross-runtime fix is in `docs/release-notes/2026-07-16-token-5-fields-observability.md`.

## Key Constraints

- **Model configuration**: `MODEL_NAME` in `.env` is `provider/model_id` with the provider fixed to `custom` by convention (e.g. `custom/ep-...`). At OpenClaw image build, `models.fragment.json` registers the `custom` provider and derives `model.id` from the part after `/`; `agents.fragment.json` uses the full `MODEL_NAME` as the primary model key. Fragments are baked into the image, so adding a new provider/model requires updating the fragment and rebuilding.
- **Delta naming**: `lift-delta:{run_id}-r{repeat}-{suite_name}`; suite cleanup via `SuiteRunResources.cleanup()` runs `docker rmi` (not for `owned=False`, so base images aren't accidentally deleted)
- **Workspace layout (OpenClaw)**:
  - The agent's default workspace inside the container is fixed at `/root/.openclaw/workspace` (container FS) so warmup-produced memory / skills are captured by `docker commit`
  - The bind-mount point for task materials and artifacts is `/workspace/task` — this is **not** captured in the delta
  - A startup hook symlinks `qN_materials/` under `/workspace/task` into the workspace; artifact directories are symlinked back to the mount point so the host and the eval system can read them
  - Workspace seeds (`SOUL.md` / `IDENTITY.md` / `USER.md` etc.) are baked at build time by `COPY workspace_seed /root/.openclaw/workspace` in the Dockerfile — no host-side copy
- **Holdout workspace isolation**: each holdout task has an isolated directory at `results/{run_id}/outcome/run-{i}/{baseline|evolved}/{category}/{task}/`
- **Container orchestration policies**:
  - Warmup: `parallel_single` (default; concurrent inside one container) / `serial_single` / `parallel_multi` (multi-container, group-memory only)
  - Holdout: `parallel_multi` (default) / `serial_multi` — each task must use its own container
  - Within a task, baseline vs. evolved: `parallel` (default) / `serial`
- **Concurrency & resources**: `--max-parallel-suites` (default 3) caps cells; `--max-concurrent-tasks` caps per-phase task containers; `--container-memory` / `--container-cpus` pass through to `docker run` — **no per-container cap by default** (OpenClaw peaks can exceed 3g; hard cgroup limits tend to trigger OOM-kill, so overall memory is left to VM kernel + swap)
- **Failure isolation + auto-retry**: cells are isolated so failures don't cascade; the pipeline collects first-pass failed cells and **retries them once globally**; phases / tasks each retry once at their own layer; the chat layer retries provider errors 5×, judge JSON parse 8×
- **Port allocation**: containers use `-p 0:N` so Docker picks an ephemeral port; the actual port is recovered via `docker inspect` after startup, avoiding collisions under concurrency

## Environment Variables (`.env` required)

```env
# Agent model (provider fixed to "custom"; suffix after "/" is the model id)
MODEL_NAME=custom/ep-20260529115331-9zxpm

# Work LLM (the agent under evaluation; OpenAI-compatible)
WORK_OPENAI_API_KEY=your_work_api_key
WORK_OPENAI_BASE_URL=https://your-openai-compatible-endpoint

# Langfuse observability (required for trace_backfill)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=http://localhost:3000

# Post-process trajectory judge (optional)
DO_TRAJECTORY_JUDGE=false
TRAJECTORY_JUDGE_OPENAI_API_KEY=your_judge_api_key
TRAJECTORY_JUDGE_OPENAI_BASE_URL=https://your-openai-compatible-endpoint
TRAJECTORY_JUDGE_MODEL=gpt-4o-mini

# Benchmark source (TOS, HuggingFace, or ModelScope)
BENCHMARK_SOURCE=tos                # or huggingface / modelscope
TOS_ACCESS_KEY=your_access_key
TOS_SECRET_KEY=your_secret_key
# BENCHMARK_HF_REPO=FeiZhuNiU-INFJA/EALE
# BENCHMARK_MODELSCOPE_REPO=iKlare/EALE
```

## OpenClaw Runtime Integration

- **Image tags**: `src/paths.py` defines `OPENCLAW_BASE_DOCKER_IMAGE` and `OPENCLAW_WITH_EVOLVE_DOCKER_IMAGE`, corresponding to `INSTALL_SELF_EVOLVING=false/true`
- **Plugins**: `langfuse-tracer`, `self-evolving-plugin-pro`, firecrawl-plugin, etc. are baked at build time and not downloaded at runtime
- **Chat channel**: `docker exec openclaw agent --local --json --session-id <sid>`; stdout JSON is parsed into the chat result; a 1000s safety timeout applies per call (timeouts retry as provider errors). Other runtimes use similar per-call ceilings (GenericAgent / OpenHuman = 1000s, Hermes = 1200s) — see `CHAT_EXEC_TIMEOUT_SECONDS` in each adapter's `chat_agent.py`
- **Langfuse correlation contract**:
  - Same Langfuse project (host and container share keys / host)
  - Same `session_id` (pre-chat span and in-container plugin trace share it)
  - Plugin trace name ∈ `{"openclaw-plugin", "Hermes turn", "genericagent-plugin", "openhuman-plugin", "evoscientist-plugin"}` (see `LANGFUSE_PLUGIN_TRACE_NAMES` in `src/models.py`)
  - Retries do **not** emit a new `*_agent` pre-chat span; post-process consumes multiple plugin traces via an extended greedy pairing
- **Workspace seed**: `agent-runtimes/openclaw/workspace_seed/` is `COPY`'d into `/root/.openclaw/workspace` at build time. It contains `SOUL.md` / `IDENTITY.md` / `USER.md` / `AGENTS.md` / `TOOLS.md` / `HEARTBEAT.md`. The old host-side copy logic has been removed.

## Status Visualization

- `--tui`: terminal TUI (`rich.Live`), best for foreground / tmux viewing; when enabled, console logging is muted to protect the render area while file logging continues
- `--dashboard [HOST:]PORT`: browser dashboard, zero-dep stdlib `http.server`; clicking any task expands the full work↔judge dialogue
- Static snapshot: each run automatically writes `results/{run_id}/dashboard.html`; `--evaluate-only` replays the report to rebuild it
- Do not use `nohup … --tui`: `rich.Live` needs a tty — redirecting to a file produces escape-code soup

## Testing

- `src/lift/tests/mock_adapter.py` for Docker-free tests
- Pipeline concurrency: `src/lift/tests/test_pipeline.py`
- Task evaluation kernel: `src/lift/tests/test_run_task.py`
- Abstract contracts: `src/lift/tests/test_abc_contracts.py`
- Group memory mixin: `src/lift/tests/test_group_memory_mixin.py`
- Status bus / dashboard: `src/lift/tests/test_dialogue_events.py`, `test_static_dashboard_export.py`

## Priority Reading

- End-to-end flow: `src/lift/pipeline/lift_pipeline.py`
- Runtime interface contract: `src/lift/adapters/base.py`
- Single-task evaluation kernel: `src/lift/eval/run_task.py`
- Suite JSON spec: `assets/suite_requirement.md`
- Chinese protocol deep-dive (preferred long-form): `docs/lift-framework-guide-cn.md`
- Flow / data / post-process details: `docs/eval-flow.md`
- OpenClaw image build: `agent-runtimes/openclaw/README.md`
- EvoScientist image build / AutoSkills active evolve: `agent-runtimes/evoscientist/README.md`
- Cross-module change narratives: `docs/release-notes/`
