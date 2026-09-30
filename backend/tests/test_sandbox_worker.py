"""沙箱执行走 worker 协议：结果、诊断、超时与取消时的进程树清理。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent.tools import sandbox as sandbox_mod
from agent.tools.sandbox import SandboxExecutor


def _use_fake_worker(monkeypatch, tmp_path: Path, body: str) -> None:
    """把执行器指向一段假 worker 脚本（用于伪造协议响应）。"""
    from agent.tools import executor_env

    script = tmp_path / "fake_worker.py"
    script.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        executor_env,
        "resolve_tool_executor",
        lambda: executor_env.ToolExecutorSpec(
            "worker-script", [sys.executable, str(script)]
        ),
    )


async def test_sandbox_runs_tool_through_worker():
    result = await SandboxExecutor().execute(
        "def run(**kwargs):\n    return {'double': kwargs['n'] * 2}\n", {"n": 21}
    )
    assert result.ok is True
    assert result.value == {"double": 42}


async def test_sandbox_returns_tool_error_with_diagnostic():
    result = await SandboxExecutor().execute(
        "def run(**kwargs):\n    raise ValueError('kaboom')\n", {}
    )
    assert result.ok is False
    assert result.category == "code_error"
    assert "ValueError" in (result.error or "")
    assert "kaboom" in result.diagnostic()


async def test_sandbox_keeps_tool_print_out_of_the_result_channel():
    result = await SandboxExecutor().execute(
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
    executor = SandboxExecutor(timeout_seconds=0.5)
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
    executor = SandboxExecutor(timeout_seconds=60)
    task = asyncio.ensure_future(
        executor.execute("import time\ndef run(**kwargs):\n    time.sleep(30)\n    return {}\n", {})
    )
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(killed) == 1 and killed[0] > 0


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


# ---------- 协议的可信度：形似成功的结果不能算成功 ----------

_FAKE_SUCCESS = (
    "{\"ok\": true, \"value\": {\"sum\": 42}, \"stdout\": \"\", "
    "\"stderr\": \"\", \"error\": null, \"error_type\": null}"
)


async def test_fake_success_with_nonzero_exit_is_rejected(tmp_path, monkeypatch):
    """真事故形状：工具自己打印一行成功结果，然后以退出码 17 退出。"""
    _use_fake_worker(
        monkeypatch,
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({json.loads(_FAKE_SUCCESS)}))\n"
        "sys.stdout.flush()\n"
        "raise SystemExit(17)\n",
    )
    result = await SandboxExecutor().execute("def run(**kwargs):\n    return {}\n", {})
    assert result.ok is False
    assert result.value is None
    assert "exit code 17" in (result.error or "")


async def test_extra_output_lines_are_rejected(tmp_path, monkeypatch):
    """结果通道必须恰好一行；多打一行就不再可信。"""
    _use_fake_worker(
        monkeypatch,
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print('noise')\n"
        f"print(json.dumps({json.loads(_FAKE_SUCCESS)}))\n",
    )
    result = await SandboxExecutor().execute("def run(**kwargs):\n    return {}\n", {})
    assert result.ok is False
    assert "恰好一行" in (result.error or "")


async def test_non_boolean_ok_is_rejected(tmp_path, monkeypatch):
    _use_fake_worker(
        monkeypatch,
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'ok': 1, 'value': {'sum': 42}}))\n",
    )
    result = await SandboxExecutor().execute("def run(**kwargs):\n    return {}\n", {})
    assert result.ok is False
    assert "布尔" in (result.error or "")


async def test_success_without_object_value_is_rejected(tmp_path, monkeypatch):
    _use_fake_worker(
        monkeypatch,
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'ok': True, 'value': [1, 2, 3]}))\n",
    )
    result = await SandboxExecutor().execute("def run(**kwargs):\n    return {}\n", {})
    assert result.ok is False
    assert result.value is None


# ---------- 系统环境白名单 ----------

_ECHO_ENV_WORKER = (
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    "keys = ['PATH', 'TEMP', 'SYSTEMROOT', 'QIO_SECRET_MARKER']\n"
    "print(json.dumps({'ok': True, 'value': {k: bool(os.environ.get(k)) for k in keys},"
    " 'stdout': '', 'stderr': '', 'error': None, 'error_type': None}))\n"
)


async def test_worker_env_keeps_system_vars_and_drops_others(tmp_path, monkeypatch):
    monkeypatch.setenv("QIO_SECRET_MARKER", "sk-should-not-be-passed")
    _use_fake_worker(monkeypatch, tmp_path, _ECHO_ENV_WORKER)
    result = await SandboxExecutor().execute("def run(**kwargs):\n    return {}\n", {})
    assert result.ok is True
    assert result.value["PATH"] is True
    # TEMP/TMP 没配时用本次调用的临时目录兜底，永远有值
    assert result.value["TEMP"] is True
    # 白名单之外的进程环境不传进工具子进程
    assert result.value["QIO_SECRET_MARKER"] is False
    if sys.platform == "win32":
        # Windows 的系统 API 依赖 SystemRoot；以前它不在环境里
        assert result.value["SYSTEMROOT"] is True


# ---------- 输出保护 ----------

async def test_huge_tool_output_is_capped_inside_the_worker():
    """工具自己狂打印：worker 端就限长，结果仍然可用（而不是把内存吃光）。"""
    result = await SandboxExecutor().execute(
        "def run(**kwargs):\n"
        "    print('x' * (8 * 1024 * 1024))\n"
        "    return {'ok': True}\n",
        {},
    )
    assert result.ok is True
    assert "输出已截断" in result.stdout
    assert len(result.stdout) < 1_000_000


async def test_runaway_stdout_from_a_broken_worker_is_cut_off(tmp_path, monkeypatch):
    _use_fake_worker(
        monkeypatch,
        tmp_path,
        "import sys\n"
        "sys.stdin.read()\n"
        "chunk = 'y' * 65536\n"
        "while True:\n"
        "    sys.stdout.write(chunk)\n"
        "    sys.stdout.flush()\n",
    )
    result = await SandboxExecutor(timeout_seconds=30).execute(
        "def run(**kwargs):\n    return {}\n", {}
    )
    assert result.ok is False
    assert result.category == "output_format"
    assert "上限" in (result.error or "")
