# -*- coding: utf-8 -*-
"""D 独立契约测试：迁移只追加（docs/qio-refactor-contract.md §8）。

覆盖：历史迁移不得改写（冻结内容哈希）、编号只追加且严格递增、本轮新增迁移
（B 的 44 起）已在 MIGRATIONS 末尾登记、新库安装 / 存量库升级 / 重复应用都不报错。

口径：不检查实现内部函数；只检查 storage.schema.MIGRATIONS 的公开形状与
storage.migrate.apply_migrations 的实际行为。阶段 1（B 的实现未合并）允许失败。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_migration_contract.py -q
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from agent.storage import migrate
from agent.storage.schema import MIGRATIONS, SCHEMA_VERSION


# d265d033（契约冻结提交）里迁移 1..43 的规范化 SQL 摘要。
# 契约 §8：只允许在 MIGRATIONS 末尾追加；不得改历史迁移语义。
# 该哈希在阶段 1 基线（d265d033）上用同一规范化函数算出。
BASELINE_HISTORY_SHA256 = "533425bc0b33a465ca90831e01a70a8a85377291fc3be8ee5d03e9daf170ec85"
BASELINE_MAX_MIGRATION = 43


def _norm_statements(statements) -> tuple[str, ...]:
    """规范化空白（不改变语义），避免行宽/缩进调整造成误报。"""
    return tuple(" ".join(str(stmt).split()) for stmt in statements)


def _history_digest(migrations) -> str:
    digest = hashlib.sha256()
    for number, statements in migrations:
        digest.update((str(number) + "\x1f").encode())
        for stmt in _norm_statements(statements):
            digest.update(stmt.encode())
            digest.update(b"\x1e")
    return digest.hexdigest()


def _numbers() -> list[int]:
    return [int(number) for number, _ in MIGRATIONS]


def test_historical_migrations_are_append_only():
    """§8：历史迁移（1..43）内容不可变，只能在末尾追加。"""
    history = [(number, statements) for number, statements in MIGRATIONS if number <= BASELINE_MAX_MIGRATION]
    assert _history_digest(history) == BASELINE_HISTORY_SHA256, (
        "契约 §8：检测到历史迁移（编号 <= "
        f"{BASELINE_MAX_MIGRATION}）的语义/语句被改写，迁移只允许在 MIGRATIONS 末尾追加。"
        "若确实改了历史迁移，必须由 Lead 明确裁决并在契约/台账记录。"
    )


def test_migration_numbers_are_strictly_increasing_and_unique():
    """§8：MIGRATIONS 编号严格递增、不重复（追加语义）。"""
    numbers = _numbers()
    assert numbers == sorted(set(numbers)), (
        f"契约 §8：MIGRATIONS 编号不是严格递增/有重复：{numbers}"
    )
    if MIGRATIONS:
        assert numbers[0] == 1, "历史迁移应从 1 开始（编号被改动？）"


def test_new_migration_44_is_registered_and_well_formed():
    """§8：B 的滚动摘要覆盖位置 / 输入版本持久化迁移 44 必须已登记。"""
    numbers = _numbers()
    assert max(numbers) >= BASELINE_MAX_MIGRATION + 1, (
        "契约 §8：本轮新增迁移尚未登记（B 的迁移 44 起）；"
        f"当前最大迁移号={max(numbers)}，基线={BASELINE_MAX_MIGRATION}"
    )
    appended = [(n, s) for n, s in MIGRATIONS if n > BASELINE_MAX_MIGRATION]
    assert appended, "契约 §8：没有找到新增迁移（编号 > 43）"
    for number, statements in appended:
        assert statements, f"契约 §8：迁移 {number} 没有 SQL 语句"
        for stmt in statements:
            assert str(stmt).strip(), f"契约 §8：迁移 {number} 含空语句"
    assert SCHEMA_VERSION == max(numbers), (
        "契约 §8：SCHEMA_VERSION 必须收敛到最大迁移号；"
        f"SCHEMA_VERSION={SCHEMA_VERSION}, max={max(numbers)}"
    )


def _fresh_conn(tmp_path, name: str = "fresh.db") -> sqlite3.Connection:
    from agent.storage.db import connect

    return connect(tmp_path / name)


def test_fresh_install_applies_to_max_and_is_idempotent(tmp_path):
    """§8：新库安装到最大版本；重复应用不报错（迁移必须可重复执行）。"""
    conn = _fresh_conn(tmp_path)
    try:
        version = migrate.apply_migrations(conn)
        numbers = _numbers()
        assert version == max(numbers), (
            f"契约 §8：新库安装后的版本={version}，应为最大迁移号 {max(numbers)}"
        )
        assert migrate.current_version(conn) == version
        again = migrate.apply_migrations(conn)
        assert again == version, "重复应用迁移返回的版本不一致"
        assert migrate.current_version(conn) == version
    finally:
        conn.close()


def _apply_history_up_to(conn: sqlite3.Connection, ceiling: int) -> None:
    """构造「存量库」：只应用编号 <= ceiling 的历史迁移（与基线 d265d033 一致）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for number, statements in MIGRATIONS:
        if number > ceiling:
            break
        with migrate.transaction(conn):
            migrate._apply_statements(conn, number, statements)  # noqa: SLF001 - 构造存量库
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (number, "2026-01-01T00:00:00+00:00"),
            )


def test_existing_database_upgrades_from_43_to_latest(tmp_path):
    """§8：已到 43 的存量库必须能追加升级到本轮最新版本，不丢对象、不报错。"""
    conn = _fresh_conn(tmp_path, "upgrade.db")
    try:
        _apply_history_up_to(conn, BASELINE_MAX_MIGRATION)
        assert migrate.current_version(conn) == BASELINE_MAX_MIGRATION
        version = migrate.apply_migrations(conn)
        assert version >= BASELINE_MAX_MIGRATION + 1, (
            "契约 §8：存量库升级后版本没有前进（新增迁移未生效）"
        )
        assert migrate.current_version(conn) == version
        missing = migrate.missing_objects(conn)
        assert not any(missing.values()), (
            "契约 §8：升级后 schema 必需对象缺失："
            f"{ {k: v for k, v in missing.items() if v} }"
        )
    finally:
        conn.close()
