"""L01：冷归档按消息身份还原（Lead 负责项的长期回归用例）。

只依据可观察行为断言：文件里的记录、SQLite 行的 storage_tier/content/raw、
restore_message_content 的返回值或异常。不依赖任何新增模块名，因此在基线
（6e073e9）与修复后都能正常收集；基线应红、修复后应绿。

反例（基线）：同一次归档写入两条不同正文的消息后，restore_message_content
对任意一条都返回归档文件的第一条 ``_archived_content``。
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent.storage.archive as archive_mod
from agent.storage.archive import archive_messages, restore_message_content

# 基线没有这个异常类型；用 RuntimeError 兜底，保证用例在基线上也能运行出「未抛出」的红。
UNRELIABLE = getattr(archive_mod, "ArchiveUnavailableError", RuntimeError)


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _seed(
    db_conn: sqlite3.Connection,
    message_id: str,
    content: str,
    *,
    days_ago: int = 300,
    raw: str = "{}",
) -> None:
    now = _iso(0)
    db_conn.execute("INSERT OR IGNORE INTO nodes VALUES ('t1','topic','t','{}',?,?)", (now, now))
    db_conn.execute(
        "INSERT OR IGNORE INTO fragments (id, topic_id, created_at) VALUES ('f1','t1',?)", (now,)
    )
    db_conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, raw, created_at, storage_tier) "
        "VALUES (?, 'f1', 'user', ?, ?, ?, 'hot')",
        (message_id, content, raw, _iso(days_ago)),
    )


def _archive_path_of(db_conn: sqlite3.Connection, message_id: str) -> Path:
    row = db_conn.execute("SELECT raw FROM messages WHERE id = ?", (message_id,)).fetchone()
    return Path(json.loads(row["raw"])["archive"])


def _make_cold_row(
    db_conn: sqlite3.Connection,
    message_id: str,
    archive_file: Path,
    *,
    archived_at: str = "2026-01-01T00:00:00+00:00",
) -> None:
    _seed(db_conn, message_id, "")
    db_conn.execute(
        "UPDATE messages SET storage_tier = 'cold', content = '', raw = ? WHERE id = ?",
        (json.dumps({"archive": str(archive_file), "archived_at": archived_at}), message_id),
    )
    db_conn.commit()


def _write_gz(path: Path, payloads: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for payload in payloads:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


# --- 主反例：同一批归档必须逐条还原 -----------------------------------------


def test_batch_restore_returns_each_message_own_content(db_conn, tmp_path):
    _seed(db_conn, "m_a", "content A", days_ago=300)
    _seed(db_conn, "m_b", "content B", days_ago=299)

    archived = archive_messages(db_conn, tmp_path / "archive")

    assert archived == 2
    assert restore_message_content(db_conn, "m_a") == "content A"
    assert restore_message_content(db_conn, "m_b") == "content B"


def test_archive_records_carry_stable_message_identity(db_conn, tmp_path):
    _seed(db_conn, "m_a", "content A", days_ago=300)
    _seed(db_conn, "m_b", "content B", days_ago=299)

    archive_messages(db_conn, tmp_path / "archive")
    archive_file = _archive_path_of(db_conn, "m_a")

    with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
        recorded = [json.loads(line) for line in fh if line.strip()]

    identities = {payload.get("_archived_message_id") for payload in recorded}
    assert identities == {"m_a", "m_b"}
    contents = {
        payload.get("_archived_message_id"): payload.get("_archived_content")
        for payload in recorded
    }
    assert contents == {"m_a": "content A", "m_b": "content B"}


def test_restore_uses_archived_message_identity_not_position(db_conn, tmp_path):
    """把目标消息放在归档文件末尾：按位置取第一条的实现会在这里露馅。"""
    _seed(db_conn, "m_first", "first body", days_ago=302)
    _seed(db_conn, "m_second", "second body", days_ago=301)
    _seed(db_conn, "m_target", "target body", days_ago=300)

    archive_messages(db_conn, tmp_path / "archive")

    assert restore_message_content(db_conn, "m_target") == "target body"
    assert restore_message_content(db_conn, "m_first") == "first body"
    assert restore_message_content(db_conn, "m_second") == "second body"


# --- 边界：空正文、目标缺失、旧格式、损坏、文件缺失 -------------------------


def test_empty_content_is_restored_as_empty_not_as_other_message(db_conn, tmp_path):
    _seed(db_conn, "m_empty", "", days_ago=300)
    _seed(db_conn, "m_other", "other body", days_ago=299)

    archive_messages(db_conn, tmp_path / "archive")

    assert restore_message_content(db_conn, "m_empty") == ""
    assert restore_message_content(db_conn, "m_other") == "other body"


def test_target_absent_from_archive_is_reported_unreliable(db_conn, tmp_path):
    archive_file = tmp_path / "archive" / "messages-legacy.jsonl.gz"
    _write_gz(
        archive_file,
        [
            {
                "_archived_message_id": "someone_else",
                "_archived_content": "not my body",
                "_archived_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    )
    _make_cold_row(db_conn, "m_target", archive_file)

    with pytest.raises(UNRELIABLE):
        restore_message_content(db_conn, "m_target")


def test_legacy_format_with_unique_timestamp_still_restores(db_conn, tmp_path):
    """旧格式（无身份字段）但时间戳唯一时，仍可精确定位目标。"""
    archive_file = tmp_path / "archive" / "messages-old.jsonl.gz"
    stamp = "2026-01-02T03:04:05.000006+00:00"
    _write_gz(
        archive_file,
        [
            {"_archived_content": "other legacy", "_archived_at": "2026-01-02T03:04:04+00:00"},
            {"_archived_content": "legacy body", "_archived_at": stamp},
        ],
    )
    _make_cold_row(db_conn, "m_legacy", archive_file, archived_at=stamp)

    assert restore_message_content(db_conn, "m_legacy") == "legacy body"


def test_legacy_format_with_ambiguous_timestamp_is_reported_unreliable(db_conn, tmp_path):
    archive_file = tmp_path / "archive" / "messages-old.jsonl.gz"
    stamp = "2026-01-02T03:04:05+00:00"
    _write_gz(
        archive_file,
        [
            {"_archived_content": "one", "_archived_at": stamp},
            {"_archived_content": "two", "_archived_at": stamp},
        ],
    )
    _make_cold_row(db_conn, "m_legacy", archive_file, archived_at=stamp)

    with pytest.raises(UNRELIABLE):
        restore_message_content(db_conn, "m_legacy")


def test_corrupt_archive_is_reported_unreliable(db_conn, tmp_path):
    archive_file = tmp_path / "archive" / "messages-broken.jsonl.gz"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_bytes(b"this is not gzip at all")
    _make_cold_row(db_conn, "m_broken", archive_file)

    with pytest.raises(UNRELIABLE):
        restore_message_content(db_conn, "m_broken")


def test_missing_archive_file_is_reported_unreliable(db_conn, tmp_path):
    _seed(db_conn, "m_gone", "gone body", days_ago=300)
    archive_messages(db_conn, tmp_path / "archive")
    _archive_path_of(db_conn, "m_gone").unlink()

    with pytest.raises(UNRELIABLE):
        restore_message_content(db_conn, "m_gone")


def test_cold_row_without_archive_reference_is_reported_unreliable(db_conn, tmp_path):
    _seed(db_conn, "m_noref", "")
    db_conn.execute(
        "UPDATE messages SET storage_tier = 'cold', content = '', raw = '{}' WHERE id = ?",
        ("m_noref",),
    )
    db_conn.commit()

    with pytest.raises(UNRELIABLE):
        restore_message_content(db_conn, "m_noref")


def test_hot_content_still_returned_unchanged(db_conn, tmp_path):
    _seed(db_conn, "m_hot", "hot body", days_ago=1)

    assert restore_message_content(db_conn, "m_hot") == "hot body"


def test_unknown_message_id_still_raises_keyerror(db_conn):
    with pytest.raises(KeyError):
        restore_message_content(db_conn, "m_does_not_exist")


# --- 写失败：不能留下「假成功」或不完整归档 --------------------------------


def test_archive_write_failure_marks_nothing_cold_and_leaves_no_pointer(db_conn, tmp_path):
    _seed(db_conn, "m_a", "A body", days_ago=300)
    _seed(db_conn, "m_b", "B body", days_ago=299)
    db_conn.execute(
        "CREATE TRIGGER rm_l01_block BEFORE UPDATE ON messages "
        "WHEN NEW.storage_tier = 'cold' BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )

    with pytest.raises(sqlite3.Error):
        archive_messages(db_conn, tmp_path / "archive")

    rows = {
        row["id"]: (row["storage_tier"], row["content"])
        for row in db_conn.execute("SELECT id, storage_tier, content FROM messages")
    }
    assert rows == {"m_a": ("hot", "A body"), "m_b": ("hot", "B body")}
    leftovers = sorted(p.name for p in (tmp_path / "archive").glob("messages-*"))
    assert leftovers == []


def test_successful_archive_leaves_no_partial_file(db_conn, tmp_path):
    _seed(db_conn, "m_a", "A body", days_ago=300)
    _seed(db_conn, "m_b", "B body", days_ago=299)

    archive_messages(db_conn, tmp_path / "archive")

    names = sorted(p.name for p in (tmp_path / "archive").iterdir())
    assert len(names) == 1
    assert names[0].endswith(".jsonl.gz")
    assert not any(name.endswith(".part") for name in names)
    # 归档文件是完整可读的：两条记录都在
    with gzip.open(tmp_path / "archive" / names[0], "rt", encoding="utf-8") as fh:
        assert sum(1 for line in fh if line.strip()) == 2
