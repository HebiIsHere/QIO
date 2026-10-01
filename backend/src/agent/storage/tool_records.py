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

# 新增设置：整条记录（参数 / 错误 / 状态）保留多少天。
# **默认 0 = 永久保留** —— 与既有行为一致：以前只按天清输出全文，记录行不删。
DEFAULT_RECORD_RETENTION_DAYS = 0


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

    惰性导入 + 兜底：存储层不该在 import 期就把整棵 `agent.tools` 拉起来
    （展示名只是显示细节，依赖方向是「工具/服务依赖存储」，不是反过来）。
    历史上这里还写着「必须先 import agent.core 才安全」的导入顺序约束 —— 那条
    约束已经不成立（tests/test_import_smoke.py 会守住「每个模块都能作为第一个
    import」）；保留惰性导入是为了依赖方向本身，而不是为了绕开循环。
    拿不到展示名时回落原始工具名 —— 诚实优先，也绝不让历史读取本身失败。
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


def delete_record(conn: sqlite3.Connection, record_id: str) -> bool:
    """删掉一条工具历史（用户主动「删除这一条」）。返回是否真的删到了。"""
    try:
        cur = conn.execute("DELETE FROM tool_records WHERE id = ?", (str(record_id),))
    except sqlite3.Error as exc:  # noqa: BLE001 - 删除失败如实返回 False
        logger.warning("tool record delete failed: %s", exc)
        return False
    return int(cur.rowcount or 0) > 0


def count_records(conn: sqlite3.Connection, *, topic_id: str | None = None) -> int:
    """工具历史条数（设置页用来说清「清空会删掉多少」）。"""
    try:
        if topic_id:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tool_records WHERE topic_id = ?", (topic_id,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM tool_records").fetchone()
    except sqlite3.Error as exc:  # noqa: BLE001 - 读不到就当 0
        logger.warning("tool record count failed: %s", exc)
        return 0
    return int(row["n"] if row is not None else 0)


def delete_records(
    conn: sqlite3.Connection,
    *,
    topic_id: str | None = None,
    older_than_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """删掉工具历史记录（可限定话题 / 只删 N 天前的）。返回删除条数。

    这是用户主动动作（「清空工具历史」），不做隐式清理：隐式删除用户数据需要
    用户先打开 `tools.record_retention_days`（默认 0 = 永久保留）。
    """
    where: list[str] = []
    params: list[Any] = []
    if topic_id:
        where.append("topic_id = ?")
        params.append(str(topic_id))
    if older_than_days is not None and int(older_than_days) > 0:
        cutoff = (
            (now or datetime.now(timezone.utc)) - timedelta(days=int(older_than_days))
        ).isoformat()
        where.append("created_at < ?")
        params.append(cutoff)
    sql = "DELETE FROM tool_records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    try:
        cur = conn.execute(sql, params)
    except sqlite3.Error as exc:  # noqa: BLE001 - 删除失败不抛给调用方
        logger.warning("tool records delete failed: %s", exc)
        return 0
    return int(cur.rowcount or 0)


def prune_records(
    conn: sqlite3.Connection, retention_days: int, *, now: datetime | None = None
) -> int:
    """按保留期删掉整条工具历史（参数、错误、状态一并删）。

    `retention_days <= 0` 表示永久保留 —— 这是**默认值**，与既有行为完全一致：
    以前只按天清输出全文，记录行本身永久保留。只有用户显式把「记录保留天数」
    设成正数，这里才会删整行。
    """
    if not retention_days or int(retention_days) <= 0:
        return 0
    cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(days=int(retention_days))
    ).isoformat()
    try:
        cur = conn.execute("DELETE FROM tool_records WHERE created_at < ?", (cutoff,))
    except sqlite3.Error as exc:  # noqa: BLE001 - 清理是维护动作
        logger.warning("tool record prune failed: %s", exc)
        return 0
    conn.commit()
    return int(cur.rowcount or 0)
