"""测试基础设施自测：模板数据库（backend/tests/conftest.py）。

这些用例不是在测业务，而是在证明「测试提速基础设施」本身没有悄悄改变语义：
* 模板确实来自当前代码与真实迁移（对比表 / schema_version）；
* 克隆走 sqlite3 backup API（spy 计数），克隆出的两个库互不影响；
* 克隆连接的外键 / WAL / busy_timeout 与生产 connect() 一致；
* real_migrations 用例确实逐次调用真实迁移（spy 计数，不是看模板长什么样）；
* 模板只读，文件只落在 pytest 临时目录内，连接关闭后不遗留 WAL 文件。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

import conftest
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import SCHEMA_VERSION


def _object_rows(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {
        (str(row[0]), str(row[1]), str(row[2]))
        for row in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def test_template_is_built_from_current_migrations(db_template, tmp_path, tmp_path_factory):
    """模板结构必须等于「当前代码对空库真实迁移」的结果（表/索引/视图 + 版本号）。"""
    assert db_template.schema_version == SCHEMA_VERSION
    real = conftest.build_real_migration_connection(tmp_path / "real.db")
    try:
        assert current_version(real) == db_template.schema_version
        assert _object_rows(real) == _object_rows(db_template.source), (
            "模板结构与当前迁移结果不一致：模板过期或生成路径被绕过"
        )
    finally:
        real.close()

    # 模板放 pytest 的 worker 专属 basetemp 里，不进仓库、也不与其它 worker 共享。
    basetemp = Path(tmp_path_factory.getbasetemp())
    assert db_template.path.parent.parent == basetemp
    repo_root = Path(conftest.__file__).resolve().parents[2]
    assert repo_root not in db_template.path.parents, "模板不得写进仓库"


def test_clone_uses_sqlite_backup_api(db_template, tmp_path, monkeypatch):
    """克隆必须走 sqlite3 backup API（行为 spy），不是直接复制可能未 checkpoint 的文件。"""
    calls: list[object] = []
    real_backup = db_template.source.backup

    def spy_backup(dest, **kwargs):  # noqa: ANN001, ANN003
        calls.append(dest)
        return real_backup(dest, **kwargs)

    wrapped = dataclasses.replace(db_template, source=SimpleNamespace(backup=spy_backup))
    clone = conftest.clone_template_db(wrapped, tmp_path / "clone.db")
    try:
        assert len(calls) == 1, f"backup API 应恰好被调用一次，实际 {len(calls)}"
        assert current_version(clone) == SCHEMA_VERSION
    finally:
        clone.close()


def test_two_clones_are_independent_and_keep_production_pragmas(db_template, tmp_path):
    """两个克隆互不影响，且连接设置与生产连接一致（外键 / WAL / busy_timeout）。"""
    first = conftest.clone_template_db(db_template, tmp_path / "a.db")
    second = conftest.clone_template_db(db_template, tmp_path / "b.db")
    try:
        for conn in (first, second):
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            assert current_version(conn) == db_template.schema_version

        first.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('infra-only-a', '1', '2026-11-10T00:00:00+00:00')"
        )
        assert (
            second.execute("SELECT COUNT(*) FROM settings WHERE key = 'infra-only-a'").fetchone()[0]
            == 0
        ), "两个克隆共用了同一份数据库文件"
        assert (
            db_template.source.execute(
                "SELECT COUNT(*) FROM settings WHERE key = 'infra-only-a'"
            ).fetchone()[0]
            == 0
        ), "测试写入泄漏进了只读模板"
    finally:
        first.close()
        second.close()


def test_template_is_readonly_and_clone_files_stay_in_test_tmp(db_template, tmp_path):
    """模板只读；克隆文件只落在本用例 tmp_path，关连接后不遗留 WAL 文件。"""
    with pytest.raises(sqlite3.OperationalError):
        db_template.source.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('x', '1', '2026-11-10T00:00:00+00:00')"
        )

    clone_path = tmp_path / "test.db"
    conn = conftest.clone_template_db(db_template, clone_path)
    assert clone_path.parent == tmp_path
    conn.close()
    assert not Path(str(clone_path) + "-wal").exists(), "最后一条连接关闭后不应遗留 WAL 文件"


@pytest.mark.real_migrations
def test_marked_case_gets_a_real_migration_connection(db_conn, db_conn_events, request):
    """real_migrations 用例的 db_conn 必须是逐次真实迁移，不能是模板克隆。"""
    events = [event for event in db_conn_events if event["nodeid"] == request.node.nodeid]
    assert events, "db_conn fixture 没有记录事件"
    assert events[-1]["mode"] == "real_migrations"


def test_unmarked_business_case_gets_a_template_clone(db_conn, db_conn_events, request):
    """普通业务用例走模板克隆（这里同时证明模板路径真的被使用）。"""
    events = [event for event in db_conn_events if event["nodeid"] == request.node.nodeid]
    assert events, "db_conn fixture 没有记录事件"
    assert events[-1]["mode"] == "template_clone"


def test_spy_proves_real_migration_path_calls_apply_migrations(monkeypatch, tmp_path):
    """真实迁移路径确实调用 apply_migrations（spy 计数，而不是检查模板内容）。"""
    calls: list[sqlite3.Connection] = []
    real = conftest.apply_migrations

    def spy(conn):  # noqa: ANN001
        calls.append(conn)
        return real(conn)

    monkeypatch.setattr(conftest, "apply_migrations", spy)
    conn = conftest.build_real_migration_connection(tmp_path / "spy.db")
    try:
        assert len(calls) == 1, f"apply_migrations 应被调用一次，实际 {len(calls)}"
        assert current_version(conn) == SCHEMA_VERSION, "spy 之后真实迁移仍必须实际发生"
    finally:
        conn.close()
