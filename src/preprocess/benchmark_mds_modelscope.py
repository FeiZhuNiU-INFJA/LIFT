"""Download ``benchmark_mds`` from a ModelScope dataset repo as an alternative source."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from src.paths import BENCHMARK_MDS_DIR, DEFAULT_BENCHMARK_MODELSCOPE_REPO

load_dotenv()


class BenchmarkMdsModelScopeFetchError(RuntimeError):
    """Raised when benchmark markdown assets cannot be fetched from ModelScope."""


def _modelscope_dataset_id() -> str:
    dataset_id = os.environ.get("BENCHMARK_MODELSCOPE_REPO", "").strip()
    return dataset_id or DEFAULT_BENCHMARK_MODELSCOPE_REPO


def _modelscope_executable() -> str:
    executable = shutil.which("modelscope")
    if executable is None:
        raise BenchmarkMdsModelScopeFetchError(
            "ModelScope CLI is required to download benchmark_mds from ModelScope. "
            "Install it with `pip install modelscope`."
        )
    return executable


def _validate_downloaded_tree(target_dir: Path) -> None:
    scene_dirs = [
        path
        for path in target_dir.iterdir()
        if path.is_dir() and path.name not in {"__MACOSX"}
    ]
    if not scene_dirs:
        raise BenchmarkMdsModelScopeFetchError(
            f"ModelScope download completed but no benchmark scene directories were found in {target_dir}."
        )


def _remove_duplicate_preview_tree(target_dir: Path) -> None:
    """Drop the optional ``benchmark_mds/`` preview tree when scenes are also at repo root."""
    preview_dir = target_dir / "benchmark_mds"
    if not preview_dir.is_dir():
        return

    sibling_scene_names = {
        path.name
        for path in target_dir.iterdir()
        if path.is_dir() and path.name not in {"__MACOSX", "benchmark_mds"}
    }
    preview_scene_names = {
        path.name
        for path in preview_dir.iterdir()
        if path.is_dir() and path.name not in {"__MACOSX"}
    }
    if sibling_scene_names and preview_scene_names and preview_scene_names.issubset(sibling_scene_names):
        shutil.rmtree(preview_dir)


def download_benchmark_mds_tree_from_modelscope(destination_dir: Path) -> Path:
    """Download markdown benchmark directories from the configured ModelScope dataset."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    dataset_id = _modelscope_dataset_id()
    command = [
        _modelscope_executable(),
        "download",
        "--dataset",
        dataset_id,
        "--local_dir",
        str(destination_dir),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Failed to download benchmark_mds from ModelScope dataset {dataset_id}."
        if detail:
            message = f"{message} {detail}"
        raise BenchmarkMdsModelScopeFetchError(message) from exc

    _remove_duplicate_preview_tree(destination_dir)
    _validate_downloaded_tree(destination_dir)
    return destination_dir


def ensure_benchmark_mds_from_modelscope(
    target_dir: Path | None = None,
    *,
    force: bool = False,
) -> Path:
    """Ensure markdown benchmark assets exist locally, downloading from ModelScope when needed."""
    resolved_target = (target_dir or BENCHMARK_MDS_DIR).resolve()
    if resolved_target.exists() and any(resolved_target.iterdir()) and not force:
        return resolved_target
    if resolved_target.exists() and force:
        shutil.rmtree(resolved_target)

    return download_benchmark_mds_tree_from_modelscope(resolved_target)
