"""Cold-archive scaffolding.

Design: messages older than HOT_TIER_DAYS are compressed into gzip files
under archive_dir. The SQLite row keeps metadata and points to the file
via messages.raw; content is moved out of messages.content.

L01：归档必须按**消息身份**精确还原。
- 每条归档记录都带 `_archived_message_id`；`restore_message_content` 只返回
  身份匹配的那一条正文，找不到就报错，绝不退化成「返回文件里的第一条」。
- 旧格式（没有身份字段）只有在归档文件里该写入时刻唯一时才接受，否则明确
  返回「无法可靠还原」。
- 写入顺序：先把**完整**归档写到 `*.part`，原子改名为正式文件，然后在一个
  事务里批量更新 DB 引用。任一步失败都会清掉刚写的文件并整体回滚，不会留下
  「DB 说已归档、归档里却没有」的假成功记录。

注意：冷归档当前没有被接入主要运行流程（保持既有启用状态，不在本次改动里接线）。
"""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.storage.db import transaction

HOT_TIER_DAYS = 183  # ~6 months

# 归档记录里的保留字段（下划线前缀，避免与消息 raw 里的业务字段冲突）。
ARCHIVED_MESSAGE_ID = "_archived_message_id"
ARCHIVED_CONTENT = "_archived_content"
ARCHIVED_AT = "_archived_at"


class ArchiveUnavailableError(RuntimeError):
    """归档存在，但无法可靠定位/解出目标消息的正文。

    出现于：归档文件缺失、损坏、旧格式无法确定目标、或文件里根本没有该消息。
    调用方应把它当作「不可靠还原」反馈，而不是拿到一个可能是别人的正文。
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def archive_messages(
    conn: sqlite3.Connection, archive_dir: Path, older_than_days: int = HOT_TIER_DAYS
) -> int:
    """Move hot messages older than the threshold into gzip archive files.

    Returns the number of messages actually marked cold. Metadata stays in
    SQLite; content is replaced by a reference marker inside raw JSON.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    rows = conn.execute(
        "SELECT id, content, raw FROM messages "
        "WHERE storage_tier = 'hot' AND created_at < ? "
        "ORDER BY created_at, id",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0

    stamp = _now().replace(":", "-").replace(".", "-")
    archive_path = archive_dir / f"messages-{stamp}.jsonl.gz"
    staging_path = archive_path.with_name(archive_path.name + ".part")

    # (message_id, content, original_raw, archived_at) —— archived_at 逐条生成，
    # 作为旧格式归档的弱身份（且要求文件内唯一）。
    records = [
        (row["id"], row["content"] or "", row["raw"] or "{}", _now()) for row in rows
    ]

    # 阶段 1：把完整归档写进暂存文件并原子发布。中途失败不留半个正式文件。
    try:
        with gzip.open(staging_path, "wt", encoding="utf-8") as fh:
            for message_id, content, original_raw, archived_at in records:
                try:
                    payload = json.loads(original_raw)
                except ValueError:
                    payload = {"_archived_raw": original_raw}
                if not isinstance(payload, dict):
                    payload = {"_archived_raw": payload}
                payload[ARCHIVED_MESSAGE_ID] = message_id
                payload[ARCHIVED_CONTENT] = content
                payload[ARCHIVED_AT] = archived_at
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staging_path, archive_path)
    except BaseException:
        staging_path.unlink(missing_ok=True)
        raise

    # 阶段 2：一个事务里批量登记引用。失败则回滚并删掉刚发布的归档，
    # 保证不会出现「DB 指向不完整/不存在的归档」。
    try:
        with transaction(conn):
            archived = 0
            for message_id, _content, original_raw, archived_at in records:
                cur = conn.execute(
                    "UPDATE messages SET storage_tier = 'cold', content = '', raw = ? "
                    "WHERE id = ? AND storage_tier = 'hot'",
                    (
                        json.dumps(
                            {
                                "archive": str(archive_path),
                                "archived_at": archived_at,
                                # 原始 raw 一起留档：归档不应该吞掉消息元数据
                                "raw": original_raw,
                            },
                            ensure_ascii=False,
                        ),
                        message_id,
                    ),
                )
                archived += max(cur.rowcount, 0)
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise

    if archived == 0:
        # 没有一行真正落库（并发下已被别的流程处理）：别留无引用的归档文件。
        archive_path.unlink(missing_ok=True)
    return archived


def restore_message_content(conn: sqlite3.Connection, message_id: str) -> str:
    """Restore archived content for a single message, matched by identity.

    Raises:
        KeyError: 消息行不存在。
        ArchiveUnavailableError: 消息标记为已归档，但归档缺失/损坏/旧格式无法
            确定目标/文件里没有该消息——即「不可靠还原」，不会拿第一条顶替。
    """
    row = conn.execute(
        "SELECT id, content, raw, storage_tier FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"message not found: {message_id}")
    if row["storage_tier"] != "cold":
        return row["content"]

    ref = _load_reference(row["raw"], message_id)
    return _read_archived_content(Path(ref["archive"]), message_id, ref.get("archived_at"))


def _load_reference(raw: str | None, message_id: str) -> dict:
    try:
        ref = json.loads(raw or "{}")
    except ValueError as exc:
        raise ArchiveUnavailableError(
            f"消息 {message_id} 的归档引用不是合法 JSON，无法可靠还原"
        ) from exc
    if not isinstance(ref, dict) or not ref.get("archive"):
        raise ArchiveUnavailableError(
            f"消息 {message_id} 标记为已归档，但没有可用的归档引用，无法可靠还原"
        )
    return ref


def _read_archived_content(
    archive_file: Path, message_id: str, archived_at: str | None
) -> str:
    if not archive_file.exists():
        raise ArchiveUnavailableError(
            f"归档文件不存在：{archive_file}（消息 {message_id} 无法可靠还原）"
        )

    identity_hits: list[str] = []
    legacy_hits: list[str] = []
    saw_identity_field = False
    try:
        with gzip.open(archive_file, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    raise ArchiveUnavailableError(
                        f"归档文件损坏（JSON 解析失败）：{archive_file}"
                    ) from None
                if not isinstance(payload, dict):
                    continue
                if ARCHIVED_MESSAGE_ID in payload:
                    # 新格式：整份归档都带身份，缺失即视为「找不到」，不做弱匹配。
                    saw_identity_field = True
                    if payload.get(ARCHIVED_MESSAGE_ID) == message_id:
                        identity_hits.append(_content_of(payload, archive_file))
                    continue
                if archived_at is not None and payload.get(ARCHIVED_AT) == archived_at:
                    legacy_hits.append(_content_of(payload, archive_file))
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise ArchiveUnavailableError(
            f"归档文件无法读取（{archive_file}）：{exc}"
        ) from exc

    if len(identity_hits) == 1:
        return identity_hits[0]
    if len(identity_hits) > 1:
        raise ArchiveUnavailableError(
            f"归档文件里有多条记录属于消息 {message_id}，无法确定唯一正文：{archive_file}"
        )
    # 旧格式（没有身份字段）只有在写入时刻唯一时才可用。
    if not saw_identity_field:
        if len(legacy_hits) == 1:
            return legacy_hits[0]
        if len(legacy_hits) > 1:
            raise ArchiveUnavailableError(
                f"旧格式归档里的写入时刻不唯一（{archived_at}），无法确定消息 "
                f"{message_id} 的正文：{archive_file}"
            )
    raise ArchiveUnavailableError(
        f"归档文件里找不到消息 {message_id} 的正文：{archive_file}"
    )


def _content_of(payload: dict, archive_file: Path) -> str:
    if ARCHIVED_CONTENT not in payload:
        raise ArchiveUnavailableError(f"归档记录缺少正文：{archive_file}")
    value = payload[ARCHIVED_CONTENT]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ArchiveUnavailableError(f"归档记录的正文不是文本：{archive_file}")
    return value
