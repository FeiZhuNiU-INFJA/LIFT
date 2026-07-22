"""Disposable 抽象：LIFT 阶段/suite 结束后须显式释放的资源。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Disposable(ABC):
    """LIFT 某阶段或 suite 评测结束后须显式释放的资源。

    实现方包括 Docker 容器（``ContainerSession``）和 commit 出的 delta 镜像
    （``DeltaRef``）。``SuiteRunResources.track()`` 收集 disposable，
    ``SuiteRunResources.cleanup()`` 按逆序释放。
    """

    @abstractmethod
    async def cleanup(self) -> None:
        """幂等地释放底层资源。

        必须可重复调用（后续调用应为 no-op）。
        典型操作：停止/删除容器、``docker rmi`` delta 镜像、
        将 bind mount 目录 chown 回宿主机用户。

        在 suite 内所有 holdout 题完成后由 ``SuiteRunResources.cleanup()`` 调用，
        也可在 adapter 的 per-session ``finally`` 中调用。
        """


class CompositeDisposable(Disposable):
    """聚合多个 ``Disposable``，一次 ``cleanup`` 逆序释放全部。

    用于"work + judge 分容器"的 ``ExecutionEnvironment``：``disposable`` 聚合 work
    与 judge 两个容器会话，使上层现有的 ``resources.track(env.disposable)`` /
    ``env.disposable.cleanup()`` 无需改动即可覆盖两个容器。逆序释放对齐单个
    ``SuiteRunResources.cleanup()`` 的语义；单个成员失败不阻断其余成员的清理。
    """

    def __init__(self, members: list[Disposable]) -> None:
        self._members = list(members)
        self._done = False

    async def cleanup(self) -> None:
        if self._done:
            return
        self._done = True
        errors: list[BaseException] = []
        for member in reversed(self._members):
            try:
                await member.cleanup()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise errors[0]

