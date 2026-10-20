"""迁移 47（B）：独立后台维护的批次 / 事项 / 操作记录。

只追加、不改历史迁移。Lead 整合时会把 MIGRATION 登记进 storage/schema.py
并收敛 SCHEMA_VERSION = COMPENSATION_VERSION = 48；本分支不修改共享文件，
定向测试用本模块的 apply() 直接在测试库上应用。

表语义（契约 §12.4 / §12.7；状态字符串全部来自 agent.maintenance.models）：

* maintenance_batches：一次维护执行（触发原因、快照、状态、模型、统计、错误）；
* maintenance_items：候选 / 独立问题 / 恢复事项（前后内容、涉及知识版本、证据、
  状态、回答）；
* maintenance_operations：真正落地的知识修改（动作、目标、结果版本、前后快照、状态）。

历史必须完整可回放：恢复只新增版本行 + 记录操作，从不删除任何行。
"""

from __future__ import annotations

import re
import sqlite3

from agent.storage.db import transaction

#: 迁移号 / SQL 的唯一来源（Lead 登记 schema.py 时直接引用这组语句）。
MIGRATION: tuple[int, list[str]] = (
    47,
    [
        """
        CREATE TABLE IF NOT EXISTS maintenance_batches (
            id             TEXT PRIMARY KEY,
            trigger_reason TEXT NOT NULL,
            snapshot       TEXT NOT NULL DEFAULT '{}',
            status         TEXT NOT NULL,
            model          TEXT,
            created_at     TEXT NOT NULL,
            finished_at    TEXT,
            stats          TEXT NOT NULL DEFAULT '{}',
            error          TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_maintenance_batches_created"
        " ON maintenance_batches(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_batches_status"
        " ON maintenance_batches(status)",
        """
        CREATE TABLE IF NOT EXISTS maintenance_items (
            id            TEXT PRIMARY KEY,
            batch_id      TEXT REFERENCES maintenance_batches(id),
            kind          TEXT NOT NULL CHECK (kind IN
                          ('merge','normalize','ask','resolution','restore')),
            status        TEXT NOT NULL,
            knowledge_ids TEXT NOT NULL DEFAULT '[]',
            before        TEXT NOT NULL DEFAULT '[]',
            after         TEXT NOT NULL DEFAULT '[]',
            question      TEXT,
            answer        TEXT,
            evidence_ids  TEXT NOT NULL DEFAULT '[]',
            reason        TEXT,
            error         TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_maintenance_items_batch"
        " ON maintenance_items(batch_id)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_items_kind_status"
        " ON maintenance_items(kind, status)",
        """
        CREATE TABLE IF NOT EXISTS maintenance_operations (
            id         TEXT PRIMARY KEY,
            batch_id   TEXT REFERENCES maintenance_batches(id),
            item_id    TEXT REFERENCES maintenance_items(id),
            action     TEXT NOT NULL,
            target_ids TEXT NOT NULL DEFAULT '[]',
            result_id  TEXT,
            before     TEXT NOT NULL DEFAULT '[]',
            after      TEXT NOT NULL DEFAULT '{}',
            status     TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_maintenance_operations_batch"
        " ON maintenance_operations(batch_id)",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_operations_item"
        " ON maintenance_operations(item_id)",
    ],
)

_CREATE_TABLE = re.compile(
    r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.IGNORECASE | re.DOTALL,
)
#: CREATE TABLE 的列行里，这些不是列名（表级约束）。
_TABLE_CONSTRAINT_KEYWORDS = frozenset(
    {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}
)


def tables() -> tuple[str, ...]:
    """本迁移创建的表名（按语句顺序）。"""
    names: list[str] = []
    for statement in MIGRATION[1]:
        match = _CREATE_TABLE.match(statement)
        if match is not None:
            names.append(match.group(1))
    return tuple(names)


def columns() -> dict[str, tuple[str, ...]]:
    """本迁移建出的 表名 -> 列名（供测试 / 诊断核对；不读数据库）。"""
    out: dict[str, tuple[str, ...]] = {}
    for statement in MIGRATION[1]:
        match = _CREATE_TABLE.match(statement)
        if match is None:
            continue
        table = match.group(1)
        body = statement[match.end() :]
        # 去掉最外层的右括号（DDL 以右括号结尾）。
        body = body.rsplit(")", 1)[0]
        names: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            first = line.split()[0]
            if first.upper() in _TABLE_CONSTRAINT_KEYWORDS:
                continue
            names.append(first)
        out[table] = tuple(names)
    return out


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 47（表已存在则跳过 CREATE TABLE）；返回本次真正执行的语句。

    生产路径由 schema.py 的 MIGRATIONS 统一应用；这里只给「本分支内定向测试」
    和被单独引用的集成方使用，不写 schema_version（版本收敛由 Lead 负责）。
    """
    existing = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            match = _CREATE_TABLE.match(statement)
            if match is not None and match.group(1) in existing:
                continue
            conn.execute(statement)
            executed.append(statement)
    return executed
