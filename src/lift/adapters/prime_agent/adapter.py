"""Prime Agent runtime adapter（baseline / 被动进化 · active 变体的科学对照组）。

对应 LIFT CLI ``-r prime_agent``：

- 基础镜像 ``PRIME_AGENT_DOCKER_IMAGE``（npm 包 ``prime-agent`` +
  LIFT overlay；构建见 ``agent-runtimes/prime_agent/``）。
- ``evolve_after_warmup`` 为 no-op：baseline 不显式触发 ``/refine``；warmup 期间
  Prime Agent 自然写入的 global harness / skills（在 ``PRIME_AGENT_STATE_DIR``）会被
  ``ContainerAgentRuntimeAdapter.materialize_delta`` 通过 ``docker commit`` 带入
  evolved 镜像。

  ⚠️ 注意 Prime Agent 的 Continual Harness 默认「local to session」——普通任务
  执行不会自动 refine 到 global。若发现 baseline 的 evolve-only diff 计数长期
  为 0（几乎无进化产物），说明需要走 ``prime_agent_active_evolve`` 显式触发
  global ``/refine``，或在 warmup 期启用 ``/autonomous`` 让 agent 自主 refine。

  ⚠️ **裸 baseline 的定位（重要，勿误读 0）**：由于上面这条默认 session-local
  语义，本 runtime 的 evolved 相**预期增益≈0**——warmup 自然写入的多是 local
  harness，被 holdout 全新 session 丢弃，能被 ``docker commit`` 带入 delta 且被
  新 session 读到的进化产物极少。因此**不要**把 ``prime_agent`` 当作“测 Prime
  Agent 开箱自进化能力”的主指标；它的真正意义是 ``prime_agent_active_evolve``
  的**科学对照组（负向对照 / ablation）**：两相共用同一镜像与 chat 路径，唯一
  差异是 active 变体在 ``evolve_after_task`` 逐题触发 global ``/refine``。有了这个
  ≈0 的被动基线，active 变体的增益才能被归因到“显式 refine 机制”而非噪声。故
  baseline 应保留、且其接近 0 的 ΔTurns/Δ 成功率是**符合预期的对照数据点**，
  不是实现缺陷。评“Prime Agent 是否真的能自进化”，看 active 变体相对本 baseline
  的差值。

- ``evolve_paths`` 白名单声明为 ``PRIME_AGENT_STATE_DIR``，供 delta preflight
  输出 evolve-only 摘要，负向判定 warmup 是否真的产出了进化产物。

warmup 并发：Continual Harness 是共享可变状态，``parallel_single`` 下多题并发
CRUD 同一 harness 会竞态（同 Hermes 写 ``/opt/hermes-state`` 的问题），因此
构造时对 ``parallel_single`` 给出告警，推荐 ``--warmup-container-policy
serial_single``。
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src.lift.adapters.container.session import ContainerSession
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.prime_agent.chat_agent import (
    PrimeAgentWorkerJudgerPairFactory,
)
from src.lift.adapters.prime_agent.session import (
    prime_agent_context,
    start_prime_agent_container,
)
from src.lift.eval.stage import HoldoutLoadState, SuiteRunPhase
from src.lift.eval.worker_judger import WorkerJudgerPairFactory
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.container import WarmupContainerPolicy
from src.models import SuiteTask
from src.paths import PRIME_AGENT_DOCKER_IMAGE, PRIME_AGENT_STATE_DIR


class PrimeAgentAdapter(ContainerAgentRuntimeAdapter):
    """Prime Agent baseline runtime（被动进化；无显式 ``/refine`` 调用）。"""

    #: Prime Agent 把 Continual Harness / skills / sessions 全写在容器内
    #: ``PRIME_AGENT_STATE_DIR``（由 env PRIME_AGENT_CODING_AGENT_DIR 钉死）。整个
    #: 目录声明为 evolve-only 白名单，让 delta preflight 能负向判定 warmup 是否产出。
    evolve_paths: tuple[str, ...] = (PRIME_AGENT_STATE_DIR,)

    def __init__(self, options: RunOptions) -> None:
        """初始化并对 warmup 并发策略做竞态提示。"""
        super().__init__(options)
        if self._options.warmup_container_policy is WarmupContainerPolicy.PARALLEL_SINGLE:
            LOGGER.warning(
                "Prime Agent warmup uses parallel_single: the Continual Harness is "
                "shared mutable state and concurrent CRUD may race. Recommend "
                "'--warmup-container-policy serial_single' for prime_agent."
            )

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """``RunOptions.docker_image`` 优先，否则默认 Prime Agent 镜像。"""
        return override or PRIME_AGENT_DOCKER_IMAGE

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
        """委托 ``start_prime_agent_container``；baseline 不区分 baseline/evolved。"""
        _ = load_state
        return await start_prime_agent_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
            container_memory=self._options.container_memory,
            container_cpus=self._options.container_cpus,
            viz_role=viz_role,
        )

    @override
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
        *,
        run_phase: SuiteRunPhase,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """绑定 Prime Agent 容器 → ``PrimeAgentWorkerJudgerPairFactory``。"""
        session: ContainerSession = env.handle
        _ = run_phase
        judge_session = env.judge_handle or env.handle
        return PrimeAgentWorkerJudgerPairFactory(
            container=prime_agent_context(session),
            judge_container=prime_agent_context(judge_session),
            workspace_dir=workspace_dir,
            run_tag=ctx.run_id,
        )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """baseline：无显式 evolve；warmup 期 harness 由 docker commit 携带。"""
        _ = (env, ctx)
        return None
