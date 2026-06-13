"""LIFTPipeline 与 adapter 共享的运行时选项（由 CLI 解析传入）。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.lift.policies.container import (
    HoldoutContainerPolicy,
    HoldoutPhasePolicy,
    WarmupContainerPolicy,
)


class RunOptions(BaseModel):
    """LIFTPipeline 与 AgentRuntimeAdapter 的运行时配置（由 CLI 解析后传入）。"""

    repeat: int = Field(default=1, description="完整 LIFT 流程重复次数（同一 run_id 内多轮）")
    warmup_only: bool = Field(
        default=False,
        description="仅 warmup + evolve + delta，跳过 hold-out 对照",
    )
    evaluate: bool = Field(
        default=True,
        description="评测结束后是否执行后处理（默认开启，--no-evaluate 关闭）",
    )
    evaluate_only: bool = Field(
        default=False,
        description="仅后处理已有 report（--evaluate-only）",
    )
    docker_image: str | None = Field(
        default=None,
        description="覆盖 agent 配置的 base 镜像（None 时由 ContainerAgentRuntimeAdapter 解析）",
    )
    warmup_container_policy: WarmupContainerPolicy = Field(
        default=WarmupContainerPolicy.PARALLEL_SINGLE,
        description=(
            "warmup 阶段容器编排策略（决定容器数量与是否并发）。"
            "题级并发由该字段统一表达，不再使用单独的 parallel 开关。"
        ),
    )
    holdout_container_policy: HoldoutContainerPolicy = Field(
        default=HoldoutContainerPolicy.PARALLEL_MULTI,
        description=(
            "hold-out 阶段容器编排策略：每题独立容器（强制），仅决定多题是否并发。"
            "默认 ``parallel_multi`` 提速；问题间需要严格隔离时改为 ``serial_multi``。"
        ),
    )
    holdout_phase_policy: HoldoutPhasePolicy = Field(
        default=HoldoutPhasePolicy.PARALLEL,
        description=(
            "单 hold-out task 内 baseline / evolved 两个 phase 的执行顺序。"
            "默认 ``parallel`` 同时启两个容器（baseline 与 evolved 镜像/workspace "
            "子目录互不依赖）；``serial`` 兼容旧行为先 baseline 后 evolved。"
        ),
    )
    delta_materialization: str = Field(
        default="commit_image",
        description="delta 物化方式（当前仅 docker commit）",
    )
    max_parallel_repeats: int | None = Field(
        default=None,
        description=(
            "repeat 并行度上限（也是串/并行开关）。``None`` 表示无上限（所有 repeat "
            "并行）；``1`` 表示串行；``N>1`` 表示最多 N 个 repeat 同时跑。"
        ),
    )
    max_parallel_suites: int | None = Field(
        default=3,
        description=(
            "单 repeat 内 suite 并行度上限。默认 ``3``（最多 3 个 suite 同时跑 "
            "warmup+hold-out）；``1`` 串行；``None`` 或 <=0 表示无上限。每个 suite "
            "各自带题级并发，并发 suite 数 × 题级并发会放大总容器数，需结合资源量设置。"
        ),
    )
    max_concurrent_tasks: int | None = Field(
        default=None,
        description=(
            "题级并发容器数上限（warmup parallel_single/parallel_multi 与 hold-out "
            "parallel_multi 共用此上限）。None 或 <=0 表示无上限。"
            "在大 suite + 资源紧张时设为较小整数避免 docker 资源耗尽。"
        ),
    )
    container_memory: str | None = Field(
        default=None,
        description=(
            "单容器内存上限，透传给 ``docker run --memory``（如 ``3g`` / ``2048m``）。"
            "``None`` 或空串表示不限制——单容器内存交给 VM 内核统一调度，超出物理内存"
            "时落 VM swap（安全网），不会因撞 cgroup 上限被误杀。单 OpenClaw 容器"
            "（node/V8 多进程）峰值可能超 3g，故默认不设上限，靠 ``--max-parallel-suites``"
            "等并发开关 + VM 内存/swap 控制总量。"
        ),
    )
    container_cpus: str | None = Field(
        default=None,
        description=(
            "单容器 CPU 上限，透传给 ``docker run --cpus``（如 ``2`` / ``1.5``）。"
            "``None`` 表示不限制（容器共享 VM 全部核）。"
        ),
    )

    @model_validator(mode="after")
    def _normalize_options(self) -> RunOptions:
        """校验 repeat 下限。``max_parallel_repeats`` / ``max_concurrent_tasks``
        保持 ``None`` 语义（无上限），由下游 ``bounded_gather`` 解释。"""
        if self.repeat < 1:
            raise ValueError("--repeat must be at least 1")
        return self
