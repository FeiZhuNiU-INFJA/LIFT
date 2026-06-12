"""群体记忆（External Memory）编排 Mixin。

提供 ``GroupMemoryAdapterMixin``：通过多重继承插入到具体 runtime adapter
（如 ``OpenClawAdapter``）之前，覆盖 LIFT 编排层方法，使 warmup 阶段可以
起 N 个并行容器、各跑各的题，evolve 产物落到外部群体记忆系统。

具体 runtime 集成示例见 ``MultiUserOpenClawAdapter``。
"""
