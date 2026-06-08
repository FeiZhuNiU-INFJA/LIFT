from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src_new.models import SuiteTask


class ArtifactPolicy(Protocol):
    @property
    def warmup_tasks(self) -> list[SuiteTask]: ...


@dataclass(frozen=True)
class WarmupThenUpdatePolicy:
    """Default: run warmup tasks, then trigger artifact update (evolve)."""

    _warmup_tasks: list[SuiteTask]

    @property
    def warmup_tasks(self) -> list[SuiteTask]:
        return self._warmup_tasks
