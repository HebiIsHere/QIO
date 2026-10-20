"""迁移 46（A）：变化日志 / 调度状态 / 用户 turn 去重。

契约：docs/qio-refactor-contract.md §12.3、§12.7。只追加、不改历史迁移；本分支
不修改 storage/schema.py，定向测试用本模块的 apply() 直接在测试库上应用，
Lead 整合时把 MIGRATION 登记进 schema.py 的 MIGRATIONS 并收敛版本。

三张表各自解决一件事（语义与 maintenance/changes.py、scheduler.py 严格一致）：

* knowledge_change_log：正式生效知识的新增 / 实质修改。seq 是处理位置
  （调度器只推进 last_processed_seq，期间新增变化自然留在后面）；唯一键
  (chain_id, version) 保证同一版本只记一次（B 的重复调用幂等）。
  consumed_batch_seq 只在成功后由调度器回填，审计用，不参与「待处理」判定。
* knowledge_maintenance_state：单行（id = 1）的调度状态。等待轮次 turn_seq
  只在**存在待处理变化**时随真实成功用户 turn 前进；running / run_started_at
  是单轮互斥与中断回收的持久化依据（进程重启后仍然有效）。
* knowledge_maintenance_turn_log：真实成功用户 turn 的去重记录（turn_id 主键）。
  通知轮 / 失败 / 取消轮由接线的 Lead 保证不进来，本模块只按 turn_id 去重。

所有语句都是 CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE，因此既可被迁移框架
重复应用（半截迁移自愈），也可被补偿迁移 48 整体重放。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import transaction

#: 迁移号 / SQL 的唯一来源（Lead 登记 schema.py 时直接引用这组语句）。
MIGRATION: tuple[int, list[str]] = (
    46,
    [
        # 变化日志：seq 自增即处理顺序（SQLite AUTOINCREMENT 保证不回退、不复用）。
        """
        CREATE TABLE IF NOT EXISTS knowledge_change_log (
            seq                INTEGER PRIMARY KEY AUTOINCREMENT,
            knowledge_id       TEXT NOT NULL,
            chain_id           TEXT NOT NULL,
            version            INTEGER NOT NULL,
            source             TEXT NOT NULL DEFAULT '',
            occurred_at        TEXT NOT NULL,
            created_turn_seq   INTEGER NOT NULL DEFAULT 0,
            consumed_batch_seq INTEGER,
            UNIQUE (chain_id, version)
        )
        """,
        # 调度状态：单行。updated_at 给一个表达式默认值，保证任何插入路径都不会
        # 留下「没有时间」的状态行（幂等 INSERT OR IGNORE 子句见下一条）。
        """
        CREATE TABLE IF NOT EXISTS knowledge_maintenance_state (
            id                       INTEGER PRIMARY KEY CHECK (id = 1),
            turn_seq                 INTEGER NOT NULL DEFAULT 0,
            last_processed_seq       INTEGER NOT NULL DEFAULT 0,
            batch_seq                INTEGER NOT NULL DEFAULT 0,
            running                  INTEGER NOT NULL DEFAULT 0
                                     CHECK (running IN (0, 1)),
            run_started_at           TEXT,
            failed_attempts          INTEGER NOT NULL DEFAULT 0,
            retry_blocked_until_turn INTEGER NOT NULL DEFAULT 0,
            last_error               TEXT,
            last_run_at              TEXT,
            updated_at               TEXT NOT NULL
                                     DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """,
        # 真实成功用户 turn 去重：一行一个 turn，重复调用 INSERT OR IGNORE 不计数。
        """
        CREATE TABLE IF NOT EXISTS knowledge_maintenance_turn_log (
            turn_id    TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """,
        # 状态单行必须存在：调度器所有读写都以这一行为前提（幂等，可重复应用）。
        """
        INSERT OR IGNORE INTO knowledge_maintenance_state (id, updated_at)
        VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
    ],
)

#: 迁移 46 建的表（供测试 / 诊断核对；与 MIGRATION 语句逐项对应）。
TABLES: tuple[str, ...] = (
    "knowledge_change_log",
    "knowledge_maintenance_state",
    "knowledge_maintenance_turn_log",
)


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 46；返回本次执行的语句。

    全部语句本身可重复执行（IF NOT EXISTS / INSERT OR IGNORE），因此不需要像
    migration_p1_b 那样先查列是否存在；也不写 schema_version（版本收敛由
    Lead 在整合时负责）。
    """
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            conn.execute(statement)
            executed.append(statement)
    return executed
