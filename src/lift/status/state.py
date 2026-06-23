"""状态聚合器：把事件折叠成一棵预建的执行状态树。

设计依据：一次 run 开始时，``repeat × suite`` 的骨架即可确定；每个 suite 真正
加载后其 ``warmup/holdout`` 题数也随即确定。因此 ``RunStateTracker`` 先按计划
预建节点（状态 ``pending``），运行时仅把对应节点翻成 ``running`` / ``done`` /
``failed``，并维护一份"当前存活容器"表。

线程安全：事件可能来自不同 asyncio 任务（并行 repeat/suite/task），所有变更都在
``self._lock`` 下进行；TUI 渲染线程读取时同样取锁拷贝快照。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from threading import Lock

from src.lift.status import events as ev

# 状态常量
PENDING = "pending"
RUNNING = "running"
RETRYING = "retrying"
DONE = "done"
FAILED = "failed"


@dataclass
class DialogueTurn:
    """单轮 work↔judge 对话快照（已脱离锁，纯展示用）。

    A 路径（运行期）由 ``DialogueTurnEvent`` 折叠而来，``latency_seconds`` 为 None；
    B 路径（后处理）从 Langfuse ``trace_chain`` 注入，``latency_seconds`` 有值、
    ``judge_*`` 留空（trace_chain 仅 work 侧）。
    """

    turn_index: int  # 1-based 轮序
    work_prompt: str
    work_result: str
    judge_success: bool
    judge_score: float
    judge_reason: str
    timestamp: float = 0.0
    latency_seconds: float | None = None


@dataclass
class PhaseNode:
    """holdout 单题下的一个 phase（baseline / evolved）。"""

    name: str  # baseline / evolved
    status: str = PENDING
    last_error: str | None = None  # status=failed 时的异常摘要 / judge 拒因
    # phase 完成时由 emit_stage(score=, success=) 写入，供 dashboard 展示分数 & KPI
    score: float | None = None
    success: bool | None = None
    # phase 完成时 emit_stage(turns=) 写入，供 dashboard "avg turns" KPI 聚合
    turns: int | None = None
    # phase 完成时 emit_stage(tool_calls=) 写入；adapter 自报 work agent tool 调用次数
    # （OpenClaw 走 trajectory.jsonl 计 toolCall block；其他 runtime 拿不到时为 None）
    tool_calls: int | None = None
    # 后处理回填（FinalSummary 注入）：完整轨迹 / token / latency 指标
    trajectory_score: float | None = None
    # work↔judge 完整对话记录（A 路径逐轮追加；B 路径后处理整体覆盖）
    dialogue: list[DialogueTurn] = field(default_factory=list)
    # 对话来源标记："runtime"（A 路径）/ "postprocess"（B 路径）/ None
    dialogue_source: str | None = None


@dataclass
class TaskNode:
    """一道 holdout 题，含 baseline / evolved 两个 phase。"""

    name: str
    status: str = PENDING
    phases: dict[str, PhaseNode] = field(default_factory=dict)
    last_error: str | None = None


@dataclass
class WarmupTaskNode:
    """一道 warmup 题（无 phase 概念）。"""

    name: str
    status: str = PENDING
    last_error: str | None = None


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
    last_error: str | None = None  # suite 级失败摘要（warmup 异常 / 重试后仍失败）


@dataclass
class RepeatNode:
    """一轮 repeat，含按 suite_index 索引的 suite 集合。"""

    index: int
    status: str = PENDING
    suites: dict[int, SuiteNode] = field(default_factory=dict)


@dataclass
class ErrorRecord:
    """最近的失败事件，按发生时间倒序展示。"""

    at: float
    kind: str  # suite / warmup / task / phase
    repeat_index: int
    suite_name: str | None
    task_name: str | None
    phase: str | None
    detail: str


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
    started_at: float = field(default_factory=time.time)


@dataclass
class FinalSummaryRow:
    """后处理 summary CSV 单行（per-suite / per-category / global）。"""

    scope: str  # suite | category | global
    label: str  # suite_path / category 名 / "global"
    task_count: int = 0
    task_count_aggregated: int = 0
    task_count_excluded: int = 0
    baseline_success_rate: float | None = None
    evolved_success_rate: float | None = None
    # mean_impr_* / mean_diff_* 动态列；key 为列名（如 mean_impr_content_score），
    # value 为 float 或 None（缺失值）。前端按需渲染。
    metrics: dict[str, float | None] = field(default_factory=dict)


@dataclass
class FinalSummary:
    """后处理跑完后的最终汇总，写入 RunSnapshot.final_summary 供前端展示。"""

    rows: list[FinalSummaryRow] = field(default_factory=list)
    # 后处理已写出的产物绝对路径，前端可生成下载链接
    artifact_paths: dict[str, str] = field(default_factory=dict)
    completed_at: float = 0.0


@dataclass
class RunSnapshot:
    """供 TUI 渲染的只读快照（已脱离锁）。"""

    run_id: str
    repeats: list[RepeatNode]
    containers: list[ContainerInfo]
    run_started_at: float = 0.0
    snapshot_at: float = 0.0
    # CLI / RunOptions 中的关键参数，供 TUI / HTTP dashboard 顶部展示
    params: list[tuple[str, str]] = field(default_factory=list)
    # 最近的失败事件（最新在前），便于 dashboard / TUI 一眼看到失败原因
    recent_errors: list[ErrorRecord] = field(default_factory=list)
    # 后处理完成后的汇总数据（B 路径），未完成时为 None
    final_summary: FinalSummary | None = None


DialogueBundle = dict[tuple[int, int, str, str], list[DialogueTurn]]
"""按 (repeat_index, suite_index, task_name, phase) 索引的对话集合（B 路径注入用）。"""


class RunStateTracker:
    """订阅事件总线，维护一棵线程安全的运行状态树。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._run_id: str = ""
        self._suite_names: tuple[str, ...] = ()
        self._repeats: dict[int, RepeatNode] = {}
        # 容器以 container_name 为键
        self._containers: dict[str, ContainerInfo] = {}
        self._run_started_at: float = 0.0
        self._params: tuple[tuple[str, str], ...] = ()
        # 最近 N 条失败事件（环形）；首条最新
        self._recent_errors: list[ErrorRecord] = []
        self._recent_errors_max: int = 50
        # 后处理结果（B 路径），由 ``set_final_summary`` 写入
        self._final_summary: FinalSummary | None = None

    # ---- 后处理钩子 ------------------------------------------------------

    def set_final_summary(self, summary: FinalSummary) -> None:
        """后处理 pipeline 完成后写入最终汇总，供 dashboard 展示。"""
        with self._lock:
            self._final_summary = summary

    def set_dialogue(self, bundle: DialogueBundle) -> None:
        """后处理完成后注入含完整对话的 dialogue，覆盖运行期文本版本（B 路径）。

        ``bundle`` 按 ``(repeat_index, suite_index, task_name, phase)`` 索引；定位失败的
        坐标静默跳过。注入后置 ``dialogue_source="postprocess"``，后续运行期
        ``DialogueTurnEvent`` 不再追加（见 ``_handle_dialogue_turn``）。
        """
        with self._lock:
            for (repeat_index, suite_index, task_name, phase), turns in bundle.items():
                repeat = self._repeats.get(repeat_index)
                if repeat is None:
                    continue
                suite = repeat.suites.get(suite_index)
                if suite is None:
                    continue
                tnode = suite.holdout_tasks.get(task_name)
                if tnode is None:
                    continue
                pnode = tnode.phases.get(phase)
                if pnode is None:
                    continue
                pnode.dialogue = [DialogueTurn(**asdict(t)) for t in turns]
                pnode.dialogue_source = "postprocess"

    def set_phase_tool_calls(
        self, bundle: dict[tuple[int, int, str, str], int]
    ) -> None:
        """后处理 backfill 完成后回写 ``phase.tool_calls``，供静态 dashboard 渲染。

        运行期 adapter 没 override ``count_tool_calls`` 时（GA / Hermes），
        ``phase.tool_calls`` 一直是 None；后处理通过 langfuse ``tool_observation_count``
        兜底拿到精确值，但只写进了 backfilled JSON。这里把同一份值推回 tracker，
        让 ``build_static_dashboard_html`` 导出的 dashboard tools 列填上数。
        """
        with self._lock:
            for (repeat_index, suite_index, task_name, phase), value in bundle.items():
                repeat = self._repeats.get(repeat_index)
                if repeat is None:
                    continue
                suite = repeat.suites.get(suite_index)
                if suite is None:
                    continue
                tnode = suite.holdout_tasks.get(task_name)
                if tnode is None:
                    continue
                pnode = tnode.phases.get(phase)
                if pnode is None:
                    continue
                pnode.tool_calls = value

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
        elif isinstance(event, ev.DialogueTurnEvent):
            self._handle_dialogue_turn(event)

    # ---- 计划（预建骨架） ------------------------------------------------

    def _handle_run_plan(self, e: ev.RunPlanEvent) -> None:
        with self._lock:
            self._run_id = e.run_id
            self._suite_names = e.suite_names
            self._params = e.params
            if self._run_started_at == 0.0:
                self._run_started_at = time.time()
            for r in range(e.repeats):
                repeat = self._repeats.setdefault(r, RepeatNode(index=r))
                for idx, name in enumerate(e.suite_names):
                    repeat.suites.setdefault(idx, SuiteNode(index=idx, name=name))

    def _handle_suite_plan(self, e: ev.SuitePlanEvent) -> None:
        with self._lock:
            suite = self._ensure_suite(e.repeat_index, e.suite_index, e.suite_name)
            # 只**补缺**节点，不覆盖已存在节点的 status/last_error
            # 否则：suite 重跑（队尾重跑）/ stage 事件先于 plan 到达时，
            # 已经 done 的题会被覆盖回 pending
            for n in e.warmup_task_names:
                suite.warmup_tasks.setdefault(n, WarmupTaskNode(name=n))
            for n in e.holdout_task_names:
                suite.holdout_tasks.setdefault(
                    n,
                    TaskNode(
                        name=n,
                        phases={
                            "baseline": PhaseNode("baseline"),
                            "evolved": PhaseNode("evolved"),
                        },
                    ),
                )
            suite.planned = True

    # ---- 状态翻转 --------------------------------------------------------

    def _handle_stage(self, e: ev.StageEvent) -> None:
        with self._lock:
            if e.kind == "repeat":
                repeat = self._repeats.setdefault(
                    e.repeat_index, RepeatNode(index=e.repeat_index)
                )
                repeat.status = e.status
                if e.status == FAILED:
                    self._record_error(e)
                return

            suite = None
            if e.suite_index is not None:
                suite = self._ensure_suite(
                    e.repeat_index, e.suite_index, e.suite_name or ""
                )
            elif e.suite_name:
                # 兜底：emit 端没传 suite_index 时（如 warmup_task 状态事件），
                # 在已有 repeat 的 suites 里按 name 查找
                repeat = self._repeats.get(e.repeat_index)
                if repeat is not None:
                    for s in repeat.suites.values():
                        if s.name == e.suite_name:
                            suite = s
                            break

            if e.kind == "suite" and suite is not None:
                suite.status = e.status
                if e.status in (RUNNING, DONE):
                    suite.last_error = None
                if e.status == DONE:
                    self._clear_errors(e)
                if e.status == FAILED:
                    suite.last_error = e.detail
                    self._record_error(e)
            elif e.kind == "warmup" and suite is not None:
                suite.warmup_status = e.status
                if e.status in (RUNNING, DONE):
                    suite.last_error = None
                if e.status == DONE:
                    self._clear_errors(e)
                elif e.status == RETRYING:
                    suite.last_error = e.detail
                if e.status == FAILED:
                    suite.last_error = e.detail
                    self._record_error(e)
            elif e.kind == "task" and suite is not None and e.task_name:
                node = suite.holdout_tasks.get(e.task_name)
                if node is None:
                    node = TaskNode(name=e.task_name)
                    suite.holdout_tasks[e.task_name] = node
                node.status = e.status
                if e.status in (RUNNING, DONE):
                    node.last_error = None
                if e.status == DONE:
                    self._clear_errors(e)
                if e.status == FAILED:
                    node.last_error = e.detail
                    # hold-out task fail 一定来自其下 phase fail（phase 那层已 record_error），
                    # 这里只更新 TaskNode.status/last_error，不再写入 recent_errors，避免
                    # dashboard / TUI 同一次失败重复显示 task + phase 两条。
            elif e.kind == "warmup_task" and suite is not None and e.task_name:
                wnode = suite.warmup_tasks.get(e.task_name)
                if wnode is None:
                    wnode = WarmupTaskNode(name=e.task_name)
                    suite.warmup_tasks[e.task_name] = wnode
                wnode.status = e.status
                if e.status in (RUNNING, DONE):
                    wnode.last_error = None
                if e.status == DONE:
                    self._clear_errors(e)
                elif e.status == RETRYING:
                    wnode.last_error = e.detail
                    if suite.warmup_status != FAILED:
                        suite.warmup_status = RETRYING
                        suite.last_error = e.detail
                if e.status == FAILED:
                    wnode.last_error = e.detail
                    suite.warmup_status = FAILED
                    suite.last_error = e.detail
                    self._record_error(e)
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
                # phase done 时把 judge 给出的分数 / 是否通过落到 PhaseNode，
                # 供 dashboard grid 色块叠分数 & KPI strip 聚合（A 路径）。
                if e.score is not None:
                    pnode.score = e.score
                if e.success is not None:
                    pnode.success = e.success
                if e.turns is not None:
                    pnode.turns = e.turns
                if e.tool_calls is not None:
                    pnode.tool_calls = e.tool_calls
                if e.status == RETRYING:
                    # 中间态：把首次错误摘要落到节点供 hover；不计入 recent_errors
                    pnode.last_error = e.detail
                elif e.status == FAILED:
                    pnode.last_error = e.detail
                    self._record_error(e)
                elif e.status == DONE:
                    # judge fail 时 detail 形如 "judge fail (score=0.42)"，落到节点
                    # 供 dashboard 展示；非 judge fail 的 done 通常 detail=None。
                    pnode.last_error = e.detail or None
                    self._clear_errors(e)
                elif e.status == RUNNING:
                    pnode.last_error = None

    def _record_error(self, e: ev.StageEvent) -> None:
        """把一条失败事件写入环形缓冲（调用方须持锁）。``detail`` 缺省时回退为 ``"failed"``。"""
        record = ErrorRecord(
            at=time.time(),
            kind=e.kind,
            repeat_index=e.repeat_index,
            suite_name=e.suite_name,
            task_name=e.task_name,
            phase=e.phase,
            detail=e.detail or "failed",
        )
        self._recent_errors.insert(0, record)
        if len(self._recent_errors) > self._recent_errors_max:
            del self._recent_errors[self._recent_errors_max :]

    def _clear_errors(self, e: ev.StageEvent) -> None:
        """最终成功后清理同坐标历史异常，避免 dashboard 展示已恢复的中间失败。"""
        self._recent_errors = [
            r
            for r in self._recent_errors
            if not (
                r.kind == e.kind
                and r.repeat_index == e.repeat_index
                and r.suite_name == e.suite_name
                and r.task_name == e.task_name
                and r.phase == e.phase
            )
        ]

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

    def _handle_dialogue_turn(self, e: ev.DialogueTurnEvent) -> None:
        """把一轮对话追加到对应 PhaseNode（A 路径运行期）。

        后处理已覆盖（``dialogue_source == "postprocess"``）则丢弃迟到的运行期事件；
        按 ``turn_index`` 单调追加，防同轮重试重复。
        """
        with self._lock:
            suite = self._ensure_suite(e.repeat_index, e.suite_index, e.suite_name)
            tnode = suite.holdout_tasks.setdefault(e.task_name, TaskNode(name=e.task_name))
            pnode = tnode.phases.setdefault(e.phase, PhaseNode(e.phase))
            if pnode.dialogue_source == "postprocess":
                return
            turn = DialogueTurn(
                turn_index=e.turn_index,
                work_prompt=e.work_prompt,
                work_result=e.work_result,
                judge_success=e.judge_success,
                judge_score=e.judge_score,
                judge_reason=e.judge_reason,
                timestamp=e.timestamp,
            )
            if not pnode.dialogue or pnode.dialogue[-1].turn_index < e.turn_index:
                pnode.dialogue.append(turn)
            if not pnode.dialogue_source:
                pnode.dialogue_source = "runtime"

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
                                k: WarmupTaskNode(
                                    name=w.name,
                                    status=w.status,
                                    last_error=w.last_error,
                                )
                                for k, w in s.warmup_tasks.items()
                            },
                            holdout_tasks={
                                k: TaskNode(
                                    name=t.name,
                                    status=t.status,
                                    phases={
                                        pk: PhaseNode(
                                            name=p.name,
                                            status=p.status,
                                            last_error=p.last_error,
                                            score=p.score,
                                            success=p.success,
                                            turns=p.turns,
                                            tool_calls=p.tool_calls,
                                            trajectory_score=p.trajectory_score,
                                            dialogue=[DialogueTurn(**asdict(d)) for d in p.dialogue],
                                            dialogue_source=p.dialogue_source,
                                        )
                                        for pk, p in t.phases.items()
                                    },
                                    last_error=t.last_error,
                                )
                                for k, t in s.holdout_tasks.items()
                            },
                            planned=s.planned,
                            last_error=s.last_error,
                        )
                        for i, s in r.suites.items()
                    },
                )
                for r in sorted(self._repeats.values(), key=lambda x: x.index)
            ]
            containers = list(self._containers.values())
            recent_errors = list(self._recent_errors)
            final_summary = self._final_summary
        return RunSnapshot(
            run_id=self._run_id,
            repeats=repeats,
            containers=containers,
            run_started_at=self._run_started_at,
            snapshot_at=time.time(),
            params=list(self._params),
            recent_errors=recent_errors,
            final_summary=final_summary,
        )
