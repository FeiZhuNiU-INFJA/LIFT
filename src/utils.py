"""Shared helpers for CLI, LIFT pipeline, and OpenClaw adapters."""

import shutil
import uuid
from datetime import datetime
from pathlib import Path

from src.config import LOGGER
from src.paths import outcome_root


def short_id(n: int = 8) -> str:
    """Return a random hex string for ephemeral names (containers, sessions)."""
    return uuid.uuid4().hex[:n]


def long_id() -> str:
    """Return a full-length random hex string (32 chars)."""
    return uuid.uuid4().hex


def make_run_id(run_id_suffix: str | None = None) -> str:
    """Build the canonical run id used in reports and ``results/{run_id}/``.

    With ``run_id_suffix`` (from ``--run_id``), returns ``lift-runid-{suffix}``.
    Otherwise generates ``lift-runid-{YYYYMMDD}-{short_id}``.
    """
    if run_id_suffix:
        return f"lift-runid-{run_id_suffix}"
    return f"lift-runid-{datetime.now().strftime('%Y%m%d')}-{short_id()}"


def outcome_workspace(
    run_id: str, repeat_index: int, phase: str, category_name: str
) -> Path:
    """Host path for one holdout task workspace, created if missing.

    Layout: ``results/{run_id}/outcome/run-{repeat}/{phase}/{category}/``.
    Mounted into containers as the per-task working directory.
    """
    workspace_dir = (
        outcome_root(run_id)
        / f"run-{repeat_index}"
        / phase
        / category_name
    )
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def stage_task_materials(workspace_dir: Path, material_dir: str | None) -> None:
    """把 task 的 materials 目录按**原目录名**复制进 ``workspace_dir``。

    benchmark 的 query 一律以工作区相对路径引用材料（如 ``q1_materials/``），而 agent
    的工作目录就是被挂载的 ``workspace_dir``（容器内 ``/workspace/task``）。因此须把
    ``material_dir``（如 ``.../q1_single_table_summary/q1_materials``）整目录复制到
    ``workspace_dir / q1_materials``，agent 才能按 query 中的名字找到材料。

    空值或目录不存在时静默跳过。复制采用 ``dirs_exist_ok=True``，重复 stage 不报错。
    """
    if not material_dir or not str(material_dir).strip():
        return
    source = Path(material_dir).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    if not source.is_dir():
        LOGGER.warning("Material dir not found, skip staging: %s", source)
        return
    workspace_dir.mkdir(parents=True, exist_ok=True)
    destination = workspace_dir / source.name
    shutil.copytree(source, destination, dirs_exist_ok=True)
    LOGGER.info("Staged materials into workspace: %s -> %s", source, destination)


def _resolved_benchmark_dir(benchmark_dir: Path) -> Path:
    """Normalize ``assets`` → ``assets/benchmarks``."""
    return benchmark_dir / "benchmarks" if benchmark_dir.name == "assets" else benchmark_dir


def iter_benchmark_paths(benchmark_dir: Path) -> list[Path]:
    """List all suite JSON files under a benchmark directory (recursive).

    Resolves ``assets`` to ``assets/benchmarks``. Raises if the path is not a
    directory or contains no ``*.json`` files when used by callers.
    """
    resolved_dir = _resolved_benchmark_dir(benchmark_dir)
    if not resolved_dir.is_dir():
        raise ValueError(f"Benchmark directory not found: {resolved_dir}")
    return sorted(resolved_dir.glob("**/*.json"))


def resolve_suite_paths(benchmark_dir: Path, suite: str) -> list[Path]:
    """Resolve ``--benchmark_dir`` + ``--suite`` into concrete suite JSON paths.

    Steps:
    1. Scan ``benchmark_dir`` for all ``*.json`` suite files.
    2. If ``suite`` is ``"all"``, return every file found.
    3. Otherwise split ``suite`` by comma (e.g. ``hello.json,foo``), normalize
       each name to end with ``.json``, verify each exists, and return matches.

    Raises ``ValueError`` when the directory is empty or a requested name is missing.
    """
    all_paths = iter_benchmark_paths(benchmark_dir)
    if not all_paths:
        resolved_dir = _resolved_benchmark_dir(benchmark_dir)
        raise ValueError(f"No suite JSON files found in {resolved_dir}")
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
