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

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

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
        except Exception:  # noqa: BLE001 — 可视化绝不能拖垮评测
            pass


# ---- 便捷发射函数（供 pipeline / container 调用） -------------------------


def emit_run_plan(run_id: str, repeats: int, suite_names: tuple[str, ...]) -> None:
    """广播一次 run 的整体计划（repeat 数 + suite 列表）。"""
    _emit(RunPlanEvent(run_id=run_id, repeats=repeats, suite_names=suite_names))


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
) -> None:
    """广播编排维度状态变更。"""
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
