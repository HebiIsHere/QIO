"""契约 2 反例：命令结果 = 真实退出码 × 如实启动失败。

未修复基线上的旧行为（这些测试在基线上应当红）：

* 旧反例："git show 不存在的引用" 退出码 128，却被报成 ok=True；
* 退出码非 0 的程序一律被吞成成功（stderr 拼进 content 就当没事）；
* 启动失败（FileNotFoundError）缺少明确、诚实的报错语义。
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys

import pytest

from agent.services.computer import CommandRisk
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool


class _AutoAccept:
    """替代 ComputerSandbox 的判定层（那是契约 1 的范围），让工具走纯执行路径。"""

    def command_verdict(self, cmd: str):
        return "auto", CommandRisk.LOW

    def shell_verdict(self, cmd: str):
        return "auto", CommandRisk.LOW

    def command_verdict_for_program(self, program, args=None, **kwargs):
        return "auto", CommandRisk.LOW


def _program_tool() -> RunProgramTool:
    t = RunProgramTool()
    t.computer = _AutoAccept()
    t.approvals = object()  # auto 路径不会用到审批
    return t


def _seed_repo(tmp_path) -> None:
    subprocess.run(
        ["git", "init", "."], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=sr-b@test",
            "-c",
            "user.name=sr-b",
            "commit",
            "--allow-empty",
            "-m",
            "seed",
        ],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_git_show_missing_reference_reports_real_exit_code(tmp_path):
    """旧反例必须消除：git show 不存在的引用要有 ok=False + 退出码语义 + 保留原因输出。"""
    _seed_repo(tmp_path)
    res = await _program_tool().run(
        program="git", args=["show", "no-such-ref-sr-b"], cwd=str(tmp_path)
    )
    assert res.ok is False
    match = re.search("退出码 ([0-9]+)", res.error or "")
    assert match, f"error 里要有退出码语义，实际：{res.error!r}"
    assert int(match.group(1)) == 128
    # content 保留合并输出：模型要能看到失败原因（退出码与引用名都可定位）
    assert res.content and "no-such-ref-sr-b" in res.content


@pytest.mark.asyncio
async def test_exit_zero_with_stderr_is_success():
    """退出码 0 → ok=True；stderr 非空不算失败，但要保留在合并输出里。"""
    res = await _program_tool().run(
        program=sys.executable,
        args=["-c", "import sys; print('SR_B_OUT'); print('SR_B_ERR', file=sys.stderr)"],
    )
    assert res.ok is True
    assert "SR_B_OUT" in (res.content or "")
    assert "SR_B_ERR" in (res.content or "")


@pytest.mark.asyncio
async def test_exit_three_without_output_is_failure():
    res = await _program_tool().run(
        program=sys.executable, args=["-c", "import sys; sys.exit(3)"]
    )
    assert res.ok is False
    assert "退出码 3" in (res.error or "")


@pytest.mark.asyncio
async def test_exit_nonzero_keeps_merged_output():
    """ok=False 也要把 stdout+stderr 合并输出留给模型（模型要看失败原因）。"""
    res = await _program_tool().run(
        program=sys.executable,
        args=[
            "-c",
            "import sys; print('SR_B_OUT'); print('SR_B_ERR', file=sys.stderr); sys.exit(3)",
        ],
    )
    assert res.ok is False
    assert "退出码 3" in (res.error or "")
    assert "SR_B_OUT" in (res.content or "")
    assert "SR_B_ERR" in (res.content or "")


@pytest.mark.asyncio
async def test_shell_nonzero_exit_is_failure():
    tool = RunCmdTool()
    tool.computer = _AutoAccept()
    tool.approvals = object()
    res = await tool.run(cmd="exit /b 7")
    assert res.ok is False
    assert "退出码 7" in (res.error or "")


@pytest.mark.asyncio
async def test_startup_failure_reports_clearly_and_never_enters_cleanup(monkeypatch):
    """启动失败要如实报错；proc 没建立就绝不进入清理流程。"""
    proc_cleanup = pytest.importorskip(
        "agent.tools.proc_cleanup", reason="契约 2 新增 proc_cleanup 模块"
    )
    cleanup_calls = []

    async def _spy(proc):
        cleanup_calls.append(proc)
        return True

    monkeypatch.setattr(proc_cleanup, "terminate_process_tree", _spy)
    res = await _program_tool().run(program="missing-program-sr-b.exe", args=[])
    assert res.ok is False
    assert "找不到程序" in (res.error or "")
    assert cleanup_calls == []
