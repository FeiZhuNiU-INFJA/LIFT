"""终端 TUI：紧凑栅格 + 双进度条 + 容器表，使用 ``rich.Live`` 原地刷新。

设计要点（取代单棵树形视图）：

- **顶部 Header**：展示 run_id、总进度条（按 (repeat × suite) 单元统计）、已用时间与
  粗略 ETA。
- **Repeat 进度条**：每个 repeat 一行进度条，反映 ``--max-parallel-repeats`` 并发
  下不同轮次的实际推进。
- **Suite × Repeat 栅格**：每个 suite 一行，按 repeat 横向展开 ``w b e`` 三列
  （warmup / baseline / evolved）状态符号；done 的 suite 自动折叠成 ``[+N done]``
  以保持单屏可读。
- **底部 Containers 表**：当前存活容器，按启动时长降序，最多展示 ``MAX_CONTAINERS``
  行，更多时折叠 "+N more"。

启用 TUI 时，调用方（CLI）应把根 logger 的 stdout StreamHandler 移除，只保留文件
handler，避免日志行冲掉 Live 渲染区。
"""

from __future__ import annotations

import threading
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from src.lift.status.state import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    ContainerInfo,
    RepeatNode,
    RunSnapshot,
    RunStateTracker,
    SuiteNode,
)

# 状态 → (符号, 颜色)。符号统一单字符宽度，对齐栅格列。
_STATUS_STYLE = {
    PENDING: ("·", "grey50"),
    RUNNING: ("◔", "yellow"),
    DONE: ("●", "green"),
    FAILED: ("✗", "red"),
}

# 容器表最多展示行数；超出折叠为 "+N more"
MAX_CONTAINERS = 10
# done 的 suite 折叠阈值：超过此数量的连续 done suite 折叠为 "+N done"
SUITE_FOLD_THRESHOLD = 3


def _status_cell(status: str) -> Text:
    """单格状态符号（warmup / baseline / evolved 之一）。"""
    symbol, color = _STATUS_STYLE.get(status, ("?", "white"))
    return Text(symbol, style=color)


def _phase_status(suite: SuiteNode, phase: str) -> str:
    """聚合 suite 下所有 holdout 题在指定 phase 的整体状态。

    - 任一 failed → failed
    - 全部 done → done
    - 任一 running → running
    - 否则 pending
    """
    if not suite.holdout_tasks:
        return PENDING
    statuses = [
        t.phases.get(phase, None).status if t.phases.get(phase) else PENDING
        for t in suite.holdout_tasks.values()
    ]
    if FAILED in statuses:
        return FAILED
    if all(s == DONE for s in statuses):
        return DONE
    if RUNNING in statuses:
        return RUNNING
    return PENDING


def _suite_overall_status(suite: SuiteNode) -> str:
    """聚合一个 suite 的总体状态：warmup + baseline + evolved 取最差。"""
    if suite.status in (DONE, FAILED):
        return suite.status
    parts = [
        suite.warmup_status,
        _phase_status(suite, "baseline"),
        _phase_status(suite, "evolved"),
    ]
    if FAILED in parts:
        return FAILED
    if all(p == DONE for p in parts):
        return DONE
    if RUNNING in parts or suite.status == RUNNING:
        return RUNNING
    return PENDING


def _repeat_progress(repeat: RepeatNode) -> tuple[int, int, int]:
    """统计 (done, running, total) for 一个 repeat 的 suites。"""
    total = len(repeat.suites)
    done = 0
    running = 0
    for s in repeat.suites.values():
        st = _suite_overall_status(s)
        if st == DONE:
            done += 1
        elif st == RUNNING:
            running += 1
    return done, running, total


def _format_duration(seconds: float) -> str:
    """格式化为 ``Hh Mm Ss`` 或 ``Mm Ss``。"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _truncate(value: str, width: int) -> str:
    """单字节截断（中文按两格估，简单按字符数兜底）。"""
    if len(value) <= width:
        return value
    return value[: max(1, width - 1)] + "…"


# ---- 渲染：Header ---------------------------------------------------------


def render_header(snapshot: RunSnapshot) -> Panel:
    """run 总进度 + 已用时间 + 粗略 ETA。"""
    total_units = sum(len(r.suites) for r in snapshot.repeats)
    done_units = 0
    running_units = 0
    for r in snapshot.repeats:
        d, run_, _ = _repeat_progress(r)
        done_units += d
        running_units += run_
    elapsed = (
        snapshot.snapshot_at - snapshot.run_started_at
        if snapshot.run_started_at > 0
        else 0.0
    )
    if done_units > 0 and total_units > 0:
        avg = elapsed / done_units
        remain_units = total_units - done_units
        eta = avg * remain_units
        eta_text = _format_duration(eta)
    else:
        eta_text = "?"

    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=None, complete_style="green", finished_style="green"),
        TextColumn("{task.completed}/{task.total} suites"),
        TextColumn("· running [yellow]{task.fields[running]}"),
        TextColumn("· elapsed [white]{task.fields[elapsed]}"),
        TextColumn("· ETA [white]{task.fields[eta]}"),
        expand=True,
    )
    progress.add_task(
        f"run {snapshot.run_id}",
        total=max(total_units, 1),
        completed=done_units,
        running=running_units,
        elapsed=_format_duration(elapsed),
        eta=eta_text,
    )
    return Panel(progress, border_style="cyan", padding=(0, 1))


# ---- 渲染：Repeat 进度条 ---------------------------------------------------


def render_repeat_bars(snapshot: RunSnapshot) -> Panel:
    """每个 repeat 一行进度条，体现并行推进情况。"""
    progress = Progress(
        TextColumn("[bold]repeat {task.fields[idx]:>2}"),
        BarColumn(bar_width=None, complete_style="green", finished_style="green"),
        TextColumn("{task.completed}/{task.total}"),
        TextColumn("· running [yellow]{task.fields[running]}"),
        expand=True,
    )
    for r in snapshot.repeats:
        done, running, total = _repeat_progress(r)
        progress.add_task(
            "",
            total=max(total, 1),
            completed=done,
            running=running,
            idx=r.index,
        )
    return Panel(progress, title="repeats", border_style="magenta", padding=(0, 1))


# ---- 渲染：Suite × Repeat 栅格 ---------------------------------------------


def render_suite_grid(snapshot: RunSnapshot) -> Panel:
    """suite 行 × repeat 列，每个单元 ``w b e`` 三个状态符号。

    完成的 suite 折叠为汇总行；其余按状态优先级排序（running > pending > done）。
    """
    repeats = snapshot.repeats
    if not repeats:
        return Panel(Text("(no repeats yet)", style="grey50"), border_style="green")

    # 构建 suite_index → list[(repeat_index, SuiteNode)]
    suite_indices = sorted({i for r in repeats for i in r.suites})
    suite_rows: list[tuple[int, str, dict[int, SuiteNode]]] = []
    for idx in suite_indices:
        cells_by_repeat: dict[int, SuiteNode] = {}
        suite_name = ""
        for r in repeats:
            s = r.suites.get(idx)
            if s is not None:
                cells_by_repeat[r.index] = s
                if not suite_name and s.name:
                    suite_name = s.name
        suite_rows.append((idx, suite_name or f"#{idx}", cells_by_repeat))

    # 排序：running > pending > done；同优先级保留原 suite_index
    def sort_key(row: tuple[int, str, dict[int, SuiteNode]]) -> tuple[int, int]:
        idx, _name, cells_by_repeat = row
        priorities = {RUNNING: 0, PENDING: 1, DONE: 2, FAILED: 0}
        worst = min(
            (priorities.get(_suite_overall_status(c), 1) for c in cells_by_repeat.values()),
            default=1,
        )
        return (worst, idx)

    suite_rows.sort(key=sort_key)

    table = Table(expand=True, show_edge=False, pad_edge=False, show_lines=False)
    table.add_column("suite", style="white", overflow="fold", min_width=20, ratio=3)
    for r in repeats:
        # 多行表头：第一行 repeat 名，第二行 w/b/e 子标头
        table.add_column(
            f"r{r.index}\n[grey62]w b e[/grey62]",
            justify="center",
            style="white",
            no_wrap=True,
            min_width=7,
        )

    folded_done_count = 0
    for _idx, name, cells_by_repeat in suite_rows:
        all_done = (
            len(cells_by_repeat) == len(repeats)
            and all(_suite_overall_status(c) == DONE for c in cells_by_repeat.values())
        )
        if all_done:
            folded_done_count += 1
            continue
        row: list[str | Text] = [_truncate(name, 32)]
        for r in repeats:
            cell = cells_by_repeat.get(r.index)
            if cell is None:
                row.append(Text("· · ·", style="grey50"))
                continue
            t = Text()
            t.append(_status_cell(cell.warmup_status))
            t.append(" ")
            t.append(_status_cell(_phase_status(cell, "baseline")))
            t.append(" ")
            t.append(_status_cell(_phase_status(cell, "evolved")))
            row.append(t)
        table.add_row(*row)

    if folded_done_count > 0:
        # 渲染折叠汇总行
        fold_row = [Text(f"+ {folded_done_count} suites done", style="green")]
        for _ in repeats:
            fold_row.append(Text("● ● ●", style="green"))
        table.add_row(*fold_row)

    legend = Text("legend: ", style="grey62")
    legend.append("w", style="cyan")
    legend.append("=warmup ", style="grey62")
    legend.append("b", style="cyan")
    legend.append("=baseline ", style="grey62")
    legend.append("e", style="cyan")
    legend.append("=evolved   ", style="grey62")
    for st in (PENDING, RUNNING, DONE, FAILED):
        sym, color = _STATUS_STYLE[st]
        legend.append(f"{sym} ", style=color)
        legend.append(f"{st} ", style="grey62")

    return Panel(
        Group(table, legend),
        title="suites × repeats",
        border_style="green",
        padding=(0, 1),
    )


# ---- 渲染：Containers 表 ---------------------------------------------------


def _container_sort_key(c: ContainerInfo) -> float:
    return c.started_at


def _short_container_name(name: str, max_len: int = 36) -> str:
    """容器名展示：保留尾部信息（suite/task/holdout/short_id），头部前缀截断为 ``…``。"""
    if len(name) <= max_len:
        return name
    keep = name[-(max_len - 1):]
    return "…" + keep


def render_containers(snapshot: RunSnapshot) -> Panel:
    """当前存活容器表：按启动时长降序，超过 MAX_CONTAINERS 折叠。"""
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("container", style="cyan", overflow="fold", ratio=3)
    table.add_column("repeat", style="magenta", justify="right", min_width=4)
    table.add_column("stage", style="magenta", min_width=8)
    table.add_column("suite", style="white", overflow="fold", ratio=2)
    table.add_column("task", style="white", overflow="fold", ratio=2)
    table.add_column("uptime", style="white", justify="right", min_width=6)

    sorted_containers = sorted(snapshot.containers, key=_container_sort_key)
    now = snapshot.snapshot_at or time.time()
    overflow = 0
    for i, c in enumerate(sorted_containers):
        if i >= MAX_CONTAINERS:
            overflow = len(sorted_containers) - MAX_CONTAINERS
            break
        uptime = now - (c.started_at or now)
        table.add_row(
            _short_container_name(c.container_name),
            "-" if c.repeat_index is None else str(c.repeat_index),
            c.stage or "-",
            c.suite_name or "-",
            c.task_name or "-",
            _format_duration(uptime),
        )
    if overflow > 0:
        table.add_row(
            Text(f"+ {overflow} more containers", style="grey62"),
            "", "", "", "", "",
        )

    title = f"alive containers: {len(snapshot.containers)}"
    return Panel(table, title=title, border_style="blue", padding=(0, 1))


# ---- 渲染：组合 -----------------------------------------------------------


def render(snapshot: RunSnapshot) -> Group:
    """组合 Header / Repeat / Grid / Containers。"""
    return Group(
        render_header(snapshot),
        render_repeat_bars(snapshot),
        render_suite_grid(snapshot),
        render_containers(snapshot),
    )


class StatusDashboard:
    """在后台线程驱动 rich.Live 的状态看板。"""

    def __init__(
        self,
        tracker: RunStateTracker,
        *,
        refresh_interval: float = 0.5,
        console: Console | None = None,
    ) -> None:
        self._tracker = tracker
        self._interval = refresh_interval
        self._console = console or Console(stderr=True)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台渲染线程。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="status-tui", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止渲染线程并做最后一次刷新。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        with Live(
            render(self._tracker.snapshot()),
            console=self._console,
            refresh_per_second=max(1, int(1 / self._interval)),
            transient=False,
        ) as live:
            while not self._stop.is_set():
                live.update(render(self._tracker.snapshot()))
                time.sleep(self._interval)
            # 退出前最终刷新一次，留下终态画面
            live.update(render(self._tracker.snapshot()))
