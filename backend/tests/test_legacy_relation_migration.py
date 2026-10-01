# -*- coding: utf-8 -*-
"""旧数据的 relation_type='unknown'：迁移是否无损、是否会被错误当成路径前提。

背景（阶段 3 迁移 16）：relation_type 是新列，旧行只能是 NULL。迁移按「能确认到
什么程度」分两档：
  * 有 source_fragment_id 且来源合法、同话题 → history_reopen（从历史继续）；
  * 其余 → unknown（分不清容量分段还是阶段变化，**不猜**）。

本文件验证三件事：
1. 迁移只做可确认的推断：坏来源（不存在 / 跨话题）被断开并落 unknown，
   合法来源保留并落 history_reopen；
2. unknown 行不会因为「关系未知」被编造出路径 —— 它没有来源，就不会成为
   任何片段的祖先，也就不可能作为【路径前提】扩大上下文；
3. 产品侧的隔离依赖的是 source_fragment_id + 话题校验（不是 relation_type），
   unknown 行最多以「同话题其他片段·仅参考」的身份出现。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.schema import MIGRATIONS

LEGACY_VERSION = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_db(tmp_path, version: int = LEGACY_VERSION):
    """造一个「还停在旧 schema 版本」的库（用产品自己的迁移语句，不带 bookkeeping）。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > version:
            break
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (target, _now())
        )
    conn.commit()
    return conn


def _topic(conn, node_id: str, name: str) -> str:
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', ?, ?)",
        (node_id, name, _now(), _now()),
    )
    return node_id


def _legacy_fragment(conn, fragment_id: str, topic_id: str, source: str | None, summary: str | None = None) -> None:
    """旧行：relation_type / boundary_reason / same_stage 全是 NULL。"""
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at, meta, "
        " source_fragment_id, content_version) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 0)",
        (fragment_id, topic_id, summary, 1 if summary else 0, _now(), _now(), source),
    )


def _rows(conn):
    return {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, topic_id, source_fragment_id, relation_type, same_stage FROM fragments"
        ).fetchall()
    }


def test_migration_only_infers_what_is_verifiable(tmp_path):
    conn = _legacy_db(tmp_path)
    _topic(conn, "t_a", "话题A")
    _topic(conn, "t_b", "话题B")
    # 合法链：a1 -> a2
    _legacy_fragment(conn, "a1", "t_a", None, "A 的第一段")
    _legacy_fragment(conn, "a2", "t_a", "a1", "A 的第二段")
    # 坏来源：指向不存在 / 指向别的话题；无来源：容量分段还是阶段变化分不清
    _legacy_fragment(conn, "dangling", "t_a", "ghost")
    _legacy_fragment(conn, "cross", "t_b", "a1")
    _legacy_fragment(conn, "plain", "t_a", None)
    conn.commit()

    apply_migrations(conn)
    rows = _rows(conn)

    assert rows["a2"]["relation_type"] == "history_reopen"
    assert rows["a2"]["source_fragment_id"] == "a1", "能确认的来源必须原样保留（无损迁移）"
    assert rows["a1"]["relation_type"] == "unknown"
    assert rows["plain"]["relation_type"] == "unknown"
    assert rows["dangling"]["source_fragment_id"] is None
    assert rows["dangling"]["relation_type"] == "unknown"
    assert rows["cross"]["source_fragment_id"] is None, "跨话题来源不能留着假装是路径"
    assert rows["cross"]["relation_type"] == "unknown"
    # 不猜：unknown 行不得被编造出 same_stage 或来源
    for fid in ("a1", "plain", "dangling", "cross"):
        assert rows[fid]["same_stage"] is None
    conn.close()


def test_unknown_legacy_fragment_never_becomes_a_path_premise(tmp_path):
    conn = _legacy_db(tmp_path)
    _topic(conn, "t_a", "话题A")
    _topic(conn, "t_b", "话题B")
    _legacy_fragment(conn, "old_unknown", "t_b", "a1", "别的话题的旧片段")
    _legacy_fragment(conn, "a1", "t_a", None, "A 的第一段")
    _legacy_fragment(conn, "a2", "t_a", "a1", "A 的第二段")
    conn.commit()
    apply_migrations(conn)

    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = None
    items = ctx.context_assembler.short_term_items("t_a", fragment_id="a2")
    text = "\n".join(item.text for item in items)
    # 按整块（标题 + 正文）判断：只看标题行是查不出问题的
    # —— 摘要正文在标题的下一行，旧测试的「不在标题行里」断言其实一直是空转。
    import re

    premise_blocks = [b for b in re.split(r"(?=【)", text) if b.startswith("【路径前提")]
    assert any("A 的第一段" in block for block in premise_blocks), "合法来源仍必须作为前提出现"
    assert not any("别的话题的旧片段" in block for block in premise_blocks), (
        "unknown / 跨话题的旧片段绝不能作为路径前提"
    )
    conn.close()


def test_unknown_row_cannot_be_widened_by_relation_type_reads(tmp_path):
    """隔离靠 source_fragment_id + 话题校验，不是 relation_type。

    这条断言是防回归用的：如果有人以后改成「按 relation_type 决定是否当祖先」，
    必须同时保证 unknown 仍然不被当成前提 —— 这里用真实调用把行为钉住。
    """
    conn = _legacy_db(tmp_path)
    _topic(conn, "t_a", "话题A")
    _legacy_fragment(conn, "u1", "t_a", None, "未知关系的旧片段")
    _legacy_fragment(conn, "u2", "t_a", None, "另一条未知旧片段")
    conn.commit()
    apply_migrations(conn)

    from agent.memory.fragment import FragmentManager

    fragments = FragmentManager(conn)
    assert fragments.source_of("u1") is None
    assert fragments.ancestors("u1", max_depth=5) == []
    # 即使有人手工把 unknown 行串起来，也必须走话题校验（跨话题直接拒绝）
    _topic(conn, "t_b", "话题B")
    _legacy_fragment(conn, "other", "t_b", None, "别的话题")
    conn.commit()
    with pytest.raises(ValueError):
        fragments.validate_source("t_a", "other")
    conn.close()
