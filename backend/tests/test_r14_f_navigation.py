# -*- coding: utf-8 -*-
"""R14-F 独立回归（F05 决策位置快照 / F08 重复接续登记不推进导航版本）。

真实 AppContext / 真实主循环 / 真实 SQLite / 真实导航与绑定服务；模型脚本化 fake，
审批可编程替身。只证明程序接线与状态事实，不声称真实模型语义准确率。

对应契约：docs/topic-review-20261110-contract.md §3.1（F05）、§3.6（F08）。
"""
from __future__ import annotations

import json

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager
from agent.services.app import AppContext
from agent.services.navigation import TopicNavigationService
from agent.services.navigation_version import current as current_nav_version
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application

ANSWER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 装置（集成：真实 AppContext）
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14_f_nav.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


class ScriptedAdapter:
    mode = "native"
    model = "fake-r14-f-nav"
    supports_stream = False

    def __init__(self, script: list) -> None:
        self.script = list(script or [])
        self.requests: list[dict] = []
        self.on_response = None

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        index = len(self.requests)
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.on_response is not None:
            self.on_response(index)
        content, calls = self.script.pop(0) if self.script else (ANSWER + "\n补答", None)
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


class FakeApprovals:
    def __init__(self, *, decisions: list[dict] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.requests: list[dict] = []

    async def request(self, kind, payload):  # noqa: ANN001
        self.requests.append(
            {"kind": str(kind), "payload": json.loads(json.dumps(payload, default=str))}
        )
        item = self.decisions.pop(0) if self.decisions else {"decision": "rejected"}
        return ApprovalResult(
            str(item.get("approval_id") or f"appr_f_{len(self.requests)}"),
            str(item.get("decision") or "rejected"),
            edits=item.get("edits"),
        )

    async def record_legacy_execution(self, approval_id, *, ok, error=None, summary=None):  # noqa: ANN001
        return {"phase": "executed" if ok else "failed"}

    def set_context(self, *, turn_id=None, session_id=None):  # noqa: ANN001
        self.context = {"turn_id": turn_id, "session_id": session_id}


def _use_fake_approvals(ctx: AppContext, fake: FakeApprovals) -> FakeApprovals:
    ctx.approvals = fake
    try:
        tool = ctx.registry.get("create_topic")
    except Exception:  # noqa: BLE001
        tool = None
    if tool is not None and hasattr(tool, "approvals"):
        tool.approvals = fake
    return fake


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _anchor(ctx: AppContext) -> str | None:
    active = AnchorService(ctx.conn).get_active()
    return active.topic_id if active is not None else None


def _install(ctx: AppContext, monkeypatch, script: list) -> ScriptedAdapter:
    adapter = ScriptedAdapter(script)

    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)
    return adapter


def _capture(ctx: AppContext) -> list:
    events: list = []
    original = ctx.bus.publish

    async def spy(event):  # noqa: ANN001
        events.append(event)
        await original(event)

    ctx.bus.publish = spy  # type: ignore[method-assign]
    return events


def _of(events: list, kind: EventType) -> list[dict]:
    return [
        dict(getattr(event, "data", None) or {})
        for event in events
        if getattr(event, "type", None) == kind
    ]


def _formal(events: list) -> list[dict]:
    return [
        item
        for item in _of(events, EventType.ASSISTANT)
        if item.get("interim") is False and str(item.get("content") or "").strip()
    ]


def _tool_ends(events: list, tool: str) -> list[dict]:
    return [item for item in _of(events, EventType.TOOL_END) if item.get("tool") == tool]


def _switch(call_id: str, topic_id: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"topic_id": topic_id, "reason": "这段内容属于该话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


def _user_message_count(ctx: AppContext, content: str) -> int:
    row = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE role = 'user' AND content = ?", (content,)
    ).fetchone()
    return int(row["c"] if row is not None else 0)


def _user_rows(ctx: AppContext, content: str) -> list:
    return ctx.conn.execute(
        "SELECT m.id, m.fragment_id, f.topic_id FROM messages m "
        "LEFT JOIN fragments f ON f.id = m.fragment_id "
        "WHERE m.role = 'user' AND m.content = ?",
        (content,),
    ).fetchall()


# ---------------------------------------------------------------------------
# F05：决策依据形成时冻结真实位置；旧模型结果不覆盖用户后来的选择
# ---------------------------------------------------------------------------


async def test_f05_manual_navigation_during_model_call_is_not_overwritten(
    ctx: AppContext, monkeypatch
):
    """模型决策期间用户手动导航：旧结果的 base 版本必须来自决策快照，不得覆盖新位置。"""
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    other = _topic(ctx, "用户后来点开的话题")
    for topic_id in (target, other):
        ctx.fragments.get_or_create_open(topic_id)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    question = "换到目标话题继续"
    observed: dict = {}

    def _hook(index: int) -> None:
        if index == 0:
            # 模型调用期间（决策依据已形成）用户自己手动切走。
            ctx.navigation.enter_topic(other)
            observed["version_after_user_nav"] = current_nav_version(ctx.conn)

    adapter = _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n在原话题回答", None),
        ],
    )
    adapter.on_response = _hook
    events = _capture(ctx)

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    assert observed.get("version_after_user_nav") is not None
    assert _anchor(ctx) == other, (
        "旧模型结果不得覆盖用户后来的手动导航；实际 Anchor=%r" % (_anchor(ctx),)
    )
    ok_switch = [item for item in _tool_ends(events, "switch_topic") if item.get("ok")]
    assert ok_switch == [], "失效的旧操作不得真实执行：%s" % ok_switch
    assert {item["content"] for item in _formal(events)} == {"在原话题回答"}, (
        "失效不是拒绝：应在当前（新）位置给出正式回答"
    )
    rows = _user_rows(ctx, question)
    assert len(rows) == 1, "原问题只能存一份"
    assert rows[0]["topic_id"] == old, (
        "旧操作失效后不搬动本轮归属：原问题仍属于提交时的绑定话题（不被旧结果搬到目标话题）"
    )


async def test_f05_same_batch_sequential_navigation_uses_causal_snapshot_update(
    ctx: AppContext, monkeypatch
):
    """同一模型批次里的两次话题调用：第一次成功后快照按因果更新，第二次以新快照为起点。

    Lead 裁决（F05 vs P1-02）：因果更新只在「同一模型批次内多次话题调用」验证；
    context_ready / ANSWERABLE 打开之后的新话题调用按迟到处理，不执行。
    """
    old = _topic(ctx, "旧话题")
    first = _topic(ctx, "目标话题一")
    second = _topic(ctx, "目标话题二")
    for topic_id in (first, second):
        ctx.fragments.get_or_create_open(topic_id)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    version_before = current_nav_version(ctx.conn)
    _install(
        ctx,
        monkeypatch,
        [
            # 同一个 completion 里的两次用户明确要求（按 dispatch 顺序执行）。
            ("", [_switch("c1", first, intent="user_request"), _switch("c2", second, intent="user_request")]),
            (ANSWER + "\n在目标话题二里的正式回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("先换到话题一再换到话题二", topic_id=old)

    assert result.get("ok") is True, result
    assert _anchor(ctx) == second, (
        "同一批次第二次导航应以第一次成功后的 ctx 快照为 base 执行；实际 Anchor=%r"
        % (_anchor(ctx),)
    )
    ok_switch = [item for item in _tool_ends(events, "switch_topic") if item.get("ok")]
    assert len(ok_switch) == 2, "同一批次两次真实导航都应执行成功：%s" % ok_switch
    # 不得把第一次导航误判成外来手动导航：没有 stale / nav_version_conflict / 拒绝证据。
    stale_errors = [
        item
        for item in _tool_ends(events, "switch_topic")
        if str(item.get("code") or "") in {"nav_version_conflict", "topic_application_stale"}
    ]
    assert stale_errors == [], stale_errors
    assert [item for item in _of(events, EventType.ERROR) if item.get("code") == "topic_ownership_failed"] == []
    assert current_nav_version(ctx.conn) == version_before + 2, "两次真实导航各推进一次版本"
    assert {item["content"] for item in _formal(events)} == {"在目标话题二里的正式回答"}
    assert _user_rows(ctx, "先换到话题一再换到话题二") != []


async def test_f05_topic_call_after_answerable_is_late_and_not_executed(
    ctx: AppContext, monkeypatch
):
    """ANSWERABLE 之后的新话题调用 = 迟到：警告、不执行、Anchor 不变、不写拒绝证据。

    这是 P1-02 冻结语义，F05 的因果更新不得把它退化成「永远继续执行」。
    """
    from agent.services.topic_application import get_topic_application_state

    old = _topic(ctx, "旧话题")
    first = _topic(ctx, "目标话题一")
    late = _topic(ctx, "迟到目标话题")
    for topic_id in (first, late):
        ctx.fragments.get_or_create_open(topic_id)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", first, intent="user_request")]),
            ("", [_switch("c2", late, intent="user_request")]),
            (ANSWER + "\n在目标话题一里的正式回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("换到目标话题一，之后再随便换一个", topic_id=old)

    assert result.get("ok") is True, result
    assert _anchor(ctx) == first, "迟到导航不得改变本轮归属"
    ok_switch = [item for item in _tool_ends(events, "switch_topic") if item.get("ok")]
    assert len(ok_switch) == 1, ok_switch
    late_tool_ends = [
        item for item in _tool_ends(events, "switch_topic") if item.get("call_id") == "c2"
    ]
    assert late_tool_ends == [], "迟到的工具调用不得执行（不伪造 TOOL_END）"
    warnings = [str(item.get("message") or "") for item in _of(events, EventType.WARNING)]
    assert any("不执行" in message for message in warnings), warnings
    assert [
        item
        for item in _of(events, EventType.ERROR)
        if str(item.get("code") or "") in {"topic_application_stale", "nav_version_conflict"}
    ] == [], "迟到 ≠ 失效：不得报 stale / nav_version_conflict"
    guard = get_topic_application_state(ctx)
    assert guard.refused_attempts == [], "迟到不写拒绝证据：%s" % guard.refused_attempts
    assert {item["content"] for item in _formal(events)} == {"在目标话题一里的正式回答"}


async def test_f05_duplicate_same_batch_topic_call_replays_without_executing_again(
    ctx: AppContext, monkeypatch
):
    """同一批次的同版本重复调用：已成功的只重放既有结果，不重复执行、不再推进版本。"""
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    version_before = current_nav_version(ctx.conn)
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request"), _switch("c2", target, intent="user_request")]),
            (ANSWER + "\n目标话题里的正式回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("换到目标话题", topic_id=old)

    assert result.get("ok") is True, result
    assert _anchor(ctx) == target
    ok_switch = [item for item in _tool_ends(events, "switch_topic") if item.get("ok")]
    assert len(ok_switch) == 1, "同版本重复调用不得重复执行：%s" % ok_switch
    assert current_nav_version(ctx.conn) == version_before + 1, "只推进一次导航版本"
    assert _user_message_count(ctx, "换到目标话题") == 1


# ---------------------------------------------------------------------------
# F08：重复接续登记不推进导航版本（真实 SQLite / 真实服务）
# ---------------------------------------------------------------------------


def _nav_env(db_conn):
    topic_id = NodeService(db_conn).create_topic("话题A").id
    fragments = FragmentManager(db_conn)
    old_one = fragments.create_child(
        topic_id, source_fragment_id=None, relation_type="normal", boundary_reason="capacity"
    )
    fragments.close(old_one, "旧片段一摘要")
    old_two = fragments.create_child(
        topic_id, source_fragment_id=None, relation_type="normal", boundary_reason="capacity"
    )
    fragments.close(old_two, "旧片段二摘要")
    open_fragment = fragments.get_or_create_open(topic_id)
    return topic_id, old_one, old_two, open_fragment, TopicNavigationService(db_conn), fragments


def _intent_for(db_conn, request_id: str):
    from agent.services.binding import TurnBindingService

    return TurnBindingService(db_conn).intent_by_request(request_id)


def test_f08_replay_same_request_id_does_not_advance_nav_version(db_conn):
    topic_id, old_one, old_two, _open, nav, fragments = _nav_env(db_conn)
    version0 = current_nav_version(db_conn)

    first = nav.register_continuation(topic_id, old_one, request_id="req-1")
    version1 = current_nav_version(db_conn)
    assert first.get("changed") is True, "第一次真实登记必须报告 changed=True：%r" % (first,)
    assert version1 == version0 + 1, (version0, version1, first)

    replay = nav.register_continuation(topic_id, old_one, request_id="req-1")
    version2 = current_nav_version(db_conn)
    assert replay.get("changed") is False, "同 request_id 重放必须报告 changed=False：%r" % (replay,)
    assert replay["nav_version"] == first["nav_version"], (first, replay)
    assert replay["intent_id"] == first["intent_id"], "重放必须复用既有登记"
    assert version2 == version1, "同 request_id 重放不得推进导航版本：%s -> %s" % (version1, version2)
    # 幂等重放不产生第二条接续意图
    intent = _intent_for(db_conn, "req-1")
    assert intent is not None and intent.intent_id == first["intent_id"]


def test_f08_repeated_same_source_click_does_not_advance_nav_version(db_conn):
    topic_id, old_one, _old_two, _open, nav, _fragments = _nav_env(db_conn)
    version0 = current_nav_version(db_conn)

    first = nav.register_continuation(topic_id, old_one)
    assert first.get("changed") is True
    assert current_nav_version(db_conn) == version0 + 1

    again = nav.register_continuation(topic_id, old_one)
    assert again.get("changed") is False, "重复点同一来源必须 changed=False：%r" % (again,)
    assert again["intent_id"] == first["intent_id"]
    assert again["nav_version"] == first["nav_version"]
    assert current_nav_version(db_conn) == version0 + 1, "重复点同一来源不得推进版本"


def test_f08_switching_source_advances_once_and_replaced_request_does_not_reactivate(
    db_conn,
):
    topic_id, old_one, old_two, _open, nav, fragments = _nav_env(db_conn)
    version0 = current_nav_version(db_conn)

    first = nav.register_continuation(topic_id, old_one, request_id="req-1")
    version1 = current_nav_version(db_conn)
    assert version1 == version0 + 1

    second = nav.register_continuation(topic_id, old_two, request_id="req-2")
    version2 = current_nav_version(db_conn)
    assert second.get("changed") is True and version2 == version1 + 1, (second, version1, version2)
    current_intent = _intent_for(db_conn, "req-2")
    assert current_intent is not None and current_intent.source_fragment_id == old_two

    # 重放已被替换的旧请求：不得重新激活旧选择、不得覆盖当前选择、不得推进版本。
    try:
        replay_old = nav.register_continuation(topic_id, old_one, request_id="req-1")
    except Exception:  # noqa: BLE001 - 明确 stale 失败也是合法收口
        replay_old = None
    assert current_nav_version(db_conn) == version2, "重放已被替换的旧请求不得推进版本"
    from agent.services.binding import TurnBindingService

    peeked = TurnBindingService(db_conn).peek_intent()
    assert peeked is not None and peeked.source_fragment_id == old_two, (
        "当前选择必须仍是用户后来改选的那一段：%r" % (peeked,)
    )
    if replay_old is not None:
        assert replay_old.get("changed") is False, replay_old
        assert replay_old["nav_version"] == version2, replay_old

# ---------------------------------------------------------------------------
# F12 后端接缝：权威 ANCHOR 事件必须携带操作身份与单调序号
# ---------------------------------------------------------------------------


async def test_f12_anchor_event_carries_authoritative_operation_identity(
    ctx: AppContext, monkeypatch
):
    """前端草稿跟随只消费「本次操作权威成功结果」：后端 ANCHOR 事件必须给出可核对的
    origin / operation_id / param_version / nav_version / seq；普通手动导航不得伪造身份。
    """
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n目标话题里的正式回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("换到目标话题继续", topic_id=old)

    assert result.get("ok") is True, result
    anchor_events = _of(events, EventType.ANCHOR)
    assert anchor_events, "换题成功后必须广播 ANCHOR"
    seqs = [item.get("seq") for item in anchor_events]
    assert all(isinstance(seq, int) for seq in seqs), seqs
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), (
        "ANCHOR seq 必须进程内单调且不重复：%s" % seqs
    )
    operation_anchors = [
        item for item in anchor_events if item.get("origin") == "topic_operation"
    ]
    assert operation_anchors, "话题工具真实成功后必须发出 origin=topic_operation 的权威 ANCHOR"
    authoritative = operation_anchors[-1]
    assert authoritative.get("topic_id") == target, authoritative
    assert authoritative.get("operation_id"), "权威 ANCHOR 必须带 operation_id：%s" % authoritative
    assert isinstance(authoritative.get("param_version"), int), authoritative
    assert isinstance(authoritative.get("nav_version"), int), authoritative
    assert authoritative.get("turn_id"), authoritative
    # 手动导航 / 内部位置推进不得伪造操作身份。
    manual = [item for item in anchor_events if item.get("origin") == "manual"]
    for item in manual:
        assert item.get("operation_id") is None, item

# ---------------------------------------------------------------------------
# F08 路由级：POST /api/anchor continue_from_history 的导航版本语义
# ---------------------------------------------------------------------------


def test_f08_anchor_api_continue_history_version_semantics(tmp_path):
    """真实 HTTP 路由：真实改选只 +1；同 request_id 重放 / 重复点同一来源不推进；
    返回相同选择；普通进入话题仍按原规则推进一次。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    conn = connect(tmp_path / "f08_http.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()

    topic = ctx.topics.nodes.create_topic("话题A").id
    fragments = ctx.fragments
    historic = fragments.create_child(
        topic, source_fragment_id=None, relation_type="normal", boundary_reason="capacity"
    )
    fragments.close(historic, "旧摘要")
    fragments.get_or_create_open(topic)

    v0 = current_nav_version(conn)
    with TestClient(app) as client:
        first = client.post(
            "/api/anchor",
            json={
                "topic_id": topic,
                "fragment_id": historic,
                "continue_from_history": True,
                "request_id": "req-http-1",
            },
        )
        assert first.status_code == 200, first.text
        body = first.json()
        v1 = current_nav_version(conn)
        assert v1 == v0 + 1, "一次真实改选只推进一次导航版本：%s -> %s" % (v0, v1)
        assert body.get("pending") is True and body.get("intent_id"), body
        assert body.get("fragment_id") == historic
        assert body.get("source_fragment_id") == historic

        # 同 request_id 重放（传输重试）：不推进版本、返回同一选择。
        replay = client.post(
            "/api/anchor",
            json={
                "topic_id": topic,
                "fragment_id": historic,
                "continue_from_history": True,
                "request_id": "req-http-1",
            },
        )
        assert replay.status_code == 200, replay.text
        replay_body = replay.json()
        assert current_nav_version(conn) == v1, "同 request_id 重放不得推进导航版本"
        assert replay_body.get("intent_id") == body.get("intent_id"), replay_body
        assert replay_body.get("pending") is True, replay_body

        # 重复点同一来源（没有 request_id）：仍不推进版本、返回同一选择。
        repeat = client.post(
            "/api/anchor",
            json={"topic_id": topic, "fragment_id": historic, "continue_from_history": True},
        )
        assert repeat.status_code == 200, repeat.text
        repeat_body = repeat.json()
        assert current_nav_version(conn) == v1, "重复点同一来源不得推进导航版本"
        assert repeat_body.get("intent_id") == body.get("intent_id"), repeat_body

        # 普通进入话题（非 continue）：仍按原规则推进一次。
        other = ctx.topics.nodes.create_topic("话题B").id
        v_before = current_nav_version(conn)
        normal = client.post("/api/anchor", json={"topic_id": other})
        assert normal.status_code == 200, normal.text
        assert current_nav_version(conn) == v_before + 1, "普通进入话题推进一次"
