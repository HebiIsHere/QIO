# -*- coding: utf-8 -*-
"""独立回答（questions.answer_question）的定向用例（P2-B）。"""

from __future__ import annotations

import json
import uuid

import pytest

from agent.knowledge.lifecycle import KnowledgeService, KnowledgeState, revise_atomic
from agent.maintenance import models, store
from agent.maintenance.runner import MaintenanceRunner

from test_p2_b_support import (
    FakeCtx,
    ScriptedAdapter,
    make_knowledge,
    prepare_db,
    resolution_output,
    revise_change,
    revoke_change,
)


@pytest.fixture()
def env(db_conn):
    prepare_db(db_conn)
    return db_conn


def _pending_item(conn, item, question: str = "这条还成立吗？") -> str:
    return store.create_item(
        conn,
        kind=store.ITEM_ASK,
        status=models.PendingStatus.AWAITING_ANSWER.value,
        knowledge_ids=[
            {"id": item.id, "version": int(item.version), "chain_id": item.chain_id}
        ],
        question=question,
        evidence_ids=[item.id],
    )


def _insert_main_chat(conn, text: str) -> None:
    conn.execute(
        "INSERT INTO messages (id, role, content, created_at) VALUES (?, 'user', ?, ?)",
        ("msg_" + uuid.uuid4().hex[:8], text, "2026-10-21T00:00:00+00:00"),
    )


async def test_answer_revise_applies_without_new_approval(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(
        resolution=[
            resolution_output(
                "resolved", changes=[revise_change(item.id, "用户周末通常在家，也会出门散步")]
            )
        ]
    )
    _insert_main_chat(conn, "主对话内容不得进入独立回答")
    messages_before = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "对，周末会在家，但也会出门散步"
    )
    assert outcome.ok is True
    assert outcome.status == "processed"
    assert outcome.applied_changes == 1
    service = KnowledgeService(conn)
    current = service.resolve_current(item.id)
    assert current is not None
    assert current.content == "用户周末通常在家，也会出门散步"
    assert current.state is KnowledgeState.ACTIVE
    assert current.provenance["source"] == "user_correction"
    assert current.provenance["answer_item_id"] == item_id
    # 用户显式回答后不需要再审批：直接是当前有效版本。
    assert service.get(item.id).state is KnowledgeState.REVOKED
    answered = store.get_item(conn, item_id)
    assert answered["status"] == models.PendingStatus.PROCESSED.value
    assert answered["answer"] == "对，周末会在家，但也会出门散步"
    # 不写主对话。
    assert conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == messages_before
    assert "主对话内容不得进入独立回答" not in adapter.calls[0][0]
    assert "主对话内容不得进入独立回答" not in adapter.calls[0][1]
    assert "[[QIO:ANSWER]]" not in adapter.calls[0][0]
    from agent.prompts import PROMPTS

    assert PROMPTS["MAINTENANCE_ANSWER_SYSTEM"] in adapter.calls[0][0]
    payload = json.loads(adapter.calls[0][1])
    assert payload["item"]["id"] == item_id
    assert payload["answer"] == "对，周末会在家，但也会出门散步"
    assert payload["knowledge"][0]["id"] == item.id


async def test_answer_revoke_applies(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(
        resolution=[resolution_output("resolved", changes=[revoke_change(item.id)])]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "这条不再成立"
    )
    assert outcome.ok is True and outcome.applied_changes == 1
    service = KnowledgeService(conn)
    assert service.get(item.id).state is KnowledgeState.REVOKED
    assert store.get_item(conn, item_id)["status"] == models.PendingStatus.PROCESSED.value


async def test_answer_needs_clarification_updates_question(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(
        resolution=[
            resolution_output("needs_clarification", question="是每个周末还是部分周末？")
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "差不多吧"
    )
    assert outcome.ok is True
    assert outcome.status == "needs_clarification"
    pending = store.get_item(conn, item_id)
    assert pending["status"] == models.PendingStatus.AWAITING_ANSWER.value
    assert pending["question"] == "是每个周末还是部分周末？"
    assert KnowledgeService(conn).get(item.id).state is KnowledgeState.ACTIVE


async def test_answer_deferred_keeps_pending(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(resolution=[resolution_output("deferred")])
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "稍后再说"
    )
    assert outcome.ok is True
    assert outcome.status == "deferred"
    assert store.get_item(conn, item_id)["status"] == models.PendingStatus.DEFERRED.value
    assert [row["id"] for row in store.list_open_questions(conn)] == [item_id]


async def test_answer_no_change(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(resolution=[resolution_output("no_change")])
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "保持原样"
    )
    assert outcome.ok is True and outcome.status == "no_change"
    assert store.get_item(conn, item_id)["status"] == models.PendingStatus.PROCESSED.value
    assert KnowledgeService(conn).get(item.id).state is KnowledgeState.ACTIVE


async def test_answer_version_conflict_protects_later_modification(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    revised = revise_atomic(
        conn, item.id, fields={"content": "用户周末通常在家，但周六例外"}
    )
    adapter = ScriptedAdapter(
        resolution=[
            resolution_output("resolved", changes=[revise_change(item.id, "被覆盖的内容")])
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "改成别的"
    )
    assert outcome.ok is False
    assert outcome.status == "conflict"
    assert outcome.applied_changes == 0
    current = KnowledgeService(conn).resolve_current(item.id)
    assert current.id == revised.id
    assert current.content == "用户周末通常在家，但周六例外"
    pending = store.get_item(conn, item_id)
    assert pending["status"] == models.PendingStatus.AWAITING_ANSWER.value
    assert pending["error"]
    # 冲突时不调用模型（写前 CAS 已拒绝），不消耗模型调用。
    assert adapter.calls == []


async def test_answer_only_allows_item_knowledge(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    other = make_knowledge(conn, "用户喜欢乌龙茶")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(
        resolution=[
            resolution_output("resolved", changes=[revise_change(other.id, "越界修改")])
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "回答"
    )
    assert outcome.ok is False
    assert outcome.status == "conflict"
    assert KnowledgeService(conn).get(other.id).content == "用户喜欢乌龙茶"
    assert store.get_item(conn, item_id)["status"] == models.PendingStatus.AWAITING_ANSWER.value


async def test_answer_invalid_output_twice_marks_failed(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(resolution=["坏输出一", "坏输出二"])
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "回答"
    )
    assert outcome.ok is False
    assert outcome.status == models.PendingStatus.FAILED.value
    assert len(adapter.calls) == 2
    assert store.get_item(conn, item_id)["status"] == models.PendingStatus.FAILED.value
    assert KnowledgeService(conn).get(item.id).state is KnowledgeState.ACTIVE


async def test_answer_triggers_repair_once(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    item_id = _pending_item(conn, item)
    adapter = ScriptedAdapter(
        resolution=[
            '{"resolution": "resolved", "changes": []}',
            resolution_output("no_change"),
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).answer_question(
        item_id, "保持原样"
    )
    assert outcome.ok is True
    assert len(adapter.calls) == 2
    from agent.prompts import PROMPTS

    assert PROMPTS["STRUCTURED_REPAIR_SYSTEM"] in adapter.calls[1][0]
