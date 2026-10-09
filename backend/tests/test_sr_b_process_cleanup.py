"""契约 2：超时 / 取消的进程树清理 —— 真实 Windows 验证。

未修复基线上的旧行为（这些测试在基线上应当红）：

* 超时后只返回一句"程序执行超时"，子进程（含孙进程）继续活着；
* 取消发生在执行协程里时，子进程同样被留在原地继续跑；
* 清理既不按树收，也没有任何"退出验证"。

按 Windows 原生 toolchain 真实验证：taskkill /T /F /PID、tasklist 查证存活。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from agent.services.computer import CommandRisk
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool

# 本组用例按 Windows 原生语义（taskkill /T /F /PID）设计与验证。
pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Windows 原生 taskkill 才是契约验收对象；本组不跑 POSIX"
)


class _AutoAccept:
    """替代 ComputerSandbox 的判定层，让工具直接进入执行路径。"""

    def command_verdict(self, cmd: str):
        return "auto", CommandRisk.LOW

    def shell_verdict(self, cmd: str):
        return "auto", CommandRisk.LOW

    def command_verdict_for_program(self, program, args=None):
        return "auto", CommandRisk.LOW


class _ApproveAccept:
    """判定走审批的桩：用于验证「审批等待期间取消」。"""

    def command_verdict(self, cmd: str):
        return "approve", CommandRisk.HIGH

    def shell_verdict(self, cmd: str):
        return "approve", CommandRisk.HIGH

    def command_verdict_for_program(self, program, args=None):
        return "approve", CommandRisk.HIGH


class _HangingApproval:
    """永不答复的审批桩（模拟用户没来得及点确认）。"""

    def __init__(self) -> None:
        self.entered = False
        self.was_cancelled = False

    async def request(self, kind: str, payload: dict):
        self.entered = True
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise


# 孙进程：真实 python，被 child 用 Popen 启动，然后 child + grand 各自长睡。
_LAUNCH_TREE = (
    "import json, os, subprocess, sys, time",
    "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])",
    "with open(sys.argv[1], 'w') as f:",
    "    json.dump({'child': os.getpid(), 'grand': grand.pid}, f)",
    "time.sleep(600)",
)


async def _wait_marker(path, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (ValueError, OSError) as exc:
                last_err = exc
        await asyncio.sleep(0.02)
    raise AssertionError(f"标记文件未写出：{path}（at {last_err}）")


async def _pid_alive(pid: int) -> bool:
    """用原生 tasklist 查证某个 pid 是否还活着（真实系统证据，不 mock）。"""
    proc = await asyncio.create_subprocess_exec(
        "tasklist",
        "/FI",
        f"PID eq {pid}",
        "/FO",
        "CSV",
        "/NH",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    for line in text.splitlines():
        parts = [p.strip('" ') for p in line.split(",")]
        if len(parts) >= 2 and parts[1] == str(pid):
            return True
    return False


async def _wait_gone(pids, deadline_seconds: float) -> bool:
    deadline = time.monotonic() + deadline_seconds
    while True:
        alive = [p for p in pids if await _pid_alive(p)]
        if not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def _kill_pid(pid: int) -> None:
    try:
        await asyncio.wait_for(
            _run_taskkill(pid), timeout=15
        )
    except Exception:
        pass


async def _run_taskkill(pid: int):
    proc = await asyncio.create_subprocess_exec(
        "taskkill", "/F", "/PID", str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


def _program_tool() -> RunProgramTool:
    t = RunProgramTool()
    t.computer = _AutoAccept()
    t.approvals = object()
    return t


@pytest.mark.asyncio
async def test_timeout_terminates_tree_within_one_second_and_spares_decoy(tmp_path):
    """工具 timeout=2：返回后 1s 内 child+grand 全消失；同名诱饵进程毫发无损。"""
    tool = _program_tool()
    victim_marker = tmp_path / "victim.json"
    decoy_marker = tmp_path / "decoy.json"
    decoy_code = (
        "import json, os, sys, time",
        "f = open(sys.argv[1], 'w')",
        "json.dump({'pid': os.getpid()}, f)",
        "f.flush()",
        "f.close()",
        "time.sleep(600)",
    )
    decoy = subprocess.Popen(
        [sys.executable, "-c", "; ".join(decoy_code), str(decoy_marker)]
    )
    victim_pids = None
    try:
        decoy_pid = (await _wait_marker(decoy_marker))["pid"]
        assert await _pid_alive(decoy_pid), "诱饵进程必须先确认活着"
        # 让 victim 与诱饵同时存在，才开始跑工具（清理必须在同名进程在场时进行）
        res = await asyncio.wait_for(
            tool.run(
                program=sys.executable,
                args=["-c", chr(10).join(_LAUNCH_TREE), str(victim_marker)],
                cwd=str(tmp_path),
                timeout=2,
            ),
            timeout=30,
        )
        assert res.ok is False
        assert "超时" in (res.error or ""), f"error 要含超时语义：{res.error!r}"
        assert "已终止" in (res.error or ""), f"error 要含终止语义：{res.error!r}"
        assert "清理未确认" not in (res.error or ""), "确认收干净就不能说过未确认"
        victim_pids = await _wait_marker(victim_marker)
        assert await _wait_gone(
            [victim_pids["child"], victim_pids["grand"]], deadline_seconds=1.0
        ), f"返回后 1s 内进程树必须全消失，实际存活：{victim_pids}"
        assert await _pid_alive(decoy_pid), "同名诱饵进程不得被误杀"
    finally:
        await _kill_pid(decoy.pid)
        if victim_pids:
            await _kill_pid(victim_pids["child"])
            await _kill_pid(victim_pids["grand"])


@pytest.mark.asyncio
async def test_cancel_during_execution_kills_tree_and_propagates(tmp_path):
    """取消：进程树被收掉 + CancelledError 原样传播（loop 靠它判定取消终态）。"""
    tool = _program_tool()
    marker = tmp_path / "victim.json"
    task = asyncio.ensure_future(
        tool.run(
            program=sys.executable,
            args=["-c", chr(10).join(_LAUNCH_TREE), str(marker)],
            cwd=str(tmp_path),
            timeout=120,
        )
    )
    victim_pids = None
    try:
        victim_pids = await _wait_marker(marker)
        await asyncio.sleep(0.3)  # 确保孙进程也已经 spawn
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_gone(
            [victim_pids["child"], victim_pids["grand"]], deadline_seconds=5.0
        ), f"取消后进程树必须退出，实际存活：{victim_pids}"
    finally:
        if victim_pids:
            await _kill_pid(victim_pids["child"])
            await _kill_pid(victim_pids["grand"])


@pytest.mark.asyncio
async def test_cancel_during_approval_wait_starts_nothing(tmp_path):
    """审批等待期间取消：不留下半启动的进程（审批被拒/取消也绝不进入执行）。"""
    tool = RunProgramTool()
    tool.computer = _ApproveAccept()
    approval = _HangingApproval()
    tool.approvals = approval
    child_flag = tmp_path / "child_started.txt"
    code = "import sys; f = open(sys.argv[1], 'w'); f.write('started'); f.close()"
    task = asyncio.ensure_future(
        tool.run(
            program=sys.executable,
            args=["-c", code, str(child_flag)],
            cwd=str(tmp_path),
            timeout=120,
        )
    )
    try:
        for _ in range(200):
            if approval.entered:
                break
            await asyncio.sleep(0.02)
        assert approval.entered, "工具应先进入审批等待"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert approval.was_cancelled, "审批等待也应收到取消"
        await asyncio.sleep(0.2)
        assert not child_flag.exists(), "审批阶段取消不得留下任何半启动进程"
    finally:
        if task.done():
            return
        task.cancel()


@pytest.mark.asyncio
async def test_shell_timeout_reports_termination(tmp_path):
    """run_shell 同样要有超时 + 终止语义（默认 shell 树一并收掉）。"""
    tool = RunCmdTool()
    tool.computer = _AutoAccept()
    tool.approvals = object()
    start = time.monotonic()
    res = await tool.run(cmd="ping -n 30 127.0.0.1 > NUL", timeout=1.5)
    elapsed = time.monotonic() - start
    assert res.ok is False
    assert "超时" in (res.error or ""), f"error 要含超时语义：{res.error!r}"
    assert "已终止" in (res.error or ""), f"error 要含终止语义：{res.error!r}"
    assert elapsed < 12, f"超时收尾必须及时返回，实际耗时 {elapsed:.1f}s"
