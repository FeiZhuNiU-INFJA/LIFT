"""``--agent-runtime`` 工厂注册：按名称构造 ``AgentRuntimeAdapter`` 实例。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.lift.pipeline.run_options import RunOptions

if TYPE_CHECKING:
    from src.lift.adapters.base import AgentRuntimeAdapter

SUPPORTED_RUNTIMES = (
    "openclaw",
    "openclaw_with_evolve",
    "openclaw_with_openspace",
    "openclaw_with_agentmemory",
    "multi_user_openclaw",
    "genericagent",
    "genericagent_active_evolve",
    "hermes",
    "hermes_with_openspace",
    "hermes_with_agentmemory",
    "openhuman",
    "openhuman_with_agentmemory",
    "evoscientist",
    "evoscientist_active_evolve",
)  # CLI 可选的运行时标识


def create_adapter(runtime: str, options: RunOptions) -> AgentRuntimeAdapter:
    """根据 ``runtime`` 名称构造对应的 ``AgentRuntimeAdapter``。"""
    normalized = runtime.strip().lower()
    if normalized == "openclaw":
        # OpenClaw 基础版（不带进化插件）：base 镜像 + evolve_after_warmup no-op，仍保留 docker commit
        from src.lift.adapters.openclaw.adapter import OpenClawAdapter

        return OpenClawAdapter(options)
    if normalized == "openclaw_with_evolve":
        # OpenClaw + self-evolving-plugin-pro：with-evolve 镜像 + warmup 后 openclaw learn review
        from src.lift.adapters.openclaw_with_evolve.adapter import OpenClawWithEvolveAdapter

        return OpenClawWithEvolveAdapter(options)
    if normalized == "openclaw_with_openspace":
        # OpenClaw + OpenSpace MCP 插件：with-openspace 镜像，复用基础 warmup/evolve/commit 流程
        from src.lift.adapters.openclaw_with_openspace.adapter import OpenClawWithOpenSpaceAdapter

        return OpenClawWithOpenSpaceAdapter(options)
    if normalized == "openclaw_with_agentmemory":
        # OpenClaw + agentmemory memory plugin：with-agentmemory 镜像；容器内起 :3111
        # agentmemory server（离线本地嵌入），强制 bridge 网络，复用基础 warmup/commit 流程
        from src.lift.adapters.openclaw_with_agentmemory.adapter import (
            OpenClawWithAgentMemoryAdapter,
        )

        return OpenClawWithAgentMemoryAdapter(options)
    if normalized == "multi_user_openclaw":
        # OpenClaw + 群体记忆 Mixin（多容器 warmup，evolve 落到外部记忆系统）
        from src.lift.adapters.openclaw_multi_user.adapter import MultiUserOpenClawAdapter

        return MultiUserOpenClawAdapter(options)
    if normalized == "genericagent":
        # GenericAgent baseline：agentmain.py --task 文件 I/O，无显式 evolve hook
        from src.lift.adapters.genericagent.adapter import GenericAgentAdapter

        return GenericAgentAdapter(options)
    if normalized == "genericagent_active_evolve":
        # GenericAgent + 主动复盘：per-task + suite 收尾各发一次 reflection chat
        from src.lift.adapters.genericagent_active_evolve.adapter import (
            GenericAgentActiveEvolveAdapter,
        )

        return GenericAgentActiveEvolveAdapter(options)
    if normalized == "hermes":
        # Hermes：容器空转 + docker exec 拉起 hermes_runner；review 驱动的隐式演化
        from src.lift.adapters.hermes.adapter import HermesAdapter

        return HermesAdapter(options)
    if normalized == "hermes_with_openspace":
        # Hermes + OpenSpace MCP 插件：with-openspace 镜像，复用基础 review/commit 流程
        from src.lift.adapters.hermes_with_openspace.adapter import HermesWithOpenSpaceAdapter

        return HermesWithOpenSpaceAdapter(options)
    if normalized == "hermes_with_agentmemory":
        # Hermes + agentmemory memory provider plugin：with-agentmemory 镜像；容器内起 :3111
        # agentmemory server（离线本地嵌入），config.yaml memory.provider=agentmemory，
        # 强制 bridge 网络，复用基础 review/commit 流程
        from src.lift.adapters.hermes_with_agentmemory.adapter import (
            HermesWithAgentMemoryAdapter,
        )

        return HermesWithAgentMemoryAdapter(options)
    if normalized == "openhuman":
        # OpenHuman baseline：Rust core serve 暴露 JSON-RPC，chat 走 HTTP agent.chat
        from src.lift.adapters.openhuman.adapter import OpenHumanAdapter

        return OpenHumanAdapter(options)
    if normalized == "openhuman_with_agentmemory":
        # OpenHuman + agentmemory backend：with-agentmemory 镜像；config.toml
        # [memory] backend=agentmemory 旁路自家 SQLite，代理到容器内 :3111 server
        # （离线本地嵌入），强制 bridge 网络，复用基础 commit 流程
        from src.lift.adapters.openhuman_with_agentmemory.adapter import (
            OpenHumanWithAgentMemoryAdapter,
        )

        return OpenHumanWithAgentMemoryAdapter(options)
    if normalized == "evoscientist":
        # EvoScientist baseline：EvoSci -p ... --output-format stream-json （headless）；
        # 无显式 evolve 触发，warmup 期自然写入的 memories/skills 由 docker commit 携带。
        from src.lift.adapters.evoscientist.adapter import EvoScientistAdapter

        return EvoScientistAdapter(options)
    if normalized == "evoscientist_active_evolve":
        # EvoScientist + AutoSkills：warmup 后显式运行 EvoMemory AutoSkills graph，
        # 等后台 run 完成后再 docker commit /root/.evoscientist。
        from src.lift.adapters.evoscientist_active_evolve.adapter import (
            EvoScientistActiveEvolveAdapter,
        )

        return EvoScientistActiveEvolveAdapter(options)
    supported = ", ".join(SUPPORTED_RUNTIMES)
    raise ValueError(f"Unknown runtime {runtime!r}; supported: {supported}")
