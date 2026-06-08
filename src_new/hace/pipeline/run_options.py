from __future__ import annotations

from dataclasses import dataclass

from src_new.hace.policies.container import WarmupContainerPolicy


@dataclass
class RunOptions:
    repeat: int = 1
    test: bool = False
    evaluate: bool = False
    evaluate_only: bool = False
    parallel: bool = False
    docker_image: str | None = None
    incremental_report: bool = True
    warmup_container_policy: WarmupContainerPolicy = WarmupContainerPolicy.SERIAL_SINGLE
    delta_materialization: str = "commit_image"
    parallel_repeats: bool = True
    max_parallel_repeats: int | None = None

    def __post_init__(self) -> None:
        if self.repeat < 1:
            raise ValueError("--repeat must be at least 1")
        if self.max_parallel_repeats is None:
            self.max_parallel_repeats = self.repeat
