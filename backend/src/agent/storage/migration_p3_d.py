"""迁移 56（P3-D）：意图显示位置 / 单意图有效 Turn / 持久化重试系列。

只追加、不改历史迁移。契约：docs/interactive-page-v2-contract.md §6.1–§6.3。

* `board_intents.display`：IntentView.display = {x, y}，与目标/授权版本分开；
  旧数据为空（null），只影响预览与成果优先落点，不参与执行授权。
* `board_intent_turns`：同一意图同一时刻只有一个有效 Turn。
  确认 / 恢复 / 自动重试在提交主循环 Turn 前先认领；Turn 结束或失败后释放。
* `board_intent_retries`：持久化 retry 系列。首次失败不占自动次数；
  自动最多 3 次；手动一次点击一次尝试（manual_attempts 单独累计，不重置自动次数）；
  重启 / 重连不重置；耗尽或不可恢复时只记录一次最终说明时间。

所有语句幂等（表 / 索引 IF NOT EXISTS；ADD COLUMN 由 apply() 先查列），可被补偿 57 重放。
"""

from __future__ import annotations

import re
import sqlite3

from agent.storage.db import transaction

_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "display",
        "ALTER TABLE board_intents ADD COLUMN display TEXT NOT NULL DEFAULT '{}'",
    ),
)

_TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS board_intent_turns (
        intent_id     TEXT PRIMARY KEY REFERENCES board_intents(id),
        board_id      TEXT NOT NULL,
        submission_id TEXT NOT NULL DEFAULT '',
        turn_id       TEXT NOT NULL DEFAULT '',
        attempt       INTEGER NOT NULL DEFAULT 1,
        source        TEXT NOT NULL DEFAULT 'confirm',
        status        TEXT NOT NULL DEFAULT 'claiming',
        reason        TEXT NOT NULL DEFAULT '',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_board_intent_turns_turn"
    " ON board_intent_turns(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_board_intent_turns_board"
    " ON board_intent_turns(board_id, status)",
    """
    CREATE TABLE IF NOT EXISTS board_intent_retries (
        id                    TEXT PRIMARY KEY,
        series_id             TEXT NOT NULL UNIQUE,
        board_id              TEXT NOT NULL,
        submission_id         TEXT NOT NULL DEFAULT '',
        intent_id             TEXT NOT NULL REFERENCES board_intents(id),
        authorization_version INTEGER NOT NULL DEFAULT 0,
        attempts              INTEGER NOT NULL DEFAULT 0,
        manual_attempts       INTEGER NOT NULL DEFAULT 0,
        retry_limit           INTEGER NOT NULL DEFAULT 3,
        status                TEXT NOT NULL DEFAULT 'pending',
        last_error            TEXT NOT NULL DEFAULT '',
        next_action           TEXT NOT NULL DEFAULT 'auto',
        retryable             INTEGER NOT NULL DEFAULT 1,
        turn_id               TEXT NOT NULL DEFAULT '',
        final_notified_at     TEXT,
        exhausted_at          TEXT,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        UNIQUE (intent_id, authorization_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_board_intent_retries_board"
    " ON board_intent_retries(board_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_board_intent_retries_intent"
    " ON board_intent_retries(intent_id, created_at)",
)

MIGRATION: tuple[int, list[str]] = (
    56,
    [statement for _name, statement in _ADDED_COLUMNS] + list(_TABLE_DDL),
)

_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_CREATE_TABLE = re.compile(
    r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.IGNORECASE | re.DOTALL,
)


def added_columns() -> tuple[str, ...]:
    return tuple(name for name, _statement in _ADDED_COLUMNS)


def tables() -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for statement in _TABLE_DDL
        if (match := _CREATE_TABLE.match(statement)) is not None
    )


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 56；返回本次真正执行的语句（已存在的列跳过）。"""
    present = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(board_intents)").fetchall()
    }
    executed: list[str] = []
    with transaction(conn):
        for name, statement in _ADDED_COLUMNS:
            if name in present:
                continue
            conn.execute(statement)
            present.add(name)
            executed.append(statement)
        for statement in _TABLE_DDL:
            conn.execute(statement)
            executed.append(statement)
    return executed
