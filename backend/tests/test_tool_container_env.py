"""依赖镜像 → 容器执行的接线：镜像真的被用上；起不来就明确失败，绝不静默降级。

本机没有 Docker 守护进程，所以这里在**进程边界**上模拟 docker 命令行（与
tests/test_sandbox_executor.py 的 stub_docker_cli 同一思路）：docker 命令走桩，其它命令
走真实实现 —— 于是「有没有偷偷换一个没有依赖的解释器再跑一遍」这件事可以被真实断言
（工具代码有副作用、非 docker 的进程会被记下来）。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent.tools import sandbox as sandbox_module
from agent.tools.dev_tools import DevRunTestsTool
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.runtime_tools import CodeTool
from agent.tools.sandbox import DEFAULT_DOCKER_IMAGE, SandboxExecutor, SandboxResult
from agent.tools.spec import ToolDefinition
from agent.tools.tool_envs import ContainerStatus, EnvStatus, ToolEnvManager

IMAGE = "qio-tool-env:3.11-0c0b24aba1fe455e"
PURE_TOOL = "def run(**k):\n    return {'ok': 1}\n"


# -- docker 命令行桩（进程边界） -------------------------------------------


class _Stream:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        if not self._data:
            return b""
        if size is None or size < 0:
            size = len(self._data)
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


class _Stdin:
    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _Process:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.pid = 4321
        self.returncode = returncode
        self.stdin = _Stdin()
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        # 可用性探测（docker version）走的是 communicate()：桩必须把预设输出给出来，
        # 否则探测永远「没应答」，测试就会在错误的路径上失败。
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def _worker_reply(value: dict) -> bytes:
    """容器里的 worker 会写出的那一行（协议结果）。"""
    payload = {
        "ok": True,
        "value": value,
        "stdout": "",
        "stderr": "",
        "error": None,
        "error_type": None,
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def stub_docker_cli(
    monkeypatch,
    *,
    daemon_version: bytes | None = b"27.0.3\n",
    run_returncode: int = 0,
    run_stdout: bytes = b"",
    run_stderr: bytes = b"",
) -> list[tuple[str, ...]]:
    """模拟 docker 命令行；返回**所有**被启动的命令（含非 docker 的，用于断言没有回退）。

    daemon_version=None 表示守护进程没应答（探测失败）。run_returncode=125 表示容器
    根本没起来（镜像不在本机 / 守护进程不可达）。
    """
    real_exec = asyncio.create_subprocess_exec
    seen: list[tuple[str, ...]] = []

    async def fake_exec(*cmd, **kwargs):  # noqa: ANN002, ANN003
        seen.append(cmd)
        if not cmd or cmd[0] != "docker":
            return await real_exec(*cmd, **kwargs)
        if len(cmd) > 1 and cmd[1] == "run":
            return _Process(run_returncode, run_stdout, run_stderr)
        if len(cmd) > 1 and cmd[1] == "version":
            if daemon_version is None:
                return _Process(1, b"", b"error during connect: cannot connect")
            return _Process(0, daemon_version, b"")
        return _Process(0, b"", b"")

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    return seen


def _docker_runs(commands: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [cmd for cmd in commands if len(cmd) > 1 and cmd[0] == "docker" and cmd[1] == "run"]


def _non_docker(commands: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [cmd for cmd in commands if not cmd or cmd[0] != "docker"]


def _image_of(run: tuple[str, ...]) -> str:
    return run[run.index("-i") + 1]


# -- 执行侧：依赖镜像真的被用上 --------------------------------------------


async def test_the_dependency_image_is_used_for_explicit_docker(monkeypatch):
    seen = stub_docker_cli(monkeypatch, run_stdout=_worker_reply({"ok": 1}))

    res = await SandboxExecutor(executor="docker").execute(
        PURE_TOOL, {}, container_image=IMAGE
    )

    assert res.ok is True, res.error
    assert res.value == {"ok": 1}
    run = _docker_runs(seen)[0]
    assert _image_of(run) == IMAGE
    assert DEFAULT_DOCKER_IMAGE not in run  # 不用默认镜像，也不用它顶替依赖镜像


async def test_the_dependency_image_is_used_when_auto_picks_the_container(monkeypatch):
    seen = stub_docker_cli(monkeypatch, run_stdout=_worker_reply({"via": "image"}))

    res = await SandboxExecutor().execute(PURE_TOOL, {}, container_image=IMAGE)

    assert res.ok is True, res.error
    assert res.value == {"via": "image"}
    assert _image_of(_docker_runs(seen)[0]) == IMAGE


async def test_a_container_that_cannot_start_never_falls_back(monkeypatch, tmp_path):
    """镜像起不来（125）：报环境/缺依赖错、工具代码一行没执行、也不换解释器再跑一遍。"""
    marker = tmp_path / "ran.txt"
    code = (
        "from pathlib import Path\n"
        f"def run(**k):\n    Path({str(marker)!r}).write_text('ran')\n    return {{'ok': 1}}\n"
    )
    seen = stub_docker_cli(
        monkeypatch,
        run_returncode=125,
        run_stderr=b"docker: Error response from daemon: No such image: " + IMAGE.encode(),
    )

    res = await SandboxExecutor().execute(code, {}, container_image=IMAGE)

    assert res.ok is False
    # 指定了依赖镜像时这不叫「可以回退的启动失败」
    assert res.launch_failed is False
    assert res.category in {"missing_dependency", "environment"}
    assert "不回退" in (res.error or "")
    assert IMAGE in (res.error or "")
    assert marker.exists() is False  # 回退到真实 worker 就会写出这个文件
    assert _non_docker(seen) == []  # 也没有偷偷启动别的解释器


async def test_a_dependency_image_that_cannot_start_never_falls_back_with_explicit_docker(
    monkeypatch, tmp_path
):
    marker = tmp_path / "ran-explicit.txt"
    code = (
        "from pathlib import Path\n"
        f"def run(**k):\n    Path({str(marker)!r}).write_text('ran')\n    return {{'ok': 1}}\n"
    )
    seen = stub_docker_cli(monkeypatch, run_returncode=125, run_stderr=b"No such image")

    res = await SandboxExecutor(executor="docker").execute(code, {}, container_image=IMAGE)

    assert res.ok is False
    assert res.error and "起不来" in res.error
    assert marker.exists() is False
    assert _non_docker(seen) == []


# -- 回归：宿主解释器 + 容器仍然拒绝、子进程 + 镜像仍然报错 ------------------


async def test_a_host_interpreter_plus_a_container_is_still_refused(monkeypatch):
    seen = stub_docker_cli(monkeypatch)

    res = await SandboxExecutor(executor="docker").execute(
        PURE_TOOL,
        {},
        interpreter="C:/envs/deadbeef/Scripts/python.exe",
        container_image=IMAGE,
    )

    assert res.ok is False
    assert "容器里不会安装这套依赖" in (res.error or "")
    assert seen == []  # 连容器的路都没走：拒绝发生在探测之前


async def test_auto_with_a_host_interpreter_still_refuses_the_container(monkeypatch):
    seen = stub_docker_cli(monkeypatch)  # 守护进程应答 → auto 会选容器

    res = await SandboxExecutor().execute(
        PURE_TOOL, {}, interpreter="C:/envs/deadbeef/Scripts/python.exe"
    )

    assert res.ok is False
    assert "容器里不会安装这套依赖" in (res.error or "")
    assert _docker_runs(seen) == []


async def test_a_dependency_image_with_the_subprocess_executor_is_rejected(monkeypatch):
    stub_docker_cli(monkeypatch)

    res = await SandboxExecutor(executor="subprocess").execute(
        PURE_TOOL, {}, container_image=IMAGE
    )

    assert res.ok is False
    assert "受限子进程" in (res.error or "")
    assert "镜像在这条路径上不起作用" in (res.error or "")


# -- 调用方接线：注册后调用（runtime_tools）与测试阶段（dev_tools） ---------


class _FakeEnvs:
    """环境管理器替身：记录「谁被问了什么」，按脚本回答。

    真实的 ToolEnvManager 容器准备另有 9 条用例（tests/test_tool_envs.py）。
    """

    def __init__(
        self,
        *,
        container_image: str | None = IMAGE,
        container_reason: str = "依赖镜像没准备好",
        host_interpreter: str | None = "C:/envs/host/Scripts/python.exe",
        host_ok: bool = True,
    ) -> None:
        self.container_image = container_image
        self.container_reason = container_reason
        self.host_interpreter = host_interpreter
        self.host_ok = host_ok
        self.container_requests: list[dict] = []
        self.host_requests: list[dict] = []

    async def ensure_container_image(self, requirements, **kwargs) -> ContainerStatus:
        self.container_requests.append({"requirements": list(requirements), **kwargs})
        if self.container_image is None:
            return ContainerStatus(ok=False, reason=self.container_reason)
        return ContainerStatus(ok=True, image=self.container_image, reused=True)

    async def ensure(self, requirements, **kwargs) -> EnvStatus:
        self.host_requests.append({"requirements": list(requirements), **kwargs})
        if not self.host_ok:
            return EnvStatus(ok=False, interpreter=None, reason="宿主环境没准备好")
        return EnvStatus(ok=True, interpreter=self.host_interpreter)

    def status_for(self, requirements) -> EnvStatus:
        self.host_requests.append({"requirements": list(requirements), "status_only": True})
        if not self.host_ok:
            return EnvStatus(ok=False, interpreter=None, reason="宿主环境没准备好")
        return EnvStatus(ok=True, interpreter=self.host_interpreter)


class _RecordingSandbox:
    executor = "docker"
    timeout_seconds = 5.0

    def __init__(self, executor: str = "docker") -> None:
        self.executor = executor
        self.executions = 0
        self.executions_args: list[dict] = []

    async def effective_executor(self) -> str:
        return self.executor

    async def execute(
        self,
        code,
        arguments,
        extra_env=None,
        policy=None,
        files=None,
        entry=None,
        interpreter=None,
        container_image=None,
    ) -> SandboxResult:
        self.executions += 1
        self.executions_args.append(
            {"interpreter": interpreter, "container_image": container_image}
        )
        return SandboxResult(ok=True, value={"ok": True}, stdout="", stderr="")


class _Approvals:
    def __init__(self, decision: str = "approved") -> None:
        self.decision = decision
        self.requests: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        self.requests.append((kind, payload))
        return ApprovalResult("appr_test", self.decision)


def _definition(**overrides) -> ToolDefinition:
    payload = {
        "name": "fetch_weather",
        "description": "查天气",
        "code": "def run(**kwargs):\n    return {'ok': True}\n",
        "tests": [{"name": "t", "input": {}, "expect": {"ok": True}}],
    }
    payload.update(overrides)
    return ToolDefinition(**payload)


async def test_a_registered_tool_uses_the_dependency_image_in_a_container():
    sandbox = _RecordingSandbox("docker")
    envs = _FakeEnvs()

    result = await CodeTool(_definition(requirements=["requests"]), sandbox, envs=envs).run()

    assert result.ok, result.error
    assert sandbox.executions_args == [{"interpreter": None, "container_image": IMAGE}]
    # 注册后的调用没有审批通道：只复用本机已有的镜像，不现构建
    assert envs.container_requests[0]["approvals"] is None
    assert envs.host_requests == []
    assert envs.container_requests[0]["requirements"] == ["requests"]


async def test_a_registered_tool_refuses_when_the_dependency_image_is_missing():
    sandbox = _RecordingSandbox("docker")
    envs = _FakeEnvs(container_image=None, container_reason="本机没有这个依赖镜像：请先跑测试")

    result = await CodeTool(_definition(requirements=["requests"]), sandbox, envs=envs).run()

    assert result.ok is False
    assert result.category == "missing_dependency"
    assert "本机没有这个依赖镜像" in (result.error or "")
    assert sandbox.executions == 0  # 没有换成随包解释器跑一遍
    assert envs.host_requests == []  # 也没去装宿主环境


async def test_a_registered_tool_still_uses_the_host_environment_in_subprocess():
    sandbox = _RecordingSandbox("subprocess")
    envs = _FakeEnvs()

    result = await CodeTool(_definition(requirements=["requests"]), sandbox, envs=envs).run()

    assert result.ok, result.error
    assert sandbox.executions_args == [
        {"interpreter": envs.host_interpreter, "container_image": None}
    ]
    assert envs.host_requests[0].get("status_only") is True
    assert envs.container_requests == []


async def test_running_tests_prepares_the_dependency_image_for_a_container(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")
    ws.write_definition(task.id, _definition(requirements=["requests"]))
    sandbox = _RecordingSandbox("docker")
    envs = _FakeEnvs()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=_Approvals(), envs=envs)

    result = await tool.run(workspace=task.id)

    assert result.ok, result.error
    assert sandbox.executions_args == [{"interpreter": None, "container_image": IMAGE}]
    # 测试阶段可以现准备（构建镜像会走审批通道），而不是像注册后调用那样只复用
    assert envs.container_requests[0]["approvals"] is not None
    assert envs.host_requests == []  # 容器路径不去装宿主环境


async def test_running_tests_fails_clearly_when_the_image_cannot_be_prepared(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")
    ws.write_definition(task.id, _definition(requirements=["requests"]))
    sandbox = _RecordingSandbox("docker")
    envs = _FakeEnvs(container_image=None, container_reason="你没有同意构建容器镜像：容器路径这次不可用")
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=_Approvals(), envs=envs)

    result = await tool.run(workspace=task.id)

    assert result.ok is False
    assert result.category == "missing_dependency"
    assert "没有同意构建容器镜像" in (result.error or "")
    assert sandbox.executions == 0


async def test_running_tests_still_prepares_the_host_environment_in_subprocess(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("查天气")
    ws.write_definition(task.id, _definition(requirements=["requests"]))
    sandbox = _RecordingSandbox("subprocess")
    envs = _FakeEnvs()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=_Approvals(), envs=envs)

    result = await tool.run(workspace=task.id)

    assert result.ok, result.error
    assert sandbox.executions_args == [
        {"interpreter": envs.host_interpreter, "container_image": None}
    ]
    assert envs.host_requests[0]["approvals"] is not None
    assert envs.container_requests == []


async def test_without_dependencies_nothing_is_prepared_in_a_container(tmp_path):
    """没有依赖声明：容器路径照旧用随包镜像，不该去准备任何依赖环境。"""
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("纯标准库工具")
    ws.write_definition(task.id, _definition())
    sandbox = _RecordingSandbox("docker")
    envs = _FakeEnvs()
    tool = DevRunTestsTool(ws, sandbox=sandbox, approvals=_Approvals(), envs=envs)

    result = await tool.run(workspace=task.id)

    assert result.ok, result.error
    assert sandbox.executions_args == [{"interpreter": None, "container_image": None}]
    assert envs.container_requests == []
    assert envs.host_requests == []

# -- 真容器：只能由 ubuntu CI 给出结论（本机没有 Docker 守护进程） ----------


@pytest.mark.requires_docker
async def test_a_real_container_runs_with_the_locked_dependency_image(tmp_path):
    """真 docker：pip 解析出锁定版本 → 按锁定清单构建镜像 → 容器里 import 到那个依赖。

    这是「容器路径也有依赖语义」的唯一真实证据来源。本机没有 Docker 守护进程时按条件
    跳过（windows CI 任务按 requires_docker 过滤）—— 跳过不算验证过，结论以 ubuntu
    CI 的这条用例为准。
    """
    if not await sandbox_module.docker_daemon_ready():
        if os.environ.get("CI"):
            # CI 上不能静默跳过：跳过等于这条「容器里真的有依赖」的验证不存在。
            pytest.fail("CI 上必须有可用的 docker 守护进程：这条用例不能跳过")
        pytest.skip("本机没有 docker 守护进程：这条只能在 ubuntu CI 上真跑")

    manager = ToolEnvManager(tmp_path / "envs")
    status = await manager.ensure_container_image(
        ["six>=1.16"], approvals=_Approvals(), tool_name="six_tool"
    )

    assert status.ok, status.reason
    assert status.image == manager.container_image_for(["six>=1.16"])
    assert status.pinned and status.pinned[0].startswith("six==")
    version = status.pinned[0].split("==", 1)[1]

    code = "import six\n\ndef run(**kwargs):\n    return {'six': six.__version__}\n"
    with_image = await SandboxExecutor(executor="docker", timeout_seconds=120).execute(
        code, {}, container_image=status.image
    )

    assert with_image.ok, with_image.diagnostic()
    assert with_image.value == {"six": version}

    # 对照组：同一个工具、默认镜像（没装 six）里跑必须失败 —— 证明依赖真的来自依赖镜像，
    # 不是碰巧随包解释器里就有。
    without_image = await SandboxExecutor(executor="docker", timeout_seconds=120).execute(code, {})
    assert without_image.ok is False
    assert "six" in (without_image.error or "") + without_image.diagnostic()
