"""回归：脱机静态 dashboard 导出必须把 live 启动块换成"只读嵌入 snapshot"启动。

之前 ``build_static_dashboard_html`` 用一整段字符串去匹配 dashboard.html 的启动块；
启动注释 / ``tick()`` 重构后匹配被打穿 → ``.replace`` 静默 no-op → 导出的 HTML 仍走
``fetch('/snapshot')`` + SSE，file:// 打开时 404 → 整页空白。这里锁定启动块确实被换掉，
并确保哨兵丢失时是 warn（而不是再次静默产出坏页）。
"""

from __future__ import annotations

import pytest

from src.lift.status import http_dashboard as hd
from src.lift.status.http_dashboard import build_static_dashboard_html
from src.lift.status.state import RunSnapshot


def _snap() -> RunSnapshot:
    return RunSnapshot(run_id="static-export-test", repeats=[], containers=[])


def test_static_export_swaps_live_boot_for_snapshot_boot() -> None:
    html = build_static_dashboard_html(_snap())

    # snapshot 被嵌入，run_id 进了 payload
    assert "window.__INITIAL_SNAPSHOT__" in html
    assert "static-export-test" in html

    # live 启动调用被移除（不再 fetch / SSE / 定时 render）
    assert "refreshSnapshot(true).then(connect)" not in html
    assert "setInterval(tick, 1000)" not in html

    # 换成了只读 snapshot 启动
    assert "snapshot = window.__INITIAL_SNAPSHOT__" in html

    # 哨兵标记不泄漏到产物里
    assert "__LIFT_STATIC_BOOT_START__" not in html
    assert "__LIFT_STATIC_BOOT_END__" not in html


def test_static_export_warns_when_boot_marker_missing(caplog: pytest.LogCaptureFixture) -> None:
    """哨兵丢失（dashboard.html 改坏标记）时必须 warn，而不是静默产出打不开的离线页。"""
    original = hd._INDEX_HTML
    hd._INDEX_HTML = original.replace("__LIFT_STATIC_BOOT_START__", "__BROKEN_MARKER__", 1)
    try:
        with caplog.at_level("WARNING", logger="src.lift.status.http_dashboard"):
            build_static_dashboard_html(_snap())
        assert "boot swap did not take effect" in caplog.text
    finally:
        hd._INDEX_HTML = original
