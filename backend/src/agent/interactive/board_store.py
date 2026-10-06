"""板面与草稿的持久化（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §1.5 / §3。

占位实现只是「能读能写」的降级版本：没有快照、没有未提交改动、没有草稿语义。
B 必须替换为完整实现：

- 每次保存写最新状态 + 追加一条快照（撤销 / 重做与撤回保护的依据）；
- 保存**绝不调用 QIO**（这里不做任何模型 / 网络调用，也不允许后续加）；
- `save_board` 必须过 `board.normalize_state`；
- 草稿是未提交的文字输入，与提交内容分开。
"""

from __future__ import annotations

import sqlite3

from agent.interactive import models


def _now() -> str:
    return models.now_iso()


def ensure_board(
    conn: sqlite3.Connection, *, board_id: str | None = None, title: str | None = None
) -> dict:
    board_id = board_id or models.DEFAULT_BOARD_ID
    row = conn.execute("SELECT id, title FROM boards WHERE id = ?", (board_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO boards (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (board_id, title or models.DEFAULT_BOARD_TITLE, _now(), _now()),
        )
        conn.execute(
            "INSERT INTO board_states (board_id, seq, state, reason, updated_at)"
            " VALUES (?, 0, ?, 'created', ?)",
            (board_id, models.dumps(models.empty_state(board_id)), _now()),
        )
        row = conn.execute("SELECT id, title FROM boards WHERE id = ?", (board_id,)).fetchone()
    return {"id": row["id"], "title": row["title"]}


def load_board(conn: sqlite3.Connection, board_id: str) -> dict:
    board = ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT seq, state FROM board_states WHERE board_id = ?", (board_id,)
    ).fetchone()
    state = models.loads(row["state"] if row else None, models.empty_state(board_id))
    state["boardId"] = board_id
    return {
        "board": board,
        "state": state,
        "seq": int(row["seq"] if row else 0),
    }


def save_board(conn: sqlite3.Connection, board_id: str, state: dict, *, reason: str = "op") -> dict:
    ensure_board(conn, board_id=board_id)
    payload = dict(state)
    payload["boardId"] = board_id
    row = conn.execute(
        "SELECT seq FROM board_states WHERE board_id = ?", (board_id,)
    ).fetchone()
    seq = int(row["seq"] if row else 0) + 1
    payload["seq"] = seq
    payload["updatedAt"] = _now()
    conn.execute(
        "UPDATE board_states SET seq = ?, state = ?, reason = ?, updated_at = ? WHERE board_id = ?",
        (seq, models.dumps(payload), reason, _now(), board_id),
    )
    return {"seq": seq, "savedAt": payload["updatedAt"], "state": payload}


def list_board_states(conn: sqlite3.Connection, board_id: str, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT seq, state, reason, created_at FROM board_state_snapshots"
        " WHERE board_id = ? ORDER BY seq DESC LIMIT ?",
        (board_id, limit),
    ).fetchall()
    return [
        {
            "seq": int(row["seq"]),
            "state": models.loads(row["state"], {}),
            "reason": row["reason"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def get_draft(conn: sqlite3.Connection, board_id: str) -> dict:
    ensure_board(conn, board_id=board_id)
    row = conn.execute(
        "SELECT drafts, updated_at FROM board_drafts WHERE board_id = ?", (board_id,)
    ).fetchone()
    return {
        "drafts": models.loads(row["drafts"] if row else None, {}),
        "updatedAt": row["updated_at"] if row else None,
    }


def save_draft(conn: sqlite3.Connection, board_id: str, drafts: dict) -> dict:
    ensure_board(conn, board_id=board_id)
    stamp = _now()
    conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(board_id) DO UPDATE SET drafts = excluded.drafts, updated_at = excluded.updated_at",
        (board_id, models.dumps(drafts or {}), stamp),
    )
    return {"drafts": drafts or {}, "updatedAt": stamp}
