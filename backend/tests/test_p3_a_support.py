"""P3-A 定向测试共享夹具（只放 helper；用例在 test_p3_a_*.py 里）。

模型一律 fake：FakeTurnAcceptor 只记录受理参数并返回 turnId，不调任何真实模型；
不联网。迁移：先跑 apply_migrations，再应用本分支的迁移 49（migration_p3_a.apply）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from agent.interactive import board_store, models, submission
from agent.storage import migration_p3_a
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

BOARD = "board_p3a_test"


def migrated_conn(tmp_path: Path, name: str = "p3a.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    migration_p3_a.apply(conn)
    return conn


def make_board(conn: sqlite3.Connection, board_id: str = BOARD) -> str:
    return board_store.ensure_board(conn, board_id=board_id)["id"]


def add_submission(
    conn: sqlite3.Connection,
    board_id: str = BOARD,
    *,
    status: str = "succeeded",
    submission_id: str | None = None,
) -> str:
    """直接落一条提交记录（等待/绑定测试只需要记录存在，不重复走提交求差）。"""
    sid = submission_id or models.new_id("sub")
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM board_submissions WHERE board_id = ?",
        (board_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
        " visible, expressions, baseline, delivery, content_hash, created_at)"
        " VALUES (?, ?, ?, ?, '{}', '{}', '{}', '[]', '{}', '{}', '', ?)",
        (sid, board_id, int(row["n"]), status, models.now_iso()),
    )
    return sid


def save_card(conn: sqlite3.Connection, text: str = "一条注释", board_id: str = BOARD) -> dict:
    card = models.new_card("text", text, checked=True)
    state = {
        "boardId": board_id,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": [card],
        "groups": [],
        "links": [],
        "selection": [],
    }
    board_store.save_board(conn, board_id, state, reason="op")
    return card


class FakeTurnAcceptor:
    """主循环受理器替身：记录调用参数，返回可配置的结果（默认成功）。"""

    def __init__(
        self,
        *,
        ok: bool = True,
        reason: str | None = None,
        topic_id: str = "topic-original",
        include_turn_id: bool = True,
    ) -> None:
        self.calls: list[dict] = []
        self.ok = ok
        self.reason = reason
        self.topic_id = topic_id
        self.include_turn_id = include_turn_id

    def __call__(self, **kwargs: Any) -> dict:
        self.calls.append(dict(kwargs))
        if not self.ok:
            return {"ok": False, "reason": self.reason or "fake 受理失败"}
        result: dict = {"ok": True, "topicId": self.topic_id}
        if self.include_turn_id:
            result["turnId"] = f"turn_fake_{len(self.calls)}"
        return result


def submit(conn: sqlite3.Connection, acceptor=None, **kwargs):
    return asyncio.run(
        submission.submit_board(conn, BOARD, turn_acceptor=acceptor, **kwargs)
    )


def understanding_rows(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM board_understanding").fetchall()
