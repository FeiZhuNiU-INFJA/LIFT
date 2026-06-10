"""OpenClaw 容器内 chat transport 与 WorkerJudgerPair 工厂。"""

from __future__ import annotations

from pathlib import Path

from src.config import CONFIG, LOGGER
from src.lift.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
    exec_openclaw_sync,
)
from src.lift.adapters.openclaw.json_output import extract_agent_text
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

CONTAINER_WORKSPACE = "/workspace/task"


class OpenClawContainerAgent:
    """容器内 OpenClaw chat：``docker exec openclaw agent --local``。"""

    def __init__(
        self,
        *,
        container: OpenClawContainerContext,
        agent_name: str,
        workspace_dir: Path,
    ) -> None:
        self._container = container
        self.agent_name = agent_name
        self._workspace_dir = workspace_dir

    def initialize(self) -> None:
        """确保宿主机 workspace 存在并在容器内 ``openclaw agents add``。"""
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        self._register_agent_in_container()

    async def activate_session(self, session_id: str) -> None:
        """OpenClaw 以 ``--session-id`` 区分对话，无需额外 activate。"""
        _ = session_id

    def augment_work_prompt(self, task: SuiteTask, prompt: str) -> str:
        _ = task
        return prompt

    def augment_judge_user_prompt(self, task: SuiteTask, prompt: str) -> str:
        _ = task
        return prompt

    async def chat(self, message: str, *, session_id: str) -> str:
        """``openclaw agent --json --local`` → 解析 payloads 文本。"""
        stdout = await exec_openclaw_async(
            self._container,
            [
                "agent",
                "--agent",
                self.agent_name,
                "--message",
                message,
                "--session-id",
                session_id,
                "--json",
                "--local",
            ],
        )
        return extract_agent_text(stdout)

    def _register_agent_in_container(self, max_retries: int = 3) -> None:
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


class OpenClawWorkerJudgerPairFactory:
    """为同一容器内的题目构建 ``WorkerJudgerPair``。"""

    def __init__(
        self,
        *,
        container: OpenClawContainerContext,
        workspace_dir: Path,
    ) -> None:
        self._container = container
        self._workspace_dir = workspace_dir

    def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"

        def create_agent(session_role: str) -> OpenClawContainerAgent:
            _ = session_role
            return OpenClawContainerAgent(
                container=self._container,
                agent_name=f"evobench-agent_name-{short_id()}",
                workspace_dir=self._workspace_dir,
            )

        work_agent = create_agent("work")
        judge_agent = create_agent("judge")
        work_agent.initialize()
        judge_agent.initialize()
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
