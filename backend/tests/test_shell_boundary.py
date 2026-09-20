"""shell 边界：安全判断的对象必须和实际执行的对象一致。

回归的是这个真实缺陷：`run_cmd` 用字符串前缀判断「这是 ls，低危」，
然后把**整条字符串**交给系统 shell 执行 —— `ls && evil` 于是被自动放行。
"""

from __future__ import annotations

import pytest

from agent.services.computer import CommandRisk, ComputerSandbox
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool


def _sandbox(root, mode="default") -> ComputerSandbox:
    return ComputerSandbox(resolve_root=lambda: str(root), permission_mode=lambda: mode)


class _Approval:
    def __init__(self, decision="approved") -> None:
        self.decision = decision
        self.calls: list[tuple[str, dict]] = []

    async def request(self, kind: str, payload: dict):
        self.calls.append((kind, payload))
        return type("R", (), {"decision": self.decision})()


@pytest.mark.parametrize(
    "cmd",
    [
        "ls && echo hacked",
        "git status; echo hacked",
        "pwd | tee /tmp/x",
        "git status && rm -rf /tmp/x",
        "echo $(whoami)",
        "cat /etc/passwd > /tmp/leak",
        "ls `whoami`",
    ],
)
def test_shell_chaining_is_never_low_risk(tmp_path, cmd):
    sandbox = _sandbox(tmp_path)
    assert sandbox.classify_command(cmd) is CommandRisk.DANGER
    # 而且自由 shell 一律走审批，不做「看起来安全就放行」
    assert sandbox.shell_verdict(cmd)[0] == "approve"


def test_plain_commands_still_classified(tmp_path):
    sandbox = _sandbox(tmp_path)
    assert sandbox.classify_command("git status") is CommandRisk.LOW
    assert sandbox.classify_command("ls -la") is CommandRisk.LOW
    assert sandbox.classify_command("pwd") is CommandRisk.LOW
    assert sandbox.classify_command("pip install requests") is CommandRisk.HIGH


@pytest.mark.asyncio
async def test_run_shell_always_asks_even_for_low_risk_looking_command(tmp_path):
    tool = RunCmdTool()
    tool.computer = _sandbox(tmp_path)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(cmd="ls")
    assert res.ok is False
    assert len(approval.calls) == 1, "shell 命令必须每次都请求审批"
    assert approval.calls[0][1]["action"] == "run_shell"


@pytest.mark.asyncio
async def test_run_shell_injection_is_not_auto_executed(tmp_path):
    tool = RunCmdTool()
    tool.computer = _sandbox(tmp_path)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(cmd="ls && echo hacked")
    assert res.ok is False
    assert len(approval.calls) == 1
    assert approval.calls[0][1]["risk"] == CommandRisk.DANGER.value


@pytest.mark.asyncio
async def test_run_shell_denied_in_plan_mode(tmp_path):
    tool = RunCmdTool()
    tool.computer = _sandbox(tmp_path, mode="plan")
    approval = _Approval()
    tool.approvals = approval

    res = await tool.run(cmd="ls")
    assert res.ok is False
    assert not approval.calls


@pytest.mark.asyncio
async def test_run_program_readonly_runs_without_shell(tmp_path):
    tool = RunProgramTool()
    tool.computer = _sandbox(tmp_path)
    approval = _Approval()
    tool.approvals = approval

    # `git --version` 是跨平台可用的只读程序（pwd/echo 在 Windows 上是 shell 内建）
    res = await tool.run(program="git", args=["--version"])
    assert res.ok is True
    assert "git" in (res.content or "").lower()
    assert not approval.calls, "只读程序白名单内可以直接执行"


@pytest.mark.asyncio
async def test_run_program_write_commands_need_approval(tmp_path):
    tool = RunProgramTool()
    tool.computer = _sandbox(tmp_path)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(program="git", args=["commit", "-m", "x"])
    assert res.ok is False
    assert len(approval.calls) == 1, "git commit 不在只读子命令白名单里"


@pytest.mark.asyncio
async def test_run_program_rejects_metacharacter_arguments(tmp_path):
    tool = RunProgramTool()
    tool.computer = _sandbox(tmp_path)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(program="grep", args=["-E", "a|b"])
    assert res.ok is False
    assert len(approval.calls) == 1


@pytest.mark.asyncio
async def test_run_program_unknown_program_needs_approval(tmp_path):
    tool = RunProgramTool()
    tool.computer = _sandbox(tmp_path)
    approval = _Approval(decision="rejected")
    tool.approvals = approval

    res = await tool.run(program="curl", args=["http://example.com"])
    assert res.ok is False
    assert len(approval.calls) == 1
