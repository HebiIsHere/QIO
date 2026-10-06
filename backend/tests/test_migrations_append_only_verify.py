"""D 独立验证：迁移只追加，历史迁移一字不改（AGENTS.md 硬性约束 + 契约 §4.2）。

做法：把基线 ee6bbff 里 version <= 25 的迁移序列规范化后取 sha256，冻结在下面。
只要有人改了历史迁移（哪怕只改一行 SQL 文本 / 顺序 / 版本号），这个摘要就会变，
用例立刻变红；新增 version 26 的 attachments 迁移不受影响。

基线：ee6bbff1841b869be8b2e17dd85dc434b496b839（25 条迁移）。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_migrations_append_only_verify.py -q
"""

from __future__ import annotations

import hashlib
import json

BASELINE_COMMIT = "ee6bbff1841b869be8b2e17dd85dc434b496b839"
BASELINE_VERSION = 25
BASELINE_DIGEST = "bce312cf92d7a3743b5fbe04c6e15a87b4b770e4132b21af343965eaca7d4f6a"

# 契约 §4.2 冻结的 attachments 列
ATTACHMENT_COLUMNS = {
    "id",
    "message_id",
    "turn_id",
    "topic_id",
    "kind",
    "original_name",
    "stored_path",
    "source_path",
    "size_bytes",
    "mtime",
    "sha256",
    "state",
    "error",
    "created_at",
    "updated_at",
}


def _canonical(prefix: int) -> str:
    from agent.storage.schema import MIGRATIONS

    baseline = [(int(version), list(statements)) for version, statements in MIGRATIONS if int(version) <= prefix]
    return hashlib.sha256(
        json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_historical_migrations_are_byte_identical_to_baseline():
    from agent.storage.schema import MIGRATIONS, SCHEMA_VERSION

    assert SCHEMA_VERSION >= BASELINE_VERSION, (SCHEMA_VERSION, BASELINE_VERSION)
    versions = [int(version) for version, _ in MIGRATIONS]
    assert versions == sorted(versions), ("迁移必须按版本升序", versions)
    assert len(set(versions)) == len(versions), ("迁移版本号不得重复", versions)
    actual = _canonical(BASELINE_VERSION)
    assert actual == BASELINE_DIGEST, (
        "历史迁移被改动了（只能追加，禁止修改已有迁移）",
        {"baseline": BASELINE_COMMIT, "expected": BASELINE_DIGEST, "actual": actual},
    )


def test_attachments_table_is_created_by_an_appended_migration(db_conn):
    rows = db_conn.execute("PRAGMA table_info(attachments)").fetchall()
    assert rows, "契约 §4.2：必须有一张 attachments 表（只追加迁移）"
    columns = {str(row["name"]) for row in rows}
    missing = ATTACHMENT_COLUMNS - columns
    assert not missing, f"attachments 缺列 {sorted(missing)}；实际={sorted(columns)}"

    # kind / state 有枚举语义；不写死 CHECK 约束（契约没冻结 DDL 细节），只查数据面
    kinds = {
        str(row["kind"])
        for row in db_conn.execute("SELECT DISTINCT kind FROM attachments").fetchall()
    }
    assert kinds <= {"copy", "reference"}, kinds
