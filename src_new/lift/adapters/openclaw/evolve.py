from __future__ import annotations

from src_new.lift.adapters.openclaw.container_env import container_runtime_env
from src_new.lift.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
    exec_shell_async,
)


async def openclaw_learn_review(container: OpenClawContainerContext) -> None:
    """Run OpenClaw evolve hook after warmup tasks."""
    env = container_runtime_env()
    await exec_shell_async(
        container.container_name,
        """
mkdir -p /workspace/task
git config --global --add safe.directory /workspace/task
WORKER_JS="${HOME}/.openclaw/extensions/self-evolving-plugin-pro/src/review/worker.js"
if [[ -f "${WORKER_JS}" ]]; then
  sed -i 's/"--thinking", "low"/"--thinking", "off"/g' "${WORKER_JS}" || true
fi
""".strip(),
        extra_env=env,
    )
    await exec_openclaw_async(container, ["learn", "review"])
