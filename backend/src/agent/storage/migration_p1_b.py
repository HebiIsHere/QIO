"""迁移 44（B）：滚动摘要覆盖位置 / 输入版本持久化。

只追加、不改历史迁移。Lead 整合时会把 MIGRATION 登记进
storage/schema.py 的 MIGRATIONS 并收敛 SCHEMA_VERSION；本分支不修改共享文件，
定向测试用本模块的 apply() 直接在测试库上应用。

列语义（与 agent.memory.rolling.ROLLING_STATE_KEYS 一致，测试会核对）：
* rolling_covered_message_id：最后一条已覆盖消息 id（覆盖游标，只前进）
* rolling_covered_count：已覆盖消息条数（游标之前的写入顺序计数）
* rolling_input_version：覆盖输入的稳定版本（覆盖消息 id 序列哈希）
* rolling_updated_at：最近一次推进游标的时间（UTC ISO 字符串）
"""

from __future__ import annotations

import re
import sqlite3

from agent.storage.db import transaction

# 迁移号 / SQL 的唯一来源（Lead 登记 schema.py 时直接引用这组语句）。
MIGRATION: tuple[int, list[str]] = (
    44,
    [
        "ALTER TABLE fragments ADD COLUMN rolling_covered_message_id TEXT",
        "ALTER TABLE fragments ADD COLUMN rolling_covered_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE fragments ADD COLUMN rolling_input_version TEXT",
        "ALTER TABLE fragments ADD COLUMN rolling_updated_at TEXT",
    ],
)

_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+fragments\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def columns() -> tuple[str, ...]:
    """迁移 44 追加的列名（供测试 / 诊断核对）。"""
    names: list[str] = []
    for statement in MIGRATION[1]:
        match = _ADD_COLUMN.match(statement)
        if match is None:  # pragma: no cover - 迁移语句写错时尽早暴露
            raise ValueError(f"无法解析迁移 44 的语句：{statement!r}")
        names.append(match.group(1))
    return tuple(names)


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 44（列已存在则跳过）；返回本次真正执行的语句。

    生产路径由 schema.py 的 MIGRATIONS 统一应用；这里只给「本分支内定向测试」
    和被单独引用的集成方使用，不写 schema_version（版本收敛由 Lead 负责）。
    """
    existing = {
        str(row["name"]) for row in conn.execute("PRAGMA table_info(fragments)")
    }
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            match = _ADD_COLUMN.match(statement)
            if match is not None and match.group(1) in existing:
                continue
            conn.execute(statement)
            executed.append(statement)
    return executed
