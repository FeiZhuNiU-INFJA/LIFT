# LIFT (`src`)

Loaded Impact on Final Task — container-per-task implementation.

**中文阅读指南（目录结构、OpenClaw 适配、推荐阅读顺序）**：[docs/lift-framework-guide-cn.md](../../docs/lift-framework-guide-cn.md)

## Architecture

Three adapter layers:

1. **`SuiteRunContext`** + **`AgentRuntimeAdapter`** (`adapters/base.py`) — per `(repeat, suite)` coordinates; template `produce_delta` / hold-out; calls `lift/eval`
2. **`ContainerAgentRuntimeAdapter`** (`adapters/container/`) — Docker lifecycle; default delta via `docker commit`
3. **`OpenClawAdapter`** (`adapters/openclaw/`) — image config, `start_container`, chat factory, `learn review`

- **before-load**: fresh container from base image (`evolve-eval-openclaw:latest`)
- **after-load**: fresh container from **delta image** (committed after warmup)
- **Cleanup**: `SuiteRunResources.cleanup()` removes containers and delta images

## Build image

```bash
bash agent-runtimes/openclaw/build-image.sh
# or: docker build -f agent-runtimes/openclaw/Dockerfile -t evolve-eval-openclaw:latest agent-runtimes/openclaw
```

Ephemeral entrypoint variant (optional): `agent-runtimes/openclaw/Dockerfile.entrypoint`

## Run

```bash
# Canonical entry (runtime-agnostic CLI)
python -m src.cli.lift_main -r openclaw --suite hello.json --warmup-only

python -m src.cli -r openclaw --suite hello.json --warmup-only

# Full LIFT (default post-process / evaluation)
python -m src.cli.lift_main -r openclaw --suite hello.json --run_id my-run

# Parallel repeats (default)
python -m src.cli.lift_main -r openclaw --suite hello.json --repeat 3
```

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-r / --agent-runtime` | *(required)* | Agent adapter; also selects base Docker image via registry |
| `--warmup-only` | off | Warmup + evolve + delta only; skip hold-out contrast |
| `--warmup-container-policy` | `serial_single` | Warmup in one container |
| `--serial-repeats` | off | Disable parallel repeats |
| `-p` | off | Parallel warmup tasks (within policy) |

## Post-process outputs (`-e` / `--evaluate-only`)

Under `results/{run_id}/`:

| File | Meaning |
|------|---------|
| `{run_id}_backfilled.json` | Report after trace_backfill |
| `{run_id}_comparison_metrics.csv` | Per-task baseline vs evolved |
| `{run_id}_summary_metrics.csv` | Category / global summary |
| `{run_id}_metrics_report.html` | HTML report |

## Tests

```bash
python -m pytest src/lift/tests -q
```

## Delta image naming

`evolve-eval-delta:{run_id}-r{repeat}-{suite_name}` — removed by `SuiteRunResources.cleanup()` after each suite run.
