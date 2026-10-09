"""B 组 R02/R03：知识版本链、原子替换、并发纠正、写失败回滚。

只用受控手段：SAVEPOINT/触发器注入一次写失败、两条真实连接 + barrier 模拟并发，
不联网、不等待、不调用真实模型。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from agent.knowledge.lifecycle import (
    KnowledgeService,
    KnowledgeState,
    VersionConflict,
    deactivate_atomic,
    list_duplicate_active_chains,
    resolve_current,
    revise_atomic,
)
from agent.knowledge.verify import VerificationService
from agent.storage.db import connect
from agent.tools.knowledge_correction import CorrectKnowledgeTool

FAIL_MARKER = "RM_B_INJECT_FAIL_MARKER"


# -- helpers ----------------------------------------------------------------


def _active(ks: KnowledgeService, content: str, category: str = "general_fact"):
    item = ks.create(category=category, content=content)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    return ks.activate(item.id)


def _active_count(conn: sqlite3.Connection, content_like: str = "%") -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge WHERE state = 'active' AND content LIKE ?",
        (content_like,),
    ).fetchone()["n"]


def _row_state(conn: sqlite3.Connection, knowledge_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM knowledge WHERE id = ?", (knowledge_id,)
    ).fetchone()
    return str(row["state"]) if row is not None else None


def _tool(conn: sqlite3.Connection, snapshot: list[dict]) -> CorrectKnowledgeTool:
    return CorrectKnowledgeTool(conn, snapshot_provider=lambda: snapshot)


# -- R02：连续纠正 -----------------------------------------------------------


def test_sequential_double_correction_leaves_single_active_successor(db_conn):
    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "用户喜欢喝牛奶")
    v2 = ks.revise_atomic(v1.id, v1.version, {"content": "用户乳糖不耐受"}, actor="user")

    # 再拿**旧版本**去纠正：版本不符 → 明确冲突，不新增第二个 active 后继
    with pytest.raises(VersionConflict) as info:
        ks.revise_atomic(v1.id, v1.version, {"content": "用户改喝豆奶"}, actor="user")

    assert info.value.current_id == v2.id
    assert info.value.status_code == 409
    assert _row_state(db_conn, v1.id) == "revoked"
    assert _row_state(db_conn, v2.id) == "active"
    chain = ks.history(v2.id)
    assert [i.id for i in chain] == [v1.id, v2.id]
    assert len(chain) == 2, "冲突的第二次纠正不得留下半截版本"
    # 第二条链（v2）当前版本唯一
    assert resolve_current(db_conn, v1.id).id == v2.id


async def test_stale_snapshot_correction_targets_the_current_version(db_conn):
    """旧注入快照连续两次纠正同一旧版本：链仍线性、只有一个 active。"""
    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "用户住在成都")
    stale_snapshot = [{"item_id": v1.id, "content": v1.content}]

    first = await _tool(db_conn, stale_snapshot).run(content="用户住在成都", new_content="用户搬到了重庆")
    assert first.ok
    second = await _tool(db_conn, stale_snapshot).run(content="用户住在成都", new_content="用户又搬到了西安")
    assert second.ok

    current = resolve_current(db_conn, v1.id)
    assert current is not None and current.content == "用户又搬到了西安"
    assert _active_count(db_conn, "%搬%") == 1
    ids = [i.id for i in ks.history(current.id)]
    assert ids[0] == v1.id and len(ids) == 3, ids
    # 只有链尾是 active
    states = {i.id: i.state.value for i in ks.history(current.id)}
    assert states[current.id] == "active"
    assert [s for s in states.values() if s == "active"] == ["active"]


def test_concurrent_corrections_from_two_connections_only_one_wins(db_conn):
    ks = KnowledgeService(db_conn)
    old = _active(ks, "当前结论：用 SQLite")

    db_path = Path(db_conn.execute("PRAGMA database_list").fetchone()["file"])
    conn2 = connect(db_path)
    try:
        barrier = threading.Barrier(2)
        results: list[tuple[str, str | None]] = []
        lock = threading.Lock()

        def worker(svc: KnowledgeService, tag: str) -> None:
            barrier.wait(timeout=10)
            try:
                item = svc.revise_atomic(
                    old.id, old.version, {"content": f"并发纠正 {tag}"}, actor=tag
                )
                outcome = ("ok", item.id)
            except VersionConflict as exc:
                outcome = ("conflict", exc.current_id)
            with lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(KnowledgeService(db_conn), "A")),
            threading.Thread(target=worker, args=(KnowledgeService(conn2), "B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not t.is_alive() for t in threads)

        ok = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        assert len(ok) == 1 and len(conflicts) == 1, results

        # 无论谁赢：链上只有一个 active；冲突方有明确的当前版本
        current = resolve_current(db_conn, old.id)
        assert current is not None
        assert current.id == ok[0][1]
        assert conflicts[0][1] == current.id
        assert _active_count(db_conn, "%并发纠正%") == 1
    finally:
        conn2.close()


def test_other_independent_knowledge_is_untouched(db_conn):
    ks = KnowledgeService(db_conn)
    target = _active(ks, "目标知识：旧版本")
    other = _active(ks, "无关知识：保持原样")

    ks.revise_atomic(target.id, target.version, {"content": "目标知识：新版本"}, actor="user")

    untouched = ks.get(other.id)
    assert untouched is not None
    assert untouched.state is KnowledgeState.ACTIVE
    assert untouched.content == "无关知识：保持原样"
    assert [i.id for i in ks.history(other.id)] == [other.id]


# -- R02：删除要核对当前版本 --------------------------------------------------


async def test_delete_of_superseded_entry_is_not_reported_as_current_deletion(db_conn):
    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "用户讨厌喝牛奶")
    v2 = ks.revise_atomic(v1.id, v1.version, {"content": "用户改喝豆浆"}, actor="user")

    outcome = ks.deactivate_atomic(v1.id)
    assert outcome.deleted is False
    assert outcome.already_inactive is True
    # 当前有效版本没有被冒充删除
    assert _row_state(db_conn, v2.id) == "active"

    tool = _tool(db_conn, [{"item_id": v1.id, "content": v1.content}])
    result = await tool.run(content=v1.content, delete=True)
    assert result.ok
    assert "当前有效版本" in result.content
    assert _row_state(db_conn, v2.id) == "revoked"


def test_stale_active_row_in_same_chain_cannot_be_deleted(db_conn):
    """历史库同链多 active：删旧行必须冲突，不能声称「当前已删」。"""
    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "历史重复有效：旧")
    rows = db_conn.execute(
        "INSERT INTO knowledge (id, category, state, content, supersedes_id, "
        "node_ids, created_at, updated_at, activated_at) "
        "VALUES ('kn_rm_b_dup2', 'general_fact', 'active', '历史重复有效：新', ?, '[]', ?, ?, ?)",
        (v1.id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    assert rows.rowcount == 1

    with pytest.raises(VersionConflict) as info:
        deactivate_atomic(db_conn, v1.id, expected_version=1)
    assert info.value.current_id == "kn_rm_b_dup2"
    assert _row_state(db_conn, v1.id) == "active"
    assert _row_state(db_conn, "kn_rm_b_dup2") == "active"
    # 只读地把冲突列出来（保守：不删内容、不改历史）
    conflicts = list_duplicate_active_chains(db_conn)
    assert len(conflicts) == 1
    assert set(conflicts[0].active_ids) == {v1.id, "kn_rm_b_dup2"}
    assert "历史重复有效：旧" in conflicts[0].contents


# -- R03：激活/替换处写失败整体回滚 -------------------------------------------


def test_insert_failure_during_revise_rolls_back_old_version(db_conn):
    ks = KnowledgeService(db_conn)
    old = _active(ks, "旧事实：版本一")
    db_conn.execute(
        "CREATE TRIGGER rm_b_block_insert BEFORE INSERT ON knowledge "
        f"WHEN NEW.content LIKE '%{FAIL_MARKER}%' "
        "BEGIN SELECT RAISE(ABORT, 'rm-b injected activation failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            revise_atomic(db_conn, old.id, old.version, {"content": f"新事实 {FAIL_MARKER}"})
    finally:
        db_conn.execute("DROP TRIGGER rm_b_block_insert")

    # 旧版本仍然 active，新版本没有被误标成功
    assert _row_state(db_conn, old.id) == "active"
    assert _active_count(db_conn, "%事实%") == 1
    assert (
        db_conn.execute(
            "SELECT COUNT(*) AS n FROM knowledge WHERE content LIKE ?", (f"%{FAIL_MARKER}%",)
        ).fetchone()["n"]
        == 0
    )
    assert [i.id for i in ks.history(old.id)] == [old.id]

    # 解除注入后正常纠正：只有新版本生效
    new = revise_atomic(db_conn, old.id, old.version, {"content": "新事实：版本二"})
    assert _row_state(db_conn, old.id) == "revoked"
    assert _row_state(db_conn, new.id) == "active"
    assert _active_count(db_conn, "%事实%") == 1
    assert [i.id for i in ks.history(new.id)] == [old.id, new.id]


def test_activate_failure_keeps_old_version_active(db_conn):
    ks = KnowledgeService(db_conn)
    old = _active(ks, "稳定结论：旧版")
    new = ks.create(
        category="general_fact",
        content=f"稳定结论：新版 {FAIL_MARKER}",
        supersedes_id=old.id,
    )
    ks.submit(new.id)
    ks.verify(new.id, verified_by="system")

    db_conn.execute(
        "CREATE TRIGGER rm_b_block_activate BEFORE UPDATE ON knowledge "
        f"WHEN NEW.state = 'active' AND NEW.content LIKE '%{FAIL_MARKER}%' "
        "BEGIN SELECT RAISE(ABORT, 'rm-b injected activate failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            ks.activate(new.id)
    finally:
        db_conn.execute("DROP TRIGGER rm_b_block_activate")

    assert _row_state(db_conn, old.id) == "active"
    assert _row_state(db_conn, new.id) == "verified"
    assert _active_count(db_conn, "稳定结论%") == 1

    # 解除后正常激活：旧版本自动撤销，只有新版本生效
    activated = ks.activate(new.id)
    assert activated.state is KnowledgeState.ACTIVE
    assert _row_state(db_conn, old.id) == "revoked"
    assert _active_count(db_conn, "稳定结论%") == 1


def test_activate_rejects_unknown_expected_version(db_conn):
    ks = KnowledgeService(db_conn)
    item = ks.create(category="general_fact", content="版本核对")
    ks.submit(item.id)
    ks.verify(item.id, verified_by="system")
    with pytest.raises(VersionConflict):
        ks.activate(item.id, expected_version=99)


def test_rejects_second_active_in_same_chain(db_conn):
    """同链已存在同号 active（fork）时激活必须冲突，不能新增第二个 active。"""
    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "分叉测试：v1")
    first = ks.revise_atomic(v1.id, v1.version, {"content": "分叉测试：v2a"}, actor="user")
    forked = ks.create(
        category="general_fact", content="分叉测试：v2b", supersedes_id=v1.id
    )
    ks.submit(forked.id)
    ks.verify(forked.id, verified_by="system")
    with pytest.raises(VersionConflict) as info:
        ks.activate(forked.id)
    assert info.value.current_id == first.id
    assert _active_count(db_conn, "分叉测试%") == 1


def test_verification_service_still_works_with_new_rule(db_conn):
    ks = KnowledgeService(db_conn)
    verify = VerificationService(db_conn, ks)
    item = ks.create(category="general_fact", content="低影响自动验证")
    ks.submit(item.id)
    result = verify.review(ks.get(item.id), verified_by="system")
    assert result.accepted
    activated = ks.activate(item.id)
    assert activated.state is KnowledgeState.ACTIVE


# -- 迁移 24 落地后的「列优先」路径（列存在必须真的用列，不能两套语义）---------


def _add_chain_columns(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE knowledge ADD COLUMN chain_id TEXT")
    conn.execute("ALTER TABLE knowledge ADD COLUMN version INTEGER NOT NULL DEFAULT 1")


def test_chain_columns_are_populated_and_used_when_present(db_conn):
    from agent.knowledge.lifecycle import _has_chain_columns

    _add_chain_columns(db_conn)
    assert _has_chain_columns(db_conn) is True

    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "列路径：版本一")
    v2 = ks.revise_atomic(v1.id, v1.version, {"content": "列路径：版本二"}, actor="user")

    row1 = db_conn.execute(
        "SELECT chain_id, version FROM knowledge WHERE id = ?", (v1.id,)
    ).fetchone()
    row2 = db_conn.execute(
        "SELECT chain_id, version FROM knowledge WHERE id = ?", (v2.id,)
    ).fetchone()
    assert row1["chain_id"] == v1.id and row1["version"] == 1
    assert row2["chain_id"] == v1.id and row2["version"] == 2
    assert resolve_current(db_conn, v1.id).id == v2.id

    with pytest.raises(VersionConflict):
        ks.revise_atomic(v1.id, 1, {"content": "列路径：不该出现"})


def test_chain_columns_catch_siblings_created_by_raw_insert(db_conn):
    _add_chain_columns(db_conn)
    ks = KnowledgeService(db_conn)
    v1 = _active(ks, "列路径分叉：v1")
    v2 = ks.revise_atomic(v1.id, v1.version, {"content": "列路径分叉：v2a"}, actor="user")

    # 手工造一个同链的兄弟行（模拟历史脏数据：同 chain_id、同 version）
    db_conn.execute(
        "INSERT INTO knowledge (id, category, state, content, supersedes_id, chain_id, version, "
        "node_ids, created_at, updated_at, activated_at) "
        "VALUES ('kn_rm_b_sib', 'general_fact', 'verified', '列路径分叉：v2b', ?, ?, 2, '[]', ?, ?, NULL)",
        (v1.id, v1.id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    with pytest.raises(VersionConflict) as info:
        ks.activate("kn_rm_b_sib")
    assert info.value.current_id == v2.id
    assert _active_count(db_conn, "列路径分叉%") == 1
