# -*- coding: utf-8 -*-
"""R9-G W1：用户要求版本绑定与授权记录失效（契约 §3、§7、§10）。

覆盖：
* UserRequirement.version / RequirementBinding / binding_for / validate_binding；
* candidates 有限候选（id/原话摘录/版本/时间，仅 active）；
* add_correction 生成 version=parent+1 的子要求、父要求失效、历史保留；
* 授权记录绑定 requirement_version；修正后旧版本记录不可复用；
* 缺 requirement_version 的历史记录不默认有效（契约 §10）；
* 要求被撤回/替换后绑定与记录失效。
"""

from __future__ import annotations

import pytest

from agent.security.requirements import (
    AuthorizationLedger,
    AuthorizationRecord,
    DEFAULT_REQUIREMENT_VERSION,
    RequirementBinding,
    RequirementLedger,
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
    authorization_record_from_outcome,
)
from agent.security.review import (
    ReviewDecision,
    ReviewObject,
    ReviewOperation,
    ReviewOutcome,
    ReviewRequest,
    operation_fingerprint,
    validate_outcome,
)

_NOW = "2026-10-23T00:00:00+00:00"


def _obj(value: str = "D:/work/报告.docx", *, identity_key="vol1:file1", file_origin="user_original") -> ReviewObject:
    return ReviewObject(kind="path", value=value, identity_key=identity_key, file_origin=file_origin)


def _operation(objects=None, *, kind: str = "write", **kwargs) -> ReviewOperation:
    return ReviewOperation(kind=kind, objects=tuple(objects or [_obj()]), **kwargs)


def _record(
    requirement_id: str,
    fingerprint: str,
    *,
    record_id: str | None = None,
    requirement_version: int | None = None,
    **overrides,
) -> AuthorizationRecord:
    data = dict(
        record_id=record_id or ("authrec_" + fingerprint[:10]),
        requirement_id=requirement_id,
        operation_kind="write",
        fingerprint=fingerprint,
        decision="allow",
        reason="模型审查允许",
        allowed_operations=("write",),
        objects=(),
        limits=("仅本次",),
        source="model_review",
        mode="restricted",
        created_at=_NOW,
        expires_at=None,
        revoked_at=None,
        review_id=None,
        requirement_version=requirement_version,
    )
    data.update(overrides)
    return AuthorizationRecord(**data)


def _validated_allow(requirement, operation):
    request = ReviewRequest(
        requirement=requirement,
        corrections=(),
        limits=(),
        mode="restricted",
        operation=operation,
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
        requirement_resolution_status="resolved",
    )
    raw = ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="用户原话明确要求修改该文档",
        requirement_id=requirement.requirement_id,
        allowed_operations=(operation.kind,),
        objects=operation.objects,
        limits=("仅本次写入该文件",),
        purpose="按用户要求修改文档",
    )
    return request, validate_outcome(raw, request)


# ---------------------------------------------------------------------------
# 版本 / 绑定 / 候选
# ---------------------------------------------------------------------------


def test_record_user_message_starts_at_version_one_and_binding_is_active(db_conn):
    ledger = RequirementLedger(db_conn)
    requirement = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1", topic_id="top")
    assert requirement.version == DEFAULT_REQUIREMENT_VERSION == 1
    assert requirement.as_dict()["version"] == 1
    binding = ledger.binding_for(requirement.requirement_id)
    assert binding == RequirementBinding(requirement_id=requirement.requirement_id, version=1)
    assert ledger.validate_binding(binding) is True
    assert ledger.validate_binding(RequirementBinding(requirement_id=requirement.requirement_id, version=2)) is False
    assert ledger.validate_binding(RequirementBinding(requirement_id="req_missing", version=1)) is False
    assert ledger.validate_binding("not-a-binding") is False  # type: ignore[arg-type]


def test_binding_disappears_after_supersede_or_withdraw(db_conn):
    ledger = RequirementLedger(db_conn)
    superseded = ledger.record_user_message(text="任务一", message_id="m1", turn_id="t1", topic_id="top")
    ledger.supersede(superseded.requirement_id)
    assert ledger.binding_for(superseded.requirement_id) is None
    assert ledger.by_id(superseded.requirement_id).status == STATUS_SUPERSEDED

    withdrawn = ledger.record_user_message(text="任务二", message_id="m2", turn_id="t2", topic_id="top")
    ledger.withdraw(withdrawn.requirement_id)
    assert ledger.binding_for(withdrawn.requirement_id) is None
    assert ledger.by_id(withdrawn.requirement_id).status == STATUS_WITHDRAWN


def test_candidates_are_bounded_and_include_text_excerpt_version_time(db_conn):
    ledger = RequirementLedger(db_conn)
    first = ledger.record_user_message(text="旧要求", message_id="m1", turn_id="t1", topic_id="top")
    second = ledger.record_user_message(text="新要求：整理 D:/work/报告.md", message_id="m2", turn_id="t2", topic_id="top")
    rows = ledger.candidates(limit=1)
    assert len(rows) == 1
    assert rows[0]["requirement_id"] == second.requirement_id  # 最近更新优先
    assert rows[0]["version"] == 1
    assert "整理" in rows[0]["text_excerpt"]
    assert rows[0]["created_at"] and rows[0]["updated_at"]
    all_rows = ledger.candidates(limit=10)
    ids = {row["requirement_id"] for row in all_rows}
    assert {first.requirement_id, second.requirement_id} <= ids
    # 非 active 不出现在候选里
    ledger.withdraw(first.requirement_id)
    assert all(row["requirement_id"] != first.requirement_id for row in ledger.candidates(limit=10))


# ---------------------------------------------------------------------------
# add_correction：版本递进、父要求失效、历史保留
# ---------------------------------------------------------------------------


def test_add_correction_increments_version_and_keeps_history(db_conn):
    ledger = RequirementLedger(db_conn)
    root = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1", topic_id="top")
    child = ledger.add_correction(root.requirement_id, "只改标题", message_id="m2")
    assert child.version == 2 and child.parent_id == root.requirement_id
    assert ledger.by_id(root.requirement_id).status == STATUS_SUPERSEDED
    grandchild = ledger.add_correction(child.requirement_id, "标题改成「本周进展」", message_id="m3")
    assert grandchild.version == 3 and grandchild.parent_id == child.requirement_id
    # 历史行仍在（撤回/修正不改写历史）
    assert ledger.by_id(root.requirement_id) is not None
    assert [item.version for item in ledger.chain(grandchild.requirement_id)] == [1, 2, 3]
    assert ledger.corrections_for(grandchild.requirement_id) == ("整理报告", "只改标题")


# ---------------------------------------------------------------------------
# 授权记录版本绑定与失效
# ---------------------------------------------------------------------------


def test_record_fills_requirement_version_and_reuse_is_version_bound(db_conn):
    ledger = RequirementLedger(db_conn)
    ledger_records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1", topic_id="top")
    operation = _operation()
    fingerprint = operation_fingerprint(operation, mode="restricted")
    record = _record(requirement.requirement_id, fingerprint)  # requirement_version 未填
    ledger_records.record(record)

    stored = ledger_records.records_for_requirement(requirement.requirement_id)[0]
    assert stored.requirement_version == 1  # 后端从台账补齐，不猜
    hit = ledger_records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint=fingerprint
    )
    assert hit is not None and hit.requirement_version == 1

    # 修正后：父要求 superseded + 记录撤销 → 旧版本不能复用
    ledger.add_correction(requirement.requirement_id, "只改标题", message_id="m2")
    assert ledger_records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint=fingerprint
    ) is None
    # 记录行保留（历史审计），只是 revoked
    kept = ledger_records.records_for_requirement(requirement.requirement_id, include_revoked=True)
    assert kept and kept[0].revoked_at is not None


def test_legacy_record_without_requirement_version_is_not_reused(db_conn):
    """契约 §10：缺 requirement 绑定的历史记录不得默认补成有效授权。"""
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1", topic_id="top")
    db_conn.execute(
        "INSERT INTO authorization_records"
        " (id, requirement_id, operation_kind, fingerprint, decision, reason,"
        "  allowed_operations, objects, limits, source, mode, created_at,"
        "  expires_at, revoked_at, review_id, requirement_version)"
        " VALUES (?, ?, 'write', 'legacy-fp', 'allow', '旧记录', '[]', '[]', '[]',"
        "         'model_review', 'restricted', ?, NULL, NULL, NULL, NULL)",
        ("authrec_legacy", requirement.requirement_id, _NOW),
    )
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint="legacy-fp"
    ) is None


def test_out_of_band_version_bump_invalidates_existing_record(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1", topic_id="top")
    fingerprint = operation_fingerprint(_operation(), mode="restricted")
    records.record(_record(requirement.requirement_id, fingerprint))
    # 模拟后台修复/迁移把版本推进（不属于任何 add_correction 路径）
    db_conn.execute(
        "UPDATE user_requirements SET version = 5 WHERE id = ?", (requirement.requirement_id,)
    )
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint=fingerprint
    ) is None


def test_record_rejects_missing_requirement(db_conn):
    records = AuthorizationLedger(db_conn)
    with pytest.raises(ValueError):
        records.record(_record("req_missing", "fp-x"))


# ---------------------------------------------------------------------------
# 由审查结果形成版本绑定的记录：修正后旧记录失效，新版本可单独复用
# ---------------------------------------------------------------------------


def test_authorization_record_from_outcome_binds_version_and_new_version_reuses_separately(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="把报告第 3 段改一下", message_id="m1", turn_id="t1", topic_id="top")
    operation = _operation()
    request, outcome = _validated_allow(requirement, operation)
    record = authorization_record_from_outcome(outcome, request=request)
    assert record.requirement_version == 1
    records.record(record)
    assert records.reuse(
        requirement_id=requirement.requirement_id,
        operation_kind=operation.kind,
        fingerprint=record.fingerprint,
    ) is not None

    child = ledger.add_correction(requirement.requirement_id, "标题也一起改", message_id="m2")
    child_operation = _operation()
    child_request, child_outcome = _validated_allow(child, child_operation)
    child_record = authorization_record_from_outcome(child_outcome, request=child_request)
    assert child_record.requirement_version == 2
    records.record(child_record)
    assert records.reuse(
        requirement_id=child.requirement_id,
        operation_kind=child_operation.kind,
        fingerprint=child_record.fingerprint,
    ) is not None
    # 同一 fingerprint 在子要求下命中的只能是子要求自己的 v2 记录，不会借到父记录
    hit_under_child = records.reuse(
        requirement_id=child.requirement_id,
        operation_kind=operation.kind,
        fingerprint=record.fingerprint,
    )
    assert hit_under_child is not None
    assert hit_under_child.record_id == child_record.record_id
    assert hit_under_child.requirement_version == 2
    assert all(
        item.requirement_id == child.requirement_id
        for item in records.records_for_requirement(child.requirement_id)
    )


def test_current_is_read_only_compatibility_not_binding(db_conn):
    """current() 仅兼容只读；授权判定必须用 binding_for（契约 §3）。"""
    ledger = RequirementLedger(db_conn)
    first = ledger.record_user_message(text="任务一", message_id="m1", turn_id="t1", topic_id="top")
    second = ledger.record_user_message(text="任务二", message_id="m2", turn_id="t2", topic_id="top")
    assert ledger.current().requirement_id == second.requirement_id
    ledger.supersede(second.requirement_id)
    # current() 仍能返回另一条 active，但它不代表"这次操作依据的要求"
    assert ledger.current() is not None
    assert ledger.binding_for(second.requirement_id) is None
    assert ledger.binding_for(first.requirement_id) == RequirementBinding(first.requirement_id, 1)
