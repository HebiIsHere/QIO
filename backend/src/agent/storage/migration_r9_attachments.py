"""迁移 52（R9）：附件从"复制副本"改为"源文件引用"。

契约：docs/r9-contracts.md §6、§9、§10。只追加、不改历史迁移。

为什么需要这些列/表：

* 新增 `source_kind` / `origin` 用来记录**文件来源与归属**，不能仅凭所在目录判断：
  用户原有文件、用户提供但由 QIO 首次落盘的材料、QIO 自主生成的工作文件，
  三者的可修改/可清理规则不同；粘贴截图虽由 QIO 保存，仍是用户提供的材料。
* `file_id`（卷序列号 + 文件 id）与 `content_sha256` 区分"同一文件"和"相同内容"：
  移动/改名后只有文件 id 相同才算同一文件；编辑后内容哈希会变、文件身份不变。
* `reference_state` 记录可用/缺失/待确认；`material_enabled` 是"作为互动材料"开关，
  只控制是否主动纳入互动上下文，不是操作系统文件访问权限。
* `legacy_paths` 保存历史副本与源文件的差异事实，迁移时不静默替换历史材料。
* `attachment_migration_audit` 让旧数据迁移可重复运行、断点续跑，不丢唯一内容。
* `material_cleanup_candidates` 把"登记迁移"与"磁盘清理"分开：先确保引用全部可用，
  再生成清理清单，经批准后移入系统回收站。

幂等性：列追加由 migrate.py 的 ADD COLUMN 预检与 IF NOT EXISTS 保护；表/索引全部
IF NOT EXISTS，可被补偿迁移 54 整体重放。
"""

from __future__ import annotations

#: 迁移号 / SQL 的唯一来源（schema.py 直接引用这组语句）。
MIGRATION: tuple[int, list[str]] = (
    52,
    [
        # 来源入口：desktop_path / dropped_path / browser_upload / clipboard_image / legacy_copy
        "ALTER TABLE attachments ADD COLUMN source_kind TEXT",
        # 归属：user_original / user_saved_material / qio_generated
        "ALTER TABLE attachments ADD COLUMN origin TEXT",
        # 文件身份键（Windows: volume+file_id；POSIX: dev+ino），用于移动修复的唯一可靠依据
        "ALTER TABLE attachments ADD COLUMN file_id TEXT",
        # 内容摘要：内容变化 ≠ 文件身份变化，两者并列记录
        "ALTER TABLE attachments ADD COLUMN content_sha256 TEXT",
        # available / missing / ambiguous / relocated
        "ALTER TABLE attachments ADD COLUMN reference_state TEXT",
        # "作为互动材料"开关：只影响上下文纳入，不是 OS 权限
        "ALTER TABLE attachments ADD COLUMN material_enabled INTEGER NOT NULL DEFAULT 1",
        # 用户对这份材料的说明（模型判断是否读取所需的信息之一）
        "ALTER TABLE attachments ADD COLUMN user_note TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE attachments ADD COLUMN last_verified_at TEXT",
        # JSON 数组：历史副本位置、与源文件的差异事实（迁移保留，不静默替换）
        "ALTER TABLE attachments ADD COLUMN legacy_paths TEXT NOT NULL DEFAULT '[]'",
        # 迁移审计（按行、按步骤）：可重复运行与断点续跑的凭据
        """
        CREATE TABLE IF NOT EXISTS attachment_migration_audit (
            attachment_id TEXT NOT NULL,
            step          TEXT NOT NULL,
            outcome       TEXT NOT NULL,
            detail        TEXT NOT NULL DEFAULT '{}',
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (attachment_id, step)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_attachment_migration_audit_step"
        " ON attachment_migration_audit(step, outcome)",
        # 冗余材料清理清单：与迁移登记分开，必须经批准后才进系统回收站
        """
        CREATE TABLE IF NOT EXISTS material_cleanup_candidates (
            attachment_id TEXT PRIMARY KEY,
            path          TEXT NOT NULL,
            reason        TEXT NOT NULL DEFAULT '',
            state         TEXT NOT NULL DEFAULT 'candidate',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_material_cleanup_state"
        " ON material_cleanup_candidates(state)",
    ],
)
