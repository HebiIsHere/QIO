"""沙箱执行走 worker 协议：结果、诊断、超时与取消时的进程树清理。

两条执行路径（受限子进程 / 容器隔离）跑的是**同一个 worker、同一套协议**，
协议判定只有一份权威实现（sandbox._parse_worker_result），所以这里断言一次就够。

docker 相关的用例不在这里：本机通常没有 docker 守护进程，而这些用例要证明的是
「受限子进程这条路径」。需要真容器隔离的用例在 test_sandbox_executor.py 里用
进程边界上的桩覆盖。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent.tools import sandbox as sandbox_mod
from agent.tools.sandbox import SandboxExecutor

TOOL = "def run(**kwargs):\n    return {}\n"


def _fake_worker_executor(tmp_path: Path, body: str, **kwargs) -> SandboxExecutor:
    """把 **fake worker** 显式注入进去的执行器（正常依赖注入，不是 monkeypatch）。

    显式注入 = 「这次执行用哪个程序跑 worker」已经定了：auto 不再去探测 docker，
    也不再走 resolve_tool_executor 的另一套解析。

    以前这里用 monkeypatch 替换 executor_env.resolve_tool_executor，而那条路只有
    受限子进程走得到 —— 在 docker 守护进程真的可用的机器（ubuntu CI）上，auto 选了
    容器路径，注入的 fake worker 一次都没跑到，10 条用例全红。
    """
    from agent.tools import executor_env

    script = tmp_path / "fake_worker.py"
    script.write_text(body, encoding="utf-8")
    spec = executor_env.ToolExecutorSpec("worker-script", [sys.executable, str(script)])
    return SandboxExecutor(tool_executor=spec, **kwargs)


def _subprocess_executor(**kwargs) -> SandboxExecutor:
    """受限子进程路径：本机与 CI 上有没有 docker 都不该改变这些用例的结论。"""
    return SandboxExecutor(executor="subprocess", **kwargs)


def _marker_worker(marker: Path) -> str:
    """一个会先留下「我跑过了」证据、再回一行协议成功的 fake worker。"""
    return (
        "import json, sys\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('ran')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'ok': True, 'value': {'sum': 42}, 'stdout': '', 'stderr': '',"
        " 'error': None, 'error_type': None}))\n"
    )


async def test_sandbox_runs_tool_through_worker():
    result = await _subprocess_executor().execute(
        "def run(**kwargs):\n    return {'double': kwargs['n'] * 2}\n", {"n": 21}
    )
    assert result.ok is True
    assert result.value == {"double": 42}


async def test_sandbox_returns_tool_error_with_diagnostic():
    result = await _subprocess_executor().execute(
        "def run(**kwargs):\n    raise ValueError('kaboom')\n", {}
    )
    assert result.ok is False
    assert result.category == "code_error"
    assert "ValueError" in (result.error or "")
    assert "kaboom" in result.diagnostic()


async def test_sandbox_keeps_tool_print_out_of_the_result_channel():
    result = await _subprocess_executor().execute(
        "def run(**kwargs):\n    print('noise')\n    return {'ok': True}\n", {}
    )
    assert result.ok is True
    assert "noise" in result.stdout  # 工具输出被捕获，不污染结果通道


async def test_timeout_cleans_the_process_tree_of_this_call(monkeypatch):
    killed: list[int] = []
    real_kill = sandbox_mod._kill_process_tree

    async def _recording_kill(process):
        killed.append(process.pid)
        await real_kill(process)

    monkeypatch.setattr(sandbox_mod, "_kill_process_tree", _recording_kill)
    executor = _subprocess_executor(timeout_seconds=0.5)
    result = await executor.execute(
        "import time\ndef run(**kwargs):\n    time.sleep(30)\n    return {}\n", {}
    )
    assert result.ok is False
    assert result.category == "timeout"
    assert len(killed) == 1 and killed[0] > 0


async def test_cancel_cleans_the_process_tree_of_this_call(monkeypatch):
    killed: list[int] = []
    real_kill = sandbox_mod._kill_process_tree

    async def _recording_kill(process):
        killed.append(process.pid)
        await real_kill(process)

    monkeypatch.setattr(sandbox_mod, "_kill_process_tree", _recording_kill)
    executor = _subprocess_executor(timeout_seconds=60)
    task = asyncio.ensure_future(
        executor.execute("import time\ndef run(**kwargs):\n    time.sleep(30)\n    return {}\n", {})
    )
    await asyncio.sleep(1.0)
    task.cancel()
    # 取消是独立语义：不伪装成「一次失败的结果」，而是把取消本身传上去。
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(killed) == 1 and killed[0] > 0


async def test_timeout_kills_the_whole_process_tree_of_this_call(tmp_path):
    """A5：工具自己起的子进程也必须一起清掉 —— 清理的是**这一次调用的进程树**。

    实现上 Windows 用 `taskkill /PID <pid> /T /F`、POSIX 用进程组（killpg），但
    上层语义必须一致：超时之后属于这次调用的任何进程都不能再干活。这条断言在
    Windows CI 与 Linux CI 上**完全相同**，就是「语义一致」的证据。
    """
    started = tmp_path / "grandchild-started.marker"
    survived = tmp_path / "grandchild-survived.marker"
    child = (
        "import pathlib, time\n"
        f"pathlib.Path({str(started)!r}).write_text('up')\n"
        "time.sleep(6)\n"
        f"pathlib.Path({str(survived)!r}).write_text('alive')\n"
    )
    code = (
        "import subprocess, sys, time\n"
        "def run(**kwargs):\n"
        f"    subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "    time.sleep(30)\n"
        "    return {}\n"
    )
    executor = _subprocess_executor(timeout_seconds=3.0)
    result = await executor.execute(code, {})
    assert result.ok is False
    assert result.category == "timeout"
    await asyncio.sleep(8.0)
    # 正对照：子进程真的起来过（否则这条用例会因为「什么都没跑」而空过）
    assert started.exists() is True, "工具的子进程根本没起来，这条用例没有证明力"
    # 真正的断言：超时清理之后，这次调用里的子进程不能再留下任何痕迹
    assert survived.exists() is False, "属于这次调用的子进程还活着"


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill 只在 Windows 上使用")
async def test_windows_cleanup_targets_pid_not_image_name(monkeypatch):
    """只按 PID 清理进程树；绝不按可执行文件名称批量结束进程。"""
    calls: list[tuple] = []

    class _FakeKiller:
        async def wait(self):
            return 0

    async def _fake_exec(*argv, **kwargs):
        calls.append(argv)
        return _FakeKiller()

    class _FakeProc:
        pid = 4321

        async def wait(self):
            return 0

    monkeypatch.setattr(sandbox_mod.asyncio, "create_subprocess_exec", _fake_exec)
    await sandbox_mod._kill_process_tree(_FakeProc())
    assert calls == [("taskkill", "/PID", "4321", "/T", "/F")]


# ---------- 显式注入的 worker 运行方式，auto 不得覆盖 ----------


async def test_injected_worker_runtime_wins_over_an_available_docker_daemon(
    tmp_path, monkeypatch
):
    """A2 回归：docker 守护进程可用时，注入的 worker 仍必须被使用。

    ubuntu CI 上守护进程真的应答（test_sandbox_worker.py 全红的根因就是它）：
    auto 去跑容器，注入的 fake worker 一次都没跑到。这里把守护进程钉成「可用」，
    断言探测根本没有发生、起的是注入的 worker、结果来自它。
    """
    marker = tmp_path / "injected-ran.marker"
    probed: list[str] = []
    launches: list[tuple] = []
    real_exec = asyncio.create_subprocess_exec

    async def _probe() -> bool:
        probed.append("probe")
        return True

    async def _recording_exec(*cmd, **kwargs):
        launches.append(cmd)
        return await real_exec(*cmd, **kwargs)

    monkeypatch.setattr(sandbox_mod, "docker_daemon_ready", _probe)
    monkeypatch.setattr(sandbox_mod.asyncio, "create_subprocess_exec", _recording_exec)

    executor = _fake_worker_executor(tmp_path, _marker_worker(marker))
    assert await executor.effective_executor() == "subprocess"
    result = await executor.execute(TOOL, {})

    assert result.ok is True, result.error
    assert result.value == {"sum": 42}  # 结果来自注入的 fake worker，不是容器
    assert marker.is_file(), "注入的 fake worker 没有被执行"
    assert probed == [], "注入了运行方式还去探测 docker，等于把它悄悄换掉"
    assert launches and launches[0][0] == sys.executable
    assert all(cmd[0] != "docker" for cmd in launches)


def test_docker_and_injected_runtime_together_are_rejected_up_front():
    """两个显式选择互相矛盾时当场报错，不猜一个。"""
    from agent.tools import executor_env

    spec = executor_env.ToolExecutorSpec("worker-script", ["python", "worker.py"])
    with pytest.raises(ValueError):
        SandboxExecutor(executor="docker", tool_executor=spec)


async def test_injected_runtime_and_project_interpreter_are_rejected(tmp_path):
    executor = _fake_worker_executor(tmp_path, _marker_worker(tmp_path / "unused.marker"))
    result = await executor.execute(TOOL, {}, interpreter="C:/envs/x/python.exe")
    assert result.ok is False
    assert result.category == "environment"
    assert "矛盾" in (result.error or "")


# ---------- 协议的可信度：形似成功的结果不能算成功 ----------

_FAKE_SUCCESS = (
    "{\"ok\": true, \"value\": {\"sum\": 42}, \"stdout\": \"\", "
    "\"stderr\": \"\", \"error\": null, \"error_type\": null}"
)


async def test_fake_success_with_nonzero_exit_is_rejected(tmp_path):
    """真事故形状：工具自己打印一行成功结果，然后以退出码 17 退出。"""
    executor = _fake_worker_executor(
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({json.loads(_FAKE_SUCCESS)}))\n"
        "sys.stdout.flush()\n"
        "raise SystemExit(17)\n",
    )
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert result.value is None
    assert "exit code 17" in (result.error or "")


async def test_extra_output_lines_are_rejected(tmp_path):
    """结果通道必须恰好一行；多打一行就不再可信。"""
    executor = _fake_worker_executor(
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print('noise')\n"
        f"print(json.dumps({json.loads(_FAKE_SUCCESS)}))\n",
    )
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert "恰好一行" in (result.error or "")


async def test_non_boolean_ok_is_rejected(tmp_path):
    executor = _fake_worker_executor(
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'ok': 1, 'value': {'sum': 42}}))\n",
    )
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert "布尔" in (result.error or "")


async def test_success_without_object_value_is_rejected(tmp_path):
    executor = _fake_worker_executor(
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'ok': True, 'value': [1, 2, 3]}))\n",
    )
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert result.value is None


async def test_empty_result_channel_is_rejected(tmp_path):
    """worker 正常退出但什么都不写：这不是成功，是「没有结果」。"""
    executor = _fake_worker_executor(tmp_path, "import sys\nsys.stdin.read()\n")
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert result.value is None
    assert result.category == "output_format"
    assert "没有返回结果" in (result.error or "")


async def test_single_line_that_is_not_json_is_rejected(tmp_path):
    executor = _fake_worker_executor(
        tmp_path, "import sys\nsys.stdin.read()\nprint('totally not json')\n"
    )
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert result.category == "output_format"
    assert "不是合法 JSON" in (result.error or "")


async def test_worker_that_dies_before_reading_the_request_is_rejected(tmp_path):
    executor = _fake_worker_executor(tmp_path, "import sys\nsys.exit(3)\n")
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert "exit code 3" in (result.error or "")


def test_signal_death_is_reported_as_a_signal_not_a_plain_exit_code():
    """被信号杀死（POSIX 上 returncode 为负）要如实说，不能只报一个负数退出码。"""
    result = sandbox_mod._parse_worker_result(
        returncode=-9,
        stdout_text="",
        stderr_text="",
        over_limit=False,
        runtime="subprocess",
    )
    assert result.ok is False
    assert result.category == "startup"
    assert "signal 9" in (result.error or "")


def test_container_protocol_failures_are_labeled_as_container():
    """容器路径与受限子进程共用解析：只是诊断里说清是哪条路径。"""
    result = sandbox_mod._parse_worker_result(
        returncode=1,
        stdout_text="",
        stderr_text="boom",
        over_limit=False,
        runtime="docker",
    )
    assert result.ok is False
    assert "容器" in (result.error or "")


# ---------- 系统环境白名单 ----------

_ECHO_ENV_KEYS = [
    "PATH",
    "TEMP",
    "SYSTEMROOT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "QIO_SECRET_MARKER",
    "QIO_SESSION_TOKEN",
    "OPENAI_API_KEY",
]

_ECHO_ENV_WORKER = (
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    f"keys = {_ECHO_ENV_KEYS!r}\n"
    "print(json.dumps({'ok': True, 'value': {k: bool(os.environ.get(k)) for k in keys},"
    " 'stdout': '', 'stderr': '', 'error': None, 'error_type': None}))\n"
)


async def test_worker_env_keeps_system_vars_and_drops_others(tmp_path, monkeypatch):
    monkeypatch.setenv("QIO_SECRET_MARKER", "sk-should-not-be-passed")
    monkeypatch.setenv("QIO_SESSION_TOKEN", "session-token-should-not-be-passed")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-be-passed")
    executor = _fake_worker_executor(tmp_path, _ECHO_ENV_WORKER)
    result = await executor.execute(TOOL, {})
    assert result.ok is True
    assert result.value["PATH"] is True
    # TEMP/TMP 没配时用本次调用的临时目录兜底，永远有值
    assert result.value["TEMP"] is True
    # Python 的 I/O 编码由白名单显式钉死，不跟随宿主 locale
    assert result.value["PYTHONIOENCODING"] is True
    assert result.value["PYTHONUTF8"] is True
    # 白名单之外的进程环境一律不传进工具子进程（不是「大部分不传」）
    assert result.value["QIO_SECRET_MARKER"] is False
    assert result.value["QIO_SESSION_TOKEN"] is False
    assert result.value["OPENAI_API_KEY"] is False
    if sys.platform == "win32":
        # Windows 的系统 API 依赖 SystemRoot；以前它不在环境里
        assert result.value["SYSTEMROOT"] is True


async def test_caller_injected_env_reaches_the_worker(tmp_path, monkeypatch):
    """调用方显式注入的 extra_env 是**受信通道**：必须在白名单之外照样生效。

    专用依赖环境与模拟服务（sitecustomize + PYTHONPATH）都靠它接线；
    白名单管的是「主机环境不许整体继承」，不是「调用方也不许显式给」。
    """
    monkeypatch.setenv("QIO_SECRET_MARKER", "sk-should-not-be-passed")
    keys = ["QIO_TOOL_ENV_MARKER", "PYTHONPATH", "QIO_SECRET_MARKER"]
    worker = (
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"keys = {keys!r}\n"
        "print(json.dumps({'ok': True, 'value': {k: os.environ.get(k) for k in keys},"
        " 'stdout': '', 'stderr': '', 'error': None, 'error_type': None}))\n"
    )
    executor = _fake_worker_executor(tmp_path, worker)
    result = await executor.execute(
        TOOL, {}, extra_env={"QIO_TOOL_ENV_MARKER": "injected", "PYTHONPATH": "/fake/sitecustomize"}
    )
    assert result.ok is True, result.error
    assert result.value["QIO_TOOL_ENV_MARKER"] == "injected"
    assert result.value["PYTHONPATH"] == "/fake/sitecustomize"
    # 主机上的无关变量仍然被剥掉：注入口子不是「整体继承」的后门
    assert result.value["QIO_SECRET_MARKER"] is None


def test_env_allowlist_is_explicit_and_never_extends_os_environ():
    """白名单是「逐项列出」的：没有 os.environ 整体继承这条路。"""
    assert "PATH" in sandbox_mod._ENV_ALLOWLIST
    assert "TEMP" in sandbox_mod._ENV_ALLOWLIST and "TMP" in sandbox_mod._ENV_ALLOWLIST
    forbidden = ("QIO_SESSION_TOKEN", "OPENAI_API_KEY", "HOME", "USERPROFILE")
    for key in forbidden:
        assert key not in sandbox_mod._ENV_ALLOWLIST


# ---------- 输出保护 ----------

async def test_huge_tool_output_is_capped_inside_the_worker():
    """工具自己狂打印：worker 端就限长，结果仍然可用（而不是把内存吃光）。"""
    result = await _subprocess_executor().execute(
        "def run(**kwargs):\n"
        "    print('x' * (8 * 1024 * 1024))\n"
        "    return {'ok': True}\n",
        {},
    )
    assert result.ok is True
    assert "输出已截断" in result.stdout
    assert len(result.stdout) < 1_000_000


async def test_runaway_stdout_from_a_broken_worker_is_cut_off(tmp_path):
    executor = _fake_worker_executor(
        tmp_path,
        "import sys\n"
        "sys.stdin.read()\n"
        "chunk = 'y' * 65536\n"
        "while True:\n"
        "    sys.stdout.write(chunk)\n"
        "    sys.stdout.flush()\n",
        timeout_seconds=30,
    )
    result = await executor.execute(TOOL, {})
    assert result.ok is False
    assert result.category == "output_format"
    assert "上限" in (result.error or "")


async def test_huge_result_value_is_rejected_with_a_legal_protocol_result():
    """返回值本身超限时，worker 仍要回一行**合法**协议结果，而不是把协议撑爆。

    真子进程 + 真 worker：3 MB 的返回值远超过结果行上限，父进程不该靠「把子进程
    连同没写完的结果一起杀掉」来表达失败。
    """
    result = await _subprocess_executor().execute(
        "def run(**kwargs):\n    return {'blob': 'z' * 3_000_000}\n", {}
    )
    assert result.ok is False
    assert result.value is None
    assert result.category == "output_format"
    assert "上限" in (result.error or "")
