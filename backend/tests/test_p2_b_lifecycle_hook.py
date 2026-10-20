# -*- coding: utf-8 -*-
"""lifecycle 变化记录钩子与维护域操作的定向用例（P2-B）。"""

from __future__ import annotations

from agent.knowledge.lifecycle import (
    KnowledgeService,
    KnowledgeState,
    merge_active,
    restore_version,
    revise_atomic,
)

from test_p2_b_support import (
    install_broken_changes_stub,
    install_changes_stub,
    make_knowledge,
    prepare_db,
)


def test_activate_records_effective_change(db_conn, monkeypatch):
    prepare_db(db_conn)
    calls = install_changes_stub(monkeypatch)
    item = make_knowledge(db_conn, "用户偏好清晨工作", source="user_approved")
    assert len(calls) == 1
    assert calls[0]["knowledge_id"] == item.id
    assert calls[0]["chain_id"] == item.chain_id
    assert calls[0]["version"] == int(item.version)
    assert calls[0]["source"] == "user_approved"


def test_activate_idempotent_does_not_double_record(db_conn, monkeypatch):
    prepare_db(db_conn)
    calls = install_changes_stub(monkeypatch)
    item = make_knowledge(db_conn, "用户偏好清晨工作")
    assert len(calls) == 1
    KnowledgeService(db_conn).activate(item.id)
    assert len(calls) == 1


def test_revise_atomic_records_with_its_source(db_conn, monkeypatch):
    prepare_db(db_conn)
    calls = install_changes_stub(monkeypatch)
    item = make_knowledge(db_conn, "用户偏好清晨工作", source="main_chat")
    revised = revise_atomic(
        db_conn,
        item.id,
        fields={"content": "用户偏好清晨工作，周末例外"},
        source="user_correction",
    )
    assert [call["source"] for call in calls] == ["main_chat", "user_correction"]
    assert calls[-1]["knowledge_id"] == revised.id
    assert calls[-1]["version"] == int(revised.version)


def test_hook_failure_does_not_break_knowledge_writes(db_conn, monkeypatch):
    prepare_db(db_conn)
    install_broken_changes_stub(monkeypatch)
    item = make_knowledge(db_conn, "用户偏好清晨工作")
    assert item.state is KnowledgeState.ACTIVE
    revised = revise_atomic(
        db_conn, item.id, fields={"content": "用户偏好清晨工作，周末例外"}
    )
    assert revised.state is KnowledgeState.ACTIVE


def test_merge_active_keeps_sources_history_and_notifies_recorder(db_conn, monkeypatch):
    prepare_db(db_conn)
    calls = install_changes_stub(monkeypatch)
    first = make_knowledge(db_conn, "用户喜欢乌龙茶")
    second = make_knowledge(db_conn, "用户喜欢乌龙茶（重复记录）")
    calls.clear()
    merged = merge_active(
        db_conn,
        source_ids=[first.id, second.id],
        content="用户喜欢乌龙茶",
        provenance={
            "source": "maintenance_merge",
            "merged_from": [
                {"id": first.id, "version": int(first.version), "chain_id": first.chain_id},
                {"id": second.id, "version": int(second.version), "chain_id": second.chain_id},
            ],
        },
    )
    service = KnowledgeService(db_conn)
    assert merged.state is KnowledgeState.ACTIVE
    assert service.get(first.id).state is KnowledgeState.REVOKED
    assert service.get(second.id).state is KnowledgeState.REVOKED
    assert service.get(first.id).provenance.get("superseded_by") == merged.id
    assert service.get(second.id).provenance.get("superseded_by") == merged.id
    # 新版本 provenance 保留每个来源的 id / version / chain_id（跨链合并不丢关系）。
    merged_from = merged.provenance.get("merged_from") or []
    assert {entry["id"] for entry in merged_from} == {first.id, second.id}
    # 合并产物自己是一条新链；每条来源的完整历史仍然可按链读取。
    assert merged.chain_id == merged.id
    assert {row.id for row in service.history(first.id)} == {first.id}
    assert calls and calls[-1]["source"] == "maintenance_merge"


def test_merge_active_rejects_cross_scope_sources(db_conn):
    prepare_db(db_conn)
    from agent.graph.nodes import NodeService

    nodes = NodeService(db_conn)
    topic_a = nodes.create_topic("话题A")
    topic_b = nodes.create_topic("话题B")
    first = make_knowledge(db_conn, "话题A里的偏好", nodes=[topic_a.id])
    second = make_knowledge(db_conn, "话题B里的偏好", nodes=[topic_b.id])
    import pytest

    with pytest.raises(ValueError):
        merge_active(
            db_conn,
            source_ids=[first.id, second.id],
            content="合并后",
            provenance={"source": "maintenance_merge"},
        )
    assert KnowledgeService(db_conn).get(first.id).state is KnowledgeState.ACTIVE
    assert KnowledgeService(db_conn).get(second.id).state is KnowledgeState.ACTIVE


def test_restore_version_adds_new_version_and_keeps_history(db_conn, monkeypatch):
    prepare_db(db_conn)
    calls = install_changes_stub(monkeypatch)
    item = make_knowledge(db_conn, "原始内容")
    revised = revise_atomic(
        db_conn, item.id, fields={"content": "维护后内容"}, source="maintenance_normalize"
    )
    calls.clear()
    restored = restore_version(
        db_conn,
        reference_id=item.id,
        content="原始内容",
        provenance={
            "source": "maintenance_restore",
            "restore_of": item.id,
            "batch_id": "mb_test",
        },
    )
    service = KnowledgeService(db_conn)
    assert restored.state is KnowledgeState.ACTIVE
    assert restored.content == "原始内容"
    assert int(restored.version) == int(revised.version) + 1
    assert service.get(revised.id).state is KnowledgeState.REVOKED
    history_ids = {row.id for row in service.history(item.id)}
    assert {item.id, revised.id, restored.id} <= history_ids
    assert calls and calls[-1]["source"] == "maintenance_restore"
