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
import logging
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.tools.policy import CapabilityLevel, ToolExecutionPolicy
from agent.tools.project_files import check_project_size, materialize

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0

DOCKER_BINARY = "docker"
# 探测只是「能不能用容器」的前置询问，不该让工具调用长时间挂住。
DOCKER_PROBE_TIMEOUT_SECONDS = 5.0

# 父进程愿意为一个工具子进程缓冲的上限。worker 自己也会限长（见
# agent/tool_worker.py），这里是第二道闸：一个不是 worker 的程序（或坏掉的
# worker）无限打印时，不能把后端进程的内存吃光。
MAX_WORKER_STDOUT_BYTES = 2 * 1024 * 1024
MAX_WORKER_STDERR_BYTES = 512 * 1024
_READ_CHUNK_BYTES = 64 * 1024

# 传给工具子进程的环境白名单。以前只给 PATH/TEMP/TMP：Windows 上连
# SystemRoot 都没有，而大量系统 API（含 Python 自身的一些调用）依赖它。
# 这里逐项列出「运行确实需要」的系统变量；凭据与用户业务配置一律不传 ——
# 子进程不是安全沙箱，但也没有理由把用不到的变量递进去。
_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

# 容器里的执行脚本：先把这个项目的文件写进容器内的 /tmp/project，再在那里执行
# 入口。语义与受限子进程那条路径一致（存在本地 docker 时才走这里）。
_DOCKER_SCRIPT_TEMPLATE = """
import importlib, json, os, sys
PROJECT = "/tmp/project"
os.makedirs(PROJECT, exist_ok=True)
for rel, content in {files}.items():
    target = os.path.join(PROJECT, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(content)
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)
CODE = {code}
ENTRY = {entry}
ARGS = {arguments}
if ENTRY:
    module_name, _, func_name = ENTRY.partition(":")
    run = getattr(importlib.import_module(module_name), func_name)
else:
    namespace = {{}}
    exec(compile(CODE, "<tool>", "exec"), namespace)
    run = namespace["run"]
print(json.dumps(run(**ARGS), ensure_ascii=False))
"""


def _system_env(scratch_dir: str) -> dict[str, str]:
    """系统环境白名单（+ 本项目固定的编码设置）。"""
    env: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.setdefault("TEMP", scratch_dir)
    env.setdefault("TMP", scratch_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


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
    # 统一的错误类别（见 core/tool_feedback.py）；上层据此分类，而不是猜文本。
    category: str | None = None

    def diagnostic(self, limit: int = 2000) -> str:
        """脱敏、限长后的诊断详情（stderr 优先，附 stdout 末尾）。

        底层捕获的 stderr 以前没有完整传到上层，模型只能看到「exit code 1」。
        """
        from agent.trace.redact import redact_text

        parts: list[str] = []
        if self.stderr.strip():
            parts.append("stderr:\n" + self.stderr.strip())
        if self.stdout.strip():
            parts.append("stdout:\n" + self.stdout.strip())
        text = redact_text("\n".join(parts)).strip()
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n…[诊断已截断，共 {len(text)} 字符]"


def _classify_failure(stderr: str) -> str:
    """从子进程 stderr 推断类别（缺依赖 / 代码异常）。"""
    text = (stderr or "").lower()
    if "modulenotfounderror" in text or "no module named" in text:
        return "missing_dependency"
    return "code_error"


def _category_for(error_type: str | None, stderr: str) -> str:
    """worker 报的异常类型 → 统一错误类别（见 core/tool_feedback.py）。"""
    text = (stderr or "").lower()
    if error_type in {"ModuleNotFoundError", "ImportError"} or "no module named" in text:
        return "missing_dependency"
    return "code_error"


def _spawn_kwargs() -> dict:
    """POSIX 上让子进程自成一个进程组，便于按组清理。"""
    if sys.platform == "win32":
        return {}
    return {"start_new_session": True}


async def _kill_process_tree(process) -> None:
    """只结束这一次工具调用对应的进程树。

    明确不做的事：按可执行文件名称批量结束进程 —— 那会误伤同一台机器上名字相同的
    其它进程（包括用户自己的）。Windows 用 `taskkill /PID <pid> /T /F`，它从这个
    PID 往下遍历子进程；POSIX 用进程组。清理失败只记日志，不盖过真正的错误。
    """
    pid = getattr(process, "pid", None)
    try:
        if pid and sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(killer.wait(), timeout=10)
        elif pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - 清理是尽力而为
        logger.warning("failed to terminate tool process tree (pid=%s)", pid, exc_info=True)
    with contextlib.suppress(Exception):
        await process.wait()


async def _exchange(process, request: bytes) -> tuple[bytes, bytes, bool]:
    """写请求、有界读回 stdout/stderr，返回 `(stdout, stderr, 超限?)`。

    以前用 `process.communicate()`：父进程先把子进程的全部输出读进内存，输出
    限制要等 worker 自己截断才生效。现在边读边计数；任何一侧超过上限就立刻终止
    这棵进程树（否则子进程继续写、父进程不再消费，管道填满后双方互等）。
    """
    over_limit = False

    async def drain(stream, limit: int) -> bytes:
        nonlocal over_limit
        chunks: list[bytes] = []
        total = 0
        capped = False
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            if capped:
                # 已经超限：把管道读干但不再留存，读完 EOF 管道才会正常关闭。
                continue
            total += len(chunk)
            if total > limit:
                room = limit - (total - len(chunk))
                if room > 0:
                    chunks.append(chunk[:room])
                over_limit = True
                capped = True
                # 立刻结束这棵进程树：否则子进程继续写、我们只读不留，
                # 会一直读到超时。
                await _kill_process_tree(process)
                continue
            chunks.append(chunk)

    try:
        process.stdin.write(request)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        # 子进程可能还没读就退出了；能不能拿到结果由下面的读取决定。
        pass
    finally:
        with contextlib.suppress(Exception):
            process.stdin.close()
        with contextlib.suppress(Exception):
            await process.stdin.wait_closed()
    stdout, stderr = await asyncio.gather(
        drain(process.stdout, MAX_WORKER_STDOUT_BYTES),
        drain(process.stderr, MAX_WORKER_STDERR_BYTES),
    )
    with contextlib.suppress(Exception):
        await process.wait()
    return stdout, stderr, over_limit


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
        files: dict[str, str] | None = None,
        entry: str | None = None,
    ) -> SandboxResult:
        """执行一次工具。

        `files` 是这个多文件项目的**其它文件**（相对路径 → 内容），`entry` 是入口
        （`pkg.main:run`）。两者都只活在这一次调用的一次性临时目录里：不进工作区、
        不留在后端进程，下一次调用是干净的。
        """
        policy = policy or ToolExecutionPolicy()
        try:
            project_files = dict(files or {})
            check_project_size(project_files)
        except ValueError as exc:
            # 项目结构本身不合法：这是「生成的项目有问题」，模型改路径就能修，
            # 所以归到 code_error（可重试），而不是环境问题。
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error=str(exc), category="code_error",
            )

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
                    category="environment",
                )
            return await self._execute_docker(code, arguments, policy, project_files, entry)

        if self.executor == "auto" and await docker_daemon_ready():
            result = await self._execute_docker(code, arguments, policy, project_files, entry)
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
                category="environment",
            )
        return await self._execute_subprocess(
            code, arguments, extra_env or {}, policy, project_files, entry
        )

    # -- subprocess executor ----------------------------------------------

    async def _execute_subprocess(
        self,
        code: str,
        arguments: dict[str, Any],
        extra_env: dict[str, str] | None = None,
        policy: ToolExecutionPolicy | None = None,
        files: dict[str, str] | None = None,
        entry: str | None = None,
    ) -> SandboxResult:
        from agent.tools.executor_env import ToolRuntimeUnavailable, resolve_tool_executor

        try:
            spec = resolve_tool_executor()
        except ToolRuntimeUnavailable as exc:
            # 没有可用执行方式：如实报环境问题，不静默用 PATH 里未知的 Python。
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error=str(exc), category="environment",
            )
        # 结构化请求走标准输入；结果只从标准输出读一行 JSON（见 agent/tool_worker.py）。
        request = json.dumps(
            {"code": code, "entry": entry or "", "arguments": arguments},
            ensure_ascii=False,
        ).encode("utf-8")
        # ignore_cleanup_errors：Windows 上被终止的子进程可能短暂占住作为 cwd 的
        # 临时目录，清理失败不应该让工具执行以异常收场（错误信息本身已经返回）。
        with tempfile.TemporaryDirectory(
            prefix="sa-tool-", ignore_cleanup_errors=True
        ) as tmp:
            # 项目文件写进这次调用自己的临时目录：工具里的 import 只看得见
            # 它自己的项目，工作区与后端目录都不在其中。
            if files:
                try:
                    materialize(files, Path(tmp))
                except ValueError as exc:
                    return SandboxResult(
                        ok=False, value=None, stdout="", stderr="",
                        error=str(exc), category="code_error",
                    )
            env = _system_env(tmp)
            if extra_env:
                env.update(extra_env)
            try:
                process = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    cwd=tmp,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_spawn_kwargs(),
                )
            except OSError as exc:
                # 命令行存在却起不来（路径损坏、权限不对）：这是环境问题，必须
                # 说清楚，而不是让调用方以为「工具跑失败」。
                return SandboxResult(
                    ok=False, value=None, stdout="", stderr="",
                    error=f"工具执行程序无法启动：{exc}",
                    category="startup",
                )
            try:
                stdout, stderr, over_limit = await asyncio.wait_for(
                    _exchange(process, request), timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                # wait_for 只取消了读取；必须真的结束这棵进程树，否则工具自己起的
                # 子进程会留下（资源泄漏 + 临时目录清理失败）。
                await _kill_process_tree(process)
                return SandboxResult(
                    ok=False, value=None, stdout="", stderr="",
                    error=f"timeout after {self.timeout_seconds}s",
                    category="timeout",
                )
            except asyncio.CancelledError:
                # 用户取消：同样只清理这一棵进程树，然后如实向上传递取消语义。
                await _kill_process_tree(process)
                raise
            if over_limit:
                # 进程树已经在读取侧终止；这里只负责如实说明为什么没结果。
                return SandboxResult(
                    ok=False, value=None, stdout="", stderr="",
                    error=(
                        "工具执行程序的输出超过上限"
                        f"（stdout {MAX_WORKER_STDOUT_BYTES} 字节 / "
                        f"stderr {MAX_WORKER_STDERR_BYTES} 字节），已终止本次执行。"
                    ),
                    category="output_format",
                )
            out_text = stdout.decode("utf-8", errors="replace").strip()
            err_text = stderr.decode("utf-8", errors="replace").strip()

            # 成功必须是「正常退出 + 恰好一行合法 JSON + 字段类型对」。以前只看
            # 最后一行 JSON：工具自己打印一行形似成功的结果（甚至接着以非零码
            # 退出）也会被当成 ok=True —— 协议的可信度就是这么丢掉的。
            if process.returncode != 0:
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error=(
                        f"工具执行程序异常退出（exit code {process.returncode}）："
                        "结果不可信，已按失败处理。"
                    ),
                    category="startup",
                )
            if not out_text:
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error="工具执行程序没有返回结果",
                    category="output_format",
                )
            lines = [line for line in out_text.splitlines() if line.strip()]
            if len(lines) != 1:
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error=f"结果通道必须恰好一行 JSON（实际 {len(lines)} 行）",
                    category="output_format",
                )
            try:
                payload = json.loads(lines[0])
            except json.JSONDecodeError:
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error="工具执行程序返回的不是合法 JSON 结果",
                    category="output_format",
                )
            if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
                return SandboxResult(
                    ok=False, value=None, stdout=out_text, stderr=err_text,
                    error="工具执行程序的返回结果缺少布尔字段 ok",
                    category="output_format",
                )
            tool_stdout = str(payload.get("stdout") or "")
            tool_stderr = str(payload.get("stderr") or "")
            if not payload.get("ok"):
                return SandboxResult(
                    ok=False,
                    value=None,
                    stdout=tool_stdout,
                    stderr=tool_stderr or err_text,
                    error=str(payload.get("error") or "工具执行失败"),
                    category=_category_for(payload.get("error_type"), tool_stderr),
                )
            value = payload.get("value")
            if not isinstance(value, dict):
                return SandboxResult(
                    ok=False, value=None, stdout=tool_stdout,
                    stderr=tool_stderr or err_text,
                    error="工具执行程序声称成功但 value 不是 JSON 对象",
                    category="output_format",
                )
            return SandboxResult(
                ok=True,
                value=value,
                stdout=tool_stdout,
                stderr=tool_stderr,
            )

    # -- docker executor (optional) --------------------------------------

    async def _execute_docker(
        self,
        code: str,
        arguments: dict[str, Any],
        policy: ToolExecutionPolicy | None = None,
        files: dict[str, str] | None = None,
        entry: str | None = None,
    ) -> SandboxResult:
        """在容器里执行。docker 是否可用由调用方确认（见 `docker_daemon_ready`）。"""
        policy = policy or ToolExecutionPolicy()
        script = _DOCKER_SCRIPT_TEMPLATE.format(
            code=repr(code),
            entry=repr(entry or ""),
            files=repr(dict(files or {})),
            arguments=repr(arguments),
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
                category="timeout",
            )
        except OSError as exc:
            # 探测之后 docker 命令行起不来了（被卸载、路径变了）：容器没起来，
            # 可以回退。
            return SandboxResult(
                ok=False, value=None, stdout="", stderr="",
                error=f"docker 无法启动：{exc}", launch_failed=True,
                category="environment",
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
                category=(
                    "environment" if process.returncode == 125
                    else _classify_failure(err_text)
                ),
            )
        try:
            value = json.loads(out_text.splitlines()[-1]) if out_text else {}
        except (json.JSONDecodeError, IndexError):
            return SandboxResult(
                ok=False, value=None, stdout=out_text, stderr=err_text,
                error="tool did not print a JSON result",
                category="output_format",
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
