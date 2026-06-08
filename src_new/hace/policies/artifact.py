from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import override

from src_new.models import SuiteTask


class ArtifactPolicy(ABC):
    """Strategy for producing UpdateArtifact before hold-out evaluation.

    Decouples *how* evolved state is created from *how* it is contrasted on
    the final task. The default HACE path uses warmup tasks plus a runtime
    update hook; other policies may inject external artifacts or skip warmup.
    """

    @property
    @abstractmethod
    def warmup_tasks(self) -> list[SuiteTask]:
        """Tasks to execute before triggering artifact update.

        In the default pipeline these are the non-hold-out prefix of the suite
        (``split_suite_tasks`` → warmup slice). The adapter runs them inside
        ``produce_delta``, then calls evolve/update; their ``PhaseRun`` rows
        are not appended to the eval report.

        Returns:
            Ordered list of warmup tasks. Must be non-empty for the default
            ``WarmupThenUpdatePolicy`` used by ``HACEPipeline``.
        """


@dataclass(frozen=True)
class WarmupThenUpdatePolicy(ArtifactPolicy):
    """Default: run warmup tasks, then trigger artifact update (evolve)."""

    _warmup_tasks: list[SuiteTask]

    @property
    @override
    def warmup_tasks(self) -> list[SuiteTask]:
        return self._warmup_tasks
