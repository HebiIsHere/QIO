"""迁移 55（R9-F）：模型授权审查 / 需求与授权记录 / 文件来源。

契约：docs/r9f-contracts.md §3、§4。只追加，不改历史迁移。

为什么需要这些表：

* `user_requirements`：受限模式自然语言授权的依据是**具体用户要求**（原话+来源），
  不是 turn/topic/session；后续修正/撤回必须能回溯到真实用户消息。
* `authorization_records`：模型审查通过后形成**有界**授权记录（requirement +
  operation fingerprint + 对象/限制），用于同范围复用与重启后重建；不是长期权限。
* `authorization_review_audit`：每次审查的输入摘要/结果/模型与提示词版本/耗时/错误，
  便于复核误判；不持久化模型推理全文。
* `file_origins`：后端维护的持久化文件来源（user_original / user_saved_material /
  qio_generated），与文件身份（volume+file_id）关联，供 fs/附件写回/原子替换查询；
  不信任模型传入的 origin 字段。

幂等：全部 IF NOT EXISTS，可被 R9 补偿 58（以及三分支整合后的统一补偿 70）整体重放。
"""

from __future__ import annotations

MIGRATION: tuple[int, list[str]] = (
    55,
    [
        # 用户要求：原话 + 真实来源 + 生命周期
        """
        CREATE TABLE IF NOT EXISTS user_requirements (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT '{}',
            parent_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            limits TEXT NOT NULL DEFAULT '[]',
            turn_id TEXT,
            topic_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_user_requirements_status ON user_requirements(status, created_at)",
        # 有界授权记录（模型审查 / 用户批准产生）
        """
        CREATE TABLE IF NOT EXISTS authorization_records (
            id TEXT PRIMARY KEY,
            requirement_id TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            allowed_operations TEXT NOT NULL DEFAULT '[]',
            objects TEXT NOT NULL DEFAULT '[]',
            limits TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'model_review',
            mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT,
            review_id TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_authorization_records_req ON authorization_records(requirement_id, fingerprint)",
        # 审查审计（输入摘要 / 结果 / 模型与提示词版本 / 耗时 / 错误）
        """
        CREATE TABLE IF NOT EXISTS authorization_review_audit (
            id TEXT PRIMARY KEY,
            requirement_id TEXT,
            request_digest TEXT NOT NULL,
            model TEXT,
            prompt_version TEXT,
            outcome TEXT NOT NULL,
            decision TEXT,
            reason TEXT,
            latency_ms INTEGER,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_authorization_review_audit_req ON authorization_review_audit(requirement_id, created_at)",
        # 文件来源（身份优先；路径仅作缺失/移动时的辅助检索）
        """
        CREATE TABLE IF NOT EXISTS file_origins (
            volume TEXT NOT NULL,
            file_id TEXT NOT NULL,
            path TEXT NOT NULL,
            origin TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            attachment_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            missing_at TEXT,
            PRIMARY KEY (volume, file_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_file_origins_path ON file_origins(path)",
    ],
)
