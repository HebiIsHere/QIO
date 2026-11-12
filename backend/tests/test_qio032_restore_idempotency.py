# -*- coding: utf-8 -*-
"""QIO-032 回归：整批撤回的幂等与「不覆盖用户后续修改」。

先用测试确认既有保护（recovery.py 的批次状态与产物版本判断）：
1. 重复整批撤回 → already_rolled_back（不再重复写恢复版本）；
2. 用户后续修改维护产物 → 恢复被 blocked，且当前版本内容/版本号不被覆盖；
3. 一个批次里部分操作可恢复、部分被拒 → 结果状态 rollback_partial（不写成完全恢复）。

装置与 test_p2_d_restore.py 相同：真实 AppContext + fake adapter，先跑一次合法 merge
造出可恢复的维护产物。
"""

from __future__ import annotations

import importlib
import json

import pytest

CLAUSE = "QIO-032 #35（撤回幂等 / 新版本保护）"


def _load(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：{name} 不可导入：{exc!r}", pytrace=False)


@pytest.fixture()
def ctx(tmp_path, db_conn):
    _load("agent.maintenance.runner")
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext

    return AppContext(Settings(data_dir=tmp_path), db_conn, EventBus())


def _active(conn, content: str, *, category: str = "general_fact") -> str:
    from agent.knowledge.lifecycle import KnowledgeService

    ks = KnowledgeService(conn)
    item = ks.create(category=category, content=content)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item.id


def _snapshot(conn, knowledge_ids):
    models = _load("agent.maintenance.models")
    rows = list(conn.execute("SELECT seq FROM knowledge_change_log ORDER BY seq"))
    seqs = [int(r["seq"]) for r in rows]
    return models.MaintenanceSnapshot(
        batch_seq=1,
        trigger=models.MaintenanceTrigger.CHANGE_COUNT,
        change_seq_max=max(seqs) if seqs else 0,
        pending_change_count=len(seqs),
        waiting_turns=0,
        change_ids=tuple(seqs),
        knowledge_ids=tuple(knowledge_ids),
        limits=dict(models.MAINTENANCE_LIMITS),
    )


def _runner(ctx):
    return _load("agent.maintenance.runner").MaintenanceRunner(ctx)


class FakeAdapter:
    def __init__(self, payload) -> None:
        self._payload = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None) -> object:
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content=self._payload))


def _fake_build(adapter):
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=adapter)


def _active_rows(conn):
    return list(
        conn.execute(
            "SELECT id, content, state, version, provenance FROM knowledge WHERE state = 'active'"
        )
    )


def _restore_versions(conn) -> list:
    rows = []
    for row in conn.execute(
        "SELECT id, content, state, version, provenance FROM knowledge"
    ):
        try:
            provenance = json.loads(row["provenance"] or "{}")
        except (TypeError, ValueError):
            provenance = {}
        if isinstance(provenance, dict) and provenance.get("source") == "maintenance_restore":
            rows.append(row)
    return rows


def _batch_status(conn, batch_id: str) -> str:
    row = conn.execute("SELECT status FROM maintenance_batches WHERE id = ?", (batch_id,)).fetchone()
    assert row is not None, f"{CLAUSE}：找不到批次 {batch_id}"
    return str(row["status"])


def _merged_id(conn, content: str) -> str:
    rows = [
        row
        for row in conn.execute("SELECT id, content, state FROM knowledge")
        if row["state"] == "active" and row["content"] == content
    ]
    assert len(rows) == 1, f"{CLAUSE}：找不到唯一的维护产物 {content!r}（{len(rows)} 条）"
    return str(rows[0]["id"])


async def _merge_once(ctx, monkeypatch, *, first_content="示例知识甲", second_content="示例知识乙"):
    conn = ctx.conn
    first = _active(conn, first_content)
    second = _active(conn, second_content)
    adapter = FakeAdapter(
        {
            "candidates": [
                {
                    "action": "merge",
                    "knowledge_ids": [first, second],
                    "new_content": "示例知识甲乙合并",
                    "reason": "语义重复",
                }
            ]
        }
    )
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    runner = _runner(ctx)
    outcome = await runner.run_batch(_snapshot(conn, [first, second]))
    assert outcome.ok is True and outcome.applied >= 1, f"{CLAUSE}：前置 merge 未成功（{outcome}）"
    assert outcome.batch_id
    return runner, outcome.batch_id, first, second


async def test_repeated_batch_restore_is_idempotent(ctx, monkeypatch):
    """重复整批撤回返回 already_rolled_back，且不再重复写恢复版本。"""
    conn = ctx.conn
    runner, batch_id, first, second = await _merge_once(ctx, monkeypatch)

    first_result = await runner.restore(batch_id=batch_id)
    assert first_result["status"] == "rolled_back", f"{CLAUSE}：{first_result}"
    assert _batch_status(conn, batch_id) == "rolled_back"
    versions_after_first = len(_restore_versions(conn))
    rows_after_first = len(list(conn.execute("SELECT id FROM knowledge")))

    second_result = await runner.restore(batch_id=batch_id)
    assert second_result["ok"] is True
    assert second_result["status"] == "already_rolled_back", (
        f"{CLAUSE}：重复整批撤回必须如实返回 already_rolled_back，实际 {second_result['status']!r}"
    )
    assert second_result["restored"] == 0 and second_result["blocked"] == 0
    assert second_result.get("operations") == []
    # 幂等：没有新增恢复版本、没有新增知识行（历史不重复写）
    assert len(_restore_versions(conn)) == versions_after_first, (
        f"{CLAUSE}：重复撤回不得再写恢复版本"
    )
    assert len(list(conn.execute("SELECT id FROM knowledge"))) == rows_after_first, (
        f"{CLAUSE}：重复撤回不得新增知识行"
    )
    assert _batch_status(conn, batch_id) == "rolled_back"


async def test_later_user_modification_blocks_restore_without_overwrite(ctx, monkeypatch):
    """用户后续修改后恢复被 blocked，当前版本内容/版本号保持不变（旧撤回不覆盖新内容）。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    runner, batch_id, first, second = await _merge_once(ctx, monkeypatch)
    merged_id = _merged_id(conn, "示例知识甲乙合并")
    user_version = KnowledgeService(conn).revise_atomic(
        merged_id, fields={"content": "用户后来修改的内容"}
    )
    assert user_version.version == 2

    preview = await runner.preview_restore(batch_id=batch_id)
    assert preview["restoreable_count"] == 0, f"{CLAUSE}：{preview}"
    assert preview["blocked_count"] >= 1

    result = await runner.restore(batch_id=batch_id)
    assert result["status"] == "rollback_partial", f"{CLAUSE}：{result}"
    statuses = {operation["operation_id"]: operation["status"] for operation in result["operations"]}
    op_statuses = set(statuses.values())
    assert op_statuses == {"blocked"}, f"{CLAUSE}：用户修改后应整项 blocked，实际 {statuses}"
    blocked = [op for op in result["operations"] if op["status"] == "blocked"][0]
    assert blocked["reasons"], f"{CLAUSE}：blocked 必须给出原因"
    # 旧撤回没有覆盖新版本内容，也没有产生新的用户内容版本
    current = KnowledgeService(conn).resolve_current(merged_id)
    assert current is not None
    assert current.content == "用户后来修改的内容", f"{CLAUSE}：当前内容被覆盖：{current.content!r}"
    assert current.version == 2, f"{CLAUSE}：不得为被拒绝的撤回新增版本，实际 v{current.version}"
    assert _restore_versions(conn) == [], f"{CLAUSE}：被拒绝的恢复不得写恢复版本"
    assert _batch_status(conn, batch_id) == "rollback_partial"


async def _two_merges_once(ctx, monkeypatch):
    conn = ctx.conn
    ids = [_active(conn, "示例知识甲"), _active(conn, "示例知识乙"), _active(conn, "示例知识丙"), _active(conn, "示例知识丁")]
    adapter = FakeAdapter(
        {
            "candidates": [
                {
                    "action": "merge",
                    "knowledge_ids": [ids[0], ids[1]],
                    "new_content": "示例知识甲乙合并",
                    "reason": "语义重复",
                },
                {
                    "action": "merge",
                    "knowledge_ids": [ids[2], ids[3]],
                    "new_content": "示例知识丙丁合并",
                    "reason": "语义重复",
                },
            ]
        }
    )
    monkeypatch.setattr(ctx, "build_adapter", _fake_build(adapter))
    runner = _runner(ctx)
    outcome = await runner.run_batch(_snapshot(conn, ids))
    assert outcome.ok is True and outcome.applied >= 2, f"{CLAUSE}：前提两个 merge 未全部成功（{outcome}）"
    assert outcome.batch_id
    return runner, outcome.batch_id


async def test_partial_batch_restore_reports_rollback_partial(ctx, monkeypatch):
    """一个批次部分可恢复、部分被拒：状态必须是 rollback_partial，且不写成完全恢复。"""
    from agent.knowledge.lifecycle import KnowledgeService

    conn = ctx.conn
    runner, batch_id = await _two_merges_once(ctx, monkeypatch)
    blocked_product_id = _merged_id(conn, "示例知识丙丁合并")
    KnowledgeService(conn).revise_atomic(blocked_product_id, fields={"content": "用户修改了丙丁"})

    result = await runner.restore(batch_id=batch_id)
    assert result["ok"] is True
    assert result["status"] == "rollback_partial", (
        f"{CLAUSE}：部分恢复必须如实返回 rollback_partial，实际 {result['status']!r}"
    )
    assert result["restored"] == 1, f"{CLAUSE}：{result}"
    assert result["blocked"] == 1, f"{CLAUSE}：{result}"
    statuses = sorted(operation["status"] for operation in result["operations"])
    assert statuses == ["blocked", "restored"], f"{CLAUSE}：逐项状态必须一恢复一拒绝，实际 {statuses}"
    assert _batch_status(conn, batch_id) == "rollback_partial"
    # 被拒绝的那条保持用户最新版本，不被旧撤回覆盖
    current = KnowledgeService(conn).resolve_current(blocked_product_id)
    assert current is not None and current.content == "用户修改了丙丁" and current.version == 2
    # 已恢复的 merge 逐来源写恢复版本（两个来源 => 两条），都是新版本、不删除历史
    restored_rows = _restore_versions(conn)
    assert len(restored_rows) == 2, f"{CLAUSE}：merge 两个来源应各写一条恢复版本，实际 {len(restored_rows)}"
    assert all(row["state"] == "active" for row in restored_rows)
