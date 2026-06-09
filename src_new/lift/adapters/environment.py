from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src_new.lift.runtime.disposable import Disposable


@dataclass(frozen=True)
class ExecutionEnvironment:
    """Handle for one warmup or hold-out execution."""

    disposable: Disposable
    workspace_dir: Path
    handle: Any
