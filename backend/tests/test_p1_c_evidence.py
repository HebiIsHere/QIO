"""Phase 1 C：轮级 / 后台证据目录（程序拥有的真实来源清单）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from agent.knowledge.evidence import (
    KIND_ATTACHMENT,
    KIND_ENTITY,
    KIND_KNOWLEDGE,
    KIND_MESSAGE,
    KIND_TOOL_RESULT,
    EvidenceCatalog,
    EvidenceEntry,
    build_background_catalog,
    build_turn_catalog,
    coerce_catalog,
    validate_epistemic_status,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_turn(conn: sqlite3.Connection, turn_id: str) -> None:
    now = _now()
    for message_id, role, content in (
        ("msg_user", "user", "我家的鹅病了"),
        ("msg_assistant", "assistant", "先观察"),
    ):
        conn.execute(
            "INSERT INTO messages (id, role, content, content_type, created_at, turn_id) "
            "VALUES (?, ?, ?, 'text', ?, ?)",
            (message_id, role, content, now, turn_id),
        )
    conn.execute(
        "INSERT INTO attachments (id, turn_id, kind, original_name, state, "
        "created_at, updated_at) VALUES ('att_1', ?, 'document', '鹅.txt', 'committed', ?, ?)",
        (turn_id, now, now),
    )
    for record_id, status in (("tr_ok", "success"), ("tr_fail", "failed")):
        conn.execute(
            "INSERT INTO tool_records (id, turn_id, tool_name, output, status, created_at) "
            "VALUES (?, ?, 'fs_read', '读取成功', ?, ?)",
            (record_id, turn_id, status, now),
        )


def test_build_turn_catalog_only_contains_real_program_owned_evidence(
    db_conn: sqlite3.Connection,
):
    _seed_turn(db_conn, "turn_1")
    catalog = build_turn_catalog(
        db_conn,
        turn_id="turn_1",
        message="我家的鹅病了",
        knowledge_snapshot=[{"item_id": "kn_1", "content": "用户养了一只鹅"}],
        entity_cards=[{"id": "ec_1", "name": "我家的鹅"}],
    )
    kinds = {entry.kind for entry in catalog.entries()}
    assert {KIND_MESSAGE, KIND_ATTACHMENT, KIND_TOOL_RESULT, KIND_KNOWLEDGE, KIND_ENTITY} <= kinds
    # 用户消息带角色：user_explicit 的依据必须能区分用户与助手。
    user_entry = catalog.get("msg_user")
    assert user_entry is not None and user_entry.role == "user"
    # 只算真实完成的工具调用：失败记录不是证据。
    assert "tr_ok" in catalog and "tr_fail" not in catalog
    assert "msg_user" in catalog and "att_1" in catalog


def test_validate_epistemic_status_uses_program_facts(db_conn: sqlite3.Connection):
    catalog = EvidenceCatalog(
        [
            EvidenceEntry(id="msg_user", kind=KIND_MESSAGE, label="用户", role="user"),
            EvidenceEntry(id="msg_ai", kind=KIND_MESSAGE, label="助手", role="assistant"),
            EvidenceEntry(id="tr_1", kind=KIND_TOOL_RESULT, label="工具"),
            EvidenceEntry(id="att_1", kind=KIND_ATTACHMENT, label="附件"),
        ]
    )
    assert validate_epistemic_status(catalog, "qio_observed", ["msg_user"]) is not None
    assert validate_epistemic_status(catalog, "qio_observed", ["tr_1"]) is None
    assert validate_epistemic_status(catalog, "qio_observed", ["att_1"]) is None
    assert validate_epistemic_status(catalog, "user_explicit", ["msg_ai"]) is not None
    assert validate_epistemic_status(catalog, "user_explicit", ["msg_user"]) is None
    assert validate_epistemic_status(catalog, "qio_inference", []) is None
    assert validate_epistemic_status(catalog, "bogus", ["msg_user"]) is not None


def test_background_catalog_contains_input_ids():
    catalog = build_background_catalog(
        fragment_id="frag_1",
        messages=[
            {"id": "msg_1", "role": "user", "content": "我喜欢清淡饮食"},
            {"id": "msg_2", "role": "assistant", "content": "记下了"},
        ],
        entity_ids=["ec_1"],
        knowledge_ids=["kn_1"],
    )
    ids = set(catalog.ids())
    assert {"frag_1", "msg_1", "msg_2", "ec_1", "kn_1"} <= ids
    assert catalog.get("msg_1").role == "user"


def test_catalog_rejects_unknown_and_coerces_lists():
    assert coerce_catalog(None).ids() == []
    catalog = coerce_catalog([{"id": "x", "kind": "tool_result", "label": "t"}])
    assert "x" in catalog
    # 坏条目（未知 kind / 空 id）不会变成证据。
    catalog = coerce_catalog(
        [{"id": "", "kind": "message"}, {"id": "y", "kind": "not_a_kind"}]
    )
    assert len(catalog) == 0
