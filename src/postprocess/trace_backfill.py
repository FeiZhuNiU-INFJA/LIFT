"""Langfuse trace backfill (轨迹回填) for eval reports.

Loads traces from Langfuse, stitches them with framework pre-chat spans, and
writes ``PhaseRun.langfuse``.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langfuse import Langfuse, get_client

from src.config import LOGGER
from src.models import EvalReport, PhaseLangfuseBundle, PhaseRun
from src.postprocess.extract import AgentSource
from src.report.langfuse_trace_stitch import RunTraceIndex, stitch_phase_langfuse_traces


# Langfuse SDK 4.x 默认 5s 超时（langfuse/_client/client.py:279 读 LANGFUSE_TIMEOUT
# fallback 5），跑大规模 run 时 trace.list 翻页会偶发 ReadTimeout 把 backfill
# pipeline 整段炸掉。后处理对延迟容忍度高（一次性 + 可重跑），把 timeout 拉到
# 60s 是安全选择。
_BACKFILL_HTTP_TIMEOUT_SECONDS = 60

# phase 之间互相独立（各自 work/judge session id），用线程池并发拉 langfuse；
# 同步 SDK 底层是 httpx.Client，连接池天然共享，适合直接喂 ThreadPoolExecutor。
# 默认 8，可通过 ``EVAL_BACKFILL_WORKERS`` 调整。
_BACKFILL_WORKERS_ENV = "EVAL_BACKFILL_WORKERS"
_BACKFILL_WORKERS_DEFAULT = 8


def get_langfuse_client():
    """Return a configured Langfuse client with API access, or raise ``RuntimeError``.

    优先用显式 ``Langfuse(timeout=60)`` 构造，避开 SDK 默认 5s 超时。如果环境
    没配 LANGFUSE_PUBLIC_KEY 等凭据，构造会失败 / ``api`` 不可用，此时回退到
    ``get_client()``（保持原行为，让校验分支统一抛 RuntimeError）。
    """
    try:
        client = Langfuse(timeout=_BACKFILL_HTTP_TIMEOUT_SECONDS)
        if hasattr(client, "api"):
            return client
    except Exception:
        LOGGER.exception("Failed to construct Langfuse client with explicit timeout, falling back.")
    client = get_client()
    if not hasattr(client, "api"):
        raise RuntimeError(
            "Langfuse client is unavailable. Configure LANGFUSE_PUBLIC_KEY and "
            "related Langfuse settings before running trace backfill."
        )
    return client


def backfill_phase(
    client: Any,
    run_tag: str,
    phase: PhaseRun | None,
    agent_source: AgentSource = "openclaw",
    index: RunTraceIndex | None = None,
):
    """Attach stitched Langfuse traces to a single ``PhaseRun``, or return None if *phase* is None.

    单题 backfill 失败（langfuse 网络抖 / 数据格式异常）时返回原 ``phase``，
    避免一题崩掉整个 pipeline 让 dashboard 拿不到 ``tool_calls``。

    ``index`` 由上层预取的 per-run trace 索引；给出时 stitch 跳过 per-phase 4 路
    ``trace.list``，把 REST 数量从 O(phase × 4) 收敛到 O(1)（run 层级预取）。
    """
    if phase is None:
        return None
    try:
        # 按 PhaseRun 存的 session id 拉 Langfuse，合并 *_agent + openclaw-plugin
        bundle = stitch_phase_langfuse_traces(
            client,
            eval_run_tag=run_tag,
            work_session_id=phase.work_session_id,
            judge_session_id=phase.judge_session_id,
            agent_source=agent_source,
            index=index,
        )
    except Exception:
        LOGGER.exception(
            "Langfuse backfill failed for phase work_sid=%s judge_sid=%s — keeping phase unchanged.",
            phase.work_session_id, phase.judge_session_id,
        )
        return phase
    update: dict[str, Any] = {"langfuse": bundle}
    fallback = fallback_tool_calls(phase.tool_calls, bundle)
    if fallback is not None:
        update["tool_calls"] = fallback
    return phase.model_copy(update=update)


def fallback_tool_calls(
    current_tool_calls: int | None,
    bundle: PhaseLangfuseBundle,
) -> int | None:
    """从 langfuse work_analytics 兜底 ``PhaseRun.tool_calls``；不需要兜底时返回 None。

    Runtime 主链路没填 ``PhaseRun.tool_calls`` 时（如 GA / EvoScientist 没有本地
    trajectory.jsonl），按可信度取两条口径的 max：

    - ``tool_observation_count``：``type=TOOL`` observation 总数（overlay 每次工具调用
      挂 ``as_type='tool'`` 时才有值，如 GA overlay）。
    - ``tool_call_blocks``：plugin trace ``metadata.toolCallBlocks`` 累加（EvoScientist
      overlay 只挂 root + generation 时靠这个字段，来自 stream 里的 ``tool_call`` 事件）。

    OpenClaw 主链路读 trajectory.jsonl 拿到精确值后已经写入了 ``PhaseRun.tool_calls``，
    ``current_tool_calls is not None`` 直接短路，兜底不会覆盖。
    """
    if current_tool_calls is not None:
        return None
    if bundle.work_analytics is None:
        return None
    gs = bundle.work_analytics.global_stats
    candidate = max(gs.tool_observation_count, gs.tool_call_blocks)
    return candidate if candidate > 0 else None


def _resolve_backfill_workers() -> int:
    """读 ``EVAL_BACKFILL_WORKERS`` 环境变量；缺省或非法值回退默认值。"""
    raw = os.environ.get(_BACKFILL_WORKERS_ENV)
    if raw is None:
        return _BACKFILL_WORKERS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning(
            "Invalid %s=%r, falling back to default %d",
            _BACKFILL_WORKERS_ENV, raw, _BACKFILL_WORKERS_DEFAULT,
        )
        return _BACKFILL_WORKERS_DEFAULT
    return max(1, value)


def backfill_report(
    report: EvalReport,
    client: Any,
    agent_source: AgentSource = "openclaw",
) -> EvalReport:
    """Backfill Langfuse data for every baseline/evolved phase in *report*.

    phase 之间完全独立，用 ``ThreadPoolExecutor`` 并发拉 langfuse；单 phase 内
    仍保持顺序（``stitch_phase_langfuse_traces`` 不是线程安全的入口契约，且
    保留单 phase 内顺序便于排查）。``backfill_phase`` 自身已 try/except 兜底，
    这里不再额外吞异常。
    """
    run_tag = report.run_id
    # 收集所有 (phase, setter) 任务；setter 把 backfill 结果写回对应 task_run。
    jobs: list[tuple[PhaseRun, Any]] = []  # (phase, callback(new_phase))

    new_runs: list[Any] = []
    for repeat in report.runs:
        new_suites: list[Any] = []
        for suite in repeat.suites:
            new_tasks: list[Any] = []
            for task_run in suite.tasks:
                slot: dict[str, PhaseRun | None] = {
                    "baseline": task_run.baseline,
                    "evolved": task_run.evolved,
                }

                def _make_setter(local_slot: dict[str, PhaseRun | None], key: str):
                    def _setter(new_phase: PhaseRun | None) -> None:
                        local_slot[key] = new_phase
                    return _setter

                if task_run.baseline is not None:
                    jobs.append((task_run.baseline, _make_setter(slot, "baseline")))
                if task_run.evolved is not None:
                    jobs.append((task_run.evolved, _make_setter(slot, "evolved")))
                new_tasks.append((task_run, slot))
            new_suites.append((suite, new_tasks))
        new_runs.append((repeat, new_suites))

    workers = _resolve_backfill_workers()
    if jobs:
        # Per-run 预取：一次按 run_tag 拉齐所有 trace 的 metadata，phase 层不再触发
        # ``trace.list``（原路径每 phase 4 路 REST，56 phase = 224 次；改为 O(1) 分页）。
        # 构造失败时 fallback 到旧路径（index=None），保证 backfill 仍能推进。
        try:
            index = RunTraceIndex(client, run_tag=run_tag)
            LOGGER.info(
                "Pre-fetched %d trace(s) for run_tag=%s (per-phase trace.list disabled).",
                len(index), run_tag,
            )
        except Exception:
            LOGGER.exception(
                "Failed to pre-fetch run trace index for %s — falling back to per-phase trace.list.",
                run_tag,
            )
            index = None

        LOGGER.info(
            "Backfilling %d phase(s) with %d worker thread(s).", len(jobs), workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                (
                    pool.submit(backfill_phase, client, run_tag, phase, agent_source, index),
                    setter,
                )
                for phase, setter in jobs
            ]
            for future, setter in futures:
                setter(future.result())

    # 按原结构重新组装 EvalReport，保留 task / suite / repeat 顺序。
    rebuilt_runs = []
    for repeat, suites in new_runs:
        rebuilt_suites = []
        for suite, tasks in suites:
            rebuilt_tasks = [
                task_run.model_copy(
                    update={"baseline": slot["baseline"], "evolved": slot["evolved"]}
                )
                for task_run, slot in tasks
            ]
            rebuilt_suites.append(suite.model_copy(update={"tasks": rebuilt_tasks}))
        rebuilt_runs.append(repeat.model_copy(update={"suites": rebuilt_suites}))
    return report.model_copy(update={"runs": rebuilt_runs})
