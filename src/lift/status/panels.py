"""状态面板装配胶水：把 ``RunStateTracker`` + TUI / HTTP dashboard 串起来。

对外提供两个 context manager：

- :func:`status_dashboard` —— ``run_lift`` 主路径用：内部托管 tracker 的
  attach/detach 生命周期，没启用任何面板时为零成本 no-op。
- :func:`optional_status_panels` —— ``--evaluate-only`` 路径用：tracker 由
  调用方先 attach 再传入，因为 replay 阶段必须在 panel 之前发事件让 tracker
  订阅到。

``--status-viz`` 启动期间会临时摘掉 console 的 ``StreamHandler``（保留
``FileHandler``），避免日志冲掉 ``rich.Live`` 渲染区。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.lift.status.state import RunStateTracker


@contextmanager
def status_dashboard(
    *, viz_enabled: bool, http_endpoint: str | None
) -> Iterator["RunStateTracker | None"]:
    """启用状态面板：终端 TUI（``viz_enabled``）与 / 或 HTTP 仪表盘（``http_endpoint``）。

    两者共享一份 ``RunStateTracker``：tracker 仅在至少一种面板被启用时注册到事件
    总线，否则保持完全 no-op。yield 出 tracker（未启用时为 ``None``），调用方
    可以在 pipeline 之后把后处理结果通过 ``tracker.set_final_summary(...)``
    注入快照供 dashboard 展示。
    """
    if not viz_enabled and not http_endpoint:
        yield None
        return

    from src.lift.status.state import RunStateTracker

    tracker = RunStateTracker()
    tracker.attach()
    try:
        with optional_status_panels(
            tracker, viz_enabled=viz_enabled, http_endpoint=http_endpoint
        ):
            yield tracker
    finally:
        tracker.detach()


@contextmanager
def optional_status_panels(
    tracker: "RunStateTracker",
    *,
    viz_enabled: bool,
    http_endpoint: str | None,
) -> Iterator[None]:
    """在已有 ``tracker`` 之上启用 TUI / HTTP 面板（按需）。

    与 :func:`status_dashboard` 的区别：tracker 由调用方传入并管理 attach/detach
    生命周期。``--evaluate-only`` 路径下 tracker 必须先于 panels 启动以便 replay
    阶段事件能被订阅，因此走此函数。
    """
    # --status-viz: 摘 console 日志 + rich.Live 看板
    stream_handlers: list[logging.Handler] = []
    dashboard = None
    if viz_enabled:
        from src.lift.status.tui import StatusDashboard

        root_logger = logging.getLogger()
        stream_handlers = [
            h
            for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        for h in stream_handlers:
            root_logger.removeHandler(h)
        dashboard = StatusDashboard(tracker)
        dashboard.start()

    # --status-http: 后台线程 HTTP 服务器
    http_dashboard = None
    if http_endpoint:
        from src.lift.status.http_dashboard import HttpDashboard

        host, port = _parse_http_endpoint(http_endpoint)
        http_dashboard = HttpDashboard(tracker, host=host, port=port)
        http_dashboard.start()

    try:
        yield
    finally:
        if http_dashboard is not None:
            http_dashboard.stop()
        if dashboard is not None:
            dashboard.stop()
        for h in stream_handlers:
            logging.getLogger().addHandler(h)


def _parse_http_endpoint(endpoint: str) -> tuple[str, int]:
    """解析 ``--status-http`` 参数为 ``(host, port)``。

    - 纯数字：默认绑定 ``127.0.0.1``（仅本机访问）。
    - ``HOST:PORT``：按 host:port 解析，host 可以是 ``0.0.0.0`` / 域名 / IP。
    """
    if ":" in endpoint:
        host, _, port_str = endpoint.rpartition(":")
        host = host.strip() or "127.0.0.1"
    else:
        host = "127.0.0.1"
        port_str = endpoint
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --status-http endpoint {endpoint!r}; expected PORT or HOST:PORT"
        ) from exc
    return host, port
