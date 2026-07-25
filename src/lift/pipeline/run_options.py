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
        description="仅 warmup + evolve + delta，跳过 holdout 对照",
    )
    evaluate: bool = Field(
        default=True,
        description="评测结束后是否执行后处理（默认开启，--no-evaluate 关闭）",
    )
    evaluate_only: bool = Field(
        default=False,
        description="仅后处理已有 report（--evaluate-only）",
    )
    resume: bool = Field(
        default=False,
        description=(
            "从 results/{run_id}/report.json 断点续跑：已完成的 (repeat, suite) cell "
            "整格跳过,只跑缺失/半成品 cell。粒度是 cell 级,因为 delta 镜像在 suite "
            "结束时已 rmi,partial cell 必须整个重跑。"
        ),
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
            "holdout 阶段容器编排策略：每题独立容器（强制），仅决定多题是否并发。"
            "默认 ``parallel_multi`` 提速；问题间需要严格隔离时改为 ``serial_multi``。"
        ),
    )
    holdout_phase_policy: HoldoutPhasePolicy = Field(
        default=HoldoutPhasePolicy.PARALLEL,
        description=(
            "单 holdout task 内 baseline / evolved 两个 phase 的执行顺序。"
            "默认 ``parallel`` 同时启两个容器（baseline 与 evolved 镜像/workspace "
            "子目录互不依赖）；``serial`` 兼容旧行为先 baseline 后 evolved。"
        ),
    )
    delta_materialization: str = Field(
        default="commit_image",
        description="delta 物化方式（当前仅 docker commit）",
    )
    max_parallel_suites: int | None = Field(
        default=3,
        description=(
            "suites × repeats 矩阵中 cell 级并发上限（一个 cell = 一个 (repeat, "
            "suite) 对，对应一次 warmup+holdout）。默认 ``3``；``1`` 串行；"
            "``None`` 或 <=0 表示无上限。每个 cell 还自带题级并发，"
            "总容器数 = 并发 cell 数 × 题级并发，需结合资源量设置。"
        ),
    )
    max_concurrent_tasks: int | None = Field(
        default=None,
        description=(
            "题级并发容器数上限（warmup parallel_single/parallel_multi 与 holdout "
            "parallel_multi 共用此上限）。None 或 <=0 表示无上限。"
            "在大 suite + 资源紧张时设为较小整数避免 docker 资源耗尽。"
        ),
    )
    max_conversation_turns: int = Field(
        default=5,
        description=(
            "单个 task 内 work→judge 的最大对话轮数：judge 未通过时用 reason 反馈"
            "重试，最多跑 ``max_conversation_turns`` 轮（由 ``--max-conversation-turns`` 设置）。"
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
        """校验 repeat 下限。``max_parallel_suites`` / ``max_concurrent_tasks``
        保持 ``None`` 语义（无上限），由下游 ``bounded_gather`` 解释。"""
        if self.repeat < 1:
            raise ValueError("--repeat must be at least 1")
        if self.max_conversation_turns < 1:
            raise ValueError("--max-conversation-turns must be at least 1")
        return self
