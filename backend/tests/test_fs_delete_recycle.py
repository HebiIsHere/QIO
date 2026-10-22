"""删除边界（契约 §0.8）：fs_delete 进系统回收站；命令行永久删除一律拒绝。

覆盖：
* restricted 下删除用户文件 → APPROVE 待审批、未删除；批准后执行器进系统回收站且原路径消失；
* 回收站失败/不可用 → 文件保留、如实报告；
* QIO 生成文件（引擎 allow）→ 直接进回收站；
* 只读模式 → 中文拒绝且文件保留；
* run_program / run_shell 的 rm/del/erase/rd/rmdir/Remove-Item 等一律拒绝（所有模式）。
"""

from __future__ import annotations

import types

import pytest

from agent.security.effects import AuthDecision, Verdict
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.computer import ComputerSandbox
from agent.tools import security_gate
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool
from agent.tools.fs_tools import FsDeleteTool


class FakeGateway:
    """轻量 fake gateway：记录 effect / 返回预设 verdict（不复制权限判定）。"""

    def __init__(self, mode="restricted", verdicts=None, reasons=None) -> None:
        self._mode = mode
        self.verdicts = dict(verdicts or {})
        self.reasons = dict(reasons or {})
        self.effects: list = []
        self.submitted: list = []

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        kind = str(getattr(getattr(effect, "kind", None), "value", effect.kind))
        return AuthDecision(
            verdict=self.verdicts.get(kind, Verdict.ALLOW),
            reason=self.reasons.get(kind, f"fake:{kind}"),
        )

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_delete_fake",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )


def _sandbox(root) -> ComputerSandbox:
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "restricted"
    )


def _ctx(operation, *, mode="restricted") -> ExecutionContext:
    return ExecutionContext(
        approval_id="appr_delete_fake",
        operation=operation,
        mode_at_request=mode,
        mode_at_approval=mode,
    )


@pytest.mark.asyncio
async def test_delete_needs_approval_then_executor_recycles(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    victim = root / "用户文件.txt"
    victim.write_text("要删除的内容", encoding="utf-8")
    gateway = FakeGateway(verdicts={"delete": Verdict.APPROVE})
    tool = FsDeleteTool(computer=_sandbox(root), security=gateway)

    result = await tool.run(paths=[str(victim)])

    assert result.ok is True and result.code == "pending_approval"
    assert victim.exists(), "待审批时不得先删除"
    assert len(gateway.submitted) == 1
    operation = gateway.submitted[0]
    assert operation.kind == "fs.delete"
    assert operation.payload["paths"][0]["path"] == str(victim)
    assert operation.payload["paths"][0]["identity"], "审批单必须带对象身份指纹"

    # 模拟用户批准后由审批执行器真正执行（不是普通工具调用）
    from agent.tools.approved_operations import execute_fs_delete

    exec_result = await execute_fs_delete(operation.payload, _ctx(operation))

    assert exec_result.ok is True, exec_result.error
    assert not victim.exists(), "批准后必须真的移入系统回收站"
    assert exec_result.detail.get("deleted_permanently") is False


@pytest.mark.asyncio
async def test_delete_failure_keeps_file(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    victim = root / "保留我.txt"
    victim.write_text("必须保留", encoding="utf-8")
    gateway = FakeGateway()  # ALLOW
    tool = FsDeleteTool(computer=_sandbox(root), security=gateway)

    from agent.services import recycle_bin as recycle_module

    monkeypatch.setattr(
        recycle_module,
        "send_to_recycle_bin",
        lambda path: {
            "ok": False,
            "path": str(path),
            "mechanism": "none",
            "reason": "模拟回收站失败",
            "deleted_permanently": False,
        },
    )
    result = await tool.run(paths=[str(victim)])

    assert result.ok is False
    assert "未删除" in (result.error or "") or "没有移入系统回收站" in (result.error or "")
    assert victim.exists(), "回收站失败时必须保留文件"
    assert (result.data or {}).get("deleted_permanently") is False


@pytest.mark.asyncio
async def test_qio_generated_file_can_be_deleted_directly(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    generated = root / "qio-generated.txt"
    generated.write_text("QIO 生成的工作文件", encoding="utf-8")
    gateway = FakeGateway()  # ALLOW：模拟 QIO 生成文件自动允许
    tool = FsDeleteTool(computer=_sandbox(root), security=gateway)

    result = await tool.run(paths=["qio-generated.txt"])  # 相对路径按工作区根解析

    assert result.ok is True, result.error
    assert not generated.exists()
    assert gateway.submitted == [], "allow 时不得再提交审批"
    assert len(gateway.effects) == 1
    assert str(getattr(gateway.effects[0].kind, "value", gateway.effects[0].kind)) == "delete"


@pytest.mark.asyncio
async def test_read_only_mode_refuses_delete(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    victim = root / "只读里的文件.txt"
    victim.write_text("不能删", encoding="utf-8")
    gateway = FakeGateway(
        mode="read_only",
        verdicts={"delete": Verdict.DENY, "internal_data": Verdict.DENY},
        reasons={"delete": "只读模式下 QIO 不能修改业务文件"},
    )
    tool = FsDeleteTool(computer=_sandbox(root), security=gateway)

    result = await tool.run(paths=[str(victim)])

    assert result.ok is False
    assert "只读" in (result.error or "")
    assert victim.exists()


@pytest.mark.asyncio
async def test_fs_delete_requires_at_least_one_path(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    tool = FsDeleteTool(computer=_sandbox(root), security=FakeGateway())

    result = await tool.run(paths=[])

    assert result.ok is False
    assert "paths" in (result.error or "")


# --------------------------------------------------------------------------
# 命令行永久删除一律拒绝（所有模式）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "program,args",
    [
        ("rm", ["-rf", "/tmp/x"]),
        ("del", ["/f", "/s", "/q", "x.txt"]),
        ("erase", ["x.txt"]),
        ("rd", ["/s", "/q", "somedir"]),
        ("rmdir", ["somedir"]),
        ("cmd", ["/c", "del", "x.txt"]),
        ("powershell", ["-Command", "Remove-Item", "x.txt"]),
    ],
)
async def test_run_program_refuses_permanent_delete(tmp_path, program, args):
    target = tmp_path / "x.txt"
    target.write_text("keep", encoding="utf-8")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=FakeGateway())

    result = await tool.run(program=program, args=args)

    assert result.ok is False
    assert "不通过命令行永久删除文件" in (result.error or "")
    assert result.code == "permanent_delete_blocked"
    assert target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /tmp/x",
        "del /f /s /q x.txt",
        "erase x.txt",
        "rd /s /q somedir",
        "rmdir somedir",
        "Remove-Item x.txt",
        "powershell -Command Remove-Item x.txt",
    ],
)
async def test_run_shell_refuses_permanent_delete(tmp_path, cmd):
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=FakeGateway())

    result = await tool.run(cmd=cmd)

    assert result.ok is False
    assert "不通过命令行永久删除文件" in (result.error or "")
    assert result.code == "permanent_delete_blocked"


@pytest.mark.asyncio
async def test_non_delete_commands_are_not_blocked(tmp_path):
    """拒绝只针对永久删除：普通只读命令不受影响。"""
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=FakeGateway())
    result = await tool.run(cmd="echo hi")
    assert result.code != "permanent_delete_blocked"
