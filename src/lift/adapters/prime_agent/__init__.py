"""Prime Agent runtime adapter（LIFT CLI ``-r prime_agent``）。

Prime Agent（npm 包 ``prime-agent``, bin ``prime-agent``；仓库
``PrimeIntellect-ai/prime-agent``）是一个「自进化」coding harness：其
Continual Harness 把补充 prompt / memory / skill / sub-agent 规范存成可
CRUD 的持久状态，``/refine`` 会回看当轮轨迹、蒸馏出证据支撑的最小编辑写回。

本包提供 baseline（被动）adapter；``prime_agent_active_evolve`` 变体在
warmup 后显式触发 global ``/refine``。
"""
