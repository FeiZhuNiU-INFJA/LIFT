"""OpenClaw chat implementation and agent pair factory."""

from __future__ import annotations

from pathlib import Path

from src_new.agents import OpenClawAgent
from src_new.config import CONFIG, LOGGER
from src_new.lift.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
    exec_openclaw_sync,
)
from src_new.lift.eval.worker_judger import WorkerJudgerPair
from src_new.models import SuiteTask
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


class OpenClawWorkerJudgerPairFactory:
    """Build ``WorkerJudgerPair`` with ``ContainerOpenClawAgent`` for tasks in one container."""

    def __init__(
        self,
        *,
        container: OpenClawContainerContext,
        run_id: str,
        repeat_index: int,
        phase: str,
        workspace_dir: Path,
    ) -> None:
        self._container = container
        self._run_id = run_id
        self._repeat_index = repeat_index
        self._phase = phase
        self._workspace_dir = workspace_dir

    def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"

        def create_agent(session_role: str) -> ContainerOpenClawAgent:
            task_id = (
                f"run-{self._repeat_index}-{self._phase}-"
                f"{task.category_name}-{task.name}-{session_role}"
            )
            return ContainerOpenClawAgent(
                container=self._container,
                run_id=self._run_id,
                task_id=task_id,
                agent_name=f"evobench-agent_name-{short_id()}",
                workspace_dir=self._workspace_dir,
                skills_dir=task.requirements.extra_skills_dir,
                material_dir=task.requirements.material_dir,
            )

        work_agent = create_agent(f"user-{work_session_id}")
        judge_agent = create_agent(f"judge-{judge_session_id}")
        work_agent.initialize()
        judge_agent.initialize()
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )

