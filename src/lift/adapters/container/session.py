"""参数化 ``docker run`` 容器会话与 Disposable 生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from pypinyin import Style, lazy_pinyin

from src.config import LOGGER
from src.lift.adapters.container.exec import redact_docker_argv
from src.lift.runtime.disposable import Disposable
from src.lift.runtime.environment_cleaner import EnvironmentCleaner
from src.lift.status import events as status_events

ContainerHook = Callable[["ContainerSession"], Awaitable[None]]  # 容器启动/销毁前后钩子


def clip_name_segment(name: str, max_len: int = 20) -> str:
    """把单段名字（如 ``suite_name`` / ``task.name``）转 ASCII 拼音并截到 ``max_len``。

    用于拼装 ``instance_id`` 之前对各段做长度归一化，使容器名既携带可读信息
    又不超过 Docker 总长度上限。中文等非 ASCII 字符会先经 ``pypinyin.lazy_pinyin``
    转写为不带声调的拼音（用 ``-`` 连接），再 sanitize 到 Docker 容器名合法字符。
    空值返回占位符 ``x``，避免拼装出连续 ``--``。

    **防塌缩**：当原值「被拼音转写」或「sanitize 后超过 ``max_len`` 被截断」时，
    会在尾部追加 4 位原值 sha1（``-{4hex}``，从可读前缀里挤出预算），保证不同
    原值永不映射到同一段。短 ASCII 名（如 ``data_analysis``）保持原样，不引入
    后缀，避免破坏现有可读性。
    """
    if not name:
        return "x"
    if not name.isascii():
        translated = "-".join(lazy_pinyin(name, style=Style.NORMAL, errors="default"))
        needs_disambig = True
    else:
        translated = name
        needs_disambig = False
    out: list[str] = []
    for ch in translated:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        else:
            out.append("-")
    sanitized = "".join(out).strip("-")
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    if len(sanitized) > max_len:
        needs_disambig = True
    if needs_disambig:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:4]
        budget = max_len - 1 - len(digest)  # 留 `-` + 4 hex
        if budget < 1:
            return digest
        head = sanitized[:budget].rstrip("-") or "x"
        return f"{head}-{digest}"
    return sanitized.strip("-") or "x"


def sanitize_container_id(value: str) -> str:
    """将 instance id sanitize 为 Docker 容器名合法字符。

    Docker ``--name`` 仅接受 ``[a-zA-Z0-9._-]``。注意 ``str.isalnum()`` 对中文等
    非 ASCII 字符返回 ``True``，故须额外要求 ``ch.isascii()``，否则中文 suite 名
    会被原样保留导致 ``docker run`` 立即失败。本函数仅做"字符级"清洗：

    - 调用方应在拼装 ``instance_id`` 前用 ``clip_name_segment`` 对 ``suite_name`` /
      ``task.name`` 等可能很长或含中文的"段"做长度归一化（默认每段最多 20 字符），
      因此本函数不再对整体长度做截断，``holdout`` / ``short_id`` 等关键尾巴会原样保留。
    - 兜底场景：若调用方未做段级处理而直接传入含非 ASCII 字符的整串，仍会触发
      pypinyin 转写并追加原值的确定性 sha1 短哈希，避免不同同音中文塌缩成同名。
    - 上层 ``ContainerSession.start`` 中的 ``[:128]`` 仍提供整体兜底（Docker 上限 255）。
    """
    if not value.isascii():
        translated = "-".join(lazy_pinyin(value, style=Style.NORMAL, errors="default"))
    else:
        translated = value
    out: list[str] = []
    for ch in translated:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        else:
            out.append("-")
    sanitized = "".join(out).strip("-")
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    if not value.isascii():
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        sanitized = f"{sanitized}-{digest}" if sanitized else digest
    return sanitized or "session"


@dataclass
class ContainerSession(Disposable):
    """容器 runtime adapter 使用的 ephemeral Docker 容器会话。

    Attributes:
        instance_id: sanitize 后的逻辑实例 id。
        container_name: ``docker run --name`` 实际容器名。
        image: 启动时使用的镜像 tag。
        metadata: 运行时附加信息（如 OpenClaw gateway token）。
        published_ports: 容器内端口 → 宿主机真实端口（启动时由 docker inspect 回填）。
    """

    instance_id: str
    container_name: str
    image: str
    metadata: dict[str, Any] = field(default_factory=dict)
    published_ports: dict[int, int] = field(default_factory=dict)
    # 可视化用：由 instance_id 推断的阶段（warmup/baseline/evolved/holdout）与题名
    viz_stage: str | None = field(default=None, repr=False)
    viz_task: str | None = field(default=None, repr=False)
    viz_repeat_index: int | None = field(default=None, repr=False)
    viz_suite_name: str | None = field(default=None, repr=False)
    _cleaner: EnvironmentCleaner = field(default_factory=EnvironmentCleaner)  # 容器/镜像清理
    _pre_cleanup_hooks: list[ContainerHook] = field(default_factory=list, repr=False)  # rm 前钩子
    _cleaned: bool = field(default=False, repr=False)  # 幂等 cleanup 标记

    @override
    async def cleanup(self) -> None:
        """执行 pre-cleanup 钩子后 ``docker rm -f`` 删除容器。"""
        if self._cleaned:
            return
        for hook in self._pre_cleanup_hooks:
            try:
                await hook(self)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "Container pre-cleanup hook failed for %s: %s",
                    self.container_name,
                    exc,
                )
        await self._cleaner.remove_container(self.container_name)
        self._cleaned = True
        status_events.emit_container(
            status="stopped",
            container_name=self.container_name,
            image=self.image,
            task_name=self.viz_task,
            stage=self.viz_stage,
            repeat_index=self.viz_repeat_index,
            suite_name=self.viz_suite_name,
        )

    @classmethod
    async def start(
        cls,
        *,
        instance_id: str,
        container_name_prefix: str,
        image: str,
        entrypoint_cmd: list[str],
        port_mappings: list[tuple[int | None, int]],
        env_vars: dict[str, str],
        volume_binds: list[tuple[str, str, str]],
        env_file: Path | None = None,
        extra_docker_args: list[str] | None = None,
        readiness_check: ContainerHook | None = None,
        post_start_hooks: list[ContainerHook] | None = None,
        pre_cleanup_hooks: list[ContainerHook] | None = None,
        metadata: dict[str, Any] | None = None,
        viz_repeat_index: int | None = None,
        viz_suite_name: str | None = None,
    ) -> ContainerSession:
        """组装 ``docker run -d`` 命令并启动容器，可选 readiness 与 post-start 钩子。

        ``port_mappings`` 中 ``host_port`` 为 ``None`` 时由 Docker 在临时端口段
        随机分配（生成 ``-p <container_port>``，避免确定性 hash 端口的碰撞与占用
        冲突）。容器启动后通过 ``docker inspect`` 读取真实端口，回填到
        ``session.published_ports``（key 为容器内端口）。
        """
        safe_id = sanitize_container_id(instance_id)
        container_name = f"{container_name_prefix}-{safe_id}"[:128]

        # 同名容器可能来自中断的上次 run
        await EnvironmentCleaner().remove_container(container_name)

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--add-host",
            "host.docker.internal:host-gateway",  # 容器内 Langfuse / 模型 API 访问宿主机
            "-v",
            "/tmp:/tmp",  # OpenClaw / 插件临时文件
        ]
        if extra_docker_args:
            cmd.extend(extra_docker_args)
        for host_port, container_port in port_mappings:
            if host_port is None or host_port == 0:
                cmd.extend(["-p", str(container_port)])  # docker 自选空闲宿主机端口
            else:
                cmd.extend(["-p", f"{host_port}:{container_port}"])
        if env_file is not None and env_file.is_file():
            cmd.extend(["--env-file", str(env_file.resolve())])
        for key, val in env_vars.items():
            cmd.extend(["-e", f"{key}={val}"])
        for host_path, container_path, mode in volume_binds:
            cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
        cmd.append(image)
        cmd.extend(entrypoint_cmd)

        LOGGER.info("Starting container session: %s", redact_docker_argv(cmd))
        stderr_text = await _docker_run(cmd)
        if stderr_text is not None and "is already in use" in stderr_text:
            # 上一次（被中断/OOM）的同名容器残留，且启动前的预删恰逢 daemon 抖动失败。
            # 容器名是确定性的（warmup 无 short_id），重跑必撞——强制再删一次后重试。
            LOGGER.warning(
                "Container name %s still in use; force-removing stale container and retrying run",
                container_name,
            )
            await EnvironmentCleaner().remove_container(container_name)
            stderr_text = await _docker_run(cmd)
        if stderr_text is not None:
            raise RuntimeError(f"docker run failed: {stderr_text}")

        published = await _resolve_published_ports(
            container_name,
            [container_port for _, container_port in port_mappings],
        )

        stage, task, inferred_repeat, inferred_suite = _infer_stage_task(instance_id)
        # 显式参数优先于从 instance_id 反解（中文 suite 名经拼音转写后无法还原原文）
        repeat_index = (
            viz_repeat_index if viz_repeat_index is not None else inferred_repeat
        )
        suite_name = viz_suite_name if viz_suite_name is not None else inferred_suite
        session = cls(
            instance_id=safe_id,
            container_name=container_name,
            image=image,
            metadata=dict(metadata or {}),
            published_ports=published,
            viz_stage=stage,
            viz_task=task,
            viz_repeat_index=repeat_index,
            viz_suite_name=suite_name,
            _pre_cleanup_hooks=list(pre_cleanup_hooks or []),
        )
        status_events.emit_container(
            status="started",
            container_name=container_name,
            image=image,
            task_name=task,
            stage=stage,
            repeat_index=repeat_index,
            suite_name=suite_name,
        )
        if readiness_check is not None:
            await readiness_check(session)  # 如 OpenClaw gateway curl
        for hook in post_start_hooks or []:
            await hook(session)  # readiness 通过后再做 seed / attestations 清理
        return session


def _infer_stage_task(
    instance_id: str,
) -> tuple[str | None, str | None, int | None, str | None]:
    """从 instance_id 命名约定推断展示用的阶段、题名、repeat 序号、suite 名（尽力而为）。

    约定（见 container/adapter.py、group_memory/mixin.py）：
    - warmup 单容器：``...-r{N}-{suite}-warmup``
    - warmup 多容器：``...-r{N}-{suite}-warmup-{task}-{short_id}``
    - holdout 每题：``...-r{N}-{suite}-{task}-holdout-{baseline|evolved}-{short_id}``
    无法解析的字段返回 ``None``，不影响功能。

    注意 suite 段已经过 ``clip_name_segment`` 转拼音 / 截断，调用方若有原始 suite_name
    应通过 ``ContainerSession.start(viz_suite_name=...)`` 显式传入；本函数仅作兜底。
    """
    parts = instance_id.split("-")
    repeat_idx: int | None = None
    suite: str | None = None
    # 找 ``r{N}`` 段；其后紧跟的是 suite 段
    for i, p in enumerate(parts):
        if len(p) >= 2 and p[0] == "r" and p[1:].isdigit():
            repeat_idx = int(p[1:])
            if i + 1 < len(parts):
                suite = parts[i + 1]
            break
    if "holdout" in parts:
        idx = parts.index("holdout")
        task = parts[idx - 1] if idx > 0 else None
        load_state = parts[idx + 1] if idx + 1 < len(parts) else None
        if load_state in ("baseline", "evolved"):
            return f"holdout/{load_state}", task, repeat_idx, suite
        return "holdout", task, repeat_idx, suite
    if "warmup" in parts:
        idx = parts.index("warmup")
        task = parts[idx + 1] if idx + 1 < len(parts) else None
        return "warmup", task, repeat_idx, suite
    return None, None, repeat_idx, suite


async def _docker_run(cmd: list[str]) -> str | None:
    """运行 ``docker run`` argv；成功返回 ``None``，失败返回 stderr 文本。"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        return None
    return stderr.decode(errors="replace")


async def _resolve_published_ports(
    container_name: str, container_ports: list[int]
) -> dict[int, int]:
    """读 ``docker inspect`` 拿到容器内端口对应的真实宿主机端口。"""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "inspect",
        "--format",
        "{{json .NetworkSettings.Ports}}",
        container_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker inspect failed for {container_name}: "
            f"{stderr.decode(errors='replace')}"
        )
    raw = stdout.decode().strip()
    ports_map = json.loads(raw) if raw and raw != "null" else {}
    resolved: dict[int, int] = {}
    for cont in container_ports:
        bindings = ports_map.get(f"{cont}/tcp") or []
        if not bindings:
            raise RuntimeError(
                f"docker inspect: no host binding for {cont}/tcp on {container_name}"
            )
        resolved[cont] = int(bindings[0]["HostPort"])
    return resolved
