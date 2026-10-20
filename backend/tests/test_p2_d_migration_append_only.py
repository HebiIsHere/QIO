# -*- coding: utf-8 -*-
"""D 独立契约测试：阶段二迁移 46/47/48 只追加（契约 §12.3 / §12.4 / §12.6 / §12.7）。

基线（bcce3a2a）最大迁移号 45。本文件核对：
* 历史迁移 1..45 的内容摘要不变（只追加，不改历史语义）；
* 新增 46（A：变化日志 / 调度状态 / turn 去重）、47（B：维护批次 / 事项 / 操作）、
  48（Lead 补偿 = 45 全部语句 + 46 + 47）逐个登记，编号唯一递增；
* 48 = 45 + 46 + 47 的语句拼接（不手抄、不丢段），且不改/删数据；
* 全新库迁移到 48 后，三组新对象确实建出来；重复应用幂等；
* REQUIRED_OBJECTS 的每一项都在补偿 48 里有对应建对象语句。

基线说明：阶段二实现未合入时，46/47/48 尚不存在，本文件会**失败**（不是 skip），
失败信息里写明缺哪条迁移——这是契约要求，不用 skip 掩盖。

运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_migration_append_only.py -q
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from agent.storage import migrate as migrate_module
from agent.storage.migrate import REQUIRED_OBJECTS, apply_migrations, current_version
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

CLAUSE = "契约 §12.6/§12.7"
BASELINE_MAX = 45
# 在 bcce3a2a 上用同一规范化函数算出的 1..45 语句摘要（只追加检查）。
BASELINE_HISTORY_SHA256 = "0d68da53abf47923add88fbfe054b63c1c608ef4f1cacada273c58245589f78c"

NEW_TABLES = (
    "knowledge_change_log",
    "knowledge_maintenance_state",
    "knowledge_maintenance_turn_log",
    "maintenance_batches",
    "maintenance_items",
    "maintenance_operations",
)


def _norm(statements) -> tuple[str, ...]:
    return tuple(" ".join(str(stmt).split()) for stmt in statements)


def _digest(numbered) -> str:
    digest = hashlib.sha256()
    for number, statements in numbered:
        digest.update((str(number) + "\x1f").encode())
        for stmt in _norm(statements):
            digest.update(stmt.encode())
            digest.update(b"\x1e")
    return digest.hexdigest()


def _by_number() -> dict[int, list[str]]:
    return {int(number): list(statements) for number, statements in MIGRATIONS}


def _missing_migrations() -> list[int]:
    numbers = {int(number) for number, _ in MIGRATIONS}
    return [number for number in (46, 47, 48) if number not in numbers]


# ---------------------------------------------------------------------------
# 只追加
# ---------------------------------------------------------------------------


def test_history_1_45_is_append_only():
    """§12.7：历史迁移（1..45）不得改写；只能在末尾追加。"""
    assert max(int(n) for n, _ in MIGRATIONS) >= BASELINE_MAX, (
        f"{CLAUSE}：迁移序列最大号小于基线 {BASELINE_MAX}（历史被删改？）"
    )
    history = [(number, statements) for number, statements in MIGRATIONS
               if int(number) <= BASELINE_MAX]
    assert _digest(history) == BASELINE_HISTORY_SHA256, (
        f"{CLAUSE}：检测到历史迁移（<= {BASELINE_MAX}）语句被改写。"
        "迁移只允许追加；若确需改动必须由 Lead 裁决并写进契约/台账。"
    )


def test_migration_numbers_are_unique_and_increasing():
    numbers = [int(number) for number, _ in MIGRATIONS]
    assert numbers == sorted(set(numbers)), f"{CLAUSE}：迁移号必须唯一且严格递增：{numbers}"


def test_migrations_46_47_48_are_registered():
    """§12.7：A 的 46、B 的 47、Lead 的补偿 48 必须逐个登记。"""
    missing = _missing_migrations()
    if missing:
        numbers = sorted(int(n) for n, _ in MIGRATIONS)
        pytest.fail(
            f"{CLAUSE}：缺少迁移号 {missing}（当前已登记：{numbers}）。"
            "迁移 46/47 由 A/B 提供 migration_p2_a.py / migration_p2_b.py，"
            "整合时由 Lead 登记到 storage/schema.py 并追加补偿 48。"
            "阶段二实现未合入时这属于「未验证」。",
            pytrace=False,
        )
    table = _by_number()
    for number in (46, 47, 48):
        assert table[number], f"{CLAUSE}：迁移 {number} 没有 SQL 语句"
    assert SCHEMA_VERSION == max(int(n) for n, _ in MIGRATIONS) == 48, (
        f"{CLAUSE}：SCHEMA_VERSION 必须收敛到 48（实际 {SCHEMA_VERSION}）"
    )
    assert COMPENSATION_VERSION == 48, (
        f"{CLAUSE}：COMPENSATION_VERSION 必须为 48（实际 {COMPENSATION_VERSION}）"
    )


def test_compensation_48_equals_45_plus_46_plus_47():
    """§12.7：补偿 48 = 迁移 45 全部语句 + 46 全部语句 + 47 全部语句。"""
    missing = _missing_migrations()
    if missing:
        pytest.fail(
            f"{CLAUSE}：迁移 {missing} 未登记，无法核对补偿 48 的拼接内容。",
            pytrace=False,
        )
    table = _by_number()
    expected = _norm(table[BASELINE_MAX]) + _norm(table[46]) + _norm(table[47])
    assert _norm(table[48]) == expected, (
        f"{CLAUSE}：迁移 48 不是「45 全部语句 + 46 + 47」的拼接："
        f"期望 {len(expected)} 条，实际 {len(table[48])} 条。"
        "补偿必须由语句列表推导（不手抄），否则存量库仍可能缺对象。"
    )


def test_compensation_48_is_additive_only():
    """§12.6：补偿迁移只建对象 / 补列，不得改删已有数据。"""
    if _missing_migrations():
        pytest.fail(f"{CLAUSE}：迁移 48 未登记。", pytrace=False)
    statements = _by_number()[48]
    for stmt in statements:
        head = str(stmt).strip().upper()
        assert not head.startswith(("DROP ", "DELETE ", "UPDATE ", "TRUNCATE")), (
            f"{CLAUSE}：补偿迁移 48 含改/删数据语句：{str(stmt)[:80]}"
        )


# ---------------------------------------------------------------------------
# 新库实际迁移
# ---------------------------------------------------------------------------


def test_fresh_database_reaches_48_with_p2_objects(tmp_path):
    """§12.6：新库一次安装到 48，三组新对象齐全；重复应用幂等。"""
    if _missing_migrations():
        pytest.fail(f"{CLAUSE}：迁移 46/47/48 尚未登记，无法验证新库形状。", pytrace=False)
    from agent.storage.db import connect

    conn: sqlite3.Connection = connect(tmp_path / "p2_fresh.db")
    try:
        version = apply_migrations(conn)
        assert version == 48, f"{CLAUSE}：新库版本应为 48（实际 {version}）"
        assert current_version(conn) == 48
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in NEW_TABLES if name not in tables]
        assert not missing, f"{CLAUSE}：新库缺少阶段二对象 {missing}"
        # 重复应用：版本不倒退、对象仍在
        assert apply_migrations(conn) == 48
        assert migrate_module.missing_objects(conn) == {
            "tables": [], "columns": [], "indexes": []}
    finally:
        conn.close()


def test_change_log_and_state_columns_match_contract(tmp_path):
    """§12.3：迁移 46 的列语义（契约里点名了这些列）。"""
    if _missing_migrations():
        pytest.fail(f"{CLAUSE}：迁移 46 未登记。", pytrace=False)
    from agent.storage.db import connect

    conn = connect(tmp_path / "cols.db")
    try:
        apply_migrations(conn)

        def columns(table: str) -> set[str]:
            return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

        change_cols = columns("knowledge_change_log")
        for name in ("seq", "knowledge_id", "chain_id", "version", "source",
                     "occurred_at", "created_turn_seq", "consumed_batch_seq"):
            assert name in change_cols, (
                f"{CLAUSE}：knowledge_change_log 缺列 {name}（实际 {sorted(change_cols)}）")
        state_cols = columns("knowledge_maintenance_state")
        for name in ("turn_seq", "last_processed_seq", "running", "run_started_at",
                     "failed_attempts", "retry_blocked_until_turn", "last_error"):
            assert name in state_cols, (
                f"{CLAUSE}：knowledge_maintenance_state 缺列 {name}（实际 {sorted(state_cols)}）")
        turn_log_cols = columns("knowledge_maintenance_turn_log")
        assert "turn_id" in turn_log_cols, (
            f"{CLAUSE}：knowledge_maintenance_turn_log 缺 turn_id（实际 {sorted(turn_log_cols)}）")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# REQUIRED_OBJECTS 与补偿 48 一一对应（契约 §12.6）
# ---------------------------------------------------------------------------


def test_required_objects_have_compensation_statements():
    if _missing_migrations():
        pytest.fail(f"{CLAUSE}：迁移 48 未登记，无法核对 REQUIRED_OBJECTS。", pytrace=False)
    statements = [str(stmt) for stmt in _by_number()[48]]
    blob = "\n".join(" ".join(stmt.split()) for stmt in statements)
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = str(spec).partition(".")
        assert f"ADD COLUMN {column}" in blob and table in blob, (
            f"{CLAUSE}：REQUIRED_OBJECTS 列 {spec} 在补偿 48 里没有对应语句")
    for name in REQUIRED_OBJECTS["indexes"]:
        assert name in blob, f"{CLAUSE}：REQUIRED_OBJECTS 索引 {name} 在补偿 48 里没有语句"
    for name in REQUIRED_OBJECTS["tables"]:
        assert f"CREATE TABLE IF NOT EXISTS {name}" in blob, (
            f"{CLAUSE}：REQUIRED_OBJECTS 表 {name} 在补偿 48 里没有 CREATE 语句")
