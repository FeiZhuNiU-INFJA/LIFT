"""OpenClaw 容器内 chat transport 与 WorkerJudgerPair 工厂。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.config import CONFIG, LOGGER
from src.lift.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
)
from src.lift.adapters.openclaw.json_output import extract_agent_text
from src.lift.eval.chat_agent import ChatAgent
from src.lift.eval.worker_judger import WorkerJudgerPair
from src.models import SuiteTask
from src.utils import short_id

CONTAINER_WORKSPACE = "/workspace/task"


class OpenClawContainerAgent(ChatAgent):
    """容器内 OpenClaw chat：``docker exec openclaw agent --local``。"""

    def __init__(
        self,
        *,
        container: OpenClawContainerContext,
        agent_name: str,
        workspace_dir: Path,
    ) -> None:
        self._container = container
        self._agent_name = agent_name
        self._workspace_dir = workspace_dir

    @property
    def agent_name(self) -> str:
        return self._agent_name

    async def initialize(self) -> None:
        """确保宿主机 workspace 存在并在容器内 ``openclaw agents add``。

        ``agents list/add`` 走 ``exec_openclaw_async``，不阻塞事件循环；
        PARALLEL_SINGLE 下多题工厂可真正并发。
        """
        self._workspace_dir.mkdir(parents=True, exist_ok=True)  # 本地 FS，瞬时完成
        await self._register_agent_in_container()

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
                session_id,  # 与 emit_pre_chat_state / langfuse-tracer 的 sessionId 对齐
                "--json",
                "--local",
            ],
        )
        return extract_agent_text(stdout)

    async def _register_agent_in_container(self, max_attempts: int = 5) -> None:
        async def exists() -> bool:
            try:
                stdout = await exec_openclaw_async(
                    self._container, ["agents", "list"]
                )
            except RuntimeError as exc:
                raise ValueError("Failed to list agents in container") from exc
            return self._agent_name in (stdout or "")

        for attempt in range(max_attempts):
            if await exists():
                LOGGER.info("Container agent %s already exists", self._agent_name)
                return
            try:
                await exec_openclaw_async(
                    self._container,
                    [
                        "agents",
                        "add",
                        self._agent_name,
                        "--model",
                        CONFIG.model,
                        "--workspace",
                        CONTAINER_WORKSPACE,
                    ],
                )
            except RuntimeError:
                pass  # 名字冲突等错误下面靠 exists() 二次确认 / 改名重试
            if await exists():
                return
            LOGGER.warning(
                "Retry container agent create %s (%d/%d)",
                self._agent_name,
                attempt + 1,
                max_attempts,
            )
            self._agent_name = f"lift-agent_name-{short_id()}"  # 名冲突时换名重试
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

    async def __call__(self, task: SuiteTask) -> WorkerJudgerPair:
        _ = task  # task 留给子类做 per-task 定制；当前 OpenClaw 路径不依赖
        work_session_id = f"user-{short_id()}"
        judge_session_id = f"judge-{short_id()}"

        def create_agent(session_role: str) -> OpenClawContainerAgent:
            _ = session_role  # work/judge 各独立 agent 实例，session 由下方 id 区分
            return OpenClawContainerAgent(
                container=self._container,
                agent_name=f"lift-agent_name-{short_id()}",
                workspace_dir=self._workspace_dir,
            )

        work_agent = create_agent("work")
        judge_agent = create_agent("judge")
        # 两个 agent 的 initialize 是独立的 docker exec 链路，可以并发跑
        await asyncio.gather(work_agent.initialize(), judge_agent.initialize())
        return WorkerJudgerPair(
            work_agent=work_agent,
            judge_agent=judge_agent,
            work_session_id=work_session_id,
            judge_session_id=judge_session_id,
        )
