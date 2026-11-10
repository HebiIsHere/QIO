# -*- coding: utf-8 -*-
"""R14-F 独立回归（F02 直接要求≠执行成功可合法重试 / F07 精确执行参数决定批准版本 /
F10 查重冲突拒绝进入统一终态）。

真实 AppContext / 真实主循环 / 真实工具与审批桥；模型脚本化 fake；审批可编程替身。
对应契约：docs/topic-review-20261110-contract.md §3.2（F02/F07/F10）、§3.7（F10）。
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
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

pytestmark = pytest.mark.topic_application

ANSWER = "[[QIO:ANSWER]]"


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14_f_identity.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


class ScriptedAdapter:
    mode = "native"
    model = "fake-r14-f-identity"
    supports_stream = False

    def __init__(self, script: list) -> None:
        self.script = list(script or [])
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
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


def _create(call_id: str, name: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"name": name, "reason": "这是独立的新话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="create_topic", arguments=arguments)


def _switch(call_id: str, topic_id: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"topic_id": topic_id, "reason": "这段内容属于该话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


def _topic_names(ctx: AppContext) -> list[str]:
    return [node.name for node in ctx.topics.nodes.list_topics()]


def _user_message_count(ctx: AppContext, content: str) -> int:
    row = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE role = 'user' AND content = ?", (content,)
    ).fetchone()
    return int(row["c"] if row is not None else 0)


def _turn_id(events: list) -> str | None:
    for event in events:
        data = getattr(event, "data", None) or {}
        value = data.get("turn_id")
        if value:
            return str(value)
    return None


def _operations(ctx: AppContext, events: list) -> list:
    registry = getattr(ctx, "topic_operation_registry", None)
    assert registry is not None, "本轮没有建立话题操作登记表"
    turn_id = _turn_id(events)
    if turn_id:
        operations = registry.for_turn_all(turn_id)
        if operations:
            return operations
    return list(getattr(registry, "_operations", {}).values())


# ---------------------------------------------------------------------------
# F02：user_request 直接执行遇查重冲突 → 同操作同参数版本可合法重试，只创建一次
# ---------------------------------------------------------------------------


async def test_f02_user_request_conflict_retry_executes_and_creates_once(ctx, monkeypatch):
    old = _topic(ctx, "旧话题")
    _topic(ctx, "布局与间距")  # 已存在相似话题：create 会先返回查重冲突
    ctx.navigation.enter_topic(old)
    approvals = _use_fake_approvals(
        ctx, FakeApprovals(decisions=[{"decision": "approved"}])
    )
    before = _topic_names(ctx)
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_create("c1", "布局与间距", intent="user_request")]),
            ("", [_create("c2", "布局与间距", intent="user_request")]),
            (ANSWER + "\n新话题里的正式回答", None),
        ],
    )
    events = _capture(ctx)
    question = "按我的要求新建一个布局与间距话题"

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    # 冲突不是最终失败：同操作同参数版本再次调用必须真的重新进入原工具与冲突确认。
    inner = [item for item in approvals.requests if item["kind"] == "create_topic"]
    assert len(inner) == 1, "冲突确认必须真实发生：%s" % approvals.requests
    assert all(item["kind"] in {"create_topic"} for item in approvals.requests), (
        "user_request 直达不得建立普通话题申请：%s" % approvals.requests
    )
    create_ends = _tool_ends(events, "create_topic")
    assert [item for item in create_ends if item.get("ok")], (
        "合法重试后 create_topic 必须真实成功：%s" % create_ends
    )
    after = _topic_names(ctx)
    assert len(after) == len(before) + 1, "只能创建一次：%s -> %s" % (before, after)
    assert [name for name in after if name == "布局与间距"], after
    assert _anchor(ctx) not in (old, None), "创建成功后本轮归属必须跟随真实目标"
    assert {item["content"] for item in _formal(events)} == {"新话题里的正式回答"}
    assert _user_message_count(ctx, question) == 1


# ---------------------------------------------------------------------------
# F07：精确执行参数决定批准版本（查重归一化只作证据）
# ---------------------------------------------------------------------------


async def test_f07_changed_actual_name_requires_new_approval_version(ctx, monkeypatch):
    """用户批准的是 "Data Science"；模型自行改成 "DataScience" → 旧批准不得复用。"""
    old = _topic(ctx, "旧话题")
    _topic(ctx, "DataScience")  # 让首名 "Data Science" 触发查重冲突（归一化后会相等）
    ctx.navigation.enter_topic(old)
    approvals = _use_fake_approvals(
        ctx,
        FakeApprovals(
            decisions=[
                {"decision": "approved"},  # 用户批准第一次申请（Data Science）
                {"decision": "rejected"},  # 改名后的新申请：用户拒绝
            ]
        ),
    )
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_create("c1", "Data Science")]),
            ("", [_create("c2", "DataScience")]),
            (ANSWER + "\n在原话题回答", None),
        ],
    )
    events = _capture(ctx)
    question = "把这块内容拆成新话题"

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    topic_requests = [item for item in approvals.requests if item["kind"] == "topic_create"]
    assert len(topic_requests) == 2, (
        "实际名称变化必须推进参数版本、建立新的申请（旧批准不得复用）：%s"
        % approvals.requests
    )
    assert topic_requests[0]["payload"].get("new_name") == "Data Science"
    assert topic_requests[1]["payload"].get("new_name") == "DataScience", (
        "执行参数必须是真实名称（不能是归一化后的名字）：%s" % topic_requests[1]["payload"]
    )
    assert _anchor(ctx) == old, "被拒绝的新版本不得执行"
    assert {item["content"] for item in _formal(events)} == {"在原话题回答"}
    # 拒绝 ≠ 继续弹：第二次拒绝后不再建立第三条申请
    assert len(approvals.requests) == 2, approvals.requests


# ---------------------------------------------------------------------------
# F10：查重冲突确认的拒绝进入统一终态（REFUSED + CONFLICT_DECLINED）
# ---------------------------------------------------------------------------


async def test_f10_conflict_declined_sets_unified_terminal_state_and_answers_in_original_topic(
    ctx, monkeypatch
):
    from agent.services.topic_application import get_topic_application_state
    from agent.services.topic_operation import ConflictStatus, OperationStatus

    old = _topic(ctx, "旧话题")
    _topic(ctx, "布局与间距")
    ctx.navigation.enter_topic(old)
    approvals = _use_fake_approvals(
        ctx,
        FakeApprovals(
            decisions=[
                {"decision": "approved"},  # 普通话题申请：用户同意新建
                {"decision": "rejected"},  # 查重冲突二次确认：用户拒绝
            ]
        ),
    )
    before = _topic_names(ctx)
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_create("c1", "布局与间距")]),
            ("", [_create("c2", "布局与间距")]),
            ("", [_create("c3", "布局与间距")]),  # 同版本：不得再重复弹冲突
            (ANSWER + "\n在原话题回答", None),
        ],
    )
    events = _capture(ctx)
    question = "把这块内容拆成新话题"

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    assert _anchor(ctx) == old, "拒绝冲突不得执行/导航"
    assert _topic_names(ctx) == before, "拒绝冲突不得创建话题"
    assert len(approvals.requests) == 2, (
        "同版本被拒绝后不得再次弹冲突确认：%s" % approvals.requests
    )
    assert {item["content"] for item in _formal(events)} == {"在原话题回答"}
    assert _user_message_count(ctx, question) == 1

    # 统一终态：决定层 refused + 冲突层 conflict_declined（不是 failed / 仍 pending）
    operations = _operations(ctx, events)
    assert operations, "必须存在本轮话题操作身份"
    operation = operations[-1]
    assert operation.status is OperationStatus.REFUSED, (
        "冲突拒绝必须进入统一终态 refused，而不是 failed；实际=%r" % (operation.status,)
    )
    assert operation.conflict is ConflictStatus.CONFLICT_DECLINED, (
        "冲突拒绝必须清除待处理冲突并记 declined；实际=%r" % (operation.conflict,)
    )
    guard = get_topic_application_state(ctx)
    refused = [item for item in guard.refused_attempts if item.get("action") == "create"]
    assert refused, "冲突拒绝必须写入拒绝证据（不得丢失）：%s" % guard.refused_attempts


# ---------------------------------------------------------------------------
# F07 支撑事实：执行参数身份不使用查重归一化（同一操作内版本推进）
# ---------------------------------------------------------------------------


def test_f07_operation_param_version_separates_execution_params_from_dedup():
    from agent.services.topic_operation import IntentSource, TopicOperationRegistry

    registry = TopicOperationRegistry()
    operation = registry.begin(
        turn_id="turn-f07",
        kind="topic_create",
        route="application",
        intent_source=IntentSource.SUGGESTION,
        arguments={"name": "Data Science"},
        from_topic="topic-old",
        from_fragment=None,
        base_nav_version=0,
        user_message_id="msg-1",
    )
    assert operation.param_version == 1
    assert operation.update_params({"name": "DataScience"}) is True, (
        "内部空格等实际存储会变化的字符必须算参数变化（决定 param_version）"
    )
    assert operation.param_version == 2
    # 只做外层 strip；末尾空白不是实际名称变化
    assert operation.update_params({"name": "DataScience "}) is False
    assert operation.param_version == 2
