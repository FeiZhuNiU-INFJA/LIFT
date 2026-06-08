from __future__ import annotations

import tempfile
from pathlib import Path

from src_new.lift.adapters.openclaw.workspace_seed import (
    WORKSPACE_READY_MARKER,
    seed_eval_workspace,
)


def test_seed_eval_workspace_copies_identity_and_removes_bootstrap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp) / "task"
        seed_eval_workspace(workspace)
        assert (workspace / "IDENTITY.md").is_file()
        assert "EvoBench Assistant" in (workspace / "IDENTITY.md").read_text(encoding="utf-8")
        assert not (workspace / "BOOTSTRAP.md").exists()
        assert (workspace / WORKSPACE_READY_MARKER).is_file()


def _run_all() -> None:
    test_seed_eval_workspace_copies_identity_and_removes_bootstrap()
    print("workspace_seed tests ok")


if __name__ == "__main__":
    _run_all()
