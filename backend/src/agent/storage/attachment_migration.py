"""旧附件数据迁移（契约 §9）：盘点 → 审计 → 幂等 / 断点续跑迁移。

背景：旧记录有两种形态（都产生在 R9 之前，source_kind 为 NULL）：

1. kind=copy：QIO 保存过独立副本（stored_path），source_path 可能还在；
2. kind=reference：只记了路径（source_path），没有副本。

R9 之后附件是**源文件引用**：读取/预览/写回都作用于源文件。迁移要做的事：

* 有有效原路径：核实对应关系后改为源引用；原文件与历史保存内容不同 →
  **保留差异事实**（写进 legacy_paths），不静默替换历史材料；
* 无原路径 / 原件失效、QIO 保存文件仍在：把现存文件登记为**当前源文件**
  （origin=user_saved_material），不删除、不再复制一份，并记录历史来源；
* 全部失效：保留 missing 状态与「重新指定位置」入口，不伪造恢复；
* 保留既有附件 id 与引用关系（message/card/preview 引用不变）。

纪律：

* 每次迁移逐行写审计 attachment_migration_audit(attachment_id, step, outcome, detail)，
  已完成的步骤在重跑时跳过（幂等 + 断点续跑）；
* 登记迁移与磁盘清理**分开**：首次迁移不搬动任何现存文件；被源引用取代的旧副本
  只登记到 material_cleanup_candidates（state=candidate），等批准后再进回收站；
* 只在本连接所在线程调用（与附件服务相同的 sqlite 线程纪律）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent.services.file_identity import content_sha256, identity_of
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

#: 迁移 52 追加的列（缺列的库如实报告"schema 未就绪"，不猜、不改历史迁移）
SOURCE_COLUMN_NAMES = (
    "source_kind",
    "origin",
    "file_id",
    "content_sha256",
    "reference_state",
    "material_enabled",
    "user_note",
    "last_verified_at",
    "legacy_paths",
)

#: 审计步骤（每行每步只保留最新事实）
STEP_INVENTORY = "inventory"
STEP_SOURCE_REF = "source_ref"
STEP_CLEANUP = "cleanup_candidate"
STEPS = (STEP_INVENTORY, STEP_SOURCE_REF, STEP_CLEANUP)

#: 迁移时计算/核对内容哈希的大小上限（迁移跑在事件循环线程，必须快；
#: 超过上限只核对 size/mtime 事实，并如实标注 hash 未核对）
HASH_MAX_BYTES = 8 * 1024 * 1024

#: 已完成、重跑时应跳过的 outcome
DONE_OUTCOMES = ("done", "missing", "skipped", "error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    names: set[str] = set()
    for row in rows:
        try:
            names.add(str(row["name"]))
        except (KeyError, IndexError):
            names.add(str(row[1]))
    return names


def schema_ready(conn: sqlite3.Connection) -> bool:
    """迁移 52 的列与审计表是否都在（不在就如实报告，不做半截迁移）。"""
    columns = _table_columns(conn, "attachments")
    if not all(name in columns for name in SOURCE_COLUMN_NAMES):
        return False
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    return "attachment_migration_audit" in tables and "material_cleanup_candidates" in tables


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def audit(
    conn: sqlite3.Connection,
    attachment_id: str,
    step: str,
    outcome: str,
    detail: dict | None = None,
) -> None:
    """写一行迁移审计（同 (attachment_id, step) 幂等覆盖为最新事实）。"""
    with transaction(conn):
        conn.execute(
            "INSERT INTO attachment_migration_audit"
            " (attachment_id, step, outcome, detail, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(attachment_id, step) DO UPDATE SET"
            " outcome = excluded.outcome, detail = excluded.detail,"
            " updated_at = excluded.updated_at",
            (
                str(attachment_id),
                str(step),
                str(outcome),
                json.dumps(detail or {}, ensure_ascii=False, separators=(",", ":")),
                _now(),
            ),
        )


def audit_outcome(conn: sqlite3.Connection, attachment_id: str, step: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT outcome FROM attachment_migration_audit"
            " WHERE attachment_id = ? AND step = ?",
            (str(attachment_id), str(step)),
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["outcome"])


def audit_rows(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT attachment_id, step, outcome, detail, updated_at"
            " FROM attachment_migration_audit ORDER BY attachment_id, step"
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        out.append(
            {
                "attachment_id": str(row["attachment_id"]),
                "step": str(row["step"]),
                "outcome": str(row["outcome"]),
                "detail": detail,
                "updated_at": str(row["updated_at"]),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------


def _managed_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / "attachments"


def _is_managed(path: Path, root: Path) -> bool:
    try:
        target = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return target == base or base in target.parents


def _card_attachment_ids(conn: sqlite3.Connection) -> set[str]:
    """互动卡片里出现过的 attachmentId（board_states.state JSON）。"""
    found: set[str] = set()
    try:
        rows = conn.execute("SELECT state FROM board_states").fetchall()
    except sqlite3.Error:
        return found
    for row in rows:
        try:
            state = json.loads(row["state"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        for card in state.get("cards") or []:
            if not isinstance(card, dict):
                continue
            meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
            block = meta.get("file") if isinstance(meta.get("file"), dict) else {}
            for key in ("attachmentId", "attachment_id"):
                value = block.get(key) or meta.get(key)
                if value:
                    found.add(str(value))
    return found


def inventory(conn: sqlite3.Connection, data_dir: Path | str) -> dict:
    """迁移前盘点：表、真实文件、消息/卡片/预览引用。只读，不改任何数据。"""
    root = _managed_root(data_dir)
    rows = conn.execute("SELECT * FROM attachments").fetchall()
    by_state: dict[str, int] = {}
    legacy = 0
    migrated = 0
    source_present = 0
    stored_present = 0
    both_missing = 0
    message_refs = 0
    referenced_files: set[str] = set()
    for row in rows:
        att = dict(row)
        state = str(att.get("state") or "")
        by_state[state] = by_state.get(state, 0) + 1
        if att.get("source_kind") is None:
            legacy += 1
        else:
            migrated += 1
        has_source = False
        source_path = str(att.get("source_path") or "")
        if source_path:
            try:
                source = Path(source_path)
                has_source = source.is_file()
                if has_source:
                    referenced_files.add(str(source.resolve()))
            except OSError:
                has_source = False
        has_stored = False
        stored_path = str(att.get("stored_path") or "")
        if stored_path:
            try:
                stored = Path(stored_path)
                has_stored = stored.is_file()
                if has_stored:
                    referenced_files.add(str(stored.resolve()))
            except OSError:
                has_stored = False
        source_present += int(has_source)
        stored_present += int(has_stored)
        if not has_source and not has_stored:
            both_missing += 1
        if att.get("message_id"):
            message_refs += 1
    disk_files: list[Path] = []
    if root.is_dir():
        disk_files = [item for item in root.rglob("*") if item.is_file()]
    orphan_files = []
    for item in disk_files:
        try:
            if str(item.resolve()) not in referenced_files:
                orphan_files.append(str(item))
        except OSError:
            orphan_files.append(str(item))
    try:
        preview_refs = int(
            conn.execute(
                "SELECT COUNT(DISTINCT attachment_id) FROM board_preview_state"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        preview_refs = 0
    return {
        "schema_ready": schema_ready(conn),
        "total": len(rows),
        "legacy_rows": legacy,
        "migrated_rows": migrated,
        "by_state": by_state,
        "source_present": source_present,
        "stored_present": stored_present,
        "both_missing": both_missing,
        "message_refs": message_refs,
        "card_refs": len(_card_attachment_ids(conn)),
        "preview_refs": preview_refs,
        "managed_copy_files": len(disk_files),
        "orphan_copy_files": len(orphan_files),
        "orphan_files_sample": orphan_files[:20],
        "data_dir": str(Path(data_dir)),
    }


# ---------------------------------------------------------------------------
# 单行迁移
# ---------------------------------------------------------------------------


def _load_facts(existing: str | None) -> list[dict]:
    try:
        items = json.loads(existing or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _append_fact(existing: str | None, entry: dict) -> str:
    items = _load_facts(existing)
    items.append(entry)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _update_source_fields(
    conn: sqlite3.Connection, attachment_id: str, fields: dict
) -> None:
    columns = _table_columns(conn, "attachments")
    usable = {name: value for name, value in fields.items() if name in columns}
    if not usable:
        return
    usable["updated_at"] = _now()
    assignments = ", ".join(f"{name} = ?" for name in usable)
    values = list(usable.values()) + [str(attachment_id)]
    with transaction(conn):
        conn.execute(
            f"UPDATE attachments SET {assignments} WHERE id = ?", tuple(values)
        )


def _cleanup_candidate(
    conn: sqlite3.Connection, attachment_id: str, path: str, reason: str
) -> None:
    """登记冗余材料清理候选（**不搬动、不删除**；等批准后再进回收站）。"""
    stamp = _now()
    with transaction(conn):
        conn.execute(
            "INSERT INTO material_cleanup_candidates"
            " (attachment_id, path, reason, state, created_at, updated_at)"
            " VALUES (?, ?, ?, 'candidate', ?, ?)"
            " ON CONFLICT(attachment_id) DO UPDATE SET"
            " path = excluded.path, reason = excluded.reason, updated_at = excluded.updated_at",
            (str(attachment_id), str(path), str(reason), stamp, stamp),
        )


def cleanup_candidates(conn: sqlite3.Connection) -> list[dict]:
    try:
        rows = conn.execute(
            "SELECT attachment_id, path, reason, state, created_at, updated_at"
            " FROM material_cleanup_candidates ORDER BY created_at, attachment_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def mark_cleanup_candidate_state(
    conn: sqlite3.Connection, attachment_id: str, state: str, detail: dict | None = None
) -> None:
    """回收执行后如实记录候选状态（recycled / failed）；失败保持可重试。"""
    stamp = _now()
    reason = str((detail or {}).get("reason") or "")
    try:
        with transaction(conn):
            if reason:
                conn.execute(
                    "UPDATE material_cleanup_candidates"
                    " SET state = ?, updated_at = ?, reason = ?"
                    " WHERE attachment_id = ?",
                    (str(state), stamp, reason, str(attachment_id)),
                )
            else:
                conn.execute(
                    "UPDATE material_cleanup_candidates"
                    " SET state = ?, updated_at = ? WHERE attachment_id = ?",
                    (str(state), stamp, str(attachment_id)),
                )
    except sqlite3.Error:
        logger.warning("mark cleanup candidate state failed", exc_info=True)


def _migrate_row(conn: sqlite3.Connection, row, data_dir: Path) -> str:
    """迁移一行；返回结果标签（幂等：已完成的步骤直接跳过）。"""
    att = dict(row)
    attachment_id = str(att.get("id") or "")
    if not attachment_id:
        return "skipped"
    audit(
        conn,
        attachment_id,
        STEP_INVENTORY,
        "done",
        {
            "kind": att.get("kind"),
            "state": att.get("state"),
            "source_path": att.get("source_path"),
            "stored_path": att.get("stored_path"),
        },
    )
    if att.get("source_kind") is not None:
        return "already_source_ref"
    previous = audit_outcome(conn, attachment_id, STEP_SOURCE_REF)
    if previous in DONE_OUTCOMES:
        return "skipped_resumed"

    old_source = str(att.get("source_path") or "").strip()
    old_stored = str(att.get("stored_path") or "").strip()
    old_sha = str(att.get("sha256") or "").strip() or None
    legacy_paths = att.get("legacy_paths")

    source_file: Path | None = None
    if old_source:
        candidate = Path(old_source)
        try:
            if candidate.is_file():
                source_file = candidate
        except OSError:
            source_file = None
    stored_file: Path | None = None
    if old_stored:
        candidate = Path(old_stored)
        try:
            if candidate.is_file() and _is_managed(candidate, _managed_root(data_dir)):
                stored_file = candidate
        except OSError:
            stored_file = None

    if source_file is not None:
        size = int(source_file.stat().st_size)
        identity = identity_of(source_file)
        digest = content_sha256(source_file) if size <= HASH_MAX_BYTES else None
        copy_sha = old_sha
        content_differs: bool | None = None
        if digest is not None and copy_sha:
            content_differs = digest != copy_sha
        relationship = "source_path_exists"
        if stored_file is not None:
            relationship = "source_and_copy_both_exist"
        facts = _append_fact(
            legacy_paths,
            {
                "event": "migrated_source_ref",
                "at": _now(),
                "mechanism": "legacy_source_path",
                "legacy_kind": att.get("kind"),
                "legacy_source_path": old_source,
                "legacy_stored_path": old_stored or None,
                "legacy_sha256": copy_sha,
                "content_differs_from_saved_copy": content_differs,
                "hash_computed": digest is not None,
                "relationship": relationship,
            },
        )
        fields = {
            "source_kind": "desktop_path",
            "origin": "user_original",
            "file_id": identity.key if identity is not None else None,
            "content_sha256": digest,
            "reference_state": "available",
            "last_verified_at": _now(),
            "legacy_paths": facts,
            "stored_path": None,
            "state": "ready",
            "error": None,
        }
        _update_source_fields(conn, attachment_id, fields)
        if stored_file is not None:
            _cleanup_candidate(
                conn,
                attachment_id,
                str(stored_file),
                "历史副本已被源文件引用取代；清理需用户批准后进系统回收站",
            )
            audit(
                conn,
                attachment_id,
                STEP_CLEANUP,
                "done",
                {"path": str(stored_file), "action": "candidate_only"},
            )
        audit(
            conn,
            attachment_id,
            STEP_SOURCE_REF,
            "done",
            {
                "outcome": "source_ref",
                "source_path": old_source,
                "content_differs": content_differs,
            },
        )
        return "migrated_source_ref"

    if stored_file is not None:
        size = int(stored_file.stat().st_size)
        identity = identity_of(stored_file)
        digest = content_sha256(stored_file) if size <= HASH_MAX_BYTES else old_sha
        facts = _append_fact(
            legacy_paths,
            {
                "event": "migrated_saved_material",
                "at": _now(),
                "mechanism": "saved_copy_as_source",
                "legacy_kind": att.get("kind"),
                "legacy_source_path": old_source or None,
                "legacy_stored_path": old_stored,
                "legacy_sha256": old_sha,
            },
        )
        fields = {
            "source_path": str(stored_file.absolute()),
            "stored_path": None,
            "source_kind": "legacy_copy",
            "origin": "user_saved_material",
            "file_id": identity.key if identity is not None else None,
            "content_sha256": digest,
            "sha256": digest,
            "size_bytes": size,
            "reference_state": "available",
            "last_verified_at": _now(),
            "legacy_paths": facts,
            "state": "ready",
            "error": None,
        }
        _update_source_fields(conn, attachment_id, fields)
        audit(
            conn,
            attachment_id,
            STEP_SOURCE_REF,
            "done",
            {
                "outcome": "saved_material",
                "source_path": str(stored_file),
                "legacy_source_path": old_source or None,
            },
        )
        return "migrated_saved_material"

    facts = _append_fact(
        legacy_paths,
        {
            "event": "migrated_missing",
            "at": _now(),
            "legacy_kind": att.get("kind"),
            "legacy_source_path": old_source or None,
            "legacy_stored_path": old_stored or None,
            "legacy_sha256": old_sha,
        },
    )
    fields = {
        "source_kind": "missing",
        "origin": att.get("origin") or "user_original",
        "reference_state": "missing",
        "legacy_paths": facts,
        "stored_path": None,
        "state": "missing",
        "error": "旧附件的位置和 QIO 保存的副本都已经不在了；可以重新指定位置",
        "last_verified_at": _now(),
    }
    _update_source_fields(conn, attachment_id, fields)
    audit(
        conn,
        attachment_id,
        STEP_SOURCE_REF,
        "missing",
        {
            "outcome": "missing",
            "legacy_source_path": old_source or None,
            "legacy_stored_path": old_stored or None,
        },
    )
    return "migrated_missing"


def migrate(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    *,
    apply: bool = True,
) -> dict:
    """迁移全部旧记录（幂等、可重复运行、可断点续跑）。

    apply=False 只盘点/计划，不写附件行（审计也不写），用于诊断。
    """
    report: dict = {
        "schema_ready": schema_ready(conn),
        "apply": bool(apply),
        "scanned": 0,
        "already_source_ref": 0,
        "migrated_source_ref": 0,
        "migrated_saved_material": 0,
        "migrated_missing": 0,
        "skipped_resumed": 0,
        "skipped": 0,
        "errors": [],
        "cleanup_candidates": 0,
    }
    if not report["schema_ready"]:
        report["errors"].append("数据库还没有迁移 52 的源引用列/审计表；未做任何迁移")
        return report
    rows = conn.execute("SELECT * FROM attachments ORDER BY created_at, id").fetchall()
    report["scanned"] = len(rows)
    for row in rows:
        try:
            if not apply:
                att = dict(row)
                if att.get("source_kind") is not None:
                    result = "already_source_ref"
                elif audit_outcome(conn, str(att.get("id")), STEP_SOURCE_REF) in DONE_OUTCOMES:
                    result = "skipped_resumed"
                elif str(att.get("source_path") or "") and Path(
                    str(att.get("source_path"))
                ).is_file():
                    result = "migrated_source_ref"
                elif str(att.get("stored_path") or "") and Path(
                    str(att.get("stored_path"))
                ).is_file():
                    result = "migrated_saved_material"
                else:
                    result = "migrated_missing"
            else:
                result = _migrate_row(conn, row, Path(data_dir))
            if result in report:
                report[result] += 1
        except Exception as exc:  # noqa: BLE001 - 单行失败不能拖垮整批（审计里留下事实）
            logger.warning("attachment migration failed: %s", exc, exc_info=True)
            report["errors"].append(
                {
                    "attachment_id": str(dict(row).get("id")),
                    "reason": type(exc).__name__ + ": " + str(exc),
                }
            )
    report["cleanup_candidates"] = len(cleanup_candidates(conn))
    return report


def pending_count(conn: sqlite3.Connection) -> int:
    """还没迁移的旧记录数（已审计完成的跳过；用于启动时决定是否跑）。"""
    if not schema_ready(conn):
        return 0
    try:
        rows = conn.execute(
            "SELECT id FROM attachments WHERE source_kind IS NULL"
        ).fetchall()
    except sqlite3.Error:
        return 0
    count = 0
    for row in rows:
        if audit_outcome(conn, str(row["id"]), STEP_SOURCE_REF) not in DONE_OUTCOMES:
            count += 1
    return count


def run_if_pending(conn: sqlite3.Connection, data_dir: Path | str) -> dict:
    """启动后台入口：有未迁移旧记录才跑（幂等，可重复调用）。"""
    pending = pending_count(conn)
    if pending <= 0:
        return {"pending": 0, "ran": False, "report": None}
    report = migrate(conn, data_dir)
    return {"pending": pending, "ran": True, "report": report}
