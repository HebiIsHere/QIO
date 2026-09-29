"""SandboxExecutor：docker 可用性探测与回退（回归用例）。

回归的缺陷：可用性只用 `shutil.which("docker")` 判断，于是「装了 Docker Desktop
但没启动」被当成「容器隔离可用」——每次工具调用都选 docker 并以 `docker exit
code 125` 失败，而不是改走受限子进程。

GitHub 的 windows runner 踩的是同一件事（装了 CLI、没有守护进程），当时的处理是
给用例加 `requires_docker` 标记并在 windows 任务里过滤掉，产品行为没动；这个文件
覆盖产品行为本身。

测试用 `stub_docker_cli` 在**进程边界**上模拟 docker 命令行：非 docker 命令仍走
真实实现，所以「回退到受限子进程」这条路径是真的跑起来了才断言通过。
"""

from __future__ import annotations

import asyncio

from agent.tools import sandbox as sandbox_module
from agent.tools.policy import ToolExecutionPolicy
from agent.tools.sandbox import SandboxExecutor

# 一个纯函数工具：返回值可断言，也足够便宜。
PURE_TOOL = "def run(**k):\n    return {'ok': 1}\n"


class _StubProcess:
    """只实现沙箱用到的三个成员：communicate / returncode / kill。"""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def stub_docker_cli(
    monkeypatch,
    *,
    daemon_version: bytes | None = b"27.0.3\n",
    run_returncode: int = 0,
    run_stdout: bytes = b"",
    run_stderr: bytes = b"",
    run_raises: BaseException | None = None,
) -> list[tuple[str, ...]]:
    """模拟 docker 命令行，返回它收到的命令列表。

    `daemon_version=None` 表示守护进程没有应答：探测命令失败，`docker run` 以
    125 失败 —— 这就是「装了 Docker Desktop 但没启动」那台机器上的真实表现。

    只有 docker 命令被替换成桩；受限子进程执行器用的解释器仍走真实实现。
    """
    real_exec = asyncio.create_subprocess_exec
    seen: list[tuple[str, ...]] = []

    async def fake_exec(*cmd, **kwargs):  # noqa: ANN002, ANN003
        if not cmd or cmd[0] != "docker":
            return await real_exec(*cmd, **kwargs)
        seen.append(cmd)
        if len(cmd) > 1 and cmd[1] == "run":  # 真正跑容器
            if daemon_version is None:
                return _StubProcess(
                    125, b"",
                    b"docker: error during connect: cannot connect to the Docker daemon",
                )
            if run_raises is not None:
                raise run_raises
            return _StubProcess(run_returncode, run_stdout, run_stderr)
        # 其余 docker 子命令按「可用性探测」处理
        if daemon_version is None:
            return _StubProcess(
                1, b"",
                b"error during connect: cannot connect to the Docker daemon",
            )
        return _StubProcess(0, daemon_version, b"")

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    return seen


# -- 可用性探测本身 ------------------------------------------------------


async def test_probe_is_false_without_the_docker_cli(monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: None)
    assert await sandbox_module.docker_daemon_ready() is False


async def test_probe_is_false_when_the_daemon_does_not_answer(monkeypatch):
    stub_docker_cli(monkeypatch, daemon_version=None)
    assert await sandbox_module.docker_daemon_ready() is False


async def test_probe_needs_a_server_version_not_just_a_zero_exit(monkeypatch):
    """只有客户端、没有服务端时退出码也可能是 0，但输出为空 —— 不算可用。"""
    stub_docker_cli(monkeypatch, daemon_version=b"")
    assert await sandbox_module.docker_daemon_ready() is False


async def test_probe_is_true_when_the_daemon_reports_a_version(monkeypatch):
    stub_docker_cli(monkeypatch, daemon_version=b"27.0.3\n")
    assert await sandbox_module.docker_daemon_ready() is True


# -- 命令行装了、守护进程没启动：不该让每个工具调用都失败 ------------------


async def test_auto_with_silent_daemon_runs_the_tool_in_restricted_subprocess(monkeypatch):
    stub_docker_cli(monkeypatch, daemon_version=None)
    res = await SandboxExecutor().execute(PURE_TOOL, {}, policy=ToolExecutionPolicy())
    assert res.ok is True, res.error
    assert res.value == {"ok": 1}


async def test_auto_with_silent_daemon_still_refuses_high_risk(monkeypatch):
    """没有容器隔离时高风险能力仍然拒绝执行，而不是换个错误码失败。"""
    stub_docker_cli(monkeypatch, daemon_version=None)
    res = await SandboxExecutor().execute(
        PURE_TOOL, {}, policy=ToolExecutionPolicy(network=True)
    )
    assert res.ok is False
    assert "拒绝执行" in (res.error or "")
    assert "125" not in (res.error or "")


async def test_explicit_docker_with_silent_daemon_reports_and_never_runs_the_tool(
    monkeypatch, tmp_path
):
    """显式指定 docker 时不静默降级，但要说清是「守护进程没应答」。"""
    stub_docker_cli(monkeypatch, daemon_version=None)
    marker = tmp_path / "ran.txt"
    code = (
        "from pathlib import Path\n"
        f"def run(**k):\n    Path({str(marker)!r}).write_text('ran')\n    return {{'ok': 1}}\n"
    )
    res = await SandboxExecutor(executor="docker").execute(code, {}, policy=ToolExecutionPolicy())
    assert res.ok is False
    assert "不可用" in (res.error or "")
    assert marker.exists() is False


# -- 探测通过、容器仍然起不来：回退而不是把整次调用判死 --------------------


async def test_auto_falls_back_when_docker_cannot_start_a_container(monkeypatch):
    """守护进程应答了，但 `docker run` 自己失败（典型：镜像拉不下来）→ exit 125。"""
    stub_docker_cli(monkeypatch, daemon_version=b"27.0.3\n", run_returncode=125,
                    run_stderr=b"docker: Error response from daemon: pull access denied")
    res = await SandboxExecutor().execute(PURE_TOOL, {}, policy=ToolExecutionPolicy())
    assert res.ok is True, res.error
    assert res.value == {"ok": 1}


async def test_auto_falls_back_when_the_docker_binary_cannot_be_spawned(monkeypatch):
    stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_raises=FileNotFoundError("docker"),
    )
    res = await SandboxExecutor().execute(PURE_TOOL, {}, policy=ToolExecutionPolicy())
    assert res.ok is True, res.error
    assert res.value == {"ok": 1}


async def test_tool_failure_inside_the_container_is_not_retried_in_subprocess(monkeypatch, tmp_path):
    """退出码来自工具自己（不是 docker 起不来）时不许回退 —— 回退等于无隔离重跑一遍。"""
    stub_docker_cli(monkeypatch, daemon_version=b"27.0.3\n", run_returncode=1,
                    run_stderr=b"Traceback (most recent call last): KeyError: 'a'")
    marker = tmp_path / "ran.txt"
    code = (
        "from pathlib import Path\n"
        f"def run(**k):\n    Path({str(marker)!r}).write_text('ran')\n    return {{'ok': 1}}\n"
    )
    res = await SandboxExecutor().execute(code, {}, policy=ToolExecutionPolicy())
    assert res.ok is False
    assert "exit code 1" in (res.error or "")
    assert marker.exists() is False


async def test_auto_uses_docker_when_the_daemon_answers(monkeypatch):
    """守护进程应答时仍然走容器：返回值来自（桩）docker，而不是子进程执行器。"""
    seen = stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=b'{"via": "docker"}\n',
    )
    res = await SandboxExecutor().execute(
        "def run(**k):\n    return {'via': 'tool'}\n", {}, policy=ToolExecutionPolicy()
    )
    assert res.ok is True, res.error
    assert res.value == {"via": "docker"}
    assert any(cmd[1] == "run" for cmd in seen)


async def test_effective_executor_reports_what_the_probe_found(monkeypatch):
    stub_docker_cli(monkeypatch, daemon_version=None)
    assert await SandboxExecutor().effective_executor() == "subprocess"
    assert await SandboxExecutor(executor="docker").effective_executor() == "docker"
    assert await SandboxExecutor(executor="subprocess").effective_executor() == "subprocess"

    stub_docker_cli(monkeypatch, daemon_version=b"27.0.3\n")
    assert await SandboxExecutor().effective_executor() == "docker"
