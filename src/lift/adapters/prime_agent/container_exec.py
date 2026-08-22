"""Prime Agent 容器 ``docker exec`` 封装与上下文。

对齐 EvoScientist / GenericAgent：把容器坐标封装成 dataclass，方便 chat_agent
在多题并发下持有稳定 handle。Prime Agent 的 Work LLM 凭据已 bake 进镜像内
``prime-agent`` 配置；只有每轮变化的 LIFT session id 需在 ``docker exec -e`` 时注入。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.lift.adapters.container.exec import docker_exec_async


@dataclass(frozen=True)
class PrimeAgentContainerContext:
    """Prime Agent 容器 exec 所需的最小上下文。

    ``container_name`` 是 chat 与 workspace 同步的唯一坐标；build 期烧入的
    provider / langfuse secrets 通过 ``docker exec`` 默认继承 ``Config.Env``，
    只有每轮变化的 LIFT session id 需要在 ``docker exec -e`` 时注入。
    """

    container_name: str


async def exec_prime_agent_async(
    ctx: PrimeAgentContainerContext,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    label: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """在 Prime Agent 容器执行 ``command`` 并返回 stdout。"""
    return await docker_exec_async(
        ctx.container_name,
        command,
        env=env,
        label=label,
        timeout_seconds=timeout_seconds,
    )
