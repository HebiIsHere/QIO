"""工具调用历史：写入 / 读取 / 清理。

与轨迹表的分工：`tool_calls`、`turn_traces` 是审计用途（截断摘要、可被用户关掉）；
这里存的是**用户能回看的完整记录**（打码后的参数与输出全文，可设置是否保存、
按天数清理输出）。它只服务界面复盘 —— 不进上下文、不进片段摘要、不进检索索引，
模型读不到这些内容（2026-09-24 与用户确认的产品边界）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.trace.redact import redact_any, redact_text

logger = logging.getLogger(__name__)

# 单条正文上限：与 web_fetch 的输出上限（4 万字）对齐 —— 工具自己产出不了更长的输出。
MAX_TEXT_CHARS = 40_000
# 历史接口随消息带回来的预览长度；全文走按 id 取全文的接口。
PREVIEW_CHARS = 400
STATUSES = ("success", "failed", "cancelled")

DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3650


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    from agent.memory.fragment import new_id

    return new_id("tr")


def _clip(text: str) -> tuple[str, bool]:
    """超长文本截断；截断标记写进正文，前端据此显示「已截断」徽标。"""
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return f"{text[:MAX_TEXT_CHARS]}\n…（已截断，原文 {len(text)} 字）", True


def record_tool_call(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    topic_id: str | None = None,
    call_id: str = "",
    seq: int = 0,
    tool_name: str,
    arguments: Any = None,
    output: str = "",
    status: str = "success",
    error: str = "",
    duration_ms: int | None = None,
    save_output: bool = True,
) -> str | None:
    """写一行工具记录。

    返回记录 id：重复的 `(turn_id, call_id)` 返回已有行的 id（幂等），写库失败返回 None。
    参数与输出都先过 `trace/redact.py` 打码；**写历史失败绝不影响工具结果**。
    """
    if status not in STATUSES:
        status = "failed"
    args_text, args_truncated = _clip(
        json.dumps(redact_any(arguments or {}), ensure_ascii=False)
    )
    if save_output:
        output_text, truncated = _clip(redact_text(output or ""))
        output_missing, missing_reason = 0, ""
    else:
        # 用户关掉「保存输出全文」：参数、状态、失败原因、耗时照旧保留
        output_text, truncated, output_missing, missing_reason = "", False, 1, "setting"
    record_id = _new_id()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tool_records "
            "(id, turn_id, topic_id, call_id, seq, tool_name, arguments, output, status, "
            " error, duration_ms, truncated, output_missing, missing_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id,
                str(turn_id or ""),
                topic_id,
                str(call_id or ""),
                int(seq or 0),
                str(tool_name or "?"),
                args_text,
                output_text,
                status,
                str(error or ""),
                int(duration_ms) if duration_ms is not None else None,
                1 if (truncated or args_truncated) else 0,
                output_missing,
                missing_reason,
                _now(),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001 - 历史写不进去不是工具失败
        logger.warning("tool record insert failed: %s", exc)
        return None
    if call_id:
        row = conn.execute(
            "SELECT id FROM tool_records WHERE turn_id = ? AND call_id = ?",
            (str(turn_id or ""), str(call_id)),
        ).fetchone()
        if row is not None:
            return row["id"]
    return record_id


def _tool_title(name: str) -> str:
    """工具的中文展示名。

    惰性导入 + 兜底：`agent.tools` 包有既有的导入顺序约束（先导入 `agent.core`
    才安全），而这里可能在只导入存储层时被调用。拿不到展示名时回落原始工具名 ——
    诚实优先，也绝不让历史读取本身失败。
    """
    try:
        from agent.tools.display import tool_label
    except ImportError:  # pragma: no cover - 真实运行路径不会走到
        return name
    return tool_label(name)


def _preview(row: dict) -> dict:

    output = row.get("output") or ""
    return {
        "id": row["id"],
        "turn_id": row["turn_id"],
        "call_id": row["call_id"],
        "seq": row["seq"],
        "tool_name": row["tool_name"],
        "title": _tool_title(row["tool_name"]),
        "status": row["status"],
        "error": row["error"],
        "duration_ms": row["duration_ms"],
        "truncated": bool(row["truncated"]),
        "output_missing": bool(row["output_missing"]),
        "missing_reason": row["missing_reason"],
        "output_chars": len(output),
        "preview": output[:PREVIEW_CHARS],
        "created_at": row["created_at"],
    }


def previews_for_turns(conn: sqlite3.Connection, turn_ids: list[str]) -> list[dict]:
    """按轮次取预览（历史接口随消息带上；不含输出全文）。"""
    ids = [str(t) for t in turn_ids if str(t or "")]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT id, turn_id, call_id, seq, tool_name, output, status, error, duration_ms, "
        "truncated, output_missing, missing_reason, created_at FROM tool_records "
        f"WHERE turn_id IN ({placeholders}) ORDER BY created_at ASC, seq ASC",
        ids,
    ).fetchall()
    return [_preview(dict(r)) for r in rows]


def get_record(conn: sqlite3.Connection, record_id: str) -> dict | None:
    """按 id 取一条记录（含解析后的参数与输出全文）；不存在返回 None。"""
    row = conn.execute("SELECT * FROM tool_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return None
    data = _preview(dict(row))
    raw_args = row["arguments"] or "{}"
    try:
        data["arguments"] = json.loads(raw_args)
    except (TypeError, ValueError):
        data["arguments"] = raw_args
    data["output"] = row["output"] or ""
    return data


def prune_outputs(
    conn: sqlite3.Connection, retention_days: int, *, now: datetime | None = None
) -> int:
    """清掉过期记录的输出全文（记录本身保留）。

    `retention_days <= 0` 表示永久保留，直接不动任何行。返回被清理的记录条数。
    """
    if not retention_days or int(retention_days) <= 0:
        return 0
    cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(days=int(retention_days))
    ).isoformat()
    cur = conn.execute(
        "UPDATE tool_records SET output = '', output_missing = 1, missing_reason = 'retention' "
        "WHERE output_missing = 0 AND created_at < ?",
        (cutoff,),
    )
    conn.commit()
    return int(cur.rowcount or 0)
