from __future__ import annotations

import sqlite3

import pytest

from agent.knowledge.inject import InjectionSource
from agent.knowledge.lifecycle import (
    KnowledgeService,
    KnowledgeState,
    impact_of,
)
from agent.knowledge.verify import VerificationService


@pytest.fixture()
def knowledge(db_conn: sqlite3.Connection) -> KnowledgeService:
    return KnowledgeService(db_conn)


@pytest.fixture()
def verify(db_conn: sqlite3.Connection, knowledge: KnowledgeService) -> VerificationService:
    return VerificationService(db_conn, knowledge)


def test_impact_classification():
    assert impact_of("user_profile") == "high"
    assert impact_of("goal") == "high"
    assert impact_of("general_fact") == "low"
    assert impact_of("tool_experience") == "low"


def test_full_lifecycle_low_impact(knowledge: KnowledgeService):
    item = knowledge.create(
        category="general_fact",
        content="用户偏好清淡饮食",
        provenance={"fragment_id": "f1"},
    )
    assert item.state == KnowledgeState.DRAFT
    item = knowledge.submit(item.id)
    assert item.state == KnowledgeState.PENDING_REVIEW
    item = knowledge.verify(item.id, verified_by="system")
    assert item.state == KnowledgeState.VERIFIED
    item = knowledge.activate(item.id)
    assert item.state == KnowledgeState.ACTIVE
    assert item.activated_at is not None


def test_high_impact_requires_user_confirmation(knowledge: KnowledgeService):
    item = knowledge.create(category="user_profile", content="用户不吃辣")
    knowledge.submit(item.id)
    with pytest.raises(PermissionError):
        knowledge.verify(item.id, verified_by="system")
    verified = knowledge.verify(item.id, verified_by="user")
    assert verified.state == KnowledgeState.VERIFIED


def test_invalid_transition_rejected(knowledge: KnowledgeService):
    item = knowledge.create(category="goal", content="完成项目")
    with pytest.raises(ValueError):
        knowledge.activate(item.id)  # draft -> active not allowed
    with pytest.raises(ValueError):
        knowledge.verify(item.id)  # draft -> verified not allowed


def test_reject_returns_to_draft(knowledge: KnowledgeService):
    item = knowledge.create(category="tool_experience", content="经验")
    knowledge.submit(item.id)
    item = knowledge.reject(item.id)
    assert item.state == KnowledgeState.DRAFT


def test_supersede_version_chain(knowledge: KnowledgeService):
    old = knowledge.create(
        category="user_profile", content="用户喜欢喝牛奶", provenance={"fragment_id": "f1"}
    )
    knowledge.submit(old.id)
    knowledge.verify(old.id, verified_by="user")
    old_active = knowledge.activate(old.id)
    assert old_active.state == KnowledgeState.ACTIVE

    new = knowledge.create(
        category="user_profile",
        content="用户乳糖不耐受，改喝豆浆",
        supersedes_id=old.id,
        provenance={"fragment_id": "f2"},
    )
    knowledge.submit(new.id)
    knowledge.verify(new.id, verified_by="user")
    new_active = knowledge.activate(new.id)
    assert new_active.state == KnowledgeState.ACTIVE
    # old version revoked automatically
    assert knowledge.get(old.id).state == KnowledgeState.REVOKED
    # chain walks oldest -> newest
    chain = knowledge.history(new.id)
    assert [c.id for c in chain] == [old.id, new.id]


def test_revoke_and_expire_leave_injection_surface(
    knowledge: KnowledgeService, db_conn: sqlite3.Connection
):
    source = InjectionSource(db_conn)
    a = knowledge.create(category="general_fact", content="事实 A")
    b = knowledge.create(category="general_fact", content="事实 B")
    for item_id in (a.id, b.id):
        knowledge.submit(item_id)
        knowledge.verify(item_id)
        knowledge.activate(item_id)
    assert len(source.list_active()) == 2
    knowledge.revoke(a.id)
    assert knowledge.get(a.id).state == KnowledgeState.REVOKED
    active_ids = [r["id"] for r in source.list_active()]
    assert a.id not in active_ids and b.id in active_ids
    knowledge.expire(b.id)
    assert [r["id"] for r in source.list_active()] == []


def test_injection_filter_and_grouping(
    knowledge: KnowledgeService, db_conn: sqlite3.Connection
):
    source = InjectionSource(db_conn)
    for category, content in (
        ("general_fact", "事实"),
        ("tool_experience", "经验"),
        ("user_profile", "画像"),
    ):
        item = knowledge.create(category=category, content=content)
        knowledge.submit(item.id)
        knowledge.verify(item.id, verified_by="user" if category == "user_profile" else "system")
        knowledge.activate(item.id)
    assert len(source.list_active(category="general_fact")) == 1
    grouped = source.by_category()
    assert set(grouped.keys()) == {"general_fact", "tool_experience", "user_profile"}
    excluded = source.list_active(exclude_ids={grouped["user_profile"][0]["id"]})
    assert len(excluded) == 2


def test_verification_service_review(knowledge: KnowledgeService, verify: VerificationService):
    low = knowledge.create(category="general_fact", content="事实")
    knowledge.submit(low.id)
    result = verify.review(knowledge.get(low.id), verified_by="system")
    assert result.accepted and "auto-verified" in result.reason
    assert knowledge.get(low.id).state == KnowledgeState.VERIFIED

    high = knowledge.create(category="goal", content="目标")
    knowledge.submit(high.id)
    result = verify.review(knowledge.get(high.id), verified_by="system")
    assert not result.accepted and "user confirmation" in result.reason
    result = verify.review(knowledge.get(high.id), verified_by="user")
    assert result.accepted and "confirmed by user" in result.reason


def test_verification_service_cross_validator(
    knowledge: KnowledgeService, verify: VerificationService
):
    item = knowledge.create(category="general_fact", content="可疑事实")
    knowledge.submit(item.id)
    result = verify.review(knowledge.get(item.id), cross_validator=lambda i: False)
    assert not result.accepted and "cross-validation" in result.reason
    assert knowledge.get(item.id).state == KnowledgeState.PENDING_REVIEW