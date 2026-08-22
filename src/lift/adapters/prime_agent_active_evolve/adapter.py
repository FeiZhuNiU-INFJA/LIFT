"""Prime Agent runtime with explicit **per-task** global ``/refine`` evolution.

保持 baseline 镜像与 chat 路径不变，仅覆盖 **逐题** evolve 钩子
（``evolve_after_task``）：每道 warmup 题完成后立刻在仍存活的 warmup 容器里对
**刚结束那题的会话**触发一次 **global** ``/refine``，把证据支撑的增量写回 global
harness；全部 warmup 题跑完后由父类正常 ``docker commit`` 捕获
``PRIME_AGENT_STATE_DIR``。

为什么逐题而非 suite 级一次性触发：warmup 强制 ``serial_single``，N 道题 = N 个
**独立 conversation session**（每题 chat 新建自己的 session）。``/refine`` 只复盘
**当前/最近一次会话**的轨迹（upstream ``rlm-runtime.md``），所以若只在
``evolve_after_warmup`` 触发一次，``-c`` 续接的“最近会话”仅是最后一道 warmup 题
—— 前 N-1 题的教训只落在各自 session-local harness，被 holdout 新 session 丢弃，
进化增益大幅打折。改为逐题触发：每题一结束就 refine，此刻“最近会话”恰是刚结束的
那题，``-c`` 天然对齐、零 session-id 回传，每题证据都被提升到 global 累积层。

并发安全：``evolve_after_task`` 在 base 的 ``on_task_done`` 回调里被**串行**触发
（``serial_single``），不会并发写 global harness 那个无锁、非原子的单文件；每次
refine 走 base 独立的 3 次重试预算（``_EVOLVE_HOOK_ATTEMPTS``）。

与 ``GenericAgentActiveEvolveAdapter`` 同构：复用 baseline 全部容器 / chat 逻辑，
在逐题钩子里多一个显式蒸馏触发。对应机制徽章 **R**(Post-hoc Reflection)。
"""

from __future__ import annotations

from typing import override

from src.config import LOGGER
from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.prime_agent.adapter import PrimeAgentAdapter
from src.lift.adapters.prime_agent.session import prime_agent_context
from src.lift.adapters.prime_agent_active_evolve.refine import (
    run_global_refine_evolve,
)
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.container import WarmupContainerPolicy
from src.models import PhaseRun, SuiteTask


class PrimeAgentActiveEvolveAdapter(PrimeAgentAdapter):
    """Prime Agent + explicit per-task global ``/refine`` active evolve runtime."""

    def __init__(self, options: RunOptions) -> None:
        """强制把 warmup 策略 coerce 到 ``serial_single``（active evolve 正确性前提）。

        逐题 ``evolve_after_task`` 用 ``prime-agent -c`` 续接“最近一次会话”做 refine，
        默认它恰好是刚结束的那道题——**只在串行时成立**。若 warmup 并发
        （``parallel_single`` / ``parallel_multi``），多题会话交错，``-c`` 可能续到
        **别的题**的会话，refine 复盘错轨迹；并发还会同时写 global harness 那个无锁、
        非原子的单文件产生竞态。故这里直接 coerce 为 ``serial_single`` 并告警（而非
        baseline 那样仅 warning 放行），确保每题证据被正确提升到 global harness。
        """
        if options.warmup_container_policy is not WarmupContainerPolicy.SERIAL_SINGLE:
            LOGGER.warning(
                "prime_agent_active_evolve requires serial warmup for per-task "
                "'-c' refine correctness; coercing warmup_container_policy %s -> "
                "serial_single.",
                options.warmup_container_policy.value,
            )
            options = options.model_copy(
                update={"warmup_container_policy": WarmupContainerPolicy.SERIAL_SINGLE}
            )
        super().__init__(options)

    @override
    async def evolve_after_task(
        self,
        env: ExecutionEnvironment,
        task: SuiteTask,
        result: PhaseRun,
        ctx: SuiteRunContext,
    ) -> None:
        """每道 warmup 题完成后，对刚结束那题的会话触发 global ``/refine``。

        ``serial_single`` 下本回调被串行调用，故触发时“最近一次会话”恰是刚结束的
        ``task``；``run_global_refine_evolve`` 内部用 ``-c`` 续接该会话，让 refine
        复盘本题真实轨迹，把增量提升到 global harness 累积。
        """
        _ = result
        session_id = (
            f"evolve-refine-r{ctx.repeat_index}-s{ctx.suite_index}-{task.name}"
        )
        await run_global_refine_evolve(
            container=prime_agent_context(env.handle),
            session_id=session_id,
        )

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """No-op：逐题 ``evolve_after_task`` 已对全部 warmup 题（含最后一题）触发过
        global ``/refine``。此处若再 refine 一次，``-c`` 只会续接最后一题会话 →
        对最后一题**重复蒸馏**，白烧 token 且无额外跨题综合（global harness 已逐题
        累积）。base 要求 ``evolve_after_warmup`` 必须实现，故保留为显式 no-op。
        """
        _ = (env, ctx)
        return None


__all__ = ["PrimeAgentActiveEvolveAdapter"]
