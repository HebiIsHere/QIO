# -*- coding: utf-8 -*-
"""R9-F W1：用户要求台账 / 有界授权记录生命周期的确定性测试。

覆盖：
* requirement 以具体用户消息为单位、原话不改写、重启后可重建；
* 修正 = append（父要求 superseded、父链授权记录失效、limits 追加修正原话）；
* 撤回 / 替换使要求与授权记录失效；
* 复用缓存键 = (requirement_id, operation fingerprint, 对象身份, 模式)，新对象/新程序/
  新接收方/模式变化一律重判；
* 由已校验 ALLOW 结果形成有界记录，重启后仍可复用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.security.requirements import (
    AuthorizationLedger,
    AuthorizationRecord,
    RequirementLedger,
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
    UserRequirement,
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

_NOW = "2026-10-22T00:00:00+00:00"


def _obj(value: str = "D:/work/报告.docx", *, identity_key=None, file_origin=None) -> ReviewObject:
    return ReviewObject(kind="path", value=value, identity_key=identity_key, file_origin=file_origin)


def _operation(*, objects=None, kind: str = "write", **kwargs) -> ReviewOperation:
    return ReviewOperation(kind=kind, objects=tuple(objects or [_obj()]), **kwargs)


def _requirement(**kwargs) -> UserRequirement:
    defaults = dict(
        requirement_id="req_make_1",
        text="把 D:/work/报告.docx 改一下",
        provenance={"kind": "user_message", "message_id": "msg_make", "turn_id": "turn_make", "topic_id": "topic_make"},
        parent_id=None,
        status=STATUS_ACTIVE,
        limits=(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    return UserRequirement(**defaults)


def _request(*, requirement: UserRequirement | None = None, operation: ReviewOperation | None = None, mode="restricted") -> ReviewRequest:
    return ReviewRequest(
        requirement=requirement or _requirement(),
        corrections=(),
        limits=(),
        mode=mode,
        operation=operation or _operation(),
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
    )


def _record(requirement_id: str, fingerprint: str, *, operation_kind="write", decision="allow", **overrides) -> AuthorizationRecord:
    data = dict(
        record_id="authrec_" + fingerprint[:8] + requirement_id[-4:],
        requirement_id=requirement_id,
        operation_kind=operation_kind,
        fingerprint=fingerprint,
        decision=decision,
        reason="模型审查允许",
        allowed_operations=("write",),
        objects=(),
        limits=("仅本次",),
        source="model_review",
        mode="restricted",
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=None,
        revoked_at=None,
        review_id=None,
    )
    data.update(overrides)
    return AuthorizationRecord(**data)


# ---------------------------------------------------------------------------
# RequirementLedger：记录 / 读取 / 重启重建
# ---------------------------------------------------------------------------


def test_record_user_message_stores_original_words_and_provenance(db_conn):
    ledger = RequirementLedger(db_conn)
    text = "把 D:/work/报告.docx 第 3 段改成新的结论"  # 原话不改写
    requirement = ledger.record_user_message(
        text=text, message_id="msg_1", turn_id="turn_1", topic_id="topic_1"
    )
    assert requirement.requirement_id.startswith("req_")
    assert requirement.text == text
    assert requirement.status == STATUS_ACTIVE
    assert requirement.parent_id is None
    assert requirement.provenance == {
        "kind": "user_message",
        "message_id": "msg_1",
        "turn_id": "turn_1",
        "topic_id": "topic_1",
    }
    assert ledger.current() == requirement
    assert ledger.by_id(requirement.requirement_id) == requirement
    assert ledger.by_id("req_missing") is None


def test_requirement_persists_across_restart(db_conn):
    first = RequirementLedger(db_conn).record_user_message(
        text="整理一下 D:/work/报告.docx", message_id="msg_1", turn_id="turn_1", topic_id="topic_1"
    )
    # 新的 ledger 实例 = 进程重启后从库里重建
    restarted = RequirementLedger(db_conn)
    assert restarted.by_id(first.requirement_id) == first
    assert restarted.current() == first


def test_current_returns_latest_active_requirement(db_conn):
    ledger = RequirementLedger(db_conn)
    first = ledger.record_user_message(text="第一条", message_id="m1", turn_id="t1", topic_id="top")
    second = ledger.record_user_message(text="第二条", message_id="m2", turn_id="t2", topic_id="top")
    assert ledger.current().requirement_id == second.requirement_id
    # 不自动 supersede：两条消息都是独立要求，由调用方决定延续/替换
    assert ledger.by_id(first.requirement_id).status == STATUS_ACTIVE
    # 一个空的 by_id 不抛异常
    assert ledger.by_id("") is None


# ---------------------------------------------------------------------------
# 修正 = append：父要求失效、父链授权记录撤销、限制继承
# ---------------------------------------------------------------------------


def test_add_correction_appends_child_and_invalidates_parent_records(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    parent = ledger.record_user_message(text="把报告改一下", message_id="m1", turn_id="t1", topic_id="top")
    records.record(_record(parent.requirement_id, "fp-parent"))

    child = ledger.add_correction(parent.requirement_id, "只改标题，不要动正文", message_id="m2")

    assert child.parent_id == parent.requirement_id
    assert child.status == STATUS_ACTIVE
    assert child.text == "只改标题，不要动正文"
    # limits 继承父限制并追加修正原话（limits 定义包含"后续修正"）
    assert child.limits == ("只改标题，不要动正文",)
    assert ledger.by_id(parent.requirement_id).status == STATUS_SUPERSEDED
    assert ledger.current().requirement_id == child.requirement_id
    # 修正后父要求的授权记录立即失效，不能借旧审查继续执行
    assert records.reuse(
        requirement_id=parent.requirement_id, operation_kind="write", fingerprint="fp-parent"
    ) is None
    assert records.records_for_requirement(parent.requirement_id) == []
    # 修正链原话可回溯（根 -> 父）
    assert ledger.chain(child.requirement_id)[0].requirement_id == parent.requirement_id
    assert ledger.corrections_for(child.requirement_id) == ("把报告改一下",)


def test_add_correction_rejects_unknown_or_inactive_requirement(db_conn):
    ledger = RequirementLedger(db_conn)
    with pytest.raises(KeyError):
        ledger.add_correction("req_missing", "改成蓝色", message_id="m2")
    parent = ledger.record_user_message(text="改报告", message_id="m1", turn_id="t1", topic_id="top")
    ledger.supersede(parent.requirement_id)
    with pytest.raises(ValueError):
        ledger.add_correction(parent.requirement_id, "改成蓝色", message_id="m2")


# ---------------------------------------------------------------------------
# 撤回 / 替换
# ---------------------------------------------------------------------------


def test_withdraw_cascades_to_children_and_revokes_records(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    root = ledger.record_user_message(text="改报告", message_id="m1", turn_id="t1", topic_id="top")
    child = ledger.add_correction(root.requirement_id, "只改标题", message_id="m2")
    records.record(_record(child.requirement_id, "fp-child"))

    ledger.withdraw(root.requirement_id)

    assert ledger.by_id(root.requirement_id).status == STATUS_WITHDRAWN
    assert ledger.by_id(child.requirement_id).status == STATUS_WITHDRAWN
    assert ledger.current() is None
    assert records.reuse(requirement_id=child.requirement_id, operation_kind="write", fingerprint="fp-child") is None


def test_supersede_marks_requirement_and_revokes_records(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="改报告", message_id="m1", turn_id="t1", topic_id="top")
    records.record(_record(requirement.requirement_id, "fp-1"))
    ledger.supersede(requirement.requirement_id)
    assert ledger.by_id(requirement.requirement_id).status == STATUS_SUPERSEDED
    assert records.records_for_requirement(requirement.requirement_id) == []
    assert ledger.current() is None


# ---------------------------------------------------------------------------
# AuthorizationLedger：复用 / 失效
# ---------------------------------------------------------------------------


def test_record_and_reuse_exact_bounded_record(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="把报告改一下", message_id="m1", turn_id="t1", topic_id="top")
    record = _record(requirement.requirement_id, "fp-exact")
    records.record(record)

    # 重启后（新实例）仍可复用
    restarted = AuthorizationLedger(db_conn)
    hit = restarted.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint="fp-exact"
    )
    assert hit is not None
    assert hit.record_id == record.record_id
    assert hit.objects == ()
    assert hit.limits == ("仅本次",)


def test_reuse_misses_on_new_object_program_mode_or_requirement(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="把报告改一下", message_id="m1", turn_id="t1", topic_id="top")

    base_op = _operation(objects=[_obj(identity_key="vol1:file1")])
    base_fp = operation_fingerprint(base_op, mode="restricted")
    records.record(_record(requirement.requirement_id, base_fp))

    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint=base_fp
    ) is not None
    # 新对象身份 → 新 fingerprint → 不再命中
    new_object_fp = operation_fingerprint(
        _operation(objects=[_obj("D:/work/报告.docx", identity_key="vol1:file2")]), mode="restricted"
    )
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint=new_object_fp
    ) is None
    # 新程序/argv → 新 fingerprint
    program_one = _operation(
        kind="execute", objects=[], program="D:/proj/python.exe", argv=("-m", "pytest"), cwd="D:/proj"
    )
    program_two = _operation(
        kind="execute", objects=[], program="C:/Windows/System32/cmd.exe", argv=("/c", "dir"), cwd="D:/proj"
    )
    records.record(
        _record(
            requirement.requirement_id,
            operation_fingerprint(program_one, mode="restricted"),
            operation_kind="execute",
        )
    )
    assert records.reuse(
        requirement_id=requirement.requirement_id,
        operation_kind="execute",
        fingerprint=operation_fingerprint(program_two, mode="restricted"),
    ) is None
    # 模式变化 → fingerprint 变化（缓存键包含模式）
    assert records.reuse(
        requirement_id=requirement.requirement_id,
        operation_kind="write",
        fingerprint=operation_fingerprint(base_op, mode="unrestricted"),
    ) is None
    # 另一条 requirement → 不能复用
    other = ledger.record_user_message(text="另一件事", message_id="m2", turn_id="t2", topic_id="top")
    assert records.reuse(
        requirement_id=other.requirement_id, operation_kind="write", fingerprint=base_fp
    ) is None


def test_reuse_ignores_revoked_expired_and_non_allow_records(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="把报告改一下", message_id="m1", turn_id="t1", topic_id="top")

    records.record(_record(requirement.requirement_id, "fp-revoked"))
    records.revoke_for_requirement(requirement.requirement_id)
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint="fp-revoked"
    ) is None

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    records.record(_record(requirement.requirement_id, "fp-expired", expires_at=past))
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint="fp-expired"
    ) is None
    records.record(_record(requirement.requirement_id, "fp-valid", expires_at=future))
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint="fp-valid"
    ) is not None

    records.record(_record(requirement.requirement_id, "fp-confirm", decision="confirm"))
    assert records.reuse(
        requirement_id=requirement.requirement_id, operation_kind="write", fingerprint="fp-confirm"
    ) is None


def test_revocation_helpers_are_idempotent(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(text="把报告改一下", message_id="m1", turn_id="t1", topic_id="top")
    records.record(_record(requirement.requirement_id, "fp-1"))
    records.record(_record(requirement.requirement_id, "fp-2", record_id="authrec_other"))
    assert records.revoke_for_requirement(requirement.requirement_id) == 2
    assert records.revoke_for_requirement(requirement.requirement_id) == 0
    records.revoke("authrec_do_not_exist")  # 不存在时静默（便于收尾重复调用）


# ---------------------------------------------------------------------------
# 由校验后的 ALLOW 结果形成有界记录
# ---------------------------------------------------------------------------


def _validated_allow(requirement: UserRequirement, operation: ReviewOperation) -> tuple[ReviewRequest, ReviewOutcome]:
    request = _request(requirement=requirement, operation=operation)
    raw = ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="用户原话明确要求修改指定文档",
        requirement_id=requirement.requirement_id,
        allowed_operations=(operation.kind,),
        objects=operation.objects,
        limits=("仅本次写入该文件",),
        purpose="按用户要求修改报告",
    )
    return request, validate_outcome(raw, request)


def test_authorization_record_from_outcome_is_bounded_and_reusable(db_conn):
    ledger = RequirementLedger(db_conn)
    records = AuthorizationLedger(db_conn)
    requirement = ledger.record_user_message(
        text="把 D:/work/报告.docx 第 3 段改一下", message_id="m1", turn_id="t1", topic_id="top"
    )
    operation = _operation(objects=[_obj(identity_key="vol1:file1", file_origin="user_original")])
    request, outcome = _validated_allow(requirement, operation)

    record = authorization_record_from_outcome(outcome, request=request, review_id="revaudit_1")
    records.record(record)

    assert record.requirement_id == requirement.requirement_id
    assert record.operation_kind == "write"
    assert record.decision == "allow"
    assert record.fingerprint == operation_fingerprint(operation, mode="restricted")
    assert record.objects == (
        {
            "kind": "path",
            "value": "D:/work/报告.docx",
            "identity_key": "vol1:file1",
            "file_origin": "user_original",
        },
    )
    assert record.limits == ("仅本次写入该文件",)
    assert record.review_id == "revaudit_1"
    assert record.expires_at is None

    # 同一 requirement + 同一操作指纹 + 同对象 + 同模式 → 可复用（不反复审批）
    hit = records.reuse(
        requirement_id=requirement.requirement_id,
        operation_kind=operation.kind,
        fingerprint=record.fingerprint,
    )
    assert hit is not None and hit.record_id == record.record_id


def test_authorization_record_from_outcome_rejects_non_allow():
    requirement = _requirement()
    operation = _operation()
    request = _request(requirement=requirement, operation=operation)
    confirm = ReviewOutcome(
        decision=ReviewDecision.CONFIRM,
        reason="依据不足",
        requirement_id=requirement.requirement_id,
        allowed_operations=(),
        objects=(),
        limits=(),
        purpose="",
    )
    with pytest.raises(ValueError):
        authorization_record_from_outcome(confirm, request=request)
