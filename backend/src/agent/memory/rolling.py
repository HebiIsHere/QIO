"""滚动摘要：已有摘要 + 真正新增的消息 → 更新后的摘要。

Phase 1 起：
* 只处理**未覆盖的新消息**（按消息写入顺序）；覆盖位置（最后一条已覆盖消息 id）
  与输入版本持久化在 fragments（迁移 44 的列；迁移尚未落地的库回退到
  fragments.meta 同名键，与派生任务队列的兼容口径一致），重启后可恢复；
* 输入过大时按「完整消息 + 可追溯分段」分块顺序折叠，不得只保留尾部；
* 提供读取接口（滚动摘要 + 未覆盖原文消息），供上下文组装使用。
* 覆盖位置只前进；写入摘要与推进游标由调用方放在同一个事务里。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from agent import prompts
from agent.adapters.base import BaseAdapter
from agent.memory.contract import (
    FragmentSummary,
    rolling_summary_task_system,
    validate_summary,
)
from agent.memory.limits import (
    MAX_SUMMARY,
    MAX_TITLE,
    ROLLING_INPUT_MAX_CHARS,
    ROLLING_INPUT_MAX_TOKENS,
)
from agent.memory.model_output import Outcome

logger = logging.getLogger(__name__)

# 迁移 44 在 fragments 上追加的列（与 meta 兜底键同名）
COVERED_MESSAGE_ID = "rolling_covered_message_id"
COVERED_COUNT = "rolling_covered_count"
INPUT_VERSION = "rolling_input_version"
UPDATED_AT = "rolling_updated_at"
ROLLING_STATE_KEYS = (COVERED_MESSAGE_ID, COVERED_COUNT, INPUT_VERSION, UPDATED_AT)


def _safe_exc(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class RollingState:
    """持久化的滚动摘要状态（摘要正文本身仍存在 fragments.summary）。"""

    summary: str | None
    covered_message_id: str | None
    covered_count: int
    input_version: str | None
    updated_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "covered_message_id": self.covered_message_id,
            "covered_count": int(self.covered_count),
            "input_version": self.input_version,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RollingView:
    """上下文组装需要的读取结果：滚动摘要 + 未覆盖原文消息。"""

    summary: str | None
    covered_message_id: str | None
    covered_count: int
    input_version: str | None
    updated_at: str | None
    uncovered: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# 列 / meta 兼容读取（迁移 44 未登记时降级，不假装有列）
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except sqlite3.Error:
        return set()


def has_rolling_columns(conn: sqlite3.Connection) -> bool:
    """fragments 是否已有迁移 44 的覆盖位置列。"""
    return set(ROLLING_STATE_KEYS) <= _table_columns(conn, "fragments")


def _fragment_id(fragment: Any) -> str:
    if isinstance(fragment, str):
        return fragment
    return str(fragment.id)


def _meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _row(conn: sqlite3.Connection, fragment_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM fragments WHERE id = ?", (fragment_id,)).fetchone()


def load_state(conn: sqlite3.Connection, fragment: Any) -> RollingState:
    """读取滚动摘要的覆盖位置与输入版本；列不存在时回退到 fragments.meta。"""
    fragment_id = _fragment_id(fragment)
    row = _row(conn, fragment_id)
    if row is None:
        return RollingState(None, None, 0, None, None)
    meta = _meta(row["meta"])
    columns = set(row.keys())
    use_columns = COVERED_MESSAGE_ID in columns

    def value(key: str) -> Any:
        if use_columns:
            return row[key]
        return meta.get(key)

    covered_raw = value(COVERED_MESSAGE_ID)
    covered_message_id = str(covered_raw) if covered_raw else None
    try:
        covered_count = int(value(COVERED_COUNT) or 0)
    except (TypeError, ValueError):
        covered_count = 0
    input_version_raw = value(INPUT_VERSION)
    updated_raw = value(UPDATED_AT)
    return RollingState(
        summary=row["summary"],
        covered_message_id=covered_message_id,
        covered_count=max(0, covered_count),
        input_version=str(input_version_raw) if input_version_raw else None,
        updated_at=str(updated_raw) if updated_raw else None,
    )


def _cursor_rowid(conn: sqlite3.Connection, fragment_id: str, message_id: str | None) -> int | None:
    if not message_id:
        return None
    row = conn.execute(
        "SELECT rowid AS rid FROM messages WHERE id = ? AND fragment_id = ?",
        (message_id, fragment_id),
    ).fetchone()
    return int(row["rid"]) if row is not None else None


def covered_message_ids(
    conn: sqlite3.Connection, fragment_id: str, covered_message_id: str | None
) -> list[str]:
    """覆盖游标之前的全部消息 id（按写入顺序）—— 输入版本与覆盖计数的唯一口径。"""
    rowid = _cursor_rowid(conn, fragment_id, covered_message_id)
    if rowid is None:
        return []
    rows = conn.execute(
        "SELECT id FROM messages WHERE fragment_id = ? AND rowid <= ? ORDER BY rowid",
        (fragment_id, rowid),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def compute_input_version(fragment_id: str, message_ids: Sequence[str]) -> str:
    """输入的稳定版本：覆盖到的消息 id 序列哈希（重启 / 重跑可比对）。"""
    digest = hashlib.sha256()
    digest.update(str(fragment_id).encode("utf-8"))
    for message_id in message_ids:
        digest.update(b"|")
        digest.update(str(message_id).encode("utf-8"))
    return digest.hexdigest()[:16]


def uncovered_messages(
    conn: sqlite3.Connection,
    fragment: Any,
    *,
    exclude_message_id: str | None = None,
) -> list[dict[str, Any]]:
    """滚动摘要尚未覆盖的原文消息（按写入顺序，含 id / role / content）。"""
    fragment_id = _fragment_id(fragment)
    state = load_state(conn, fragment_id)
    rowid = _cursor_rowid(conn, fragment_id, state.covered_message_id)
    if rowid is None:
        rows = conn.execute(
            "SELECT * FROM messages WHERE fragment_id = ? ORDER BY rowid",
            (fragment_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages WHERE fragment_id = ? AND rowid > ? ORDER BY rowid",
            (fragment_id, rowid),
        ).fetchall()
    messages = [dict(row) for row in rows]
    if exclude_message_id is not None:
        messages = [m for m in messages if m.get("id") != exclude_message_id]
    return messages


def rolling_view(
    conn: sqlite3.Connection,
    fragment: Any,
    *,
    exclude_message_id: str | None = None,
) -> RollingView:
    """供上下文组装：滚动摘要 + 未覆盖原文消息。"""
    state = load_state(conn, fragment)
    return RollingView(
        summary=state.summary,
        covered_message_id=state.covered_message_id,
        covered_count=state.covered_count,
        input_version=state.input_version,
        updated_at=state.updated_at,
        uncovered=tuple(
            uncovered_messages(
                conn, fragment, exclude_message_id=exclude_message_id
            )
        ),
    )


def save_state(
    conn: sqlite3.Connection,
    fragment: Any,
    *,
    summary: str,
    summary_model: str | None,
    meta: dict[str, Any],
    covered_message_id: str,
    updated_at: str,
) -> bool:
    """把滚动摘要 + 推进后的覆盖位置写进片段（调用方负责事务边界）。

    覆盖位置**只前进**：新的覆盖计数不大于已持久化的计数时不写任何行、返回 False。
    有迁移 44 的列就写列；没有就写 fragments.meta 的同名键（诚实降级）。
    """
    fragment_id = _fragment_id(fragment)
    current = load_state(conn, fragment_id)
    ids = covered_message_ids(conn, fragment_id, covered_message_id)
    if not ids:
        return False
    covered_count = len(ids)
    if covered_count <= current.covered_count:
        return False
    version = compute_input_version(fragment_id, ids)
    db_meta = dict(meta)
    if has_rolling_columns(conn):
        cursor = conn.execute(
            "UPDATE fragments SET summary = ?, summary_model = ?, "
            "summary_version = summary_version + 1, meta = ?, "
            "rolling_covered_message_id = ?, rolling_covered_count = ?, "
            "rolling_input_version = ?, rolling_updated_at = ? WHERE id = ?",
            (
                summary,
                summary_model,
                json.dumps(db_meta, ensure_ascii=False),
                covered_message_id,
                covered_count,
                version,
                updated_at,
                fragment_id,
            ),
        )
    else:
        db_meta.update(
            {
                COVERED_MESSAGE_ID: covered_message_id,
                COVERED_COUNT: covered_count,
                INPUT_VERSION: version,
                UPDATED_AT: updated_at,
            }
        )
        cursor = conn.execute(
            "UPDATE fragments SET summary = ?, summary_model = ?, "
            "summary_version = summary_version + 1, meta = ? WHERE id = ?",
            (
                summary,
                summary_model,
                json.dumps(db_meta, ensure_ascii=False),
                fragment_id,
            ),
        )
    return int(cursor.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# 模型调用：已有摘要 + 未覆盖消息 → 新摘要（输入过大时按块顺序折叠）
# ---------------------------------------------------------------------------


async def summarize_rolling_outcome(
    adapter: BaseAdapter,
    old_summary: str | None,
    messages: Sequence[Any],
    *,
    temperature: float = 0.2,
) -> Outcome[FragmentSummary]:
    """Rolling summary: old summary + new messages -> updated summary.

    只应传入**未覆盖的新消息**。输入超出单次预算时按完整消息分块顺序折叠，
    每一块都基于上一块的结果继续更新，避免尾部截断丢信息。
    """
    from agent.memory.summary import message_chunks, structured_summary_call

    message_list = list(messages)
    if not message_list:
        return Outcome(None, "没有新的未覆盖消息")
    chunks = message_chunks(
        message_list,
        max_chars=ROLLING_INPUT_MAX_CHARS,
        max_tokens=ROLLING_INPUT_MAX_TOKENS,
    )
    system = rolling_summary_task_system()
    current = (old_summary or "").strip() or None
    notes: list[Any] = []
    outcome: Outcome[FragmentSummary] = Outcome(None, "滚动摘要未执行")
    for index, chunk in enumerate(chunks):
        data = {
            "pass_index": index,
            "pass_count": len(chunks),
            "previous_summary": current,
            "new_messages": chunk,
        }
        outcome = await structured_summary_call(
            adapter, system, data, temperature=temperature
        )
        notes.extend(outcome.notes)
        if outcome.value is None:
            reason = outcome.error or "模型不可用"
            return Outcome(
                None,
                f"第 {index + 1}/{len(chunks)} 段滚动摘要失败：{reason}",
                tuple(notes),
            )
        current = outcome.value.summary
    return Outcome(outcome.value, outcome.error, tuple(notes))


async def summarize_rolling(
    adapter: BaseAdapter,
    old_summary: str | None,
    messages: Sequence[Any],
    *,
    temperature: float = 0.2,
) -> tuple[FragmentSummary | None, str | None]:
    """兼容入口：见 :func:`summarize_rolling_outcome`。"""
    outcome = await summarize_rolling_outcome(
        adapter, old_summary, messages, temperature=temperature
    )
    return outcome.value, outcome.error


# 兼容导出：旧调用方 / 既有测试按名字引用。正文只有 prompts 一个来源。
ROLLING_SUMMARY_PROMPT = prompts.PROMPTS["ROLLING_SUMMARY_SYSTEM"]
