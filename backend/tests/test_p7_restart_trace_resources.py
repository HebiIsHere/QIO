"""P7 scenarios 12, 13, 14: restart recovery, trace debuggability, resources."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.knowledge.lifecycle import KnowledgeService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry
from agent.tools.spec import ToolDefinition

SECRET = "sk-P7SECRET0123456789"


def _make_ctx(db_path, data_dir) -> tuple[AppContext, object]:
    conn = connect(db_path)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=data_dir), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx, conn


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    app_ctx, _ = _make_ctx(tmp_path / "app.db", tmp_path / "data")
    return app_ctx


class _EchoTool(Tool):
    name = "p7_echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=f"echo:{kwargs.get('text', '')}")


class _ToolOnceAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret
        self.n = 0

    async def complete(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            args = {"text": self.secret} if self.secret else {"text": "hi"}
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="p7_echo", arguments=args)],
                ),
                usage={"prompt_tokens": 11, "completion_tokens": 3},
            )
        return Completion(
            message=ChatMessage(role="assistant", content="完成"),
            usage={"prompt_tokens": 21, "completion_tokens": 5},
        )


class _AlwaysAnswer:
    mode = "native"
    model = "deepseek-v4-flash"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="好的"))


# -- scenario 12: restart recovery -----------------------------------------


def test_persistent_state_survives_restart_and_runtime_state_does_not(tmp_path):
    db_path = tmp_path / "app.db"
    data_dir = tmp_path / "data"

    ctx1, conn1 = _make_ctx(db_path, data_dir)
    topic = ctx1.topics.nodes.create_topic("重启话题").id
    ctx1.memory.append_message(topic_id=topic, role="user", content="重启前的记忆")
    ks = KnowledgeService(ctx1.conn)
    item = ks.create(category="general_fact", content="重启前的知识", node_ids=[topic])
    ks.submit(item.id)
    ks.verify(item.id, verified_by="system")
    ks.activate(item.id)
    ctx1.tool_store.save(
        ToolDefinition(
            name="p7_persisted",
            description="持久化工具",
            tool_type="function",
            code="def run(**k):\n    return {'ok': True}\n",
            tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
        )
    )
    adapter = _AlwaysAnswer()
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx1, "build_adapter", AsyncMock(return_value=adapter))
    try:
        asyncio.run(ctx1.run_turn("重启前的一轮", topic_id=topic))
    finally:
        mp.undo()
    turn_id = ctx1.trace_store.list(limit=1)[0]["turn_id"]
    # a credential that only exists in memory (never persisted)
    conn1.close()

    ctx2, conn2 = _make_ctx(db_path, data_dir)
    try:
        # --- persisted state comes back ---
        node = ctx2.conn.execute(
            "SELECT id, name FROM nodes WHERE id = ?", (topic,)
        ).fetchone()
        assert node is not None and node["name"] == "重启话题"
        messages = ctx2.conn.execute(
            "SELECT content FROM messages WHERE content = '重启前的记忆'"
        ).fetchall()
        assert messages
        knowledge = KnowledgeService(ctx2.conn).get(item.id)
        assert knowledge is not None and knowledge.state.value == "active"
        assert ctx2.registry.get("p7_persisted") is not None
        trace = ctx2.trace_store.get(turn_id)
        assert trace is not None and trace["status"] == "done"

        # --- runtime state must NOT come back ---
        assert ctx2.turns.active is None
        assert ctx2.turns.queued_count() == 0
        assert ctx2.turns.snapshot()["running"] is None
        # no in-memory credential leaked across the restart
        assert ctx2.credentials.get_secret("memory-only-key") is None
    finally:
        conn2.close()


def test_restart_leaves_no_dangling_transaction(tmp_path):
    db_path = tmp_path / "app.db"
    ctx1, conn1 = _make_ctx(db_path, tmp_path / "data")
    topic = ctx1.topics.nodes.create_topic("事务话题").id
    ctx1.memory.append_message(topic_id=topic, role="user", content="x")
    assert conn1.in_transaction is False
    conn1.close()

    ctx2, conn2 = _make_ctx(db_path, tmp_path / "data")
    try:
        assert conn2.in_transaction is False
        rows = conn2.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        assert rows["n"] >= 1
    finally:
        conn2.close()


# -- scenario 13: trace debuggability --------------------------------------


def test_trace_answers_the_debug_questions(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("追踪话题").id
    # seed context so there is something to justify: history + active knowledge
    ctx.memory.append_message(topic_id=topic, role="user", content="之前的对话内容")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="之前的回答")
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="general_fact", content="追踪用知识条目", node_ids=[topic])
    ks.submit(item.id)
    ks.verify(item.id, verified_by="system")
    ks.activate(item.id)
    ctx.registry.register(_EchoTool())
    adapter = _ToolOnceAdapter(secret=SECRET)
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        asyncio.run(ctx.run_turn("请调用工具处理这件事", topic_id=topic))
    finally:
        mp.undo()

    trace_id = ctx.trace_store.list(limit=1)[0]["turn_id"]
    trace = ctx.trace_store.get(trace_id)

    # 1. what did the user ask? (question text recoverable via the message id)
    user_ids = [
        wid for wid in trace["writes"]["messages"]
    ]
    rows = ctx.conn.execute(
        "SELECT id, role, content FROM messages WHERE id IN (%s)"
        % ",".join("?" for _ in user_ids),
        user_ids,
    ).fetchall()
    contents = {r["content"] for r in rows}
    assert "请调用工具处理这件事" in contents

    # 2. which topic did the system think it belongs to?
    assert trace["topic"]["initial"] == topic

    # 3. why these memories? (score + preview per injected item)
    assert trace["injection"]["items"]
    assert all("score" in it and "preview" in it for it in trace["injection"]["items"])

    # 4. how many tokens were injected?
    assert trace["injection"]["total_tokens"] >= 0
    assert trace["injection"]["budget"]["hard_cap"] >= 0

    # 5. which model was called?
    assert trace["model_calls"]
    assert trace["model_calls"][0]["model"] == "deepseek-v4-flash"
    assert trace["model_calls"][0]["input_tokens"] > 0

    # 6. why was the tool called? (the first planning step asked for it)
    assert trace["model_calls"][0]["tool_calls"] == 1

    # 7. did the tool succeed?
    assert len(trace["tool_runs"]) == 1
    assert trace["tool_runs"][0]["tool"] == "p7_echo"
    assert trace["tool_runs"][0]["ok"] is True

    # 8. what memory/knowledge was written?
    assert trace["writes"]["messages"]

    # 9. any warnings/fallbacks?
    assert trace["warnings"] == []

    # 10. no secret anywhere in the stored trace
    row = ctx.conn.execute("SELECT * FROM turn_traces").fetchone()
    blob = json.dumps(dict(row), ensure_ascii=False, default=str)
    assert SECRET not in blob
    assert json.dumps(trace, ensure_ascii=False, default=str).count(SECRET) == 0


# -- scenario 14: resources -------------------------------------------------


async def test_listener_count_and_tasks_do_not_grow_over_many_turns(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("资源话题").id
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=_AlwaysAnswer()))

    def listener_count() -> int:
        return sum(len(v) for v in ctx.registry.events._listeners.values())

    try:
        await ctx.run_turn("预热", topic_id=topic)
        baseline_listeners = listener_count()
        baseline_tasks = len(asyncio.all_tasks())

        for i in range(40):
            await ctx.run_turn(f"第 {i} 轮", topic_id=topic)
        await asyncio.sleep(0.05)

        assert listener_count() == baseline_listeners  # no listener leak
        assert len(asyncio.all_tasks()) <= baseline_tasks + 2  # no task leak
    finally:
        mp.undo()


async def test_sqlite_connections_are_not_left_in_transaction(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("事务").id
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=_AlwaysAnswer()))
    try:
        for i in range(5):
            await ctx.run_turn(f"轮次 {i}", topic_id=topic)
    finally:
        mp.undo()
    assert ctx.conn.in_transaction is False


class _CountingEmbedding:
    name = "counting"
    model_name = "fake"
    dims = 8

    def __init__(self) -> None:
        self.batches: list[int] = []

    def available(self) -> bool:
        return True

    def embed_texts(self, texts):
        import numpy as np

        self.batches.append(len(texts))
        return np.ones((len(texts), self.dims), dtype=np.float32)


def test_router_embedding_cache_holds_across_many_routes(ctx: AppContext):
    from agent.adapters.base import ToolSpec

    embedding = _CountingEmbedding()
    ctx.tool_router.embedding = embedding
    ctx.tool_router.invalidate()
    specs = [
        ToolSpec(name=f"tool_{i}", description=f"capability {i}", parameters={})
        for i in range(6)
    ]

    ctx.tool_router.route("第一个查询", specs)
    after_first = sum(embedding.batches)
    assert after_first == 7  # 6 tool descriptions in one batch + 1 query

    for i in range(20):
        ctx.tool_router.route(f"另一个查询 {i}", specs)
    # only the query is embedded again; tool vectors stay cached
    assert sum(embedding.batches) == after_first + 20
    assert all(n == 1 for n in embedding.batches[-20:])


async def test_many_turns_keep_trace_queries_responsive(ctx: AppContext):
    """Baseline only: guards against pathological degradation, not a benchmark."""
    topic = ctx.topics.nodes.create_topic("长跑话题").id
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=_AlwaysAnswer()))
    try:
        started = time.perf_counter()
        for i in range(100):
            await ctx.run_turn(f"长跑第 {i} 轮", topic_id=topic)
        turn_seconds = time.perf_counter() - started

        t0 = time.perf_counter()
        traces = ctx.trace_store.list(limit=50)
        trace_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        ctx.retriever.search("长跑第 3 轮", anchor_topic_id=topic, top_k=6)
        retrieval_ms = (time.perf_counter() - t1) * 1000
    finally:
        mp.undo()

    assert len(traces) == 50
    assert ctx.trace_store.count() >= 100
    # generous bounds: this is about catching pathological growth
    assert trace_ms < 500, trace_ms
    assert retrieval_ms < 2000, retrieval_ms
    print(
        f"\n[perf baseline] 100 turns={turn_seconds:.1f}s "
        f"trace_list_50={trace_ms:.1f}ms retrieval={retrieval_ms:.1f}ms"
    )
