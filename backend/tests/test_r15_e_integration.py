# -*- coding: utf-8 -*-
"""R1—R4 跨模块集成验证（E 角色独立用例）。

对应契约：docs/topic-review-r1r4-contract-20261111.md §7（跨模块集成）。

本文件只做**跨模块边界**验证，不重复 A/B/C/D 的模块内用例：

* R1 后端 + R2 前端：上下文装配期间改选历史 → 旧依据下的话题操作不得执行；
  后端不得为被拒绝的旧操作广播 ANCHOR（前端侧的事件序列由
  frontend/src/__tests__/r15-e-integration.test.ts 覆盖）。
* R3 后端 + R2 前端：真实 HTTP 下 A→B→重放 A 不改变后端位置；相应事件不得
  改变前端已到的 B（前端侧同上）。
* R4 + R2：恢复旧问题不抢占新轮次、不改变当前导航；同一逻辑答案只有一个执行权。
* 正常切换 / 接续点击 / 封存后恢复等受影响集合的回归（组合层，不重写实现）。
* 无第二套状态权威：后端位置只由 TopicNavigationService 写，前端只消费
  ANCHOR / 权威快照（后端侧断言 AnchorService 行与 /api/runtime/state 快照一致）。

纪律：真实 AppContext / 真实 SQLite / 真实主循环 / 真实 HTTP 路由与事务；
模型一律脚本化 fake（不联网、不读密钥）；时序用 asyncio.Event 屏障，不用 sleep
制造通过；断言失败即如实失败，不放宽。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services import navigation_version, topic_recovery
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app(tmp_path) -> AppContext:  # noqa: ANN001
    conn = connect(tmp_path / "r15_e_integration.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


@pytest.fixture()
def app(tmp_path) -> AppContext:
    return _app(tmp_path)


def _http_app(tmp_path):  # noqa: ANN001
    """真实 FastAPI 应用 + 同一 SQLite：路由层与主循环共用同一个 AppContext。"""
    conn = connect(tmp_path / "r15_e_http.db")
    apply_migrations(conn)
    api = create_app(Settings(data_dir=tmp_path), conn)
    ctx = api.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    return api, ctx


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _seed_fragment(
    app: AppContext,
    topic_id: str,
    *,
    closed: bool = True,
    title: str = "历史片段",
    payload: str = "历史原文",
) -> str:
    """写入一个片段（可选封块 + 索引行），返回 fragment_id。"""
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    app.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (frag_id, topic_id, f"{title} 的摘要", 1, now, now if closed else None),
    )
    msg_id = f"{frag_id}_m1"
    app.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (msg_id, frag_id, payload, now),
    )
    app.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (msg_id, msg_id, frag_id),
    )
    if closed:
        app.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
            "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (new_id("idx"), frag_id, topic_id, title, now),
        )
    app.conn.commit()
    return frag_id


def _anchor(app: AppContext):
    return AnchorService(app.conn).get_active()


def _nav(app: AppContext) -> int:
    return navigation_version.current(app.conn)


class ScriptedAdapter:
    """非流式脚本 adapter：每次 complete 依次取一条 (content, tool_calls) 脚本。"""

    mode = "native"
    model = "fake-r15-e"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self, script: list) -> None:
        self.script = list(script or [])
        self.requests: list[dict] = []
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        content, calls = self.script.pop(0) if self.script else (ANSWER + "\n补答", None)
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


class BarrierAdapter:
    """可屏障的 fake 模型：第 1 次调用挂起，其余调用立刻返回。"""

    mode = "native"
    model = "fake-r15-e-barrier"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self, text: str = ANSWER + "\n恢复后的回答") -> None:
        self.text = text
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await asyncio.wait_for(self.release.wait(), timeout=10)
        return Completion(message=ChatMessage(role="assistant", content=self.text))


def _switch(call_id: str, topic_id: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"topic_id": topic_id, "reason": "这段内容属于该话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


def _install_adapter(app: AppContext, monkeypatch, script: list) -> ScriptedAdapter:
    adapter = ScriptedAdapter(script)

    async def fake_build():  # noqa: ANN202
        return adapter

    monkeypatch.setattr(app, "build_adapter", fake_build)
    return adapter


def _install_barrier_adapter(app: AppContext, monkeypatch, adapter) -> None:  # noqa: ANN001
    async def fake_build():  # noqa: ANN202
        return adapter

    monkeypatch.setattr(app, "build_adapter", fake_build)


def _capture(app: AppContext) -> list:
    events: list = []
    original = app.bus.publish

    async def spy(event):  # noqa: ANN001
        events.append(event)
        await original(event)

    app.bus.publish = spy  # type: ignore[method-assign]
    return events


def _of(events: list, kind: EventType) -> list[dict]:
    return [
        dict(getattr(event, "data", None) or {})
        for event in events
        if getattr(event, "type", None) == kind
    ]


def _tool_ends(events: list, tool: str) -> list[dict]:
    return [item for item in _of(events, EventType.TOOL_END) if item.get("tool") == tool]


def _assistant_rows(app: AppContext, turn_id: str) -> list:
    return app.conn.execute(
        "SELECT role, content, fragment_id, turn_id FROM messages "
        "WHERE role = 'assistant' AND turn_id = ? ORDER BY rowid",
        (turn_id,),
    ).fetchall()


def _spy_turn_submit(app: AppContext, monkeypatch) -> dict:  # noqa: ANN001
    recorded: dict = {"turn_ids": []}
    original = app.turns.submit

    def spy(message, topic_id=None, **kwargs):  # noqa: ANN001, ANN202
        turn_ctx = original(message, topic_id, **kwargs)
        recorded["turn_ids"].append(turn_ctx.turn_id)
        recorded["turn_id"] = turn_ctx.turn_id
        return turn_ctx

    monkeypatch.setattr(app.turns, "submit", spy)
    return recorded


def _prepare_common_message(
    app: AppContext,
    *,
    turn_id: str = "turn_old",
    content: str = "旧问题",
) -> tuple[str, str, str]:
    """真实建话题/开放片段/轮绑定/用户消息，返回 (topic_id, fragment_id, message_id)。"""
    topic_id = _topic(app, "旧问题话题")
    fragment = app.fragments.get_or_create_open(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=content,
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _record_for_message(
    app: AppContext,
    topic_id: str,
    fragment_id: str,
    message_id: str,
    *,
    turn_id: str = "turn_old",
    record_id: str | None = None,
) -> str:
    record = topic_recovery.record_topic_failure(
        app,
        turn_id=turn_id,
        topic_id=topic_id,
        fragment_id=fragment_id,
        user_message_id=message_id,
        operation_id="op-r15-e",
        param_version=1,
        recovery_nav_version=_nav(app),
        failed_stage="context_prepare",
        message="回答准备失败（E 集成用例）",
        record_id=record_id,
    )
    return str(record["record_id"])


def _prepare_recovery_record(
    app: AppContext,
    *,
    turn_id: str = "turn_old",
    record_id: str | None = None,
    content: str = "旧问题",
) -> tuple[str, str, str]:
    """真实建话题/绑定/用户消息/恢复记录，返回 (topic_id, fragment_id, record_id)。"""
    topic_id, fragment_id, message_id = _prepare_common_message(
        app, turn_id=turn_id, content=content
    )
    rid = _record_for_message(
        app,
        topic_id,
        fragment_id,
        message_id,
        turn_id=turn_id,
        record_id=record_id,
    )
    return topic_id, fragment_id, rid


# ---------------------------------------------------------------------------
# ① R1 后端：上下文装配期间改选历史 → 旧依据下的话题操作不得执行
# ---------------------------------------------------------------------------


async def test_assembly_reselect_history_rejects_stale_topic_operation(app, monkeypatch):
    """装配暂停期间用户在同一话题改选历史：旧 switch_topic 必须被 stale 拒绝。

    这是本轮最核心的跨模块边界：快照时机（R1）决定后续写入前 CAS 是否还成立。
    """
    old = _topic(app, "旧话题")
    target = _topic(app, "目标话题")
    open_frag = app.fragments.get_or_create_open(old).id
    historic = _seed_fragment(app, old, closed=True, title="要改选的历史")
    app.fragments.get_or_create_open(target)
    events = _capture(app)
    version_before = _nav(app)
    submitted = _spy_turn_submit(app, monkeypatch)

    assembly_started = asyncio.Event()
    assembly_release = asyncio.Event()
    original_build = app.turn_orchestrator.build_context

    async def paused_build(turn_ctx, adapter):  # noqa: ANN001, ANN202
        assembly_started.set()
        await asyncio.wait_for(assembly_release.wait(), timeout=10)
        return await original_build(turn_ctx, adapter)

    monkeypatch.setattr(app.turn_orchestrator, "build_context", paused_build)
    _install_adapter(
        app,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n按原绑定保存的回答", None),
        ],
    )

    task = asyncio.create_task(app.run_turn("装配期间改选历史的问题", topic_id=old))
    await asyncio.wait_for(assembly_started.wait(), timeout=10)
    # 装配期间：用户在同一话题内改选历史片段（真实导航，推进版本）
    registered = app.navigation.register_continuation(old, historic)
    version_after = _nav(app)
    assert registered["changed"] is True, registered
    assert version_after > version_before, "改选历史必须推进导航版本（前置条件）"
    assembly_release.set()
    result = await asyncio.wait_for(task, timeout=30)

    assert result.get("ok") is True, result
    turn_id = submitted["turn_id"]

    # 1) 旧操作未执行：目标话题从未成为 Anchor（只断言「未被旧操作覆盖」。
    #    P1-04 既有语义：装配期间用户登记的更新接续选择（version 更大）优先，
    #    轮末 advance_anchor 会跳过位置细化 —— 这不属于 R1 契约 §3 的冻结范围，
    #    因此不断言轮末必须细化到本轮绑定片段。）
    active = _anchor(app)
    assert active is not None
    assert active.topic_id == old, "旧依据下的话题操作不得执行（Anchor 被覆盖到目标话题）"

    # 2) 用户的新选择仍在：待接续意图没有被旧操作消费 / 替换
    pending = app.bindings.peek_intent()
    assert pending is not None and pending.topic_id == old, pending
    assert pending.source_fragment_id == historic, pending
    assert pending.state == "registered", pending

    # 3) 旧操作没有推进导航版本
    assert _nav(app) == version_after, "被拒绝的旧操作不得推进导航版本"

    # 4) 后端事件屏障：没有为目标话题广播 ANCHOR
    anchor_events = _of(events, EventType.ANCHOR)
    assert [
        item for item in anchor_events if item.get("topic_id") == target
    ] == [], "旧操作被拒绝时不得广播目标话题的 ANCHOR"

    # 5) 本轮正式回答仍按原绑定归属保存并完成
    rows = _assistant_rows(app, turn_id)
    assert len(rows) == 1, [dict(row) for row in rows]
    assert rows[0]["fragment_id"] == open_frag
    assert rows[0]["content"] == "按原绑定保存的回答"

    # 6) 工具真实失败事实：按装配前快照做的写入前 CAS 必须判 stale。
    #    （TOOL_END 事件只暴露 code/error/status 等公开字段，不暴露 facts；
    #    起点版本本身由 A 的快照单元测试直接断言，这里断言集成层可观察的结局。）
    ends = _tool_ends(events, "switch_topic")
    assert ends, "缺少 switch_topic 的 TOOL_END 事件"
    assert ends[0].get("ok") is False, ends[0]
    assert ends[0].get("code") in ("nav_version_conflict", "topic_application_stale"), ends[0]
    assert ends[0].get("error"), ends[0]


async def test_assembly_leave_and_return_rejects_stale_topic_operation(app, monkeypatch):
    """装配期间离开再返回（版本推进两次）：旧操作同样必须 stale。"""
    old = _topic(app, "旧话题二")
    other = _topic(app, "临时浏览话题")
    target = _topic(app, "目标话题二")
    open_frag = app.fragments.get_or_create_open(old).id
    app.fragments.get_or_create_open(other)
    app.fragments.get_or_create_open(target)
    events = _capture(app)
    version_before = _nav(app)
    submitted = _spy_turn_submit(app, monkeypatch)

    assembly_started = asyncio.Event()
    assembly_release = asyncio.Event()
    original_build = app.turn_orchestrator.build_context

    async def paused_build(turn_ctx, adapter):  # noqa: ANN001, ANN202
        assembly_started.set()
        await asyncio.wait_for(assembly_release.wait(), timeout=10)
        return await original_build(turn_ctx, adapter)

    monkeypatch.setattr(app.turn_orchestrator, "build_context", paused_build)
    _install_adapter(
        app,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n按原绑定保存的回答", None),
        ],
    )

    task = asyncio.create_task(app.run_turn("装配期间离开又返回", topic_id=old))
    await asyncio.wait_for(assembly_started.wait(), timeout=10)
    app.navigation.enter_topic(other)
    app.navigation.enter_topic(old)
    version_after = _nav(app)
    assert version_after >= version_before + 2, "离开+返回应推进两次版本（前置条件）"
    assembly_release.set()
    result = await asyncio.wait_for(task, timeout=30)

    assert result.get("ok") is True, result
    turn_id = submitted["turn_id"]
    active = _anchor(app)
    assert active is not None and active.topic_id == old, active
    assert active.fragment_id == open_frag, active
    assert _nav(app) == version_after, "旧操作不得推进导航版本"
    assert [item for item in _of(events, EventType.ANCHOR) if item.get("topic_id") == target] == []
    rows = _assistant_rows(app, turn_id)
    assert len(rows) == 1 and rows[0]["fragment_id"] == open_frag, [dict(r) for r in rows]
    ends = _tool_ends(events, "switch_topic")
    assert ends and ends[0].get("ok") is False, ends


async def test_normal_switch_without_assembly_navigation_executes(app, monkeypatch):
    """对照组（正常切换回归）：装配期间没有导航时，话题操作必须真实执行。"""
    old = _topic(app, "正常旧话题")
    target = _topic(app, "正常目标话题")
    app.fragments.get_or_create_open(old)
    app.fragments.get_or_create_open(target)
    events = _capture(app)
    version_before = _nav(app)
    _install_adapter(
        app,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n目标话题里的正式回答", None),
        ],
    )

    result = await app.run_turn("正常切换到目标话题", topic_id=old)

    assert result.get("ok") is True, result
    active = _anchor(app)
    assert active is not None and active.topic_id == target, active
    assert _nav(app) == version_before + 1, "一次真实导航只推进一次版本"
    ends = _tool_ends(events, "switch_topic")
    assert ends and ends[0].get("ok") is True, ends


# ---------------------------------------------------------------------------
# ② R3 后端：A→B→重放 A 不改变后端位置（真实 HTTP）
# ---------------------------------------------------------------------------


async def test_http_a_to_b_replay_a_keeps_backend_position(tmp_path):
    api, ctx = _http_app(tmp_path)
    a = _topic(ctx, "甲话题")
    b = _topic(ctx, "乙话题")
    fa = _seed_fragment(ctx, a, closed=True, title="甲的历史 A")
    fb = _seed_fragment(ctx, b, closed=True, title="乙的历史 B")
    base0 = _nav(ctx)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-a",
                "base_nav_version": base0,
            },
        )
        assert first.status_code == 200, first.text
        body_a = first.json()
        assert body_a.get("intent_id"), body_a
        version_a = _nav(ctx)

        second = await client.post(
            "/api/anchor",
            json={
                "topic_id": b,
                "fragment_id": fb,
                "continue_from_history": True,
                "request_id": "req-b",
                "base_nav_version": version_a,
            },
        )
        assert second.status_code == 200, second.text
        version_b = _nav(ctx)
        assert version_b == version_a + 1, "一次真实改选只推进一次导航版本"
        anchor_b = _anchor(ctx)
        assert anchor_b is not None and anchor_b.topic_id == b and anchor_b.fragment_id == fb
        pending_b = ctx.bindings.peek_intent()
        assert pending_b is not None
        assert pending_b.topic_id == b and pending_b.source_fragment_id == fb
        fragments_before = ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]

        replay = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-a",
                "base_nav_version": base0,
            },
        )

    # 冻结契约 §5/§14.2：已失效旧选择（intent 已被 replaced/cancelled）的重放必须
    # 409 continuation_request_inactive（replay 既有拒绝事实、inactive=true），
    # 零位置写入 / 零版本推进 / 不重新获得位置控制权（位置与选择必须仍是 B）。
    assert replay.status_code == 409, (
        "已失效旧选择的请求重放必须 409 continuation_request_inactive，实际 %s: %s"
        % (replay.status_code, replay.text)
    )
    payload = replay.json()
    assert payload.get("ok") is False and payload.get("inactive") is True, payload
    assert payload.get("replay") is True, payload
    assert payload.get("changed") is False, payload
    assert payload.get("code") == "continuation_request_inactive", payload
    assert payload.get("command_id") == "request:req-a", payload

    anchor_after = _anchor(ctx)
    assert anchor_after is not None and anchor_after.topic_id == b, anchor_after
    assert anchor_after.fragment_id == fb, anchor_after
    pending_after = ctx.bindings.peek_intent()
    assert pending_after is not None and pending_after.intent_id == pending_b.intent_id
    assert _nav(ctx) == version_b, "重放旧请求不得推进导航版本"
    fragments_after = ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
    assert fragments_after == fragments_before, "点击/重放路径不得创建片段"
    intent_a = ctx.bindings.intent_by_request("req-a")
    assert intent_a is not None and intent_a.state == "replaced", intent_a
    intent_b = ctx.bindings.intent_by_request("req-b")
    assert intent_b is not None and intent_b.state == "registered", intent_b


async def test_http_continue_click_is_two_phase_and_creates_no_fragment(tmp_path):
    """回归（受影响集合）：点击历史只登记接续，不创建片段；重复点同一来源零推进。"""
    api, ctx = _http_app(tmp_path)
    a = _topic(ctx, "甲话题")
    fa = _seed_fragment(ctx, a, closed=True, title="甲的历史 A")
    fragments_before = ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
    base0 = _nav(ctx)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-same",
                "base_nav_version": base0,
            },
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        version_first = _nav(ctx)
        intent_first = first_body.get("intent_id")
        assert intent_first, first_body
        assert first_body.get("created_fragment_id") in (None, ""), first_body

        again = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-same",
                "base_nav_version": base0,
            },
        )
        assert again.status_code == 200, again.text
        again_body = again.json()

    assert again_body.get("intent_id") == intent_first, "同 request_id 重放必须复用同一选择"
    assert _nav(ctx) == version_first, "幂等重放不得推进导航版本"
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == fragments_before
    anchor = _anchor(ctx)
    assert anchor is not None and anchor.topic_id == a and anchor.fragment_id == fa


# ---------------------------------------------------------------------------
# ③ R4 + R2：恢复旧问题不抢占新轮次、不改变当前导航
# ---------------------------------------------------------------------------


async def test_recovery_old_question_does_not_preempt_running_turn(app, monkeypatch):
    """新轮次正在跑（模型挂起）时恢复旧问题：新轮次照常完成，导航不变。"""
    topic_id, fragment_id, record_id = _prepare_recovery_record(app)
    version_before = _nav(app)
    anchor_before = _anchor(app)
    submitted = _spy_turn_submit(app, monkeypatch)

    new_turn_adapter = BarrierAdapter(ANSWER + "\n新轮次的正式回答")
    _install_barrier_adapter(app, monkeypatch, new_turn_adapter)
    new_turn_task = asyncio.create_task(app.run_turn("新轮次的问题", topic_id=topic_id))
    await asyncio.wait_for(new_turn_adapter.started.wait(), timeout=10)

    # 新轮次模型调用挂起期间：恢复旧问题（真实恢复服务 + 真实 SQLite 事务）
    recovery_adapter = BarrierAdapter(ANSWER + "\n恢复旧问题的回答")
    recovery_adapter.started.set()
    recovery_adapter.release.set()
    _install_barrier_adapter(app, monkeypatch, recovery_adapter)
    retry = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(app, record_id), timeout=20
    )
    new_turn_adapter.release.set()
    new_result = await asyncio.wait_for(new_turn_task, timeout=30)

    # 1) 恢复旧问题没有抢占 / 取消新轮次
    assert new_result.get("ok") is True, new_result
    assert len(submitted["turn_ids"]) == 1, (
        "恢复旧问题不得提交新轮次：%r" % (submitted["turn_ids"],)
    )
    new_rows = _assistant_rows(app, submitted["turn_id"])
    assert len(new_rows) == 1 and new_rows[0]["content"] == "新轮次的正式回答", [
        dict(row) for row in new_rows
    ]

    # 2) 恢复真实生成并保存了旧问题的唯一答案（按原轮次归属）
    assert retry.get("ok") is True and retry.get("answered") is True, retry
    old_rows = _assistant_rows(app, "turn_old")
    assert len(old_rows) == 1 and old_rows[0]["content"] == "恢复旧问题的回答", [
        dict(row) for row in old_rows
    ]
    assert old_rows[0]["fragment_id"] == fragment_id

    # 3) 当前导航与位置完全不变（恢复不覆盖用户当前所在）
    anchor_after = _anchor(app)
    assert anchor_after is not None and anchor_after.topic_id == topic_id
    if anchor_before is not None:
        assert anchor_after.fragment_id == anchor_before.fragment_id
    assert _nav(app) == version_before, "恢复旧问题不得推进导航版本"


async def test_recovery_same_answer_key_has_single_execution_right(app, monkeypatch):
    """同一逻辑答案的两条恢复记录：第二个请求 202 处理中，模型只调用一次。

    这是 R4 的跨模块边界（恢复服务 + 真实 SQLite + 唯一答案约束）：
    不能靠"每个 record 各自领租约"重复生成。
    """
    topic_id, fragment_id, message_id = _prepare_common_message(app)
    record_a = _record_for_message(app, topic_id, fragment_id, message_id, record_id="rec-a")
    record_b = _record_for_message(app, topic_id, fragment_id, message_id, record_id="rec-b")

    adapter = BarrierAdapter(ANSWER + "\n同一逻辑答案")
    _install_barrier_adapter(app, monkeypatch, adapter)

    first_task = asyncio.create_task(topic_recovery.retry_topic_answer(app, record_a))
    await asyncio.wait_for(adapter.started.wait(), timeout=10)
    second = await asyncio.wait_for(
        topic_recovery.retry_topic_answer(app, record_b), timeout=20
    )
    adapter.release.set()
    first = await asyncio.wait_for(first_task, timeout=20)

    # 1) 同一逻辑答案只发起一次模型调用
    assert adapter.calls == 1, "同一 answer_key 只能有一次生成执行权，实际模型调用=%d" % adapter.calls
    # 2) 第二个请求是结构化 202 处理中（不是再生成，也不是成功）
    assert second.get("in_progress") is True, second
    assert second.get("code") == "recovery_in_progress", second
    assert second.get("status_code") == 202, second
    # 3) 唯一答案只保存一份
    assert first.get("ok") is True and first.get("answered") is True, first
    rows = app.conn.execute(
        "SELECT id, content, turn_id FROM messages WHERE role = 'assistant' AND turn_id = 'turn_old'"
    ).fetchall()
    assert len(rows) == 1, [dict(row) for row in rows]
    # 4) 两条记录都收口为 answered，且指向同一 answer_id
    stored_a = topic_recovery.get_record(app, record_a)
    stored_b = topic_recovery.get_record(app, record_b)
    assert stored_a is not None and stored_b is not None
    assert stored_a.get("status") == "answered", stored_a
    assert stored_b.get("status") == "answered", stored_b
    assert stored_a.get("answer_id") == stored_b.get("answer_id"), (stored_a, stored_b)
    assert stored_a.get("answer_id") == rows[0]["id"]


# ---------------------------------------------------------------------------
# ④ 受影响集合回归（组合层）：封存后恢复仍按原归属保存
# ---------------------------------------------------------------------------


async def test_sealed_fragment_recovery_keeps_original_attribution(app, monkeypatch):
    """封存后恢复（回归）：答案写回原封存片段，不重开、不写新片段、不动导航。"""
    topic_id, fragment_id, record_id = _prepare_recovery_record(app, content="封存前的旧问题")
    app.fragments.seal(fragment_id, reason="capacity")
    sealed_at = app.fragments.get(fragment_id).closed_at
    new_open = app.fragments.get_or_create_open(topic_id)
    assert new_open.id != fragment_id
    version_before = _nav(app)
    anchor_before = _anchor(app)
    fragment_count_before = app.conn.execute(
        "SELECT COUNT(*) c FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchone()["c"]

    adapter = BarrierAdapter(ANSWER + "\n封存后的迟到答案")
    adapter.started.set()
    adapter.release.set()
    _install_barrier_adapter(app, monkeypatch, adapter)
    result = await topic_recovery.retry_topic_answer(app, record_id)

    assert result.get("ok") is True and result.get("answered") is True, result
    rows = _assistant_rows(app, "turn_old")
    assert len(rows) == 1, [dict(row) for row in rows]
    assert rows[0]["fragment_id"] == fragment_id, "答案必须写回原封存片段，不得写进新片段"
    assert rows[0]["content"] == "封存后的迟到答案"
    assert app.fragments.get(fragment_id).closed_at == sealed_at, "不得重开已封存片段"
    fragment_count_after = app.conn.execute(
        "SELECT COUNT(*) c FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchone()["c"]
    assert fragment_count_after == fragment_count_before, "恢复不得新建片段"
    assert _nav(app) == version_before, "恢复不得推进导航版本"
    anchor_after = _anchor(app)
    if anchor_before is not None:
        assert anchor_after.topic_id == anchor_before.topic_id
        assert anchor_after.fragment_id == anchor_before.fragment_id


# ---------------------------------------------------------------------------
# ⑤ 无第二套状态权威：/api/runtime/state 的 anchor 与 AnchorService/版本一致
# ---------------------------------------------------------------------------


async def test_runtime_state_anchor_matches_navigation_authority(tmp_path):
    """后端位置权威只有一份：runtime state 快照必须等于导航服务写入的真实位置。

    同时覆盖 R3(b) 冻结接口：/api/runtime/state 追加 anchor 对象。
    """
    api, ctx = _http_app(tmp_path)
    a = _topic(ctx, "权威话题甲")
    b = _topic(ctx, "权威话题乙")
    fa = _seed_fragment(ctx, a, closed=True, title="甲的历史")
    fb = _seed_fragment(ctx, b, closed=True, title="乙的历史")
    ctx.navigation.enter_topic(a, fragment_id=fa)
    version_a = _nav(ctx)
    base_replay = version_a
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        state_a = await client.get("/api/runtime/state")
        assert state_a.status_code == 200, state_a.text
        body_a = state_a.json()
        assert "anchor" in body_a, "runtime state 必须包含权威 anchor 快照（契约 §4.2b）"
        assert body_a["anchor"].get("topic_id") == a, body_a.get("anchor")
        assert body_a["anchor"].get("fragment_id") == fa, body_a.get("anchor")
        assert body_a["anchor"].get("nav_version") == version_a, body_a.get("anchor")
        assert body_a.get("instance_id"), "runtime state 必须带进程实例身份"

        # 先真实选择 A（request_id=req-authority-a），再真实选择 B：
        # 之后用**已失效的旧 request_id** 重放 A 才是「旧位置事件」。
        chosen_a = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-authority-a",
                "base_nav_version": version_a,
            },
        )
        assert chosen_a.status_code == 200, chosen_a.text
        version_a2 = _nav(ctx)

        # 真实导航（只走 TopicNavigationService）：B
        moved = await client.post(
            "/api/anchor",
            json={
                "topic_id": b,
                "fragment_id": fb,
                "continue_from_history": True,
                "request_id": "req-authority-b",
                "base_nav_version": version_a2,
            },
        )
        assert moved.status_code == 200, moved.text
        version_b = _nav(ctx)
        assert version_b == version_a2 + 1, "一次真实改选只推进一次版本"
        anchor_db = _anchor(ctx)
        assert anchor_db is not None and anchor_db.topic_id == b and anchor_db.fragment_id == fb

        state_b = await client.get("/api/runtime/state")
        assert state_b.status_code == 200, state_b.text
        body_b = state_b.json()
        assert body_b["anchor"].get("topic_id") == b, body_b.get("anchor")
        assert body_b["anchor"].get("fragment_id") == fb, body_b.get("anchor")
        assert body_b["anchor"].get("nav_version") == version_b, body_b.get("anchor")

        # R16 I04/C3：重放已失效的旧 request_id（A）返回既有结果（replay），
        # 不重新导航、不改变权威快照（断言强度不降：快照必须仍是 B）。
        stale = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-authority-a",
                "base_nav_version": base_replay,
            },
        )
        state_after = await client.get("/api/runtime/state")
    assert stale.status_code == 409, (
        "已失效旧 request_id 的重放必须 409 continuation_request_inactive，实际 %s: %s"
        % (stale.status_code, stale.text)
    )
    stale_body = stale.json()
    assert stale_body.get("ok") is False and stale_body.get("inactive") is True, stale_body
    assert stale_body.get("replay") is True and stale_body.get("changed") is False, stale_body
    assert stale_body.get("code") == "continuation_request_inactive", stale_body
    assert stale_body.get("command_id") == "request:req-authority-a", stale_body
    body_after = state_after.json()
    assert body_after["anchor"].get("topic_id") == b, (
        "旧位置请求不得改变权威快照：%r" % (body_after.get("anchor"),)
    )
    assert body_after["anchor"].get("fragment_id") == fb
    assert body_after["anchor"].get("nav_version") == version_b
    assert _nav(ctx) == version_b


async def test_runtime_state_new_request_id_is_a_real_new_selection(tmp_path):
    """对照组（契约 §5.3）：新 request_id + 选择 A = 用户重新主动选择 A。

    这不是「旧位置事件」：必须 200 changed、Anchor 回到 A、版本 +1，
    权威快照随之变成 A —— 与「旧 request_id 重放 = 409 且位置不变」严格区分。
    """
    api, ctx = _http_app(tmp_path)
    a = _topic(ctx, "重新选择甲")
    b = _topic(ctx, "重新选择乙")
    fa = _seed_fragment(ctx, a, closed=True, title="甲的历史")
    fb = _seed_fragment(ctx, b, closed=True, title="乙的历史")
    base0 = _nav(ctx)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first_a = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-first-a",
                "base_nav_version": base0,
            },
        )
        assert first_a.status_code == 200, first_a.text
        moved_b = await client.post(
            "/api/anchor",
            json={
                "topic_id": b,
                "fragment_id": fb,
                "continue_from_history": True,
                "request_id": "req-first-b",
                "base_nav_version": _nav(ctx),
            },
        )
        assert moved_b.status_code == 200, moved_b.text
        version_b = _nav(ctx)
        anchor_b = _anchor(ctx)
        assert anchor_b is not None and anchor_b.topic_id == b

        # 用户重新主动选择 A：新 request_id（不是重放）
        reselect = await client.post(
            "/api/anchor",
            json={
                "topic_id": a,
                "fragment_id": fa,
                "continue_from_history": True,
                "request_id": "req-reselect-a",
                "base_nav_version": version_b,
            },
        )
        assert reselect.status_code == 200, reselect.text
        body = reselect.json()
        assert body.get("ok") is True, body
        assert body.get("topic_id") == a and body.get("fragment_id") == fa, body
        version_reselect = _nav(ctx)
        assert version_reselect == version_b + 1, "真实新选择必须推进一次导航版本"
        anchor_reselect = _anchor(ctx)
        assert anchor_reselect is not None
        assert anchor_reselect.topic_id == a and anchor_reselect.fragment_id == fa
        pending = ctx.bindings.peek_intent()
        assert pending is not None and pending.topic_id == a and pending.source_fragment_id == fa

        state = await client.get("/api/runtime/state")
    assert state.status_code == 200, state.text
    body_state = state.json()
    assert "anchor" in body_state, body_state
    assert body_state["anchor"].get("topic_id") == a, body_state.get("anchor")
    assert body_state["anchor"].get("fragment_id") == fa, body_state.get("anchor")
    assert body_state["anchor"].get("nav_version") == version_reselect, body_state.get("anchor")
    # 旧的选择已被替换：两条旧意图不再有效，唯一当前选择是 A
    old_a = ctx.bindings.intent_by_request("req-first-a")
    assert old_a is not None and old_a.state == "replaced", old_a
    old_b = ctx.bindings.intent_by_request("req-first-b")
    assert old_b is not None and old_b.state == "replaced", old_b
