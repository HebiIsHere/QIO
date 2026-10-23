"""R9-H3 迁移 63：只撤销 targeted 例外产生的越权授权记录。"""

from __future__ import annotations

import uuid

from agent.storage.migration_r9h3_review_records import REVOKE_DATA
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION


def _seed_record(conn, *, mode: str, source: str, revoked_at=None) -> str:
    rid = "authrec_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO authorization_records"
        " (id, requirement_id, requirement_version, operation_kind, fingerprint,"
        "  decision, reason, allowed_operations, objects, limits, source, mode,"
        "  created_at, expires_at, revoked_at, review_id)"
        " VALUES (?, 'req_test', 1, 'write', 'fp', 'allow', 'test', '[]', '[]', '[]',"
        "  ?, ?, '2026-10-23T00:00:00+00:00', NULL, ?, 'review_test')",
        (rid, source, mode, revoked_at),
    )
    return rid


def test_revoke_only_targeted_unrestricted_review_records(db_conn):
    targeted = _seed_record(db_conn, mode="unrestricted", source="model_review")
    restricted = _seed_record(db_conn, mode="restricted", source="model_review")
    other_source = _seed_record(db_conn, mode="unrestricted", source="manual")
    already = _seed_record(
        db_conn,
        mode="unrestricted",
        source="model_review",
        revoked_at="2026-01-01T00:00:00+00:00",
    )
    audit_before = db_conn.execute(
        "SELECT COUNT(*) AS n FROM authorization_review_audit"
    ).fetchone()["n"]

    db_conn.execute(REVOKE_DATA)
    db_conn.commit()

    def revoked(rid: str):
        return db_conn.execute(
            "SELECT revoked_at FROM authorization_records WHERE id = ?", (rid,)
        ).fetchone()["revoked_at"]

    assert revoked(targeted)
    assert revoked(restricted) is None
    assert revoked(other_source) is None
    assert revoked(already) == "2026-01-01T00:00:00+00:00"
    # 行与审计保留，不删除历史。
    assert db_conn.execute(
        "SELECT COUNT(*) AS n FROM authorization_records"
    ).fetchone()["n"] == 4
    assert db_conn.execute(
        "SELECT COUNT(*) AS n FROM authorization_review_audit"
    ).fetchone()["n"] == audit_before


def test_versions_and_compensation_are_additive_only():
    # R9-H4 迁移 65/66 在其后追加；本用例核对 63/64 仍在最终补偿链中。
    assert SCHEMA_VERSION == COMPENSATION_VERSION >= 64
    targets = {target for target, _ in MIGRATIONS}
    assert 63 in targets and 64 in targets and 65 in targets and 66 in targets
    statements = [
        str(stmt)
        for target, stmts in MIGRATIONS
        if target == COMPENSATION_VERSION
        for stmt in stmts
    ]
    assert statements, "最终补偿必须包含追加 DDL 才能补齐对象"
    for stmt in statements:
        head = " ".join(str(stmt).strip().split()).upper()
        assert not head.startswith(("UPDATE ", "DELETE ", "DROP ", "TRUNCATE")), stmt[:80]
