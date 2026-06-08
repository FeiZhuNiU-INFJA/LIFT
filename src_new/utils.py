import uuid
from datetime import datetime
from pathlib import Path

from src_new.paths import outcome_root


def short_id(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]


def long_id() -> str:
    return uuid.uuid4().hex


def make_run_id(run_id_suffix: str | None = None) -> str:
    if run_id_suffix:
        return f"evobench-runid-{run_id_suffix}"
    return f"evobench-runid-{datetime.now().strftime('%Y%m%d')}-{short_id()}"


def outcome_workspace(
    run_id: str, repeat_index: int, phase: str, category_name: str
) -> Path:
    workspace_dir = (
        outcome_root(run_id)
        / f"run-{repeat_index}"
        / phase
        / category_name
    )
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def iter_benchmark_paths(benchmark_dir: Path) -> list[Path]:
    if not benchmark_dir.is_dir():
        raise ValueError(f"--benchmark_dir must be a directory: {benchmark_dir}")
    resolved_dir = benchmark_dir / "benchmarks" if benchmark_dir.name == "assets" else benchmark_dir
    if not resolved_dir.is_dir():
        raise ValueError(f"Benchmark directory not found: {resolved_dir}")
    return sorted(resolved_dir.glob("**/*.json"))


def resolve_suite_paths(benchmark_dir: Path, suite: str) -> list[Path]:
    all_paths = iter_benchmark_paths(benchmark_dir)
    if not all_paths:
        raise ValueError(f"No suite JSON files found in {benchmark_dir}")
    if suite == "all":
        return all_paths
    suite_names = [name.strip() for name in suite.split(",")]
    allowed_stems = {name if name.endswith(".json") else f"{name}.json" for name in suite_names}
    missing = allowed_stems - {path.name for path in all_paths}
    if missing:
        available = [path.name for path in all_paths]
        raise ValueError(
            f"--suite specified non-existent file(s): {sorted(missing)}. "
            f"Available: {available}"
        )
    return [path for path in all_paths if path.name in allowed_stems]