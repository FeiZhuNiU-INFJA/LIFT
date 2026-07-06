"""运行状态可视化：内存事件总线 + 状态聚合器 + 终端 TUI + HTTP 仪表盘。

该包用于在 LIFT 评测运行时实时观测 repeat × suite × task × phase 四个维度的
执行状态，以及当前存活的容器。设计上完全可选：

- ``events``：模块级事件发射器。``LIFTPipeline`` 与 ``ContainerSession`` 无条件
  调用 ``emit_*``；未注册监听器时为零成本 no-op，因此默认运行行为不受影响。
- ``state``：``RunStateTracker``，把事件聚合成一棵预建的状态树（数量在 run 开始
  时即确定）。
- ``tui``：``StatusDashboard``，基于 ``rich.Live`` 在终端原地刷新状态树与容器列表。
- ``http_dashboard``：``HttpDashboard``，零依赖标准库实现，提供浏览器侧的实时仪表盘
  （``GET /``、``GET /snapshot``、``GET /events`` SSE 长连接）。

CLI 在 ``--tui`` 时启动终端 TUI，``--dashboard [HOST:]PORT`` 时启动
HTTP 仪表盘；两者各自独立，可单开也可同时开。事件总线 ``events.subscribe`` 支持
任意数量并行订阅者，未来如需再挂 WebSocket / 时间轴甘特图等可视化，只需再注册
一个监听器。
"""

from __future__ import annotations
