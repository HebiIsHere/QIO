"""R9-H2 迁移 61 数据纠正：只纠正有明确解析失败证据的 structured/[] 旧记录。"""

from __future__ import annotations

import uuid

from agent.storage.migration_r9h2_identity import DATA_CORRECTION


def _insert_requirement(conn, *, text: str, turn_id: str, state: str = "structured", limits: str = "[]") -> str:
    rid = "req_" + uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO user_requirements"
        " (id, text, provenance, parent_id, status, limits, turn_id, topic_id,"
        "  created_at, updated_at, version, structured_limits,"
        "  structured_limits_state, structured_limits_source)"
        " VALUES (?, ?, '{}', NULL, 'active', '[]', ?, 'topic',"
        "  '2026-10-23T00:00:00+00:00', '2026-10-23T00:00:00+00:00', 1, ?, ?, 'user_message')",
        (rid, text, turn_id, limits, state),
    )
    return rid


def _insert_failure_audit(conn, *, turn_id: str, outcome: str = "unavailable") -> None:
    conn.execute(
        "INSERT INTO requirement_resolutions"
        " (id, message_id, turn_id, relation, target_requirement_id, outcome,"
        "  reason, model, prompt_version, latency_ms, error, created_at)"
        " VALUES (?, NULL, ?, 'unknown', NULL, ?, '', NULL, NULL, NULL, 'boom',"
        "  '2026-10-23T00:00:00+00:00')",
        ("reqres_" + uuid.uuid4().hex[:16], turn_id, outcome),
    )


def test_migration_corrects_only_evidenced_failure_records(db_conn):
    bad = _insert_requirement(db_conn, text="处理项目，但不要修改 B", turn_id="turn_bad")
    _insert_failure_audit(db_conn, turn_id="turn_bad")

    no_evidence = _insert_requirement(db_conn, text="正常要求", turn_id="turn_ok")

    # 有失败审计但 outcome 不是 unavailable：不能纠正。
    other_outcome = _insert_requirement(db_conn, text="另一条", turn_id="turn_applied")
    _insert_failure_audit(db_conn, turn_id="turn_applied", outcome="applied")

    # 已有非空限制：不能因为 turn 相同就清空/改写。
    non_empty = _insert_requirement(
        db_conn,
        text="有明确限制",
        turn_id="turn_bad",
        limits='[{"operations": ["write"], "objects": [], "reason": "x"}]',
    )

    db_conn.execute(DATA_CORRECTION)
    db_conn.commit()

    row = db_conn.execute(
        "SELECT structured_limits_state, structured_limits_source, structured_limits"
        " FROM user_requirements WHERE id = ?",
        (bad,),
    ).fetchone()
    assert row["structured_limits_state"] == "unresolved"
    assert row["structured_limits_source"] == "migration_61:resolution_unavailable"
    assert row["structured_limits"] == "[]"
    # 历史与审计保留。
    assert db_conn.execute(
        "SELECT COUNT(*) AS n FROM requirement_resolutions WHERE turn_id = 'turn_bad'"
    ).fetchone()["n"] == 1
    assert db_conn.execute(
        "SELECT text FROM user_requirements WHERE id = ?", (bad,)
    ).fetchone()["text"] == "处理项目，但不要修改 B"

    for rid in (no_evidence, other_outcome):
        state = db_conn.execute(
            "SELECT structured_limits_state FROM user_requirements WHERE id = ?", (rid,)
        ).fetchone()["structured_limits_state"]
        assert state == "structured", rid
    kept = db_conn.execute(
        "SELECT structured_limits_state, structured_limits FROM user_requirements WHERE id = ?",
        (non_empty,),
    ).fetchone()
    assert kept["structured_limits_state"] == "structured"
    assert kept["structured_limits"] != "[]"
