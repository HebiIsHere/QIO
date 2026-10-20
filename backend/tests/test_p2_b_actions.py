# -*- coding: utf-8 -*-
"""维护动作 merge / normalize / ask 的定向用例（P2-B）。"""

from __future__ import annotations

import pytest

from agent.knowledge.lifecycle import KnowledgeService, KnowledgeState, revise_atomic
from agent.maintenance import actions, models, store

from test_p2_b_support import (
    ask_candidate,
    make_knowledge,
    merge_candidate,
    normalize_candidate,
    prepare_db,
)


@pytest.fixture()
def env(db_conn):
    prepare_db(db_conn)
    batch_id = store.create_batch(
        db_conn,
        trigger_reason=models.MaintenanceTrigger.CHANGE_COUNT.value,
        snapshot={},
        model=None,
    )
    return db_conn, batch_id


def _allowed(*items):
    return {
        item.id: {"version": int(item.version), "chain_id": item.chain_id}
        for item in items
    }


def _evidence(*items):
    return {item.id for item in items}


def _two_duplicates(conn):
    first = make_knowledge(conn, "用户喜欢乌龙茶")
    second = make_knowledge(conn, "用户喜欢乌龙茶（重复）")
    return first, second


def test_merge_applies_and_preserves_multi_source_relations(env):
    conn, batch_id = env
    first, second = _two_duplicates(conn)
    candidate = models.MaintenanceCandidate(
        **merge_candidate(
            [first.id, second.id],
            "用户喜欢乌龙茶（合并后）",
            evidence_ids=[first.id],
            reason="重复表达",
        )
    )
    outcome = actions.execute_candidate(
        conn,
        candidate,
        batch_id=batch_id,
        allowed_knowledge=_allowed(first, second),
        allowed_evidence=_evidence(first, second),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.APPLIED.value
    service = KnowledgeService(conn)
    merged = service.get(outcome.result_id)
    assert merged is not None and merged.state is KnowledgeState.ACTIVE
    assert merged.content == "用户喜欢乌龙茶（合并后）"
    provenance = merged.provenance
    assert provenance["source"] == models.SOURCE_MAINTENANCE_MERGE
    assert provenance["batch_id"] == batch_id
    assert provenance["evidence_ids"] == [first.id]
    merged_from = provenance["merged_from"]
    assert {entry["id"] for entry in merged_from} == {first.id, second.id}
    assert {int(entry["version"]) for entry in merged_from} == {
        int(first.version),
        int(second.version),
    }
    assert {entry["chain_id"] for entry in merged_from} == {
        first.chain_id,
        second.chain_id,
    }
    # 来源行撤销但保留完整历史（不删除）。
    for source in (first, second):
        row = service.get(source.id)
        assert row.state is KnowledgeState.REVOKED
        assert row.provenance.get("superseded_by") == merged.id
        # 来源行不删除：原行仍在，链历史完整可读（合并产物是一条独立新链）。
        assert source.id in {member.id for member in service.history(source.id)}
    assert merged.chain_id == merged.id
    operation = store.get_operation(conn, outcome.operation_id)
    assert operation["action"] == "merge"
    assert set(operation["target_ids"]) == {first.id, second.id}
    assert operation["result_id"] == merged.id
    assert len(operation["before"]) == 2
    item = store.get_item(conn, outcome.item_id)
    assert item["kind"] == store.ITEM_MERGE
    assert item["status"] == models.MaintenanceCandidateStatus.APPLIED.value


def test_merge_rejects_different_category_and_keeps_original(env):
    conn, batch_id = env
    first = make_knowledge(conn, "用户喜欢乌龙茶")
    second = make_knowledge(conn, "用户目标与茶无关", category="goal")
    outcome = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **merge_candidate([first.id, second.id], "合并后")
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(first, second),
        allowed_evidence=_evidence(first, second),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.REJECTED.value
    assert "类别" in outcome.reason
    service = KnowledgeService(conn)
    assert service.get(first.id).state is KnowledgeState.ACTIVE
    assert service.get(second.id).state is KnowledgeState.ACTIVE
    assert store.get_item(conn, outcome.item_id)["status"] == "rejected"


def test_merge_rejects_different_epistemic_status(env):
    conn, batch_id = env
    first = make_knowledge(conn, "用户喜欢乌龙茶", epistemic_status="user_explicit")
    second = make_knowledge(
        conn, "用户可能喜欢乌龙茶", epistemic_status="qio_inference"
    )
    outcome = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **merge_candidate([first.id, second.id], "合并后")
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(first, second),
        allowed_evidence=_evidence(first, second),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.REJECTED.value
    assert "确定程度" in outcome.reason
    service = KnowledgeService(conn)
    assert service.get(first.id).state is KnowledgeState.ACTIVE
    assert service.get(second.id).state is KnowledgeState.ACTIVE


def test_merge_rejects_different_scope(env):
    from agent.graph.nodes import NodeService

    conn, batch_id = env
    nodes = NodeService(conn)
    topic_a = nodes.create_topic("话题A")
    topic_b = nodes.create_topic("话题B")
    first = make_knowledge(conn, "话题A里的偏好", nodes=[topic_a.id])
    second = make_knowledge(conn, "话题B里的偏好", nodes=[topic_b.id])
    outcome = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **merge_candidate([first.id, second.id], "合并后")
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(first, second),
        allowed_evidence=_evidence(first, second),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.REJECTED.value
    assert "归属" in outcome.reason
    service = KnowledgeService(conn)
    assert service.get(first.id).state is KnowledgeState.ACTIVE
    assert service.get(second.id).state is KnowledgeState.ACTIVE


def test_merge_rejects_unknown_id_and_version_mismatch(env):
    conn, batch_id = env
    first, second = _two_duplicates(conn)
    unknown = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **merge_candidate([first.id, "kn_missing"], "合并后")
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(first, second),
        allowed_evidence=_evidence(first, second),
    )
    assert unknown.status == models.MaintenanceCandidateStatus.REJECTED.value

    allowed = _allowed(first, second)
    revise_atomic(conn, first.id, fields={"content": "用户喜欢乌龙茶，务必记住"})
    stale = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **merge_candidate([first.id, second.id], "合并后")
        ),
        batch_id=batch_id,
        allowed_knowledge=allowed,
        allowed_evidence=_evidence(first, second),
    )
    assert stale.status == models.MaintenanceCandidateStatus.REJECTED.value
    assert "版本" in stale.reason
    service = KnowledgeService(conn)
    assert service.get(second.id).state is KnowledgeState.ACTIVE


def test_normalize_only_changes_format_and_records_provenance(env):
    conn, batch_id = env
    item = make_knowledge(conn, "用户喜欢乌龙茶，  每天都喝。")
    candidate = models.MaintenanceCandidate(
        **normalize_candidate(item.id, "用户喜欢乌龙茶，每天都喝。")
    )
    outcome = actions.execute_candidate(
        conn,
        candidate,
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.APPLIED.value
    service = KnowledgeService(conn)
    revised = service.get(outcome.result_id)
    assert revised.content == "用户喜欢乌龙茶，每天都喝。"
    assert revised.category == item.category
    assert revised.node_ids == item.node_ids
    assert revised.provenance["source"] == models.SOURCE_MAINTENANCE_NORMALIZE
    assert revised.provenance["operation"] == "normalize"
    assert revised.provenance["batch_id"] == batch_id
    assert revised.provenance.get("epistemic_status") == item.provenance.get(
        "epistemic_status"
    )
    assert service.get(item.id).state is KnowledgeState.REVOKED
    operation = store.get_operation(conn, outcome.operation_id)
    assert operation["action"] == "normalize"


def test_normalize_rejects_noop_unknown_and_version_mismatch(env):
    conn, batch_id = env
    item = make_knowledge(conn, "用户喜欢乌龙茶。")
    noop = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(**normalize_candidate(item.id, item.content)),
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert noop.status == models.MaintenanceCandidateStatus.REJECTED.value
    assert "未产生实际变化" in noop.reason

    allowed = _allowed(item)
    revise_atomic(conn, item.id, fields={"content": "用户喜欢乌龙茶，也喜欢红茶。"})
    stale = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **normalize_candidate(item.id, "用户喜欢乌龙茶，也喜欢红茶。")
        ),
        batch_id=batch_id,
        allowed_knowledge=allowed,
        allowed_evidence=_evidence(item),
    )
    assert stale.status == models.MaintenanceCandidateStatus.REJECTED.value


def test_ask_persists_pending_and_does_not_repeat_same_question(env):
    conn, batch_id = env
    item = make_knowledge(conn, "用户周末通常在家")
    candidate = models.MaintenanceCandidate(
        **ask_candidate("你周末是否也常出门？", [item.id], evidence_ids=[item.id])
    )
    outcome = actions.execute_candidate(
        conn,
        candidate,
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.ASKED.value
    pending = store.get_item(conn, outcome.item_id)
    assert pending["kind"] == store.ITEM_ASK
    assert pending["status"] == models.PendingStatus.AWAITING_ANSWER.value
    assert pending["question"] == "你周末是否也常出门？"
    assert pending["evidence_ids"] == [item.id]
    duplicate = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **ask_candidate("你周末是否也常出门？", [item.id], evidence_ids=[item.id])
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert duplicate.status == models.MaintenanceCandidateStatus.ASKED.value
    assert duplicate.detail.get("duplicate_question") is True
    assert duplicate.item_id == outcome.item_id
    item_count = conn.execute(
        "SELECT COUNT(*) AS c FROM maintenance_items WHERE kind = 'ask'"
    ).fetchone()["c"]
    assert int(item_count) == 1
    different = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(**ask_candidate("你工作日几点起床？", [item.id])),
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert different.status == models.MaintenanceCandidateStatus.ASKED.value
    assert different.item_id != outcome.item_id


def test_ask_does_not_change_knowledge_and_rejects_unknown_ids(env):
    conn, batch_id = env
    item = make_knowledge(conn, "用户周末通常在家")
    unknown = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **ask_candidate("这条还成立吗？", ["kn_missing"])
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert unknown.status == models.MaintenanceCandidateStatus.REJECTED.value
    service = KnowledgeService(conn)
    assert service.get(item.id).state is KnowledgeState.ACTIVE


def test_unknown_evidence_id_rejects_candidate(env):
    conn, batch_id = env
    item = make_knowledge(conn, "用户喜欢乌龙茶")
    outcome = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **normalize_candidate(item.id, "用户喜欢乌龙茶。", evidence_ids=["bogus"])
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence=_evidence(item),
    )
    assert outcome.status == models.MaintenanceCandidateStatus.REJECTED.value
    assert "evidence_ids" in outcome.reason
    assert KnowledgeService(conn).get(item.id).state is KnowledgeState.ACTIVE
