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
    """Runtime-specific implementation of the HACE evaluation contract.

    Each agent backend (OpenClaw, Hermes, …) subclasses this ABC and wires
    warmup → delta production → hold-out baseline/evolved execution.
    ``HACEPipeline`` calls these four methods in a fixed order per suite/repeat.
    """

    @abstractmethod
    async def open_repeat_scope(self, ctx: RunContext) -> RepeatScope:
        """Open a resource scope for one ``--repeat`` iteration of one suite.

        Called once per (repeat_index, suite) before warmup or hold-out work.
        The returned ``RepeatScope`` should be used to ``track()`` any containers
        or sessions created during this repeat so ``scope.cleanup()`` can release
        them when the suite finishes (including the delta image).

        Args:
            ctx: Immutable run coordinates (run_id, repeat_index, suite metadata).

        Returns:
            A fresh ``RepeatScope`` bound to ``ctx.run_id`` / ``ctx.repeat_index``
            / ``ctx.suite_name``. Pipeline passes the same scope through
            ``produce_delta``, ``run_before_load``, and ``run_after_load``.
        """

    @abstractmethod
    async def produce_delta(
        self,
        scope: RepeatScope,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        """Run warmup tasks, trigger artifact update, and materialize delta.

        Implements the **ArtifactPolicy** phase of HACE: execute all warmup
        (non-hold-out) tasks, then call the runtime's evolve/update hook
        (e.g. OpenClaw ``openclaw learn review``), and persist the resulting
        state as a loadable artifact.

        For OpenClaw this is typically ``docker commit`` of the warmup container
        into a temporary image tagged via ``delta_image_tag()``. The delta is
        stored on ``scope.delta`` and later consumed by ``run_after_load``.

        Warmup ``PhaseRun`` results are **not** written to the eval report;
        only the delta reference is returned to the pipeline.

        Args:
            scope: Repeat scope; implementors should ``track()`` warmup containers
                here and assign ``scope.delta`` before returning.
            policy: How artifacts are produced (default: ``WarmupThenUpdatePolicy``).
            warmup_tasks: Tasks from ``split_suite_tasks`` excluding hold-out.
            ctx: Same run coordinates as ``open_repeat_scope``.

        Returns:
            ``DeltaRef`` pointing at the committed artifact (e.g. delta image tag).

        Raises:
            ValueError: If ``warmup_tasks`` is empty or policy is unsupported.
        """

    @abstractmethod
    async def run_before_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
        ctx: RunContext,
        *,
        phase: str = "baseline",
    ) -> PhaseRun:
        """Evaluate one hold-out task **without** loading evolved artifacts.

        Corresponds to HACE **before-load** / ``LoadState.BEFORE_LOAD``: a fresh
        runtime environment (base image for OpenClaw) with an isolated per-task
        workspace. The agent must not see warmup evolve output.

        Runs the full task loop (user agent + judge, multi-turn until success
        or max rounds) and returns a ``PhaseRun`` for ``TaskRun.baseline``.

        Args:
            task: A single hold-out ``SuiteTask``.
            scope: Repeat scope for tracking ephemeral containers.
            ctx: Run coordinates.
            phase: Report/workspace label (pipeline passes ``"baseline"``).

        Returns:
            ``PhaseRun`` with success, content_score, session ids, workspace_dir.
        """

    @abstractmethod
    async def run_after_load(
        self,
        task: SuiteTask,
        scope: RepeatScope,
        delta: DeltaRef,
        ctx: RunContext,
    ) -> PhaseRun:
        """Evaluate the same hold-out task **with** evolved artifacts loaded.

        Corresponds to HACE **after-load** / ``LoadState.AFTER_LOAD``: same task
        and judge protocol as ``run_before_load``, but the runtime starts from
        the delta produced by ``produce_delta`` (e.g. OpenClaw delta image).

        Workspace must remain per-task isolated (new directory per phase) so
        answers do not leak from baseline; only the **artifact load state**
        differs between the two runs.

        Args:
            task: Same hold-out task as the paired baseline run.
            scope: Repeat scope for tracking ephemeral containers.
            delta: Artifact reference from ``produce_delta``.
            ctx: Run coordinates.

        Returns:
            ``PhaseRun`` for ``TaskRun.evolved``.
        """
