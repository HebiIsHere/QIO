# -*- coding: utf-8 -*-
"""P1-01 集成：主循环模型的 intent 是唯一的语义受控通道（A 角色定向测试）。

用 fake adapter 走**真实**的 TurnOrchestrator → AgentLoop → TopicApplicationBridge
→ 原工具管线，只把「模型说什么」和「用户点哪个按钮」换成脚本。证明：

* intent="user_request"：原工具**直达执行**，不弹普通话题确认；
* 缺省 intent（suggestion）：先建立申请；用户拒绝时工具不执行、在原话题回答；
* 同一轮重复/迟到的同参数调用只产生**一次**逻辑操作（导航版本只推进一次）；
* 模型请求里能看到本轮用户原话与两个话题工具的 intent 参数 schema。

不声称真实模型准确率；这里只验证接线与分支衔接。
"""
from __future__ import annotations

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

# 关闭 conftest 的旧测试自动批准兜底：本文件要验证真实申请/拒绝分支。
pytestmark = pytest.mark.topic_application


class ScriptedAdapter:
    mode = "native"
    model = "fake-p1r-a-intent"
    supports_stream = False

    def __init__(self, script: list[tuple[str | None, list[ToolCall] | None]]) -> None:
        self.script = list(script)
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        content, calls = self.script.pop(0) if self.script else ("收尾回答", None)
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


class FakeApprovals:
    """可编程的用户决定：记录申请；默认拒绝；记录申请发生时的真实位置。"""

    def __init__(self, app: AppContext, *, decision: str = "rejected") -> None:
        self.app = app
        self.decision = decision
        self.requests: list[dict] = []
        self.anchor_at_request: list[str | None] = []
        self.executions: list[dict] = []

    def set_context(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        """与真实 ApprovalService 同形的轻量替身：本轮审批上下文不影响断言。"""

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        active = AnchorService(self.app.conn).get_active()
        self.anchor_at_request.append(active.topic_id if active is not None else None)
        self.requests.append({"kind": kind, "payload": dict(payload)})
        return ApprovalResult(f"appr-{len(self.requests)}", self.decision)

    async def record_legacy_execution(self, approval_id, *, ok, error=None, summary=None):  # noqa: ANN001
        self.executions.append(
            {"approval_id": approval_id, "ok": bool(ok), "error": error}
        )
        return {"phase": "executed" if ok else "failed"}


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "intent.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _install(app: AppContext, monkeypatch, adapter) -> FakeApprovals:
    async def fake_build():
        return adapter

    monkeypatch.setattr(app, "build_adapter", fake_build)
    approvals = FakeApprovals(app)
    app.approvals = approvals
    return approvals


def _anchor(app: AppContext) -> str | None:
    active = AnchorService(app.conn).get_active()
    return active.topic_id if active is not None else None


def _turn_id(app: AppContext) -> str:
    row = app.conn.execute(
        "SELECT turn_id FROM messages WHERE role = 'user' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and row["turn_id"]
    return str(row["turn_id"])


def _switch(topic_id: str, *, intent: str | None = None, call_id: str = "c1") -> ToolCall:
    arguments: dict = {"topic_id": topic_id, "reason": "用户明确要求"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


# -- 1. user_request：直达原工具，不弹普通确认 -------------------------------


async def test_user_request_switch_executes_original_tool_directly(app, monkeypatch):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.navigation.enter_topic(old_topic)
    adapter = ScriptedAdapter(
        [
            (None, [_switch(target_topic, intent="user_request")]),
            ("最终回答：已经在新话题里", None),
        ]
    )
    approvals = _install(app, monkeypatch, adapter)

    result = await app.run_turn("切到目标话题", topic_id=old_topic)

    assert result["ok"] is True
    assert approvals.requests == [], "user_request 不得再弹普通话题确认"
    # 原工具真的执行了：位置、绑定、本轮归属都迁到目标话题。
    assert _anchor(app) == target_topic
    binding = app.bindings.binding_for(_turn_id(app))
    assert binding is not None and binding.topic_id == target_topic
    # 正式回答只落一份，并且在目标话题的片段里。
    rows = app.conn.execute(
        "SELECT content FROM messages WHERE role = 'assistant' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchall()
    assert any("最终回答" in (row["content"] or "") for row in rows)


async def test_user_request_create_executes_original_tool_directly(app, monkeypatch):
    old_topic = _topic(app, "旧话题")
    app.navigation.enter_topic(old_topic)
    adapter = ScriptedAdapter(
        [
            (
                None,
                [
                    ToolCall(
                        id="c1",
                        name="create_topic",
                        arguments={"name": "量子计算", "reason": "独立新目标", "intent": "user_request"},
                    )
                ],
            ),
            ("最终回答：新话题已建立", None),
        ]
    )
    approvals = _install(app, monkeypatch, adapter)

    result = await app.run_turn("新建一个话题叫量子计算", topic_id=old_topic)

    assert result["ok"] is True
    assert approvals.requests == [], "user_request 新建不得弹普通确认"
    created = app.conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = ?", ("量子计算",)
    ).fetchone()
    assert created is not None
    assert _anchor(app) == created["id"]


# -- 2. suggestion（缺省）：先申请；用户决定前工具不执行 --------------------


async def test_suggestion_waits_for_user_and_does_not_execute(app, monkeypatch):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.navigation.enter_topic(old_topic)
    # 故意不写 intent：缺省值就是 suggestion（安全侧）。
    adapter = ScriptedAdapter(
        [
            (None, [_switch(target_topic)]),
            ("最终回答：继续在旧话题回答", None),
        ]
    )
    approvals = _install(app, monkeypatch, adapter)

    result = await app.run_turn("我们换成目标话题聊吧", topic_id=old_topic)

    assert result["ok"] is True
    assert len(approvals.requests) == 1
    request = approvals.requests[0]
    assert request["kind"] == "topic_switch"
    assert request["payload"]["intent_source"] == "suggestion"
    # 用户还没有决定时工具绝不执行：申请发生时位置仍是旧话题。
    assert approvals.anchor_at_request == [old_topic]
    assert _anchor(app) == old_topic
    binding = app.bindings.binding_for(_turn_id(app))
    assert binding is not None and binding.topic_id == old_topic
    rows = app.conn.execute(
        "SELECT content FROM messages WHERE role = 'assistant' ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchall()
    assert any("继续在旧话题回答" in (row["content"] or "") for row in rows)


# -- 3. 同轮只一次逻辑操作（重复/迟到调用幂等）-------------------------------


async def test_same_turn_duplicate_switch_is_one_logical_operation(app, monkeypatch):
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    app.navigation.enter_topic(old_topic)
    adapter = ScriptedAdapter(
        [
            (None, [_switch(target_topic, intent="user_request", call_id="c1")]),
            # 第二次模型响应再次请求同一操作（网络重放 / 迟到调用）。
            (None, [_switch(target_topic, intent="user_request", call_id="c2")]),
            ("最终回答：只执行了一次", None),
        ]
    )
    approvals = _install(app, monkeypatch, adapter)
    # 直接数「原工具真实执行次数」：同轮同参数的第二次调用必须被桥幂等拦下，
    # 不能再执行一次导航（迟到的 c2 不得改变归属）。
    executions: list[str] = []
    original_execute = app.registry.execute

    async def _count_executions(call):  # noqa: ANN001
        if str(getattr(call, "name", "") or "") in ("switch_topic", "create_topic"):
            executions.append(str(getattr(call, "id", "") or ""))
        return await original_execute(call)

    monkeypatch.setattr(app.registry, "execute", _count_executions)

    result = await app.run_turn("切到目标话题", topic_id=old_topic)

    assert result["ok"] is True
    assert approvals.requests == []
    assert executions == ["c1"], f"同一轮逻辑操作只能执行一次原工具：{executions}"
    assert _anchor(app) == target_topic
    binding = app.bindings.binding_for(_turn_id(app))
    assert binding is not None and binding.topic_id == target_topic


# -- 4. 模型输入侧：原话完整 + intent 参数 schema 可见 ----------------------


async def test_model_sees_original_text_and_intent_parameter_schema(app, monkeypatch):
    topic = _topic(app, "当前话题")
    app.navigation.enter_topic(topic)
    adapter = ScriptedAdapter([("好的。", None)])
    _install(app, monkeypatch, adapter)

    await app.run_turn("不要切换到原神话题，继续回答这里的问题", topic_id=topic)

    request = adapter.requests[0]
    user_texts = [
        str(getattr(message, "content", "") or "")
        for message in request["messages"]
        if getattr(message, "role", "") == "user"
    ]
    assert user_texts[-1] == "不要切换到原神话题，继续回答这里的问题"
    specs = {str(getattr(spec, "name", "")): spec for spec in request["tools"]}
    assert "switch_topic" in specs and "create_topic" in specs
    for name in ("switch_topic", "create_topic"):
        intent_schema = specs[name].parameters["properties"]["intent"]
        assert intent_schema["enum"] == ["user_request", "suggestion"]
