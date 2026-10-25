"""左侧任务栏：持久化展示记录 + 权威快照（对话页 v2）。

设计边界（见 docs/conversation-page-contract-20261022.md §4.1）：

* **只存展示所需的事实**：身份 / 来源 / 原始与展示状态 / 时间 / 是否需处理 /
  用户是否已知晓 / 预览与详情入口。它不替代任何执行记录：
  - 子任务执行结果在 TaskManager（活）+ 这里的历史事实；
  - 派生任务在 derived_tasks；
  - 维护批次在 maintenance_batches；
  - 工具执行在 tool_records；
  - 开发任务在 DevWorkspace（磁盘 state.json）。
* 展示状态是**诚实映射**：rawStatus 原样保留；不认识的 → unknown，绝不默认 running；
  partially_completed 只能是 partial；budget_exhausted / cancelled 独立存在。
* 派生任务自动重试（state=failed 且已安排退避）不算最终失败，显示为「排队中（自动重试）」。
* 后端重启后**不**把历史任务重新标成 running：活任务状态只从仍存活的
  TaskManager 快照读取；持久行保留它最后一次已知的终态。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SOURCE_SUBAGENT = "subagent"
SOURCE_DERIVED = "derived"
SOURCE_DEV = "dev_task"
SOURCE_MAINTENANCE = "maintenance"
SOURCE_TOOL = "tool"

ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_approval", "waiting_user"})
ATTENTION_STATUSES = frozenset({"waiting_approval", "waiting_user", "failed"})
TERMINAL_STATUSES = frozenset(
    {"completed", "partial", "failed", "cancelled", "budget_exhausted", "unknown"}
)
RECENT_FINISHED_SECONDS = 90.0
DEFAULT_LIMIT = 30
MAX_LIMIT = 100

#: 工具记录里不进入任务栏的「外壳类」工具（它们的等价事实在别的来源里）。
_SHELL_TOOLS = frozenset(
    {
        "await_task",
        "read_task_result",
        "list_available_credentials",
        "memory_search",
        "switch_topic",
        "create_topic",
        "continue_from_fragment",
        "correct_entity",
        "correct_knowledge",
        "declare_completion",
        "dev_list_tasks",
        "dev_list_files",
        "dev_read_file",
        "dev_write_file",
        "echo",
        "now",
    }
)
#: 这些工具明确代表「代码 / 测试 / 脚本 / 清理 / 外部动作」，即使很快也值得列出。
_EXEC_TOOLS = frozenset(
    {
        "run_program",
        "run_shell",
        "dev_run_tests",
        "dev_submit_tool",
        "create_tool",
        "fs_write",
        "fs_patch",
        "proc_kill",
        "web_search",
        "web_fetch",
    }
)

_DERIVED_KIND_LABELS = {
    "summary": "记忆摘要",
    "knowledge": "知识提炼",
    "entities": "实体提炼",
    "entity": "实体提炼",
}

_MAINTENANCE_STATUS_MAP = {
    "running": "running",
    "completed": "completed",
    "partial": "partial",
    "failed": "failed",
    "rolled_back": "cancelled",
    "rollback_partial": "partial",
}

_TOOL_STATUS_MAP = {
    "success": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

_RAW_OUTCOME_MAP = {
    "completed": "completed",
    "partially_completed": "partial",
    "failed": "failed",
    "cancelled": "cancelled",
    "budget_exhausted": "budget_exhausted",
    "done": "completed",
    "running": "running",
    "queued": "queued",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _duration_ms(started: Any, finished: Any) -> int | None:
    begin = _parse_ts(started)
    end = _parse_ts(finished)
    if begin is None or end is None:
        return None
    return max(0, int((end - begin).total_seconds() * 1000))


def _clip(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _camel_actions(actions: list[dict[str, Any]]) -> str:
    return json.dumps(actions, ensure_ascii=False)


def _base_item(
    *,
    source: str,
    source_id: str,
    title: str,
    status: str,
    raw_status: str,
    origin: str = "unknown",
    topic_id: str | None = None,
    topic_title: str | None = None,
    executor: str | None = None,
    summary: str = "",
    queued_at: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    requires_attention: bool = False,
    output_preview: str = "",
    output_truncated: bool = False,
    output_hint: str = "",
    actions: list[dict[str, Any]] | None = None,
    detail: str = "",
    acknowledged: bool = False,
) -> dict[str, Any]:
    return {
        "id": f"{source}:{source_id}",
        "source": source,
        "sourceId": str(source_id),
        "title": _clip(title, 200) or "任务",
        "status": status if status in TERMINAL_STATUSES | ACTIVE_STATUSES else "unknown",
        "rawStatus": str(raw_status or ""),
        "origin": origin if origin in ("topic", "global", "unknown") else "unknown",
        "topicId": topic_id,
        "topicTitle": topic_title,
        "executor": executor,
        "summary": _clip(summary, 400),
        "queuedAt": queued_at,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "durationMs": duration_ms,
        "requiresAttention": bool(requires_attention),
        "acknowledged": bool(acknowledged),
        "output": {
            "truncated": bool(output_truncated),
            "preview": output_preview,
            "hint": output_hint,
            "readMore": bool(actions and any(a.get("kind") == "read_more" for a in actions)),
        },
        "actions": actions or [],
        "detail": _clip(detail, 2000),
    }


# ── 写入：子任务生命周期 ───────────────────────────────────────────────────


def record_subagent(
    conn: sqlite3.Connection,
    record: Any,
    *,
    instance_id: str | None = None,
    tool_label: str | None = None,
) -> None:
    """把一条 TaskManager 记录写成任务栏展示事实（幂等 upsert）。"""
    task_id = str(getattr(record, "task_id", "") or "")
    if not task_id:
        return
    outcome = str(getattr(record, "outcome", "") or "").strip()
    raw_status = outcome or str(getattr(record, "status", "") or "")
    status = _RAW_OUTCOME_MAP.get(raw_status, "unknown")
    if status == "unknown" and getattr(record, "done", False):
        # 终态却读不出精确结局：如实标记「状态无法确认」，不猜成完成。
        status = "unknown"
    origin = dict(getattr(record, "origin", {}) or {})
    topic_id = str(origin.get("topic_id") or "") or None
    topic_title = str(origin.get("topic_title") or "") or None
    tool = str(getattr(record, "tool", "") or "")
    label = tool_label or tool or "子智能体任务"
    result = getattr(record, "result", None)
    error = str(getattr(result, "error", "") or "") if result is not None else ""
    content = str(
        getattr(record, "full_content", None)
        or (getattr(result, "content", "") if result is not None else "")
        or ""
    )
    total_chars = int(getattr(record, "total_chars", 0) or len(content))
    created = str(getattr(record, "created_at", "") or "") or _now()
    finished = getattr(record, "finished_at", None)
    started = None
    if status in ("running", "completed", "partial", "failed", "cancelled", "budget_exhausted"):
        started = created
    if status == "queued":
        finished = None
    actions: list[dict[str, Any]] = [
        {"kind": "read_more", "label": "查看更多", "target": task_id, "supported": True}
    ]
    if not getattr(record, "done", False):
        actions.append(
            {"kind": "cancel", "label": "取消任务", "target": task_id, "supported": True}
        )
    requires_attention = status == "failed"
    if status == "failed" and error:
        detail = error
        summary = error
    elif status == "partial":
        detail = "任务按真实结束事实收口：只完成了部分工作。"
        summary = content
    else:
        detail = error
        summary = content
    row = _base_item(
        source=SOURCE_SUBAGENT,
        source_id=task_id,
        title=f"独立任务 · {label}",
        status=status,
        raw_status=raw_status,
        origin="topic" if topic_id else "unknown",
        topic_id=topic_id,
        topic_title=topic_title,
        executor=label,
        summary=summary,
        queued_at=created if status == "queued" else created,
        started_at=started,
        finished_at=str(finished) if finished else None,
        duration_ms=_duration_ms(created, finished),
        requires_attention=requires_attention,
        output_preview=_clip(content, 400),
        output_truncated=total_chars > 400,
        output_hint="仅显示部分" if total_chars > 400 else "",
        actions=actions,
        detail=detail,
    )
    _upsert(conn, row, instance_id=instance_id)


def _upsert(conn: sqlite3.Connection, row: dict[str, Any], *, instance_id: str | None) -> None:
    now = _now()
    try:
        conn.execute(
            """
            INSERT INTO task_feed (
                id, source, source_id, title, origin, topic_id, topic_title, status,
                raw_status, executor, summary, queued_at, started_at, finished_at,
                duration_ms, requires_attention, output_preview, output_truncated,
                output_hint, actions, detail, instance_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                origin = excluded.origin,
                topic_id = COALESCE(excluded.topic_id, task_feed.topic_id),
                topic_title = COALESCE(excluded.topic_title, task_feed.topic_title),
                status = excluded.status,
                raw_status = excluded.raw_status,
                executor = excluded.executor,
                summary = excluded.summary,
                queued_at = COALESCE(task_feed.queued_at, excluded.queued_at),
                started_at = COALESCE(excluded.started_at, task_feed.started_at),
                finished_at = COALESCE(excluded.finished_at, task_feed.finished_at),
                duration_ms = COALESCE(excluded.duration_ms, task_feed.duration_ms),
                requires_attention = excluded.requires_attention,
                output_preview = excluded.output_preview,
                output_truncated = excluded.output_truncated,
                output_hint = excluded.output_hint,
                actions = excluded.actions,
                detail = excluded.detail,
                instance_id = excluded.instance_id,
                updated_at = excluded.updated_at
            """,
            (
                row["id"], row["source"], row["sourceId"], row["title"], row["origin"],
                row["topicId"], row["topicTitle"], row["status"], row["rawStatus"],
                row["executor"], row["summary"], row["queuedAt"], row["startedAt"],
                row["finishedAt"], row["durationMs"], 1 if row["requiresAttention"] else 0,
                row["output"]["preview"], 1 if row["output"]["truncated"] else 0,
                row["output"]["hint"], _camel_actions(row["actions"]), row["detail"],
                instance_id, now,
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001 - 展示记录写失败不影响任务本身
        logger.warning("task feed upsert failed: %s", exc)


# ── 读取：各来源 → 统一展示模型 ───────────────────────────────────────────


def _acknowledged_ids(conn: sqlite3.Connection) -> set[str]:
    if not _table_exists(conn, "task_ack"):
        return set()
    try:
        return {str(row["id"]) for row in conn.execute("SELECT id FROM task_ack")}
    except sqlite3.Error:
        return set()


def _topic_titles(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    clean = [value for value in ids if value]
    if not clean:
        return {}
    try:
        placeholders = ",".join("?" for _ in clean)
        rows = conn.execute(
            f"SELECT id, name FROM nodes WHERE id IN ({placeholders})", clean
        ).fetchall()
        return {str(row["id"]): str(row["name"]) for row in rows}
    except sqlite3.Error:
        return {}


def _subagent_rows(ctx: Any, acked: set[str]) -> list[dict[str, Any]]:
    conn = ctx.conn
    if not _table_exists(conn, "task_feed"):
        return []
    rows: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT * FROM task_feed WHERE source = ? ORDER BY updated_at DESC LIMIT 200",
        (SOURCE_SUBAGENT,),
    ):
        item = _row_to_item(row)
        item["acknowledged"] = item["id"] in acked
        rows[item["id"]] = item
    # 活任务用 TaskManager 的**当前**快照校正（持久行可能落后一次状态写入）。
    try:
        for snapshot in ctx.task_manager.snapshot():
            raw = str(snapshot.get("outcome") or snapshot.get("status") or "")
            status = _RAW_OUTCOME_MAP.get(raw, "unknown")
            task_id = str(snapshot.get("task_id") or "")
            if not task_id:
                continue
            item_id = f"{SOURCE_SUBAGENT}:{task_id}"
            origin = dict(snapshot.get("origin") or {})
            topic_id = str(origin.get("topic_id") or "") or None
            label = str(snapshot.get("tool") or "")
            base = _base_item(
                source=SOURCE_SUBAGENT,
                source_id=task_id,
                title=f"独立任务 · {label or '子智能体任务'}",
                status=status,
                raw_status=raw,
                origin="topic" if topic_id else "unknown",
                topic_id=topic_id,
                executor=label or None,
                summary=str(snapshot.get("content_preview") or snapshot.get("error") or ""),
                queued_at=None,
                started_at=None,
                finished_at=None,
                requires_attention=False,
                output_preview=str(snapshot.get("content_preview") or ""),
                actions=[
                    {"kind": "read_more", "label": "查看更多", "target": task_id, "supported": True},
                    {"kind": "cancel", "label": "取消任务", "target": task_id, "supported": True},
                ],
            )
            existing = rows.get(item_id)
            base["queuedAt"] = existing["queuedAt"] if existing else None
            base["startedAt"] = existing["startedAt"] if existing else None
            base["acknowledged"] = False
            rows[item_id] = base
    except Exception:  # noqa: BLE001 - 快照读失败不拖垮整页
        logger.warning("task feed live snapshot failed", exc_info=True)
    return list(rows.values())


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    try:
        actions = json.loads(row["actions"] or "[]")
    except (TypeError, ValueError):
        actions = []
    return {
        "id": str(row["id"]),
        "source": str(row["source"]),
        "sourceId": str(row["source_id"]),
        "title": str(row["title"] or "任务"),
        "status": str(row["status"] or "unknown"),
        "rawStatus": str(row["raw_status"] or ""),
        "origin": str(row["origin"] or "unknown"),
        "topicId": row["topic_id"],
        "topicTitle": row["topic_title"],
        "executor": row["executor"],
        "summary": str(row["summary"] or ""),
        "queuedAt": row["queued_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "durationMs": row["duration_ms"],
        "requiresAttention": bool(row["requires_attention"]),
        "acknowledged": bool(row["acknowledged_at"]),
        "output": {
            "truncated": bool(row["output_truncated"]),
            "preview": str(row["output_preview"] or ""),
            "hint": str(row["output_hint"] or ""),
            "readMore": any(action.get("kind") == "read_more" for action in actions),
        },
        "actions": actions,
        "detail": str(row["detail"] or ""),
    }


def _maintenance_rows(conn: sqlite3.Connection, acked: set[str]) -> list[dict[str, Any]]:
    if not _table_exists(conn, "maintenance_batches"):
        return []
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, trigger_reason, status, model, created_at, finished_at, stats, error "
        "FROM maintenance_batches ORDER BY created_at DESC LIMIT 100"
    ):
        raw = str(row["status"] or "")
        status = _MAINTENANCE_STATUS_MAP.get(raw, "unknown")
        stats: dict[str, Any] = {}
        try:
            stats = json.loads(row["stats"] or "{}")
        except (TypeError, ValueError):
            stats = {}
        parts = []
        for key, label in (
            ("scanned", "检查"),
            ("applied", "应用"),
            ("asked", "提问"),
            ("rejected", "未采纳"),
            ("failed", "失败"),
        ):
            value = stats.get(key)
            if isinstance(value, (int, float)) and value:
                parts.append(f"{label} {int(value)} 项")
        summary = "，".join(parts) or ("正在整理记忆" if status == "running" else "")
        error = str(row["error"] or "")
        item_id = f"{SOURCE_MAINTENANCE}:{row['id']}"
        actions = [
            {"kind": "read_more", "label": "查看详情", "target": str(row["id"]), "supported": True}
        ]
        if status == "failed":
            actions.append({"kind": "ack", "label": "知道了", "target": str(row["id"]), "supported": True})
        rows.append(
            _base_item(
                source=SOURCE_MAINTENANCE,
                source_id=str(row["id"]),
                title="记忆整理",
                status=status,
                raw_status=raw,
                origin="global",
                executor=row["model"],
                summary=summary,
                queued_at=row["created_at"],
                started_at=row["created_at"],
                finished_at=row["finished_at"],
                duration_ms=_duration_ms(row["created_at"], row["finished_at"]),
                requires_attention=status == "failed",
                actions=actions,
                detail=error,
                acknowledged=item_id in acked,
            )
        )
    return rows


def _derived_rows(conn: sqlite3.Connection, acked: set[str]) -> list[dict[str, Any]]:
    if not _table_exists(conn, "derived_tasks"):
        return []
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT d.id, d.kind, d.state, d.attempts, d.last_error, d.run_after, "
        "d.created_at, d.updated_at, f.topic_id AS topic_id "
        "FROM derived_tasks d LEFT JOIN fragments f ON f.id = d.fragment_id "
        "ORDER BY d.updated_at DESC LIMIT 200"
    ):
        state = str(row["state"] or "")
        attempts = int(row["attempts"] or 0)
        retrying = state == "failed" and bool(row["run_after"])
        if state == "completed":
            status = "completed"
        elif state == "running":
            status = "running"
        elif retrying:
            status = "queued"
        elif state == "failed":
            status = "failed"
        elif state == "pending":
            status = "queued"
        else:
            status = "unknown"
        raw = state if not retrying else f"{state}（自动重试第 {attempts} 次）"
        kind = str(row["kind"] or "")
        title = _DERIVED_KIND_LABELS.get(kind, "后台整理")
        error = str(row["last_error"] or "")
        summary = (
            f"等待自动重试（已尝试 {attempts} 次）"
            if retrying
            else (error or "")
        )
        item_id = f"{SOURCE_DERIVED}:{row['id']}"
        rows.append(
            _base_item(
                source=SOURCE_DERIVED,
                source_id=str(row["id"]),
                title=title,
                status=status,
                raw_status=raw,
                origin="topic" if row["topic_id"] else "unknown",
                topic_id=row["topic_id"],
                summary=summary,
                queued_at=row["created_at"],
                started_at=row["created_at"] if status == "running" else None,
                finished_at=row["updated_at"] if status == "completed" else None,
                requires_attention=status == "failed",
                actions=[
                    {"kind": "read_more", "label": "查看详情", "target": str(row["id"]), "supported": True}
                ]
                + (
                    [{"kind": "ack", "label": "知道了", "target": str(row["id"]), "supported": True}]
                    if status == "failed"
                    else []
                ),
                detail=error,
                acknowledged=item_id in acked,
            )
        )
    return rows


def _dev_rows(ctx: Any, acked: set[str]) -> list[dict[str, Any]]:
    try:
        tasks = ctx.dev_workspaces.list_tasks()
    except Exception:  # noqa: BLE001
        return []
    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        if not task_id:
            continue
        submitted = bool(getattr(task, "submitted", False))
        abandoned = bool(getattr(task, "abandoned", False))
        active = None
        try:
            active = ctx.dev_workspaces.active_run(task_id)
        except Exception:  # noqa: BLE001
            active = None
        if abandoned:
            status, raw = "cancelled", "abandoned"
        elif submitted:
            status, raw = "completed", "submitted"
        elif active is not None:
            status, raw = "running", str(active.get("stage") or "running")
        else:
            status, raw = "waiting_user", "unfinished"
        actions: list[dict[str, Any]] = []
        if status == "waiting_user":
            actions = [
                {"kind": "continue_dev", "label": "继续开发", "target": task_id, "supported": True},
                {"kind": "revoke_auth", "label": "撤销执行授权", "target": task_id,
                 "supported": bool(getattr(task, "test_authorization", None))},
                {"kind": "abandon_dev", "label": "放弃开发", "target": task_id, "supported": True},
            ]
        summary = str(getattr(task, "last_test_summary", "") or getattr(task, "request", "") or "")
        rows.append(
            _base_item(
                source=SOURCE_DEV,
                source_id=task_id,
                title=str(getattr(task, "request", "") or "开发任务"),
                status=status,
                raw_status=raw,
                origin="topic",
                summary=summary,
                queued_at=getattr(task, "created_at", None),
                started_at=getattr(task, "created_at", None) if status == "running" else None,
                finished_at=getattr(task, "submitted_at", None),
                requires_attention=status == "waiting_user",
                actions=actions,
                detail=str(getattr(task, "last_test_summary", "") or ""),
                acknowledged=False,
            )
        )
    return rows


def _tool_rows(ctx: Any, conn: sqlite3.Connection, acked: set[str]) -> list[dict[str, Any]]:
    if not _table_exists(conn, "tool_records"):
        return []
    subagent_tools: set[str] = set()
    try:
        for definition in ctx.tool_store.load_all():
            if str(getattr(definition, "tool_type", "")) == "subagent":
                subagent_tools.add(str(getattr(definition, "name", "")))
    except Exception:  # noqa: BLE001
        subagent_tools = set()
    rows: list[dict[str, Any]] = []
    try:
        records = conn.execute(
            "SELECT id, turn_id, topic_id, tool_name, status, error, duration_ms, truncated, "
            "output_missing, missing_reason, created_at, substr(output, 1, 400) AS preview, "
            "length(output) AS output_chars FROM tool_records "
            "ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    except sqlite3.Error:
        return []
    for row in records:
        name = str(row["tool_name"] or "")
        if name in subagent_tools or name in _SHELL_TOOLS:
            continue
        raw_status = str(row["status"] or "")
        duration = row["duration_ms"]
        interesting = (
            name in _EXEC_TOOLS
            or raw_status != "success"
            or (isinstance(duration, (int, float)) and duration >= 1000)
        )
        if not interesting:
            continue
        status = _TOOL_STATUS_MAP.get(raw_status, "unknown")
        created_at = str(row["created_at"] or "")
        from agent.tools.display import tool_label

        label = tool_label(name)
        missing = bool(row["output_missing"])
        missing_reason = str(row["missing_reason"] or "")
        hint = ""
        if missing:
            hint = (
                "输出正文已按保留策略清理"
                if missing_reason == "retention"
                else "这次没有保存输出正文"
            )
        error = str(row["error"] or "")
        preview = "" if missing else str(row["preview"] or "")
        truncated = bool(row["truncated"]) or int(row["output_chars"] or 0) > 400
        item_id = f"{SOURCE_TOOL}:{row['id']}"
        actions = [
            {"kind": "read_more", "label": "查看更多", "target": str(row["id"]), "supported": True}
        ]
        if status == "failed":
            actions.append({"kind": "ack", "label": "知道了", "target": str(row["id"]), "supported": True})
        started = None
        if isinstance(duration, (int, float)) and _parse_ts(created_at) is not None:
            started = (
                _parse_ts(created_at).timestamp() - float(duration) / 1000.0
            )
            started = datetime.fromtimestamp(started, tz=timezone.utc).isoformat()
        rows.append(
            _base_item(
                source=SOURCE_TOOL,
                source_id=str(row["id"]),
                title=f"工具执行 · {label}",
                status=status,
                raw_status=raw_status,
                origin="topic" if row["topic_id"] else "unknown",
                topic_id=row["topic_id"],
                executor=label,
                summary=error or preview.replace("\n", " "),
                queued_at=created_at,
                started_at=started,
                finished_at=created_at,
                duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
                requires_attention=status == "failed",
                output_preview=preview,
                output_truncated=truncated,
                output_hint=hint or ("仅显示部分" if truncated else ""),
                actions=actions,
                detail=error or missing_reason,
                acknowledged=item_id in acked,
            )
        )
    return rows


def _enrich_topics(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
    ids = {str(item.get("topicId")) for item in items if item.get("topicId")}
    titles = _topic_titles(conn, ids)
    for item in items:
        if item.get("topicId") and not item.get("topicTitle"):
            item["topicTitle"] = titles.get(str(item["topicId"]))


def _sort_key(item: dict[str, Any]) -> str:
    return str(item.get("startedAt") or item.get("queuedAt") or "")


def _view_items(items: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    current: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for item in items:
        status = item["status"]
        attention = bool(item.get("requiresAttention")) and not item.get("acknowledged")
        finished = _parse_ts(item.get("finishedAt"))
        recent = (
            finished is not None
            and (now - finished).total_seconds() <= RECENT_FINISHED_SECONDS
        )
        if status in ACTIVE_STATUSES:
            is_current = True
        elif attention:
            # 需要用户处理的失败/等待项一直留在当前，直到用户点「知道了」。
            is_current = True
        elif finished is None:
            # 没有结束时间的终态行：无法证明它已经结束很久（历史行也不会凭空消失），
            # 留在当前并如实显示真实状态，而不是悄悄塞进历史。
            is_current = True
        else:
            # 成功/部分完成/取消/预算耗尽：短暂停留后进入历史；用户已知晓的直接进历史。
            is_current = recent and not item.get("acknowledged")
        (current if is_current else history).append(item)
    # 需要处理置顶；其余按实际开始（排队项用入队时间）从新到旧。
    def ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: (
                0 if (row.get("requiresAttention") and not row.get("acknowledged")) else 1,
                _invert(_sort_key(row)),
            ),
        )

    return ordered(current if view == "current" else history)


def _invert(value: str) -> str:
    """ISO 时间字符串倒序：用 `~` 不可行，直接返回负的 timestamp。"""
    parsed = _parse_ts(value)
    return f"{-(parsed.timestamp() if parsed else 0.0):020.3f}"


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "queued": 0,
        "running": 0,
        "waitingApproval": 0,
        "waitingUser": 0,
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "cancelled": 0,
        "budgetExhausted": 0,
        "unknown": 0,
    }
    for item in items:
        status = item["status"]
        if status == "waiting_approval":
            counts["waitingApproval"] += 1
        elif status == "waiting_user":
            counts["waitingUser"] += 1
        elif status == "budget_exhausted":
            counts["budgetExhausted"] += 1
        elif status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
    return counts


def build_feed(
    ctx: Any, *, view: str = "current", cursor: str | None = None, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """权威快照：当前 / 历史两个视图 + 计数 + 分页游标。"""
    conn = ctx.conn
    acked = _acknowledged_ids(conn)
    items: dict[str, dict[str, Any]] = {}
    for collect in (
        lambda: _subagent_rows(ctx, acked),
        lambda: _derived_rows(conn, acked),
        lambda: _dev_rows(ctx, acked),
        lambda: _maintenance_rows(conn, acked),
        lambda: _tool_rows(ctx, conn, acked),
    ):
        try:
            for item in collect():
                items[item["id"]] = item
        except Exception:  # noqa: BLE001 - 一个来源坏掉不该让整页无任务
            logger.warning("task feed source failed", exc_info=True)
    merged = list(items.values())
    deny = frozenset({"unknown"})
    for item in merged:
        if item["status"] in deny:
            item["status"] = "unknown"
    _enrich_topics(conn, merged)
    counts = _counts(merged)
    ordered = _view_items(merged, "current" if view != "history" else "history")
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        offset = 0
    size = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    page = ordered[offset : offset + size]
    next_cursor = str(offset + size) if offset + size < len(ordered) else None
    return {
        "items": page,
        "nextCursor": next_cursor,
        "counts": counts,
        "serverTime": _now(),
        "instanceId": getattr(ctx, "instance_id", None),
    }


# ── 详情 / 确认 / 取消 ────────────────────────────────────────────────────


def _find_item(ctx: Any, task_id: str) -> tuple[dict[str, Any] | None, str, str]:
    if ":" not in task_id:
        return None, "", ""
    source, _, source_id = task_id.partition(":")
    conn = ctx.conn
    acked = _acknowledged_ids(conn)
    pools = {
        SOURCE_SUBAGENT: lambda: _subagent_rows(ctx, acked),
        SOURCE_DERIVED: lambda: _derived_rows(conn, acked),
        SOURCE_DEV: lambda: _dev_rows(ctx, acked),
        SOURCE_MAINTENANCE: lambda: _maintenance_rows(conn, acked),
        SOURCE_TOOL: lambda: _tool_rows(ctx, conn, acked),
    }
    collector = pools.get(source)
    if collector is None:
        return None, source, source_id
    try:
        for item in collector():
            if item["id"] == task_id:
                return item, source, source_id
    except Exception:  # noqa: BLE001
        logger.warning("task detail lookup failed", exc_info=True)
    return None, source, source_id


def task_detail(ctx: Any, task_id: str) -> dict[str, Any] | None:
    item, source, source_id = _find_item(ctx, task_id)
    if source == SOURCE_SUBAGENT:
        live = ctx.task_manager.task_facts(source_id)
        if live is not None and item is not None:
            page = ctx.task_manager.read_result(source_id, offset=0, limit=8000) or {}
            content = str(page.get("content") or "")
            parts = [
                {
                    "kind": "output",
                    "label": "结果",
                    "text": content,
                    "truncated": bool(page.get("has_more")),
                }
            ]
            if live.get("error"):
                parts.append(
                    {"kind": "error", "label": "失败原因", "text": str(live["error"]), "truncated": False}
                )
            return {
                "id": task_id,
                "item": item,
                "parts": parts,
                "outputMissing": False,
                "missingReason": "",
            }
    if source == SOURCE_TOOL:
        from agent.storage import tool_records

        record = tool_records.get_record(ctx.conn, source_id)
        if record is None:
            return None
        parts = [
            {"kind": "output", "label": "输出", "text": str(record.get("output") or ""),
             "truncated": bool(record.get("truncated"))},
            {"kind": "error", "label": "失败原因", "text": str(record.get("error") or ""),
             "truncated": False},
        ]
        missing = bool(record.get("output_missing"))
        return {
            "id": task_id,
            "item": item,
            "parts": [part for part in parts if part["text"]],
            "outputMissing": missing,
            "missingReason": str(record.get("missing_reason") or ""),
        }
    if item is None:
        return None
    parts = [
        {"kind": "text", "label": "任务", "text": item.get("summary") or item.get("title") or "",
         "truncated": False},
        {"kind": "error", "label": "原因", "text": item.get("detail") or "", "truncated": False},
    ]
    return {
        "id": task_id,
        "item": item,
        "parts": [part for part in parts if part["text"]],
        "outputMissing": False,
        "missingReason": "",
    }


def acknowledge(ctx: Any, task_id: str) -> bool:
    conn = ctx.conn
    now = _now()
    try:
        conn.execute(
            "INSERT INTO task_ack (id, acknowledged_at) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET acknowledged_at = excluded.acknowledged_at",
            (task_id, now),
        )
        if _table_exists(conn, "task_feed"):
            conn.execute("UPDATE task_feed SET acknowledged_at = ? WHERE id = ?", (now, task_id))
        conn.commit()
        return True
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.warning("task ack failed: %s", exc)
        return False


def cancel_task(ctx: Any, task_id: str) -> dict[str, Any]:
    """只有真实支持的控制动作才允许取消；申请取消不等于底层执行单元已停止。"""
    _, source, source_id = _find_item(ctx, task_id)
    if source == SOURCE_SUBAGENT:
        try:
            accepted = bool(ctx.task_manager.cancel(source_id))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "accepted": False, "detail": f"取消请求失败：{exc}"}
        if not accepted:
            return {
                "ok": False,
                "accepted": False,
                "detail": "这个任务已经结束或已不存在，没有可取消的执行",
            }
        return {
            "ok": True,
            "accepted": True,
            "detail": "已提交取消请求；任务的真实结束事实会以它自己的终态为准",
        }
    return {
        "ok": False,
        "accepted": False,
        "detail": "这个来源的任务没有单独的取消入口，只能按它自己的业务流程处理",
    }
