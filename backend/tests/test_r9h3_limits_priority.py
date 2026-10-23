# -*- coding: utf-8 -*-
"""R9-H3 A：已知限制优先级与执行前核对（契约 §2）。

原则：
* 真实 RequirementLedger / AuthorizationLedger / 真实 AuthorizationEngine / 真实
  RecheckService；模型审查用 FakeReviewer（可计数、可控返回），不联网、不用真实 Key；
* 不预构造最终 DENY：全部通过 engine.authorize / authorize_many / authorize_batch /
  recheck_approved / RecheckService.recheck 真实判定；
* 覆盖：unresolved/legacy 的 known limits 优先于内部数据确认/旧 allow/人工批准/审批恢复/
  模式/模型审查；范围外不误伤；真实 apply_resolution 解除后恢复；批量逐项；执行前新增
  限制后 recheck 拒绝；unrestricted unresolved 不再调用普通独立授权审查。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.gateway import AuthContext
from agent.security.recheck import OperationBinding, RecheckService
from agent.security.requirements import (
    AuthorizationLedger,
    AuthorizationRecord,
    RequirementBinding,
    RequirementLedger,
)
from agent.security.resolution import RequirementRelation, ResolutionOutcome
from agent.security.review import (
    ReviewDecision,
    ReviewOutcome,
    ReviewRequest,
)
from agent.storage.settings import SettingsStore

_NOW = "2026-10-23T00:00:00+00:00"
STATE_STRUCTURED = "structured"
STATE_LEGACY = "legacy"
STATE_UNRESOLVED = "unresolved"


class FakeReviewer:
    """计数型审查替身：默认 ALLOW；用于证明"已知限制/未恢复状态下不调用"。"""

    def __init__(self, decision: ReviewDecision = ReviewDecision.ALLOW) -> None:
        self.decision = decision
        self.calls: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> ReviewOutcome:
        self.calls.append(request)
        return ReviewOutcome(
            decision=self.decision,
            reason="fake reviewer " + self.decision.value,
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(request.operation.kind,),
            objects=tuple(request.operation.objects),
            limits=("fake reviewer 允许本次测试操作",),
            purpose="fake reviewer",
        )


def make_security(
    conn,
    tmp_path: Path,
    *,
    mode: str = "unrestricted",
    protected=(),
    reviewer=None,
):
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    ledger = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    engine = AuthorizationEngine(
        conn=conn,
        approvals=None,
        settings=store,
        instance_id="inst_test",
        workspace_root=lambda: workspace,
        internal_data_paths=lambda: [Path(item) for item in protected],
        requirements=ledger,
        authorizations=authorizations,
        reviewer=reviewer,
    )
    return SimpleNamespace(
        engine=engine,
        store=store,
        requirements=ledger,
        authorizations=authorizations,
        workspace=workspace,
    )


def write_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path)),),
        purpose="写入测试文件",
    )


def internal_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.INTERNAL_DATA,
        targets=(Target("path", str(path)),),
        purpose="内部数据写入测试",
    )


def delete_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.DELETE,
        targets=(Target("path", str(path), identity="vol:fake-file"),),
        purpose="永久删除测试文件",
        detail={"permanent": True},
    )


def b_limit(b_path, *, operations=("write",)) -> tuple[dict, ...]:
    return (
        {
            "operations": list(operations),
            "objects": [{"kind": "path", "value": str(b_path)}],
            "reason": "用户明确要求不要修改 B",
        },
    )


def make_requirement(security, *, state: str, limits=(), text="修复项目，但不要修改 B。"):
    requirements = security.requirements
    return requirements.record_user_message(
        text=text,
        message_id="msg_test",
        turn_id="turn_test",
        structured_state=state,
        structured_limits=tuple(dict(item) for item in limits),
        structured_source="test_setup",
    )


def binding_of(requirement) -> RequirementBinding:
    return RequirementBinding(
        requirement_id=requirement.requirement_id, version=int(requirement.version)
    )


def ctx_for(binding: RequirementBinding) -> AuthContext:
    return AuthContext(requirement=binding)


def seed_allow(security, requirement, *, kind: str = "write", fingerprint: str = "fp_old"):
    record = AuthorizationRecord(
        record_id="authrec_" + requirement.requirement_id[-6:],
        requirement_id=requirement.requirement_id,
        operation_kind=kind,
        fingerprint=fingerprint,
        decision="allow",
        reason="旧的模型审查允许",
        allowed_operations=(kind,),
        objects=(),
        limits=(),
        source="model_review",
        mode="restricted",
        created_at=_NOW,
        expires_at=None,
        revoked_at=None,
        review_id=None,
    )
    security.authorizations.record(record)
    return record


# ---------------------------------------------------------------------------
# 1) unresolved 已知限制优先于内部数据确认/模型审查；范围外不误伤
# ---------------------------------------------------------------------------


async def test_unresolved_known_limit_denies_before_internal_and_review(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer()
    security = make_security(
        db_conn, tmp_path, mode="unrestricted", protected=[b_path], reviewer=reviewer
    )
    requirement = make_requirement(
        security, state=STATE_UNRESOLVED, limits=b_limit(b_path)
    )
    binding = binding_of(requirement)

    with requirement_context(binding):
        decision = await security.engine.authorize(write_effect(b_path))
    assert decision.verdict == Verdict.DENY
    assert "不要修改 B" in decision.reason
    assert reviewer.calls == []

    # 内部数据 effect 也必须先命中限制（不能先进入三次确认分支）
    internal = await security.engine.authorize(
        internal_effect(b_path), ctx=ctx_for(binding)
    )
    assert internal.verdict == Verdict.DENY
    assert reviewer.calls == []

    # 未被限制的目标：内部数据仍按三次确认（APPROVE），不是 DENY
    other_internal = await security.engine.authorize(
        internal_effect(tmp_path / "data" / "x.log"), ctx=ctx_for(binding)
    )
    assert other_internal.verdict == Verdict.APPROVE
    assert reviewer.calls == []

    # 范围外普通写入：unresolved 相关操作给具体确认（不是 DENY），reviewer 0
    outside = await security.engine.authorize(
        write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(binding)
    )
    assert outside.verdict == Verdict.APPROVE
    assert reviewer.calls == []


async def test_structured_known_limit_denies_even_always_allow_reviewer(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer(decision=ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state=STATE_STRUCTURED, limits=b_limit(b_path))
    binding = binding_of(requirement)

    decision = await security.engine.authorize(write_effect(b_path), ctx=ctx_for(binding))
    assert decision.verdict == Verdict.DENY
    assert reviewer.calls == [], "已知限制必须在模型审查之前 DENY"


# ---------------------------------------------------------------------------
# 2) unrestricted 不再调用普通独立授权审查；restricted 仍走原审查
# ---------------------------------------------------------------------------


async def test_unrestricted_unresolved_has_no_reviewer_but_restricted_does(db_conn, tmp_path):
    reviewer = FakeReviewer(decision=ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    requirement = make_requirement(security, state=STATE_UNRESOLVED, limits=())
    binding = binding_of(requirement)
    target = tmp_path / "proj" / "A.py"

    first = await security.engine.authorize(write_effect(target), ctx=ctx_for(binding))
    assert first.verdict == Verdict.APPROVE  # 未恢复部分：具体确认
    assert reviewer.calls == []
    second = await security.engine.authorize(write_effect(target), ctx=ctx_for(binding))
    assert second.verdict == Verdict.APPROVE
    assert reviewer.calls == [], "unrestricted 重复判定也不得调用普通独立授权审查"

    security.store.set("computer.permission_mode", "restricted")
    restricted = await security.engine.authorize(
        write_effect(target), ctx=ctx_for(binding)
    )
    assert restricted.verdict == Verdict.ALLOW  # fake reviewer ALLOW，restricted 原审查正常
    assert len(reviewer.calls) == 1


# ---------------------------------------------------------------------------
# 3) 旧 allow / 人工批准 / 审批恢复 / 内部数据确认不能覆盖有效禁止
# ---------------------------------------------------------------------------


async def test_old_allow_and_manual_approval_cannot_override(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer(decision=ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state=STATE_UNRESOLVED, limits=b_limit(b_path))
    binding = binding_of(requirement)
    seed_allow(security, requirement, kind="write", fingerprint="fp_old_write")

    decision = await security.engine.authorize(write_effect(b_path), ctx=ctx_for(binding))
    assert decision.verdict == Verdict.DENY
    assert reviewer.calls == [], "旧 allow 记录/审查都不能覆盖有效禁止"

    rechecked = security.engine.recheck_approved(
        write_effect(b_path),
        context=ctx_for(binding),
        approval_kind="fs.write",
        confirmations_required=1,
        confirmed_stage=1,
    )
    assert rechecked.verdict == Verdict.DENY


async def test_permanent_delete_old_allow_cannot_override(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer(decision=ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(
        security,
        state=STATE_UNRESOLVED,
        limits=b_limit(b_path, operations=("delete",)),
    )
    binding = binding_of(requirement)
    seed_allow(security, requirement, kind="delete_permanent", fingerprint="fp_perm_old")

    decision = await security.engine.authorize(delete_effect(b_path), ctx=ctx_for(binding))
    assert decision.verdict == Verdict.DENY
    assert reviewer.calls == []

    rechecked = security.engine.recheck_approved(
        delete_effect(b_path),
        context=ctx_for(binding),
        approval_kind="delete_permanent",
        confirmations_required=1,
        confirmed_stage=1,
    )
    assert rechecked.verdict == Verdict.DENY


async def test_internal_data_confirmation_cannot_override_known_limit(db_conn, tmp_path):
    b_path = tmp_path / "internal" / "state.db"
    reviewer = FakeReviewer()
    security = make_security(
        db_conn, tmp_path, mode="unrestricted", protected=[b_path], reviewer=reviewer
    )
    requirement = make_requirement(security, state=STATE_UNRESOLVED, limits=b_limit(b_path))
    binding = binding_of(requirement)

    decision = await security.engine.authorize(
        internal_effect(b_path), ctx=ctx_for(binding)
    )
    assert decision.verdict == Verdict.DENY, "三次确认不能绕过有效禁止"
    assert reviewer.calls == []

    rechecked = security.engine.recheck_approved(
        internal_effect(b_path),
        context=ctx_for(binding),
        approval_kind="internal_data",
        confirmations_required=3,
        confirmed_stage=3,
    )
    assert rechecked.verdict == Verdict.DENY


# ---------------------------------------------------------------------------
# 4) 真实 apply_resolution 明确解除限制后恢复
# ---------------------------------------------------------------------------


async def test_apply_resolution_removes_limit_then_allows(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    requirement = make_requirement(security, state=STATE_STRUCTURED, limits=b_limit(b_path))
    binding = binding_of(requirement)
    before = await security.engine.authorize(write_effect(b_path), ctx=ctx_for(binding))
    assert before.verdict == Verdict.DENY

    resolution = ResolutionOutcome(
        relation=RequirementRelation.AMEND,
        target_requirement_id=requirement.requirement_id,
        target_version=int(requirement.version),
        corrections=("B 的限制取消",),
        limits=(),
        removed_limits=b_limit(b_path),
        reason="用户明确解除 B 的限制",
        model="fake-r9h3",
        prompt_version="fake-r9h3-resolution",
    )
    applied = security.requirements.apply_resolution(
        resolution,
        message_id="msg_remove",
        turn_id="turn_remove",
        message="之前说不要修改 B 的限制取消。",
    )
    child = applied.requirement
    assert child is not None and child.status == "active"
    assert child.structured_limits_state == STATE_STRUCTURED
    assert child.structured_limits == ()
    child_binding = binding_of(child)

    after = await security.engine.authorize(
        write_effect(b_path), ctx=ctx_for(child_binding)
    )
    assert after.verdict == Verdict.ALLOW
    assert reviewer.calls == []


# ---------------------------------------------------------------------------
# 5) 批量逐项：只有 B 子项 DENY
# ---------------------------------------------------------------------------


async def test_authorize_batch_only_b_denied(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    requirement = make_requirement(security, state=STATE_UNRESOLVED, limits=b_limit(b_path))
    binding = binding_of(requirement)
    effects = [write_effect(a_path), write_effect(b_path)]

    decisions = await security.engine.authorize_batch(effects, ctx=ctx_for(binding))
    assert len(decisions) == 2
    assert decisions[0].verdict != Verdict.DENY, "范围外 A 不得因局部限制变成 DENY"
    assert decisions[1].verdict == Verdict.DENY
    assert reviewer.calls == []

    many = await security.engine.authorize_many(effects, ctx=ctx_for(binding))
    assert many.verdict == Verdict.DENY
    assert "不要修改 B" in many.reason
    assert reviewer.calls == []


# ---------------------------------------------------------------------------
# 6) 执行前重核对：最新限制（含 legacy/unresolved known limits）优先
# ---------------------------------------------------------------------------


async def test_recheck_service_uses_latest_limits_after_update(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(security, state=STATE_STRUCTURED, limits=())
    binding = binding_of(requirement)
    service = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        authorizations=security.authorizations,
    )
    operation_binding = OperationBinding(
        requirement=binding,
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    first = await service.recheck(operation_binding, write_effect(b_path))
    assert first.ok is True

    # 绑定之后、执行之前新增有效限制（同一版本，模拟恢复/更新持久化限制）
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        b_limit(b_path),
        source="late_limit_update",
        state=STATE_STRUCTURED,
    )
    second = await service.recheck(operation_binding, write_effect(b_path))
    assert second.ok is False
    assert second.code == "denied"
    assert "不要修改 B" in second.reason
    assert second.requirement_state == STATE_STRUCTURED


async def test_recheck_uses_legacy_known_limits(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(
        security, state=STATE_UNRESOLVED, limits=b_limit(b_path)
    )
    # 模拟 H2 历史行：state=legacy 但已保存 known limits（record_user_message 的
    # legacy 参数会清空结构化内容，这里直接还原历史状态）。
    db_conn.execute(
        "UPDATE user_requirements SET structured_limits_state = ? WHERE id = ?",
        (STATE_LEGACY, requirement.requirement_id),
    )
    db_conn.commit()
    requirement = security.requirements.by_id(requirement.requirement_id)
    assert requirement is not None and requirement.structured_limits_state == STATE_LEGACY
    assert requirement.structured_limits == b_limit(b_path)
    binding = binding_of(requirement)
    service = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        authorizations=security.authorizations,
    )
    operation_binding = OperationBinding(
        requirement=binding,
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    result = await service.recheck(operation_binding, write_effect(b_path))
    assert result.ok is False
    assert result.code == "denied"
    assert "不要修改 B" in result.reason
    assert result.requirement_state == STATE_LEGACY


# ---------------------------------------------------------------------------
# 7) 静态约束：engine 不再保留 targeted/unresolved 审查分支
# ---------------------------------------------------------------------------


def test_engine_source_has_no_targeted_review_branch():
    source = Path("src/agent/security/engine.py").read_text(encoding="utf-8")
    assert "targeted_limits_review" not in source
    assert "unresolved_review" not in source
    assert 'EffectKind.INTERNAL_DATA: "write"' in source
