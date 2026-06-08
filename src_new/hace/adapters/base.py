from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from src_new.models import PhaseRun, SuiteTask

from src_new.hace.policies.artifact import ArtifactPolicy
from src_new.hace.runtime.delta_ref import DeltaRef
from src_new.hace.runtime.repeat_scope import RepeatScope


class LoadState(Enum):
    BEFORE_LOAD = "before_load"
    AFTER_LOAD = "after_load"


@dataclass
class RunContext:
    run_id: str
    repeat_index: int
    suite_path: Path
    category_name: str
    suite_name: str


class RuntimeAdapter(ABC):
    @abstractmethod
    async def open_repeat_scope(self, ctx: RunContext) -> RepeatScope: ...

    @abstractmethod
    async def produce_delta(
        self,
        scope: RepeatScope,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef: ...

    @abstractmethod
    async def run_before_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
        ctx: RunContext,
        *,
        phase: str = "baseline",
    ) -> PhaseRun: ...

    @abstractmethod
    async def run_after_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
        delta: DeltaRef,
        ctx: RunContext,
    ) -> PhaseRun: ...
