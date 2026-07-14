"""warmup 容器 commit 为 delta 镜像的薄封装。"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from src.config import LOGGER
from src.lift.runtime.environment_cleaner import EnvironmentCleaner

# `docker diff` 摘要里挑几条最有信息量的路径展示，避免日志爆炸
_DIFF_SUMMARY_TOP_N = 8
# `docker diff` 30s 超时；健康容器 <1s，超时不阻塞 commit（仅跳过观测日志）
_DIFF_TIMEOUT_SECONDS = 30.0
# unlisted evolve path 建议：过滤明显的噪音顶层目录（前 3 层前缀），因为它们几乎
# 不可能是 evolve 产物。此表按"绝对不会是 evolve 产物"的保守原则维护；某个 runtime
# 若确实把 evolve 产物写在这些目录下，可以显式声明 evolve_paths 覆盖，此表只影响
# "建议"，不影响 evolve-only 摘要本身。
_NOISE_PATH_PREFIXES: tuple[str, ...] = (
    "/tmp",
    "/var/tmp",
    "/var/log",
    "/var/cache",
    "/var/lib/apt",
    "/var/lib/dpkg",
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/root/.cache",
    "/root/.npm",
    "/root/.pip",
    "/root/.local/share",
    "/root/.config",
    "/root/.bash_history",
    "/root/.python_history",
    "/root/.wget-hsts",
    "/usr/lib/node_modules",
    "/opt/openclaw/node_modules",
)
# 建议名单最多显示几条顶层目录（避免噪音）
_UNLISTED_SUGGESTIONS_TOP_N = 5


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


def _suggest_unlisted_evolve_paths(
    diff_output: str,
    *,
    declared_paths: Sequence[str],
) -> list[tuple[str, int]]:
    """从 diff 输出中挑出"疑似 evolve 产物但未在 evolve_paths 中声明"的顶层目录。

    启发式：把每条 ``A|C`` 变更（``D`` 表示删除，不像新增产物）压到前 3 层目录，
    然后剔除两类：
      - 已经命中 ``declared_paths`` 白名单的（前缀匹配）
      - 命中 ``_NOISE_PATH_PREFIXES`` 黑名单的（pip/cache/tmp/log 等）

    剩下的按出现次数排序返回 top-N，作为"你可能想把这些加入 evolve_paths"的建议。
    这是纯启发式提示，误报可能有，但对集成新 runtime 时"evolve_paths 声明错了"的
    场景很有价值——它把"用户没意识到 agent 把状态写在了别处"翻译成一行可见的
    log 提示。
    """
    prefixes: Counter[str] = Counter()
    for line in diff_output.splitlines():
        line = line.strip()
        if not line or len(line) < 3 or line[1] != " ":
            continue
        op, path = line[0], line[2:]
        if op not in ("A", "C"):
            continue
        if _path_matches_any(path, declared_paths):
            continue
        if _path_matches_any(path, _NOISE_PATH_PREFIXES):
            continue
        parts = [p for p in path.split("/") if p]
        prefix = "/" + "/".join(parts[:3]) if parts else path
        prefixes[prefix] += 1
    return prefixes.most_common(_UNLISTED_SUGGESTIONS_TOP_N)


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


def _dump_full_diff(
    diff_output: str,
    *,
    dump_path: Path,
    container_name: str,
) -> None:
    """把 ``docker diff`` 原始输出落盘到 ``dump_path``（best-effort，不抛异常）。

    落盘位置约定：``results/{run_id}/delta_diff_{container_name}.txt``——
    集成新 runtime 时可直接 ``grep -v /root/.cache`` 类似过滤看真实持久化路径，
    也可作为事后归档（delta 镜像清理后仍可回溯）。
    """
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(diff_output, encoding="utf-8")
        LOGGER.info(
            "Delta preflight diff (full dump) [%s] -> %s",
            container_name,
            dump_path,
        )
    except OSError as exc:
        LOGGER.debug(
            "Failed to dump docker diff for %s to %s: %r",
            container_name,
            dump_path,
            exc,
        )


async def _log_diff_preview(
    container_name: str,
    *,
    evolve_paths: Sequence[str] = (),
    dump_path: Path | None = None,
) -> None:
    """docker commit 之前打 diff 摘要，帮助定位 evolve 是否真的落进容器 FS。

    - 始终打一行 ``full`` 摘要（全部 upperdir 变更）
    - 如果 adapter 声明了 ``evolve_paths``，再打一行 ``evolve-only`` 摘要，只统计
      白名单目录下的变更；若该视角计数为 0，输出 WARNING —— 这是判断"本次 warmup
      是否真的产出了进化产物"的负向信号（``full`` 中的 pip / cache / temp 等副作用
      本来就不能作为进化的正向证据）
    - 当 evolve-only 计数为 0 且 full 中存在疑似 evolve 产物的未声明路径，追加一行
      INFO 列出候选（供开发者判断是否需要修正 ``evolve_paths`` 声明）
    - 如给出 ``dump_path``，把完整 ``docker diff`` 输出落盘，便于事后查阅任意深度的
      具体路径（log 摘要只按前 3 层目录聚合）
    """
    diff_output = await _docker_diff(container_name)
    if diff_output is None:
        LOGGER.info(
            "Delta preflight diff: unavailable (docker diff failed) for %s",
            container_name,
        )
        return
    if dump_path is not None:
        _dump_full_diff(diff_output, dump_path=dump_path, container_name=container_name)
    LOGGER.info(
        "Delta preflight diff (full) [%s]: %s",
        container_name,
        _summarize_diff(diff_output),
    )
    if not evolve_paths:
        return
    evolve_summary = _summarize_diff(diff_output, include_paths=evolve_paths)
    # evolve-only 计数为 0 的判定：`_summarize_diff` 只有"无变更"分支会以 "no changes"
    # 开头（含 include_paths 场景），此时提升为 WARNING 并追加 unlisted 候选建议
    if evolve_summary.startswith("no changes"):
        LOGGER.warning(
            "Delta preflight diff (evolve-only) [%s]: %s — warmup produced no evolve artifacts",
            container_name,
            evolve_summary,
        )
        suggestions = _suggest_unlisted_evolve_paths(
            diff_output, declared_paths=evolve_paths
        )
        if suggestions:
            top_repr = ", ".join(f"{p} x{n}" for p, n in suggestions)
            LOGGER.info(
                "Delta preflight diff (candidate unlisted evolve paths) [%s]: %s — "
                "若这些是真进化产物，请把顶层目录加入 adapter.evolve_paths 声明",
                container_name,
                top_repr,
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
    diff_dump_path: Path | None = None,
) -> str:
    """将 warmup 容器文件系统 commit 为 delta 镜像 tag（容器须仍在运行）。

    commit 之前先跑 ``docker diff`` 打摘要：``full`` 覆盖 upperdir 全集，若 adapter
    通过 ``evolve_paths`` 声明了进化产物落地目录，则再补一行 ``evolve-only``（不
    匹配任何白名单目录时 WARNING，并追加 unlisted 候选建议）。``diff_dump_path``
    给出时把完整 ``docker diff`` 原始输出落盘，便于集成/排错时从任意深度的路径反查
    真实持久化位置。bind mount (``/workspace/task``) 的写入天然不出现在 upperdir
    diff 里 —— 如果 ``full`` 显示 ``no changes``，几乎可以断定进化产物落错了位置。
    """
    await _log_diff_preview(
        container_name,
        evolve_paths=evolve_paths,
        dump_path=diff_dump_path,
    )
    cleaner = EnvironmentCleaner()
    return await cleaner.commit_container(container_name, image_tag)
