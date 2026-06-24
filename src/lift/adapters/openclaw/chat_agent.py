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

# 单次 ``openclaw agent --message`` 在宿主机侧的 wall-clock 上限。
# 正常一轮 work / judge chat 在 30-90 秒内返回，触达模型 maxTokens 会走另一条
# truncation marker 通道；这里 600 秒纯粹用于兜底"既不出错也不返回"的情况——
# 历史 run 里出现过 8 小时不退的死锁（容器内 openclaw-agent 被卡住），加这个
# 上限后超时会被当作 provider error 走 5 次重试通道，最坏情况从永远 hang 收敛到
# ~50 分钟内自愈。
CHAT_EXEC_TIMEOUT_SECONDS = 600.0
CHAT_EXEC_TIMEOUT_MARKER = "chat exec timeout"


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
        """``openclaw agent --json --local`` → 解析 payloads 文本。

        这一次 chat 在宿主侧带 ``CHAT_EXEC_TIMEOUT_SECONDS`` 上限：超时返回带
        ``CHAT_EXEC_TIMEOUT_MARKER`` 前缀的字符串，让上层 ``_looks_like_provider_error``
        把它当作 provider error 走重试通道，避免单次 chat 永久 hang 把整个 run 拖死。
        """
        try:
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
                timeout_seconds=CHAT_EXEC_TIMEOUT_SECONDS,
            )
        except RuntimeError as exc:
            text = str(exc)
            if "timed out after" in text:
                LOGGER.warning(
                    "[openclaw chat] exec timeout, container=%s agent=%s session=%s",
                    self._container.container_name,
                    self.agent_name,
                    session_id,
                )
                # 首行包含 marker，``_looks_like_provider_error`` 取首行作为摘要
                return f"{CHAT_EXEC_TIMEOUT_MARKER}: {text}"
            raise
        return extract_agent_text(stdout)

    async def _register_agent_in_container(self, max_attempts: int = 5) -> None:
        """注册 agent；优化路径：直接 ``add``，失败再 ``list`` 兜底确认。

        历史实现是 ``list → add → list`` 三次 docker exec，每次都在容器里冷启一次
        node CLI（OpenClaw CLI 是 Node.js）。成功路径上的两次 ``list`` 是浪费——
        新建容器每次都是新 short_id 名字，前置 ``list`` 永远是 false；后置 ``list``
        只是在确认 ``add`` 自己刚说过的事。改成"先 ``add``，仅在异常分支才 ``list``
        兜底"后，成功路径从 3 次 node 冷启降到 1 次。
        """
        async def exists() -> bool:
            try:
                stdout = await exec_openclaw_async(
                    self._container, ["agents", "list"]
                )
            except RuntimeError as exc:
                raise ValueError("Failed to list agents in container") from exc
            return self._agent_name in (stdout or "")

        for attempt in range(max_attempts):
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
                return  # ``add`` 退 0 即视作成功；不再做后置 list 双确认
            except RuntimeError:
                # 异常分支：可能是 (a) 名字冲突，(b) docker exec 抖动但实际已 add 成功，
                # (c) CLI 真的失败。用一次 ``list`` 兜底区分 (a)/(b) vs (c)。
                pass
            if await exists():
                LOGGER.info(
                    "Container agent %s present after add error; treating as success",
                    self._agent_name,
                )
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
