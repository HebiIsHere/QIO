"""SandboxExecutor：docker 可用性探测与回退 + 容器路径的进程/输出保护（回归用例）。

回归的缺陷 1：可用性只用 shutil.which("docker") 判断，于是「装了 Docker Desktop
但没启动」被当成「容器隔离可用」——每次工具调用都选 docker 并以 docker exit code 125
失败，而不是改走受限子进程。

回归的缺陷 2（2026-10-02）：容器路径是**第二套执行实现** —— 它自己拼一段脚本、
自己 print JSON、自己取「最后一行」当结果，不校验 ok/value、不限长、超时只 kill
docker 客户端（容器继续在后台跑）。现在容器里跑的是同一份 worker 源码、同一套协议，
协议判定与受限子进程共用 sandbox._parse_worker_result。

测试用 stub_docker_cli 在**进程边界**上模拟 docker 命令行：非 docker 命令仍走
真实实现，所以「回退到受限子进程」这条路径是真的跑起来了才断言通过。
"""

from __future__ import annotations

import asyncio
import json
import sys

from agent.tools import sandbox as sandbox_module
from agent.tools.policy import ToolExecutionPolicy
from agent.tools.sandbox import SandboxExecutor

# 一个纯函数工具：返回值可断言，也足够便宜。
PURE_TOOL = "def run(**k):\n    return {'ok': 1}\n"


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


class _StubStream:
    """子进程的一侧管道：按块读出预设字节（block=True 时永不返回，模拟卡住的容器）。"""

    def __init__(self, data: bytes = b"", *, block: bool = False) -> None:
        self._data = data
        self._block = block

    async def read(self, size: int = -1) -> bytes:
        if self._block:
            await asyncio.sleep(3600)
        if not self._data:
            return b""
        if size is None or size < 0:
            size = len(self._data)
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


class _StubStdin:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _StubProcess:
    """沙箱真正用到的成员：pid / 三条管道 / returncode / kill / wait（+ 探测用 communicate）。"""

    def __init__(
        self,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
        *,
        block: bool = False,
        pid: int = 4321,
    ) -> None:
        self.pid = pid
        self.returncode = returncode
        self.stdin = _StubStdin()
        self.stdout = _StubStream(stdout, block=block)
        self.stderr = _StubStream(stderr, block=block)
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
    run_block: bool = False,
    spawns: list | None = None,
) -> list[tuple[str, ...]]:
    """模拟 docker 命令行，返回它收到的命令列表。

    daemon_version=None 表示守护进程没有应答：探测命令失败，docker run 以
    125 失败 —— 这就是「装了 Docker Desktop 但没启动」那台机器上的真实表现。

    run_block=True 表示 docker run 起来的容器永不返回（超时/取消用例）。

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
            process = _StubProcess(
                run_returncode, run_stdout, run_stderr, block=run_block
            )
            if spawns is not None:
                spawns.append(process)
            return process
        # docker version：可用性探测（没应答时退出码 1 + connect 报错）
        if len(cmd) > 1 and cmd[1] == "version":
            if daemon_version is None:
                return _StubProcess(
                    1, b"",
                    b"error during connect: cannot connect to the Docker daemon",
                )
            return _StubProcess(0, daemon_version, b"")
        # docker rm -f：超时/取消/超限后的容器清理
        return _StubProcess(0, b"", b"")

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: f"/fake/bin/{name}")
    return seen


def _container_name_from(commands: list[tuple[str, ...]]) -> str:
    run = next(cmd for cmd in commands if len(cmd) > 1 and cmd[1] == "run")
    return run[run.index("--name") + 1]


def _removals(commands: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [cmd for cmd in commands if len(cmd) > 1 and cmd[1] == "rm"]


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
    """守护进程应答了，但 docker run 自己失败（典型：镜像拉不下来）→ exit 125。"""
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
    spawns: list = []
    seen = stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=_worker_reply({"via": "docker"}),
        spawns=spawns,
    )
    res = await SandboxExecutor().execute(
        "def run(**k):\n    return {'via': 'tool'}\n", {"a": 1}, policy=ToolExecutionPolicy()
    )
    assert res.ok is True, res.error
    assert res.value == {"via": "docker"}
    assert any(cmd[1] == "run" for cmd in seen)
    # 请求是用同一套协议从 stdin 递进容器的（不是把代码拼进命令行）
    request = json.loads(spawns[0].stdin.written.decode("utf-8"))
    assert request["arguments"] == {"a": 1}
    assert "return {'via': 'tool'}" in request["code"]
    assert spawns[0].stdin.closed is True


async def test_effective_executor_reports_what_the_probe_found(monkeypatch):
    stub_docker_cli(monkeypatch, daemon_version=None)
    assert await SandboxExecutor().effective_executor() == "subprocess"
    assert await SandboxExecutor(executor="docker").effective_executor() == "docker"
    assert await SandboxExecutor(executor="subprocess").effective_executor() == "subprocess"

    stub_docker_cli(monkeypatch, daemon_version=b"27.0.3\n")
    assert await SandboxExecutor().effective_executor() == "docker"


# -- 容器路径：协议判定与受限子进程同源 ----------------------------------


async def test_container_result_channel_uses_the_same_strict_protocol(monkeypatch):
    """容器里 worker 多打一行：与受限子进程一样判失败（以前只取最后一行）。"""
    stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=b'noise\n{"ok": true, "value": {"via": "docker"}}\n',
    )
    res = await SandboxExecutor().execute(PURE_TOOL, {}, policy=ToolExecutionPolicy())
    assert res.ok is False
    assert "恰好一行" in (res.error or "")


async def test_container_non_boolean_ok_is_rejected(monkeypatch):
    stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=b'{"ok": 1, "value": {"via": "docker"}}\n',
    )
    res = await SandboxExecutor().execute(PURE_TOOL, {}, policy=ToolExecutionPolicy())
    assert res.ok is False
    assert "布尔" in (res.error or "")


async def test_container_success_without_object_value_is_rejected(monkeypatch):
    stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=_worker_reply({"via": "docker"}).replace(b'{"via": "docker"}', b"[1, 2]"),
    )
    res = await SandboxExecutor().execute(PURE_TOOL, {}, policy=ToolExecutionPolicy())
    assert res.ok is False
    assert res.value is None


# -- 容器路径：超时 / 取消 / 输出超限都要清掉这次的容器 --------------------


async def test_container_timeout_removes_this_calls_container(monkeypatch):
    seen = stub_docker_cli(
        monkeypatch, daemon_version=b"27.0.3\n", run_block=True
    )
    res = await SandboxExecutor(executor="docker", timeout_seconds=0.2).execute(
        PURE_TOOL, {}, policy=ToolExecutionPolicy()
    )
    assert res.ok is False
    assert res.category == "timeout"
    name = _container_name_from(seen)
    assert _removals(seen) == [("docker", "rm", "-f", name)], "超时后容器必须被清掉"


async def test_container_cancel_removes_this_calls_container(monkeypatch):
    seen = stub_docker_cli(
        monkeypatch, daemon_version=b"27.0.3\n", run_block=True
    )
    task = asyncio.ensure_future(
        SandboxExecutor(executor="docker", timeout_seconds=60).execute(
            PURE_TOOL, {}, policy=ToolExecutionPolicy()
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - 取消必须向上传递，不能伪装成一次失败
        raise AssertionError("取消没有作为取消传上去")
    name = _container_name_from(seen)
    assert _removals(seen) == [("docker", "rm", "-f", name)], "取消后容器必须被清掉"


async def test_container_runaway_output_is_cut_off_and_stops_the_container(monkeypatch):
    monkeypatch.setattr(sandbox_module, "MAX_WORKER_STDOUT_BYTES", 4096)
    seen = stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=b"y" * 200_000,
    )
    res = await SandboxExecutor(executor="docker", timeout_seconds=30).execute(
        PURE_TOOL, {}, policy=ToolExecutionPolicy()
    )
    assert res.ok is False
    assert res.category == "output_format"
    assert "上限" in (res.error or "")
    name = _container_name_from(seen)
    assert _removals(seen) == [("docker", "rm", "-f", name)]


# -- 依赖镜像：用了就用它，起不来不回退 ----------------------------------

IMAGE_TOOL = (
    "from pathlib import Path\n"
    "def run(**k):\n"
    "    Path(k['marker']).write_text('ran')\n"
    "    return {'ok': 1}\n"
)


async def test_dependency_image_replaces_the_default_image(monkeypatch):
    seen = stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=0,
        run_stdout=_worker_reply({"via": "container-env"}),
    )
    res = await SandboxExecutor(executor="docker").execute(
        PURE_TOOL, {}, policy=ToolExecutionPolicy(), container_image="qio-tool-env:3.12-abc123"
    )
    assert res.ok is True, res.error
    run = next(cmd for cmd in seen if len(cmd) > 1 and cmd[1] == "run")
    assert "qio-tool-env:3.12-abc123" in run
    assert "python:3.12-slim" not in run


async def test_missing_dependency_image_is_reported_and_never_falls_back(
    monkeypatch, tmp_path
):
    """镜像不在本机（docker run 以 125 收场）：如实报缺依赖，绝不用宿主解释器重跑。"""
    marker = tmp_path / "subprocess-ran.txt"
    seen = stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=125,
        run_stderr=b"docker: Error response from daemon: pull access denied for qio-tool-env",
    )
    res = await SandboxExecutor(executor="docker").execute(
        IMAGE_TOOL,
        {"marker": str(marker)},
        policy=ToolExecutionPolicy(),
        container_image="qio-tool-env:3.12-deadbeef",
    )
    assert res.ok is False
    assert res.category == "missing_dependency"
    assert "qio-tool-env:3.12-deadbeef" in (res.error or "")
    assert "不回退" in (res.error or "")
    assert marker.exists() is False, "回退了：工具在宿主受限子进程里跑了一遍"
    assert len([cmd for cmd in seen if len(cmd) > 1 and cmd[1] == "run"]) == 1


async def test_auto_with_missing_dependency_image_does_not_fall_back(monkeypatch, tmp_path):
    marker = tmp_path / "subprocess-ran.txt"
    stub_docker_cli(
        monkeypatch,
        daemon_version=b"27.0.3\n",
        run_returncode=125,
        run_stderr=b"docker: Error response from daemon: no such image",
    )
    res = await SandboxExecutor().execute(
        IMAGE_TOOL,
        {"marker": str(marker)},
        policy=ToolExecutionPolicy(),
        container_image="qio-tool-env:3.12-deadbeef",
    )
    assert res.ok is False
    assert res.category == "missing_dependency"
    assert marker.exists() is False, "auto 也不许把依赖镜像失败降级成宿主执行"


async def test_dependency_image_with_the_subprocess_executor_is_rejected(monkeypatch):
    res = await SandboxExecutor(executor="subprocess").execute(
        PURE_TOOL, {}, policy=ToolExecutionPolicy(), container_image="qio-tool-env:3.12-abc"
    )
    assert res.ok is False
    assert res.category == "environment"
    assert "受限子进程" in (res.error or "")


# -- 容器里跑的是同一份 worker 源码 --------------------------------------


async def test_container_bootstrap_runs_the_same_worker(tmp_path):
    """把 docker run 里那段引导脚本拿本地解释器跑一遍（不需要 docker）。

    证明容器路径与受限子进程路径跑的是同一个 worker（同一套 stdin/stdout 协议、
    同一套退出码语义），而不是「容器里另写一段打印 JSON 的脚本」。
    """
    from agent.tools import executor_env
    from agent.tools.sandbox import _DOCKER_BOOTSTRAP_TEMPLATE, _worker_request

    files = {
        "pkg/__init__.py": "",
        "pkg/util.py": "def double(x):\n    return x * 2\n",
        "pkg/main.py": (
            "from .util import double\n\n"
            "def run(**kwargs):\n    return {'value': double(kwargs['x'])}\n"
        ),
    }
    script = _DOCKER_BOOTSTRAP_TEMPLATE.format(
        project=repr(str(tmp_path / "project")),
        worker_path=repr(str(tmp_path / "worker.py")),
        files=repr(files),
        worker_source=repr(executor_env.worker_source()),
    )
    request = _worker_request("", "pkg.main:run", {"x": 21})
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(request), timeout=60)
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    lines = [line for line in stdout.decode("utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, stdout
    payload = json.loads(lines[0])
    assert payload["ok"] is True
    assert payload["value"] == {"value": 42}
    # 工程文件确实被带进容器，包内相对 import 成立
    assert (tmp_path / "project" / "pkg" / "util.py").is_file()
