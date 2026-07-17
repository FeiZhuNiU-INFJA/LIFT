"""EvoScientist runtime with explicit AutoSkills evolution.

This adapter keeps the baseline EvoScientist image and chat path unchanged, but
overrides the suite-level warmup evolve hook. After all warmup tasks complete,
it runs EvoScientist's own AutoSkills maintenance graph in the still-live warmup
container, waits for the graph run to finish, then lets the normal docker commit
capture `/root/.evoscientist`.
"""

from __future__ import annotations

from typing import override

from src.lift.adapters.base import SuiteRunContext
from src.lift.adapters.environment import ExecutionEnvironment
from src.lift.adapters.evoscientist.adapter import EvoScientistAdapter
from src.lift.adapters.evoscientist.session import evoscientist_context
from src.lift.adapters.evoscientist_active_evolve.autoskills import (
    run_autoskills_evolve,
)


class EvoScientistActiveEvolveAdapter(EvoScientistAdapter):
    """EvoScientist + AutoSkills active evolve runtime."""

    @override
    async def evolve_after_warmup(
        self, env: ExecutionEnvironment, ctx: SuiteRunContext
    ) -> None:
        """Run EvoScientist AutoSkills before materializing the delta image."""
        session_id = f"evolve-autoskills-r{ctx.repeat_index}-s{ctx.suite_index}"
        await run_autoskills_evolve(
            container=evoscientist_context(env.handle),
            workspace_dir=env.workspace_dir,
            session_id=session_id,
        )


__all__ = ["EvoScientistActiveEvolveAdapter"]
