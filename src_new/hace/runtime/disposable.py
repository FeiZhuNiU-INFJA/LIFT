from __future__ import annotations

from abc import ABC, abstractmethod


class Disposable(ABC):
    """Resource that must be explicitly released after a HACE phase or repeat.

    Implementors include Docker containers (``ContainerSession``) and committed
    delta images (``DeltaRef``). ``RepeatScope.track()`` collects disposables
    and ``RepeatScope.cleanup()`` disposes them in reverse order.
    """

    @abstractmethod
    async def cleanup(self) -> None:
        """Release underlying resources idempotently.

        Must be safe to call more than once (subsequent calls are no-ops).
        Typical work: stop/remove containers, ``docker rmi`` delta images,
        reclaim bind-mount ownership on the host.

        Called by ``RepeatScope.cleanup()`` after all hold-out tasks for a
        suite complete, or from adapter ``finally`` blocks per session.
        """
