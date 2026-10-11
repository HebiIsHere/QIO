# -*- coding: utf-8 -*-
"""Migration 39 (submission identity) + compensation 40 verification on a real DB.

B-only, migration-only verification (no full test suite). Operates on a COPY of the real
main database (D:/QIO-data/app.db): the real database is only read; every write goes to a
temporary copy.

Four scenarios (task item 5):
  A. copy of the real main DB: upgrade to 40; new column + new index present; drafts /
     historical submissions / delivery / board state / user content unchanged; repeated
     startup idempotent (structure, version rows and content all stable).
  B. version already 39 but the new objects were deleted: object-wise check -> replay
     compensation 40 -> restored (version rows unchanged), content unchanged, the unique
     index still blocks a duplicate identity.
  C. empty database: reaches 40 in one run, column + index present, a submission carrying an
     identity can really be written, the unique index blocks a duplicate identity.
  D. half-applied DB (version 38, column missing): replaying migration 39 adds the column
     with the empty-string default for historical rows.

Run:
    cd backend
    uv run --frozen python scripts/verify_submission_identity_migration.py
    uv run --frozen python scripts/verify_submission_identity_migration.py --db <path>

Log lines are ASCII on purpose: PowerShell reads redirected output as ANSI and would mangle
CJK labels, making the evidence unreadable.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.storage.db import connect, transaction  # noqa: E402
from agent.storage.migrate import (  # noqa: E402
    apply_migrations,
    current_version,
    missing_objects,
    verify_required_objects,
)
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION  # noqa: E402

DEFAULT_DB = Path("D:/QIO-data/app.db")
COLUMN = "client_request_id"
INDEX = "idx_board_submissions_client_request"
TS = "2026-10-11T00:00:00+00:00"
failures: list = []
passes = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passes
    if ok:
        passes += 1
    else:
        failures.append(label)
    line = ("  OK   " if ok else "  FAIL ") + label
    if detail:
        line += " -- " + str(detail)[:300]
    print(line)
    return ok


def columns(conn: sqlite3.Connection, table: str) -> list:
    return [str(row[1]) for row in conn.execute("PRAGMA table_info(" + table + ")")]


def objects(conn: sqlite3.Connection, kind: str) -> set:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name IS NOT NULL", (kind,)
        )
    }


def index_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    return str(row[0]) if row is not None else ""


def count(conn: sqlite3.Connection, table: str) -> int:
    if table not in objects(conn, "table"):
        return -1
    return int(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])


CONTENT_TABLES = (
    "boards",
    "board_states",
    "board_state_snapshots",
    "board_drafts",
    "board_submissions",
    "board_pending",
    "board_intents",
    "board_understanding",
    "board_executions",
    "messages",
    "knowledge",
    "attachments",
)


def content_snapshot(conn: sqlite3.Connection) -> dict:
    snapshot = {table: count(conn, table) for table in CONTENT_TABLES}
    if "board_submissions" in objects(conn, "table"):
        snapshot["submissions_detail"] = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, status, delivery, before_state, after_state, expressions"
                " FROM board_submissions ORDER BY id LIMIT 20"
            )
        ]
    if "board_drafts" in objects(conn, "table"):
        snapshot["drafts"] = [
            tuple(row)
            for row in conn.execute(
                "SELECT board_id, drafts, updated_at FROM board_drafts ORDER BY board_id LIMIT 20"
            )
        ]
    if "boards" in objects(conn, "table"):
        snapshot["boards"] = [
            tuple(row)
            for row in conn.execute("SELECT id, title FROM boards ORDER BY id LIMIT 20")
        ]
    return snapshot


def assert_content_kept(before: dict, after: dict, where: str) -> None:
    for table in CONTENT_TABLES:
        was, now = before.get(table), after.get(table)
        if was == -1 and now == 0:
            check(
                where + ": " + table + " was absent before, empty after",
                True,
                "(table created by this migration; nothing lost)",
            )
            continue
        check(
            where + ": " + table + " row count unchanged",
            was == now,
            str(was) + " -> " + str(now),
        )
    for key in ("submissions_detail", "drafts", "boards"):
        if key not in before and key not in after:
            continue
        check(where + ": " + key + " byte-identical", before.get(key) == after.get(key))


def version_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0])


def structural_snapshot(conn: sqlite3.Connection) -> tuple:
    return (
        sorted(objects(conn, "table")),
        sorted(objects(conn, "index")),
        sorted(columns(conn, "board_submissions")),
        version_rows(conn),
    )


def copy_db(source: Path, target: Path) -> Path:
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(source) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(target) + suffix))
    return target


def insert_submission(conn, *, submission_id, board_id, seq, identity, delivery="{}", after="{}"):
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
        " visible, expressions, baseline, delivery, content_hash, error, created_at, "
        + COLUMN
        + ") VALUES (?, ?, ?, 'succeeded', '{}', ?, '{}', '[]', '{}', ?, '', NULL, ?, ?)",
        (submission_id, board_id, seq, after, delivery, TS, identity),
    )


def duplicate_identity_blocked(conn, *, submission_id, board_id, seq, identity) -> bool:
    try:
        insert_submission(
            conn, submission_id=submission_id, board_id=board_id, seq=seq, identity=identity
        )
    except sqlite3.IntegrityError:
        return True
    return False


def build_through(conn: sqlite3.Connection, target: int) -> None:
    """Build the DB up to target using the real migration definitions."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    already = ("duplicate column name", "already exists")
    for version, statements in MIGRATIONS:
        if version > target:
            continue
        with transaction(conn):
            for stmt in statements:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if not any(tok in str(exc).lower() for tok in already):
                        raise
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, TS)
            )


def seed_marker_rows(conn: sqlite3.Connection) -> list:
    """Seed recognisable marker rows into the copy, so 'nothing is lost' is really exercised.

    The real DB may be an early-version (v26) database with zero interaction rows; without
    seeded rows the 'content unchanged' assertions would be vacuously true. Every insert is
    guarded: a table/column may not exist in the old shape, and that is reported, not hidden.
    """
    marker = {
        "board": "verify-m39-board",
        "draft": '{"verify-note": "draft must survive migration 39"}',
        "submission": "verify-m39-sub",
        "delivery": '{"delivered": true, "status": "accepted", "detail": "verify marker"}',
        "message": "verify-m39-message",
        "knowledge": "verify-m39-knowledge",
    }
    seeded = []
    statements = [
        (
            "boards",
            "INSERT INTO boards (id, title, created_at, updated_at, meta)"
            " VALUES (?, 'migration 39 marker board', ?, ?, '{}')",
            (marker["board"], TS, TS),
        ),
        (
            "board_states",
            "INSERT INTO board_states (board_id, seq, state, reason, updated_at)"
            " VALUES (?, 1, '{\"seq\": 1, \"cards\": []}', 'op', ?)",
            (marker["board"], TS),
        ),
        (
            "board_state_snapshots",
            "INSERT INTO board_state_snapshots (id, board_id, seq, state, reason, created_at)"
            " VALUES ('verify-m39-snap', ?, 1, '{\"seq\": 1}', 'op', ?)",
            (marker["board"], TS),
        ),
        (
            "board_drafts",
            "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)",
            (marker["board"], marker["draft"], TS),
        ),
        (
            "board_submissions",
            "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
            " visible, expressions, baseline, delivery, content_hash, error, created_at)"
            " VALUES (?, ?, 1, 'succeeded', '{}', '{\"seq\": 1}', '{}', '[]', '{}', ?,"
            " 'verify-hash', NULL, ?)",
            (marker["submission"], marker["board"], marker["delivery"], TS),
        ),
        (
            "board_intents",
            "INSERT INTO board_intents (id, board_id, submission_id, title, summary, status,"
            " preview, impact, depends_on, conflicts_with, conflict_key, material_refs, progress,"
            " applied, revert, reason, demo, created_at, updated_at)"
            " VALUES ('verify-m39-intent', ?, ?, 'marker intent', '', 'applied', '{}', '{}',"
            " '[]', '[]', '', '[]', '{}', '{}', '{}', '', 0, ?, ?)",
            (marker["board"], marker["submission"], TS, TS),
        ),
        (
            "messages",
            "INSERT INTO messages (id, fragment_id, role, content, created_at)"
            " VALUES (?, NULL, 'user', 'message must survive migration 39', ?)",
            (marker["message"], TS),
        ),
        (
            "knowledge",
            "INSERT INTO knowledge (id, category, state, content, node_ids, created_at,"
            " updated_at) VALUES (?, 'general_fact', 'active',"
            " 'knowledge must survive migration 39', '[]', ?, ?)",
            (marker["knowledge"], TS, TS),
        ),
    ]
    for table, sql, params in statements:
        if table not in objects(conn, "table"):
            print("  seed skipped (table absent in the old shape): " + table)
            continue
        try:
            conn.execute(sql, params)
        except sqlite3.Error as exc:
            print("  seed skipped (" + table + "): " + str(exc)[:120])
            continue
        seeded.append(table)
    print("  seeded marker rows into: " + ", ".join(seeded))
    return seeded


def scenario_a_real_database(db_path: Path, workdir: Path) -> None:
    print("")
    print("[A] copy of the real main database: " + str(db_path))
    if not db_path.exists():
        check("A: real database exists", False, "not found: " + str(db_path))
        return
    conn = connect(copy_db(db_path, workdir / "app_copy.db"))
    try:
        before_version = current_version(conn)
        seed_marker_rows(conn)
        before = content_snapshot(conn)
        print("  start version = " + str(before_version))
        print(
            "  start rows: board_submissions=" + str(before["board_submissions"])
            + " board_drafts=" + str(before["board_drafts"])
            + " board_understanding=" + str(before["board_understanding"])
        )
        check(
            "A: start DB has no " + COLUMN,
            COLUMN not in columns(conn, "board_submissions"),
            "(a newer DB would still satisfy the remaining checks)",
        )
        version = apply_migrations(conn)
        check(
            "A: upgraded to compensation version " + str(COMPENSATION_VERSION),
            version == COMPENSATION_VERSION == SCHEMA_VERSION,
            str(version),
        )
        check("A: new column present", COLUMN in columns(conn, "board_submissions"))
        check("A: new index present", INDEX in objects(conn, "index"))
        sql = index_sql(conn, INDEX)
        check(
            "A: index is a partial UNIQUE index on (board_id, client_request_id)",
            ("UNIQUE" in sql.upper()) and ("board_id" in sql) and ("<>" in sql),
            sql,
        )
        check(
            "A: object-wise check reports nothing missing",
            not any(missing_objects(conn).values()),
            str(missing_objects(conn)),
        )
        assert_content_kept(before, content_snapshot(conn), "A")
        if before["board_submissions"] > 0:
            rows = conn.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN " + COLUMN + " = '' THEN 1 ELSE 0 END)"
                " AS empty FROM board_submissions"
            ).fetchone()
            check(
                "A: historical submission rows all carry the empty identity (legacy clients)",
                int(rows["n"]) == int(rows["empty"]),
                "total=" + str(rows["n"]) + " empty=" + str(rows["empty"]),
            )
        board = conn.execute("SELECT id FROM boards ORDER BY created_at LIMIT 1").fetchone()
        if board is not None:
            board_id = str(board[0])
            insert_submission(
                conn, submission_id="verify_sub_m39", board_id=board_id, seq=999000,
                identity="verify-crid",
            )
            check(
                "A: unique index blocks a duplicate identity on the real DB",
                duplicate_identity_blocked(
                    conn, submission_id="verify_sub_m39_dup", board_id=board_id,
                    seq=999001, identity="verify-crid",
                ),
            )
            conn.execute(
                "DELETE FROM board_submissions WHERE id IN ('verify_sub_m39',"
                " 'verify_sub_m39_dup')"
            )
        structure = structural_snapshot(conn)
        content = content_snapshot(conn)
        for _ in range(3):
            check("A: repeated startup returns the same version", apply_migrations(conn) == version)
        check("A: repeated startup keeps structure stable", structural_snapshot(conn) == structure)
        check("A: repeated startup keeps content stable", content_snapshot(conn) == content)
    finally:
        conn.close()


def scenario_b_lost_objects(workdir: Path) -> None:
    print("")
    print("[B] version already 39, new objects deleted: replay compensation 40")
    conn = connect(workdir / "lost.db")
    try:
        build_through(conn, 39)
        conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at, meta) VALUES"
            " ('board-b', 'board B', ?, ?, '{}')",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES"
            " ('board-b', '{\"c1\": \"draft text\"}', ?)",
            (TS,),
        )
        insert_submission(
            conn, submission_id="sub-b-1", board_id="board-b", seq=1, identity="crid-b-1",
            delivery="{\"delivered\": true}", after="{\"seq\": 1}",
        )
        conn.execute("DROP INDEX " + INDEX)
        conn.execute("ALTER TABLE board_submissions DROP COLUMN " + COLUMN)
        missing = missing_objects(conn)
        check(
            "B: missing column detected",
            ("board_submissions." + COLUMN) in missing["columns"],
            str(missing["columns"]),
        )
        check("B: missing index detected", INDEX in missing["indexes"], str(missing["indexes"]))
        check("B: version is still 39", current_version(conn) == 39, str(current_version(conn)))
        before = content_snapshot(conn)
        rows = version_rows(conn)
        print(
            "  note: SQLite DROP COLUMN discards that column data, so the old row identity"
            " is now the empty string; uniqueness is proven with a fresh write."
        )
        remaining = verify_required_objects(conn)
        check("B: nothing missing after compensation", not any(remaining.values()), str(remaining))
        check("B: column restored", COLUMN in columns(conn, "board_submissions"))
        check("B: index restored", INDEX in objects(conn, "index"))
        check(
            "B: version rows unchanged (compensation writes no version)",
            version_rows(conn) == rows,
        )
        check(
            "B: restored column defaults to the empty string",
            conn.execute(
                "SELECT " + COLUMN + " FROM board_submissions WHERE id='sub-b-1'"
            ).fetchone()[0] == "",
        )
        assert_content_kept(before, content_snapshot(conn), "B")
        insert_submission(conn, submission_id="sub-b-2", board_id="board-b", seq=2, identity="")
        insert_submission(
            conn, submission_id="sub-b-3", board_id="board-b", seq=3, identity="crid-b-new"
        )
        check(
            "B: empty identities still may repeat (legacy clients)",
            count(conn, "board_submissions") == 3,
            str(count(conn, "board_submissions")),
        )
        check(
            "B: restored unique index blocks a duplicate identity",
            duplicate_identity_blocked(
                conn, submission_id="sub-b-4", board_id="board-b", seq=4,
                identity="crid-b-new",
            ),
        )
    finally:
        conn.close()


def scenario_c_fresh(workdir: Path) -> None:
    print("")
    print("[C] empty database: reaches 40 in one run")
    conn = connect(workdir / "fresh.db")
    try:
        version = apply_migrations(conn)
        check(
            "C: reaches " + str(COMPENSATION_VERSION) + " in one run",
            version == COMPENSATION_VERSION and current_version(conn) == version,
            str(version),
        )
        check("C: column present", COLUMN in columns(conn, "board_submissions"))
        check("C: index present", INDEX in objects(conn, "index"))
        check(
            "C: object-wise check reports nothing missing",
            not any(missing_objects(conn).values()),
        )
        conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at, meta) VALUES"
            " ('board-c', 'board C', ?, ?, '{}')",
            (TS, TS),
        )
        insert_submission(
            conn, submission_id="sub-c-1", board_id="board-c", seq=1, identity="crid-c-1"
        )
        check(
            "C: unique index blocks a duplicate identity",
            duplicate_identity_blocked(
                conn, submission_id="sub-c-2", board_id="board-c", seq=2, identity="crid-c-1"
            ),
        )
        check(
            "C: legacy empty identities may repeat",
            not duplicate_identity_blocked(
                conn, submission_id="sub-c-3", board_id="board-c", seq=3, identity=""
            ),
        )
    finally:
        conn.close()


def scenario_d_half_applied(workdir: Path) -> None:
    print("")
    print("[D] half-applied DB (version 38, column missing): replay migration 39")
    conn = connect(workdir / "half.db")
    try:
        build_through(conn, 38)
        check("D: start version is 38", current_version(conn) == 38, str(current_version(conn)))
        conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at, meta) VALUES"
            " ('board-d', 'board D', ?, ?, '{}')",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
            " visible, expressions, baseline, delivery, content_hash, error, created_at)"
            " VALUES ('sub-d-1', 'board-d', 1, 'succeeded', '{}', '{}', '{}', '[]', '{}', '{}',"
            " 'hash-d', NULL, ?)",
            (TS,),
        )
        version = apply_migrations(conn)
        check(
            "D: upgraded to " + str(COMPENSATION_VERSION),
            version == COMPENSATION_VERSION,
            str(version),
        )
        check("D: column added", COLUMN in columns(conn, "board_submissions"))
        check(
            "D: historical row carries the empty-string default",
            conn.execute(
                "SELECT " + COLUMN + " FROM board_submissions WHERE id='sub-d-1'"
            ).fetchone()[0] == "",
        )
        check("D: historical submission still there", count(conn, "board_submissions") == 1)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="migration 39 / compensation 40 verification")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="real main DB path (read-only, copied)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary copy directory")
    args = parser.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="qio-m39-verify-"))
    print("temp workdir: " + str(workdir))
    print("real DB: " + args.db + " (read-only; every write goes to the copy)")
    try:
        scenario_a_real_database(Path(args.db), workdir)
        scenario_b_lost_objects(workdir)
        scenario_c_fresh(workdir)
        scenario_d_half_applied(workdir)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    print("")
    print("===== RESULT =====")
    print("passed = " + str(passes) + "  failed = " + str(len(failures)))
    if failures:
        for item in failures:
            print("  - " + item)
        return 1
    print(
        "ALL CHECKS PASSED (real main DB copy upgrade, lost-object compensation, empty DB,"
        " half-applied DB, repeated-startup idempotence)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
