"""warmup 容器 commit 为 delta 镜像的薄封装。"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence

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


def _path_matches_any(path: str, roots: Sequence[str]) -> bool:
    """判定 ``path`` 是否位于 ``roots`` 中任一目录下（前缀匹配，带路径分隔）。"""
    for root in roots:
        r = root.rstrip("/")
        if not r:
            continue
        if path == r or path.startswith(r + "/"):
            return True
    return False


def _summarize_diff(
    diff_output: str,
    *,
    include_paths: Sequence[str] | None = None,
) -> str:
    """把 ``docker diff`` 原始输出压成一行 log 摘要。

    - ``A/C/D`` 分类计数（新增 / 修改 / 删除）
    - 按前 3 层目录聚合出现次数最多的路径前缀，展示 top-N 缩略行
    - bind mount 目录（LIFT 用 ``/workspace``）天然不会出现在 upperdir diff 里，
      所以这份摘要就是"docker commit 会捕获的全部变更"
    - 当传入 ``include_paths`` 时，只统计位于这些目录下的条目（用于 evolve-only 视角）
    """
    counts: Counter[str] = Counter()
    prefixes: Counter[str] = Counter()
    for line in diff_output.splitlines():
        line = line.strip()
        if not line or len(line) < 3 or line[1] != " ":
            continue
        op, path = line[0], line[2:]
        if op not in ("A", "C", "D"):
            continue
        if include_paths is not None and not _path_matches_any(path, include_paths):
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
        if include_paths is not None:
            return f"no changes under evolve_paths={list(include_paths)}"
        return "no changes (empty upperdir — evolve produced nothing in container FS)"
    top = prefixes.most_common(_DIFF_SUMMARY_TOP_N)
    top_repr = ", ".join(f"{p} x{n}" for p, n in top)
    return f"+{added}A ~{changed}C -{deleted}D across {len(prefixes)} paths (top: {top_repr})"


async def _log_diff_preview(
    container_name: str,
    *,
    evolve_paths: Sequence[str] = (),
) -> None:
    """docker commit 之前打 diff 摘要，帮助定位 evolve 是否真的落进容器 FS。

    - 始终打一行 ``full`` 摘要（全部 upperdir 变更）
    - 如果 adapter 声明了 ``evolve_paths``，再打一行 ``evolve-only`` 摘要，只统计
      白名单目录下的变更；若该视角计数为 0，输出 WARNING —— 这是判断"本次 warmup
      是否真的产出了进化产物"的负向信号（``full`` 中的 pip / cache / temp 等副作用
      本来就不能作为进化的证据）
    """
    diff_output = await _docker_diff(container_name)
    if diff_output is None:
        LOGGER.info(
            "Delta preflight diff: unavailable (docker diff failed) for %s",
            container_name,
        )
        return
    LOGGER.info(
        "Delta preflight diff (full) [%s]: %s",
        container_name,
        _summarize_diff(diff_output),
    )
    if not evolve_paths:
        return
    evolve_summary = _summarize_diff(diff_output, include_paths=evolve_paths)
    # evolve-only 计数为 0 的判定：`_summarize_diff` 只有"无变更"分支会以 "no changes"
    # 开头（含 include_paths 场景），此时提升为 WARNING
    if evolve_summary.startswith("no changes"):
        LOGGER.warning(
            "Delta preflight diff (evolve-only) [%s]: %s — warmup produced no evolve artifacts",
            container_name,
            evolve_summary,
        )
    else:
        LOGGER.info(
            "Delta preflight diff (evolve-only) [%s]: %s",
            container_name,
            evolve_summary,
        )


async def commit_delta_image(
    container_name: str,
    image_tag: str,
    *,
    evolve_paths: Sequence[str] = (),
) -> str:
    """将 warmup 容器文件系统 commit 为 delta 镜像 tag（容器须仍在运行）。

    commit 之前先跑 ``docker diff`` 打一至两行摘要：``full`` 覆盖 upperdir 全集，
    若 adapter 通过 ``evolve_paths`` 声明了进化产物落地目录，则再补一行
    ``evolve-only``（不匹配任何白名单目录时 WARNING）。bind mount
    (``/workspace/task``) 的写入天然不出现在 upperdir diff 里 —— 如果 ``full``
    显示 ``no changes``，几乎可以断定进化产物落错了位置（见 skill §1.7/§6.5）。
    """
    await _log_diff_preview(container_name, evolve_paths=evolve_paths)
    cleaner = EnvironmentCleaner()
    return await cleaner.commit_container(container_name, image_tag)
