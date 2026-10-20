"""明确拒绝的墓碑（Phase 1 C：拒绝事实要能跨入口、跨任务保留）。

背景：用户在对话里「忽略」过一条候选，或者在审批面板把提议「打回」，这条内容就
不能被另一个入口（主对话 propose、后台提炼 commit、管理 API 手动新建）无理由地
再次保存成待审批 / 有效知识。

做法：不新增表，只在既有 provenance JSON 里写 **拒绝事实**
（ignored_at / rejected_at + 原因），历史行与原文都保留；查询按
category + content 精确匹配墓碑（去空白）。这是**事实记录**，不是审批状态：
审批状态仍然由 knowledge.state 表示。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

IGNORED_AT_KEY = "ignored_at"
REJECTED_AT_KEY = "rejected_at"
REJECTION_REASON_KEY = "rejection_reason"

REASON_IGNORED = "user_ignored"
REASON_REJECTED = "user_rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance(row: Any) -> dict:
    try:
        raw = row["provenance"]
    except (KeyError, IndexError, TypeError):
        raw = getattr(row, "provenance", None)
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _rejection_fact(provenance: dict) -> tuple[str, str] | None:
    for key in (REJECTED_AT_KEY, IGNORED_AT_KEY):
        value = str(provenance.get(key) or "").strip()
        if value:
            reason = str(
                provenance.get(REJECTION_REASON_KEY) or provenance.get("reason") or ""
            )
            return key, value, reason
    return None


def rejection_record(
    conn: sqlite3.Connection, *, category: str, content: str
) -> dict[str, Any] | None:
    """同一 category + content 的拒绝事实（最近一条）；没有返回 None。

    只读：不改状态、不清历史。墓碑行通常是 revoked；这里不按状态过滤，
    因为「用户明确拒绝过」本身就是事实，历史状态怎么流转都不改变它。
    """
    text = str(content or "").strip()
    if not text:
        return None
    cat = str(category or "").strip()
    rows = conn.execute(
        "SELECT id, category, content, state, provenance FROM knowledge "
        "WHERE category = ? ORDER BY updated_at DESC",
        (cat,),
    ).fetchall()
    for row in rows:
        if str(row["content"] or "").strip() != text:
            continue
        provenance = _provenance(row)
        fact = _rejection_fact(provenance)
        if fact is None:
            continue
        key, at, reason = fact
        return {
            "knowledge_id": str(row["id"]),
            "category": cat,
            "content": text,
            "state": str(row["state"] or ""),
            "rejected_at": at,
            "marker": key,
            "reason": reason or REASON_REJECTED,
        }
    return None


def is_rejected(
    conn: sqlite3.Connection, *, category: str, content: str
) -> dict[str, Any] | None:
    """兼容别名：返回拒绝事实或 None。"""
    return rejection_record(conn, category=category, content=content)


def _write_fact(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    key: str,
    reason: str,
    at: str | None,
) -> dict | None:
    row = conn.execute(
        "SELECT provenance FROM knowledge WHERE id = ?", (str(knowledge_id),)
    ).fetchone()
    if row is None:
        return None
    provenance = _provenance(row)
    if not (
        str(provenance.get(REJECTED_AT_KEY) or "").strip()
        or str(provenance.get(IGNORED_AT_KEY) or "").strip()
    ):
        provenance[key] = at or _now()
        if key == IGNORED_AT_KEY:
            provenance["reason"] = str(reason or REASON_IGNORED)
        else:
            provenance[REJECTION_REASON_KEY] = str(reason or REASON_REJECTED)
    conn.execute(
        "UPDATE knowledge SET provenance = ?, updated_at = ? WHERE id = ?",
        (json.dumps(provenance, ensure_ascii=False), _now(), str(knowledge_id)),
    )
    return provenance


def mark_rejected(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    reason: str = REASON_REJECTED,
    at: str | None = None,
) -> dict[str, Any] | None:
    """写下明确拒绝的事实（保留 state 与历史，由调用方决定状态流转）。

    返回更新后的 provenance；条目不存在返回 None。重复调用是幂等的
    （保留最早的拒绝时刻，不反复改写成新时间）。
    """
    return _write_fact(
        conn, knowledge_id, key=REJECTED_AT_KEY, reason=reason, at=at
    )


def mark_ignored(
    conn: sqlite3.Connection,
    knowledge_id: str,
    *,
    reason: str = REASON_IGNORED,
    at: str | None = None,
) -> dict[str, Any] | None:
    """用户在对话里「忽略」的墓碑（既有语义：provenance.ignored_at）。"""
    return _write_fact(
        conn, knowledge_id, key=IGNORED_AT_KEY, reason=reason, at=at
    )
