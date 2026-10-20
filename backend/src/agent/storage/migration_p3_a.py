"""迁移 49（A）：提交—主循环 turn 绑定 / 等待事项 / 续作恢复。

契约：docs/qio-refactor-contract.md §13.3、§13.7。只追加、不改历史迁移；本分支
不修改 storage/schema.py，定向测试用本模块的 apply() 直接在测试库上应用，
Lead 整合时把 MIGRATION 登记进 schema.py 的 MIGRATIONS 并收敛版本（补偿 51）。

为什么需要这些对象（旧流程没有它们）：

* 互动提交以前对应一条 board_understanding 记录；新流程把「一次提交」作为主循环
  的一个 Turn 受理。要保证「提交 → 等待 → 用户下一条消息自动续作 → 重启恢复」，
  必须把提交与主循环 turn 的绑定居久化：board_submissions.turn_id 记最近一次绑定，
  board_submission_turns 记这一提交历次轮次（首轮 + 各次续作）与续作状态。
* QIO 在对话里提出的问题（board_wait_for_input 的参数）必须**持久保存**并跨重启
  恢复：board_waiting 一行一个待回答问题，status=waiting/resolved/cancelled；
  只有 QIO 通过 resolved_question_ids 明确解除才 resolve，回答不足不清除。

幂等性：ALTER TABLE 语句先查 PRAGMA table_info 再执行（列已存在就跳过，等价于
migrate.py 的半截迁移自愈）；建表 / 索引全部 IF NOT EXISTS，因此既可被迁移框架
重复应用，也可被补偿迁移 51 整体重放。
"""

from __future__ import annotations

import re
import sqlite3

from agent.storage.db import transaction

#: 迁移号 / SQL 的唯一来源（Lead 登记 schema.py 时直接引用这组语句）。
MIGRATION: tuple[int, list[str]] = (
    49,
    [
        # 提交与主循环 turn 的绑定（最近一次）：重启后仍能把提交接到它的对话轮次。
        "ALTER TABLE board_submissions ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''",
        # 续作状态（JSON）：原话题、最近绑定、续作次数等由程序写入，模型不可编造。
        "ALTER TABLE board_submissions ADD COLUMN resume_state TEXT NOT NULL DEFAULT '{}'",
        # 这一提交历次绑定的主循环轮次（首轮 + 每次自动续作）：审计 / 恢复依据。
        """
        CREATE TABLE IF NOT EXISTS board_submission_turns (
            submission_id TEXT NOT NULL,
            turn_id       TEXT NOT NULL,
            resume_state  TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL,
            PRIMARY KEY (submission_id, turn_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_board_submission_turns_turn"
        " ON board_submission_turns(turn_id)",
        # 等待事项：QIO 在对话里问出的问题必须持久保存（不用易超时的临时等待代替）。
        """
        CREATE TABLE IF NOT EXISTS board_waiting (
            id                    TEXT PRIMARY KEY,
            board_id              TEXT NOT NULL,
            submission_id         TEXT NOT NULL,
            turn_id               TEXT NOT NULL DEFAULT '',
            question              TEXT NOT NULL,
            missing_fields        TEXT NOT NULL DEFAULT '[]',
            status                TEXT NOT NULL DEFAULT 'waiting'
                                  CHECK (status IN ('waiting', 'resolved', 'cancelled')),
            resolved_question_ids TEXT NOT NULL DEFAULT '[]',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_board_waiting_board"
        " ON board_waiting(board_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_board_waiting_submission"
        " ON board_waiting(submission_id, status)",
    ],
)

#: 迁移 49 新增的列（表名 -> 列名），供测试 / Lead 的 REQUIRED_OBJECTS 核对。
COLUMNS: dict[str, tuple[str, ...]] = {
    "board_submissions": ("turn_id", "resume_state"),
}

#: 迁移 49 新增的表。
TABLES: tuple[str, ...] = ("board_submission_turns", "board_waiting")

_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 49；返回本次真正执行的语句（已存在的列 / 表跳过）。"""
    executed: list[str] = []
    with transaction(conn):
        present: dict[str, set[str]] = {}
        for statement in MIGRATION[1]:
            match = _ADD_COLUMN.match(statement)
            if match is not None:
                table, column = match.group(1), match.group(2)
                if table not in present:
                    present[table] = {
                        str(row["name"])
                        for row in conn.execute("PRAGMA table_info(" + table + ")")
                    }
                if column in present[table]:
                    continue
                conn.execute(statement)
                present[table].add(column)
            else:
                conn.execute(statement)
            executed.append(statement)
    return executed
