"""迁移 57（R9-G）：用户要求版本绑定 / 审查与审批的操作归属。

契约：docs/r9g-contracts.md §3、§6。只追加，不改历史迁移。

为什么需要：

* `user_requirements.version`：补充/修正产生子要求时父/子版本必须可核对；
  操作只能绑定它实际依据的那个要求版本，不能"取最近一条 active 要求"。
* `pending_approvals.requirement_id / requirement_version`：审批单必须记录它依据的
  具体用户要求及版本；重启/排队后执行前核对要求是否仍有效、是否被修正/撤回。
* `authorization_records.requirement_version`：有界授权记录与要求版本绑定，
  修正后旧版本记录不能继续复用。
* `authorization_review_audit.requirement_version`：审查审计能定位当时依据的版本。
* `requirement_resolutions`：模型对"这条新消息是补充/修正/撤回/新要求"的判断与
  后端应用结果审计（不落推理全文，只落关系/目标/原因/模型与版本/结果）。

幂等：全部 IF NOT EXISTS / 追加列由 migrate.py 的 ADD COLUMN 预检保护，可被补偿迁移 58 重放。
"""

from __future__ import annotations

MIGRATION: tuple[int, list[str]] = (
    57,
    [
        "ALTER TABLE user_requirements ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE pending_approvals ADD COLUMN requirement_id TEXT",
        "ALTER TABLE pending_approvals ADD COLUMN requirement_version INTEGER",
        "ALTER TABLE authorization_records ADD COLUMN requirement_version INTEGER",
        "ALTER TABLE authorization_review_audit ADD COLUMN requirement_version INTEGER",
        """
        CREATE TABLE IF NOT EXISTS requirement_resolutions (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            turn_id TEXT,
            relation TEXT NOT NULL,
            target_requirement_id TEXT,
            outcome TEXT NOT NULL,
            reason TEXT,
            model TEXT,
            prompt_version TEXT,
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_requirement_resolutions_target ON requirement_resolutions(target_requirement_id, created_at)",
    ],
)
