# -*- coding: utf-8 -*-
"""MaintenanceRunner.run_batch 的定向用例（P2-B）。"""

from __future__ import annotations

import json
import uuid

import pytest

from agent.knowledge.lifecycle import KnowledgeService, KnowledgeState
from agent.maintenance import models, store
from agent.maintenance.runner import MaintenanceRunner

from test_p2_b_support import (
    FakeCtx,
    ScriptedAdapter,
    add_change,
    make_knowledge,
    make_snapshot,
    maintenance_output,
    merge_candidate,
    ask_candidate,
    prepare_db,
)


@pytest.fixture()
def env(db_conn):
    prepare_db(db_conn)
    return db_conn


def _insert_main_chat(conn, text: str) -> None:
    conn.execute(
        "INSERT INTO messages (id, role, content, created_at) VALUES (?, 'user', ?, ?)",
        ("msg_" + uuid.uuid4().hex[:8], text, "2026-10-21T00:00:00+00:00"),
    )


def _dedupe_setup(conn):
    first = make_knowledge(conn, "用户喜欢乌龙茶")
    second = make_knowledge(conn, "用户喜欢乌龙茶（重复）")
    seq = add_change(conn, second, turn_seq=5)
    snapshot = make_snapshot(
        changes=[{"seq": seq}], knowledge_ids=[first.id, second.id]
    )
    return first, second, seq, snapshot


async def test_run_batch_merge_end_to_end_and_prompt_isolation(env):
    conn = env
    first, second, seq, snapshot = _dedupe_setup(conn)
    adapter = ScriptedAdapter(
        maintenance=[
            maintenance_output(
                [
                    merge_candidate(
                        [first.id, second.id],
                        "用户喜欢乌龙茶（合并后）",
                        evidence_ids=[f"chg:{seq}"],
                    )
                ]
            )
        ]
    )
    ctx = FakeCtx(conn, adapter)
    runner = MaintenanceRunner(ctx)
    _insert_main_chat(conn, "主对话里的私密原文不应进入维护提示词")
    messages_before = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]

    outcome = await runner.run_batch(snapshot)

    assert outcome.ok is True
    assert outcome.status == models.BatchStatus.COMPLETED.value
    assert outcome.processed_change_seq == snapshot.change_seq_max
    assert outcome.applied == 1
    assert outcome.rejected == 0
    batch = store.get_batch(conn, outcome.batch_id)
    assert batch["status"] == models.BatchStatus.COMPLETED.value
    assert batch["stats"]["applied"] == 1
    assert batch["model"] == "fake-p2-b"
    merged = [
        item
        for item in KnowledgeService(conn).list_items(state="active")
        if item.content.startswith("用户喜欢乌龙茶（合并后）")
    ]
    assert len(merged) == 1

    # 模型输入：恰好 system + 一条独立 JSON user；无回答标记、无主对话消息历史。
    assert len(adapter.calls) == 1
    system, user = adapter.calls[0]
    assert "[[QIO:ANSWER]]" not in system
    assert "[[QIO:ANSWER]]" not in user
    assert "主对话里的私密原文不应进入维护提示词" not in system
    assert "主对话里的私密原文不应进入维护提示词" not in user
    from agent.prompts import PROMPTS

    assert PROMPTS["MAINTENANCE_SYSTEM"] in system
    payload = json.loads(user)
    assert payload["pending_changes"][0]["id"] == f"chg:{seq}"
    assert payload["pending_changes"][0]["knowledge_id"] == second.id
    assert payload["pending_changes"][0]["created_turn_seq"] == 5
    assert {entry["id"] for entry in payload["knowledge"]} >= {first.id, second.id}
    assert payload["limits"] == dict(models.MAINTENANCE_LIMITS)
    assert conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == messages_before


async def test_run_batch_invalid_candidate_rejected_keeps_original(env):
    conn = env
    first, second, seq, snapshot = _dedupe_setup(conn)
    adapter = ScriptedAdapter(
        maintenance=[
            maintenance_output(
                [merge_candidate([first.id, "kn_missing"], "合并后")]
            )
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).run_batch(snapshot)
    assert outcome.ok is True
    assert outcome.status == models.BatchStatus.PARTIAL.value
    assert outcome.rejected == 1 and outcome.applied == 0
    service = KnowledgeService(conn)
    assert service.get(first.id).state is KnowledgeState.ACTIVE
    assert service.get(second.id).state is KnowledgeState.ACTIVE
    assert not [
        item
        for item in service.list_items()
        if item.content == "合并后" and item.state is KnowledgeState.ACTIVE
    ]


async def test_run_batch_repairs_at_most_once_then_succeeds(env):
    conn = env
    first, second, seq, snapshot = _dedupe_setup(conn)
    adapter = ScriptedAdapter(
        maintenance=[
            "{不是合法 JSON",
            maintenance_output(
                [merge_candidate([first.id, second.id], "合并后")]
            ),
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).run_batch(snapshot)
    assert outcome.ok is True
    assert len(adapter.calls) == 2
    assert outcome.detail["repair"] is True
    system, _user = adapter.calls[1]
    from agent.prompts import PROMPTS

    assert PROMPTS["STRUCTURED_REPAIR_SYSTEM"] in system
    assert "不是合法 JSON" in system
    assert "field_errors" in system


async def test_run_batch_fails_without_writes_when_output_invalid_twice(env):
    conn = env
    first, second, seq, snapshot = _dedupe_setup(conn)
    adapter = ScriptedAdapter(maintenance=["坏输出一", "坏输出二"])
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).run_batch(snapshot)
    assert outcome.ok is False
    assert outcome.status == models.BatchStatus.FAILED.value
    assert outcome.processed_change_seq is None
    assert len(adapter.calls) == 2
    service = KnowledgeService(conn)
    assert service.get(first.id).state is KnowledgeState.ACTIVE
    assert service.get(second.id).state is KnowledgeState.ACTIVE
    batch = store.get_batch(conn, outcome.batch_id)
    assert batch["status"] == models.BatchStatus.FAILED.value
    assert batch["error"]


async def test_run_batch_model_failure_keeps_data_and_position(env):
    conn = env
    first, second, seq, snapshot = _dedupe_setup(conn)

    class BrokenAdapter:
        mode = "native"
        model = "broken"

        async def complete(self, messages, tools, **kwargs):
            raise RuntimeError("provider down")

    outcome = await MaintenanceRunner(FakeCtx(conn, BrokenAdapter())).run_batch(snapshot)
    assert outcome.ok is False
    assert outcome.status == models.BatchStatus.FAILED.value
    assert outcome.processed_change_seq is None
    service = KnowledgeService(conn)
    assert service.get(first.id).state is KnowledgeState.ACTIVE
    assert service.get(second.id).state is KnowledgeState.ACTIVE


async def test_run_batch_ask_persists_pending_question(env):
    conn = env
    item = make_knowledge(conn, "用户周末通常在家")
    seq = add_change(conn, item, turn_seq=2)
    snapshot = make_snapshot(changes=[{"seq": seq}], knowledge_ids=[item.id])
    adapter = ScriptedAdapter(
        maintenance=[
            maintenance_output(
                [ask_candidate("你周末是否也常出门？", [item.id], evidence_ids=[item.id])]
            )
        ]
    )
    outcome = await MaintenanceRunner(FakeCtx(conn, adapter)).run_batch(snapshot)
    assert outcome.ok is True
    assert outcome.asked == 1
    pending = [
        row for row in store.list_items(conn, kind=store.ITEM_ASK)
        if row["status"] == models.PendingStatus.AWAITING_ANSWER.value
    ]
    assert len(pending) == 1
    assert pending[0]["question"] == "你周末是否也常出门？"
    assert KnowledgeService(conn).get(item.id).state is KnowledgeState.ACTIVE
