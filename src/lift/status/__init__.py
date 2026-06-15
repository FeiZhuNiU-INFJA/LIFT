"""运行状态可视化：内存事件总线 + 状态聚合器 + 终端 TUI。

该包用于在 LIFT 评测运行时实时观测 repeat × suite × task × phase 四个维度的
执行状态，以及当前存活的容器。设计上完全可选：

- ``events``：模块级事件发射器。``LIFTPipeline`` 与 ``ContainerSession`` 无条件
  调用 ``emit_*``；未注册监听器时为零成本 no-op，因此默认运行行为不受影响。
- ``state``：``RunStateTracker``，把事件聚合成一棵预建的状态树（数量在 run 开始
  时即确定）。
- ``tui``：``StatusDashboard``，基于 ``rich.Live`` 在终端原地刷新状态树与容器列表。

仅当 CLI 传入 ``--status-viz`` 时才注册监听器并启动 TUI。

扩展点（预留，未实现）：HTTP / WebSocket Dashboard
====================================================

事件总线 ``events.subscribe(listener)`` 支持任意数量的并行监听器，因此 TUI 之外
还可以再挂一个 HTTP 仪表盘订阅者，把同一份事件流通过 SSE / WebSocket 推到浏览器，
解决以下 TUI 解决不了的痛点：

- ``nohup`` / 远端机器跑评测时，没有 tty 也想看实时状态
- 多人同时观察一次 run（团队会议 / 协同调试）
- 时间轴 / 甘特图 / suite 详情下钻等富交互

实现轮廓（约 250 行；零额外依赖，纯 Python ``http.server`` 即可）：

1. 新建 ``src/lift/status/http_dashboard.py``，定义 ``HttpDashboard`` 类：
   - ``start(host, port)`` 起后台线程跑 ``ThreadingHTTPServer``。
   - ``__init__`` 内 ``ev.subscribe(self._on_event)``，把事件追加到内存环形缓冲。
   - 路由：``GET /`` 返回内嵌单文件 HTML；``GET /events`` 返回 SSE 事件流；
     ``GET /snapshot`` 返回 ``RunStateTracker.snapshot()`` 的 JSON。
2. ``src/cli/lift_main.py`` 增加 ``--status-http [HOST:]PORT``；与 ``--status-viz``
   独立，可分别开启或同时开启。
3. 不需要新增依赖；如以后想换 FastAPI/uvicorn 也只是替换该模块的内部实现。
4. ``ContainerInfo.started_at`` / ``RunSnapshot.run_started_at`` 字段已经具备，
   足以渲染时间轴。
"""

from __future__ import annotations
