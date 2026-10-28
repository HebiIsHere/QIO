"""B：设置页改版迁移（73/74）与旧库回填保真。

口径（_STG-COMMON.md §7）：
* 只追加迁移，历史迁移一字不改；
* 旧 credentials（含 tags / budget / verify_state / 审计 / 用量账本）升级后，
  引用可解析、默认项与标签映射保留、账本与审计不丢；
* 回填可重放（IF NOT EXISTS / INSERT OR IGNORE），不删列、不改列、不搬密钥；
* 迁移 74 是补偿入口（纯 DDL + 继承的 1 条幂等 seed），能按对象把被删的表补回来。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent.storage.db import connect
from agent.storage.migrate import (
    REQUIRED_OBJECTS,
    apply_migrations,
    current_version,
    missing_objects,
)
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

TS = "2026-10-26T00:00:00+00:00"

LEGACY_COLUMNS = (
    "id, version, tags, endpoint, default_model, budget, budget_used, status,"
    " created_at, updated_at, last_used_at, note, enabled, kind, verify_state,"
    " verified_at, verify_error, is_default, usage_input, usage_output"
)


def _db_at(tmp_path: Path, version: int) -> sqlite3.Connection:
    """只用真实迁移定义把库建到指定版本（与历史测试同一做法）。"""
    conn = connect(tmp_path / f"v{version}.db")
    current_version(conn)
    for target, statements in MIGRATIONS:
        if target > version:
            break
        for statement in statements:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "duplicate column name" in message or "already exists" in message:
                    continue
                raise
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (target, TS)
        )
    assert current_version(conn) == version
    return conn


def _seed_legacy(conn: sqlite3.Connection) -> None:
    """一份「升级前」的真实数据：主对话 + 看图 + 自定义标签 + 账本 + 审计 + 老凭据。"""
    conn.execute(
        f"INSERT INTO credentials ({LEGACY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "key_main",
            2,
            '["main-loop", "vision", "写作"]',
            "https://api.deepseek.com/v1",
            "deepseek-v4-flash",
            1000.0,
            250.0,
            "active",
            TS,
            TS,
            TS,
            "DeepSeek 官方",
            1,
            "openai",
            "verified",
            TS,
            None,
            1,
            100.0,
            150.0,
        ),
    )
    conn.execute(
        "INSERT INTO credential_audit (id, credential_id, action, from_version, to_version,"
        " triggered_by, created_at) VALUES ('aud_1', 'key_main', 'create', NULL, 1, 'user', ?)",
        (TS,),
    )
    # 另一把主对话 Key（不是默认项）：默认模型设置必须只回填 is_default=1 的那条。
    conn.execute(
        f"INSERT INTO credentials ({LEGACY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "key_second",
            1,
            '["main-loop"]',
            "https://api.openai.com/v1",
            "gpt-5.6-luna",
            None,
            0.0,
            "active",
            "2026-10-26T00:00:01+00:00",
            "2026-10-26T00:00:01+00:00",
            None,
            "OpenAI",
            1,
            "openai",
            "verified",
            TS,
            None,
            0,
            0.0,
            0.0,
        ),
    )
    # 改动前的老凭据：没有 default_model（基线由运行时猜兜底模型）——回填不得复制猜测值。
    conn.execute(
        f"INSERT INTO credentials ({LEGACY_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "key_legacy",
            1,
            '["subagent"]',
            "https://api.example.com/v1",
            None,
            None,
            0.0,
            "active",
            "2026-10-26T00:00:02+00:00",
            "2026-10-26T00:00:02+00:00",
            None,
            None,
            1,
            None,
            "legacy",
            None,
            None,
            0,
            0.0,
            0.0,
        ),
    )


def test_versions_are_appended_and_compensation_is_the_latest(tmp_path: Path):
    numbers = [int(number) for number, _ in MIGRATIONS]
    assert numbers == sorted(set(numbers)), numbers
    assert 71 in numbers and 72 in numbers, "历史迁移必须保留"
    assert 73 in numbers, "没有登记只追加迁移 73（模型目录 / 用途分配 / 默认模型）"
    assert 74 in numbers, "没有登记补偿入口迁移 74"
    assert SCHEMA_VERSION == COMPENSATION_VERSION == max(numbers) == 74


def test_required_objects_include_new_tables_and_indexes():
    assert "credential_models" in REQUIRED_OBJECTS["tables"]
    assert "model_usages" in REQUIRED_OBJECTS["tables"]
    for index in (
        "idx_credential_models_credential",
        "idx_credential_models_model",
        "idx_model_usages_usage_key",
    ):
        assert index in REQUIRED_OBJECTS["indexes"], index


def test_compensation_74_is_ddl_only_and_covers_new_objects():
    comp = [str(statement) for target, statements in MIGRATIONS
            if int(target) == COMPENSATION_VERSION for statement in statements]
    heads = [" ".join(statement.split()).upper() for statement in comp]
    assert any("CREATE TABLE IF NOT EXISTS CREDENTIAL_MODELS" in head for head in heads)
    assert any("CREATE TABLE IF NOT EXISTS MODEL_USAGES" in head for head in heads)
    for head in heads:
        assert not head.startswith(("UPDATE", "DELETE", "DROP", "TRUNCATE")), head[:80]
    inserts = [head for head in heads if head.startswith("INSERT")]
    assert len(inserts) == 1 and "KNOWLEDGE_MAINTENANCE_STATE" in inserts[0], (
        "补偿只允许继承的 1 条幂等 seed；回填不能进补偿（只跑一次）"
    )


def test_fresh_database_has_model_catalog_shape(db_conn: sqlite3.Connection):
    columns = {str(row[1]) for row in db_conn.execute("PRAGMA table_info(credential_models)")}
    assert {
        "id",
        "credential_id",
        "model_id",
        "display_name",
        "source",
        "capabilities",
        "verify_state",
        "verified_at",
        "verify_error",
        "verify_fingerprint",
        "context_window",
        "discovered_at",
        "updated_at",
    } <= columns
    usage_columns = {str(row[1]) for row in db_conn.execute("PRAGMA table_info(model_usages)")}
    assert {"id", "usage_key", "credential_id", "model_id", "mode", "updated_at"} <= usage_columns
    indexes = {
        str(row[0])
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name IN"
            " ('credential_models', 'model_usages')"
        )
    }
    assert {
        "idx_credential_models_credential",
        "idx_credential_models_model",
        "idx_model_usages_usage_key",
    } <= indexes
    assert not any(missing_objects(db_conn).values())


def test_upgrade_from_72_backfills_and_preserves_legacy_facts(tmp_path: Path):
    conn = _db_at(tmp_path, 72)
    _seed_legacy(conn)
    budget_before = dict(
        conn.execute(
            "SELECT budget, budget_used, usage_input, usage_output FROM credentials"
            " WHERE id = 'key_main'"
        ).fetchone()
    )

    assert apply_migrations(conn) == SCHEMA_VERSION

    # 1) 旧 default_model → credential_models(source='legacy')，不伪造验证指纹。
    models = {
        (row["credential_id"], row["model_id"]): dict(row)
        for row in conn.execute("SELECT * FROM credential_models")
    }
    main = models[("key_main", "deepseek-v4-flash")]
    assert main["source"] == "legacy"
    assert main["verify_state"] == "verified", "旧结论照抄，但只属于旧配置"
    assert main["verify_fingerprint"] is None, "不得伪造指纹把旧结论说成当前配置已验证"
    assert ("key_second", "gpt-5.6-luna") in models
    assert not any(credential_id == "key_legacy" for credential_id, _ in models), (
        "没有 default_model 的旧凭据不得回填猜测模型"
    )

    # 2) tags → model_usages（main-loop 展开成主对话 + 通用对话；自定义标签保留）。
    usages = {
        row["usage_key"]: dict(row) for row in conn.execute("SELECT * FROM model_usages")
    }
    assert usages["main-chat"]["credential_id"] == "key_main"
    assert usages["main-chat"]["model_id"] == "deepseek-v4-flash"
    assert usages["main-chat"]["mode"] == "explicit"
    assert usages["general-chat"]["credential_id"] == "key_main"
    assert usages["vision"]["credential_id"] == "key_main"
    assert usages["custom:写作"]["credential_id"] == "key_main"
    assert "subagent" not in usages, "没有模型的旧凭据不产生显式分配（不猜模型）"

    # 3) is_default=1 的那条 → settings['model.default']（只一条）。
    raw = conn.execute("SELECT value FROM settings WHERE key = 'model.default'").fetchone()
    assert raw is not None
    assert '"key_main"' in str(raw["value"]) and '"deepseek-v4-flash"' in str(raw["value"])

    # 4) 旧数据仍然完整：账本 / 预算 / 标签 / 验证状态 / 审计一条不丢。
    after = conn.execute(
        "SELECT tags, default_model, verify_state, budget, budget_used, usage_input,"
        " usage_output, status FROM credentials WHERE id = 'key_main'"
    ).fetchone()
    assert (
        after["budget"],
        after["budget_used"],
        after["usage_input"],
        after["usage_output"],
    ) == (
        budget_before["budget"],
        budget_before["budget_used"],
        budget_before["usage_input"],
        budget_before["usage_output"],
    )
    assert after["tags"] == '["main-loop", "vision", "写作"]'
    assert after["default_model"] == "deepseek-v4-flash"
    assert after["verify_state"] == "verified"
    assert after["status"] == "active"
    audit = conn.execute(
        "SELECT action FROM credential_audit WHERE credential_id = 'key_main'"
    ).fetchall()
    assert [row["action"] for row in audit] == ["create"]


def test_backfill_is_replay_safe(tmp_path: Path):
    conn = _db_at(tmp_path, 72)
    _seed_legacy(conn)
    apply_migrations(conn)
    first_models = conn.execute("SELECT COUNT(*) AS n FROM credential_models").fetchone()["n"]
    first_usages = conn.execute("SELECT COUNT(*) AS n FROM model_usages").fetchone()["n"]
    # 再跑一次（真实场景：同一版本重复应用 / 补偿重放）不得产生重复行，也不得覆盖。
    assert apply_migrations(conn) == SCHEMA_VERSION
    conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'model.default'",
        ('{"credential_id": "key_second", "model_id": "gpt-5.6-luna"}',),
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM credential_models").fetchone()["n"] == first_models
    assert conn.execute("SELECT COUNT(*) AS n FROM model_usages").fetchone()["n"] == first_usages


def test_compensation_repairs_deleted_tables(tmp_path: Path):
    conn = connect(tmp_path / "repaired.db")
    apply_migrations(conn)
    conn.execute("DROP TABLE model_usages")
    conn.execute("DROP TABLE credential_models")
    assert {"credential_models", "model_usages"} <= set(missing_objects(conn)["tables"])
    assert apply_migrations(conn) == COMPENSATION_VERSION
    assert not any(missing_objects(conn).values())
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"credential_models", "model_usages"} <= tables
