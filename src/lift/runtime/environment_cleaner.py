"""Docker 容器/镜像与 workspace 目录的幂等清理工具。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

from src.config import LOGGER

_SAFE_TAG_RE = re.compile(r"[^a-zA-Z0-9._-]")  # Docker tag 非法字符替换

# ``docker rm -f`` / ``docker rmi -f`` 在宿主机侧的 wall-clock 上限。
# 健康场景下两者通常 <1s 完成；超时多见于 daemon 抖动 / aufs busy / cgroup
# 卡顿。给一个保守的 30s 上限，避免某次卡死把整条 cleanup finally 链拖住，
# 让后续的 delta 镜像 rmi、其它 disposable 仍能继续执行；超时只是泄露一个
# 容器/镜像，不会影响 evaluation 主流程。
_DOCKER_CLEANUP_TIMEOUT_SECONDS = 30.0


async def _run_docker_cleanup_subprocess(
    cmd: list[str],
    *,
    timeout: float = _DOCKER_CLEANUP_TIMEOUT_SECONDS,
) -> tuple[int | None, str, bool]:
    """跑 docker 清理子进程，返回 ``(returncode, stderr, timed_out)``。

    超时会 ``kill`` 客户端进程并返回 ``timed_out=True``；不抛异常，让 finally
    链上的其它清理动作可以继续。stdout 对清理路径没有信息量，丢弃。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stderr_bytes = (await asyncio.wait_for(proc.communicate(), timeout=timeout))[1]
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            stderr_bytes = (await proc.communicate())[1]
        except Exception:  # noqa: BLE001 — 诊断态，wait 失败也不能拖垮主流程
            stderr_bytes = b""
        return (
            proc.returncode,
            stderr_bytes.decode("utf-8", errors="replace"),
            True,
        )
    return (
        proc.returncode,
        stderr_bytes.decode("utf-8", errors="replace"),
        False,
    )


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


def _is_missing_target(stderr_text: str) -> bool:
    """判断 docker rm/rmi 的 stderr 是否表示"目标本来就不存在"。

    这种情况是清理路径上的正常 no-op（例如 ``ContainerSession.start`` 启动前会
    预删同名容器），降到 debug 即可，避免误报 warning 噪音。
    """
    if not stderr_text:
        return False
    text = stderr_text.lower()
    return (
        "no such container" in text
        or "no such image" in text
        or "is not running" in text  # 罕见但语义同样无害
    )


class EnvironmentCleaner:
    """Docker 与文件系统的幂等清理 helper。"""

    async def remove_container(self, container_name: str) -> None:
        """``docker rm -f`` 删除容器，带 30s 超时。

        失败/超时不抛异常（cleanup 必须幂等且不能阻塞 finally 链），但失败和
        超时都会以 warning 级别落日志并附带 stderr 摘要——便于事后排查泄露的
        容器，避免被 debug 日志吞掉。"""
        if not container_name:
            return
        returncode, stderr_text, timed_out = await _run_docker_cleanup_subprocess(
            ["docker", "rm", "-f", container_name],
        )
        if timed_out:
            LOGGER.warning(
                "Container remove timed out after %.0fs, leaking %s; stderr=%r",
                _DOCKER_CLEANUP_TIMEOUT_SECONDS,
                container_name,
                stderr_text.strip()[:200],
            )
            return
        if returncode == 0:
            if _is_missing_target(stderr_text):
                # docker rm -f 对不存在的容器返回 rc=0 但 stderr 写 "No such container"。
                # 这是 ContainerSession.start 启动前预删的常态，降到 debug 减少噪音。
                LOGGER.debug(
                    "Container %s already absent (rm -f no-op): %s",
                    container_name,
                    stderr_text.strip(),
                )
                return
            LOGGER.info("Removed container %s", container_name)
            return
        if _is_missing_target(stderr_text):
            LOGGER.debug(
                "Container %s already absent (rm no-op): %s",
                container_name,
                stderr_text.strip(),
            )
            return
        LOGGER.warning(
            "Container remove failed for %s (rc=%s): %s",
            container_name,
            returncode,
            stderr_text.strip()[:500],
        )

    async def remove_image(self, image_tag: str, *, force: bool = True) -> None:
        """``docker rmi`` 删除镜像，带 30s 超时；失败/超时仅 warning，不抛异常。"""
        if not image_tag:
            return
        cmd = ["docker", "rmi"]
        if force:
            cmd.append("-f")
        cmd.append(image_tag)
        returncode, stderr_text, timed_out = await _run_docker_cleanup_subprocess(cmd)
        if timed_out:
            LOGGER.warning(
                "Image remove timed out after %.0fs, leaking %s; stderr=%r",
                _DOCKER_CLEANUP_TIMEOUT_SECONDS,
                image_tag,
                stderr_text.strip()[:200],
            )
            return
        if returncode == 0:
            if _is_missing_target(stderr_text):
                LOGGER.debug(
                    "Image %s already absent (rmi -f no-op): %s",
                    image_tag,
                    stderr_text.strip(),
                )
                return
            LOGGER.info("Removed image %s", image_tag)
            return
        if _is_missing_target(stderr_text):
            LOGGER.debug(
                "Image %s already absent (rmi no-op): %s",
                image_tag,
                stderr_text.strip(),
            )
            return
        LOGGER.warning(
            "Image remove failed for %s (rc=%s): %s",
            image_tag,
            returncode,
            stderr_text.strip()[:500],
        )

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
