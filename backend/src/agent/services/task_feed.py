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
* 计数与返回的视图同口径：当前视图只统计进行中 + 仍需用户处理的事项；用户点过
  「知道了」的失败进历史并保留真实结果，但不再占「需要处理」数量。终态行即使
  没有结束时间，只要用户已知晓也按真实终态归档（不补造结束时间）。
* 同一轮内只有**同一工具 + 同一操作身份**（cmd+cwd / query / url）的后续调用才能
  收口前一次尝试；不同操作互不覆盖，缺少可追踪身份时不推定关联。
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

# ── 工具失败进入任务栏的条件（用户提示词 §一，2026-11-09）────────────────────
# 单次工具失败默认只保留在执行记录里。任务栏只展示值得持续关注的工作、需要用户
# 处理的阻碍、确实需要持续核查的结果；判断只用程序字段（tool_name / status /
# code / category / turn 结束事实 / 尝试顺序），不按错误文本关键词猜，也不额外调模型。
#
# 本轮只对这三个工具做判定：不把同一规则无条件下沉到其它工具（它们的展示规则不变）。
_TRIAGE_TOOLS = frozenset({"run_shell", "web_search", "web_fetch"})
#: 需要用户真正动手才能继续的失败类别（补齐条件后可继续执行）。
_ACTIONABLE_CATEGORIES = frozenset({"permission", "missing_dependency", "environment", "startup"})
#: 需要用户真正动手的稳定代码（服务未配置 → 去设置里配置）。
_ACTIONABLE_CODES = frozenset({"search_not_configured"})
#: 命令超时可能已产生副作用且无法确认是否生效 → 待核查（只对 run_shell）。
_TRIAGE_TIMEOUT_TOOLS = frozenset({"run_shell"})
#: 每个 triage 工具的「操作身份」参数（程序字段，不读错误文本）。
#:
#: 同一 turn 内只有这些参数完全相同的调用才算「同一项工作的重试/替换尝试」，前一次
#: 失败才能被后续尝试收口；参数不同就是**不同的操作**，彼此不能覆盖结果（例如
#: 「复制文件」超时与「查看当前目录」成功不是同一项工作，不能据此判定复制完成）。
#: 主参数缺失/无法解析时没有可追踪的操作身份，绝不靠工具名、时间接近或同一轮对话猜。
_TRIAGE_IDENTITY_ARGS: dict[str, tuple[str, ...]] = {
    "run_shell": ("cmd", "cwd"),
    "web_search": ("query",),
    "web_fetch": ("url",),
}
#: 非零退出码常见于检查类命令：保留真实结果，但不进任务栏、不计入需要处理。
_EXIT_CODE = "exit_code"
#: turn_journal 里「这一轮还没结束」的状态：此刻模型可能正在重试，不先冒独立条目。
_TURN_OPEN_STATUSES = frozenset({"queued", "running"})

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


def _turn_statuses(conn: sqlite3.Connection, turn_ids: set[str]) -> dict[str, str]:
    """批量取每轮在 turn_journal 里的真实状态（一次查询，不 N+1）。

    缺行 = 没有结束事实可依赖（旧记录 / 已清理 / 非 API 路径写入）：不猜「已完成」，
    这类失败默认不进任务栏 —— 首次加载与重连后不能重新冒出一批旧错误。
    """
    ids = sorted(turn_id for turn_id in turn_ids if turn_id)
    if not ids or not _table_exists(conn, "turn_journal"):
        return {}
    out: dict[str, str] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                "SELECT turn_id, status FROM turn_journal "
                f"WHERE turn_id IN ({placeholders})",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            return out
        for row in rows:
            out[str(row["turn_id"])] = str(row["status"] or "")
    return out


def _triage_identity(tool_name: str, arguments: Any) -> str | None:
    """从参数里取一次调用的操作对象（程序字段，可追踪的重试关系只靠它）。

    返回 None = 主参数缺失 / 参数无法解析 / 该工具没有可追踪身份：调用方此时不得
    把这条尝试与任何别的调用合并（缺少关联时不推定解决）。

    参数本身在写入 tool_records 时已按统一规则打码限长，这里只做相等的规范化
    （去首尾空白，缺省 cwd 视为空），不读错误文本、不做关键词匹配。
    """
    fields = _TRIAGE_IDENTITY_ARGS.get(tool_name)
    if not fields:
        return None
    parsed: Any = arguments
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(parsed, dict):
        return None
    parts: list[str] = []
    for index, field in enumerate(fields):
        value = parsed.get(field)
        if value is None:
            if index == 0:
                return None  # 主参数缺失：没有操作对象，无法追踪
            parts.append("")
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None  # 结构不符合预期：不猜它是不是同一个操作
        text = str(value)
        text = text.strip() if isinstance(value, str) else text
        if index == 0 and not text:
            return None  # 主参数为空 = 没有操作对象（缺参失败），无法追踪
        parts.append(text)
    return "\u0000".join(parts)


def _triage_operation_key(row: Any) -> tuple[str, str, str] | None:
    """一次调用的尝试链身份：同一 turn + 同一工具 + 同一操作身份。

    只有这三个事实都相同，后面的尝试才能收口前面的失败；任何一项不同（换了命令 /
    查询 / 来源，或换了轮次）都是**不同的工作**，不能互相覆盖结果。
    无法从参数里追踪到操作对象时返回 None —— 调用方按「独立记录」处理，绝不靠
    工具名、时间接近或同一轮对话猜它们有关联。
    """
    turn_id = str(row["turn_id"] or "")
    tool_name = str(row["tool_name"] or "")
    if not turn_id or not tool_name:
        return None
    identity = _triage_identity(tool_name, row["arguments"])
    if identity is None:
        return None
    return (turn_id, tool_name, identity)


def _triage_attempts(
    conn: sqlite3.Connection, turn_ids: set[str]
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """这三类工具在相关轮次里的全部尝试，按**操作身份**分组、按真实执行顺序排。

    同一 turn 内同一工具、同一操作对象（run_shell 的 cmd+cwd / web_search 的 query /
    web_fetch 的 url）的多次调用才构成一条「尝试链」：链里只有最后一次尝试能代表这项
    工作的最终状态，前面的失败要么已被后续重试补齐，要么已被最新一次尝试取代。

    不同操作对象之间没有可追踪的重试关系（例如「复制文件」超时之后「查看当前目录」
    成功），不并成一条链，也就不能拿后者的结果去撤掉前者的待核查提醒。
    """
    ids = sorted(turn_id for turn_id in turn_ids if turn_id)
    if not ids:
        return {}
    chains: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                "SELECT id, turn_id, seq, tool_name, status, code, category, arguments, "
                "created_at "
                "FROM tool_records "
                f"WHERE turn_id IN ({placeholders}) "
                "AND tool_name IN ('run_shell', 'web_search', 'web_fetch') "
                "ORDER BY turn_id, created_at, seq",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            return {}
        for row in rows:
            key = _triage_operation_key(row)
            if key is None:
                # 无法追踪操作身份：这条尝试不并入任何链（它只代表自己）。
                key = (str(row["turn_id"] or ""), str(row["tool_name"] or ""),
                       "\u0000record:" + str(row["id"]))
            chains.setdefault(key, []).append(dict(row))
    return chains


def _triage_deserves_attention(tool_name: str, row: dict[str, Any]) -> bool:
    """依据结构化事实判断「这条失败值不值得进任务栏」——不读错误正文。"""
    code = str(row.get("code") or "")
    category = str(row.get("category") or "")
    if code == _EXIT_CODE:
        # 非零退出码可能是检查结果（grep / diff 的常见返回值）：执行记录保留真实值，
        # 但任务栏不据此推导「整个任务失败」，也不计入需要处理数量。
        return False
    if category in _ACTIONABLE_CATEGORIES or code in _ACTIONABLE_CODES:
        return True
    if category == "timeout" and tool_name in _TRIAGE_TIMEOUT_TOOLS:
        # 命令可能已经产生副作用，无法确认是否生效 → 待核查。
        return True
    # 其余（external_service / code_error / output_format / assertion / unknown /
    # 没有结构化事实的旧行）：瞬时失败或已由本轮回复解释，默认不进任务栏。
    return False


def _triage_summary(row: dict[str, Any], label: str) -> str:
    """需要处理/待核查条目的可读中文说明：说清用户要做什么、处理后能继续什么。"""
    code = str(row.get("code") or "")
    category = str(row.get("category") or "")
    if code == "search_not_configured":
        return (
            "联网搜索服务还没有配置：在「设置 → 模型与服务」里配置搜索服务后，"
            "搜索请求才能拿到结果。"
        )
    if category == "timeout" or code == "timeout":
        return (
            f"{label}超时，无法确认是否已经生效：请先核查目标是否已被改动，"
            "再决定是否重新执行。"
        )
    if category == "permission":
        return (
            f"{label}需要你的授权才能继续：批准这次调用或调整执行权限后，"
            "可以重新执行它。"
        )
    if category == "missing_dependency":
        return f"运行环境缺少{label}需要的依赖或程序：补齐后可以继续这次执行。"
    if category in ("environment", "startup"):
        return f"{label}没有启动成功（执行环境不可用）：检查程序或环境后可以重新执行。"
    return f"{label}失败后需要你处理：处理后可以继续这次工作。"


def _triage_hidden_ids(
    conn: sqlite3.Connection, records: list[sqlite3.Row]
) -> set[str]:
    """本轮过滤：返回失败但「不值得进任务栏」的记录 id（执行记录本身原样保留）。

    规则（全部来自结构化事实）：
    1) 同一 turn 内同一工具、**同一操作身份**的尝试链只认最后一次尝试：同一项工作
       被重试/改写前的失败不单列；不同操作（不同命令/查询/来源）互不覆盖；
    2) 无法追踪操作身份的失败按独立记录处理 —— 不靠工具相同、时间接近或同一轮推定；
    3) 同一操作之后已有新的执行结果（成功或失败）→ 旧失败不再单列；这一轮还在跑/
       排队（可能正在重试）或没有结束事实 → 暂不列；
    4) 按 code / category 判定：只有真正需要用户处理或必须持续核查的才列。
    """
    candidates: dict[str, dict[str, Any]] = {}
    turn_ids: set[str] = set()
    for row in records:
        name = str(row["tool_name"] or "")
        if name not in _TRIAGE_TOOLS or str(row["status"] or "") != "failed":
            continue
        record = dict(row)
        candidates[str(row["id"])] = record
        turn_id = str(row["turn_id"] or "")
        if turn_id:
            turn_ids.add(turn_id)
    if not candidates:
        return set()

    chains = _triage_attempts(conn, turn_ids)
    statuses = _turn_statuses(conn, turn_ids)
    hidden: set[str] = set()
    for record_id, record in candidates.items():
        turn_id = str(record.get("turn_id") or "")
        if not turn_id:
            hidden.add(record_id)  # 没有可靠轮次事实：无法判定重试/补齐，默认不展示
            continue
        key = _triage_operation_key(record)
        chain = chains.get(key) if key is not None else None
        if chain is None:
            # 没有可追踪的操作身份：这条失败只代表它自己，绝不与同工具/同轮其它调用合并。
            chain = [record]
        position = next(
            (
                index
                for index, attempt in enumerate(chain)
                if str(attempt.get("id")) == record_id
            ),
            None,
        )
        later = chain[position + 1 :] if position is not None else []
        # 同一操作已有新的执行结果（成功=重试补齐；失败=被最新一次尝试取代）。
        # 只有被取消的后续尝试不算结果 —— 它不能证明原操作是否已经生效。
        if any(str(attempt.get("status") or "") in ("success", "failed") for attempt in later):
            hidden.add(record_id)
            continue
        status = statuses.get(turn_id, "")
        if status in _TURN_OPEN_STATUSES or not status:
            hidden.add(record_id)  # 这一轮还在跑/没有结束事实：先不生成独立条目
            continue
        if not _triage_deserves_attention(str(record.get("tool_name") or ""), record):
            hidden.add(record_id)
    return hidden


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
            "length(output) AS output_chars, code, category, recoverable, result_status, "
            "arguments "
            "FROM tool_records ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    except sqlite3.Error:
        return []
    # 用户 §一：单次失败默认只留在执行记录里。过滤发生在生成处，当前/历史/计数/
    # 徽标/顶部通知因此共用同一批 item（不是只在 Vue 模板里藏一行）。
    hidden = _triage_hidden_ids(conn, records)
    for row in records:
        name = str(row["tool_name"] or "")
        if name in subagent_tools or name in _SHELL_TOOLS:
            continue
        if str(row["id"]) in hidden:
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
        # 默认摘要用工具给的原文；这三类工具的失败若是「值得展示」的，
        # 摘要换成可读中文（要做什么 / 处理后能继续什么），错误原文只进详情。
        summary = error or preview.replace("\n", " ")
        detail = error or missing_reason
        attention = status == "failed"
        if name in _TRIAGE_TOOLS and raw_status == "failed":
            attention = True
            summary = _triage_summary(dict(row), label)
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
                summary=summary,
                queued_at=created_at,
                started_at=started,
                finished_at=created_at,
                duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
                requires_attention=attention,
                output_preview=preview,
                output_truncated=truncated,
                output_hint=hint or ("仅显示部分" if truncated else ""),
                actions=actions,
                detail=detail,
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


#: 当前视图中「需要用户处理」的状态：与前端 isAttentionItem / TaskCounts 的
#: waitingApproval / waitingUser / failed 三个计数键同口径。
def _needs_attention(item: dict[str, Any]) -> bool:
    return bool(item.get("requiresAttention")) and not item.get("acknowledged")


def _classify_views(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按真实终态与用户知晓状态把合并后的事项分到当前 / 历史两个视图。

    * 进行中的状态（排队 / 运行 / 等待）一直在当前；
    * 仍需用户处理的失败/等待项一直在当前，直到用户点「知道了」；
    * 已「知道了」的终态（含失败）直接进历史 —— **不要求它有完成时间**：后台整理
      等来源可能没有结束事实，但终态 + 已知晓本身就是可以归档的真实事实；
    * 其余终态：没有结束时间的仍留在当前（无法证明它已经结束很久，也不伪造结束
      时间），有结束时间的停留一小段可读期后进历史。
    两边各自「需要处理置顶、其余按真实开始/入队时间从新到旧」。
    """
    now = datetime.now(timezone.utc)
    current: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for item in items:
        status = item["status"]
        finished = _parse_ts(item.get("finishedAt"))
        recent = (
            finished is not None
            and (now - finished).total_seconds() <= RECENT_FINISHED_SECONDS
        )
        if status in ACTIVE_STATUSES:
            is_current = True
        elif _needs_attention(item):
            is_current = True
        elif item.get("acknowledged"):
            # 用户已知晓的终态：按真实终态归档；缺失结束时间不影响归类，也不补造时间。
            is_current = False
        elif finished is None:
            is_current = True
        else:
            is_current = recent
        (current if is_current else history).append(item)
    # 需要处理置顶；其余按实际开始（排队项用入队时间）从新到旧。
    def ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows,
            key=lambda row: (
                0 if _needs_attention(row) else 1,
                _invert(_sort_key(row)),
            ),
        )

    return ordered(current), ordered(history)


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
    """权威快照：当前 / 历史两个视图 + 计数 + 分页游标。

    计数与本次返回的视图同口径（数字与可见事项一致）：
    * view=current 只统计仍留在当前视图的事项（进行中 + 仍需用户处理）；
      用户点过「知道了」的失败已经归档到历史，保留真实结果但不再计入需要处理数量
      —— 前端不靠本地减一掩盖这个事实；
    * view=history 统计历史视图里的真实终态，已确认的失败仍然在历史计数里如实存在。
    """
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
    current_items, history_items = _classify_views(merged)
    ordered = history_items if view == "history" else current_items
    # 计数与返回的视图同口径：当前视图里已「知道了」的失败不再占「需要处理」数量；
    # 历史视图仍如实统计它自己包含的真实终态。
    counts = _counts(ordered)
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
        # 被任务栏过滤掉的失败（§一：单次失败只留在执行记录里）不提供任务栏详情入口；
        # 执行记录本身仍走 /api/tool-records/{id} 完整可读，不删除、不改写成成功。
        if record is None or item is None:
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
