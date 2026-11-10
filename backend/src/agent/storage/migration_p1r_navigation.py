"""迁移 77（P1R-C）：单调导航版本表（P1-04 契约 §5）。

只追加、不改历史迁移。`storage/schema.py` 把它登记进 `MIGRATIONS`（77），
补偿入口 78 重放本表的建表语句（表丢了能补回来）。定向测试可用本模块的
`apply()` 独立应用（不写 schema_version，版本收敛由 schema.py 负责）。

表语义
------
* 单行（id = 1）：一个数据库只有一条全局「对话位置」版本；
* version：单调递增整数，只在真实导航位置变化时推进（见 services/navigation_version.py）；
* source / topic_id / fragment_id / turn_id / updated_at：最近一次推进的真实来源事实，
  只用于诊断、事件与恢复记录，不参与版本比较。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import transaction

TABLE = "navigation_version"

#: 建表语句（补偿入口 78 复用的**唯一**来源）。
TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS navigation_version (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        version     INTEGER NOT NULL DEFAULT 0,
        source      TEXT NOT NULL DEFAULT '',
        topic_id    TEXT,
        fragment_id TEXT,
        turn_id     TEXT,
        updated_at  TEXT NOT NULL DEFAULT ''
    )
    """,
]

#: 迁移 77：建表 + 种入初始行（版本从 0 开始）。
MIGRATION: tuple[int, list[str]] = (
    77,
    list(TABLE_DDL)
    + [
        "INSERT OR IGNORE INTO navigation_version (id, version, source, updated_at) "
        "VALUES (1, 0, '', '')",
    ],
)


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 77；返回本次真正执行的语句（表已经在 → 空列表）。

    生产路径由 schema.py 的 MIGRATIONS 统一应用；这里只给定向测试和被单独
    引用的集成方使用，不写 schema_version。
    """
    if _table_exists(conn):
        return []
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            conn.execute(statement)
            executed.append(statement)
    return executed


__all__ = ["TABLE", "TABLE_DDL", "MIGRATION", "apply"]
