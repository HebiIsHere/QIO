# -*- coding: utf-8 -*-
"""V 组反例证据（B01）：版本已记到 29、但本轮对象缺失 → 后端起不来。

现场（基线 `da0436b`）：`apply_migrations` 只看版本号；29 已记录 → 整段跳过 →
`instances` / `record_owners` / 一堆列与索引都不存在 →
`InstanceRegistry.start()` 直接 `no such table: instances`。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from agent.storage.db import connect, transaction  # noqa: E402
from agent.storage.migrate import apply_migrations, current_version  # noqa: E402
from agent.storage.schema import MIGRATIONS  # noqa: E402

NOW = "2026-10-10T00:00:00+00:00"

TABLES = ("instances", "record_owners")
COLUMNS = {
    "turn_journal": ("owner_instance_id",),
    "derived_tasks": ("owner_instance_id", "claim_generation"),
    "pending_approvals": ("owner_instance_id",),
    "knowledge": ("chain_id", "version"),
    "entity_cards": ("revision", "field_meta"),
}
INDEXES = (
    "idx_instances_heartbeat",
    "idx_record_owners_instance",
    "idx_turn_journal_owner",
    "idx_knowledge_chain",
)


def build_through(conn, target: int) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version, statements in MIGRATIONS:
        if version > target:
            continue
        with transaction(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, NOW)
            )


def missing(conn) -> list[str]:
    tables = {str(r["name"]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    out: list[str] = []
    for table in TABLES:
        if table not in tables:
            out.append(f"table:{table}")
    for table, columns in COLUMNS.items():
        have = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column in columns:
            if column not in have:
                out.append(f"column:{table}.{column}")
    indexes = {str(r["name"]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    for index in INDEXES:
        if index not in indexes:
            out.append(f"index:{index}")
    return out


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-b01-"))
    conn = connect(tmp / "old.db")
    build_through(conn, 25)
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES ('msg_v1', NULL, 'user', '这是一条不能丢的历史消息', ?)", (NOW,)
    )
    for version in (26, 27, 28, 29):
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, NOW)
        )

    print("[构造] 版本记录 =", current_version(conn), "（29 已记录）")
    print("[基线观察] 迁移前缺失对象 =", missing(conn))

    new_version = apply_migrations(conn)
    print("[基线观察] apply_migrations 返回 =", new_version)
    print("[基线观察] 迁移后缺失对象 =", missing(conn))

    from agent.storage.instance_registry import InstanceRegistry  # noqa: E402

    try:
        InstanceRegistry(conn, "qio_repro", pid=os.getpid()).start()
        print("[基线观察] InstanceRegistry.start() = 成功")
    except sqlite3.OperationalError as exc:
        print("[基线观察] InstanceRegistry.start() 抛 =", type(exc).__name__, exc)

    count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    print("[基线观察] 用户数据仍在 =", count, "条 messages")
    print("[结论] 版本号说「已经到 29」，对象却一个都不在，后端起不来。")
    conn.close()


if __name__ == "__main__":
    main()
