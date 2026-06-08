# LIFT (`src_new`)

Load-state Isolated Final-task Test — container-per-task implementation.

**中文阅读指南（目录结构、OpenClaw 适配、推荐阅读顺序）**：[docs/lift-framework-guide-cn.md](../../docs/lift-framework-guide-cn.md)

## Architecture

- **Host**: `LIFTPipeline`, report JSON, preprocess/postprocess (`src_new`)
- **Container**: OpenClaw agent only ([`agents/openclaw/`](../../agents/openclaw/))
- **before-load**: fresh container from base image (`evolve-eval-openclaw:latest`)
- **after-load**: fresh container from **delta image** (`docker commit` after warmup)
- **Isolation**: each hold-out task gets its own before/after containers; shared delta, per-task workspace
- **Cleanup**: `SuiteRunResources.cleanup()` removes containers and delta images

## Build image

```bash
bash agents/openclaw/build-image.sh
# or: docker build -f agents/openclaw/Dockerfile -t evolve-eval-openclaw:latest agents/openclaw
```

Ephemeral entrypoint variant (optional): `agents/openclaw/Dockerfile.entrypoint`

## Run

```bash
# Canonical entry (runtime-agnostic CLI)
python -m src_new.cli.lift_main --runtime openclaw --suite hello.json --warmup-only

python -m src_new.cli --runtime openclaw --suite hello.json --warmup-only

# Full LIFT
python -m src_new.cli.lift_main --runtime openclaw --suite hello.json

# Parallel repeats (default)
python -m src_new.cli.lift_main --runtime openclaw --suite hello.json --repeat 3
```

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--runtime` | *(required)* | Agent adapter; also selects base Docker image via registry |
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
python -m src_new.lift.tests.test_holdout
python -m src_new.lift.tests.test_runtime
python -m src_new.lift.tests.test_pipeline
```

## Delta image naming

`evolve-eval-delta:{run_id}:r{repeat}:{suite_name}` — removed by `SuiteRunResources.cleanup()` after each suite run.
