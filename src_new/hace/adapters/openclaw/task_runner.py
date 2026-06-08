"""OpenClaw task execution inside Docker containers."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from src_new.agents import OpenClawAgent
from src_new.config import CONFIG, LOGGER
from src_new.eval_core import openclaw_run_task
from src_new.hace.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
    exec_openclaw_sync,
    exec_shell_async,
)
from src_new.hace.adapters.openclaw.container_env import container_runtime_env
from src_new.models import PhaseRun, SuiteTask
from src_new.utils import short_id

CONTAINER_WORKSPACE = "/workspace/task"


class ContainerOpenClawAgent(OpenClawAgent):
    """OpenClaw agent that runs CLI via docker exec."""

    def __init__(
        self,
        *,
        container: OpenClawContainerContext,
        run_id: str,
        task_id: str,
        agent_name: str,
        workspace_dir: Path,
        skills_dir: str | None = None,
        material_dir: str | None = None,
    ) -> None:
        super().__init__(
            run_id=run_id,
            task_id=task_id,
            agent_name=agent_name,
            skills_dir=skills_dir,
            material_dir=material_dir,
            workspace_dir=workspace_dir,
        )
        self._container = container

    def initialize(self) -> None:
        self.workspace_dir = Path(self._workspace_dir_arg).expanduser().resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._create_agent_in_container()

    def _create_agent_in_container(self, max_retries: int = 3) -> None:
        def exists() -> bool:
            result = exec_openclaw_sync(
                self._container,
                ["agents", "list"],
                check=False,
            )
            if result.returncode != 0:
                raise ValueError("Failed to list agents in container")
            return self.agent_name in (result.stdout or "")

        for attempt in range(max_retries):
            if exists():
                LOGGER.info("Container agent %s already exists", self.agent_name)
                return
            try:
                exec_openclaw_sync(
                    self._container,
                    [
                        "agents",
                        "add",
                        self.agent_name,
                        "--model",
                        CONFIG.model,
                        "--workspace",
                        CONTAINER_WORKSPACE,
                    ],
                    check=False,
                )
            except RuntimeError:
                pass
            if exists():
                return
            LOGGER.warning(
                "Retry container agent create %s (%d/%d)",
                self.agent_name,
                attempt + 1,
                max_retries,
            )
            self.agent_name = f"evobench-agent_name-{short_id()}"
        raise ValueError("Failed to create container agent")

    async def _run_cmd_checked_capture(self, args: list[str]) -> str:
        if args and args[0] == "openclaw":
            openclaw_args = args[1:]
        else:
            openclaw_args = args
        return (await exec_openclaw_async(self._container, openclaw_args)).strip()


def create_agents_for_task(
    *,
    task: SuiteTask,
    run_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    container: OpenClawContainerContext,
) -> tuple[ContainerOpenClawAgent, ContainerOpenClawAgent, str, str]:
    factory = _agent_factory(
        run_id=run_id,
        repeat_index=repeat_index,
        phase=phase,
        task=task,
        workspace_dir=workspace_dir,
        container=container,
    )
    user_session_id = f"user-{short_id()}"
    judge_session_id = f"judge-{short_id()}"
    user_agent = factory(f"user-{user_session_id}")
    judge_agent = factory(f"judge-{judge_session_id}")
    user_agent.initialize()
    judge_agent.initialize()
    return user_agent, judge_agent, user_session_id, judge_session_id


def _agent_factory(
    *,
    run_id: str,
    repeat_index: int,
    phase: str,
    task: SuiteTask,
    workspace_dir: Path,
    container: OpenClawContainerContext,
):
    def create_agent(session_role: str) -> ContainerOpenClawAgent:
        task_id = f"run-{repeat_index}-{phase}-{task.category_name}-{task.name}-{session_role}"
        return ContainerOpenClawAgent(
            container=container,
            run_id=run_id,
            task_id=task_id,
            agent_name=f"evobench-agent_name-{short_id()}",
            workspace_dir=workspace_dir,
            skills_dir=task.requirements.extra_skills_dir,
            material_dir=task.requirements.material_dir,
        )

    return create_agent


async def run_openclaw_task_phase(
    *,
    task: SuiteTask,
    run_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    container: OpenClawContainerContext,
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
    log_label: str = "task",
    agents: tuple[ContainerOpenClawAgent, ContainerOpenClawAgent, str, str] | None = None,
) -> PhaseRun:
    if agents is None:
        agents = create_agents_for_task(
            task=task,
            run_id=run_id,
            repeat_index=repeat_index,
            phase=phase,
            workspace_dir=workspace_dir,
            container=container,
        )
    user_agent, judge_agent, user_session_id, judge_session_id = agents
    LOGGER.info(
        "Running %s %s: %s run_id=%s repeat=%d workspace=%s container=%s",
        phase,
        log_label,
        task.name,
        run_id,
        repeat_index,
        workspace_dir,
        container.container_name,
    )
    success, work_sid, judge_sid, content_score = await openclaw_run_task(
        task,
        run_id,
        user_agent=user_agent,
        judge_agent=judge_agent,
        user_session_id=user_session_id,
        judge_session_id=judge_session_id,
        is_evolve_turn=is_evolve_turn,
        is_final_task=is_final_task,
    )
    return PhaseRun(
        work_session_id=work_sid,
        judge_session_id=judge_sid,
        success=success,
        content_score=content_score,
        workspace_dir=str(workspace_dir.resolve()),
    )


async def run_openclaw_task_phase_batch(
    *,
    tasks: list[SuiteTask],
    run_id: str,
    repeat_index: int,
    phase: str,
    workspace_dir: Path,
    container: OpenClawContainerContext,
    parallel: bool,
    is_evolve_turn: bool = False,
    is_final_task: bool = False,
    log_label: str = "task",
) -> list[PhaseRun]:
    if not tasks:
        return []

    async def run_one(task: SuiteTask) -> PhaseRun:
        return await run_openclaw_task_phase(
            task=task,
            run_id=run_id,
            repeat_index=repeat_index,
            phase=phase,
            workspace_dir=workspace_dir,
            container=container,
            is_evolve_turn=is_evolve_turn,
            is_final_task=is_final_task,
            log_label=log_label,
        )

    if parallel:
        return list(await asyncio.gather(*[run_one(t) for t in tasks]))
    results: list[PhaseRun] = []
    for task in tasks:
        results.append(await run_one(task))
    return results


async def evolve_in_container(container: OpenClawContainerContext, session_id: str) -> None:
    _ = session_id
    env = container_runtime_env()
    await exec_shell_async(
        container.container_name,
        """
mkdir -p /workspace/task
git config --global --add safe.directory /workspace/task
WORKER_JS="${HOME}/.openclaw/extensions/self-evolving-plugin-pro/src/review/worker.js"
if [[ -f "${WORKER_JS}" ]]; then
  sed -i 's/"--thinking", "low"/"--thinking", "off"/g' "${WORKER_JS}" || true
fi
""".strip(),
        extra_env=env,
    )
    await exec_openclaw_async(container, ["learn", "review"])
