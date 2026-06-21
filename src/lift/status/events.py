"""进程内状态事件总线（零依赖、未注册监听器时为 no-op）。

``LIFTPipeline`` 与 ``ContainerSession`` 在关键生命周期点调用本模块的 ``emit_*``
函数。若没有任何监听器（默认情况），这些调用直接返回，不产生任何开销，也不会
改变现有运行行为；仅当 ``--status-viz`` 注册了 ``RunStateTracker`` 后才会聚合。

事件分两类：
- 编排事件（run/repeat/suite/task/phase 的 start/end）：由 ``LIFTPipeline`` 发出，
  携带四维坐标，是状态树更新的来源。
- 容器事件（container started/stopped）：由 ``ContainerSession`` 发出，用于展示
  当前存活容器。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from src.config import LOGGER

# ---- 事件数据结构 ---------------------------------------------------------


@dataclass(frozen=True)
class RunPlanEvent:
    """run 开始时一次性广播执行计划，供监听器预建状态树。"""

    run_id: str
    repeats: int
    # 每个 suite 的静态信息：suite 文件名（去后缀）/ warmup 题数 / holdout 题名列表。
    # 注：实际题数要到 suite 真正加载时才知道，这里仅给出 suite 列表占位，
    # 题级骨架在 ``SuitePlanEvent`` 中补全。
    suite_names: tuple[str, ...] = ()
    # 关键运行参数（用于 dashboard / TUI 顶部展示，串联日志便于回查）。
    # 取自 CLI / RunOptions，键值对均序列化为字符串以便直接渲染。
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SuitePlanEvent:
    """单个 suite 的题级骨架（warmup/holdout 题在加载后才确定）。"""

    run_id: str
    repeat_index: int
    suite_index: int
    suite_name: str
    warmup_task_names: tuple[str, ...]
    holdout_task_names: tuple[str, ...]


@dataclass(frozen=True)
class StageEvent:
    """编排维度状态变更（repeat/suite/task/phase 的 start 或 end）。

    ``kind`` 取值：``repeat`` / ``suite`` / ``warmup`` / ``task`` / ``phase``。
    ``status`` 取值：``running`` / ``done`` / ``failed``。
    其余字段按维度选填，未用到的维度留 ``None``。

    ``score`` / ``success``: phase done 时携带 ``content_score`` 与 judge 是否通过，
    供 dashboard 实时展示 per-phase 分数与汇总 KPI（A 路径，运行期就有数据）。
    """

    kind: str
    status: str
    run_id: str
    repeat_index: int
    suite_index: int | None = None
    suite_name: str | None = None
    task_name: str | None = None
    # phase 维度：baseline / evolved
    phase: str | None = None
    # 任务/阶段成败的可选附加信息（如 judge 是否通过、错误摘要）
    detail: str | None = None
    # phase done 时 judge 给出的内容分（0–1）和是否通过
    score: float | None = None
    success: bool | None = None
    # phase done 时实际进行的 work↔judge 对话轮数（dashboard KPI 用）
    turns: int | None = None
    # phase done 时 work agent 工具调用总数（adapter 自报；OpenClaw 读 trajectory.jsonl，
    # 其他 runtime 拿不到时为 None，dashboard 显示 "—"）
    tool_calls: int | None = None


@dataclass(frozen=True)
class ContainerEvent:
    """容器生命周期事件（started / stopped）。"""

    status: str  # started / stopped
    container_name: str
    image: str
    # 关联坐标（尽力而为；ContainerSession 自身不持有，故可能为 None）
    run_id: str | None = None
    repeat_index: int | None = None
    suite_name: str | None = None
    task_name: str | None = None
    stage: str | None = None  # warmup / baseline / evolved


@dataclass(frozen=True)
class DialogueTurnEvent:
    """一轮 work↔judge 对话完成（A 路径运行期）。每轮一条，坐标精确到 phase。

    由 ``run_task`` 的 ``on_turn`` 回调经 ``_run_holdout`` 的 emitter 发出，
    驱动 dashboard 的"完整对话记录"视图。监听器把 turn 追加到对应 PhaseNode。
    """

    run_id: str
    repeat_index: int
    suite_index: int
    suite_name: str
    task_name: str
    phase: str  # baseline / evolved
    turn_index: int  # 1-based 轮序
    work_prompt: str
    work_result: str
    judge_success: bool
    judge_score: float
    judge_reason: str
    timestamp: float = field(default_factory=time.time)


Listener = Callable[[object], None]


@dataclass
class _Bus:
    """监听器注册表（线程安全；事件可能来自不同 asyncio 任务/线程）。"""

    listeners: list[Listener] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)


_BUS = _Bus()


def subscribe(listener: Listener) -> None:
    """注册一个事件监听器（接收任意上面定义的事件 dataclass）。"""
    with _BUS.lock:
        _BUS.listeners.append(listener)


def unsubscribe(listener: Listener) -> None:
    """注销监听器；未注册时静默忽略。"""
    with _BUS.lock:
        try:
            _BUS.listeners.remove(listener)
        except ValueError:
            pass


def clear_listeners() -> None:
    """清空所有监听器（主要用于测试隔离）。"""
    with _BUS.lock:
        _BUS.listeners.clear()


def _emit(event: object) -> None:
    """向所有监听器派发事件；无监听器时快速返回。监听器异常被吞掉，绝不影响主流程。"""
    with _BUS.lock:
        if not _BUS.listeners:
            return
        listeners = list(_BUS.listeners)
    for listener in listeners:
        try:
            listener(event)
        except Exception as exc:  # noqa: BLE001 — 可视化绝不能拖垮评测
            # 只记一行 warning 暴露根因（参数类型不匹配、字段缺失等），不向上抛
            LOGGER.warning(
                "status event listener %r raised on %r: %r",
                listener, type(event).__name__, exc,
            )


# ---- 便捷发射函数（供 pipeline / container 调用） -------------------------


def emit_run_plan(
    run_id: str,
    repeats: int,
    suite_names: tuple[str, ...],
    params: tuple[tuple[str, str], ...] = (),
) -> None:
    """广播一次 run 的整体计划（repeat 数 + suite 列表 + 关键参数）。"""
    _emit(
        RunPlanEvent(
            run_id=run_id,
            repeats=repeats,
            suite_names=suite_names,
            params=params,
        )
    )


def emit_suite_plan(
    *,
    run_id: str,
    repeat_index: int,
    suite_index: int,
    suite_name: str,
    warmup_task_names: tuple[str, ...],
    holdout_task_names: tuple[str, ...],
) -> None:
    """广播单个 suite 加载后的题级骨架。"""
    _emit(
        SuitePlanEvent(
            run_id=run_id,
            repeat_index=repeat_index,
            suite_index=suite_index,
            suite_name=suite_name,
            warmup_task_names=warmup_task_names,
            holdout_task_names=holdout_task_names,
        )
    )


def emit_stage(
    *,
    kind: str,
    status: str,
    run_id: str,
    repeat_index: int,
    suite_index: int | None = None,
    suite_name: str | None = None,
    task_name: str | None = None,
    phase: str | None = None,
    detail: str | None = None,
    score: float | None = None,
    success: bool | None = None,
    turns: int | None = None,
    tool_calls: int | None = None,
) -> None:
    """广播编排维度状态变更。

    ``detail``: 可选的人类可读摘要，``status="failed"`` 时建议带异常类型 + 简短信息
    （如 ``"RuntimeError: container ... is not running"``）；``status="done"`` 时
    可带语义标签（如 judge 评分），由监听器自行决定是否展示。

    ``score`` / ``success``: phase done 时上报 ``content_score`` 与是否 judge 通过，
    驱动 dashboard 的 per-phase 分数渲染与 KPI 聚合。
    ``turns``: phase done 时上报实际对话轮数，驱动 dashboard "avg turns" KPI。
    ``tool_calls``: phase done 时 adapter 自报 work agent tool 调用总次数；缺失时
    dashboard 显示 "—"，仅 OpenClaw 路径会真填。
    """
    _emit(
        StageEvent(
            kind=kind,
            status=status,
            run_id=run_id,
            repeat_index=repeat_index,
            suite_index=suite_index,
            suite_name=suite_name,
            task_name=task_name,
            phase=phase,
            detail=detail,
            score=score,
            success=success,
            turns=turns,
            tool_calls=tool_calls,
        )
    )


def emit_container(
    *,
    status: str,
    container_name: str,
    image: str,
    run_id: str | None = None,
    repeat_index: int | None = None,
    suite_name: str | None = None,
    task_name: str | None = None,
    stage: str | None = None,
) -> None:
    """广播容器 started / stopped 事件。"""
    _emit(
        ContainerEvent(
            status=status,
            container_name=container_name,
            image=image,
            run_id=run_id,
            repeat_index=repeat_index,
            suite_name=suite_name,
            task_name=task_name,
            stage=stage,
        )
    )


def emit_dialogue_turn(
    *,
    run_id: str,
    repeat_index: int,
    suite_index: int,
    suite_name: str,
    task_name: str,
    phase: str,
    turn_index: int,
    work_prompt: str,
    work_result: str,
    judge_success: bool,
    judge_score: float,
    judge_reason: str,
) -> None:
    """广播一轮 work↔judge 对话（运行期），供 dashboard 实时展示完整对话记录。

    坐标与 ``emit_stage`` 一致（repeat/suite/task/phase）；``turn_index`` 为 1-based
    轮序。``work_prompt`` / ``work_result`` / ``judge_reason`` 建议由调用方截断
    （见 ``adapters/base._truncate``）以控制 SSE / snapshot 体量。无监听器时为 no-op。
    """
    _emit(
        DialogueTurnEvent(
            run_id=run_id,
            repeat_index=repeat_index,
            suite_index=suite_index,
            suite_name=suite_name,
            task_name=task_name,
            phase=phase,
            turn_index=turn_index,
            work_prompt=work_prompt,
            work_result=work_result,
            judge_success=judge_success,
            judge_score=judge_score,
            judge_reason=judge_reason,
        )
    )
