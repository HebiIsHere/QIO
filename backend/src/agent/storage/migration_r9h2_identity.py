"""迁移 61（R9-H2）：内部数据身份登记 + 解析失败状态纠正（契约 §4/§5）。

只追加，不改历史迁移。

两部分：

1. internal_data_identities：可信应用维护的内部数据对象身份登记（volume:file_id）。
   用于硬链接/移动/改名后仍按真实对象身份保护；同内容复制件身份不同不登记。
   身份读取失败时保留历史登记，不静默降级。
2. 数据纠正：只纠正**有明确解析失败审计证据**的旧记录：
   structured_limits_state='structured' AND structured_limits='[]'
   且同 turn 存在 requirement_resolutions.outcome='unavailable'
   （解析失败 fallback 当时错误地写成了“已确认无限制”）。
   无失败证据的 [] 保持 structured；不删除任何要求/审计/授权历史。

注意：补偿迁移禁止改/删数据，因此 62 只重放本迁移的建表/索引语句；
一次性 UPDATE 纠正只随迁移 61 执行，不进入补偿。
"""

from __future__ import annotations

#: 一次性数据纠正（只在迁移 61 执行；不进补偿迁移）。
DATA_CORRECTION = """
UPDATE user_requirements
SET structured_limits_state = 'unresolved',
    structured_limits_source = 'migration_61:resolution_unavailable'
WHERE structured_limits_state = 'structured'
  AND structured_limits = '[]'
  AND turn_id IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM requirement_resolutions rr
      WHERE rr.outcome = 'unavailable'
        AND rr.relation = 'unknown'
        AND rr.turn_id = user_requirements.turn_id
  )
"""

#: 可补偿的追加对象（建表/索引）；补偿 62 只重放这些语句。
COMPENSATION_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS internal_data_identities (
        identity_key TEXT PRIMARY KEY,
        path TEXT,
        kind TEXT NOT NULL DEFAULT 'file',
        active INTEGER NOT NULL DEFAULT 1,
        source TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        retired_reason TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_internal_data_identities_path ON internal_data_identities(path)",
    "CREATE INDEX IF NOT EXISTS idx_internal_data_identities_active ON internal_data_identities(active)",
]

MIGRATION: tuple[int, list[str]] = (
    61,
    [*COMPENSATION_STATEMENTS, DATA_CORRECTION],
)
