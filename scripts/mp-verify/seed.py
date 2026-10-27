"""为视觉/交互验收造「真实接口能读到」的数据（只写隔离数据目录）。

原则：
1. **不碰用户真实数据目录**：数据目录只从 --data-dir / QIO_DATA_DIR 取，且必须是显式给出的
   路径（不设默认值指向 %TEMP% 之外）。
2. 话题 / 知识 / 实体 / 凭据复用仓库既有脚本 scripts/baseline/seed_fixtures.py（同一份确定性数据，
   基线 vs 改版能逐像素对比）。
3. 待处理（整理提问）与整理记录写真实表 maintenance_batches / maintenance_items /
   maintenance_operations；实体更新候选写 entity_cards.field_meta.pending（A04 候选的真实形状）。
4. 造不出来的东西**如实记录**（见输出的 unavailable 列表），不伪造接口、不在产品代码里放演示数据。

用法：
    <python> scripts/mp-verify/seed.py --data-dir %TEMP%\qio-mp-verify\data\c --worktree D:\qio-dev\qio-mp-c [--reset]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PREFIX = "验收-"
BATCH_PREFIX = "mt_batch_verify_"
ITEM_PREFIX = "mt_item_verify_"
OP_PREFIX = "mt_op_verify_"


def now(offset_min: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_min)).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def run_base_seed(worktree: Path, data_dir: Path, python: Path, reset: bool) -> dict:
    """复用仓库既有的确定性数据脚本（话题 / 知识 / 实体 / 凭据）。"""
    script = worktree / "scripts" / "baseline" / "seed_fixtures.py"
    if not script.exists():
        return {"ok": False, "reason": f"缺少 {script}"}
    env = dict(os.environ)
    env["QIO_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(worktree / "backend" / "src")
    args = [str(python), str(script)]
    if reset:
        args.append("--reset")
    proc = subprocess.run(args, capture_output=True, text=True, env=env, cwd=str(worktree))
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip().splitlines()[-3:],
        "stderr": proc.stderr.strip().splitlines()[-6:] if proc.returncode else [],
    }


def connect(data_dir: Path, worktree: Path) -> sqlite3.Connection:
    sys.path.insert(0, str(worktree / "backend" / "src"))
    from agent.storage.migrate import apply_migrations

    conn = sqlite3.connect(data_dir / "app.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    apply_migrations(conn)
    return conn


def ensure_maintenance_tables(conn: sqlite3.Connection, worktree: Path) -> dict:
    """维护表迁移 47 在基线上还没接进 schema.py：验收库里按模块自带的 DDL 补上。

    这是**验收数据目录**里的补丁，不改任何产品代码与产品迁移顺序；
    返回是否本来就有 / 这次是否补上，写进输出供核对。
    """
    existing = {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    needed = ("maintenance_batches", "maintenance_items", "maintenance_operations")
    if all(name in existing for name in needed):
        return {"present": True, "applied": False}
    sys.path.insert(0, str(worktree / "backend" / "src"))
    try:
        from agent.storage import migration_p2_b

        executed = migration_p2_b.apply(conn)
        return {"present": True, "applied": bool(executed), "statements": len(executed)}
    except Exception as error:  # noqa: BLE001 - 迁移不可用就如实报告
        return {"present": False, "applied": False, "reason": f"{type(error).__name__}: {error}"}


def reset_verify_rows(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name LIKE ?", (PREFIX + "%",)
    ).fetchall():
        tid = row["id"]
        for frag in conn.execute("SELECT id FROM fragments WHERE topic_id=?", (tid,)).fetchall():
            conn.execute("DELETE FROM messages WHERE fragment_id=?", (frag["id"],))
            conn.execute("DELETE FROM memory_index WHERE fragment_id=?", (frag["id"],))
        conn.execute("DELETE FROM cursor WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM fragments WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM knowledge WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM entity_mentions WHERE topic_id=?", (tid,))
        conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (tid, tid))
        conn.execute("DELETE FROM nodes WHERE id=?", (tid,))
    conn.execute("DELETE FROM maintenance_operations WHERE id LIKE ?", (OP_PREFIX + "%",))
    conn.execute("DELETE FROM maintenance_items WHERE id LIKE ?", (ITEM_PREFIX + "%",))
    conn.execute("DELETE FROM maintenance_batches WHERE id LIKE ?", (BATCH_PREFIX + "%",))
    conn.commit()


def seed_maintenance(conn: sqlite3.Connection) -> dict:
    """一次真实形状的维护批次：已应用合并 / 已应用规范化 / 待回答提问 / 已撤回恢复。"""
    knowledge_rows = conn.execute(
        "SELECT id, content, chain_id, version FROM knowledge ORDER BY created_at DESC LIMIT 4"
    ).fetchall()
    knowledge_ids = [
        {"id": row["id"], "version": int(row["version"] or 1), "chain_id": row["chain_id"]}
        for row in knowledge_rows
    ]
    if not knowledge_ids:
        return {"ok": False, "reason": "库里没有知识条目，维护记录无法引用真实知识"}

    def ref(index: int) -> dict:
        row = knowledge_rows[index % len(knowledge_rows)]
        return {
            "id": row["id"],
            "version": int(row["version"] or 1),
            "content": row["content"],
            "category": "",
            "scope": [],
            "state": "active",
            "source": "knowledge_base",
        }

    batch_id = new_id(BATCH_PREFIX)
    created = now(-90)
    conn.execute(
        "INSERT INTO maintenance_batches (id, trigger_reason, snapshot, status, model, created_at, finished_at, stats, error)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            batch_id,
            "change_count",
            json.dumps({"knowledge_total": len(knowledge_rows)}, ensure_ascii=False),
            "partial",
            "verify-seed",
            created,
            now(-80),
            json.dumps({"merged": 1, "normalized": 1, "asked": 1, "failed": 0}, ensure_ascii=False),
            None,
        ),
    )
    items = [
        ("merge", "applied", [knowledge_ids[0]], [ref(0)], [ref(1)], None, None, None, -88),
        ("normalize", "applied", [knowledge_ids[1]], [ref(1)], [ref(1)], None, None, None, -86),
        (
            "ask",
            "awaiting_answer",
            [knowledge_ids[2]],
            [ref(2)],
            [ref(2)],
            "关于「复制失败提示」这条偏好，是否也要作用于导出失败？",
            None,
            None,
            -84,
        ),
        ("restore", "applied", [knowledge_ids[3]] if len(knowledge_ids) > 3 else [knowledge_ids[0]], [ref(3)], [ref(3)], None, None, None, -82),
    ]
    for index, (kind, status, kids, before, after, question, answer, error, offset) in enumerate(items):
        conn.execute(
            "INSERT INTO maintenance_items (id, batch_id, kind, status, knowledge_ids, before, after,"
            " question, answer, evidence_ids, reason, error, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(ITEM_PREFIX),
                batch_id,
                kind,
                status,
                json.dumps(kids, ensure_ascii=False),
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
                question,
                answer,
                json.dumps([], ensure_ascii=False),
                "验收数据：真实接口形状，用于渲染列表与详情",
                error,
                now(offset),
                now(offset),
            ),
        )
    applied_items = conn.execute(
        "SELECT id, kind FROM maintenance_items WHERE batch_id=? AND status='applied'", (batch_id,)
    ).fetchall()
    for row in applied_items:
        conn.execute(
            "INSERT INTO maintenance_operations (id, batch_id, item_id, action, target_ids, result_id,"
            " before, after, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(OP_PREFIX),
                batch_id,
                row["id"],
                str(row["kind"]),
                json.dumps([knowledge_ids[0]["id"]], ensure_ascii=False),
                knowledge_ids[0]["id"],
                json.dumps([ref(0)], ensure_ascii=False),
                json.dumps({"revised": True}, ensure_ascii=False),
                "applied",
                now(-87),
            ),
        )
    # 第二条批次：只有运行中的空批次，用来检验「没有事项的批次」也不崩
    second = new_id(BATCH_PREFIX)
    conn.execute(
        "INSERT INTO maintenance_batches (id, trigger_reason, snapshot, status, model, created_at, finished_at, stats, error)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (second, "waiting_turns", "{}", "running", "verify-seed", now(-30), None, "{}", None),
    )
    conn.commit()
    return {"ok": True, "batch_id": batch_id, "items": len(items), "batches": 2}


def seed_entity_candidates(conn: sqlite3.Connection) -> dict:
    """实体更新候选写进 entity_cards.field_meta.pending（A04 的真实形状）。"""
    rows = conn.execute(
        "SELECT id, name, aliases, summary, kind, revision, field_meta FROM entity_cards ORDER BY created_at LIMIT 3"
    ).fetchall()
    if not rows:
        return {"ok": False, "reason": "库里没有实体卡，实体候选无处挂载"}
    seeded = []
    for index, row in enumerate(rows):
        try:
            meta = json.loads(row["field_meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        for key, default in (
            ("fields", {}),
            ("attributes", {}),
            ("tombstones", {}),
            ("pending", []),
            ("resolved", []),
            ("card", {}),
            ("pending_relations", []),
        ):
            meta.setdefault(key, default)
        revision = int(row["revision"] or 0)
        field_name = "summary" if index == 0 else ("aliases" if index == 1 else "attributes.用途")
        value = (
            f"验收候选：{row['name']} 的摘要有一版更准确的说法（自动提炼，等待人工确认）。"
            if index == 0
            else (["验收别名"] if index == 1 else "验收用途")
        )
        meta["pending"] = [
            {
                "id": "pc_verify_" + str(index) + uuid.uuid4().hex[:6],
                "field": field_name,
                "value": value,
                "reason": "user_value",
                "base_revision": revision,
                "at": now(-20 + index),
            }
        ] + [item for item in meta["pending"] if isinstance(item, dict)]
        conn.execute(
            "UPDATE entity_cards SET field_meta=?, updated_at=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), now(), row["id"]),
        )
        seeded.append({"entity_id": row["id"], "field": field_name})
    conn.commit()
    return {"ok": True, "candidates": seeded}


def main() -> int:
    parser = argparse.ArgumentParser(description="为验收造真实形状的数据（只写隔离数据目录）")
    parser.add_argument("--data-dir", required=True, help="隔离数据目录（必填，绝不指向用户真实目录）")
    parser.add_argument("--worktree", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python", default=sys.executable, help="后端解释器（用于调用既有 seed 脚本）")
    parser.add_argument("--reset", action="store_true", help="先清掉上一轮验收数据")
    parser.add_argument("--no-base-seed", action="store_true", help="跳过既有 seed_fixtures.py")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    worktree = Path(args.worktree).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["QIO_DATA_DIR"] = str(data_dir)

    report: dict = {"data_dir": str(data_dir), "worktree": str(worktree), "unavailable": []}
    if not args.no_base_seed:
        report["base_seed"] = run_base_seed(worktree, data_dir, Path(args.python), args.reset)
        if not report["base_seed"]["ok"]:
            report["unavailable"].append("既有 seed_fixtures.py 未成功：话题/知识/实体可能缺失")

    conn = connect(data_dir, worktree)
    try:
        report["maintenance_tables"] = ensure_maintenance_tables(conn, worktree)
        if args.reset:
            reset_verify_rows(conn)
        if report["maintenance_tables"]["present"]:
            report["maintenance"] = seed_maintenance(conn)
            if not report["maintenance"].get("ok"):
                report["unavailable"].append("整理记录未造出：" + str(report["maintenance"].get("reason")))
        else:
            report["unavailable"].append(
                "维护记录表不可用（迁移 47 未接入）：整理记录场景未验证"
            )
            report["maintenance"] = {"ok": False}
        report["entity_candidates"] = seed_entity_candidates(conn)
        if not report["entity_candidates"].get("ok"):
            report["unavailable"].append(
                "实体更新候选未造出：" + str(report["entity_candidates"].get("reason"))
            )
        report["counts"] = {
            "topics": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='topic'").fetchone()[0],
            "knowledge": conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
            "knowledge_pending_review": conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE state='pending_review'"
            ).fetchone()[0],
            "entity_cards": conn.execute("SELECT COUNT(*) FROM entity_cards").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "maintenance_batches": (
                conn.execute("SELECT COUNT(*) FROM maintenance_batches").fetchone()[0]
                if report["maintenance_tables"]["present"]
                else None
            ),
            "maintenance_items": (
                conn.execute("SELECT COUNT(*) FROM maintenance_items").fetchone()[0]
                if report["maintenance_tables"]["present"]
                else None
            ),
        }
    finally:
        conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
