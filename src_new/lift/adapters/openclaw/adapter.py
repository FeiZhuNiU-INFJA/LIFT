"""OpenClaw runtime adapter：镜像解析、容器启动、chat factory 与 evolve 钩子。"""

from __future__ import annotations

from pathlib import Path
from typing import override

from src_new.lift.adapters.base import SuiteRunContext
from src_new.lift.adapters.container.adapter import ContainerAgentRuntimeAdapter
from src_new.lift.adapters.container.session import ContainerSession
from src_new.lift.adapters.environment import ExecutionEnvironment
from src_new.lift.adapters.openclaw.agent import OpenClawWorkerJudgerPairFactory
from src_new.lift.adapters.openclaw.evolve import openclaw_learn_review
from src_new.lift.adapters.openclaw.session import openclaw_context, start_openclaw_container
from src_new.lift.eval.stage import SuiteRunPhase
from src_new.lift.eval.worker_judger import WorkerJudgerPairFactory
from src_new.models import SuiteTask


class OpenClawAdapter(ContainerAgentRuntimeAdapter):
    """OpenClaw：镜像配置、容器启动、chat factory 与 evolve 钩子。"""

    @classmethod
    @override
    def resolve_docker_image(cls, *, override: str | None = None) -> str:
        """CLI override 优先，否则从 ``container_defaults.yaml`` 读取 ``docker_image``。"""
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
        """OpenClaw 容器默认配置 YAML 的仓库内路径。"""
        return Path(__file__).resolve().parents[4] / "agents" / "openclaw" / "container_defaults.yaml"

    @override
    async def start_container(
        self,
        *,
        instance_id: str,
        image: str,
        ctx: SuiteRunContext,
        workspace_dir: Path,
        seed_workspace: bool,
        task: SuiteTask | None,
    ) -> ContainerSession:
        """委托 ``start_openclaw_container`` 启动 gateway 容器。"""
        return await start_openclaw_container(
            instance_id=instance_id,
            image=image,
            ctx=ctx,
            workspace_dir=workspace_dir,
            seed_workspace=seed_workspace,
            task=task,
        )

    @override
    def worker_judger_factory(
        self,
        env: ExecutionEnvironment,
        ctx: SuiteRunContext,
        *,
        run_phase: SuiteRunPhase,
        workspace_dir: Path,
    ) -> WorkerJudgerPairFactory:
        """绑定 OpenClaw 容器上下文，返回 ``OpenClawWorkerJudgerPairFactory``。"""
        session: ContainerSession = env.handle
        return OpenClawWorkerJudgerPairFactory(
            container=openclaw_context(session),
            run_id=ctx.run_id,
            repeat_index=ctx.repeat_index,
            run_phase=run_phase,
            workspace_dir=workspace_dir,
        )

    @override
    async def apply_evolve(self, env: ExecutionEnvironment, ctx: SuiteRunContext) -> None:
        """warmup 完成后在容器内执行 ``openclaw learn review``。"""
        _ = ctx
        session: ContainerSession = env.handle
        await openclaw_learn_review(openclaw_context(session))
