"""P1-03 恢复入口定向测试：retry-answer 只重做未完成步骤（幂等）。

用 fake provider + 真实 SQLite（AppContext）证明接线与分支衔接：
* context_prepare 失败重试 → 只重装配上下文 + 生成回答（不重跑话题工具、不新增用户消息）；
* answer 失败重试 → 只重做回答；
* 已成功 → 幂等返回既有结果（不重复生成、不复制消息）；
* 导航版本变化 → 如实 stale，不覆盖用户后来的选择，记录保持可恢复；
* 路由注册 POST /api/recovery/records/{record_id}/retry-answer。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.api.recovery_routes import build_router
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.topic_operation import TopicStages
from agent.services.navigation_version import advance as advance_nav_version
from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import TopicOperation
from agent.services.topic_recovery import (
    TopicContextPrepareError,
    record_topic_failure,
    retry_topic_answer,
)
from agent.storage.db import connect
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry
from agent.storage.migrate import apply_migrations

ANSWER_MARKER = "[[QIO:ANSWER]]"


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "topic_recovery.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


class FakeAdapter:
    mode = "native"
    model = "fake-recovery"
    protocol_in_prompt = False

    def __init__(self, *, text: str = ANSWER_MARKER + "\n恢复后的回答", error=None) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.requests: list = []

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.error is not None:
            raise self.error
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self, *, messages=None, error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error
        self.calls: list = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append(
            {"turn_id": turn_id, "topic_id": topic_id, "user_message_id": user_message_id}
        )
        if self.error is not None:
            raise self.error
        if self.messages is not None:
            return list(self.messages)
        return [
            ChatMessage(role="system", content="SYS_FOR:" + str(topic_id)),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire(app: AppContext, orchestrator: FakeOrchestrator, adapter: FakeAdapter) -> None:
    app.turn_orchestrator = orchestrator  # type: ignore[assignment]
    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]


def _prepare(app: AppContext, *, name: str = "目标话题"):
    topic_id = app.topics.nodes.create_topic(name).id
    app.fragments.get_or_create_open(topic_id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="原问题",
        content_type="text",
        model="fake",
        turn_id="turn_r",
    )
    return topic_id, message_id


def _make_record(app: AppContext, topic_id: str, message_id: str, **overrides):
    payload = {
        "turn_id": "turn_r",
        "topic_id": topic_id,
        "user_message_id": message_id,
        "operation_id": "op-1",
        "param_version": 1,
        "base_nav_version": None,
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    return record_topic_failure(app, **payload)


def _message_rows(app: AppContext, role: str | None = None) -> list:
    sql = "SELECT role, content, fragment_id FROM messages"
    params: tuple = ()
    if role is not None:
        sql += " WHERE role = ?"
        params = (role,)
    return app.conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# 1) context_prepare 失败重试：只做装配 + 回答；幂等；不新增用户消息
# ---------------------------------------------------------------------------


async def test_retry_after_context_prepare_failure_generates_answer_once(app: AppContext):
    topic_id, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)
    assert record["status"] == "pending"

    topics_before = app.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type = 'topic'"
    ).fetchone()["c"]

    result = await retry_topic_answer(app, record["record_id"])

    assert result["ok"] is True
    assert result["answered"] is True
    assert result["record_id"] == record["record_id"]
    assert result["turn_id"] == "turn_r"
    assert result["topic_id"] == topic_id
    assert result["answer"] == "恢复后的回答"
    assert result["answer_id"]
    assert adapter.calls == 1
    assert orchestrator.calls == [
        {"turn_id": "turn_r", "topic_id": topic_id, "user_message_id": message_id}
    ]
    # 正文按内容角色协议解析：声明标记不进回答正文
    assistant_rows = _message_rows(app, "assistant")
    assert [row["content"] for row in assistant_rows] == ["恢复后的回答"]
    # 不新增用户消息、不新建轮次；不重跑话题工具（create/switch 次数不变）
    assert len(_message_rows(app, "user")) == 1
    topics_after = app.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type = 'topic'"
    ).fetchone()["c"]
    assert topics_after == topics_before, "恢复不得重跑 create/switch（不新建话题）"
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    # R14-B / F03：答案正文只有一个权威位置（messages 行）；记录只保存 answer_id，
    # 成功收口后不长期保留第二份正文。旧断言按内存记录形状写，已按新语义更新。
    assert stored["answer_id"] == result["answer_id"]
    assert stored.get("answer_text") in (None, "")

    # 幂等：重复调用直接返回既有结果，不重复生成、不复制消息
    again = await retry_topic_answer(app, record["record_id"])
    assert again["ok"] is True
    assert again["answered"] is True
    assert again["answer_id"] == result["answer_id"]
    assert again["answer"] == "恢复后的回答"
    assert adapter.calls == 1
    assert len(_message_rows(app, "assistant")) == 1


async def test_retry_reports_navigation_version_conflict_without_overwriting(app: AppContext):
    topic_id, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id, base_nav_version=3)

    # C 的 navigation_version 模块尚未落地时用替身：见下，用 sys.modules 注入。
    fake_module = types.ModuleType("agent.services.navigation_version")
    fake_module.current = lambda conn: 7
    previous = sys.modules.get("agent.services.navigation_version")
    sys.modules["agent.services.navigation_version"] = fake_module
    try:
        result = await retry_topic_answer(app, record["record_id"])
    finally:
        if previous is not None:
            sys.modules["agent.services.navigation_version"] = previous
        else:
            sys.modules.pop("agent.services.navigation_version", None)

    assert result["ok"] is False
    assert result["conflict"] is True
    assert result["code"] == "topic_application_stale"
    assert result["expected_nav_version"] == 3
    assert result["actual_nav_version"] == 7
    assert adapter.calls == 0, "stale 时不得重新生成回答"
    assert orchestrator.calls == [], "stale 时不得重新装配上下文/执行任何操作"
    assert _message_rows(app, "assistant") == []
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "pending", "记录保留可恢复入口（stale ≠ 拒绝）"
    assert stored["last_error"] == "nav_version_conflict"


# ---------------------------------------------------------------------------
# 2) 装配再次失败：记录不丢、不写消息
# ---------------------------------------------------------------------------


async def test_retry_keeps_record_when_context_prepare_fails_again(app: AppContext):
    topic_id, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator(
        error=TopicContextPrepareError(
            "boom", operation_id="op-1", topic_id=topic_id
        )
    )
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)

    result = await retry_topic_answer(app, record["record_id"])

    assert result["ok"] is False
    assert result["code"] == "topic_context_prepare_failed"
    assert result["recoverable"] is True
    assert result["failed_stage"] == "context_prepare"
    assert adapter.calls == 0
    assert _message_rows(app, "assistant") == []
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "pending"


# ---------------------------------------------------------------------------
# 3) answer 失败单独恢复：只重做回答
# ---------------------------------------------------------------------------


async def test_retry_after_answer_failure_only_regenerates_answer(app: AppContext):
    topic_id, message_id = _prepare(app)
    adapter = FakeAdapter(error=RuntimeError("provider down while answering"))
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(
        app, topic_id, message_id, failed_stage="answer", message="话题已切换，回答生成失败"
    )

    failed = await retry_topic_answer(app, record["record_id"])
    assert failed["ok"] is False
    assert failed["code"] == "topic_answer_failed"
    assert failed["failed_stage"] == "answer"
    assert _message_rows(app, "assistant") == []
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "pending"
    assert stored["failed_stage"] == "answer"

    # 注入的模型故障恢复后重试成功：只生成一次回答
    adapter.error = None
    ok = await retry_topic_answer(app, record["record_id"])
    assert ok["ok"] is True
    assert ok["answered"] is True
    assert adapter.calls == 2
    assert len(_message_rows(app, "assistant")) == 1


async def test_retry_uses_real_navigation_version_module(app: AppContext):
    """不注入替身：真实 navigation_version.current 参与复核（P1-04 接缝）。"""
    from agent.services.navigation_version import current as current_nav_version

    topic_id, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    actual = current_nav_version(app.conn)
    # 版本与记录里的起点不一致 → 如实 stale，不生成、不写消息
    record = _make_record(
        app, topic_id, message_id, base_nav_version=int(actual) + 1
    )
    result = await retry_topic_answer(app, record["record_id"])
    assert result["ok"] is False
    assert result["code"] == "topic_application_stale"
    assert result["actual_nav_version"] == actual
    assert adapter.calls == 0
    assert _message_rows(app, "assistant") == []

    # 起点与当前一致 → 正常恢复
    same = _make_record(app, topic_id, message_id, base_nav_version=int(actual))
    ok = await retry_topic_answer(app, same["record_id"])
    assert ok["ok"] is True
    assert ok["navigation_version_check"] == "ok"
    assert adapter.calls == 1

# ---------------------------------------------------------------------------
# 5) 真实 loop 失败 → 记录以「失败时刻」位置为恢复基线 → retry 成功
# ---------------------------------------------------------------------------


async def _loop_context_failure_scenario(app: AppContext):
    """真实 AgentLoop + 假桥：工具导航推进版本后上下文装配失败。"""
    from agent.core.loop import AgentLoop
    from agent.core.turn import TurnContext

    topic_id, message_id = _prepare(app)
    fragment_id = app.fragments.get_or_create_open(topic_id).id
    ctx = TurnContext(turn_id="turn_r", message="原问题")
    ctx.user_message_id = message_id
    ctx.bound_topic = topic_id
    ctx.bound_fragment_id = fragment_id

    class _SwitchTool(Tool):
        name = "switch_topic"
        description = "fake switch"
        parameters = {"type": "object", "properties": {}}

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="已切换到目标话题")

    registry = ToolRegistry()
    registry.register(_SwitchTool())
    version_before = current_nav_version(app.conn)
    operation = TopicOperation(
        operation_id="op-seam",
        turn_id="turn_r",
        kind="topic_switch",
        base_nav_version=version_before,
    )

    class _Bridge:
        def __init__(self) -> None:
            self.app = app
            self.ctx = ctx
            self.loop = None
            self.decisions = ["approved"]

        def set_phase(self, phase) -> None:
            if self.loop is not None:
                self.loop.set_topic_phase(phase)

        async def before_tool(self, call):
            if call.name != "switch_topic":
                return None
            if self.decisions.pop(0) != "approved":
                return ToolResult(ok=False, content="拒绝")
            return None

        async def after_tool(self, call, result):
            # 真实导航成功：services/navigation 的 cas_or_advance 会把版本推进 1。
            advance_nav_version(
                app.conn,
                source="tool_navigation",
                topic_id=topic_id,
                fragment_id=fragment_id,
                turn_id="turn_r",
            )
            return {
                "rebind_topic_id": topic_id,
                "operation_id": "op-seam",
                "target_topic_id": topic_id,
                "stages": {
                    "tool_success": True,
                    "navigation_applied": True,
                    "turn_ownership": True,
                    "context_ready": False,
                },
                "keep_decision_open": False,
            }

        async def build_system(self, topic):
            raise RuntimeError("topic context rebuild failed: boom")

        def current_operation(self):
            return operation

    bridge = _Bridge()
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text_chunks=["我先确认一下。"],
                tool_calls=[
                    ScriptedToolCall(
                        id="c1",
                        name="switch_topic",
                        arguments={"topic_id": topic_id},
                    )
                ],
            )
        ]
    )
    loop = AgentLoop(
        adapter,
        registry,
        EventBus(),
        topic_application=bridge,
        turn_id="turn_r",
    )
    bridge.loop = loop
    result = await asyncio.wait_for(loop.run("原问题"), timeout=15)
    return loop, result, topic_id, message_id, version_before


async def test_loop_failure_records_failure_time_version_and_retry_succeeds(
    app: AppContext,
):
    """集成缺口回归：工具导航推进版本后，记录不能用申请起点快照做恢复基线。"""
    loop, result, topic_id, message_id, version_before = (
        await _loop_context_failure_scenario(app)
    )

    assert result.formal_answer is False
    assert result.final_content is None
    recovery = loop._topic_recovery
    assert recovery is not None and recovery["failed_stage"] == "context_prepare"
    stored = topic_recovery.get_record(app, recovery["record_id"])
    assert stored["base_nav_version"] == version_before, "申请起点快照仍如实保留"
    actual = current_nav_version(app.conn)
    assert actual == version_before + 1, "工具导航成功会推进导航版本"
    assert stored["recovery_nav_version"] == actual, (
        "恢复基线必须是失败时刻的真实位置，而不是等待前的申请起点"
    )
    assert stored["user_message_id"] == message_id
    assert stored["topic_id"] == topic_id

    # 用户没有新导航 → retry 只重做上下文装配 + 回答，成功且不会 stale
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    retried = await retry_topic_answer(app, recovery["record_id"])
    assert retried["ok"] is True
    assert retried["answered"] is True
    assert retried["navigation_version_check"] == "ok"
    assert retried["answer"] == "恢复后的回答"
    assert adapter.calls == 1
    assert orchestrator.calls and orchestrator.calls[0]["topic_id"] == topic_id
    assert len(_message_rows(app, "assistant")) == 1
    assert len(_message_rows(app, "user")) == 1

def test_retry_answer_http_route_is_wired(tmp_path):
    """经 api.server 的真实路由（Lead include build_router）→ 未知记录 404。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    conn = connect(tmp_path / "http.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        response = client.post(
            "/api/recovery/records/trec_missing/retry-answer",
            json={"expected_class": "topic_answer_recovery"},
        )
    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "recovery_not_found"

# ---------------------------------------------------------------------------
# 4) 路由注册与幂等响应形状（E 按此消费）
# ---------------------------------------------------------------------------


def test_retry_answer_route_is_registered(app: AppContext):
    router = build_router(app)
    paths = {(route.path, tuple(sorted(getattr(route, "methods", set())))) for route in router.routes}
    assert ("/api/recovery/records/{record_id}/retry-answer", ("POST",)) in paths


async def test_retry_answer_unknown_record_returns_404_shape(app: AppContext):
    result = await retry_topic_answer(app, "trec_missing")
    assert result["ok"] is False
    assert result["code"] == "recovery_not_found"
    assert result["status_code"] == 404


async def test_retry_answer_expected_status_conflict_does_not_generate(app: AppContext):
    topic_id, message_id = _prepare(app)
    adapter = FakeAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)

    result = await retry_topic_answer(
        app, record["record_id"], expected_status="answered"
    )
    assert result["ok"] is False
    assert result["code"] == "recovery_status_conflict"
    assert adapter.calls == 0
