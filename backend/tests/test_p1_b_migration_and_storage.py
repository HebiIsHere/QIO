# -*- coding: utf-8 -*-
"""Phase 1 / B：迁移 44（滚动摘要覆盖位置）+ 存取路径不再二次截断摘要正文。

验收点：
* 迁移 44 只追加 fragments 列、幂等、升级路径不丢已有行；
* 列名与 agent.memory.rolling.ROLLING_STATE_KEYS 一致（防两处漂移）；
* 存取路径读回完整摘要正文（不再按旧 2000 字上限 text[:MAX_SUMMARY] 截断）；
* 知识提炼拿到的摘要包含开头与结尾（不是只保留尾部）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.limits import MAX_SUMMARY
from agent.memory.rolling import ROLLING_STATE_KEYS
from agent.services.app import AppContext
from agent.storage import migration_p1_b
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _columns(conn) -> set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(fragments)")}


def _payload(summary: str) -> str:
    return json.dumps(
        {"title": "标题", "summary": summary, "entities": [], "keywords": []},
        ensure_ascii=False,
    )


class _ScriptedAdapter:
    mode = "native"
    model = "fake-p1b-migration"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        system = messages[0].content if not isinstance(messages[0], dict) else messages[0]["content"]
        prompt = "\n".join(
            message["content"] if isinstance(message, dict) else (message.content or "")
            for message in messages
        )
        self.calls.append((system, prompt))
        if "extracting durable knowledge" in prompt:
            return Completion(
                message=ChatMessage(role="assistant", content='{"candidates": []}')
            )
        if "你是实体提炼器" in prompt:
            return Completion(
                message=ChatMessage(role="assistant", content='{"entities": []}')
            )
        return Completion(message=ChatMessage(role="assistant", content=_payload("正常摘要")))


class _CaptureExtraction:
    mode = "native"
    model = "fake-p1b-extraction"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        self.prompts.append(
            "\n".join(
                message["content"]
                if isinstance(message, dict)
                else (message.content or "")
                for message in messages
            )
        )
        return Completion(
            message=ChatMessage(role="assistant", content='{"candidates": []}')
        )


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "migration_storage.db")
    apply_migrations(conn)
    migration_p1_b.apply(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


# ---------------------------------------------------------------------------
# 迁移 44
# ---------------------------------------------------------------------------


def test_migration_44_is_append_only_and_idempotent(db_conn):
    version, statements = migration_p1_b.MIGRATION
    assert version == 44, "迁移号由契约 §8 冻结：44 = B（滚动摘要覆盖位置/输入版本）"
    assert statements, "迁移 44 必须有 SQL"
    for statement in statements:
        assert statement.strip().upper().startswith("ALTER TABLE FRAGMENTS ADD COLUMN")
    assert set(migration_p1_b.columns()) == set(ROLLING_STATE_KEYS), (
        "迁移列名必须与 agent.memory.rolling.ROLLING_STATE_KEYS 一致"
    )
    assert set(ROLLING_STATE_KEYS).isdisjoint(_columns(db_conn))

    first = migration_p1_b.apply(db_conn)
    assert len(first) == len(statements)
    assert set(ROLLING_STATE_KEYS) <= _columns(db_conn)

    second = migration_p1_b.apply(db_conn)
    assert second == [], "重复应用必须是幂等空操作"
    assert set(ROLLING_STATE_KEYS) <= _columns(db_conn)


def test_migration_44_preserves_existing_rows(db_conn):
    now = "2026-10-21T00:00:00+00:00"
    db_conn.execute(
        "INSERT INTO nodes (id, type, name, created_at, updated_at) "
        "VALUES ('t_mig', 'topic', '迁移话题', ?, ?)",
        (now, now),
    )
    db_conn.execute(
        "INSERT INTO fragments (id, topic_id, created_at, summary_version, meta, summary) "
        "VALUES ('frag_mig', 't_mig', ?, 1, '{}', '升级前摘要')",
        (now,),
    )
    migration_p1_b.apply(db_conn)
    row = db_conn.execute(
        "SELECT summary, rolling_covered_message_id, rolling_covered_count, "
        "rolling_input_version, rolling_updated_at FROM fragments WHERE id = 'frag_mig'"
    ).fetchone()
    assert row["summary"] == "升级前摘要", "升级不得改写已有摘要"
    assert row["rolling_covered_message_id"] is None
    assert int(row["rolling_covered_count"]) == 0
    assert row["rolling_input_version"] is None
    assert row["rolling_updated_at"] is None


# ---------------------------------------------------------------------------
# 存取路径：完整摘要正文（不再 text[:MAX_SUMMARY]）
# ---------------------------------------------------------------------------


def test_stored_long_summary_is_read_back_in_full(tmp_path: Path):
    ctx = _app(tmp_path)
    topic = ctx.topics.nodes.create_topic("长摘要").id
    ctx.memory.append_message(topic_id=topic, role="user", content="用户说了很多细节")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="我记下了")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    adapter = _ScriptedAdapter()
    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    long_text = "长摘要开头" + "细节内容" * 1500 + "长摘要结尾"
    assert len(long_text) > MAX_SUMMARY, "构造的存量摘要必须超过旧 2000 字上限"
    ctx.conn.execute(
        "UPDATE fragments SET summary = ? WHERE id = ?", (long_text, sealed.id)
    )
    fragment = ctx.memory_lifecycle.fragments.get(sealed.id)
    summary = ctx.memory_lifecycle._summary_for_fragment(fragment)
    assert summary is not None
    assert summary.summary == long_text, "存取路径不得再次截断摘要正文"
    assert len(summary.summary) == len(long_text)

    capturing = _CaptureExtraction()
    extraction, error = asyncio.run(
        ctx.memory_lifecycle.extract_knowledge_candidates(capturing, summary)
    )
    assert error is None, error
    assert extraction is not None
    assert capturing.prompts, "知识提炼必须真实调用模型"
    prompt = capturing.prompts[0]
    assert long_text in prompt, "知识提炼必须拿到完整摘要（不是只保留尾部）"
    assert long_text[:20] in prompt and long_text[-20:] in prompt
