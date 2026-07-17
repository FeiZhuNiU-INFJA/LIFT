"""Run EvoScientist AutoSkills as LIFT's explicit evolve hook."""

from __future__ import annotations

import json
from pathlib import Path
import textwrap
from typing import Any

from src.config import LOGGER
from src.lift.adapters.evoscientist.container_exec import (
    EvoScientistContainerContext,
    exec_evoscientist_async,
)


AUTOSKILLS_EVOLVE_TIMEOUT_SECONDS = 1800.0
_RESULT_PREFIX = "LIFT_AUTOSKILLS_RESULT="


def _autoskills_evolve_script(workspace_dir: Path) -> str:
    """Build the in-container script that runs AutoSkills to completion."""
    _ = workspace_dir
    return textwrap.dedent(
        """\
        set -euo pipefail
        export PATH=/opt/venv/bin:$PATH
        mkdir -p /workspace/task
        /opt/venv/bin/python - <<'PY'
        import json
        from pathlib import Path

        from EvoScientist.config import get_effective_config, set_config_value
        from EvoScientist.langgraph_dev.manager import ensure_langgraph_dev
        from EvoScientist.langgraph_dev.sdk import get_langgraph_sync_client, langgraph_dev_url
        from EvoScientist.memory.autoskills.schedule import run_autoskill_now
        from EvoScientist.memory.autoskills.proposals import list_skill_proposals
        from EvoScientist import paths

        workspace_dir = Path("/workspace/task").resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Use EvoScientist's own AutoSkills mechanism in auto-approval mode.
        # This mirrors the public /autoskills auto + /autoskills run workflow,
        # while avoiding fragile TTY automation in LIFT's non-interactive hook.
        set_config_value("memory_skill_synthesis_enabled", "true")
        set_config_value("memory_skill_synthesis_mode", "auto")
        cfg = get_effective_config()

        ensure_langgraph_dev(cfg, workspace_dir=workspace_dir)
        result = run_autoskill_now(cfg, workspace_dir=workspace_dir)
        thread_id = str(result["thread_id"])
        run_id = str(result["run_id"])

        client = get_langgraph_sync_client(url=langgraph_dev_url(cfg))
        client.runs.join(thread_id, run_id)
        run = client.runs.get(thread_id, run_id)
        status = str(run.get("status", ""))
        if status and status not in {"success", "completed"}:
            raise RuntimeError(f"AutoSkills run {run_id} ended with status={status}: {run}")

        proposals_all = list_skill_proposals(paths.MEMORIES_DIR, workspace_dir=workspace_dir)
        proposals_approved = list_skill_proposals(
            paths.MEMORIES_DIR,
            status="approved",
            workspace_dir=workspace_dir,
        )
        proposals_pending = list_skill_proposals(
            paths.MEMORIES_DIR,
            status="pending",
            workspace_dir=workspace_dir,
        )

        def child_dirs(path: Path) -> list[str]:
            if not path.exists():
                return []
            return sorted(p.name for p in path.iterdir() if p.is_dir())

        payload = {
            "thread_id": thread_id,
            "run_id": run_id,
            "status": status or "unknown",
            "memory_dir": str(paths.MEMORIES_DIR),
            "global_skills_dir": str(paths.GLOBAL_SKILLS_DIR),
            "proposal_count": len(proposals_all),
            "approved_proposal_count": len(proposals_approved),
            "pending_proposal_count": len(proposals_pending),
            "global_skill_dirs": child_dirs(paths.GLOBAL_SKILLS_DIR),
            "autoskill_memory_dirs": child_dirs(paths.MEMORIES_DIR / "autoskills"),
        }
        print("LIFT_AUTOSKILLS_RESULT=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
        PY
        """
    )


def _parse_autoskills_result(stdout: str) -> dict[str, Any]:
    """Extract the sentinel JSON emitted by the in-container AutoSkills script."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(_RESULT_PREFIX):
            payload = line[len(_RESULT_PREFIX) :]
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise ValueError("AutoSkills result payload must be a JSON object")
            return parsed
    raise ValueError(f"AutoSkills result sentinel not found in output:\n{stdout[-4000:]}")


async def run_autoskills_evolve(
    *,
    container: EvoScientistContainerContext,
    workspace_dir: Path,
    session_id: str,
) -> dict[str, Any]:
    """Run EvoScientist AutoSkills in the warmup container and wait for completion."""
    LOGGER.info(
        "[evoscientist_active_evolve] AutoSkills start container=%s workspace=%s session=%s",
        container.container_name,
        workspace_dir,
        session_id,
    )
    stdout = await exec_evoscientist_async(
        container,
        ["bash", "-lc", _autoskills_evolve_script(workspace_dir)],
        env={"LIFT_EVOSCI_SESSION_ID": session_id},
        label="evoscientist autoskills evolve",
        timeout_seconds=AUTOSKILLS_EVOLVE_TIMEOUT_SECONDS,
    )
    result = _parse_autoskills_result(stdout)
    LOGGER.info(
        "[evoscientist_active_evolve] AutoSkills done container=%s result=%s",
        container.container_name,
        result,
    )
    if not result.get("approved_proposal_count"):
        LOGGER.warning(
            "[evoscientist_active_evolve] AutoSkills completed but approved no proposals "
            "container=%s run_id=%s",
            container.container_name,
            result.get("run_id"),
        )
    return result


__all__ = [
    "AUTOSKILLS_EVOLVE_TIMEOUT_SECONDS",
    "run_autoskills_evolve",
    "_autoskills_evolve_script",
    "_parse_autoskills_result",
]
