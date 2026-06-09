from __future__ import annotations

from pathlib import Path
from typing import override

from src_new.lift.adapters.base import RunContext
from src_new.lift.adapters.container.adapter import ContainerRuntimeAdapter
from src_new.lift.adapters.container.session import ContainerSession
from src_new.lift.adapters.environment import ExecutionEnvironment
from src_new.lift.adapters.openclaw.agent import OpenClawAgentPairFactory
from src_new.lift.adapters.openclaw.evolve import openclaw_learn_review
from src_new.lift.adapters.openclaw.session import openclaw_context, start_openclaw_container
from src_new.lift.eval.agent_pair import TaskAgentPairFactory
from src_new.models import SuiteTask
class OpenClawAdapter(ContainerRuntimeAdapter):
    """OpenClaw: image config, container start, chat factory, and evolve hook."""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        if override:
            return override
        config_path = cls._agent_config_path()
        if not config_path.is_file():
            raise FileNotFoundError(
                f"OpenClaw agent config not found: {config_path}. "
                "Build the image and ensure agents/openclaw/container_defaults.yaml exists."
            )
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("docker_image:"):
                image = line.split(":", 1)[1].strip().strip('"').strip("'")
                if image:
                    return image
                break
        raise ValueError(f"docker_image not set in {config_path}")

    @staticmethod
    def _agent_config_path() -> Path:
        return Path(__file__).resolve().parents[4] / "agents" / "openclaw" / "container_defaults.yaml"

    @override
    async def start_container(
        self,
        *,
        instance_id: str,
        image: str,
        ctx: RunContext,
        workspace_dir: Path,
        seed_workspace: bool,
        task: SuiteTask | None,
    ) -> ContainerSession:
        return await start_openclaw_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
        )

    @override
    def create_agent_pair_factory(
        self,
        env: ExecutionEnvironment,
        ctx: RunContext,
        *,
        phase: str,
        workspace_dir: Path,
    ) -> TaskAgentPairFactory:
        session: ContainerSession = env.handle
        return OpenClawAgentPairFactory(
            container=openclaw_context(session),
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            phase=phase,
            workspace_dir=workspace_dir,
        )

    @override
    async def apply_evolve(self, env: ExecutionEnvironment, ctx: RunContext) -> None:
        _ = ctx
        session: ContainerSession = env.handle
        await openclaw_learn_review(openclaw_context(session))
