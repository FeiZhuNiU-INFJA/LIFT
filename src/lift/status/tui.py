"""终端 TUI：基于 ``rich.Live`` 原地刷新运行状态树与存活容器列表。

``StatusDashboard`` 在后台线程驱动 ``rich.Live``，周期性从 ``RunStateTracker``
取快照并重绘。它与评测主协程并行运行，通过 ``start()`` / ``stop()`` 管理生命周期。

注意：启用 TUI 时，调用方（CLI）应把根 logger 的 stdout StreamHandler 移除，
只保留文件 handler，避免日志行冲掉 Live 渲染区。
"""

from __future__ import annotations

import threading
import time

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from src.lift.status.state import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    RunSnapshot,
    RunStateTracker,
)

# 状态 → (符号, 颜色)
_STATUS_STYLE = {
    PENDING: ("○", "grey50"),
    RUNNING: ("◔", "yellow"),
    DONE: ("●", "green"),
    FAILED: ("✗", "red"),
}


def _status_text(label: str, status: str) -> Text:
    """渲染 ``符号 label`` 的着色文本。"""
    symbol, color = _STATUS_STYLE.get(status, ("?", "white"))
    text = Text()
    text.append(f"{symbol} ", style=color)
    text.append(label, style=color if status in (RUNNING, FAILED) else "default")
    return text


def _phase_label(name: str, status: str) -> Text:
    text = Text()
    symbol, color = _STATUS_STYLE.get(status, ("?", "white"))
    text.append(f"{symbol} ", style=color)
    text.append(name, style=color)
    return text


def render_tree(snapshot: RunSnapshot) -> Tree:
    """把状态快照渲染成 rich.Tree。"""
    root = Tree(Text(f"run {snapshot.run_id}", style="bold cyan"))
    for repeat in snapshot.repeats:
        rnode = root.add(_status_text(f"repeat {repeat.index}", repeat.status))
        for suite in sorted(repeat.suites.values(), key=lambda s: s.index):
            snode = rnode.add(_status_text(f"suite[{suite.index}] {suite.name}", suite.status))
            # warmup 行（warmup 多题在单容器内执行，按阶段聚合展示）
            total = len(suite.warmup_tasks)
            warmup_label = f"warmup ({total} tasks)" if total else "warmup"
            snode.add(_status_text(warmup_label, suite.warmup_status))
            # holdout 题
            for task in suite.holdout_tasks.values():
                tnode = snode.add(_status_text(f"holdout {task.name}", task.status))
                for phase in ("baseline", "evolved"):
                    pnode = task.phases.get(phase)
                    if pnode is not None:
                        tnode.add(_phase_label(pnode.name, pnode.status))
    return root


def render_containers(snapshot: RunSnapshot) -> Panel:
    """把当前存活容器渲染成表格面板。"""
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("container", style="cyan", overflow="fold")
    table.add_column("stage", style="magenta")
    table.add_column("task", style="white")
    table.add_column("image", style="grey62", overflow="fold")
    for c in sorted(snapshot.containers, key=lambda x: x.container_name):
        table.add_row(
            c.container_name,
            c.stage or "-",
            c.task_name or "-",
            c.image,
        )
    title = f"alive containers: {len(snapshot.containers)}"
    return Panel(table, title=title, border_style="blue")


def render(snapshot: RunSnapshot) -> Group:
    """组合状态树与容器面板。"""
    return Group(
        Panel(render_tree(snapshot), title="LIFT status", border_style="green"),
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
