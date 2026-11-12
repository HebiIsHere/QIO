"""验收专用：在隔离数据目录里造两个可预测的维护批次。

为什么要自己造：seed.py 的批次目标知识可能是 pending_review/draft（产品数据排序会变），
预览可能全部 blocked，既验证不了「部分恢复」，也验证不了「完整撤回后只看历史」。
这里只用**当前 active 知识**造：
- partial 批次：1 个可恢复 normalize + 1 个必然 blocked 的 merge（来源仍是有效知识）
- full 批次：1 个可恢复 normalize
两个批次用两条不同的知识，互不影响。

只写隔离验收数据目录（必须显式 --data-dir），不改产品代码与迁移。
用法：<python> make_rollback_batch.py --data-dir <dir> --worktree <dir>
输出 JSON：{ok, partial_batch_id, full_batch_id, partial_knowledge, full_knowledge}
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now(offset_min: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_min)).isoformat()


def snapshot(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "version": int(row["version"] or 1),
        "content": row["content"],
        "category": row["category"] or "",
        "scope": [],
        "state": "active",
        "source": "knowledge_base",
    }


def insert_batch(conn: sqlite3.Connection, status: str, stats: dict) -> tuple[str, str]:
    batch_id = "mt_batch_verify_case_" + uuid.uuid4().hex[:10]
    created = now(2)
    conn.execute(
        "INSERT INTO maintenance_batches (id, trigger_reason, snapshot, status, model, created_at, finished_at, stats, error)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (batch_id, "change_count", json.dumps(stats), status, "verify-seed", created, now(3),
         json.dumps(stats), None),
    )
    return batch_id, created


def insert_operation(
    conn: sqlite3.Connection, *, batch_id: str, action: str, snap: dict, created: str
) -> None:
    item_id = "mt_item_verify_" + uuid.uuid4().hex[:10]
    operation_id = "mt_op_verify_" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO maintenance_items (id, batch_id, kind, status, knowledge_ids, before, after,"
        " question, answer, evidence_ids, reason, error, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, batch_id, action, "applied",
         json.dumps([{"id": snap["id"], "version": snap["version"]}], ensure_ascii=False),
         json.dumps([snap], ensure_ascii=False), json.dumps([snap], ensure_ascii=False),
         None, None, json.dumps([], ensure_ascii=False),
         "验收数据：可预测的维护批次", None, created, created),
    )
    conn.execute(
        "INSERT INTO maintenance_operations (id, batch_id, item_id, action, target_ids, result_id,"
        " before, after, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (operation_id, batch_id, item_id, action,
         json.dumps([snap["id"]], ensure_ascii=False), snap["id"],
         json.dumps([snap], ensure_ascii=False), json.dumps({"revised": True}, ensure_ascii=False),
         "applied", created),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--worktree", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    worktree = Path(args.worktree).resolve()
    sys.path.insert(0, str(worktree / "backend" / "src"))
    from agent.storage.migrate import apply_migrations

    conn = sqlite3.connect(data_dir / "app.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    apply_migrations(conn)
    try:
        rows = conn.execute(
            "SELECT id, version, content, category FROM knowledge "
            "WHERE state='active' ORDER BY updated_at ASC LIMIT 6"
        ).fetchall()
        if len(rows) < 2:
            print(json.dumps({"ok": False, "reason": "active 知识不足 2 条"}, ensure_ascii=False))
            return 1
        partial_snap = snapshot(rows[0])
        full_snap = snapshot(rows[1])

        partial_id, created = insert_batch(conn, "partial", {"merged": 1, "normalized": 1})
        # merge 先执行：来源仍是有效知识 → blocked；normalize 随后恢复成功 → 批次 rollback_partial
        insert_operation(conn, batch_id=partial_id, action="merge", snap=partial_snap, created=created)
        insert_operation(conn, batch_id=partial_id, action="normalize", snap=partial_snap, created=now(2))

        full_id, created_full = insert_batch(conn, "partial", {"normalized": 1})
        insert_operation(conn, batch_id=full_id, action="normalize", snap=full_snap, created=created_full)

        conn.commit()
        print(json.dumps({
            "ok": True,
            "partial_batch_id": partial_id,
            "full_batch_id": full_id,
            "partial_knowledge": partial_snap["content"],
            "full_knowledge": full_snap["content"],
        }, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())