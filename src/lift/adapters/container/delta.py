"""warmup 容器 commit 为 delta 镜像的薄封装。"""

from __future__ import annotations

import asyncio
from collections import Counter

from src.config import LOGGER
from src.lift.runtime.environment_cleaner import EnvironmentCleaner

# `docker diff` 摘要里挑几条最有信息量的路径展示，避免日志爆炸
_DIFF_SUMMARY_TOP_N = 8
# `docker diff` 30s 超时；健康容器 <1s，超时不阻塞 commit（仅跳过观测日志）
_DIFF_TIMEOUT_SECONDS = 30.0


async def _docker_diff(container_name: str) -> str | None:
    """``docker diff <container>`` 输出（每行 ``<A|C|D> <path>``），失败返回 None。

    仅观测用途；失败/超时都不抛异常，让 commit 主流程照跑。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "diff",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        LOGGER.debug("docker diff spawn failed for %s: %r", container_name, exc)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_DIFF_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        LOGGER.debug("docker diff timed out for %s", container_name)
        return None
    if proc.returncode != 0:
        LOGGER.debug(
            "docker diff rc=%s for %s: %s",
            proc.returncode,
            container_name,
            stderr.decode(errors="replace").strip()[:200],
        )
        return None
    return stdout.decode(errors="replace")


def _summarize_diff(diff_output: str) -> str:
    """把 ``docker diff`` 原始输出压成一行 log 摘要。

    - ``A/C/D`` 分类计数（新增 / 修改 / 删除）
    - 按前 3 层目录聚合出现次数最多的路径前缀，展示 top-N 缩略行
    - bind mount 目录（LIFT 用 ``/workspace``）天然不会出现在 upperdir diff 里，
      所以这份摘要就是"docker commit 会捕获的全部变更"
    """
    counts = Counter()
    prefixes: Counter[str] = Counter()
    for line in diff_output.splitlines():
        line = line.strip()
        if not line or len(line) < 3 or line[1] != " ":
            continue
        op, path = line[0], line[2:]
        if op not in ("A", "C", "D"):
            continue
        counts[op] += 1
        # 取前 3 层目录聚合（例如 /opt/GenericAgent/memory/foo.md → /opt/GenericAgent/memory）
        parts = [p for p in path.split("/") if p]
        prefix = "/" + "/".join(parts[:3]) if parts else path
        prefixes[prefix] += 1
    added = counts.get("A", 0)
    changed = counts.get("C", 0)
    deleted = counts.get("D", 0)
    total = added + changed + deleted
    if total == 0:
        return "no changes (empty upperdir — evolve produced nothing in container FS)"
    top = prefixes.most_common(_DIFF_SUMMARY_TOP_N)
    top_repr = ", ".join(f"{p} x{n}" for p, n in top)
    return f"+{added}A ~{changed}C -{deleted}D across {len(prefixes)} paths (top: {top_repr})"


async def _log_diff_preview(container_name: str) -> None:
    """docker commit 之前打一行 diff 摘要，帮助定位 evolve 是否真的落进容器 FS。"""
    diff_output = await _docker_diff(container_name)
    if diff_output is None:
        LOGGER.info(
            "Delta preflight diff: unavailable (docker diff failed) for %s",
            container_name,
        )
        return
    LOGGER.info("Delta preflight diff (%s): %s", container_name, _summarize_diff(diff_output))


async def commit_delta_image(container_name: str, image_tag: str) -> str:
    """将 warmup 容器文件系统 commit 为 delta 镜像 tag（容器须仍在运行）。

    commit 之前先跑 ``docker diff`` 打一行摘要日志（``+NA ~NC -ND``），让
    "evolve 是否真的落到容器 FS 层" 从 pipeline 日志第一时间可见。bind mount
    (``/workspace/task``) 的写入天然不出现在 upperdir diff 里 —— 如果这行日志
    显示 ``no changes``，几乎可以断定进化产物落错了位置（见 skill §1.7/§6.5）。
    """
    await _log_diff_preview(container_name)
    cleaner = EnvironmentCleaner()
    return await cleaner.commit_container(container_name, image_tag)
