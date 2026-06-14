"""Docker 容器/镜像与 workspace 目录的幂等清理工具。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from src.config import LOGGER

_SAFE_TAG_RE = re.compile(r"[^a-zA-Z0-9._-]")  # Docker tag 非法字符替换


def sanitize_image_tag(tag: str) -> str:
    """将字符串 sanitize 为 Docker 镜像 tag 安全字符集（最长 128）。

    非 ASCII 字符（如中文 suite 名）会被全部替换成 ``-``，不同中文名可能塌缩成
    相同串而导致 delta 镜像 tag 碰撞（A suite 的 delta 被 B suite 误用）。因此当
    sanitize 改写了原值时，追加原值的确定性短哈希以保证唯一且可复现。
    """
    safe = _SAFE_TAG_RE.sub("-", tag)
    if not tag.isascii():
        digest = hashlib.sha1(tag.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:119].strip('-')}-{digest}"
    return safe[:128]


def delta_image_tag(*, run_id: str, repeat_index: int, suite_name: str) -> str:
    """生成 warmup commit 后的 delta 镜像 tag（``evolve-eval-delta:...``）。"""
    safe_run = sanitize_image_tag(run_id)
    safe_suite = sanitize_image_tag(suite_name)
    # Docker allows only one ':' (repository:tag); use '-' inside the tag.
    tag = f"{safe_run}-r{repeat_index}-{safe_suite}"
    return f"evolve-eval-delta:{tag[:128]}"


class EnvironmentCleaner:
    """Docker 与文件系统的幂等清理 helper。"""

    async def remove_container(self, container_name: str) -> None:
        """``docker rm -f`` 删除容器（不存在或失败时仅 debug 日志）。"""
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
        """``docker rmi`` 删除镜像（不存在或失败时仅 debug 日志）。"""
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
        """``docker commit`` 将容器文件系统固化为 delta 镜像。"""
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
        """递归删除宿主机 workspace 目录（``rm -rf``）。"""
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
