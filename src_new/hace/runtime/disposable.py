from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Disposable(Protocol):
    async def cleanup(self) -> None: ...
