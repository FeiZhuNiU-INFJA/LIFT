"""状态聚合器：把事件折叠成一棵预建的执行状态树。

设计依据：一次 run 开始时，``repeat × suite`` 的骨架即可确定；每个 suite 真正
加载后其 ``warmup/holdout`` 题数也随即确定。因此 ``RunStateTracker`` 先按计划
预建节点（状态 ``pending``），运行时仅把对应节点翻成 ``running`` / ``done`` /
``failed``，并维护一份"当前存活容器"表。

线程安全：事件可能来自不同 asyncio 任务（并行 repeat/suite/task），所有变更都在
``self._lock`` 下进行；TUI 渲染线程读取时同样取锁拷贝快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from src.lift.status import events as ev

# 状态常量
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


@dataclass
class PhaseNode:
    """holdout 单题下的一个 phase（baseline / evolved）。"""

    name: str  # baseline / evolved
    status: str = PENDING


@dataclass
class TaskNode:
    """一道 holdout 题，含 baseline / evolved 两个 phase。"""

    name: str
    status: str = PENDING
    phases: dict[str, PhaseNode] = field(default_factory=dict)


@dataclass
class WarmupTaskNode:
    """一道 warmup 题（无 phase 概念）。"""

    name: str
    status: str = PENDING


@dataclass
class SuiteNode:
    """一个 suite：warmup 阶段 + holdout 题集合。"""

    index: int
    name: str
    status: str = PENDING
    warmup_status: str = PENDING
    warmup_tasks: dict[str, WarmupTaskNode] = field(default_factory=dict)
    holdout_tasks: dict[str, TaskNode] = field(default_factory=dict)
    # 题级骨架是否已由 SuitePlanEvent 填充
    planned: bool = False


@dataclass
class RepeatNode:
    """一轮 repeat，含按 suite_index 索引的 suite 集合。"""

    index: int
    status: str = PENDING
    suites: dict[int, SuiteNode] = field(default_factory=dict)


@dataclass
class ContainerInfo:
    """当前存活容器的展示信息。"""

    container_name: str
    image: str
    run_id: str | None = None
    repeat_index: int | None = None
    suite_name: str | None = None
    task_name: str | None = None
    stage: str | None = None


@dataclass
class RunSnapshot:
    """供 TUI 渲染的只读快照（已脱离锁）。"""

    run_id: str
    repeats: list[RepeatNode]
    containers: list[ContainerInfo]


class RunStateTracker:
    """订阅事件总线，维护一棵线程安全的运行状态树。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._run_id: str = ""
        self._suite_names: tuple[str, ...] = ()
        self._repeats: dict[int, RepeatNode] = {}
        # 容器以 container_name 为键
        self._containers: dict[str, ContainerInfo] = {}

    # ---- 订阅生命周期 ----------------------------------------------------

    def attach(self) -> None:
        """注册到事件总线。"""
        ev.subscribe(self._on_event)

    def detach(self) -> None:
        """从事件总线注销。"""
        ev.unsubscribe(self._on_event)

    # ---- 事件入口 --------------------------------------------------------

    def _on_event(self, event: object) -> None:
        if isinstance(event, ev.RunPlanEvent):
            self._handle_run_plan(event)
        elif isinstance(event, ev.SuitePlanEvent):
            self._handle_suite_plan(event)
        elif isinstance(event, ev.StageEvent):
            self._handle_stage(event)
        elif isinstance(event, ev.ContainerEvent):
            self._handle_container(event)

    # ---- 计划（预建骨架） ------------------------------------------------

    def _handle_run_plan(self, e: ev.RunPlanEvent) -> None:
        with self._lock:
            self._run_id = e.run_id
            self._suite_names = e.suite_names
            for r in range(e.repeats):
                repeat = self._repeats.setdefault(r, RepeatNode(index=r))
                for idx, name in enumerate(e.suite_names):
                    repeat.suites.setdefault(idx, SuiteNode(index=idx, name=name))

    def _handle_suite_plan(self, e: ev.SuitePlanEvent) -> None:
        with self._lock:
            suite = self._ensure_suite(e.repeat_index, e.suite_index, e.suite_name)
            suite.warmup_tasks = {
                n: WarmupTaskNode(name=n) for n in e.warmup_task_names
            }
            suite.holdout_tasks = {
                n: TaskNode(
                    name=n,
                    phases={
                        "baseline": PhaseNode("baseline"),
                        "evolved": PhaseNode("evolved"),
                    },
                )
                for n in e.holdout_task_names
            }
            suite.planned = True

    # ---- 状态翻转 --------------------------------------------------------

    def _handle_stage(self, e: ev.StageEvent) -> None:
        with self._lock:
            if e.kind == "repeat":
                repeat = self._repeats.setdefault(
                    e.repeat_index, RepeatNode(index=e.repeat_index)
                )
                repeat.status = e.status
                return

            suite = None
            if e.suite_index is not None:
                suite = self._ensure_suite(
                    e.repeat_index, e.suite_index, e.suite_name or ""
                )

            if e.kind == "suite" and suite is not None:
                suite.status = e.status
            elif e.kind == "warmup" and suite is not None:
                suite.warmup_status = e.status
            elif e.kind == "task" and suite is not None and e.task_name:
                node = suite.holdout_tasks.get(e.task_name)
                if node is None:
                    node = TaskNode(name=e.task_name)
                    suite.holdout_tasks[e.task_name] = node
                node.status = e.status
            elif e.kind == "warmup_task" and suite is not None and e.task_name:
                wnode = suite.warmup_tasks.get(e.task_name)
                if wnode is None:
                    wnode = WarmupTaskNode(name=e.task_name)
                    suite.warmup_tasks[e.task_name] = wnode
                wnode.status = e.status
            elif (
                e.kind == "phase"
                and suite is not None
                and e.task_name
                and e.phase
            ):
                tnode = suite.holdout_tasks.setdefault(
                    e.task_name, TaskNode(name=e.task_name)
                )
                pnode = tnode.phases.setdefault(e.phase, PhaseNode(e.phase))
                pnode.status = e.status

    # ---- 容器 ------------------------------------------------------------

    def _handle_container(self, e: ev.ContainerEvent) -> None:
        with self._lock:
            if e.status == "started":
                self._containers[e.container_name] = ContainerInfo(
                    container_name=e.container_name,
                    image=e.image,
                    run_id=e.run_id,
                    repeat_index=e.repeat_index,
                    suite_name=e.suite_name,
                    task_name=e.task_name,
                    stage=e.stage,
                )
            elif e.status == "stopped":
                self._containers.pop(e.container_name, None)

    # ---- 工具 ------------------------------------------------------------

    def _ensure_suite(
        self, repeat_index: int, suite_index: int, suite_name: str
    ) -> SuiteNode:
        """取或建指定 (repeat, suite) 节点（调用方须持锁）。"""
        repeat = self._repeats.setdefault(repeat_index, RepeatNode(index=repeat_index))
        suite = repeat.suites.get(suite_index)
        if suite is None:
            suite = SuiteNode(index=suite_index, name=suite_name)
            repeat.suites[suite_index] = suite
        elif suite_name and not suite.name:
            suite.name = suite_name
        return suite

    def snapshot(self) -> RunSnapshot:
        """取一份脱锁的深拷贝快照，供渲染线程安全读取。"""
        with self._lock:
            repeats = [
                RepeatNode(
                    index=r.index,
                    status=r.status,
                    suites={
                        i: SuiteNode(
                            index=s.index,
                            name=s.name,
                            status=s.status,
                            warmup_status=s.warmup_status,
                            warmup_tasks={
                                k: WarmupTaskNode(name=w.name, status=w.status)
                                for k, w in s.warmup_tasks.items()
                            },
                            holdout_tasks={
                                k: TaskNode(
                                    name=t.name,
                                    status=t.status,
                                    phases={
                                        pk: PhaseNode(name=p.name, status=p.status)
                                        for pk, p in t.phases.items()
                                    },
                                )
                                for k, t in s.holdout_tasks.items()
                            },
                            planned=s.planned,
                        )
                        for i, s in r.suites.items()
                    },
                )
                for r in sorted(self._repeats.values(), key=lambda x: x.index)
            ]
            containers = list(self._containers.values())
        return RunSnapshot(
            run_id=self._run_id, repeats=repeats, containers=containers
        )
