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

        在 suite 内所有 hold-out 题完成后由 ``SuiteRunResources.cleanup()`` 调用，
        也可在 adapter 的 per-session ``finally`` 中调用。
        """
