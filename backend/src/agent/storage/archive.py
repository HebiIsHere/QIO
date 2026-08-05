"""Cold-archive scaffolding.

Design: messages older than HOT_TIER_DAYS are compressed into gzip files
under archive_dir. The SQLite row keeps metadata and points to the file
via messages.raw; content is moved out of messages.content.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOT_TIER_DAYS = 183  # ~6 months


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive_messages(
    conn: sqlite3.Connection, archive_dir: Path, older_than_days: int = HOT_TIER_DAYS
) -> int:
    """Move hot messages older than the threshold into gzip archive files.

    Returns the number of messages archived. Metadata stays in SQLite;
    content is replaced by a reference marker inside raw JSON.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    rows = conn.execute(
        "SELECT id, content, raw FROM messages WHERE storage_tier = 'hot' AND created_at < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0

    stamp = _now().replace(":", "-").replace(".", "-")
    archive_path = archive_dir / f"messages-{stamp}.jsonl.gz"
    count = 0
    with gzip.open(archive_path, "wt", encoding="utf-8") as fh:
        for row in rows:
            payload = json.loads(row["raw"] or "{}")
            payload["_archived_content"] = row["content"]
            payload["_archived_at"] = _now()
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            with conn:
                conn.execute(
                    "UPDATE messages SET storage_tier = 'cold', content = '', raw = ? "
                    "WHERE id = ?",
                    (
                        json.dumps(
                            {
                                "archive": str(archive_path),
                                "archived_at": payload["_archived_at"],
                            },
                            ensure_ascii=False,
                        ),
                        row["id"],
                    ),
                )
            count += 1
    return count


def restore_message_content(conn: sqlite3.Connection, message_id: str) -> str:
    """Restore archived content for a single message, if available."""
    row = conn.execute(
        "SELECT id, content, raw, storage_tier FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"message not found: {message_id}")
    if row["storage_tier"] != "cold":
        return row["content"]
    ref = json.loads(row["raw"] or "{}")
    archive_file = Path(ref["archive"])
    with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
        for line in fh:
            payload = json.loads(line)
            if payload.get("_archived_content") is not None:
                return payload["_archived_content"]
    return ""