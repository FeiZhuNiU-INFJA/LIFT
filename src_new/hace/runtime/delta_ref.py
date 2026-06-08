from __future__ import annotations

from dataclasses import dataclass, field

from src_new.hace.runtime.environment_cleaner import EnvironmentCleaner


@dataclass
class DeltaRef:
    """Warmup-produced artifact as a committed Docker image."""

    image_tag: str
    source_container: str | None = None
    _cleaner: EnvironmentCleaner = field(default_factory=EnvironmentCleaner)
    _cleaned: bool = field(default=False, repr=False)

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        await self._cleaner.remove_image(self.image_tag)
        self._cleaned = True
