"""迁移 81（R15-D / R4）：逻辑答案执行权表 + 恢复记录执行权列（契约 §6.3）。

只追加、不改历史迁移。`storage/schema.py` 把它登记进 `MIGRATIONS`（81），
补偿入口 82 重放 80 的全部语句 + 本模块的建对象语句；历史迁移一条不动。
`migrate.REQUIRED_OBJECTS` 同步登记新表 / 新列 / 新索引。

为什么需要它（契约 §6.1）
-------------------------
旧实现按 **recovery record** 领取租约：同一原问题留下两条恢复记录（answer_key 相同）
时，两条记录各自领到租约 -> 各调一次模型；活任务超过固定 600s 租约也会被再次接管。
修复不是延长超时，而是把执行权收口到**逻辑答案身份**：

表语义（topic_answer_runs）
--------------------------
* 一行 = 一个逻辑答案（answer_key = sha256(turn_id + ":" + user_message_id)）；
  同一 answer_key 的多个恢复记录共享同一个生成执行权，不能分别调用模型；
* `generation`：执行世代。每次领取 / 接管 +1；**写入权 = (answer_key, generation,
  owner)**。旧执行者的迟到暂存 / 保存 / 失败更新 / 释放一律 CAS 失败即放弃写入；
* `owner`：执行权令牌（`<instance_id>|<random>`）；`owner_instance` 供
  `InstanceRegistry.owner_alive` 判定旧实例是否**确认已退出**（True 不接管、
  False 可接管、None 不接管）；
* `claim_until`：租约到期时间（ISO UTC）。模型调用期间由心跳按
  `answer_key + generation + owner` 条件续租，**不持有 SQLite 写事务**、
  不做全局锁（不同 answer_key 可并行）；
* `status`：pending / running / answered（读侧按字符串兼容未来状态）；
* `record_id` / `answer_id`：当前领取者绑定的恢复记录 / 最终答案行。

topic_recovery_records 追加列
------------------------------
* `execution_generation` / `execution_owner`：这条记录当前绑定的执行权；
  记录侧写操作（暂存 / 收口 / 失败更新 / 释放）与 run 行同代次同 owner 才允许写；
* `last_heartbeat_at`：最近一次心跳续租时间（诊断 / 清单用）。

迁移纪律：每条迁移在 `apply_migrations` 的一个事务里执行（语句 + schema_version
原子落盘）。本模块的 `apply()` 只给定向测试 / 独立集成方使用，不写 schema_version。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import transaction

TABLE = "topic_answer_runs"

TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS topic_answer_runs (
        answer_key    TEXT PRIMARY KEY,
        status        TEXT NOT NULL DEFAULT 'pending',
        generation    INTEGER NOT NULL DEFAULT 0,
        owner         TEXT,
        owner_instance TEXT,
        claim_until   TEXT,
        record_id     TEXT,
        answer_id     TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )
    """,
    # 按 (status, claim_until) 找可接管 / 处理中的执行权。
    "CREATE INDEX IF NOT EXISTS idx_topic_answer_runs_status "
    "ON topic_answer_runs(status, claim_until)",
    # 按实例查它手上还有哪些执行权（诊断 / 失效实例收口）。
    "CREATE INDEX IF NOT EXISTS idx_topic_answer_runs_owner "
    "ON topic_answer_runs(owner_instance) WHERE owner_instance IS NOT NULL",
]

RECORD_DDL: list[str] = [
    # 记录侧执行权令牌：与 topic_answer_runs 的 generation/owner 同时校验。
    "ALTER TABLE topic_recovery_records ADD COLUMN execution_generation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE topic_recovery_records ADD COLUMN execution_owner TEXT",
    "ALTER TABLE topic_recovery_records ADD COLUMN last_heartbeat_at TEXT",
]

#: 迁移 81：执行权表 + 执行权索引 + 恢复记录追加列。
MIGRATION: tuple[int, list[str]] = (81, list(TABLE_DDL) + list(RECORD_DDL))

#: 补偿 82 需要重放的「建对象语句」（纯 CREATE / ALTER，不改数据）。
COMPENSATION_DDL: list[str] = list(TABLE_DDL) + list(RECORD_DDL)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 81（对象已在 -> 空列表）。生产路径由 schema.py 统一应用。"""
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            if " ADD COLUMN " in statement.upper():
                table = statement.split()[2]
                column = statement.split()[5]
                if _column_exists(conn, table, column):
                    continue
            conn.execute(statement)
            executed.append(statement)
    return executed


__all__ = [
    "TABLE",
    "TABLE_DDL",
    "RECORD_DDL",
    "MIGRATION",
    "COMPENSATION_DDL",
    "apply",
]
