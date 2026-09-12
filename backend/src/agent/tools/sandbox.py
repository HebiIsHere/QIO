"""Sandbox executor for agent-created tools.

Default executor: restricted subprocess (no credentials by default, temp
cwd, timeout, captured output). Docker is optional and detected at runtime;
on machines without a usable Docker daemon the subprocess executor is the
fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from agent.tools.policy import CapabilityLevel, ToolExecutionPolicy

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    value: dict[str, Any] | None
    stdout: str
    stderr: str
    error: str | None = None


class SandboxExecutor:
    def __init__(
        self,
        executor: str = "auto",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.executor = executor
        self.timeout_seconds = timeout_seconds

    @property
    def effective_executor(self) -> str:
        if self.executor != "auto":
            return self.executor
        return "docker" if self._docker_available() else "subprocess"

    def _docker_available(self) -> bool:
        return shutil.which("docker") is not None

    async def execute(
        self,
        code: str,
        arguments: dict[str, Any],
        extra_env: dict[str, str] | None = None,
        policy: ToolExecutionPolicy | None = None,
    ) -> SandboxResult:
        policy = policy or ToolExecutionPolicy()
        executor = self.effective_executor
        # restricted subprocess 不是安全沙箱：高风险能力若无法可靠隔离，
        # 拒绝执行（或需经过 Trusted 显式批准），绝不静默降级安全等级。
        if executor != "docker" and policy.is_high_risk() and policy.level != CapabilityLevel.TRUSTED:
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
        if self.effective_executor == "docker":
            return await self._execute_docker(code, arguments, policy)
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
        with tempfile.TemporaryDirectory(prefix="sa-tool-") as tmp:
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
        policy = policy or ToolExecutionPolicy()
        if not self._docker_available():
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error="docker not available",
            )
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
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error=f"docker timeout after {self.timeout_seconds}s",
            )
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            return SandboxResult(
                ok=False, value=None, stdout=out_text, stderr=err_text,
                error=f"docker exit code {process.returncode}",
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
