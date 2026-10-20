"""迁移 50（B）：意图版本 / 授权快照 / 权威操作账本。

只追加、不改历史迁移。Lead 整合时会把 MIGRATION 登记进 storage/schema.py 并追加
补偿迁移 51（48 全部 + 49 + 50），收敛 SCHEMA_VERSION = COMPENSATION_VERSION = 51；
本分支不修改共享文件，定向测试用 apply() 直接应用。

内容（契约 §13.4 / §13.7）：

* board_intents 增量列：client_ref / version / scope / authorization / outcome /
  attempt / finished_at（summary 在迁移 34/35 已存在；goal / expected / basis 在迁移 37
  已存在，不重复添加）；
* board_intent_versions：完整修改历史（每次实质更新 +1，不删旧版本）；
* board_operations：权威操作账本（operation_key 幂等 / 请求 hash / 前后快照 / 结果）。

所有语句幂等（ALTER 由 apply() 先查列；表 / 索引 IF NOT EXISTS），可被补偿 51 重放。
"""

from __future__ import annotations

import re
import sqlite3

from agent.storage.db import transaction

#: board_intents 新增列：列名 -> SQL（顺序即 MIGRATION 里的语句顺序）。
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("client_ref", "ALTER TABLE board_intents ADD COLUMN client_ref TEXT NOT NULL DEFAULT ''"),
    ("version", "ALTER TABLE board_intents ADD COLUMN version INTEGER NOT NULL DEFAULT 1"),
    ("scope", "ALTER TABLE board_intents ADD COLUMN scope TEXT NOT NULL DEFAULT '{}'"),
    (
        "authorization",
        "ALTER TABLE board_intents ADD COLUMN authorization TEXT NOT NULL DEFAULT '{}'",
    ),
    ("outcome", "ALTER TABLE board_intents ADD COLUMN outcome TEXT NOT NULL DEFAULT ''"),
    ("attempt", "ALTER TABLE board_intents ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
    ("finished_at", "ALTER TABLE board_intents ADD COLUMN finished_at TEXT"),
)

_TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS board_intent_versions (
        intent_id     TEXT NOT NULL REFERENCES board_intents(id),
        version       INTEGER NOT NULL,
        payload       TEXT NOT NULL DEFAULT '{}',
        authorization TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL,
        PRIMARY KEY (intent_id, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_board_intent_versions_intent"
    " ON board_intent_versions(intent_id, version)",
    """
    CREATE TABLE IF NOT EXISTS board_operations (
        id            TEXT PRIMARY KEY,
        board_id      TEXT NOT NULL,
        intent_id     TEXT NOT NULL REFERENCES board_intents(id),
        operation_key TEXT NOT NULL,
        request_hash  TEXT NOT NULL DEFAULT '',
        action        TEXT NOT NULL,
        status        TEXT NOT NULL,
        before        TEXT NOT NULL DEFAULT '{}',
        after         TEXT NOT NULL DEFAULT '{}',
        result        TEXT NOT NULL DEFAULT '{}',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (intent_id, operation_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_board_operations_board"
    " ON board_operations(board_id)",
    "CREATE INDEX IF NOT EXISTS idx_board_operations_intent"
    " ON board_operations(intent_id, status)",
)

MIGRATION: tuple[int, list[str]] = (
    50,
    [statement for _name, statement in _ADDED_COLUMNS] + list(_TABLE_DDL),
)

_CREATE_TABLE = re.compile(
    r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.IGNORECASE | re.DOTALL,
)


def added_columns() -> tuple[str, ...]:
    """board_intents 由本迁移追加的列名（供测试 / 诊断核对）。"""
    return tuple(name for name, _statement in _ADDED_COLUMNS)


def tables() -> tuple[str, ...]:
    """本迁移新建的表名。"""
    return tuple(
        match.group(1)
        for statement in _TABLE_DDL
        if (match := _CREATE_TABLE.match(statement)) is not None
    )


def columns() -> dict[str, tuple[str, ...]]:
    """新建表的 表名 -> 列名（不读数据库）。"""
    out: dict[str, tuple[str, ...]] = {}
    for statement in _TABLE_DDL:
        match = _CREATE_TABLE.match(statement)
        if match is None:
            continue
        body = statement[match.end() :].rsplit(")", 1)[0]
        names: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            first = line.split()[0]
            if first.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}:
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", first) is None:
                continue
            names.append(first)
        out[match.group(1)] = tuple(names)
    return out


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 50；返回本次真正执行的语句。不写 schema_version（Lead 收敛）。"""
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(board_intents)").fetchall()
    }
    executed: list[str] = []
    with transaction(conn):
        for name, statement in _ADDED_COLUMNS:
            if name in existing:
                continue
            conn.execute(statement)
            executed.append(statement)
        for statement in _TABLE_DDL:
            conn.execute(statement)
            executed.append(statement)
    return executed
