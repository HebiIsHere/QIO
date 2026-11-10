"""迁移 83（R16-A）：完整位置状态 + 导航命令身份 + 逻辑答案暂存列（契约 §3/§4/§5/§6/§14）。

只追加、不改历史迁移。storage/schema.py 把它登记进 MIGRATIONS（83），
补偿入口 84 重放 82 的全部语句 + 本模块的**建对象语句**；历史迁移一条不动。
migrate.REQUIRED_OBJECTS 同步登记新表 / 新列 / 新索引。

表语义
------
position_state（单行 id=1）
* position_version：完整位置状态版本。**任何真实位置状态变化**推进一次
  （话题/片段/historic/pending/open_fragment_id 变化）；重放、同值、渲染不推进。
* db_identity：数据库身份（settings['db.instance_id'] 的副本）。只有启动期
  PositionStateService.ensure_identity() 的权威重建流程可以在换库时把版本归零并采用新身份；
  实时事件 / 命令不得自行切换身份。
* last_source/last_turn_id/last_operation_id/last_nav_version/updated_at：最近一次推进的
  因果事实，只用于诊断 / 事件 / 恢复，不参与版本比较。

navigation_commands
* 一行 = 一次导航命令身份的结果事实。command_id == identity_key（确定性、跨重启可复现）。
* identity_key 唯一：request:<id> / operation:<opID>:<pv> / internal:<kind>:<scope>。
  request/operation 行永久保留（低频、保证旧 ID 不会被当成新请求重新执行；本轮不做清理）。
* param_digest：实际执行参数的精确规范摘要；同身份不同摘要 = 冲突（零副作用、不新增行）。
* position_json：命令结果时的 position wire 快照；replay 时与**当前**只读 position 组合返回。
* status：applied / rejected / superseded / conflict。

topic_answer_runs 追加列（契约 §6，D 只读消费；D 不改 schema）
* phase：waiting / preparing / generating / staged / answered / failed；
* staged_answer / staged_at / staged_model：共享暂存结果的唯一权威（按 answer_key）。

迁移纪律：每条迁移在 apply_migrations 的一个事务里执行（语句 + schema_version 原子落盘）。
本模块的 apply() 只给定向测试 / 独立集成方使用，不写 schema_version。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import transaction

POSITION_STATE_TABLE = "position_state"
NAVIGATION_COMMANDS_TABLE = "navigation_commands"

POSITION_STATE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS position_state (
        id                INTEGER PRIMARY KEY CHECK (id = 1),
        position_version  INTEGER NOT NULL DEFAULT 0,
        db_identity       TEXT NOT NULL DEFAULT '',
        last_source       TEXT NOT NULL DEFAULT '',
        last_turn_id      TEXT,
        last_operation_id TEXT,
        last_nav_version  INTEGER,
        updated_at        TEXT NOT NULL DEFAULT ''
    )
    """,
]

POSITION_STATE_SEED: list[str] = [
    "INSERT OR IGNORE INTO position_state "
    "(id, position_version, db_identity, last_source, last_turn_id, last_operation_id, "
    " last_nav_version, updated_at) "
    "VALUES (1, 0, '', '', NULL, NULL, NULL, '')",
]

NAVIGATION_COMMANDS_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS navigation_commands (
        command_id       TEXT PRIMARY KEY,
        identity_key     TEXT NOT NULL,
        identity_kind    TEXT NOT NULL,
        command_kind     TEXT NOT NULL,
        param_digest     TEXT NOT NULL,
        request_id       TEXT,
        operation_id     TEXT,
        param_version    INTEGER,
        status           TEXT NOT NULL,
        reason           TEXT NOT NULL DEFAULT '',
        result_json      TEXT NOT NULL DEFAULT '{}',
        position_json    TEXT NOT NULL DEFAULT '{}',
        nav_version      INTEGER,
        position_version INTEGER,
        initiator        TEXT NOT NULL,
        turn_id          TEXT,
        approval_id      TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_commands_identity "
    "ON navigation_commands(identity_key)",
    "CREATE INDEX IF NOT EXISTS idx_navigation_commands_created "
    "ON navigation_commands(created_at)",
]

TOPIC_ANSWER_RUN_DDL: list[str] = [
    "ALTER TABLE topic_answer_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'waiting'",
    "ALTER TABLE topic_answer_runs ADD COLUMN staged_answer TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE topic_answer_runs ADD COLUMN staged_at TEXT",
    "ALTER TABLE topic_answer_runs ADD COLUMN staged_model TEXT",
]

#: 迁移 83 内的数据语句（只在本迁移执行一次；补偿入口不重放数据）。
TOPIC_ANSWER_RUN_BACKFILL: list[str] = [
    # 契约 §14.6：旧执行行按既有 status 事实回填 phase；只追加迁移内执行，不改历史迁移。
    "UPDATE topic_answer_runs SET phase = CASE status "
    "WHEN 'answered' THEN 'answered' ELSE 'waiting' END WHERE phase = 'waiting'",
]

#: 迁移 83：建表 + 种子行 + 导航命令表/索引 + 逻辑答案暂存列 + phase 回填。
MIGRATION: tuple[int, list[str]] = (
    83,
    list(POSITION_STATE_DDL)
    + list(POSITION_STATE_SEED)
    + list(NAVIGATION_COMMANDS_DDL)
    + list(TOPIC_ANSWER_RUN_DDL)
    + list(TOPIC_ANSWER_RUN_BACKFILL),
)

#: 补偿 84 需要重放的「建对象语句」（纯 CREATE / ALTER，不改数据）。
COMPENSATION_DDL: list[str] = (
    list(POSITION_STATE_DDL) + list(NAVIGATION_COMMANDS_DDL) + list(TOPIC_ANSWER_RUN_DDL)
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 83（对象已在 -> 对应语句跳过）；返回本次真正执行的语句。

    生产路径由 schema.py 统一应用；这里只给定向测试和被单独引用的集成方使用，
    不写 schema_version。
    """
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            upper = " ".join(str(statement).split()).upper()
            if upper.startswith("ALTER TABLE") and " ADD COLUMN " in upper:
                parts = str(statement).split()
                table = parts[2]
                column = parts[5]
                if _column_exists(conn, table, column):
                    continue
            conn.execute(statement)
            executed.append(statement)
    return executed


__all__ = [
    "POSITION_STATE_TABLE",
    "NAVIGATION_COMMANDS_TABLE",
    "POSITION_STATE_DDL",
    "NAVIGATION_COMMANDS_DDL",
    "TOPIC_ANSWER_RUN_DDL",
    "MIGRATION",
    "COMPENSATION_DDL",
    "apply",
]
