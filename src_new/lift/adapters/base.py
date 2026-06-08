from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from src_new.models import PhaseRun, SuiteTask

from src_new.lift.policies.artifact import ArtifactPolicy
from src_new.lift.runtime.delta_ref import DeltaRef
from src_new.lift.runtime.suite_run_resources import SuiteRunResources


class LoadState(Enum):
    """hold-out 题评测时的产物加载状态。"""

    BEFORE_LOAD = "before_load"  # baseline：不加载进化产物
    AFTER_LOAD = "after_load"  # evolved：加载 warmup 产出的 delta


class RunContext(BaseModel):
    """单次 suite 评测的不可变运行坐标，由 pipeline 构造并传给 adapter 各方法。"""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(description="评测批次 ID（如 evobench-runid-hello-full）")
    repeat_index: int = Field(description="当前 repeat 序号（0 起，对应 --repeat 第几轮）")
    suite_path: Path = Field(description="suite JSON 文件路径")
    category_name: str = Field(description="场景分类名（来自 Suite.category）")
    suite_name: str = Field(description="suite 名称（来自 Suite.name）")


class RuntimeAdapter(ABC):
    """LIFT 评测契约的运行时实现基类。

    各 Agent 后端（OpenClaw、Hermes 等）继承本类，串联
    warmup → 产出 delta → hold-out baseline/evolved 执行。
    ``LIFTPipeline`` 在每个 suite/repeat 内按固定顺序调用下列四个方法。
    """

    @abstractmethod
    async def create_suite_run_resources(self, ctx: RunContext) -> SuiteRunResources:
        """为单次 suite 评测创建资源登记簿（``SuiteRunResources``）。

        在每个 (repeat_index, suite) 开始 warmup 或 hold-out 之前调用一次。
        返回的 ``SuiteRunResources`` 用于 ``track()`` 登记本 suite 评测中创建的
        容器或会话，以便 suite 结束时由 ``resources.cleanup()`` 统一释放（含 delta 镜像）。

        参数:
            ctx: 不可变的运行坐标（run_id、repeat_index、suite 元数据）。

        返回:
            绑定 ``ctx.run_id`` / ``ctx.repeat_index`` / ``ctx.suite_name`` 的
            新 ``SuiteRunResources``。Pipeline 会将同一登记簿贯穿
            ``produce_delta``、``run_before_load``、``run_after_load``。
        """

    @abstractmethod
    async def produce_delta(
        self,
        resources: SuiteRunResources,
        policy: ArtifactPolicy,
        warmup_tasks: list[SuiteTask],
        ctx: RunContext,
    ) -> DeltaRef:
        """执行 warmup 题、触发产物更新，并物化为 delta。

        实现 LIFT 的 **ArtifactPolicy** 阶段：跑完所有 warmup（非 hold-out）题，
        再调用运行时的 evolve/update 钩子（如 OpenClaw 的 ``openclaw learn review``），
        并将结果状态固化为可加载的产物。

        OpenClaw 典型做法是对 warmup 容器 ``docker commit`` 为临时镜像，
        标签由 ``delta_image_tag()`` 生成；delta 写入 ``resources.delta``，
        供后续 ``run_after_load`` 使用。

        warmup 的 ``PhaseRun`` **不会**写入评测 report，仅向 pipeline 返回 delta 引用。

        参数:
            resources: suite 资源登记簿；实现方应在此 ``track()`` warmup 容器，
                并在返回前设置 ``resources.delta``。
            policy: 产物如何产生（默认 ``WarmupThenUpdatePolicy``）。
            warmup_tasks: ``split_suite_tasks`` 切分后的 warmup 题列表。
            ctx: 与 ``create_suite_run_resources`` 相同的运行坐标。

        返回:
            指向已固化产物的 ``DeltaRef``（如 delta 镜像 tag）。

        异常:
            ValueError: ``warmup_tasks`` 为空或 policy 不受支持时。
        """

    @abstractmethod
    async def run_before_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        ctx: RunContext,
        *,
        phase: str = "baseline",
    ) -> PhaseRun:
        """在**未加载**进化产物的前提下评测一道 hold-out 题。

        对应 LIFT **before-load** / ``LoadState.BEFORE_LOAD``：全新运行时环境
        （OpenClaw 为 base 镜像）+ 按题隔离的 workspace，Agent 不得看到 warmup evolve 结果。

        执行完整 task 回路（user agent + judge，多轮直至成功或达上限），
        返回写入 ``TaskRun.baseline`` 的 ``PhaseRun``。

        参数:
            task: 单道 hold-out ``SuiteTask``。
            resources: suite 资源登记簿，用于跟踪 hold-out 临时容器。
            ctx: 运行坐标。
            phase: report/workspace 标签（pipeline 传入 ``"baseline"``）。

        返回:
            含 success、content_score、session id、workspace_dir 的 ``PhaseRun``。
        """

    @abstractmethod
    async def run_after_load(
        self,
        task: SuiteTask,
        resources: SuiteRunResources,
        delta: DeltaRef,
        ctx: RunContext,
    ) -> PhaseRun:
        """在**已加载**进化产物的前提下评测同一道 hold-out 题。

        对应 LIFT **after-load** / ``LoadState.AFTER_LOAD``：与 ``run_before_load``
        相同的题目与 judge 协议，但运行时从 ``produce_delta`` 产出的 delta 启动
        （如 OpenClaw 的 delta 镜像）。

        workspace 仍须按题隔离（每 phase 独立目录），避免与 baseline 串答案；
        两次运行之间仅 **产物加载状态** 不同。

        参数:
            task: 与配对 baseline 相同的 hold-out 题。
            resources: suite 资源登记簿，用于跟踪 hold-out 临时容器。
            delta: ``produce_delta`` 返回的产物引用。
            ctx: 运行坐标。

        返回:
            写入 ``TaskRun.evolved`` 的 ``PhaseRun``。
        """
