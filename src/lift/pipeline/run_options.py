"""LIFTPipeline 与 adapter 共享的运行时选项（由 CLI 解析传入）。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.lift.policies.container import HoldoutContainerPolicy, WarmupContainerPolicy


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
    incremental_report: bool = Field(
        default=True,
        description="每个 suite 完成后是否增量写 report JSON",
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
    max_concurrent_tasks: int | None = Field(
        default=None,
        description=(
            "题级并发容器数上限（warmup parallel_single/parallel_multi 与 hold-out "
            "parallel_multi 共用此上限）。None 或 <=0 表示无上限。"
            "在大 suite + 资源紧张时设为较小整数避免 docker 资源耗尽。"
        ),
    )

    @model_validator(mode="after")
    def _normalize_options(self) -> RunOptions:
        """校验 repeat 下限。``max_parallel_repeats`` / ``max_concurrent_tasks``
        保持 ``None`` 语义（无上限），由下游 ``bounded_gather`` 解释。"""
        if self.repeat < 1:
            raise ValueError("--repeat must be at least 1")
        return self
