# -*- coding: utf-8 -*-
"""F 组独立验证：R02（知识版本链唯一 active）、R03（纠正激活失败必须整体回滚）。

只依据可观察行为断言（SQLite 行、active 数量、工具返回），不引用任何组的
内部实现细节，也不 import 尚未存在的新模块作为断言前提。

* R02 —— 连续两次纠正**同一个旧版本**（旧注入快照还指着它）之后，该版本链上
  只能有一个 active；两个连接各纠正一次时也只允许一个生效。
* R03 —— 在新版本「变成 active」的那次写入处注入一次真实 SQLite 写失败：
  旧版本必须仍然是 active（整体回滚），且这次纠正不得报成成功；故障解除之后
  正常纠正只有新版本生效，替换历史仍可追踪。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent.knowledge.lifecycle import KnowledgeService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.knowledge_correction import CorrectKnowledgeTool

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _chain_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, state, supersedes_id, content FROM knowledge"
        ).fetchall()
    ]


def _component(conn: sqlite3.Connection, seed_id: str) -> set[str]:
    """从 seed 出发、沿 supersedes_id 双向可达的所有版本（一条链 = 一个分量）。"""
    rows = _chain_rows(conn)
    by_id = {r["id"]: r for r in rows}
    children: dict[str, list[str]] = {}
    for row in rows:
        if row["supersedes_id"]:
            children.setdefault(row["supersedes_id"], []).append(row["id"])
    seen: set[str] = set()
    stack = [seed_id]
    while stack:
        current = stack.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        parent = by_id[current]["supersedes_id"]
        if parent:
            stack.append(parent)
        stack.extend(children.get(current, []))
    return seen


def _active_in_chain(conn: sqlite3.Connection, seed_id: str) -> set[str]:
    rows = {r["id"]: r for r in _chain_rows(conn)}
    return {i for i in _component(conn, seed_id) if rows[i]["state"] == "active"}


def _active_knowledge(conn: sqlite3.Connection, content: str) -> str:
    """建一条 active 知识（走公开生命周期入口）。"""
    ks = KnowledgeService(conn)
    item = ks.create(category="general_fact", content=content, node_ids=["user:rmf"])
    ks.submit(item.id)
    ks.verify(item.id)
    ks.activate(item.id)
    got = ks.get(item.id)
    assert got is not None and got.state.value == "active", got
    return item.id


def _stale_snapshot(item_id: str, content: str):
    """旧注入快照：仍然指着已经过时的版本（真实场景里就是模型刚看到的那份）。"""
    return lambda: [{"item_id": item_id, "content": content}]


def _correct(conn: sqlite3.Connection, item_id: str, content: str, new_content: str):
    """跑一次纠正；异常当作「明确失败」收下来（不掩盖，也不让用例崩掉）。"""
    tool = CorrectKnowledgeTool(conn, _stale_snapshot(item_id, content))
    try:
        return asyncio.run(tool.run(content=content, new_content=new_content))
    except Exception as exc:  # noqa: BLE001 - 冲突/写失败都必须如实收下
        return exc


# ---------------------------------------------------------------------------
# R02
# ---------------------------------------------------------------------------


def test_r02_second_correction_of_the_same_old_version_never_yields_two_actives(tmp_path: Path):
    conn = connect(tmp_path / "kn.db")
    apply_migrations(conn)
    try:
        old_id = _active_knowledge(conn, "用户喜欢咖啡")

        first = _correct(conn, old_id, "用户喜欢咖啡", "用户喜欢拿铁")
        assert not isinstance(first, Exception) and first.ok, first

        # 第二次纠正的匹配来源仍是**旧快照**（模型看到的那条还没更新的版本）
        second = _correct(conn, old_id, "用户喜欢咖啡", "用户喜欢美式")

        actives = _active_in_chain(conn, old_id)
        assert len(actives) == 1, (
            "同一个版本链只能有一个 active：第二次纠正旧版本不得再产生一个 active；"
            f"second={second!r}, actives={sorted(actives)}"
        )
    finally:
        conn.close()


def test_r02_two_connections_correcting_the_same_version_keep_one_active(tmp_path: Path):
    db_path = tmp_path / "kn2.db"
    conn1 = connect(db_path)
    apply_migrations(conn1)
    conn2 = connect(db_path)
    try:
        old_id = _active_knowledge(conn1, "用户住在杭州")

        first = _correct(conn1, old_id, "用户住在杭州", "用户住在南京")
        second = _correct(conn2, old_id, "用户住在杭州", "用户住在苏州")

        actives = _active_in_chain(conn1, old_id)
        assert len(actives) == 1, (
            "两个连接并发纠正同一个版本时，只能有一个有效后继（另一个必须明确冲突）；"
            f"first={first!r}, second={second!r}, actives={sorted(actives)}"
        )
    finally:
        conn1.close()
        conn2.close()


# ---------------------------------------------------------------------------
# R03
# ---------------------------------------------------------------------------


class _FlakyActivationConn(sqlite3.Connection):
    """在「把某个版本写成 active」的写入处注入一次真实 SQLite 失败。"""

    inject_activation_failure = False

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        text = " ".join(str(sql).split()).lower()
        params = args[0] if args else ()
        values = " ".join(str(v) for v in (params or ()))
        writes_knowledge = text.startswith(("update knowledge", "insert into knowledge"))
        if (
            self.inject_activation_failure
            and writes_knowledge
            and ("'active'" in text or '"active"' in text or "active" in values)
        ):
            self.inject_activation_failure = False  # 只注入一次
            raise sqlite3.OperationalError("disk I/O error (injected by rm-f)")
        return super().execute(sql, *args, **kwargs)


def _flaky_connect(db_path: Path) -> _FlakyActivationConn:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path), check_same_thread=False, factory=_FlakyActivationConn
    )
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def test_r03_failed_activation_keeps_the_old_version_active(tmp_path: Path):
    conn = _flaky_connect(tmp_path / "kn3.db")
    apply_migrations(conn)
    try:
        old_id = _active_knowledge(conn, "用户的猫叫做球球")

        conn.inject_activation_failure = True
        failed = _correct(conn, old_id, "用户的猫叫做球球", "用户的猫叫做团团")

        actives = _active_in_chain(conn, old_id)
        assert actives == {old_id}, (
            "新版本激活写失败时必须整体回滚：旧版本仍然是唯一的 active；"
            f"failed={failed!r}, actives={sorted(actives)}"
        )
        assert not (not isinstance(failed, Exception) and getattr(failed, "ok", False)), (
            f"激活写失败不得报成成功：{failed!r}"
        )

        # 故障解除后正常纠正：只有新版本生效，替换历史可追踪
        ok = _correct(conn, old_id, "用户的猫叫做球球", "用户的猫叫做团团")
        assert not isinstance(ok, Exception) and ok.ok, ok
        actives_after = _active_in_chain(conn, old_id)
        assert len(actives_after) == 1 and old_id not in actives_after, (
            f"解除故障后应当只有新版本 active：{sorted(actives_after)}"
        )
        assert len(_component(conn, old_id)) >= 2, "替换历史必须仍可追踪（链上不止一个版本）"
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
