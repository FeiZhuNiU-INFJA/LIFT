"""GenericAgent + 主动进化 runtime adapter（``-r genericagent_active_evolve``）。

在标准 ``GenericAgentAdapter`` 基础上叠两层主动复盘钩子：

- 每题完成后（``evolve_after_task``）发一条 per-task 复盘 chat（B 粒度）；
- 全部 warmup 完成后（``evolve_after_warmup``）再发一条 suite 级总复盘
  chat（A 粒度），全新 session_id，与既有 work / judge GA 进程隔离。

两次复盘都让 GA 自己按 ``memory/memory_management_sop.md`` 决定写到哪一层
（L1 ``global_mem_insight.txt`` / L2 ``global_mem.txt`` / L3 ``memory/*.md``），
框架仅负责拉起复盘进程并等其落盘后再做 ``materialize_delta`` 的 ``docker commit``。
"""
