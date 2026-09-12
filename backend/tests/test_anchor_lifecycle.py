"""Anchor 生命周期：StartHere → 消费 → 位置推进；Topic 往返位置恢复。

规则（本轮确定的产品语义，见 docs/architecture.md「Anchor 生命周期」）：
- 用户「从这里开始」选中的历史片段，只在下一次（或下一次成功）Turn 提供 Focus；
- 一轮 **成功** 后，当前位置推进到本轮消息真正写入的片段，历史选择就此消费；
- 失败 / 取消的 Turn 不消费用户选择（重试仍能拿到同一历史位置）；
- 切回一个已有历史位置的话题时，恢复该话题最近的位置（而不是清空）；
- memory_search 只读，绝不改变 anchor。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.topic_tools import SwitchTopicTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str = "测试话题") -> str:
    return ctx.topics.nodes.create_topic(name).id


def _seed_fragment(
    ctx: AppContext,
    topic_id: str,
    *,
    closed: bool = True,
    title: str = "历史片段",
    messages: tuple[tuple[str, str], ...] = (("user", "历史消息"), ("assistant", "历史回答")),
) -> str:
    """写入一个片段（可选封块 + 索引行），返回 fragment_id。"""
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (frag_id, topic_id, f"{title} 的摘要", 1, now, now if closed else None),
    )
    first = None
    last = None
    for i, (role, content) in enumerate(messages):
        msg_id = f"{frag_id}_m{i}"
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?, 'text', ?)",
            (msg_id, frag_id, role, content, now),
        )
        first = first or msg_id
        last = msg_id
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (first, last, frag_id),
    )
    if closed:
        ctx.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
            "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (new_id("idx"), frag_id, topic_id, title, now),
        )
    ctx.conn.commit()
    return frag_id


class CapturingAdapter:
    """记录每次 complete() 实际看到的 prompt，便于断言注入了什么。"""

    mode = "native"
    model = "fake-model"

    def __init__(self, *, final: str = "好的，我继续。", fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.final = final
        self.fail = fail
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.prompts.append("\n".join(m.content or "" for m in messages))
        if self.fail:
            raise RuntimeError("planning failed")
        return Completion(message=ChatMessage(role="assistant", content=self.final))


def _install_adapter(ctx: AppContext, monkeypatch, adapter: CapturingAdapter) -> CapturingAdapter:
    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    return adapter


def _cursor_rows(ctx: AppContext) -> list[tuple]:
    return [
        tuple(r)
        for r in ctx.conn.execute(
            "SELECT anchor_type, topic_id, fragment_id FROM cursor ORDER BY anchor_type, updated_at"
        ).fetchall()
    ]


# -- Case 1: StartHere 后的下一轮必须拿到该片段 Focus --------------------


async def test_start_here_focus_injected_next_turn(ctx: AppContext, monkeypatch):
    topic = _topic(ctx, "锚点话题")
    payload = "PAYLOAD-ANCHOR-A13"
    frag = _seed_fragment(
        ctx, topic, title="Anchor 生命周期", messages=(("user", payload), ("assistant", "记下了"))
    )
    AnchorService(ctx.conn).set_active(topic, frag)

    adapter = _install_adapter(ctx, monkeypatch, CapturingAdapter())
    result = await ctx.run_turn("接着聊", topic_id=topic)

    assert result["ok"] is True
    assert adapter.prompts, "turn 必须真正调用过模型"
    assert payload in adapter.prompts[0], "StartHere 选中的片段必须进入本轮 Focus"
    assert "聚焦片段" in adapter.prompts[0]


# -- Case 2: 成功一轮后位置推进，不再每轮重复同一历史片段 ----------------


async def test_successful_turn_advances_position_and_stops_repeating(ctx: AppContext, monkeypatch):
    topic = _topic(ctx, "推进话题")
    payload = "PAYLOAD-ANCHOR-A13"
    frag13 = _seed_fragment(
        ctx, topic, title="Anchor 生命周期", messages=(("user", payload), ("assistant", "记下了"))
    )
    AnchorService(ctx.conn).set_active(topic, frag13)

    adapter = _install_adapter(ctx, monkeypatch, CapturingAdapter())
    await ctx.run_turn("第一轮", topic_id=topic)
    assert payload in adapter.prompts[0]

    active_after = AnchorService(ctx.conn).get_active()
    assert active_after.topic_id == topic
    assert active_after.fragment_id not in (None, frag13), "成功一轮后位置应推进到本轮片段"

    adapter.prompts.clear()
    await ctx.run_turn("第二轮", topic_id=topic)
    assert adapter.prompts, "第二轮必须真实发生"
    assert payload not in adapter.prompts[0], "历史选择已被消费，不得每轮重复注入"


# -- Case 3: 失败 Turn 不消费用户选择 ------------------------------------


async def test_failed_turn_keeps_anchor_for_retry(ctx: AppContext, monkeypatch):
    topic = _topic(ctx, "失败话题")
    payload = "PAYLOAD-RETRY"
    frag = _seed_fragment(ctx, topic, messages=(("user", payload),))
    AnchorService(ctx.conn).set_active(topic, frag)

    failing = _install_adapter(ctx, monkeypatch, CapturingAdapter(fail=True))
    result = await ctx.run_turn("这会失败", topic_id=topic)
    assert result["ok"] is False
    assert AnchorService(ctx.conn).get_active().fragment_id == frag, "失败不得消费历史选择"

    retry = _install_adapter(ctx, monkeypatch, CapturingAdapter())
    result2 = await ctx.run_turn("重试", topic_id=topic)
    assert result2["ok"] is True
    assert payload in retry.prompts[0], "重试仍应获得同一历史位置"
    assert failing.calls >= 1


# -- Case 4: 取消的 Turn 不消费用户选择 ----------------------------------


async def test_cancelled_turn_keeps_anchor(ctx: AppContext, monkeypatch):
    topic = _topic(ctx, "取消话题")
    frag = _seed_fragment(ctx, topic, messages=(("user", "PAYLOAD-CANCEL"),))
    AnchorService(ctx.conn).set_active(topic, frag)

    adapter = _install_adapter(ctx, monkeypatch, CapturingAdapter())

    original_persist = ctx.turn_orchestrator.persist

    async def cancel_then_persist(turn_ctx, ad, plan, result):
        turn_ctx.cancelled = True  # 模拟取消：turn 被标记取消后仍走到收尾阶段
        return await original_persist(turn_ctx, ad, plan, result)

    monkeypatch.setattr(ctx.turn_orchestrator, "persist", cancel_then_persist)
    result = await ctx.run_turn("被取消的一轮", topic_id=topic)
    assert result["ok"] is True

    active = AnchorService(ctx.conn).get_active()
    assert active.topic_id == topic
    assert active.fragment_id == frag, "取消的 turn 不得消费用户的历史位置"
    assert adapter.calls >= 1


# -- Case 5/6/7: Topic 往返位置恢复 + 用户选择优先 ------------------------


def test_switch_back_restores_topic_position(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag13 = _seed_fragment(ctx, a)
    anchors = AnchorService(ctx.conn)
    anchors.set_active(a, frag13)

    tool = SwitchTopicTool(ctx.conn)
    assert tool.run_sync(topic_id=b).ok
    assert anchors.get_active().topic_id == b
    assert anchors.get_active().fragment_id is None, "新话题没有历史位置时为 None"

    assert tool.run_sync(topic_id=a).ok
    restored = anchors.get_active()
    assert restored.topic_id == a
    assert restored.fragment_id == frag13, "切回话题必须恢复该话题保存的位置"


def test_user_choice_beats_older_topic_position(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag27 = _seed_fragment(ctx, a, title="旧位置")
    frag13 = _seed_fragment(ctx, a, title="用户选中")
    anchors = AnchorService(ctx.conn)
    anchors.set_active(a, frag27)
    anchors.set_active(a, frag13)  # 用户在 Planet 明确选择 13

    SwitchTopicTool(ctx.conn).run_sync(topic_id=b)
    SwitchTopicTool(ctx.conn).run_sync(topic_id=a)
    assert anchors.get_active().fragment_id == frag13, "用户当前明确选择优先于更早的自动位置"


def test_topic_without_fragment_history_switches_cleanly(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    anchors = AnchorService(ctx.conn)
    anchors.set_active(a)  # 只有话题，没有片段位置
    assert SwitchTopicTool(ctx.conn).run_sync(topic_id=b).ok
    assert anchors.get_active().fragment_id is None
    assert SwitchTopicTool(ctx.conn).run_sync(topic_id=a).ok
    assert anchors.get_active().fragment_id is None


# -- Case 8: 损坏 / 跨话题的 anchor 安全降级 -----------------------------


def test_position_fragment_validates_ownership(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag = _seed_fragment(ctx, a)
    anchors = AnchorService(ctx.conn)

    anchors.set_active(a, frag)
    assert anchors.position_fragment(a) == frag
    assert anchors.position_fragment(b) is None, "别的 topic 不得拿到该片段"

    # 位置指向不存在的片段（历史遗留 / 外部破坏的数据，FK 关闭才能写入）→ 降级为 None
    ctx.conn.execute("PRAGMA foreign_keys = OFF")
    ctx.conn.execute(
        "UPDATE cursor SET fragment_id = 'frag_ghost' WHERE anchor_type = 'active'"
    )
    ctx.conn.execute("PRAGMA foreign_keys = ON")
    ctx.conn.commit()
    assert anchors.position_fragment(a) is None
    assert anchors.position_fragment(b) is None


def test_switch_with_stale_fragment_degrades_to_none(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag = _seed_fragment(ctx, a)
    anchors = AnchorService(ctx.conn)
    anchors.set_active(a, frag)
    ctx.conn.execute("PRAGMA foreign_keys = OFF")
    ctx.conn.execute(
        "UPDATE cursor SET fragment_id = 'frag_ghost' WHERE anchor_type = 'active'"
    )
    ctx.conn.execute("PRAGMA foreign_keys = ON")
    ctx.conn.commit()

    assert SwitchTopicTool(ctx.conn).run_sync(topic_id=b).ok
    assert SwitchTopicTool(ctx.conn).run_sync(topic_id=a).ok
    assert anchors.get_active().topic_id == a
    assert anchors.get_active().fragment_id is None, "损坏位置必须降级为 None，不得注入错误片段"


def test_cross_topic_focus_never_injected(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag = _seed_fragment(ctx, a, messages=(("user", "PAYLOAD-CROSS"),))
    AnchorService(ctx.conn).set_active(b, frag)  # 人为制造跨话题锚点
    assert ctx._focus_block(b, frag) == ""


# -- 只读保证：memory_search 绝不改 anchor ------------------------------


def test_memory_search_is_read_only_for_anchor(ctx: AppContext):
    import asyncio

    from agent.tools.memory_search import MemorySearchTool

    topic = _topic(ctx, "检索只读")
    frag = _seed_fragment(ctx, topic, closed=True, title="检索片段")
    anchors = AnchorService(ctx.conn)
    anchors.set_active(topic, frag)
    before = _cursor_rows(ctx)

    tool = MemorySearchTool(ctx.retriever)
    for _ in range(10):
        asyncio.run(tool.run(query="历史消息"))

    assert _cursor_rows(ctx) == before, "memory_search 不得改变 cursor/anchor"
    assert anchors.get_active().fragment_id == frag


def test_open_fragment_position_anchor_roundtrip(ctx: AppContext):
    """开放片段也能成为位置（StartHere 到当前开放片段时不应崩）。"""
    topic = _topic(ctx, "开放片段")
    frag = _seed_fragment(ctx, topic, closed=False)
    anchors = AnchorService(ctx.conn)
    anchors.set_active(topic, frag)
    assert anchors.position_fragment(topic) == frag


def test_focus_fragment_only_for_historic_position(ctx: AppContext):
    """当前开放片段不需要额外 Focus（它已经通过短期转录入上下文）。"""
    topic = _topic(ctx, "Focus 规则")
    open_frag = _seed_fragment(ctx, topic, closed=False, title="当前片段")
    historic = _seed_fragment(ctx, topic, closed=True, title="历史片段")
    anchors = AnchorService(ctx.conn)

    anchors.set_active(topic, historic)
    assert anchors.focus_fragment(topic) == historic
    assert anchors.is_historic_position(topic) is True

    anchors.set_active(topic, open_frag)
    assert anchors.focus_fragment(topic) is None
    assert anchors.is_historic_position(topic) is False

    anchors.set_active(topic, None)
    assert anchors.focus_fragment(topic) is None
