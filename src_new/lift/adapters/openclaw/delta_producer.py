from __future__ import annotations

from pathlib import Path

from src_new.config import LOGGER
from src_new.lift.adapters.openclaw.container_session import ContainerSession
from src_new.lift.adapters.openclaw.task_runner import (
    evolve_in_container,
    run_openclaw_task_phase_batch,
)
from src_new.lift.policies.container import WarmupContainerPolicy
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.environment_cleaner import EnvironmentCleaner, delta_image_tag
from src_new.lift.runtime.suite_run_resources import SuiteRunResources
from src_new.models import SuiteTask
from src_new.utils import outcome_workspace


async def produce_delta_from_warmup(
    *,
    resources: SuiteRunResources,
    warmup_tasks: list[SuiteTask],
    run_id: str,
    repeat_index: int,
    category_name: str,
    suite_name: str,
    docker_image: str,
    warmup_policy: WarmupContainerPolicy,
    parallel_warmup_tasks: bool,
) -> DeltaRef:
    if not warmup_tasks:
        raise ValueError("produce_delta requires at least one warmup task")

    instance_id = f"{run_id}-r{repeat_index}-{suite_name}-warmup"
    workspace = outcome_workspace(run_id, repeat_index, "warmup", category_name)

    if warmup_policy == WarmupContainerPolicy.SERIAL_SINGLE:
        session = await ContainerSession.start(
            instance_id=instance_id,
            image=docker_image,
            run_id=run_id,
            repeat_index=repeat_index,
            workspace_dir=workspace,
        )
        resources.track(session)
        await run_openclaw_task_phase_batch(
            tasks=warmup_tasks,
            run_id=run_id,
            repeat_index=repeat_index,
            phase="warmup",
            workspace_dir=workspace,
            container=session.context,
            parallel=False,
            is_final_task=False,
            log_label="warmup",
        )
        await evolve_in_container(session.context, f"lift-evolve-{repeat_index}-{category_name}")
        image_tag = delta_image_tag(
            run_id=run_id,
            repeat_index=repeat_index,
            suite_name=suite_name,
        )
        delta = DeltaRef(
            image_tag=image_tag,
            source_container=session.container_name,
        )
        cleaner = EnvironmentCleaner()
        await cleaner.commit_container(session.container_name, image_tag)
        await session.cleanup()
        resources.delta = delta
        LOGGER.info("Delta committed: %s", image_tag)
        return delta

    if warmup_policy == WarmupContainerPolicy.PARALLEL_MULTI:
        if parallel_warmup_tasks:
            raise NotImplementedError(
                "parallel_multi warmup with per-task containers is not implemented yet"
            )
        raise NotImplementedError("parallel_multi warmup policy is not implemented yet")

    raise ValueError(f"Unknown warmup container policy: {warmup_policy}")
