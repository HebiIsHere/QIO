"""迁移 79（R14-B / F03+F04+F13）：恢复记录持久化 + 唯一答案身份 + 迟到答案标记。

只追加、不改历史迁移。`storage/schema.py` 把它登记进 `MIGRATIONS`（79），
补偿入口 80 重放本模块的全部建对象语句（表/列/索引丢了能补回来）。

表语义（topic_recovery_records）
--------------------------------
* 一次「话题操作已经发生、但回答没有完成」的可恢复事项**持久化**成一行。
  旧实现是 app 内存 dict，进程重启即丢（刷新/断连/TURN_END 后用户再也看不到）；
* `status`：pending / answered（以及未来可扩展的失败态，读侧按字符串兼容）；
* `answer_key`：唯一逻辑答案身份 = sha256(turn_id + ":" + user_message_id)；
* `answer_text`：生成成功但保存失败时的**暂存正文**，后续重试只补存、不重复生成；
* `claim_owner` / `claim_until` / `attempt_count`：原子领取 + 租约。
  模型调用期间不持有 SQLite 写事务（领取和保存各是一个短事务）；
* `fragment_id`：失败时刻原用户消息所在的固定片段。恢复答案必须写回它，
  不得写当前开放片段（F13）。

messages 追加列（迟到答案）
---------------------------
* `answer_key`：同一逻辑答案只能有一行（部分唯一索引，NULL 不参与）。
  多个恢复记录 / 并发请求最终只产生一份正式答案；
* `late_answer_of_message_id`：这行回答是「原用户消息」的迟到答案（普通消息为 NULL）。
  迟到答案仍是普通 messages 行：历史加载/检索/导出/删除清理天然统一，不存在第二份正文。

迁移纪律：每条迁移在 `apply_migrations` 的一个事务里执行（语句 + schema_version 原子落盘）。
本模块的 `apply()` 只给定向测试/独立集成方使用，不写 schema_version。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import transaction

TABLE = "topic_recovery_records"

TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS topic_recovery_records (
        record_id          TEXT PRIMARY KEY,
        kind               TEXT NOT NULL DEFAULT 'topic_answer_recovery',
        status             TEXT NOT NULL DEFAULT 'pending',
        turn_id            TEXT NOT NULL DEFAULT '',
        topic_id           TEXT NOT NULL DEFAULT '',
        from_topic_id      TEXT,
        fragment_id        TEXT,
        user_message_id    TEXT,
        operation_id       TEXT,
        param_version      INTEGER,
        base_nav_version   INTEGER,
        recovery_nav_version INTEGER,
        failed_stage       TEXT NOT NULL DEFAULT '',
        message            TEXT NOT NULL DEFAULT '',
        stages_json        TEXT NOT NULL DEFAULT '{}',
        answer_key         TEXT,
        answer_id          TEXT,
        answer_text        TEXT,
        last_error         TEXT,
        claim_owner        TEXT,
        claim_until        TEXT,
        attempt_count      INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    )
    """,
    # 清单按「未完成 + 最近」取；租约列单独建部分索引（大量 NULL 不占空间）。
    "CREATE INDEX IF NOT EXISTS idx_topic_recovery_status "
    "ON topic_recovery_records(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_topic_recovery_claim "
    "ON topic_recovery_records(claim_until) WHERE claim_until IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_topic_recovery_answer_key "
    "ON topic_recovery_records(answer_key) WHERE answer_key IS NOT NULL",
]

MESSAGE_DDL: list[str] = [
    # 唯一逻辑答案身份：同一 turn+user_message 的正文只允许一行。
    "ALTER TABLE messages ADD COLUMN answer_key TEXT",
    # F13：迟到答案关联的原用户消息（普通回答为 NULL）。
    "ALTER TABLE messages ADD COLUMN late_answer_of_message_id TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_answer_key "
    "ON messages(answer_key) WHERE answer_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_late_answer "
    "ON messages(late_answer_of_message_id) WHERE late_answer_of_message_id IS NOT NULL",
]

#: 迁移 79：恢复记录表 + messages 答案身份/迟到标记列 + 索引。
MIGRATION: tuple[int, list[str]] = (79, list(TABLE_DDL) + list(MESSAGE_DDL))

#: 补偿专用：messages 的**现代完整形状**（迁移 1 + 17 + 79 的合集）。
#:
#: 为什么必须放进补偿：REQUIRED_OBJECTS 现在包含 messages.answer_key /
#: messages.late_answer_of_message_id；按既有约定（见迁移 48 的 fragments 基础表补建），
#: 清单里的「追加列」必须在补偿里有对应表的 CREATE TABLE，否则「整张基础表被删」
#: 时只能发现缺失、补不回来（test_main_closeout_migration_objects 会判红）。
#: 语句只进补偿入口、不进迁移 79：正常升级时 messages 一定已存在，多一条 IF NOT EXISTS
#: 只是每次都跑的空操作。
BASE_TABLE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           TEXT PRIMARY KEY,
        fragment_id  TEXT REFERENCES fragments(id),
        role         TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
        content      TEXT NOT NULL DEFAULT '',
        content_type TEXT NOT NULL DEFAULT 'text',
        model        TEXT,
        raw          TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL,
        storage_tier TEXT NOT NULL DEFAULT 'hot' CHECK (storage_tier IN ('hot','cold')),
        turn_id      TEXT,
        answer_key   TEXT,
        late_answer_of_message_id TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_fragment ON messages(fragment_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id)",
]

#: 补偿入口 80 需要重放的「建对象语句」（纯 CREATE / ALTER，不改数据）。
COMPENSATION_DDL: list[str] = (
    list(BASE_TABLE_DDL) + list(TABLE_DDL) + list(MESSAGE_DDL)
)


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone()
    return row is not None


def apply(conn: sqlite3.Connection) -> list[str]:
    """幂等应用迁移 79（表已在 → 空列表）。生产路径由 schema.py 统一应用。"""
    executed: list[str] = []
    with transaction(conn):
        for statement in MIGRATION[1]:
            add_column = " ADD COLUMN " in statement.upper()
            if add_column:
                # 列已存在时跳过（与 migrate.py 的幂等规则一致），避免测试库重复报错。
                table = statement.split()[2]
                column = statement.split()[5]
                existing = {
                    str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if column in existing:
                    continue
            conn.execute(statement)
            executed.append(statement)
    return executed


__all__ = [
    "TABLE",
    "TABLE_DDL",
    "MESSAGE_DDL",
    "BASE_TABLE_DDL",
    "MIGRATION",
    "COMPENSATION_DDL",
    "apply",
]
