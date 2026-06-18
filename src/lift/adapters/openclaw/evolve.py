"""OpenClaw warmup 后 evolve 钩子：``openclaw learn review``。"""

from __future__ import annotations

from src.config import LOGGER
from src.lift.adapters.container.exec import (
    capture_container_logs,
    docker_exec_shell_async,
)
from src.lift.adapters.openclaw.container_exec import (
    OpenClawContainerContext,
    exec_openclaw_async,
)


async def openclaw_learn_review(container: OpenClawContainerContext) -> None:
    """warmup 题完成后在容器内执行 evolve（learn review + worker 配置）。

    所有 runtime env（``LANGFUSE_BASE_URL`` 等）已在 ``docker run`` 阶段写入容器
    ``Config.Env``，``docker exec`` 自动继承，因此这里不再显式 ``-e`` 注入。
    """
    # 预备：
    # 1. self-evolving-plugin-pro `/instances/onboard` 要求 workspace_root 是个
    #    git repo（git_root == workspace_root）且至少有一个 HEAD commit；warmup
    #    workspace 是 LIFT 现 seed 的目录，没初始化过，需要在调用 `learn review`
    #    前 `git init` + 一次空 commit，否则会被 plugin 拒为 400。
    # 2. git safe.directory：容器内 git 默认拒绝 owner 不符的目录（host bind mount）。
    # 3. review worker 改 thinking=off：Ark 不支持 thinking=low，加速 warmup evolve。
    await docker_exec_shell_async(
        container.container_name,
        """
mkdir -p /workspace/task
git config --global --add safe.directory /workspace/task
git config --global user.email "lift@local"
git config --global user.name "lift"
if [[ ! -d /workspace/task/.git ]]; then
  git -C /workspace/task init -q
  git -C /workspace/task add -A
  git -C /workspace/task commit -q --allow-empty -m "lift: warmup baseline"
fi
WORKER_JS="${HOME}/.openclaw/extensions/self-evolving-plugin-pro/src/review/worker.js"
if [[ -f "${WORKER_JS}" ]]; then
  sed -i 's/"--thinking", "low"/"--thinking", "off"/g' "${WORKER_JS}" || true
fi
""".strip(),
    )
    review_stdout = await exec_openclaw_async(container, ["learn", "review"])
    if review_stdout.strip():
        LOGGER.info(
            "openclaw learn review stdout (%s):\n%s",
            container.container_name,
            review_stdout.strip(),
        )
    container_log = await capture_container_logs(container.container_name, tail=500)
    if container_log:
        LOGGER.info(
            "openclaw learn review container logs (%s, last 500 lines):\n%s",
            container.container_name,
            container_log,
        )

