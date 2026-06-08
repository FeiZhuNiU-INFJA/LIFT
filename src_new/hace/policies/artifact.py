from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import override

from src_new.models import SuiteTask


class ArtifactPolicy(ABC):
    """How to produce or refresh UpdateArtifact before hold-out evaluation."""

    @property
    @abstractmethod
    def warmup_tasks(self) -> list[SuiteTask]: ...


@dataclass(frozen=True)
class WarmupThenUpdatePolicy(ArtifactPolicy):
    """Default: run warmup tasks, then trigger artifact update (evolve)."""

    _warmup_tasks: list[SuiteTask]

    @property
    @override
    def warmup_tasks(self) -> list[SuiteTask]:
        return self._warmup_tasks
