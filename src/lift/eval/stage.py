"""LIFT suite stage: warmup vs hold-out, and hold-out load state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SuiteStage(StrEnum):
    """Suite run 的大阶段：warmup（产物进化）或 holdout（终测对照）。"""

    WARMUP = "warmup"  # 非 hold-out 题 + evolve，产出 delta
    HOLDOUT = "holdout"  # before-load / after-load 对照评测


class HoldoutLoadState(StrEnum):
    """Hold-out artifact load state (before-load vs after-load)."""

    BASELINE = "baseline"  # before-load: fresh runtime, no delta
    EVOLVED = "evolved"  # after-load: runtime with warmup delta


@dataclass(frozen=True)
class SuiteRunPhase:
    """Which phase of a suite run is active (warmup vs hold-out load state).

    Not a benchmark ``SuiteTask`` — describes where we are in the LIFT flow for
    the current ``SuiteRunContext``.
    """

    stage: SuiteStage  # warmup 或 holdout
    load_state: HoldoutLoadState | None = None  # holdout 时必填：baseline / evolved

    def __post_init__(self) -> None:
        """校验 stage 与 load_state 的组合合法。"""
        if self.stage == SuiteStage.HOLDOUT and self.load_state is None:
            raise ValueError("holdout stage requires load_state")
        if self.stage == SuiteStage.WARMUP and self.load_state is not None:
            raise ValueError("warmup stage must not set load_state")

    @classmethod
    def warmup(cls) -> SuiteRunPhase:
        """构造 warmup 阶段（无 load_state）。"""
        return cls(stage=SuiteStage.WARMUP)

    @classmethod
    def holdout(cls, load_state: HoldoutLoadState) -> SuiteRunPhase:
        """构造 hold-out 阶段，指定 before-load 或 after-load。"""
        return cls(stage=SuiteStage.HOLDOUT, load_state=load_state)

    @property
    def workspace_segment(self) -> str:
        """Path segment under ``outcome/run-{i}/`` (``warmup`` or load_state value)."""
        if self.stage == SuiteStage.WARMUP:
            return SuiteStage.WARMUP.value
        assert self.load_state is not None
        return self.load_state.value

    @property
    def log_label(self) -> str:
        """Human-readable label for logs (LIFT before/after-load semantics)."""
        if self.stage == SuiteStage.WARMUP:
            return "warmup"
        assert self.load_state is not None
        return (
            "before-load"
            if self.load_state == HoldoutLoadState.BASELINE
            else "after-load"
        )

    @property
    def is_final_task(self) -> bool:
        """hold-out 题标记为 final task（写入 Langfuse tags）。"""
        return self.stage == SuiteStage.HOLDOUT

    @property
    def is_evolve_turn(self) -> bool:
        """after-load 阶段标记为 evolve turn（加载了 warmup delta）。"""
        return (
            self.stage == SuiteStage.HOLDOUT
            and self.load_state == HoldoutLoadState.EVOLVED
        )
