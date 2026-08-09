"""OpenClawWithAgentMemoryActiveEvolveAdapter：agentmemory 主动进化变体。

被动的 ``OpenClawWithAgentMemoryAdapter`` 只在 warmup 期把 agentmemory 写下的
**原始 observation** 随 ``docker commit`` 带进 delta，agentmemory 内置的
consolidate/reflect 蒸馏引擎因为 :3111 server 启动时**没有 LLM provider**（落到零-LLM
``NoopProvider``）而全程 no-op——delta 里全是未经综合的碎片记忆。

本变体在被动版之上加两步"主动进化"，其余（镜像 / force_bridge / prelaunch /
delta 路径）完全复用父类：

1. **点火（ignition）**：warmup 容器启动时（``load_state is None``）向 ``docker run``
   注入 ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` / ``OPENAI_MODEL``（复用被测 agent
   同源的 ``WORK_OPENAI_*`` + ``MODEL_NAME``），让 prelaunch 起的 :3111 server 在 boot
   时构造出真正的 OpenAI provider。holdout 容器**不点火**——baseline/evolved 只读记忆、
   跑被测任务，不应触发后台蒸馏消耗 token、也不该污染交互效率指标。

2. **蒸馏（distill）**：``evolve_after_warmup`` 在 ``docker commit`` 之前于容器内 curl
   :3111，按 agentmemory session-end hook 规范触发 ``crystals/auto`` +
   ``consolidate-pipeline{tier:all}``（内部 semantic-merge→reflect→procedural），把
   碎片记忆蒸馏成 semantic facts / higher-order insights 后再进 delta。

**token 口径提示**：蒸馏走的 LLM 调用打到 ``WORK_OPENAI_BASE_URL``，与被测 agent 同
key/端点，因此会计入该端点的 token 消耗。评估"进化收益"时 evolved 阶段本身不含蒸馏
（蒸馏发生在 warmup→delta 之间），但整批 run 的总 token 会比被动版高——这是主动进化的
预期成本，需在跨 runtime 对比时区分 warmup 蒸馏开销与 holdout 推理开销。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.openclaw.session import openclaw_context, start_openclaw_container
from src.lift.adapters.openclaw_with_agentmemory.adapter import (
    OpenClawWithAgentMemoryAdapter,
)
from src.lift.adapters.openclaw_with_agentmemory_active_evolve.evolve import (
    agentmemory_distill,
    build_ignition_env,
)
from src.lift.eval.stage import HoldoutLoadState
from src.models import SuiteTask


class OpenClawWithAgentMemoryActiveEvolveAdapter(OpenClawWithAgentMemoryAdapter):
    """OpenClaw + agentmemory + 主动进化：warmup 点火 provider + 显式蒸馏。"""

    @override
    async def start_container(
        self,
        *,
        instance_id: str,
        image: str,
        ctx: SuiteRunContext,
        workspace_dir: Path,
        seed_workspace: bool,
        task: SuiteTask | None,
        load_state: HoldoutLoadState | None = None,
        viz_role: str | None = None,
    ) -> ContainerSession:
        """委托 ``start_openclaw_container``；仅 warmup 容器注入点火 env。

        ``load_state is None`` 即 warmup 路径（holdout 恒为 ``BASELINE`` /
        ``EVOLVED``）。只有 warmup 容器需要 agentmemory 的 LLM provider——它负责在
        ``evolve_after_warmup`` 触发蒸馏。holdout 不注入，避免 baseline/evolved 阶段
        agentmemory 的后台定时器/session-end 蒸馏动用 LLM 或干扰交互效率测量。
        """
        ignition_env: dict[str, str] = {}
        if load_state is None:
            ignition_env = build_ignition_env()
            if ignition_env:
                LOGGER.info(
                    "agentmemory active-evolve: igniting LLM provider on warmup "
                    "container %s (model=%s)",
                    instance_id,
                    ignition_env.get("OPENAI_MODEL"),
                )
            else:
                LOGGER.warning(
                    "agentmemory active-evolve: WORK_OPENAI_* / MODEL_NAME incomplete; "
                    "warmup container %s will run zero-LLM (distill will be a no-op). "
                    "Falling back to passive behavior.",
                    instance_id,
                )
        return await start_openclaw_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
            force_bridge_network=self.force_bridge_network,
            agentmemory_prelaunch=True,
            extra_env=ignition_env or None,
            viz_role=viz_role,
        )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """warmup 全部完成后、``docker commit`` 之前，在容器内触发 agentmemory 蒸馏。

        仅在 warmup 容器成功点火（provider 可用）时触发；未点火则 no-op（curl force
        也会因 NoopProvider 让各 tier 记 error，触发无意义，直接跳过更干净）。
        """
        _ = ctx
        if not build_ignition_env():
            LOGGER.warning(
                "agentmemory active-evolve: skip distill (provider not ignited)."
            )
            return
        session: ContainerSession = env.handle
        await agentmemory_distill(openclaw_context(session))
