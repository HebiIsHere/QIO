"""R9-H5 回归（续）：人工等待、批量逐项、结构化决策、程序精确匹配。"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import DECISION_UNRESOLVED_LIMITS, AuthDecision, Effect, EffectKind, Target, Verdict
from agent.security.operations import ExecutionContext, PendingOperation
from agent.security.requirements import RequirementBinding
from agent.security import effects as effects_module
from agent.tools import security_gate
from agent.tools.fs_tools import FsDeleteTool, FsWriteTool

from _r9_auth_support import make_security


@pytest.fixture()
def ctx(db_conn, tmp_path):
    return create_app(Settings(data_dir=tmp_path / "data"), db_conn).state.ctx


async def _wait_state(ctx, approval_id: str, states=("completed", "failed", "invalidated"), timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    row = None
    while asyncio.get_running_loop().time() < deadline:
        row = ctx.approvals.store.get(approval_id)
        if row and str(row.get("state")) in states:
            return row
        await asyncio.sleep(0.03)
    return row




async def _batch_scenario(ctx, monkeypatch, *, mutate=None):
    workspace = ctx.computer.root()
    a = workspace / "batch2-a.txt"
    b = workspace / "batch2-b.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="删除两个文件", message_id="m-b2", turn_id="t-b2"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "restricted")
    trash = workspace / "fake-trash2"
    trash.mkdir(exist_ok=True)

    async def fake_move(path):
        source = Path(path)
        os.replace(source, trash / source.name)
        return True, "fake_recycle", ""

    import agent.tools.approved_operations as approved_ops
    monkeypatch.setattr(approved_ops, "_move_to_recycle_bin", fake_move)
    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(a), str(b)])
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    if mutate is not None:
        mutate(requirement, a, b)
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    return row, a, b


async def test_batch_delete_all_valid_both_complete(ctx, monkeypatch):
    row, a, b = await _batch_scenario(ctx, monkeypatch)
    assert row is not None and str(row.get("state")) == "completed", row
    assert not a.exists() and not b.exists()
    detail = row.get("result") or {}
    assert detail.get("partial") is False and detail.get("completed_count") == 2


async def test_batch_delete_all_invalid_both_remain(ctx, monkeypatch):
    def mutate(requirement, a, b):
        ctx.requirements.set_structured_limits(
            requirement.requirement_id,
            [
                {"operations": ["delete"], "objects": [{"kind": "path", "value": str(a)}], "reason": "禁止 A"},
                {"operations": ["delete"], "objects": [{"kind": "path", "value": str(b)}], "reason": "禁止 B"},
            ],
            source="test",
            state=requirement_limits.STATE_STRUCTURED,
        )

    row, a, b = await _batch_scenario(ctx, monkeypatch, mutate=mutate)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert a.exists() and b.exists()


async def test_batch_delete_one_content_changed_only_that_item_invalid(ctx, monkeypatch):
    def mutate(requirement, a, b):
        b.write_text("user-changed", encoding="utf-8")

    row, a, b = await _batch_scenario(ctx, monkeypatch, mutate=mutate)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert not a.exists(), "A 未变化应完成"
    assert b.read_text(encoding="utf-8") == "user-changed"


async def test_batch_delete_common_requirement_withdrawn_stops_all(ctx, monkeypatch):
    def mutate(requirement, a, b):
        ctx.requirements.withdraw(requirement.requirement_id)

    row, a, b = await _batch_scenario(ctx, monkeypatch, mutate=mutate)
    assert row is not None and str(row.get("state")) == "invalidated", row
    assert a.exists() and b.exists()

async def test_script_changed_during_human_approval_not_executed(ctx):
    script = ctx.computer.root() / "approval-script.py"
    marker = ctx.computer.root() / "approval-marker.txt"
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('original')\n" % marker,
        encoding="utf-8",
    )
    ctx.settings_store.set("computer.permission_mode", "restricted")
    from agent.tools.cmd_tools import RunProgramTool

    tool = RunProgramTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    result = await tool.run(program=sys.executable, args=[str(script)], cwd=str(ctx.computer.root()), timeout=30)
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    script.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('replaced')\n" % marker,
        encoding="utf-8",
    )
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert not marker.exists(), "人工批准恢复也必须核对脚本事实，不能执行替换后的新脚本"


async def test_fs_write_service_recheck_waits_then_real_file_changes(ctx, monkeypatch):
    """真实 RecheckService 路径：模型审查等待期间文件被改，服务 ok 也不能跳过本地核对。"""
    target = ctx.computer.root() / "service-wait.txt"
    target.write_text("before", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    engine = ctx.authorization
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_apply_review(effect, context, mode, reason, scope_id):
        started.set()
        await release.wait()
        return Verdict.ALLOW, "fake review allow", None, True

    monkeypatch.setattr(engine, "_apply_review", fake_apply_review)
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=engine)
    task = asyncio.create_task(tool.run(path=str(target), content="tool-content"))
    await asyncio.wait_for(started.wait(), 5)
    target.write_text("user-during-review", encoding="utf-8")
    release.set()
    result = await task
    assert result.ok is False, result
    assert target.read_text(encoding="utf-8") == "user-during-review"


async def test_fs_write_human_approval_wait_file_change_not_overwritten(ctx):
    target = ctx.computer.root() / "human-wait.txt"
    target.write_text("before", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    tool = FsWriteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    result = await tool.run(path=str(target), content="tool-content")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    target.write_text("user-during-approval", encoding="utf-8")
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert target.read_text(encoding="utf-8") == "user-during-approval"


async def test_fs_patch_human_approval_wait_change_not_applied(ctx):
    target = ctx.computer.root() / "patch-wait.txt"
    target.write_text("old-value", encoding="utf-8")
    ctx.settings_store.set("computer.permission_mode", "restricted")
    from agent.tools.fs_tools import FsPatchTool

    tool = FsPatchTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    result = await tool.run(path=str(target), old="old-value", new="tool-new")
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    target.write_text("user-during-approval", encoding="utf-8")
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("failed", "invalidated"), row
    assert target.read_text(encoding="utf-8") == "user-during-approval"


async def test_batch_delete_only_changed_item_invalidated(ctx, monkeypatch):
    workspace = ctx.computer.root()
    a = workspace / "batch-a.txt"
    b = workspace / "batch-b.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    requirement = ctx.requirements.record_user_message(
        text="删除这两个文件", message_id="m-batch", turn_id="t-batch"
    )
    binding = ctx.requirements.binding_for(requirement.requirement_id)
    ctx.settings_store.set("computer.permission_mode", "restricted")

    trash = workspace / "fake-trash"
    trash.mkdir(exist_ok=True)

    async def fake_move(path):
        source = Path(path)
        os.replace(source, trash / source.name)
        return True, "fake_recycle", ""

    import agent.tools.approved_operations as approved_ops
    monkeypatch.setattr(approved_ops, "_move_to_recycle_bin", fake_move)

    tool = FsDeleteTool(computer=ctx.computer, approvals=ctx.approvals, security=ctx.authorization)
    with requirement_context(binding):
        result = await tool.run(paths=[str(a), str(b)])
    assert result.ok is True and result.code == "pending_approval", result
    approval_id = result.data["approval_id"]
    # 审批等待期间恢复“禁止删除 B”；A 仍应执行。
    ctx.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["delete"], "objects": [{"kind": "path", "value": str(b)}], "reason": "禁止删除 B"}],
        source="test",
        state=requirement_limits.STATE_STRUCTURED,
    )
    await ctx.approvals.decide(approval_id, "approve")
    row = await _wait_state(ctx, approval_id)
    assert row is not None and str(row.get("state")) in ("completed", "failed", "invalidated"), row
    assert not a.exists(), "A 应继续完成（单项限制只影响 B）"
    assert b.exists(), "B 被禁止删除，必须保留"
    detail = row.get("result") or {}
    items = detail.get("items") or detail.get("results") or []
    assert len(items) >= 2, detail
    assert detail.get("partial") is True, detail
    assert detail.get("completed_count") == 1 and detail.get("failed_count") == 1, detail


def _pending_operation(security, *, requirement, effect, text="操作"):
    return PendingOperation(
        kind="fs.write",
        label=text,
        payload={"path": str(next(iter(effect.path_targets())).value)},
        effect=security_gate.effect_to_dict(effect),
        fingerprint={"kind": "fs.write"},
        purpose=text,
        mode_at_request="unrestricted",
        requirement_id=requirement.requirement_id,
        requirement_version=int(requirement.version),
    )


def _exec_ctx(security, operation):
    return ExecutionContext(
        approval_id="appr-test",
        operation=operation,
        mode_at_request="unrestricted",
        mode_at_approval="unrestricted",
        engine=security.engine,
    )


async def test_specific_approval_completion_uses_structured_code_not_reason(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    requirement = security.requirements.record_user_message(
        text="处理项目", message_id="m-unres", turn_id="t-unres",
        structured_state=requirement_limits.STATE_UNRESOLVED,
    )
    target = tmp_path / "workspace" / "a.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    effect = security_gate.write_effect("fs.write", target, purpose="写", content_sha256="x")
    operation = _pending_operation(security, requirement=requirement, effect=effect)
    ctx = _exec_ctx(security, operation)

    allowed, reason = await ctx.authorize_started()
    assert allowed is True, reason
    assert ctx.authorization_basis.get("decision_code") == DECISION_UNRESOLVED_LIMITS

    # 换任意中文/空 reason，但结构 code 保持：行为必须一致。
    real_recheck = security.engine.recheck_approved
    def fake_recheck(effect_arg, **kwargs):
        decision = real_recheck(effect_arg, **kwargs)
        return AuthDecision(
            verdict=decision.verdict,
            reason="完全不同的说明文字",
            decision_code=decision.decision_code,
            effects=decision.effects,
        )
    security.engine.recheck_approved = fake_recheck
    try:
        ctx2 = _exec_ctx(security, operation)
        allowed2, reason2 = await ctx2.authorize_started()
        assert allowed2 is True, reason2
    finally:
        security.engine.recheck_approved = real_recheck

    # 相同中文但没有结构化 code：不能据此放行。
    def fake_no_code(effect_arg, **kwargs):
        return AuthDecision(
            verdict=Verdict.APPROVE,
            reason="历史结构化限制尚未可靠恢复",
            decision_code=None,
        )
    security.engine.recheck_approved = fake_no_code
    try:
        ctx3 = _exec_ctx(security, operation)
        allowed3, _reason3 = await ctx3.authorize_started()
        assert allowed3 is False
    finally:
        security.engine.recheck_approved = real_recheck

    # 即使有结构化 code，已知禁止仍在执行前拒绝（不能借“完成 unresolved”绕限制）。
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["write"], "objects": [{"kind": "path", "value": str(target)}], "reason": "禁止写"}],
        source="test-known",
        state=requirement_limits.STATE_STRUCTURED,
    )
    ctx4 = _exec_ctx(security, operation)
    allowed4, reason4 = await ctx4.authorize_started()
    assert allowed4 is False
    assert "限制" in reason4 or "禁止" in reason4 or "未执行" in reason4


async def test_program_limit_path_does_not_expand_to_same_name(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="unrestricted")
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    dir_a.mkdir(); dir_b.mkdir()
    program_a = dir_a / "tool"
    program_b = dir_b / "tool"
    program_a.write_text("A", encoding="utf-8")
    program_b.write_text("B", encoding="utf-8")
    requirement = security.requirements.record_user_message(
        text="只禁止 A/tool", message_id="m-prog", turn_id="t-prog"
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": str(program_a)}], "reason": "禁止 A"}],
        source="test",
        state=requirement_limits.STATE_STRUCTURED,
    )
    binding = security.requirements.binding_for(requirement.requirement_id)

    def effect_for(path):
        return Effect(kind=EffectKind.EXECUTE, targets=(Target("program", str(path)),), purpose="运行程序")

    same_stem = tmp_path / "C" / "tool.exe"
    same_stem.parent.mkdir()
    same_stem.write_text("C", encoding="utf-8")
    with requirement_context(binding):
        denied = await security.engine.authorize(effect_for(program_a))
        allowed = await security.engine.authorize(effect_for(program_b))
        allowed_suffix = await security.engine.authorize(effect_for(same_stem))
    assert denied.verdict == Verdict.DENY
    assert allowed.verdict == Verdict.ALLOW, allowed.reason
    assert allowed_suffix.verdict == Verdict.ALLOW, allowed_suffix.reason
    # 裸名称不是隐式路径/去后缀回退：历史不明确时不能扩大禁止范围。
    name_requirement = security.requirements.record_user_message(
        text="只禁止名称 tool", message_id="m-name", turn_id="t-name"
    )
    security.requirements.set_structured_limits(
        name_requirement.requirement_id,
        [{"operations": ["execute"], "objects": [{"kind": "program", "value": "tool"}], "reason": "名称"}],
        source="test",
        state=requirement_limits.STATE_STRUCTURED,
    )
    name_binding = security.requirements.binding_for(name_requirement.requirement_id)
    with requirement_context(name_binding):
        by_name = await security.engine.authorize(effect_for(program_b))
    # R9-H6 §4：裸 program=工具名明确归一为 program_name，必须生效而不是静默忽略；
    # 它只按明确名称类型匹配，不再把同名路径限制混为一谈。
    assert by_name.verdict == Verdict.DENY, "program_name 限制必须对同名程序生效"

    link = tmp_path / "alias-tool"
    try:
        os.symlink(program_a, link)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        alias_effect = Effect(kind=EffectKind.EXECUTE, targets=(Target("program", str(link)),), purpose="运行链接")
        with requirement_context(binding):
            alias_decision = await security.engine.authorize(alias_effect)
        assert alias_decision.verdict == Verdict.DENY, "链接别名指向被禁止的同一程序对象"
    normalized = dir_a / ".." / "A" / "tool"
    with requirement_context(binding):
        normalized_decision = await security.engine.authorize(effect_for(normalized))
    assert normalized_decision.verdict == Verdict.DENY, "规范化后同一路径必须命中"
    if os.name == "nt":
        case_variant = Path(str(program_a).upper())
        with requirement_context(binding):
            case_decision = await security.engine.authorize(effect_for(case_variant))
        assert case_decision.verdict == Verdict.DENY, "Windows 路径大小写不敏感"
