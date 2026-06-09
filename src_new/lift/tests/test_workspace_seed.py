"""Unit tests for OpenClaw eval workspace seeding.

OpenClaw 评测工作区种子文件的单元测试。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from src_new.lift.adapters.openclaw.workspace_seed import (
    WORKSPACE_READY_MARKER,
    seed_eval_workspace,
)


def test_seed_eval_workspace_copies_identity_and_removes_bootstrap() -> None:
    """Verify seeding copies IDENTITY.md, removes BOOTSTRAP.md, and writes ready marker.

    验证种子化会复制 IDENTITY.md、删除 BOOTSTRAP.md 并写入就绪标记。
    """
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "task"
        seed_eval_workspace(workspace)
        assert (workspace / "IDENTITY.md").is_file()
        assert "EvoBench Assistant" in (workspace / "IDENTITY.md").read_text(encoding="utf-8")
        assert not (workspace / "BOOTSTRAP.md").exists()
        assert (workspace / WORKSPACE_READY_MARKER).is_file()
