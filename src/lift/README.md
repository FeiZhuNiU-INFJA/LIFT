# LIFT (`src`)

Loaded Impact on Final Task — container-per-task implementation.

**中文阅读指南（目录结构、OpenClaw 适配、推荐阅读顺序）**：[docs/lift-framework-guide-cn.md](../../docs/lift-framework-guide-cn.md)

## Architecture

Three adapter layers:

1. **`SuiteRunContext`** + **`AgentRuntimeAdapter`** (`adapters/base.py`) — per `(repeat, suite)` coordinates; template `produce_delta` / holdout; calls `lift/eval`
2. **`ContainerAgentRuntimeAdapter`** (`adapters/container/`) — Docker lifecycle; default delta via `docker commit`
3. **`OpenClawAdapter`** (`adapters/openclaw/`) — base image + `start_container` + chat factory; **不带**进化插件，`evolve_after_warmup` 为 no-op；带进化插件的变体为 `OpenClawWithEvolveAdapter`（`adapters/openclaw_with_evolve/`，warmup 后 `openclaw learn review`）

- **before-load**: fresh container from base image (`evolve-eval-openclaw-base:latest` / `evolve-eval-openclaw-with-evolve:latest`)
- **after-load**: fresh container from **delta image** (committed after warmup)
- **Cleanup**: `SuiteRunResources.cleanup()` removes containers and delta images

## Build image

```bash
bash agent-runtimes/openclaw/build-image.sh
# 默认产出 evolve-eval-openclaw-with-evolve:latest（带进化插件）；
# INSTALL_SELF_EVOLVING=false bash agent-runtimes/openclaw/build-image.sh → evolve-eval-openclaw-base:latest（不带进化插件）
```

## Run

```bash
# Canonical entry (runtime-agnostic CLI)
python -m src.cli.lift_main -r openclaw --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only

python -m src.cli -r openclaw --benchmark_dir assets/benchmarks_demo --suite hello.json --warmup-only

# Full LIFT (default post-process / evaluation)
python -m src.cli.lift_main -r openclaw --benchmark_dir assets/benchmarks_demo --suite hello.json --run_id my-run

# Parallel repeats (default)
python -m src.cli.lift_main -r openclaw --benchmark_dir assets/benchmarks_demo --suite hello.json --repeat 3
```

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-r / --agent-runtime` | *(required)* | Agent adapter; also selects base Docker image via registry |
| `--warmup-only` | off | Warmup + evolve + delta only; skip holdout contrast |
| `--warmup-container-policy` | `parallel_single` | Warmup container orchestration: `serial_single` / `parallel_single` / `parallel_multi` |
| `--holdout-container-policy` | `parallel_multi` | Holdout container orchestration: `serial_multi` / `parallel_multi` |
| `--max-parallel-suites` | `3` | Cap parallel cells in the suites x repeats matrix (one cell = one (repeat, suite) pair); `1` for serial, `<=0` for no cap |
| `--max-concurrent-tasks` | unlimited | Cap concurrent task containers within a suite |

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
