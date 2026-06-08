from __future__ import annotations

from pathlib import Path

from src_new.models import SuiteTask


def resolve_host_path(path_value: str | None) -> Path | None:
    if not path_value or not str(path_value).strip():
        return None
    p = Path(path_value).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def default_volume_binds(
    *,
    run_id: str,
    repeat_index: int,
) -> list[tuple[str, str, str]]:
    """Host paths bind-mounted into the OpenClaw container."""
    binds: list[tuple[str, str, str]] = []
    outcome_root = Path.cwd() / "results" / run_id / "outcome"
    if outcome_root.is_dir():
        binds.append((str(outcome_root.resolve()), "/workspace/outcome", "rw"))

    benchmarks = Path.cwd() / "assets" / "benchmarks"
    if benchmarks.is_dir():
        binds.append((str(benchmarks.resolve()), "/workspace/benchmarks", "ro"))

    reports = Path.cwd() / "evobench-reports"
    reports.mkdir(parents=True, exist_ok=True)
    binds.append((str(reports.resolve()), "/workspace/evobench-reports", "rw"))
    return binds


def task_volume_binds(task: SuiteTask) -> list[tuple[str, str, str]]:
    """Per-task read-only mounts for skills and materials."""
    binds: list[tuple[str, str, str]] = []
    skills = resolve_host_path(task.requirements.extra_skills_dir)
    if skills is not None and skills.is_dir():
        binds.append((str(skills), "/workspace/skills", "ro"))
    materials = resolve_host_path(task.requirements.material_dir)
    if materials is not None and materials.is_dir():
        binds.append((str(materials), "/workspace/materials", "ro"))
    return binds


def material_digest_for_task(material_dir: str | None) -> str:
    """Stable identifier for hold-out before/after fairness (path-based for now)."""
    resolved = resolve_host_path(material_dir)
    if resolved is None:
        return ""
    return str(resolved)
