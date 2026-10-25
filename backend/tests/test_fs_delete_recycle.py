"""删除边界（契约 §0.8）：fs_delete 进系统回收站；命令行永久删除一律拒绝。

覆盖：
* restricted 下删除用户文件 → APPROVE 待审批、未删除；批准后执行器进系统回收站且原路径消失；
* 回收站失败/不可用 → 文件保留、如实报告；
* QIO 生成文件（引擎 allow）→ 直接进回收站；
* 只读模式 → 中文拒绝且文件保留；
* 命令行永久删除：read_only 拒绝；restricted 必须有明确人工确认（模型 ALLOW 不可替代）；
  unrestricted 允许；shell 含管道/重定向/串联/嵌套时不视为可解析的永久删除（普通流程）。
"""

from __future__ import annotations

import sys
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
    # R9-G §7：执行前必须重新走一次授权（模式/要求/授权是否仍覆盖），
    # 所以一次删除会有「判定 + 执行前重核对」两次 authorize；两次都必须是 delete。
    assert gateway.effects, "删除必须经过统一授权网关"
    assert all(
        str(getattr(effect.kind, "value", effect.kind)) == "delete"
        for effect in gateway.effects
    ), [getattr(effect.kind, "value", effect.kind) for effect in gateway.effects]


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
# 命令行永久删除（R9-G 契约 §5）：read_only 拒绝 / restricted 明确人工确认 /
# unrestricted 允许；模型 ALLOW 不可替代人工确认；shell 控制字符 → 普通流程
# --------------------------------------------------------------------------


def _delete_command(path) -> str:
    if sys.platform == "win32":
        return "del /f /q " + str(path)
    return "rm -f " + str(path)


def _delete_dir_command(path) -> str:
    if sys.platform == "win32":
        return "rd /s /q " + str(path)
    return "rm -rf " + str(path)


@pytest.mark.asyncio
async def test_restricted_permanent_delete_requires_explicit_confirmation(tmp_path):
    """模型 ALLOW 不能替代明确人工确认：restricted 下永久删除必须提交审批单。"""
    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    gateway = FakeGateway()  # 默认 execute=ALLOW（模拟模型/一般授权允许）
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(target))

    assert result.ok is True and result.code == "pending_approval"
    assert "永久删除" in (result.content or "")
    assert target.exists(), "待确认时不得先删除"
    assert len(gateway.submitted) == 1, "必须提交一条明确的永久删除审批"
    operation = gateway.submitted[0]
    assert operation.payload["explicit_confirmation_required"] is True
    assert operation.payload["permanent_delete"]["permanent"] is True
    assert operation.payload["recycle_bin"] is False
    assert operation.payload["irreversible"] is True
    assert "永久删除" in (operation.label or "")
    assert operation.fingerprint.get("explicit_confirmation_required") is True
    assert operation.fingerprint.get("permanent_delete", {}).get("warning")


@pytest.mark.asyncio
async def test_read_only_permanent_delete_is_denied(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    gateway = FakeGateway(mode="read_only")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(target))

    assert result.ok is False
    assert result.code == "readonly_execution_denied"
    assert target.exists()
    assert gateway.submitted == [], "只读模式不得提交永久删除审批"
    assert gateway.effects == [], "只读模式在执行层就拒绝，不进入引擎判定"


@pytest.mark.asyncio
async def test_unrestricted_permanent_delete_is_allowed(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("delete me", encoding="utf-8")
    gateway = FakeGateway(mode="unrestricted")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(target), timeout=60)

    assert result.ok is True, (result.error, result.content)
    assert not target.exists(), "完全无限制模式允许永久删除"
    assert gateway.submitted == [], "无限制模式不需要额外确认"


@pytest.mark.asyncio
async def test_discussion_text_does_not_trigger_permanent_delete(tmp_path):
    """讨论/引用/输出里出现 rm/del 等文字不改变授权（不全文搜索命令文字）。"""
    gateway = FakeGateway()
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd="echo rm -rf /tmp/x")

    assert result.code != "pending_approval"
    assert gateway.submitted == []
    assert result.ok is True
    assert "rm -rf /tmp/x" in (result.content or "")

    # run_program：argv 里出现 "rm -rf" 字样但程序身份不是删除程序 → 不触发
    program_tool = RunProgramTool(computer=_sandbox(tmp_path), security=FakeGateway())
    program_result = await program_tool.run(program="git", args=["log", "--grep", "rm -rf"])
    assert program_result.code != "pending_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd",
    [
        "echo hi | rm -f x",
        "echo hi > rm.txt",
        "echo hi && rm -f x",
        "echo hi; rm -f x",
    ],
)
async def test_shell_control_chars_are_not_parseable_permanent_delete(tmp_path, cmd):
    """shell 含管道/重定向/串联 → 不视为可解析的永久删除（走普通流程）。"""
    gateway = FakeGateway()
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=cmd)

    assert result.code != "pending_approval", "含控制字符的 shell 不属于可解析的永久删除"
    assert gateway.submitted == []


@pytest.mark.asyncio
async def test_internal_data_three_confirmations_priority_over_permanent_delete(tmp_path):
    """内部运行数据：三次确认优先，不叠加第四次确认。"""
    internal = tmp_path / "qio-data"
    internal.mkdir()
    victim = internal / "qio.db"
    victim.write_text("db", encoding="utf-8")
    gateway = FakeGateway()
    gateway.internal_data_paths = [internal]
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(victim))

    assert result.ok is True and result.code == "pending_approval"
    assert len(gateway.submitted) == 1, "只提交一次审批（三次确认，不叠加第四次）"
    operation = gateway.submitted[0]
    assert operation.confirmations_required == 3
    assert operation.payload.get("permanent_delete", {}).get("permanent") is True
    assert "永久删除" in (operation.label or "")
    assert victim.exists(), "确认前不得删除内部数据"


@pytest.mark.asyncio
async def test_nested_shell_delete_goes_through_normal_flow(tmp_path):
    """run_program 用 shell 程序包装删除（嵌套）：不属可解析的永久删除，走普通流程。"""
    gateway = FakeGateway()
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    if sys.platform == "win32":
        program, args = "cmd", ["/c", "del", str(tmp_path / "missing.txt")]
    else:
        program, args = "sh", ["-c", "rm -f " + str(tmp_path / "missing.txt")]

    result = await tool.run(program=program, args=args, timeout=60)

    assert result.code != "pending_approval"
    assert gateway.submitted == []
