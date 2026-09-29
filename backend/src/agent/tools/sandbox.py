"""Sandbox executor for agent-created tools.

**这不是安全沙箱，必须按「受限子进程」理解**（诚实的边界声明，2026-09-15）：

* 默认执行器是同一个用户权限下的子进程：没有凭据注入、临时工作目录、超时、
  输出截断、剥离环境变量 —— 这些是**降险措施**，不是隔离；
* 因此声明为 PURE 的能力只是「策略承诺」，不是「拿不到文件系统/网络」。
  一个谎报能力的工具仍然能读写用户能读写的任何东西（实测过：声明 PURE 的
  工具仍可读取用户目录）；
* Docker 存在时才可能获得真正的隔离，运行期探测——**命令行在 PATH 里且守护进程
  应答**才算可用（见 `docker_daemon_ready`）：只装了 Docker Desktop 没启动时不算，
  否则每次工具调用都会选到 docker 再以 `docker exit code 125` 失败；
* 探测不到容器隔离时不会假装有：高风险能力直接拒绝执行，不做静默降级（见
  tools/policy.py 的能力分级与 tools/lifecycle.py 的审批流程）。`auto` 在「容器
  起不来」时改走下面的受限子进程，显式 `executor="docker"` 不回退；
* 由此得到的实际结论：**生成工具的执行必须由用户批准**，能力指纹变化后必须
  重新批准（`services/app.py::_restore_tools` 会跳过指纹不匹配的旧授权）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from agent.tools.policy import CapabilityLevel, ToolExecutionPolicy

DEFAULT_TIMEOUT_SECONDS = 10.0

DOCKER_BINARY = "docker"
# 探测只是「能不能用容器」的前置询问，不该让工具调用长时间挂住。
DOCKER_PROBE_TIMEOUT_SECONDS = 5.0


async def docker_daemon_ready(
    timeout_seconds: float = DOCKER_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """docker 真的可用吗 = 命令行在 PATH 里 **且** 守护进程应答。

    只看 `shutil.which("docker")` 会把「装了 Docker Desktop 但没启动」判成可用
    （客户端在 PATH 里，守护进程没起），于是每次工具调用都选 docker、再以
    `docker exit code 125` 失败。这里多问一次守护进程：能报出服务端版本才算数。

    每次都重新探测、不缓存：装没装、Docker Desktop 起没起，用户随时会变；一次
    调用多一条本地命令的代价，换「判断始终是当下的」。
    """
    if shutil.which(DOCKER_BINARY) is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            DOCKER_BINARY, "version", "--format", "{{.Server.Version}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        # 同受限子进程执行器：wait_for 只取消了读取，进程得自己终止。
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        return False
    except OSError:
        # 命令行存在却起不来（路径损坏、权限不对）
        return False
    return process.returncode == 0 and bool(stdout.strip())


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    value: dict[str, Any] | None
    stdout: str
    stderr: str
    error: str | None = None
    # docker 自己没把容器跑起来（不是容器里的工具失败了）。只有这种情况下
    # 才允许回退 —— 见 SandboxExecutor.execute。
    launch_failed: bool = False


class SandboxExecutor:
    def __init__(
        self,
        executor: str = "auto",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.executor = executor
        self.timeout_seconds = timeout_seconds

    async def effective_executor(self) -> str:
        """实际会用的执行器。

        auto 下「有 docker 命令行」不算可用，要守护进程应答（见 docker_daemon_ready）。
        """
        if self.executor != "auto":
            return self.executor
        return "docker" if await docker_daemon_ready() else "subprocess"

    async def execute(
        self,
        code: str,
        arguments: dict[str, Any],
        extra_env: dict[str, str] | None = None,
        policy: ToolExecutionPolicy | None = None,
    ) -> SandboxResult:
        policy = policy or ToolExecutionPolicy()

        if self.executor == "docker":
            # 显式指定 docker：不回退。回退等于把「容器隔离」静默降成受限子进程，
            # 那是安全等级的降级，只能由用户改配置来做。
            if not await docker_daemon_ready():
                return SandboxResult(
                    ok=False,
                    value=None,
                    stdout="",
                    stderr="",
                    error=(
                        "docker 不可用：命令行在 PATH 里，但守护进程没有应答"
                        "（Docker Desktop 没启动？）；已拒绝执行。"
                    ),
                )
            return await self._execute_docker(code, arguments, policy)

        if self.executor == "auto" and await docker_daemon_ready():
            result = await self._execute_docker(code, arguments, policy)
            if not result.launch_failed:
                return result
            # 容器根本没起来（守护进程中途掉了 / 镜像拉不下来）：工具代码一行都没
            # 执行过，所以按「没有容器隔离」改走受限子进程，而不是把整次调用判死。
            # 退出码来自工具自己时不是 launch_failed，那种情况不许重跑。

        # restricted subprocess 不是安全沙箱：高风险能力若无法可靠隔离，
        # 拒绝执行（或需经过 Trusted 显式批准），绝不静默降级安全等级。
        if policy.is_high_risk() and policy.level != CapabilityLevel.TRUSTED:
            return SandboxResult(
                ok=False,
                value=None,
                stdout="",
                stderr="",
                error=(
                    "该工具申请了需要隔离的能力（联网/文件/进程/凭据），"
                    "但当前没有可用的容器隔离；已拒绝执行。"
                ),
            )
        return await self._execute_subprocess(code, arguments, extra_env or {}, policy)

    # -- subprocess executor ----------------------------------------------

    async def _execute_subprocess(
        self,
        code: str,
        arguments: dict[str, Any],
        extra_env: dict[str, str] | None = None,
        policy: ToolExecutionPolicy | None = None,
    ) -> SandboxResult:
        script = (
            "import json, sys\n"
            f"CODE = {code!r}\n"
            f"ARGS = {arguments!r}\n"
            "namespace = {}\n"
            "exec(compile(CODE, '<tool>', 'exec'), namespace)\n"
            "result = namespace['run'](**ARGS)\n"
            "print(json.dumps(result, ensure_ascii=False))\n"
        )
        # ignore_cleanup_errors：Windows 上被终止的子进程可能短暂占住作为 cwd 的
        # 临时目录，清理失败不应该让工具执行以异常收场（错误信息本身已经返回）。
        with tempfile.TemporaryDirectory(
            prefix="sa-tool-", ignore_cleanup_errors=True
        ) as tmp:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "TEMP": os.environ.get("TEMP", tmp),
                "TMP": os.environ.get("TMP", tmp),
                "PYTHONIOENCODING": "utf-8",
            }
            if extra_env:
                env.update(extra_env)
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    script,
                    cwd=tmp,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                # 必须真正终止子进程：wait_for 只取消了读取，进程会继续运行并
                # 持有临时目录（资源泄漏 + 后续清理失败）。
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
                return SandboxResult(
                    ok=False, value=None, stdout="", stderr="",
                    error=f"timeout after {self.timeout_seconds}s",
                )
            out_text = stdout.decode("utf-8", errors="replace").strip()
            err_text = stderr.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error=f"exit code {process.returncode}",
                )
            try:
                value = json.loads(out_text.splitlines()[-1]) if out_text else {}
            except (json.JSONDecodeError, IndexError):
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error="tool did not print a JSON result",
                )
            return SandboxResult(ok=True, value=value, stdout=out_text, stderr=err_text)

    # -- docker executor (optional) --------------------------------------

    async def _execute_docker(
        self,
        code: str,
        arguments: dict[str, Any],
        policy: ToolExecutionPolicy | None = None,
    ) -> SandboxResult:
        """在容器里执行。docker 是否可用由调用方确认（见 `docker_daemon_ready`）。"""
        policy = policy or ToolExecutionPolicy()
        script = (
            "import json, sys\n"
            f"CODE = {code!r}\n"
            f"ARGS = {arguments!r}\n"
            "namespace = {}\n"
            "exec(compile(CODE, '<tool>', 'exec'), namespace)\n"
            "print(json.dumps(namespace['run'](**ARGS), ensure_ascii=False))\n"
        )
        command = self._docker_command(script, policy)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            # 超时不回退：容器可能已经在跑，重跑等于在没有隔离的情况下又执行一遍。
            # 同受限子进程执行器，被终止的进程必须真的终止。
            with contextlib.suppress(Exception):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error=f"docker timeout after {self.timeout_seconds}s",
            )
        except OSError as exc:
            # 探测之后 docker 命令行起不来了（被卸载、路径变了）：容器没起来，
            # 可以回退。
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error=f"docker 无法启动：{exc}", launch_failed=True,
            )
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            # 125 是 docker 自己的「这条命令根本没跑起来」（守护进程不可达、镜像拉
            # 不下来、参数被拒）；126/127 是容器起来了但命令跑不了，其余退出码来自
            # 容器里的工具本身 —— 只有 125 属于「容器没起来」，只有它允许回退。
            return SandboxResult(
                ok=False, value=None, stdout=out_text, stderr=err_text,
                error=f"docker exit code {process.returncode}",
                launch_failed=process.returncode == 125,
            )
        try:
            value = json.loads(out_text.splitlines()[-1]) if out_text else {}
        except (json.JSONDecodeError, IndexError):
            return SandboxResult(
                ok=False, value=None, stdout=out_text, stderr=err_text,
                error="tool did not print a JSON result",
            )
        return SandboxResult(ok=True, value=value, stdout=out_text, stderr=err_text)

    def _docker_command(self, script: str, policy: ToolExecutionPolicy) -> list[str]:
        """Build the docker run command from the execution policy."""
        command = [
            "docker", "run", "--rm",
            "--network", "bridge" if policy.network else "none",
            "--memory", "256m",
            "--cpus", "1",
            "--pids-limit", "64",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "--security-opt", "no-new-privileges",
        ]
        for path in policy.filesystem:
            import hashlib

            tag = int(hashlib.md5(path.encode("utf-8")).hexdigest(), 16) % 10000
            command += ["-v", f"{path}:/mnt/{tag}:ro"]
        command += ["-i", "python:3.12-slim", "python", "-c", script]
        return command
