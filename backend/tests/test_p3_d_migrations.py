# -*- coding: utf-8 -*-
"""D 独立契约测试：阶段三迁移 49/50/51 只追加（契约 §13.6 / §13.7）。

基线（f80a5fa8）最大迁移号 48。核对：
* 历史迁移 1..48 内容摘要不变（只追加，不改历史语义）；
* 49（A：提交—主循环 turn 绑定 / 等待事项 / 续作恢复）、50（B：意图版本与授权快照 /
  权威操作账本 board_operations）、51（Lead 补偿 = 48 全部语句 + 49 + 50）逐个登记；
* 51 的语句是 48 + 49 + 50 的拼接；补偿只增不改删；
* 全新库到 51；REQUIRED_OBJECTS 每项在补偿 51 有对应语句。

基线说明：本阶段实现未合入时 49/50/51 不存在，本文件会**失败**（不是 skip），
失败信息写明缺哪条迁移。运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_migrations.py -q
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from agent.storage import migrate as migrate_module
from agent.storage.migrate import REQUIRED_OBJECTS, apply_migrations, current_version
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

CLAUSE = "契约 §13.6/§13.7"
BASELINE_MAX = 48
# 在 f80a5fa8 上用同一规范化函数算出的 1..48 语句摘要。
BASELINE_HISTORY_SHA256 = "9f5b7e8ee68951d3a1038ab0a547d9ea4abc0881904d2855d3a3b32d0b397258"


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


def _missing() -> list[int]:
    numbers = {int(number) for number, _ in MIGRATIONS}
    return [number for number in (49, 50, 51) if number not in numbers]


def test_history_1_48_is_append_only():
    """§13.7：历史迁移（1..48）不得改写；只能在末尾追加。"""
    history = [(number, statements) for number, statements in MIGRATIONS
               if int(number) <= BASELINE_MAX]
    assert _digest(history) == BASELINE_HISTORY_SHA256, (
        f"{CLAUSE}：检测到历史迁移（<= {BASELINE_MAX}）语句被改写。"
        "迁移只允许追加；若确需改动必须由 Lead 裁决并写进契约/台账。")


def test_migration_numbers_increasing_unique():
    numbers = [int(number) for number, _ in MIGRATIONS]
    assert numbers == sorted(set(numbers)), f"{CLAUSE}：迁移号必须唯一且递增：{numbers}"


def test_migrations_49_50_51_registered_and_versions_converged():
    """§13.7：49=A、50=B、51=补偿；SCHEMA_VERSION = COMPENSATION_VERSION = 51。"""
    missing = _missing()
    if missing:
        numbers = sorted(int(n) for n, _ in MIGRATIONS)
        pytest.fail(
            f"{CLAUSE}：缺少迁移号 {missing}（当前已登记：{numbers}）。"
            "A/B 分别提供 storage/migration_p3_a.py / migration_p3_b.py，"
            "Lead 登记 schema.py 并追加补偿 51。阶段三实现未合入时属于「未验证」。",
            pytrace=False,
        )
    table = _by_number()
    for number in (49, 50, 51):
        assert table[number], f"{CLAUSE}：迁移 {number} 没有 SQL 语句"
    # R9（2026-10-22）追加迁移 52/53 与补偿 54 后，阶段三的 49/50/51 仍是历史事实；
    # 收敛断言改为"最新迁移 = SCHEMA_VERSION = COMPENSATION_VERSION 且不小于 51"，
    # 既保持阶段三契约（51 已登记）又允许后续追加（只追加，不推翻历史编号）。
    latest = max(int(n) for n, _ in MIGRATIONS)
    assert latest >= 51
    assert SCHEMA_VERSION == latest, f"{CLAUSE}：SCHEMA_VERSION 必须等于最新迁移（实际 {SCHEMA_VERSION} / {latest}）"
    assert COMPENSATION_VERSION == latest, (
        f"{CLAUSE}：COMPENSATION_VERSION 必须等于最新迁移（实际 {COMPENSATION_VERSION} / {latest}）")


def test_compensation_51_equals_48_plus_49_plus_50():
    """§13.7：补偿 51 = 迁移 48 全部语句 + 49 全部语句 + 50 全部语句。"""
    if _missing():
        pytest.fail(f"{CLAUSE}：迁移 {_missing()} 未登记，无法核对补偿 51。", pytrace=False)
    table = _by_number()
    expected = _norm(table[48]) + _norm(table[49]) + _norm(table[50])
    assert _norm(table[51]) == expected, (
        f"{CLAUSE}：迁移 51 不是「48 全部语句 + 49 + 50」的拼接："
        f"期望 {len(expected)} 条，实际 {len(table[51])} 条。")


def test_compensation_51_is_additive_only():
    if _missing():
        pytest.fail(f"{CLAUSE}：迁移 51 未登记。", pytrace=False)
    for stmt in _by_number()[51]:
        head = str(stmt).strip().upper()
        assert not head.startswith(("DROP ", "DELETE ", "UPDATE ", "TRUNCATE")), (
            f"{CLAUSE}：补偿迁移 51 含改/删数据语句：{str(stmt)[:80]}")


def test_migration_modules_export_frozen_shape():
    """§13.7：A/B 的迁移模块导出 MIGRATION=(号,[SQL])，版本号分别为 49 / 50。"""
    import importlib

    for module_name, number in (
        ("agent.storage.migration_p3_a", 49),
        ("agent.storage.migration_p3_b", 50),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"{CLAUSE}：{module_name} 不可导入（实现未合入时属于「未验证」）：{exc!r}",
                pytrace=False,
            )
        migration = getattr(module, "MIGRATION", None)
        assert isinstance(migration, tuple) and len(migration) == 2, (
            f"{CLAUSE}：{module_name} 必须导出 MIGRATION = (号, [SQL...])")
        assert int(migration[0]) == number, (
            f"{CLAUSE}：{module_name} 的迁移号必须是 {number}")
        assert list(migration[1]) and all(str(s).strip() for s in migration[1])


def test_migration_49_provides_waiting_and_turn_binding_storage():
    """§13.3/§13.7：49 必须提供提交—turn 绑定 / 等待 / 续作恢复的持久化对象。"""
    if _missing():
        pytest.fail(f"{CLAUSE}：迁移 49 未登记。", pytrace=False)
    blob = " ".join(" ".join(str(s).split()) for s in _by_number()[49]).lower()
    assert "create table" in blob or "alter table" in blob, (
        f"{CLAUSE}：迁移 49 没有创建/扩展任何持久化对象")
    # 等待语义必须落在持久化列/表里（不能只放内存）
    assert any(token in blob for token in ("waiting", "wait")), (
        f"{CLAUSE}：迁移 49 未见等待事项相关列/表")
    assert any(token in blob for token in ("turn_id", "resume")), (
        f"{CLAUSE}：迁移 49 未见提交—turn 绑定 / 续作相关列")


def test_migration_50_provides_intent_versions_and_operation_ledger(db_conn):
    """§13.4/§13.7：50 必须包含意图版本历史与权威操作账本（board_operations）。

    summary / goal / expected / basis 是更早迁移（34/35/37）已有的列，50 不重复添加；
    这里同时核对「增量列」与「最终表确实具备完成/账本所需字段」。
    """
    if _missing():
        pytest.fail(f"{CLAUSE}：迁移 50 未登记。", pytrace=False)
    blob = " ".join(" ".join(str(s).split()) for s in _by_number()[50]).lower()
    for table in ("board_intent_versions", "board_operations"):
        assert f"create table if not exists {table}" in blob, (
            f"{CLAUSE}：迁移 50 缺少表 {table}")
    for column in ("client_ref", "version", "scope", "authorization", "outcome",
                   "attempt", "finished_at"):
        assert column in blob, (
            f"{CLAUSE}：迁移 50 的 board_intents 增量缺列 {column}")
    final_columns = {
        str(row["name"])
        for row in db_conn.execute("PRAGMA table_info(board_intents)")
    }
    for existing in ("goal", "expected", "basis", "summary"):
        assert existing in final_columns, (
            f"{CLAUSE}：迁移 51 后 board_intents 仍缺列 {existing}")
    for column in ("operation_key", "request_hash", "before", "after", "result"):
        assert column in blob, (
            f"{CLAUSE}：board_operations 缺列 {column}")


def test_fresh_database_reaches_51_and_is_idempotent(tmp_path):
    if _missing():
        pytest.fail(f"{CLAUSE}：迁移 49/50/51 尚未登记，无法验证新库形状。", pytrace=False)
    from agent.storage.db import connect

    conn: sqlite3.Connection = connect(tmp_path / "p3.db")
    try:
        assert apply_migrations(conn) == COMPENSATION_VERSION
        assert current_version(conn) == COMPENSATION_VERSION
        assert migrate_module.missing_objects(conn) == {
            "tables": [], "columns": [], "indexes": []}
        # 幂等：第二次 apply 仍返回当前补偿版本（不再改动库）。
        assert apply_migrations(conn) == COMPENSATION_VERSION
    finally:
        conn.close()


def test_required_objects_have_compensation_statements():
    """§13.6：REQUIRED_OBJECTS 的每一项都必须在当前补偿迁移里有对应建对象语句。"""
    if _missing():
        pytest.fail(f"{CLAUSE}：迁移 51 未登记，无法核对 REQUIRED_OBJECTS。", pytrace=False)
    blob = "\n".join(" ".join(str(s).split()) for s in _by_number()[COMPENSATION_VERSION])
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = str(spec).partition(".")
        assert f"ADD COLUMN {column}" in blob and table in blob, (
            f"{CLAUSE}：REQUIRED_OBJECTS 列 {spec} 在补偿 {COMPENSATION_VERSION} 里没有对应语句")
    for name in REQUIRED_OBJECTS["indexes"]:
        assert name in blob, (
            f"{CLAUSE}：REQUIRED_OBJECTS 索引 {name} 在补偿 {COMPENSATION_VERSION} 里没有语句")
    for name in REQUIRED_OBJECTS["tables"]:
        assert f"CREATE TABLE IF NOT EXISTS {name}" in blob, (
            f"{CLAUSE}：REQUIRED_OBJECTS 表 {name} 在补偿 {COMPENSATION_VERSION} 里没有 CREATE 语句")
