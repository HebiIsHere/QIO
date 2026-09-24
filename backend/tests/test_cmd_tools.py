from __future__ import annotations

import pytest

from agent.services.computer import CommandRisk
from agent.tools.cmd_tools import (
    APPROVAL_SLACK_MS,
    ProcKillTool,
    ProcListTool,
    RunCmdTool,
    RunProgramTool,
    SysInfoTool,
)
# 放在 cmd_tools 之后导入：保持这个测试模块「先导入 agent.tools」的顺序不变
from agent.tools.approval import DEFAULT_TIMEOUT_SECONDS


class _FakeSandbox:
    def __init__(self, verdict="approve", mode="default") -> None:
        self._verdict = verdict
        self._mode = mode

    def classify_command(self, cmd: str) -> CommandRisk:
        return CommandRisk.LOW if cmd == "echo hi" else CommandRisk.HIGH

    def classify_argv(self, program: str, args=None) -> CommandRisk:
        return CommandRisk.LOW if program in ("echo", "git") else CommandRisk.HIGH

    def command_verdict(self, cmd: str):
        risk = self.classify_command(cmd)
        verdict = self._verdict
        return verdict, risk

    def shell_verdict(self, cmd: str):
        # run_shell 永远走审批：fake 只在显式 verdict="auto" 时模拟放行
        return self.command_verdict(cmd)

    def command_verdict_for_program(self, program: str, args=None):
        return self._verdict, self.classify_argv(program, args)

    @property
    def mode(self) -> str:
        return self._mode


class _FakeApproval:
    def __init__(self, decision="approved") -> None:
        self._decision = decision
        self.calls: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append((kind, payload))
        return type("R", (), {"decision": self._decision})()


@pytest.mark.asyncio
async def test_run_shell_auto_when_sandbox_allows():
    """sandbox 判 auto 时工具才直接执行（真实 sandbox 下 shell 永远是 approve）。"""
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="auto")
    t.approvals = _FakeApproval()
    res = await t.run(cmd="echo hi")
    assert res.ok
    assert "hi" in res.content


@pytest.mark.asyncio
async def test_run_shell_high_needs_approval():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="approve")
    appr = _FakeApproval()
    t.approvals = appr
    res = await t.run(cmd="echo hi")  # fake sandbox returns HIGH for non-"echo hi"
    # HIGH 命令应走审批；fake 审批 approved → 执行
    assert res.ok
    assert len(appr.calls) == 1


@pytest.mark.asyncio
async def test_run_shell_rejected():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="approve")
    t.approvals = _FakeApproval(decision="rejected")
    res = await t.run(cmd="rm -rf /tmp/x")
    assert not res.ok
    assert "未获批准" in res.error


def test_command_tool_timeout_covers_approval_window():
    """真实事故：run_shell 的工具超时是 45 秒，而审批给用户 5 分钟 ——
    用户还没点确认，工具就已经报「超时（45000 毫秒）」结束。审批窗口必须算进去。"""
    approval_ms = int(DEFAULT_TIMEOUT_SECONDS * 1000)
    assert APPROVAL_SLACK_MS == approval_ms
    assert RunCmdTool.timeout_ms >= approval_ms + 45_000
    assert RunProgramTool.timeout_ms >= approval_ms + 45_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision, expected",
    [
        ("timeout", "审批等待超时"),
        ("rejected", "你点了拒绝"),
        ("cancelled", "本轮已停止"),
    ],
)
async def test_shell_approval_outcome_is_specific(decision, expected):
    """超时 / 拒绝 / 取消必须分开说：以前三种结局都是同一句「未获批准，未执行」。"""
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="approve")
    t.approvals = _FakeApproval(decision=decision)
    res = await t.run(cmd="echo hi")
    assert not res.ok
    assert "未获批准" in res.error
    assert expected in res.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision, expected",
    [
        ("timeout", "审批等待超时"),
        ("rejected", "你点了拒绝"),
        ("cancelled", "本轮已停止"),
    ],
)
async def test_run_program_approval_outcome_is_specific(decision, expected):
    t = RunProgramTool()
    t.computer = _FakeSandbox(verdict="approve")
    t.approvals = _FakeApproval(decision=decision)
    res = await t.run(program="curl", args=["http://example.com"])
    assert not res.ok
    assert "未获批准" in res.error
    assert expected in res.error


@pytest.mark.asyncio
async def test_run_shell_plan_mode_denied():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="deny", mode="plan")
    t.approvals = _FakeApproval()
    res = await t.run(cmd="rm -rf /tmp/x")
    assert not res.ok


@pytest.mark.asyncio
async def test_run_shell_missing_cmd():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="auto")
    t.approvals = _FakeApproval()
    res = await t.run()
    assert not res.ok
    assert "必填" in res.error


@pytest.mark.asyncio
async def test_run_program_uses_argv_not_shell():
    t = RunProgramTool()
    t.computer = _FakeSandbox(verdict="auto")
    appr = _FakeApproval()
    t.approvals = appr
    res = await t.run(program="git", args=["--version"])
    assert res.ok
    assert "git" in res.content.lower()
    assert not appr.calls


@pytest.mark.asyncio
async def test_run_program_needs_approval_for_non_allowlisted_program():
    t = RunProgramTool()
    t.computer = _FakeSandbox(verdict="approve")
    appr = _FakeApproval(decision="rejected")
    t.approvals = appr
    res = await t.run(program="curl", args=["http://example.com"])
    assert not res.ok
    assert len(appr.calls) == 1
    assert appr.calls[0][1]["action"] == "run_program"


@pytest.mark.asyncio
async def test_sys_info_ok():
    t = SysInfoTool()
    t.computer = _FakeSandbox(verdict="auto")
    t.approvals = _FakeApproval()
    res = await t.run()
    assert res.ok
    assert "platform" in res.content


@pytest.mark.asyncio
async def test_proc_list_ok():
    t = ProcListTool()
    t.computer = _FakeSandbox(verdict="auto")
    t.approvals = _FakeApproval()
    res = await t.run()
    assert res.ok


@pytest.mark.asyncio
async def test_proc_kill_approval():
    t = ProcKillTool()
    t.computer = _FakeSandbox(verdict="approve")
    appr = _FakeApproval()
    t.approvals = appr
    res = await t.run(pid="999999")
    assert len(appr.calls) == 1
    # 不存在进程 → 失败但不崩溃
    assert res.ok is False or "pid" in res.error or "不存在" in res.error
