"""GenericAgent 容器内 ``docker exec`` 封装与上下文。"""

from __future__ import annotations

from dataclasses import dataclass

from src.lift.adapters.container.exec import docker_exec_async


@dataclass(frozen=True)
class GenericAgentContainerContext:
    """GA 容器 exec 所需的最小上下文。

    与 OpenClaw 不同：GA 没有 gateway / token / 端口，``docker exec`` 时不需要任何
    握手参数。``container_name`` 是后续 chat 路径的唯一坐标。
    """

    container_name: str


async def exec_genericagent_async(
    ctx: GenericAgentContainerContext,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    label: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """``docker exec`` GA 容器执行 ``command`` 并返回 stdout。

    GA 不需要 OpenClaw 的 ``OPENCLAW_GATEWAY_TOKEN`` 等运行时 secret——所有
    模型 / Langfuse 凭据已 bake 进 ``mykey.py``，``docker exec`` 默认继承
    容器 ``Config.Env``，无需通过 ``-e`` 注入除 LIFT 自有标签外的任何变量。
    ``env`` 仅在 chat 时注入 ``LIFT_GA_SESSION_ID`` / ``LIFT_EVAL_RUN_TAG``。
    """
    return await docker_exec_async(
        ctx.container_name,
        command,
        env=env,
        label=label,
        timeout_seconds=timeout_seconds,
    )
