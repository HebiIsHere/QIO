"""P7 scenarios 4 & 5: updated decisions win; stable info survives recency.

Scenario 4: "we decided PostgreSQL" → later "final decision: SQLite, the
PostgreSQL plan is dropped" → several unrelated recent messages → ask what the
decision is. The superseded fact must not be presented as current.

Scenario 5: an old but stable preference must not be pushed out purely by
newer, unrelated chatter.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.knowledge.lifecycle import KnowledgeService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _CapturingAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, answer: str = "最终是 SQLite") -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        self.calls.append("\n".join(m.content or "" for m in messages))
        return Completion(message=ChatMessage(role="assistant", content=self.answer))

    @property
    def first_request(self) -> str:
        return self.calls[0]


def _activate(ks: KnowledgeService, item_id: str) -> None:
    ks.submit(item_id)
    ks.verify(item_id, verified_by="system")
    ks.activate(item_id)


async def test_superseded_decision_is_not_presented_as_current(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("数据库选型").id
    ks = KnowledgeService(ctx.conn)

    postgres = ks.create(
        category="general_fact",
        content="项目暂定使用 PostgreSQL",
        node_ids=[topic],
        provenance={"fragment_id": "f1"},
    )
    _activate(ks, postgres.id)

    sqlite = ks.create(
        category="general_fact",
        content="最终决定改用 SQLite，前面的 PostgreSQL 方案废弃",
        node_ids=[topic],
        provenance={"fragment_id": "f2"},
        supersedes_id=postgres.id,
    )
    _activate(ks, sqlite.id)

    # the old item is no longer active
    assert ks.get(postgres.id).state.value == "revoked"
    # the version chain is walkable
    chain = [item.id for item in ks.history(sqlite.id)]
    assert sqlite.id in chain and postgres.id in chain

    # several newer, unrelated messages
    for i in range(6):
        ctx.memory.append_message(topic_id=topic, role="user", content=f"今天午饭吃了面 {i}")
        ctx.memory.append_message(topic_id=topic, role="assistant", content=f"好的 {i}")

    adapter = _CapturingAdapter()
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await ctx.run_turn("数据库最终定的是什么？", topic_id=topic)
    finally:
        mp.undo()

    prompt = adapter.first_request
    assert "SQLite" in prompt, prompt
    # the revoked plan must not be injected as current knowledge
    knowledge_lines = [ln for ln in prompt.splitlines() if ln.startswith("[知识·")]
    assert knowledge_lines, prompt
    # the *superseded statement* must not be injected as current knowledge.
    # (The newer line may legitimately mention PostgreSQL to say it was dropped.)
    assert not any("项目暂定使用 PostgreSQL" in ln for ln in knowledge_lines), knowledge_lines
    assert any("最终决定改用 SQLite" in ln for ln in knowledge_lines), knowledge_lines

    # trace records the retrieval/injection that produced this answer
    trace = ctx.trace_store.get(ctx.trace_store.list(limit=1)[0]["turn_id"])
    injected_ids = {item["item_id"] for item in trace["injection"]["items"]}
    assert sqlite.id in injected_ids
    assert postgres.id not in injected_ids


async def test_stable_user_knowledge_survives_recent_chatter(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("杂谈").id
    user_node = ctx._user_root_id()
    ks = KnowledgeService(ctx.conn)

    pref = ks.create(
        category="general_fact",
        content="用户偏好：回答先给结论，再给理由",
        node_ids=[user_node],
    )
    _activate(ks, pref.id)

    for i in range(20):
        ctx.memory.append_message(topic_id=topic, role="user", content=f"随便聊聊 {i}")
        ctx.memory.append_message(topic_id=topic, role="assistant", content=f"嗯 {i}")

    adapter = _CapturingAdapter(answer="先给结论")
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await ctx.run_turn("你打算怎么回答我？", topic_id=topic)
    finally:
        mp.undo()

    assert "回答先给结论" in adapter.first_request, adapter.first_request
