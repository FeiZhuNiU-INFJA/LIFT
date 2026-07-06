"""HTTP 状态仪表盘：浏览器侧实时观察 LIFT 评测进度（零额外依赖，标准库实现）。

设计目标：在 ``--tui`` 终端 TUI 之外，再提供一个浏览器端的实时仪表盘，
解决以下场景：

- ``nohup`` / 远端机器跑评测时没有 tty 也想看实时状态
- 多人同时观察一次 run（团队会议 / 协同调试）

实现方式：

- 复用 ``src.lift.status.events`` 事件总线，注册一个监听器把事件追加到环形缓冲；
  同时持有一个 ``RunStateTracker``，用于响应 ``GET /snapshot`` 全量请求。
- 用标准库 ``http.server.ThreadingHTTPServer`` 起后台线程，零外部依赖。
- 路由：

  - ``GET /``：返回内嵌单文件 HTML（前端展示与 TUI 同等信息：Header/Repeats/
    Suites×Repeats 栅格/Containers）。
  - ``GET /snapshot``：返回 ``RunSnapshot`` 的 JSON，前端进入页面时一次性拉取。
  - ``GET /events``：Server-Sent Events 长连接，连接建立后先推一份 snapshot，
    然后随事件总线实时推送增量；客户端用 ``EventSource`` 接收并合并到本地状态。

线程安全：``RunStateTracker.snapshot()`` 已经在锁下深拷贝；事件 fan-out 由
``events`` 模块保证。
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from pathlib import Path

from src.lift.status import events as ev
from src.lift.status.state import RunSnapshot, RunStateTracker

LOGGER = logging.getLogger(__name__)

_DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"


def _load_index_html() -> str:
    """读取 dashboard.html 模板文件（模块加载时缓存）。"""
    return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")


_INDEX_HTML = _load_index_html()

_SSE_KEEPALIVE_SECONDS = 15.0  # 没有事件时定期发心跳，防止反向代理断连


# ---- 事件 / 快照 → JSON 的纯函数 ----------------------------------------


def _to_jsonable(obj: Any) -> Any:
    """递归把 dataclass / dict / list 转成 JSON 可序列化结构。"""
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _snapshot_payload(snapshot: RunSnapshot) -> dict[str, Any]:
    """把 ``RunSnapshot`` 序列化成前端易消费的 JSON 结构。"""
    return _to_jsonable(snapshot)


def _event_payload(event: object) -> dict[str, Any]:
    """把事件 dataclass 序列化为 ``{type, data}``。"""
    return {"type": type(event).__name__, "data": _to_jsonable(event)}


# ---- 静态 HTML 快照导出 --------------------------------------------------


# dashboard.html 的启动块用固定哨兵 __LIFT_STATIC_BOOT_START/END__ 包裹；静态导出时把
# 整块替换成"只读嵌入 snapshot"的启动分支。用哨兵而非整段字符串匹配，避免日后改启动
# 注释 / 重构 tick() 又把匹配打穿（之前 _STATIC_BOOT_PLACEHOLDER 就因此静默失效过）。
_STATIC_BOOT_RE = re.compile(
    r"// __LIFT_STATIC_BOOT_START__.*?// __LIFT_STATIC_BOOT_END__", re.S
)


_STATIC_BOOT_REPLACEMENT = """// 静态快照启动：使用嵌入的 INITIAL_SNAPSHOT，不发起任何网络请求。
snapshot = window.__INITIAL_SNAPSHOT__ || null;
const _conn = document.getElementById('conn');
if (_conn) {
  _conn.classList.remove('live');
  _conn.classList.add('dead');
  const lbl = _conn.querySelector('.conn-label');
  if (lbl) lbl.textContent = 'snapshot';
}
render();
// 不再发起 SSE 或 fetch；这是冻结的最终状态快照。"""


def build_static_dashboard_html(snapshot: RunSnapshot) -> str:
    """渲染一份脱机可看的 dashboard HTML，把当前 ``snapshot`` 作为初始数据嵌入。

    生成的 HTML 不依赖 ``/snapshot`` 与 ``/events`` 接口，可直接用浏览器打开
    （或拷贝到他人电脑）查看；所有交互（折叠 / 切换 / 过滤）仍然有效，但不会
    再有实时更新——SSE 与定时 refresh 已被禁用。
    """
    payload = json.dumps(_snapshot_payload(snapshot), ensure_ascii=False)
    # 把 ``</`` 转义，防止字符串里出现 ``</script>`` 提前结束嵌入脚本
    payload_safe = payload.replace("</", "<\\/")
    injected = (
        f"<script>window.__INITIAL_SNAPSHOT__ = {payload_safe};</script>\n</head>"
    )
    html = _INDEX_HTML.replace("</head>", injected, 1)
    html = _STATIC_BOOT_RE.sub(_STATIC_BOOT_REPLACEMENT, html)
    if "refreshSnapshot(true).then(connect)" in html:
        # 启动块没被换掉（哨兵被改坏 / 删除）→ 离线导出仍会走 live fetch + SSE，
        # file:// 打不开。直接检查 live 启动调用是否还在，比查哨兵更可靠：哨兵本身
        # 可能正是被改坏的那一端。
        LOGGER.warning(
            "static dashboard boot swap did not take effect (sentinel markers missing "
            "in dashboard.html); offline export will fall back to live fetch and render blank."
        )
    return html


def export_dashboard_snapshot(run_id: str, tracker: RunStateTracker) -> None:
    """把当前 tracker 快照渲染为静态 HTML 写到 ``results/<run_id>/dashboard.html``。"""
    from src.paths import results_run_dir

    out = results_run_dir(run_id) / "dashboard.html"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_static_dashboard_html(tracker.snapshot()), encoding="utf-8")
        LOGGER.info("Dashboard static snapshot: %s", out)
    except Exception:
        LOGGER.exception("Failed to export dashboard snapshot to %s", out)


# ---- SSE 客户端注册表 ----------------------------------------------------


class _SSEClient:
    """单个 SSE 客户端的事件队列。"""

    def __init__(self) -> None:
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=1024)


class _ClientRegistry:
    """SSE 客户端集中注册表（多线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[_SSEClient] = []

    def add(self) -> _SSEClient:
        c = _SSEClient()
        with self._lock:
            self._clients.append(c)
        return c

    def remove(self, c: _SSEClient) -> None:
        with self._lock:
            try:
                self._clients.remove(c)
            except ValueError:
                pass

    def broadcast(self, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c.queue.put_nowait(payload)
            except queue.Full:
                # 客户端落后太多：踢掉，让它重连后通过 /snapshot 重建状态
                try:
                    c.queue.put_nowait(None)
                except queue.Full:
                    pass


# ---- 主入口：HttpDashboard -----------------------------------------------


class HttpDashboard:
    """HTTP 仪表盘：后台线程跑 ``ThreadingHTTPServer`` + SSE 推送。

    使用方式（在 lift_main 里包一层 contextmanager）::

        dashboard = HttpDashboard(tracker, host="0.0.0.0", port=8765)
        dashboard.start()
        try:
            ...  # 运行 pipeline
        finally:
            dashboard.stop()
    """

    def __init__(
        self,
        tracker: RunStateTracker,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self._tracker = tracker
        self._host = host
        self._port = port
        self._registry = _ClientRegistry()
        self._server: ThreadingHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ---- 生命周期 ----

    def start(self) -> None:
        """注册事件订阅、启动后台 HTTP 线程。失败时静默降级（仅 warning）。"""
        try:
            self._server = ThreadingHTTPServer(
                (self._host, self._port), self._make_handler()
            )
        except OSError as exc:
            LOGGER.warning(
                "HttpDashboard cannot bind %s:%d (%s); dashboard disabled.",
                self._host,
                self._port,
                exc,
            )
            self._server = None
            return

        ev.subscribe(self._on_event)
        self._serve_thread = threading.Thread(
            target=self._server.serve_forever,
            name="lift-http-dashboard",
            daemon=True,
        )
        self._serve_thread.start()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="lift-http-dashboard-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()
        LOGGER.info(
            "HTTP status dashboard listening on http://%s:%d",
            self._host,
            self._port,
        )

    def stop(self) -> None:
        """注销事件订阅、关闭 HTTP 服务并等待线程退出。"""
        ev.unsubscribe(self._on_event)
        self._stop_event.set()
        # 通知所有 SSE 客户端结束
        self._registry.broadcast(_SENTINEL_DONE)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for t in (self._serve_thread, self._keepalive_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

    # ---- 事件 → SSE -----------------------------------------------------

    def _on_event(self, event: object) -> None:
        try:
            data = json.dumps(_event_payload(event), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            LOGGER.debug("HttpDashboard cannot serialize event: %s", exc)
            return
        self._registry.broadcast(_format_sse(data))

    def _keepalive_loop(self) -> None:
        """每 ``_SSE_KEEPALIVE_SECONDS`` 秒发一次心跳（注释行），防止反向代理断连。"""
        while not self._stop_event.wait(timeout=_SSE_KEEPALIVE_SECONDS):
            self._registry.broadcast(": keepalive\n\n")

    # ---- HTTP handler ---------------------------------------------------

    def _make_handler(self):
        dashboard = self
        registry = self._registry
        tracker = self._tracker

        class Handler(BaseHTTPRequestHandler):
            # 重写日志：默认会打到 stderr，干扰 TUI / 日志
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                LOGGER.debug("http_dashboard %s - %s", self.address_string(), format % args)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/" or path == "/index.html":
                    self._send_html(_INDEX_HTML)
                elif path == "/snapshot":
                    snapshot = tracker.snapshot()
                    body = json.dumps(_snapshot_payload(snapshot), ensure_ascii=False)
                    self._send_json(body)
                elif path == "/events":
                    self._serve_sse(registry, tracker)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "Not found")

            def _send_html(self, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def _serve_sse(
                self, registry: _ClientRegistry, tracker: RunStateTracker
            ) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")  # 关掉 nginx 缓冲
                self.end_headers()

                client = registry.add()
                # 连接建立后先推一份 snapshot，前端可立即重建状态
                try:
                    snap_payload = json.dumps(
                        {
                            "type": "Snapshot",
                            "data": _snapshot_payload(tracker.snapshot()),
                        },
                        ensure_ascii=False,
                    )
                    self.wfile.write(_format_sse(snap_payload).encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    registry.remove(client)
                    return

                try:
                    while not dashboard._stop_event.is_set():
                        try:
                            payload = client.queue.get(timeout=1.0)
                        except queue.Empty:
                            continue
                        if payload is None or payload == _SENTINEL_DONE:
                            break
                        try:
                            self.wfile.write(payload.encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                finally:
                    registry.remove(client)

        return Handler


_SENTINEL_DONE = "__done__"


def _format_sse(data: str) -> str:
    """SSE 协议格式化：``data:`` 行 + 空行结尾。多行 data 自动按行拆分。"""
    lines = data.splitlines() or [""]
    return "".join(f"data: {ln}\n" for ln in lines) + "\n"
