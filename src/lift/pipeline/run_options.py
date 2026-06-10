"""LIFTPipeline 与 adapter 共享的运行时选项（由 CLI 解析传入）。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.lift.policies.container import WarmupContainerPolicy


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
    parallel: bool = Field(
        default=False,
        description="warmup 题是否并行执行（受 warmup_container_policy 约束）",
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
        default=WarmupContainerPolicy.SERIAL_SINGLE,
        description="warmup 阶段容器编排策略",
    )
    delta_materialization: str = Field(
        default="commit_image",
        description="delta 物化方式（当前仅 docker commit）",
    )
    parallel_repeats: bool = Field(
        default=True,
        description="多轮 repeat 是否并行执行",
    )
    max_parallel_repeats: int | None = Field(
        default=None,
        description="repeat 并行度上限（默认等于 repeat）",
    )

    @model_validator(mode="after")
    def _normalize_options(self) -> RunOptions:
        """校验 repeat 下限并补全 ``max_parallel_repeats`` 默认值。"""
        if self.repeat < 1:
            raise ValueError("--repeat must be at least 1")
        if self.max_parallel_repeats is None:
            self.max_parallel_repeats = self.repeat
        return self
