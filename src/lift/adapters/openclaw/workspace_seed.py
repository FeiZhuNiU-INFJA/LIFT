"""Pre-seed OpenClaw workspace identity for eval runs (skip BOOTSTRAP onboarding)."""

from __future__ import annotations

import shutil
from pathlib import Path

from src.config import LOGGER
from src.paths import OPENCLAW_WORKSPACE_SEED_DIR

HOST_WORKSPACE_SEED_DIR = OPENCLAW_WORKSPACE_SEED_DIR
CONTAINER_WORKSPACE_SEED_DIR = "/opt/evolve-eval/workspace_seed"  # 镜像内 seed 路径
WORKSPACE_READY_MARKER = ".lift-workspace-ready"  # seed 完成标记文件


def default_workspace_seed_dir() -> Path:
    """默认 OpenClaw eval workspace seed 目录路径。"""
    return HOST_WORKSPACE_SEED_DIR


def seed_eval_workspace(workspace_dir: Path, *, seed_dir: Path | None = None) -> None:
    """Copy eval workspace seed into a host workspace before Docker volume mount."""
    source = seed_dir or default_workspace_seed_dir()
    if not source.is_dir():
        raise FileNotFoundError(f"OpenClaw workspace seed not found: {source}")

    workspace_dir.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir()):
        dest = workspace_dir / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dest)

    (workspace_dir / "BOOTSTRAP.md").unlink(missing_ok=True)
    (workspace_dir / WORKSPACE_READY_MARKER).touch()
    LOGGER.info("Seeded eval workspace: %s <- %s", workspace_dir, source)


def container_workspace_seed_shell() -> str:
    """Run inside container after mount: sync image seed and drop BOOTSTRAP."""
    return f"""
if [[ -d "{CONTAINER_WORKSPACE_SEED_DIR}" ]]; then
  cp -a "{CONTAINER_WORKSPACE_SEED_DIR}/." /workspace/task/ 2>/dev/null || true
fi
rm -f /workspace/task/BOOTSTRAP.md 2>/dev/null || true
touch /workspace/task/{WORKSPACE_READY_MARKER} 2>/dev/null || true
""".strip()
