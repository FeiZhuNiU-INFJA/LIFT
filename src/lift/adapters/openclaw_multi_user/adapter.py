"""MultiUserOpenClawAdapter：OpenClaw runtime + 群体记忆编排策略组合。

通过多重继承把 ``GroupMemoryAdapterMixin`` 的多容器 + 外部记忆编排
混入 ``OpenClawAdapter``。

特性：
    - warmup 每题独立容器并行（``WarmupContainerPolicy.PARALLEL_MULTI``，本 adapter
      默认覆盖该值）
    - evolve_after_task / evolve_after_warmup 默认 no-op（群体记忆在 chat 期间由 OpenClaw 插件写入）
    - materialize_delta 不做 docker commit，evolved holdout 复用 base 镜像
    - holdout 通过 ``load_state`` 区分 baseline / evolved（baseline 不读群体记忆，
      evolved 读取已学群体记忆——具体注入逻辑由 runtime 插件配合实现）

依赖：
    群体记忆插件本身不在本框架职责内；本 adapter 仅保证编排层能正确把信号传到
    OpenClaw 容器。建议群体记忆插件读取的环境变量名通过子类 ``start_container``
    在 evolved 路径注入（如 ``GROUP_MEMORY_NAMESPACE`` 等）。
"""

from __future__ import annotations

from src.lift.adapters.group_memory.mixin import GroupMemoryAdapterMixin
from src.lift.adapters.openclaw.adapter import OpenClawAdapter
from src.lift.pipeline.run_options import RunOptions
from src.lift.policies.container import WarmupContainerPolicy


class MultiUserOpenClawAdapter(GroupMemoryAdapterMixin, OpenClawAdapter):
    """OpenClaw + 群体记忆 Mixin 的组合 adapter。

    MRO（C3 线性化）::

        MultiUserOpenClawAdapter
          → GroupMemoryAdapterMixin   (编排层 override：produce_delta 等)
          → OpenClawAdapter           (runtime 特性：start_container / chat factory)
          → ContainerAgentRuntimeAdapter
          → AgentRuntimeAdapter
          → ABC

    Mixin 不继承 ``AgentRuntimeAdapter``，是单纯能力混入。本 adapter 在 ``__init__``
    中把 ``warmup_container_policy`` 默认覆盖为 ``PARALLEL_MULTI``——只要 CLI 没有
    显式指定其它值即生效；群体记忆产物形态强依赖多容器编排，否则 Mixin 校验失败。

    具体的群体记忆 env 注入（如 ``GROUP_MEMORY_NAMESPACE``）建议在子类中覆盖
    ``start_container`` 实现，按 ``load_state`` 区分 baseline / evolved。
    """

    def __init__(self, options: RunOptions | None = None) -> None:
        """默认把 ``warmup_container_policy`` 覆盖为 ``PARALLEL_MULTI``。

        除非调用方已显式指定 ``PARALLEL_MULTI``，否则一律升级——群体记忆产物形态
        强依赖多容器编排，``SERIAL_SINGLE`` / ``PARALLEL_SINGLE`` 共享容器都会让
        Mixin 校验失败。
        """
        opts = options or RunOptions()
        if opts.warmup_container_policy is not WarmupContainerPolicy.PARALLEL_MULTI:
            opts = opts.model_copy(
                update={"warmup_container_policy": WarmupContainerPolicy.PARALLEL_MULTI}
            )
        super().__init__(opts)
