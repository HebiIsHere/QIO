"""Trace storage: one JSON-document row per turn.

Chosen to match QIO's existing SQLite style (single table + JSON columns) so
traces do not bloat the DB with full prompts/raw output. Reads tolerate
corrupted/partial JSON (never raise).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from agent.trace.model import TurnTrace
from agent.trace.redact import preview, redact_any, redact_text

_JSON_COLS = ("topic", "injection", "model_calls", "tool_runs", "writes", "warnings")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    def __init__(self, conn: sqlite3.Connection, *, enabled: bool = True) -> None:
        self.conn = conn
        self.enabled = enabled

    # -- lifecycle --------------------------------------------------------

    def begin(self, turn_id: str, *, initial_topic: str | None = None) -> None:
        if not self.enabled:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO turn_traces "
            "(turn_id, status, started_at, initial_topic, final_topic) "
            "VALUES (?, 'running', ?, ?, ?)",
            (turn_id, _now(), initial_topic, initial_topic),
        )

    def finish(
        self,
        turn_id: str,
        status: str,
        *,
        error: str | None = None,
        final_preview: str = "",
        final_topic: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        row = self.conn.execute(
            "SELECT started_at FROM turn_traces WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        started = row["started_at"] if row is not None else _now()
        duration = _duration_ms(started)
        sets = ["status = ?", "ended_at = ?", "duration_ms = ?", "error = ?", "final_preview = ?"]
        params: list[Any] = [status, _now(), duration, error, preview(final_preview, 500)]
        if final_topic is not None:
            sets.append("final_topic = ?")
            params.append(final_topic)
        params.append(turn_id)
        self.conn.execute(f"UPDATE turn_traces SET {', '.join(sets)} WHERE turn_id = ?", params)

    # -- per-section recording -------------------------------------------

    def set_topic(self, turn_id: str, topic: dict) -> None:
        self._patch(turn_id, topic=redact_any(topic))

    def set_injection(self, turn_id: str, injection: dict) -> None:
        self._patch(turn_id, injection=redact_any(injection))

    def record_model_call(self, turn_id: str, call: dict) -> None:
        self._append(turn_id, "model_calls", redact_any(call))

    def record_tool_run(self, turn_id: str, run: dict) -> None:
        self._append(turn_id, "tool_runs", redact_any(run))

    def record_write(self, turn_id: str, kind: str, value: str) -> None:
        writes = self._get_json(turn_id, "writes") or {}
        writes.setdefault(kind, [])
        writes[kind].append(redact_text(str(value)))
        self._patch(turn_id, writes=writes)

    def add_warning(self, turn_id: str, code: str, message: str) -> None:
        self._append(turn_id, "warnings", {"code": code, "message": redact_text(message)})

    # -- reads ------------------------------------------------------------

    def get(self, turn_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM turn_traces WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT turn_id, status, started_at, ended_at, duration_ms, "
            "initial_topic, final_topic, error FROM turn_traces "
            "ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (max(1, limit), max(0, offset)),
        ).fetchall()
        return [dict(r) for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM turn_traces").fetchone()[0])

    # -- internals --------------------------------------------------------

    def _patch(self, turn_id: str, **cols: Any) -> None:
        if not self.enabled or not cols:
            return
        sets = ", ".join(f"{k} = ?" for k in cols)
        params = [json.dumps(v, ensure_ascii=False) for v in cols.values()]
        params.append(turn_id)
        self.conn.execute(f"UPDATE turn_traces SET {sets} WHERE turn_id = ?", params)

    def _append(self, turn_id: str, col: str, item: dict) -> None:
        if not self.enabled:
            return
        current = self._get_json(turn_id, col) or []
        current.append(item)
        self._patch(turn_id, **{col: current})

    def _get_json(self, turn_id: str, col: str) -> Any:
        row = self.conn.execute(
            f"SELECT {col} FROM turn_traces WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[col])
        except (json.JSONDecodeError, TypeError):
            return None  # corrupted/partial → 容忍

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        for col in _JSON_COLS:
            raw = data.get(col)
            if isinstance(raw, str):
                try:
                    data[col] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[col] = None  # 兼容损坏记录
        return data


def _duration_ms(started_at: str) -> int:
    try:
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    except (ValueError, TypeError):
        return 0
