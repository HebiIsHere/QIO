"""迁移 53（R9）：审批生命周期 v2 —— 可恢复操作、原子领取、状态机与审计。

契约：docs/r9-contracts.md §5、§10。只追加、不改历史迁移。

为什么需要这些列/表：

* `operation`：持久化"批准后真正要执行什么"（可序列化参数），不能只保存弹窗内容；
  重启后仍能续接，而不是只显示一条历史记录。
* `fingerprint`：批准后、执行前重新核对的对象身份/内容/范围摘要。
* `instruction_id` / `instruction_text` / `purpose`：授权依据不是 turn/topic/session，
  而是具体用户指令；审计必须能回溯到原话。
* `mode_at_request` / `mode_at_approval`：模式切换后重核对（严格模式不得执行旧宽松审批）。
* `state`：waiting / approved / executing / completed / failed / rejected / cancelled /
  expired / interrupted_unverified / invalidated。`status` 旧列保留兼容。
* `confirmed_stage`：内部运行数据"连续三次确认"的后端状态（CAS 推进）。
* `attempt` / `claimed_at`：通过 `UPDATE ... WHERE state='approved'` 的原子领取防止重复执行。
* `approval_audit`：状态迁移审计（每次动作一条，seq 单调）；
  `approval_history`：执行结果的可读历史（前端"审批结果"列表）。

幂等性：列追加由 migrate.py 的 ADD COLUMN 预检与 IF NOT EXISTS 保护；表/索引全部
IF NOT EXISTS，可被补偿迁移 54 整体重放。
"""

from __future__ import annotations

#: 迁移号 / SQL 的唯一来源（schema.py 直接引用这组语句）。
MIGRATION: tuple[int, list[str]] = (
    53,
    [
        # 权威状态机（旧 status 列保留兼容）
        "ALTER TABLE pending_approvals ADD COLUMN state TEXT",
        # 批准后真正要执行的操作（kind/payload/effect/label，JSON）
        "ALTER TABLE pending_approvals ADD COLUMN operation TEXT NOT NULL DEFAULT '{}'",
        # 授权依据：具体用户指令
        "ALTER TABLE pending_approvals ADD COLUMN instruction_id TEXT",
        "ALTER TABLE pending_approvals ADD COLUMN instruction_text TEXT",
        "ALTER TABLE pending_approvals ADD COLUMN purpose TEXT",
        # 重核对指纹：对象身份 / 内容摘要 / 范围
        "ALTER TABLE pending_approvals ADD COLUMN fingerprint TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE pending_approvals ADD COLUMN mode_at_request TEXT",
        "ALTER TABLE pending_approvals ADD COLUMN mode_at_approval TEXT",
        # 内部运行数据三次确认的后端状态
        "ALTER TABLE pending_approvals ADD COLUMN confirmed_stage INTEGER NOT NULL DEFAULT 0",
        # 乐观并发 / 重复领取防护
        "ALTER TABLE pending_approvals ADD COLUMN revisions INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pending_approvals ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pending_approvals ADD COLUMN claimed_at TEXT",
        # 执行结果与恢复事实
        "ALTER TABLE pending_approvals ADD COLUMN result TEXT",
        "ALTER TABLE pending_approvals ADD COLUMN error TEXT",
        "ALTER TABLE pending_approvals ADD COLUMN resumed_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_pending_approvals_state"
        " ON pending_approvals(state)",
        "CREATE INDEX IF NOT EXISTS idx_pending_approvals_expiry"
        " ON pending_approvals(expires_at)",
        # 状态迁移审计（每次动作一条）
        """
        CREATE TABLE IF NOT EXISTS approval_audit (
            approval_id TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            action      TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '{}',
            at          TEXT NOT NULL,
            PRIMARY KEY (approval_id, seq)
        )
        """,
        # 执行结果的可读历史
        """
        CREATE TABLE IF NOT EXISTS approval_history (
            approval_id TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            outcome     TEXT NOT NULL,
            detail      TEXT NOT NULL DEFAULT '{}',
            at          TEXT NOT NULL
        )
        """,
    ],
)
