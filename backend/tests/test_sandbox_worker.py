"""沙箱执行走 worker 协议：结果、诊断、超时与取消时的进程树清理。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from agent.tools import sandbox as sandbox_mod
from agent.tools.sandbox import SandboxExecutor


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
