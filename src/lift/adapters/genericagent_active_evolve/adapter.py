"""GenericAgent + 主动进化 adapter（``-r genericagent_active_evolve``）。

继承基础 ``GenericAgentAdapter``，叠两层主动复盘钩子：

1. ``evolve_after_task``：每题完成后在同一容器里起一个**独立 GA 进程**
   （新 ``iodir`` + 新 ``session_id``），发送 per-task 复盘 prompt，让 GA
   按 ``memory/memory_management_sop.md`` 决定写哪一层；
2. ``evolve_after_warmup``：所有 warmup 题完成后再起一个 GA 进程发送 suite
   级总复盘 prompt（同样独立 ``iodir`` + ``session_id``），与既有
   work / judge 进程完全隔离。

两次复盘都跑在 warmup 容器仍存活时，复盘后 GA 写出的
``/opt/GenericAgent/memory/`` 由 ``ContainerAgentRuntimeAdapter.materialize_delta``
通过 ``docker commit`` 自然带入 evolved 镜像。
"""

from __future__ import annotations

from typing import override

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.genericagent.adapter import GenericAgentAdapter
from src.lift.adapters.genericagent.chat_agent import GenericAgentContainerAgent
from src.lift.adapters.genericagent.session import genericagent_context
from src.lift.adapters.genericagent_active_evolve.reflection_prompts import (
    SUITE_REFLECTION_PROMPT,
    per_task_reflection_prompt,
)
from src.models import PhaseRun, SuiteTask
from src.utils import short_id


# 复盘 chat 没有判官，单轮即结束（GA 回 DONE 或写完 memory 自然终止）。
# 多给一轮余量，应付 GA 把"我先看一眼 memory"和"我现在写"拆成两次输出。
_REFLECTION_MAX_TURNS = 2


async def _run_reflection(
    *,
    env: ExecutionEnvironment,
    role: str,
    prompt: str,
) -> None:
    """在 warmup 容器内起一个独立 GA 进程跑一次复盘 chat。

    与 work / judge 不同，复盘 agent：
    - 用独立 ``iodir``（``role`` 区分），避免污染既有 work / judge 输入文件；
    - 用独立 ``session_id``（``reflect-*``），Langfuse 上能与对话 trace 区分；
    - 单轮交互即结束，GA 单方面回 DONE 或自然写完 memory 后我们也不再续 reply。

    任何异常都会被 ``_run_evolve_after_*_with_retry`` 兜住做 3 次重试；这里
    透明抛出即可。
    """
    session: ContainerSession = env.handle
    container = genericagent_context(session)
    workspace_dir = env.workspace_dir
    session_id = f"reflect-{role}-{short_id()}"
    agent_name = f"lift-genericagent-reflect-{role}-{short_id()}"
    agent = GenericAgentContainerAgent(
        container=container,
        agent_name=agent_name,
        workspace_dir=workspace_dir,
        role=f"reflect-{role}",
    )
    await agent.initialize()
    LOGGER.info(
        "[active_evolve] reflection chat start role=%s session=%s container=%s",
        role, session_id, container.container_name,
    )
    reply = await agent.chat(prompt, session_id=session_id)
    LOGGER.info(
        "[active_evolve] reflection chat done role=%s session=%s reply_head=%r",
        role, session_id, reply[:120].replace("\n", "\\n"),
    )
    _ = _REFLECTION_MAX_TURNS  # 当前实现单轮即返；保留常量以便后续扩展


class GenericAgentActiveEvolveAdapter(GenericAgentAdapter):
    """GenericAgent + 主动复盘：每题 + suite 收尾两层 reflection chat。"""

    @override
    async def evolve_after_task(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
    ) -> None:
        """每题完成后起独立 GA 进程发 per-task 复盘 prompt。"""
        _ = ctx
        prompt = per_task_reflection_prompt(task, result)
        await _run_reflection(env=env, role=f"task-{task.name}", prompt=prompt)

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """所有 warmup 完成后起独立 GA 进程发 suite 级总复盘 prompt。"""
        _ = ctx
        await _run_reflection(env=env, role="suite", prompt=SUITE_REFLECTION_PROMPT)
