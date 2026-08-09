"""OpenClaw + agentmemory memory plugin 的**主动进化**变体
(``-r openclaw_with_agentmemory_active_evolve``)。

与被动的 ``openclaw_with_agentmemory`` 的关键区别见 ``adapter`` 模块 docstring：
warmup 容器点火 agentmemory 的 LLM provider + warmup 结束后显式触发一次
consolidate/reflect，把原始 observation 蒸馏成 semantic facts / higher-order
insights 后再 ``docker commit`` 进 delta 镜像。
"""
