"""评测容器的 volume bind 解析（outcome、benchmarks、skills、materials）。"""

from __future__ import annotations

from pathlib import Path

from src_new.models import SuiteTask
from src_new.paths import (
    CONTAINER_BENCHMARKS_ROOT,
    CONTAINER_EVOBENCH_REPORTS_ROOT,
    CONTAINER_OUTCOME_ROOT,
    default_report_root,
    outcome_root,
)


def resolve_host_path(path_value: str | None) -> Path | None:
    """将相对/绝对路径字符串解析为宿主机 ``Path``（空值返回 None）。"""
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
    """评测容器默认 bind mount：outcome、benchmarks、reports。"""
    binds: list[tuple[str, str, str]] = []
    host_outcome = outcome_root(run_id)
    if host_outcome.is_dir():
        binds.append((str(host_outcome.resolve()), CONTAINER_OUTCOME_ROOT, "rw"))

    benchmarks = Path.cwd() / "assets" / "benchmarks"
    if benchmarks.is_dir():
        binds.append((str(benchmarks.resolve()), CONTAINER_BENCHMARKS_ROOT, "ro"))

    reports = default_report_root()
    reports.mkdir(parents=True, exist_ok=True)
    binds.append((str(reports.resolve()), CONTAINER_EVOBENCH_REPORTS_ROOT, "rw"))
    _ = repeat_index
    return binds


def task_volume_binds(task: SuiteTask) -> list[tuple[str, str, str]]:
    """单题只读挂载：extra skills 与 materials 目录。"""
    binds: list[tuple[str, str, str]] = []
    skills = resolve_host_path(task.requirements.extra_skills_dir)
    if skills is not None and skills.is_dir():
        binds.append((str(skills), "/workspace/skills", "ro"))
    materials = resolve_host_path(task.requirements.material_dir)
    if materials is not None and materials.is_dir():
        binds.append((str(materials), "/workspace/materials", "ro"))
    return binds


def material_digest_for_task(material_dir: str | None) -> str:
    """hold-out before/after 公平性用的 material 稳定标识（当前为路径字符串）。"""
    resolved = resolve_host_path(material_dir)
    if resolved is None:
        return ""
    return str(resolved)
