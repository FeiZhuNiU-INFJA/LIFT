from __future__ import annotations

from abc import ABC, abstractmethod


class Disposable(ABC):
    @abstractmethod
    async def cleanup(self) -> None: ...
