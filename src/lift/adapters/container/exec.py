"""容器内 ``docker exec`` 通用封装（供各 agent runtime 复用）。"""

from __future__ import annotations

import asyncio
import subprocess

from src.config import LOGGER

# ``docker run -e KEY=VAL`` / ``docker exec -e KEY=VAL`` 写日志时需要脱敏的 key 子串
# （大小写不敏感，子串匹配——例如 ``ARK_API_KEY`` 命中 ``API_KEY``）。
_REDACT_KEY_SUBSTRINGS = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWORD",
)


def _should_redact_env(key: str) -> bool:
    """env key 是否属于敏感 secret（包含 TOKEN/SECRET/KEY/PASSWORD 子串）。"""
    upper = key.upper()
    return any(needle in upper for needle in _REDACT_KEY_SUBSTRINGS)


def redact_docker_argv(cmd: list[str]) -> str:
    """把 ``docker run/exec`` argv 中 ``-e KEY=VAL`` 的敏感值替换为 ``***``。

    扫描每个 ``-e`` 后续的 ``KEY=VAL`` 段：若 KEY 命中 ``_should_redact_env`` 则
    把值改为 ``***``；其它参数原样保留。仅用于日志输出，不影响实际 subprocess 调用。
    """
    redacted: list[str] = []
    i = 0
    while i < len(cmd):
        token = cmd[i]
        redacted.append(token)
        if token == "-e" and i + 1 < len(cmd):
            kv = cmd[i + 1]
            if "=" in kv:
                key, _, _ = kv.partition("=")
                if _should_redact_env(key):
                    redacted.append(f"{key}=***")
                else:
                    redacted.append(kv)
            else:
                redacted.append(kv)
            i += 2
            continue
        i += 1
    return " ".join(redacted)


def build_docker_exec_argv(
    container_name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    """拼装 ``docker exec [-e KEY=VAL ...] CONTAINER COMMAND...`` argv。"""
    cmd: list[str] = ["docker", "exec"]
    if env:
        for key, val in env.items():
            cmd.extend(["-e", f"{key}={val}"])  # 逐对 -e，兼容任意 runtime CLI 环境
    cmd.append(container_name)
    cmd.extend(command)
    return cmd


async def docker_exec_async(
    container_name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    label: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    """异步 ``docker exec``，非零退出码抛 ``RuntimeError``，返回 stdout 文本。

    ``timeout_seconds`` 是宿主机侧 wall-clock 上限：超时会 ``kill`` 掉
    docker exec 客户端进程并抛 ``RuntimeError`` 让上层走重试通道。容器内被卡住的
    openclaw-agent 子进程不会被 docker 端联动 kill（docker 行为如此），但下一次
    chat 走的是新 ``docker exec``，并不会被旧的 hang 阻塞。
    """
    cmd = build_docker_exec_argv(container_name, command, env=env)
    LOGGER.info("Container exec: %s", redact_docker_argv(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if timeout_seconds is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
    except asyncio.TimeoutError as exc:
        # 超时：kill 客户端进程，让端口/句柄释放；容器内子进程靠下一次 chat 自然替换
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 — 诊断态，wait 失败也不能拖垮主流程
            pass
        hint = label or " ".join(command)
        raise RuntimeError(
            f"docker exec timed out after {timeout_seconds:.0f}s for "
            f"{container_name} ({hint})"
        ) from exc
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        detail = stderr_text or stdout_text
        hint = label or " ".join(command)
        # 失败时抓容器最后 200 行 docker logs：plugin / gateway 自身的报错
        # （如 self-evolving plugin 18090 FastAPI 返回 400 的 body）通常被
        # ``curl -fsS`` 吞掉，但 plugin 进程的 stderr 都落在 docker logs 里。
        container_log = await capture_container_logs(container_name, tail=200)
        if container_log:
            LOGGER.error(
                "docker exec failed (%s); last container logs:\n%s",
                hint, container_log,
            )
        raise RuntimeError(
            f"docker exec failed for {container_name} ({hint}): {detail}"
        )
    return stdout_text


async def capture_container_logs(container_name: str, *, tail: int = 200) -> str:
    """抓容器最后 ``tail`` 行 ``docker logs``（best-effort，失败返回空串）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "--tail", str(tail), container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return ""
        text = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        return f"{text}\n{err}".strip()
    except Exception:  # noqa: BLE001 — 诊断功能不能拖垮主流程
        return ""


def docker_exec_sync(
    container_name: str,
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """同步 ``docker exec``（阻塞场景，如 initialize）。"""
    cmd = build_docker_exec_argv(container_name, command, env=env)
    LOGGER.info("Container exec (sync): %s", redact_docker_argv(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


async def docker_exec_shell_async(
    container_name: str,
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """异步 ``docker exec ... bash -lc <script>``。"""
    LOGGER.info("Container shell: %s", script[:120])
    return await docker_exec_async(
        container_name,
        ["bash", "-lc", script],
        env=env,
        label=f"shell: {script[:120]}",
    )

