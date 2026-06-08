from __future__ import annotations

from dataclasses import dataclass, field

from src_new.hace.runtime.delta_ref import DeltaRef
from src_new.hace.runtime.disposable import Disposable


@dataclass
class RepeatScope:
    """Owns all disposable resources for one repeat within a suite."""

    run_id: str
    repeat_index: int
    suite_name: str
    delta: DeltaRef | None = None
    sessions: list[Disposable] = field(default_factory=list)
    _cleaned: bool = field(default=False, repr=False)

    def track(self, session: Disposable) -> Disposable:
        self.sessions.append(session)
        return session

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        for session in reversed(self.sessions):
            await session.cleanup()
        if self.delta is not None:
            await self.delta.cleanup()
        self._cleaned = True
