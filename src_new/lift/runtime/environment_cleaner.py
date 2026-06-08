from __future__ import annotations

import asyncio
import re
from pathlib import Path

from src_new.config import LOGGER

_SAFE_TAG_RE = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize_image_tag(tag: str) -> str:
    return _SAFE_TAG_RE.sub("-", tag)[:128]


def delta_image_tag(*, run_id: str, repeat_index: int, suite_name: str) -> str:
    safe_run = sanitize_image_tag(run_id)
    safe_suite = sanitize_image_tag(suite_name)
    # Docker allows only one ':' (repository:tag); use '-' inside the tag.
    tag = f"{safe_run}-r{repeat_index}-{safe_suite}"
    return f"evolve-eval-delta:{tag[:128]}"


class EnvironmentCleaner:
    """Docker and filesystem cleanup helpers (idempotent)."""

    async def remove_container(self, container_name: str) -> None:
        if not container_name:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0:
            LOGGER.info("Removed container %s", container_name)
        else:
            LOGGER.debug("Container remove skipped or failed: %s", container_name)

    async def remove_image(self, image_tag: str, *, force: bool = True) -> None:
        if not image_tag:
            return
        cmd = ["docker", "rmi"]
        if force:
            cmd.append("-f")
        cmd.append(image_tag)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0:
            LOGGER.info("Removed image %s", image_tag)
        else:
            LOGGER.debug("Image remove skipped or failed: %s", image_tag)

    async def commit_container(self, container_name: str, image_tag: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "commit",
            container_name,
            image_tag,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"docker commit failed for {container_name}: "
                f"{stderr.decode(errors='replace')}"
            )
        image_id = stdout.decode().strip()
        LOGGER.info("Committed %s -> %s (%s)", container_name, image_tag, image_id)
        return image_tag

    async def remove_workspace(self, path: Path) -> None:
        if not path.exists():
            return
        proc = await asyncio.create_subprocess_exec(
            "rm",
            "-rf",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
