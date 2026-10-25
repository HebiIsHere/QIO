"""R9-G W3：文件工具的要求绑定 + 执行前重核对（契约 §3、§7）。

真实 FsWriteTool/FsPatchTool/FsDeleteTool/FsReadTool + 真实临时文件；
fake 网关只记录 effect / 返回预设 verdict（接口级），不冒充端到端引擎。
覆盖：
* effect.detail 带**后端核实**的 requirement 绑定，模型参数无法注入；
* 没有绑定时如实标注 unavailable，不回退「最近一条要求」；
* 两次操作使用不同绑定不串用；审批单携带同一绑定；
* 执行前重核对：判定后授权变化/模式变化 → 不执行、保留文件、明确状态。
"""

from __future__ import annotations

import contextvars
import os

import pytest

from agent.security import context as security_context
from agent.security.effects import AuthDecision, Verdict
from agent.services.computer import ComputerSandbox
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsWriteTool

BINDING_VAR_NAME = "current_requirement_binding"


class ScriptedGateway:
    """顺序返回 verdict 的 fake 网关；记录 effect（含 detail.requirement_binding）。"""

    def __init__(self, verdicts: list[str] | None = None, mode: str = "restricted") -> None:
        self.verdicts = list(verdicts or ["allow"])
        self._mode = mode
        self.effects: list = []
        self.submitted: list = []
        self._calls = 0

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        index = min(self._calls, len(self.verdicts) - 1)
        self._calls += 1
        verdict = {
            "allow": Verdict.ALLOW,
            "approve": Verdict.APPROVE,
            "deny": Verdict.DENY,
        }[self.verdicts[index]]
        return AuthDecision(verdict=verdict, reason=f"fake:{self.verdicts[index]}")

    async def submit(self, operation, *, ctx=None):
        import types

        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_test_1",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=int(getattr(operation, "confirmations_required", 1) or 1),
        )


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _sandbox(root):
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "restricted"
    )


@pytest.fixture()
def binding_ctx(monkeypatch):
    var: contextvars.ContextVar = contextvars.ContextVar(BINDING_VAR_NAME, default=None)
    # Lead 的 security/context.py 尚未合入 RequirementBinding contextvar 时，
    # 用同名 contextvar 模拟接线后的读取点（工具只按接口名读取）。
    monkeypatch.setattr(security_context, BINDING_VAR_NAME, var, raising=False)
    return var


def test_write_effect_carries_backend_verified_requirement_binding(workspace, binding_ctx):
    binding = {"requirement_id": "req_alpha", "version": 3}
    token = binding_ctx.set(binding)
    gateway = ScriptedGateway()
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    try:
        import asyncio

        result = asyncio.run(tool.run(path="报告.md", content="内容"))
    finally:
        binding_ctx.reset(token)
    assert result.ok, result.error
    assert gateway.effects, "必须经过统一授权网关"
    detail = gateway.effects[0].detail
    assert detail["requirement_binding"] == binding, detail
    assert detail["requirement_binding_source"] == "security.context"


def test_model_cannot_inject_requirement_binding(workspace, binding_ctx):
    """模型参数里的 requirement_binding 不被采用；只认后端 context。"""
    gateway = ScriptedGateway()
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    import asyncio

    result = asyncio.run(
        tool.run(
            path="被注入.md",
            content="x",
            requirement_binding={"requirement_id": "req_forged", "version": 1},
            requirement="req_forged",
        )
    )
    assert result.ok, result.error
    detail = gateway.effects[0].detail
    assert detail["requirement_binding"] is None, "没有后端绑定时不得采信模型字段"
    assert detail["requirement_binding_source"] == "unavailable"
    assert "req_forged" not in str(detail)


def test_two_requirements_do_not_cross_use(workspace, binding_ctx):
    gateway = ScriptedGateway()
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    import asyncio

    first = {"requirement_id": "req_one", "version": 1}
    second = {"requirement_id": "req_two", "version": 2}
    token1 = binding_ctx.set(first)
    try:
        assert asyncio.run(tool.run(path="一.md", content="one")).ok
    finally:
        binding_ctx.reset(token1)
    token2 = binding_ctx.set(second)
    try:
        assert asyncio.run(tool.run(path="二.md", content="two")).ok
    finally:
        binding_ctx.reset(token2)
    bindings = [effect.detail["requirement_binding"] for effect in gateway.effects]
    # 每个文件一次判定 + 一次执行前重核对；各自绑定自己的 requirement
    assert bindings[0] == first and bindings[1] == first
    assert bindings[2] == second and bindings[3] == second


def test_pending_operation_carries_same_binding(workspace, binding_ctx):
    binding = {"requirement_id": "req_approve", "version": 4}
    token = binding_ctx.set(binding)
    gateway = ScriptedGateway(["approve"])
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    try:
        import asyncio

        result = asyncio.run(tool.run(path="要批准的.md", content="x"))
    finally:
        binding_ctx.reset(token)
    assert result.code == "pending_approval", result
    assert len(gateway.submitted) == 1
    operation = gateway.submitted[0]
    assert operation.payload["requirement_binding"] == binding
    assert operation.fingerprint["requirement_binding"] == binding


def test_execution_recheck_failure_does_not_write(workspace):
    # 第一次 allow（工具被允许），执行前重核对时改为 deny（等待期间授权失效）
    gateway = ScriptedGateway(["allow", "deny"])
    target = workspace / "不许写.txt"
    target.write_text("原内容", encoding="utf-8")
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    import asyncio

    result = asyncio.run(tool.run(path="不许写.txt", content="覆盖内容"))
    assert result.ok is False
    assert result.code == "recheck_denied", (result.code, result.error)
    assert target.read_text(encoding="utf-8") == "原内容", "重核对失败不得写入"


def test_execution_recheck_mode_change_requires_decision(workspace):
    # 判定 allow；执行前重核对时网关返回 approve（模式/要求变化需要用户重新确认）
    gateway = ScriptedGateway(["allow", "approve"])
    target = workspace / "要重新确认.txt"
    target.write_text("原内容", encoding="utf-8")
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    import asyncio

    result = asyncio.run(tool.run(path="要重新确认.txt", content="新内容"))
    assert result.code == "pending_approval", (result.code, result.error)
    assert target.read_text(encoding="utf-8") == "原内容", "重新确认前不得写入"


def test_patch_recheck_failure_keeps_original(workspace):
    # H5 §3：patch 先读授权（allow），再写授权（allow），最后执行前重核对（deny）。
    gateway = ScriptedGateway(["allow", "allow", "deny"])
    target = workspace / "补丁.txt"
    target.write_text("OLD", encoding="utf-8")
    tool = FsPatchTool(computer=_sandbox(workspace), security=gateway)
    import asyncio

    result = asyncio.run(tool.run(path="补丁.txt", old="OLD", new="NEW"))
    assert result.ok is False and result.code == "recheck_denied"
    assert target.read_text(encoding="utf-8") == "OLD"
