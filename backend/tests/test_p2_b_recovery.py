# -*- coding: utf-8 -*-
"""安全恢复（recovery）的定向用例（P2-B）。"""

from __future__ import annotations

import pytest

from agent.knowledge.lifecycle import (
    KnowledgeService,
    KnowledgeState,
    restore_version,
    revise_atomic,
)
from agent.maintenance import actions, models, store
from agent.maintenance.runner import MaintenanceRunner

from test_p2_b_support import (
    FakeCtx,
    make_knowledge,
    merge_candidate,
    normalize_candidate,
    prepare_db,
)


@pytest.fixture()
def env(db_conn):
    prepare_db(db_conn)
    return db_conn


def _allowed(*items):
    return {
        item.id: {"version": int(item.version), "chain_id": item.chain_id}
        for item in items
    }


def _new_batch(conn, trigger="change_count") -> str:
    return store.create_batch(
        conn, trigger_reason=trigger, snapshot={}, model=None
    )


def _merge_batch(conn):
    first = make_knowledge(conn, "用户喜欢乌龙茶")
    second = make_knowledge(conn, "用户喜欢乌龙茶（重复）")
    batch_id = _new_batch(conn)
    outcome = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **merge_candidate([first.id, second.id], "用户喜欢乌龙茶（合并）")
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(first, second),
        allowed_evidence={first.id, second.id},
    )
    assert outcome.status == "applied"
    store.finish_batch(
        conn, batch_id, status=models.BatchStatus.COMPLETED.value, stats={}
    )
    return batch_id, first, second, outcome


def _normalize_batch(conn):
    item = make_knowledge(conn, "用户喜欢 乌龙茶 。")
    batch_id = _new_batch(conn)
    outcome = actions.execute_candidate(
        conn,
        models.MaintenanceCandidate(
            **normalize_candidate(item.id, "用户喜欢乌龙茶。")
        ),
        batch_id=batch_id,
        allowed_knowledge=_allowed(item),
        allowed_evidence={item.id},
    )
    assert outcome.status == "applied"
    store.finish_batch(
        conn, batch_id, status=models.BatchStatus.COMPLETED.value, stats={}
    )
    return batch_id, item, outcome


async def test_preview_restore_merge_does_not_write(env):
    conn = env
    batch_id, first, second, outcome = _merge_batch(conn)
    runner = MaintenanceRunner(FakeCtx(conn, None))
    knowledge_before = conn.execute(
        "SELECT COUNT(*) AS c FROM knowledge"
    ).fetchone()["c"]
    operations_before = conn.execute(
        "SELECT COUNT(*) AS c FROM maintenance_operations"
    ).fetchone()["c"]
    preview = await runner.preview_restore(batch_id=batch_id)
    assert preview["ok"] is True
    assert preview["restoreable"] is True
    assert len(preview["operations"]) == 1
    plan = preview["operations"][0]
    assert plan["action"] == "merge"
    assert plan["product"]["state"] == "current"
    assert len(plan["targets"]) == 2
    assert preview["impact"]["creates_versions"] == 2
    assert conn.execute("SELECT COUNT(*) AS c FROM knowledge").fetchone()["c"] == knowledge_before
    assert (
        conn.execute("SELECT COUNT(*) AS c FROM maintenance_operations").fetchone()["c"]
        == operations_before
    )


async def test_restore_whole_merge_batch_restores_each_source(env):
    conn = env
    batch_id, first, second, outcome = _merge_batch(conn)
    runner = MaintenanceRunner(FakeCtx(conn, None))
    result = await runner.restore(batch_id=batch_id)
    assert result["ok"] is True
    assert result["status"] == models.BatchStatus.ROLLED_BACK.value
    assert result["restored"] == 1 and result["blocked"] == 0
    service = KnowledgeService(conn)
    # 每个来源逐来源恢复为新的 active 版本，内容与原始一致。
    for source in (first, second):
        current = service.resolve_current(source.id)
        assert current is not None and current.state is KnowledgeState.ACTIVE
        assert current.id != source.id
        assert current.content == source.content
        assert current.provenance["source"] == models.SOURCE_MAINTENANCE_RESTORE
        assert current.provenance["restore_of"] == source.id
        assert current.provenance["batch_id"] == batch_id
    # 维护产物撤销但保留历史行。
    product = service.get(outcome.result_id)
    assert product.state is KnowledgeState.REVOKED
    # 恢复记录可回放：restore 事项 + restore 操作。
    restore_items = store.list_items(conn, kind=store.ITEM_RESTORE)
    assert len(restore_items) == 1
    assert restore_items[0]["status"] == "applied"
    restore_ops = [
        op
        for op in store.list_operations(conn, batch_id=batch_id)
        if op["action"] == "restore"
    ]
    assert len(restore_ops) == 1
    batch = store.get_batch(conn, batch_id)
    assert batch["status"] == models.BatchStatus.ROLLED_BACK.value
    assert batch["stats"]["rollback"]["restored_operations"] == 1
    # 幂等：再撤一次直接报告已撤回，不重复写。
    again = await runner.restore(batch_id=batch_id)
    assert again["status"] == "already_rolled_back"


async def test_restore_protects_later_user_modification_of_product(env):
    conn = env
    batch_id, first, second, outcome = _merge_batch(conn)
    user_version = revise_atomic(
        conn, outcome.result_id, fields={"content": "用户后来手动改的合并内容"}
    )
    runner = MaintenanceRunner(FakeCtx(conn, None))
    preview = await runner.preview_restore(item_id=outcome.item_id)
    assert preview["ok"] is True
    assert preview["restoreable"] is False
    assert preview["operations"][0]["product"]["state"] == "modified"
    assert preview["warnings"]
    result = await runner.restore(item_id=outcome.item_id)
    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["blocked"] >= 1
    assert result["blocked_reasons"]
    service = KnowledgeService(conn)
    current = service.resolve_current(outcome.result_id)
    assert current.id == user_version.id
    assert current.content == "用户后来手动改的合并内容"
    # 用户后续修改没有被覆盖；来源也没有被恢复成 active。
    assert service.get(first.id).state is KnowledgeState.REVOKED
    assert service.get(second.id).state is KnowledgeState.REVOKED


async def test_restore_merge_partial_when_one_source_chain_changed(env):
    conn = env
    batch_id, first, second, outcome = _merge_batch(conn)
    # 模拟用户在合并后对其中一个来源所在链又做了修改（新版本行）。
    restore_version(
        conn,
        reference_id=second.id,
        content="用户后来在第二条记录上的修改",
        provenance={"source": "user_approved"},
    )
    runner = MaintenanceRunner(FakeCtx(conn, None))
    preview = await runner.preview_restore(batch_id=batch_id)
    merge_plan = preview["operations"][0]
    statuses = {
        merge_plan["targets"][0]["knowledge_id"]: merge_plan["targets"][0]["status"],
        merge_plan["targets"][1]["knowledge_id"]: merge_plan["targets"][1]["status"],
    }
    assert statuses[second.id] == "blocked"
    assert statuses[first.id] == "restoreable"
    result = await runner.restore(batch_id=batch_id)
    assert result["ok"] is True
    assert result["status"] == models.BatchStatus.ROLLBACK_PARTIAL.value
    # 操作级：这条操作部分恢复（1 个来源恢复、1 个来源拒绝覆盖）。
    assert result["blocked"] == 1
    partial_op = result["operations"][0]
    assert partial_op["status"] == "partial"
    assert len(partial_op["restored_knowledge_ids"]) == 1
    assert len(partial_op["blocked_knowledge_ids"]) == 1
    service = KnowledgeService(conn)
    # 安全的来源恢复；有后续修改的来源保留用户版本；维护产物保留（不丢信息）。
    assert service.resolve_current(first.id).content == first.content
    assert service.resolve_current(second.id).content == "用户后来在第二条记录上的修改"
    assert service.get(outcome.result_id).state is KnowledgeState.ACTIVE
    batch = store.get_batch(conn, batch_id)
    assert batch["status"] == models.BatchStatus.ROLLBACK_PARTIAL.value


async def test_restore_normalize_by_item_and_idempotent(env):
    conn = env
    batch_id, item, outcome = _normalize_batch(conn)
    runner = MaintenanceRunner(FakeCtx(conn, None))
    preview = await runner.preview_restore(item_id=outcome.item_id)
    assert preview["restoreable"] is True
    result = await runner.restore(item_id=outcome.item_id)
    assert result["ok"] is True
    assert result["status"] == "restored"
    service = KnowledgeService(conn)
    current = service.resolve_current(item.id)
    assert current.content == item.content
    assert current.provenance["source"] == models.SOURCE_MAINTENANCE_RESTORE
    assert current.provenance["restore_of"] == item.id
    assert current.provenance["batch_id"] == batch_id
    assert service.get(outcome.result_id).state is KnowledgeState.REVOKED
    again = await runner.restore(item_id=outcome.item_id)
    assert again["status"] in ("already_restored", "nothing_to_restore")


async def test_preview_and_restore_require_valid_target(env):
    conn = env
    runner = MaintenanceRunner(FakeCtx(conn, None))
    preview = await runner.preview_restore()
    assert preview["ok"] is False
    assert preview["status"] == "invalid_request"
    missing = await runner.preview_restore(batch_id="mb_missing")
    assert missing["ok"] is False and missing["status"] == "not_found"
    missing_item = await runner.restore(item_id="mi_missing")
    assert missing_item["ok"] is False and missing_item["status"] == "not_found"
