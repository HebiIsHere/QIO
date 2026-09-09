from __future__ import annotations

import pytest

from agent.services.computer import CommandRisk
from agent.tools.cmd_tools import RunCmdTool, SysInfoTool, ProcListTool, ProcKillTool


class _FakeSandbox:
    def __init__(self, verdict="approve", mode="default") -> None:
        self._verdict = verdict
        self._mode = mode

    def classify_command(self, cmd: str) -> CommandRisk:
        return CommandRisk.LOW if cmd == "echo hi" else CommandRisk.HIGH

    def command_verdict(self, cmd: str):
        risk = self.classify_command(cmd)
        verdict = self._verdict
        return verdict, risk

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
async def test_run_cmd_low_auto():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="auto")
    t.approvals = _FakeApproval()
    res = await t.run(cmd="echo hi")
    assert res.ok
    assert "hi" in res.content


@pytest.mark.asyncio
async def test_run_cmd_high_needs_approval():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="approve")
    appr = _FakeApproval()
    t.approvals = appr
    res = await t.run(cmd="echo hi")  # fake sandbox returns HIGH for non-"echo hi"
    # HIGH 命令应走审批；fake 审批 approved → 执行
    assert res.ok
    assert len(appr.calls) == 1


@pytest.mark.asyncio
async def test_run_cmd_rejected():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="approve")
    t.approvals = _FakeApproval(decision="rejected")
    res = await t.run(cmd="rm -rf /tmp/x")
    assert not res.ok
    assert "未获批准" in res.error


@pytest.mark.asyncio
async def test_run_cmd_plan_mode_denied():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="deny", mode="plan")
    t.approvals = _FakeApproval()
    res = await t.run(cmd="rm -rf /tmp/x")
    assert not res.ok


@pytest.mark.asyncio
async def test_run_cmd_missing_cmd():
    t = RunCmdTool()
    t.computer = _FakeSandbox(verdict="auto")
    t.approvals = _FakeApproval()
    res = await t.run()
    assert not res.ok
    assert "必填" in res.error


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
