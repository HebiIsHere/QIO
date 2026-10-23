"""R9-G W2：cmd_tools 固定查询 / 永久删除 / 审批绑定 / 执行前重核对。

验证真实工具路径（不是只测纯函数）：
* 固定查询靠 fixed_queries 单一来源，effect.detail 携带可从引擎重算的真实执行事实；
* 三种模式的固定查询/永久删除政策按契约 §8 执行；
* 永久删除 restricted 必须有明确人工确认（模型 ALLOW 不可替代），read_only 拒绝，
  unrestricted 允许；内部数据三次确认优先；
* 审批 payload/fingerprint 绑定 requirement + OperationBinding（§7 字段）；
* proc.* 重放执行前重核对（要求版本/状态、模式变化、永久删除对象身份、注入式
  RecheckService），不一致 → invalidated 且不执行。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from agent.security import fixed_queries
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.computer import ComputerSandbox
from agent.tools import security_gate
from agent.tools.approved_operations import (
    execute_proc_run_program,
    execute_proc_shell,
)
from agent.tools.cmd_tools import RunCmdTool, RunProgramTool
from agent.tools.security_types import AuthDecision, Verdict

PY = sys.executable
WINDOWS = sys.platform == "win32"


# --------------------------------------------------------------------------
# 轻量替身（模型调用不涉及；这里是引擎/审批/台账接口替身）
# --------------------------------------------------------------------------


class FakeGateway:
    """记录 effect / 提交操作，返回预设 verdict（不复制权限判定）。"""

    def __init__(self, *, mode="restricted", verdict="allow") -> None:
        self._mode = mode
        self.verdict = verdict
        self.effects: list = []
        self.submitted: list = []
        self.internal_data_paths: list = []

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    def current_requirement_binding(self):
        return {"requirement_id": "req_test", "requirement_version": 1, "source": "binding"}

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        verdict = self.verdict(effect) if callable(self.verdict) else self.verdict
        return AuthDecision(verdict=Verdict(verdict), reason="fake:" + str(verdict))

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_r9g",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )


class FakeLedger:
    def __init__(self, *, status: str = "active", version: int = 1) -> None:
        self.status = status
        self.version = version

    def by_id(self, requirement_id: str):
        if requirement_id != "req_test":
            return None
        return types.SimpleNamespace(
            requirement_id=requirement_id, status=self.status, version=self.version
        )


class FakeEngine:
    def __init__(self, ledger: FakeLedger | None = None) -> None:
        self.requirements = ledger or FakeLedger()


class FakeRecheckService:
    def __init__(self, *, ok: bool = True, code: str = "", reason: str = "") -> None:
        self.ok = ok
        self.code = code
        self.reason = reason
        self.calls: list = []

    async def recheck(self, binding, *, effect):
        self.calls.append({"binding": dict(binding or {}), "effect": dict(effect or {})})
        return types.SimpleNamespace(ok=self.ok, code=self.code, reason=self.reason)


def _sandbox(root) -> ComputerSandbox:
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "restricted"
    )


def _mode_getter(mode: str):
    return lambda: mode


def _replay_ctx(
    operation: PendingOperation,
    *,
    mode: str = "restricted",
    ledger: FakeLedger | None = None,
    recheck=None,
) -> ExecutionContext:
    ctx = ExecutionContext(
        approval_id="appr_r9g",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=FakeEngine(ledger),
        mode_getter=_mode_getter(mode),
    )
    if recheck is not None:
        ctx.recheck = recheck  # type: ignore[attr-defined] - 契约 §7 注入点
    return ctx


def _delete_command(path) -> str:
    return ("del /f /q " + str(path)) if WINDOWS else ("rm -f " + str(path))


async def _submit(tool, **kwargs) -> PendingOperation:
    result = await tool.run(**kwargs)
    assert result.ok is True and result.code == "pending_approval", result
    submitted = tool.security.submitted
    assert submitted, "必须提交待审批操作"
    return submitted[-1]


# --------------------------------------------------------------------------
# 固定查询：唯一来源 + 三种模式
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_query_effect_detail_can_be_reclassified_by_engine(tmp_path):
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(program=PY, args=["--version"], cwd=str(tmp_path), timeout=60)

    assert result.ok is True, result.error
    assert len(gateway.effects) == 1
    effect = gateway.effects[0]
    execution = dict(effect.detail.get("execution") or {})
    assert execution.get("program_path")
    assert execution.get("program_name")
    assert execution.get("shell") is False
    assert len(str(execution.get("spec_digest"))) == 64
    # 引擎侧用同一份 facts + 分类器重新判定（不采信工具自报）
    facts = fixed_queries.fixed_query_facts_from_detail(effect.detail)
    assert facts is not None
    classified = fixed_queries.classify_fixed_query(facts)
    assert classified is not None and classified.kind == "version"
    assert "fixed_query" not in effect.detail


@pytest.mark.asyncio
async def test_read_only_allows_fixed_query_and_denies_other_without_engine_call(tmp_path):
    gateway = FakeGateway(mode="read_only", verdict="allow")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)

    allowed = await tool.run(program=PY, args=["--version"], cwd=str(tmp_path), timeout=60)
    assert allowed.ok is True, allowed.error
    effects_after_fixed = len(gateway.effects)

    denied = await tool.run(
        program=PY, args=["-c", "print(1)"], cwd=str(tmp_path), timeout=60
    )
    assert denied.ok is False
    assert denied.code == "readonly_execution_denied"
    assert len(gateway.effects) == effects_after_fixed, "非固定查询在引擎判定前就被执行层拒绝"


@pytest.mark.asyncio
async def test_restricted_fixed_query_does_not_submit_approval(tmp_path):
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(program=PY, args=["--version"], cwd=str(tmp_path), timeout=60)

    assert result.ok is True, result.error
    assert gateway.submitted == [], "固定查询在受限模式不弹确认（引擎按分类器 ALLOW）"


@pytest.mark.asyncio
async def test_effect_detail_has_no_trusted_fixed_query_claim(tmp_path):
    gateway = FakeGateway(mode="restricted", verdict="approve")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd="echo hello")
    assert "fixed_query" not in operation.effect.get("detail", {})
    assert "fixed_query" not in (operation.payload or {})


# --------------------------------------------------------------------------
# 永久删除：三种模式 + 明确人工确认 + 内部数据优先级
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restricted_permanent_delete_requires_explicit_confirmation(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")  # 模型 ALLOW 也不能替代
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(victim))

    assert result.ok is True and result.code == "pending_approval"
    operation = gateway.submitted[-1]
    payload = dict(operation.payload)
    assert payload.get("explicit_confirmation_required") is True
    assert payload.get("permanent_delete", {}).get("permanent") is True
    assert payload.get("recycle_bin") is False
    assert payload.get("irreversible") is True
    assert "永久删除" in (operation.label or "")
    fingerprint = dict(operation.fingerprint)
    assert fingerprint.get("explicit_confirmation_required") is True
    delete_binding = dict(fingerprint.get("permanent_delete") or {})
    assert delete_binding.get("identity_set"), "审批指纹必须绑定具体对象身份"
    assert any(str(victim) in str(item[0]) for item in delete_binding["identity_set"])
    assert victim.exists(), "待确认时不得删除"


@pytest.mark.asyncio
async def test_read_only_permanent_delete_denied(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    gateway = FakeGateway(mode="read_only", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(victim))

    assert result.ok is False
    assert result.code == "readonly_execution_denied"
    assert victim.exists()
    assert gateway.submitted == []


@pytest.mark.asyncio
async def test_internal_data_three_confirmations_priority(tmp_path):
    internal = tmp_path / "qio-data"
    internal.mkdir()
    victim = internal / "qio.db"
    victim.write_text("db", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    gateway.internal_data_paths = [internal]
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    result = await tool.run(cmd=_delete_command(victim))

    assert result.ok is True and result.code == "pending_approval"
    assert len(gateway.submitted) == 1, "三次确认优先，不叠加第四次确认"
    operation = gateway.submitted[0]
    assert operation.confirmations_required == 3
    assert operation.payload.get("permanent_delete", {}).get("permanent") is True
    assert "永久删除" in (operation.label or "")
    assert victim.exists()


@pytest.mark.asyncio
async def test_discussion_text_does_not_bind_permanent_delete(tmp_path):
    gateway = FakeGateway(mode="restricted", verdict="approve")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)

    operation = await _submit(tool, cmd="echo rm -rf /tmp/x")

    assert not operation.payload.get("permanent_delete")
    assert not operation.fingerprint.get("explicit_confirmation_required")
    assert not operation.fingerprint.get("delete_intent")


# --------------------------------------------------------------------------
# 审批绑定：requirement + OperationBinding（§7）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_payload_and_fingerprint_bind_requirement_and_operation(tmp_path):
    gateway = FakeGateway(mode="restricted", verdict="approve")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)

    operation = await _submit(
        tool, program=PY, args=["-c", "print(1)"], cwd=str(tmp_path), timeout=60
    )

    payload = dict(operation.payload)
    assert payload["requirement_binding"]["requirement_id"] == "req_test"
    assert payload["requirement_binding"]["requirement_version"] == 1
    assert payload["operation_kind"] == "proc.run_program"
    assert payload["execution_facts"]["program_path"]
    fingerprint = dict(operation.fingerprint)
    requirement = dict(fingerprint.get("requirement") or {})
    assert requirement.get("requirement_id") == "req_test"
    assert requirement.get("requirement_version") == 1
    binding = dict(fingerprint.get("operation_binding") or {})
    assert binding.get("requirement") == {"requirement_id": "req_test", "version": 1}
    assert binding.get("mode") == "restricted"
    assert binding.get("operation_kind") == "proc.run_program"
    assert binding.get("fingerprint")
    assert len(str(binding.get("spec_digest"))) == 64
    assert isinstance(binding.get("content_digests"), list) and binding["content_digests"]
    assert binding.get("conditions") == []
    assert binding.get("authorization_id") is None
    assert binding.get("delegation") is None
    assert binding.get("permanent_delete") is None
    assert fingerprint.get("payload_digest")


# --------------------------------------------------------------------------
# 永久删除重放：执行前重核对
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_delete_replay_executes_after_explicit_approval(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("delete me", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd=_delete_command(victim))
    ctx = _replay_ctx(operation)

    result = await execute_proc_shell(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is True, (result.error, result.summary)
    assert not victim.exists(), "明确人工确认后应按审批绑定执行删除"
    assert result.detail["permit"]["source"] == "approval"
    recheck = dict(result.detail.get("recheck") or {})
    assert recheck.get("mode") == "local_fallback"
    assert "delete_confirmation_binding" in (recheck.get("checks") or [])


@pytest.mark.asyncio
async def test_permanent_delete_replay_invalidates_when_object_changes(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd=_delete_command(victim))
    # 审批之后把对象换成一个目录：对象身份/类型变化 → 旧确认失效
    victim.unlink()
    victim.mkdir()
    ctx = _replay_ctx(operation)

    result = await execute_proc_shell(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "身份" in (result.summary or "") or "对象集合" in (result.summary or "")
    assert victim.exists() and victim.is_dir()


@pytest.mark.asyncio
async def test_permanent_delete_replay_invalidates_when_requirement_version_changes(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd=_delete_command(victim))
    ctx = _replay_ctx(operation, ledger=FakeLedger(status="active", version=2))

    result = await execute_proc_shell(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "版本" in (result.summary or "")
    assert victim.exists()


@pytest.mark.asyncio
async def test_permanent_delete_replay_invalidates_when_requirement_withdrawn(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd=_delete_command(victim))
    ctx = _replay_ctx(operation, ledger=FakeLedger(status="superseded"))

    result = await execute_proc_shell(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert victim.exists()


@pytest.mark.asyncio
async def test_replay_invalidates_legacy_approval_without_requirement_binding(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd=_delete_command(victim))
    # 模拟 R9-G 之前的历史审批：没有 requirement 绑定字段
    operation.fingerprint.pop("requirement", None)
    operation.fingerprint.pop("operation_binding", None)
    ctx = _replay_ctx(operation)

    result = await execute_proc_shell(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False
    assert "要求" in (result.summary or "")
    assert victim.exists(), "历史审批缺绑定不得沿用旧许可执行"


@pytest.mark.asyncio
async def test_replay_invalidates_when_unrestricted_switched_back_to_restricted(tmp_path):
    gateway = FakeGateway(mode="unrestricted", verdict="approve")
    tool = RunProgramTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, program=PY, args=["-c", "print(1)"], cwd=str(tmp_path))
    ctx = _replay_ctx(operation, mode="restricted")

    result = await execute_proc_run_program(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )

    assert result.ok is False
    assert result.detail.get("invalidated") is True
    assert "模式" in (result.summary or "")


@pytest.mark.asyncio
async def test_replay_uses_injected_recheck_service(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("x", encoding="utf-8")
    gateway = FakeGateway(mode="restricted", verdict="allow")
    tool = RunCmdTool(computer=_sandbox(tmp_path), security=gateway)
    operation = await _submit(tool, cmd=_delete_command(victim))

    rejecting = FakeRecheckService(ok=False, code="requirement_revoked", reason="要求已撤回")
    ctx = _replay_ctx(operation, recheck=rejecting)
    rejected = await execute_proc_shell(
        operation.payload, ctx, computer=_sandbox(tmp_path), security=None
    )
    assert rejected.ok is False
    assert rejected.detail.get("invalidated") is True
    assert "要求已撤回" in (rejected.summary or "")
    assert rejecting.calls and rejecting.calls[0]["binding"]
    assert victim.exists(), "重核对失败不得执行"

    approving = FakeRecheckService(ok=True)
    ctx2 = _replay_ctx(operation, recheck=approving)
    allowed = await execute_proc_shell(
        operation.payload, ctx2, computer=_sandbox(tmp_path), security=None
    )
    assert allowed.ok is True, (allowed.error, allowed.summary)
    assert allowed.detail["recheck"]["mode"] == "service"
    assert not victim.exists()
