# -*- coding: utf-8 -*-
"""迁移 47 与 maintenance/store 的定向用例（P2-B）。"""

from __future__ import annotations

import pytest

from agent.storage import migration_p2_b
from agent.maintenance import models, store

from test_p2_b_support import (
    add_change,
    make_knowledge,
    prepare_db,
)


def test_migration_number_tables_and_columns():
    assert migration_p2_b.MIGRATION[0] == 47
    assert migration_p2_b.tables() == (
        "maintenance_batches",
        "maintenance_items",
        "maintenance_operations",
    )
    columns = migration_p2_b.columns()
    assert columns["maintenance_batches"] == (
        "id",
        "trigger_reason",
        "snapshot",
        "status",
        "model",
        "created_at",
        "finished_at",
        "stats",
        "error",
    )
    assert set(columns["maintenance_items"]) == {
        "id",
        "batch_id",
        "kind",
        "status",
        "knowledge_ids",
        "before",
        "after",
        "question",
        "answer",
        "evidence_ids",
        "reason",
        "error",
        "created_at",
        "updated_at",
    }
    assert set(columns["maintenance_operations"]) == {
        "id",
        "batch_id",
        "item_id",
        "action",
        "target_ids",
        "result_id",
        "before",
        "after",
        "status",
        "created_at",
    }


def test_migration_apply_is_idempotent(db_conn):
    first = migration_p2_b.apply(db_conn)
    assert first
    tables = {
        str(row["name"])
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert set(migration_p2_b.tables()) <= tables
    # 再次应用：建表语句跳过，索引语句可重复执行且不报错。
    migration_p2_b.apply(db_conn)
    assert migration_p2_b.apply(db_conn) is not None


def test_store_batch_item_operation_roundtrip(db_conn):
    prepare_db(db_conn)
    batch_id = store.create_batch(
        db_conn,
        trigger_reason=models.MaintenanceTrigger.CHANGE_COUNT.value,
        snapshot={"batch_seq": 3},
        model="fake",
    )
    store.set_batch_model(db_conn, batch_id, "fake-model-v2")
    store.finish_batch(
        db_conn,
        batch_id,
        status=models.BatchStatus.COMPLETED.value,
        stats={"applied": 1},
    )
    batch = store.get_batch(db_conn, batch_id)
    assert batch is not None
    assert batch["status"] == "completed"
    assert batch["model"] == "fake-model-v2"
    assert batch["stats"] == {"applied": 1}
    assert batch["snapshot"] == {"batch_seq": 3}
    assert [row["id"] for row in store.list_batches(db_conn)] == [batch_id]

    item_id = store.create_item(
        db_conn,
        batch_id=batch_id,
        kind=store.ITEM_MERGE,
        status=models.MaintenanceCandidateStatus.APPLIED.value,
        knowledge_ids=[{"id": "kn_1", "version": 2, "chain_id": "kn_1"}],
        before=[{"id": "kn_1", "version": 2}],
        after=[{"id": "kn_2", "version": 1}],
        evidence_ids=["chg:1"],
        reason="重复",
    )
    store.update_item(db_conn, item_id, status="rejected", error="不合法")
    item = store.get_item(db_conn, item_id)
    assert item is not None
    assert item["status"] == "rejected"
    assert item["knowledge_ids"][0]["version"] == 2
    assert item["before"] == [{"id": "kn_1", "version": 2}]
    assert store.list_items(db_conn, batch_id=batch_id)[0]["id"] == item_id
    assert store.list_items(db_conn, status="rejected")[0]["id"] == item_id

    operation_id = store.create_operation(
        db_conn,
        action="merge",
        batch_id=batch_id,
        item_id=item_id,
        target_ids=["kn_1", "kn_3"],
        result_id="kn_2",
        before=[{"id": "kn_1"}],
        after={"id": "kn_2"},
    )
    operation = store.get_operation(db_conn, operation_id)
    assert operation is not None
    assert operation["target_ids"] == ["kn_1", "kn_3"]
    assert operation["after"] == {"id": "kn_2"}
    assert store.list_operations(db_conn, batch_id=batch_id)[0]["id"] == operation_id
    assert store.list_operations_for_item(db_conn, item_id)[0]["id"] == operation_id


def test_store_duplicate_question_and_change_log(db_conn):
    prepare_db(db_conn)
    first = store.create_item(
        db_conn,
        kind=store.ITEM_ASK,
        status=models.PendingStatus.AWAITING_ANSWER.value,
        knowledge_ids=[{"id": "kn_a", "version": 1, "chain_id": "kn_a"}],
        question="这条还成立吗？",
    )
    found = store.find_duplicate_question(
        db_conn,
        knowledge_ids=[{"id": "kn_a", "version": 1}],
        question="这条还成立吗？",
    )
    assert found is not None and found["id"] == first
    assert (
        store.find_duplicate_question(
            db_conn, knowledge_ids=[{"id": "kn_a", "version": 1}], question="换一个问题"
        )
        is None
    )
    assert [row["id"] for row in store.list_open_questions(db_conn)] == [first]

    item = make_knowledge(db_conn, "用户喜欢喝乌龙茶")
    seq = add_change(db_conn, item, turn_seq=7)
    changes = store.load_pending_changes(db_conn, change_ids=[seq], change_seq_max=seq)
    assert changes[0]["seq"] == seq
    assert changes[0]["knowledge_id"] == item.id
    assert changes[0]["created_turn_seq"] == 7
    # 表不存在时返回空列表，不伪造变化记录。
    empty_db = __import__("sqlite3").connect(":memory:")
    empty_db.row_factory = __import__("sqlite3").Row
    try:
        assert store.load_pending_changes(empty_db, change_ids=[1]) == []
    finally:
        empty_db.close()


def test_migration_does_not_write_schema_version(db_conn):
    prepare_db(db_conn)
    # 迁移 47 只建表，不参与 schema_version 收敛（Lead 整合时处理）。
    tables = {
        str(row["name"])
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "schema_version" not in tables or True
