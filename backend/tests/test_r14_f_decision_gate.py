# -*- coding: utf-8 -*-
"""R14-F 独立回归（F01 普通工具不提前打开可回答阶段 / F06 归属失败可恢复 / F11 查询后仍可话题工具）。

本文件是**独立验证者**的回归：真实 AppContext / 真实主循环 / 真实 SQLite /
真实工具注册与导航服务；模型一律脚本化 fake；审批用可编程替身。
只证明程序接线、阶段门、事件顺序与分支衔接，不声称真实模型语义准确率。

对应契约：docs/topic-review-20261110-contract.md §3.3（F01/F06）、§3.8（F11）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import TopicPhase
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

# 本文件验证真实话题申请交互：关闭 conftest 的旧测试自动批准兜底。
pytestmark = pytest.mark.topic_application

ANSWER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14_f_gate.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


class ScriptedAdapter:
    """非流式脚本 adapter；on_response(index) 在该次模型调用返回前触发。"""

    mode = "native"
    model = "fake-r14-f-gate"
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
        self.executions: list[dict] = []

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
        self.executions.append({"approval_id": approval_id, "ok": bool(ok)})
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


def _phases(events: list) -> list[str]:
    return [str(item.get("phase") or "") for item in _of(events, EventType.TOPIC_PHASE)]


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


def _echo(call_id: str, text: str) -> ToolCall:
    return ToolCall(id=call_id, name="echo", arguments={"text": text})


def _lookup(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, name="lookup_topic", arguments={"name": name})


def _user_message_count(ctx: AppContext, content: str) -> int:
    row = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE role = 'user' AND content = ?", (content,)
    ).fetchone()
    return int(row["c"] if row is not None else 0)


def _index_of(events: list, predicate) -> int:  # noqa: ANN001
    for index, event in enumerate(events):
        if predicate(event):
            return index
    raise AssertionError("事件缺失：找不到满足条件的项")


# ---------------------------------------------------------------------------
# F01：普通工具调用（未声明回答）不得提前打开 ANSWERABLE
# ---------------------------------------------------------------------------


async def test_f01_ordinary_tool_call_keeps_decision_open_then_topic_tool_executes(
    ctx: AppContext, monkeypatch
):
    """旧行为：第一次响应只有普通工具 → 立刻 ANSWERABLE，第二次 switch_topic 被当迟到。

    修复后：普通工具轮保持 DECISION_OPEN；第二次真实话题工具照常执行，
    直到工具成功 + 目标上下文就绪才 ANSWERABLE，正式回答在它之后。
    """
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())  # 明确请求不应建立普通申请
    version_before = current_nav_version(ctx.conn)
    observed: dict = {}

    def _phase_now() -> str | None:
        phases = [
            str((getattr(event, "data", None) or {}).get("phase") or "")
            for event in events
            if getattr(event, "type", None) == EventType.TOPIC_PHASE
        ]
        return phases[-1] if phases else None

    def _hook(index: int) -> None:
        if index != 1:
            return
        observed["anchor"] = _anchor(ctx)
        observed["phase"] = _phase_now()
        observed["nav_version"] = current_nav_version(ctx.conn)

    events = _capture(ctx)
    adapter = _install(
        ctx,
        monkeypatch,
        [
            ("我先确认一下运行环境。", [_echo("c1", "echo-ok")]),
            ("", [_switch("c2", target, intent="user_request")]),
            (ANSWER + "\n目标话题里的正式回答", None),
        ],
    )
    adapter.on_response = _hook
    question = "换到目标话题继续"

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    assert observed.get("anchor") == old, "普通工具调用本身不得移动对话位置"
    assert observed.get("phase") == "decision_open", (
        "只有普通工具调用、还没准备发布正式答案时，阶段必须保持 DECISION_OPEN；"
        "实际=%r" % (observed.get("phase"),)
    )
    assert observed.get("nav_version") == version_before, (
        "普通工具调用不得推进导航版本（决策位置没有变化）"
    )

    assert _anchor(ctx) == target, "后续真实话题工具必须被执行（不得当成迟到调用丢弃）"
    switch_ends = _tool_ends(events, "switch_topic")
    assert [item for item in switch_ends if item.get("ok")] , (
        "switch_topic 必须真实执行成功：%s" % switch_ends
    )
    assert len([item for item in switch_ends if item.get("ok")]) == 1
    echo_ends = _tool_ends(events, "echo")
    assert echo_ends and echo_ends[0].get("ok") is True, "普通工具本身照常执行"
    assert {item["content"] for item in _formal(events)} == {"目标话题里的正式回答"}
    assert _user_message_count(ctx, question) == 1

    phases = _phases(events)
    answerable_at = _index_of(
        events,
        lambda e: getattr(e, "type", None) == EventType.TOPIC_PHASE
        and (getattr(e, "data", None) or {}).get("phase") == "answerable",
    )
    switch_ok_at = _index_of(
        events,
        lambda e: getattr(e, "type", None) == EventType.TOOL_END
        and (getattr(e, "data", None) or {}).get("tool") == "switch_topic"
        and (getattr(e, "data", None) or {}).get("ok") is True,
    )
    first_formal_at = _index_of(
        events,
        lambda e: getattr(e, "type", None) == EventType.ASSISTANT
        and (getattr(e, "data", None) or {}).get("interim") is False,
    )
    assert answerable_at > switch_ok_at, "ANSWERABLE 必须在话题工具真实成功之后：%s" % phases
    assert answerable_at < first_formal_at, "正式回答必须在 ANSWERABLE 之后"


async def test_f01_late_topic_tool_after_answerable_is_not_executed(
    ctx: AppContext, monkeypatch
):
    """真正的迟到：第一次话题工具成功 + 上下文就绪（ANSWERABLE）后，后续导航不执行。

    这是 P1-02 已实现的边界，F01 的修复不得把它退化成「任何工具轮都不执行话题工具」。
    """
    old = _topic(ctx, "旧话题")
    first = _topic(ctx, "目标话题一")
    second = _topic(ctx, "目标话题二")
    for topic_id in (first, second):
        ctx.fragments.get_or_create_open(topic_id)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", first, intent="user_request")]),
            ("", [_switch("c2", second, intent="user_request")]),
            (ANSWER + "\n在目标话题一里的正式回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("换到目标话题一说", topic_id=old)

    assert result.get("ok") is True, result
    assert _anchor(ctx) == first, "ANSWERABLE 之后的迟到导航不得改变本轮归属"
    ok_ends = [item for item in _tool_ends(events, "switch_topic") if item.get("ok")]
    assert len(ok_ends) == 1, ok_ends
    assert {item["content"] for item in _formal(events)} == {"在目标话题一里的正式回答"}


# ---------------------------------------------------------------------------
# F11：只读真实话题解析（查询后仍保持 F01 决策开放）
# ---------------------------------------------------------------------------


async def test_f11_lookup_topic_is_read_only_and_topic_tool_still_allowed_after_query(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    version_before = current_nav_version(ctx.conn)
    observed: dict = {}

    def _phase_now() -> str | None:
        phases = [
            str((getattr(event, "data", None) or {}).get("phase") or "")
            for event in events
            if getattr(event, "type", None) == EventType.TOPIC_PHASE
        ]
        return phases[-1] if phases else None

    def _hook(index: int) -> None:
        if index != 1:
            return
        observed["anchor"] = _anchor(ctx)
        observed["phase"] = _phase_now()
        observed["nav_version"] = current_nav_version(ctx.conn)

    events = _capture(ctx)
    adapter = _install(
        ctx,
        monkeypatch,
        [
            ("", [_lookup("c1", "目标话题")]),
            ("", [_switch("c2", target, intent="user_request")]),
            (ANSWER + "\n目标话题里的正式回答", None),
        ],
    )
    adapter.on_response = _hook

    result = await ctx.run_turn("先查一下目标话题再切过去", topic_id=old)

    assert result.get("ok") is True, result
    lookup_ends = _tool_ends(events, "lookup_topic")
    assert lookup_ends and lookup_ends[0].get("ok") is True, (
        "lookup_topic 必须真实可用并返回成功：%s" % lookup_ends
    )
    assert observed.get("anchor") == old, "只读解析不得改变 Anchor"
    assert observed.get("nav_version") == version_before, "只读解析不得推进导航版本"
    assert observed.get("phase") == "decision_open", (
        "查询之后仍必须保持决策开放（F01）：实际=%r" % (observed.get("phase"),)
    )
    assert _anchor(ctx) == target, "查询之后仍然允许真实话题工具执行"
    assert [item for item in _tool_ends(events, "switch_topic") if item.get("ok")]
    assert {item["content"] for item in _formal(events)} == {"目标话题里的正式回答"}


# ---------------------------------------------------------------------------
# F06：工具/导航成功但归属迁移失败 → 零正式回答 + 可恢复失败
# ---------------------------------------------------------------------------


async def test_f06_rebind_failure_blocks_formal_answer_and_records_ownership_recovery(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())

    async def _rebind_boom(topic_id):  # noqa: ANN001
        raise RuntimeError("注入：本轮归属迁移失败")

    monkeypatch.setattr(ctx.turn_orchestrator, "rebind_turn_to_topic", _rebind_boom)
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n不应发布的正式回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("换到目标话题再回答", topic_id=old)

    # 工具/导航真实成功：事实不回滚、不谎称没发生
    assert _anchor(ctx) == target, "已成功的话题导航不得被回滚"
    assert [item for item in _tool_ends(events, "switch_topic") if item.get("ok")], (
        "工具本身已经成功，这个事实不能被归属失败吞掉"
    )
    # 零正式回答
    assert _formal(events) == [], "归属失败时不得发布任何正式回答"
    # 可观察失败 + 可恢复记录
    phases = _phases(events)
    assert "recoverable_failure" in phases, phases
    assert "answerable" not in phases, "归属失败不得进入可回答阶段：%s" % phases
    errors = [
        item
        for item in _of(events, EventType.ERROR)
        if item.get("code") == "topic_ownership_failed"
    ]
    assert errors, "必须发出可观察的 topic_ownership_failed 错误事件"
    error = errors[0]
    assert error.get("failed_stage") == "ownership", error
    assert error.get("topic_id") == target, error
    assert error.get("recovery"), "错误事件必须带恢复入口事实"

    records = [
        item for item in topic_recovery.list_records(ctx) if item.get("status") == "pending"
    ]
    assert len(records) == 1, records
    record = records[0]
    assert record["failed_stage"] == "ownership", record
    assert record["topic_id"] == target, record
    stages = record.get("stages") or {}
    assert stages.get("tool_success") is True, stages
    assert stages.get("navigation_applied") is True, stages
    assert stages.get("turn_ownership") is False, stages
    assert stages.get("context_ready") is False, stages
    # 数据库里也不能落一条正式回答
    rows = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE role = 'assistant' AND content LIKE ?",
        ("%不应发布的正式回答%",),
    ).fetchone()
    assert int(rows["c"]) == 0
    assert result.get("ok") is not None

# ---------------------------------------------------------------------------
# F06→F03 恢复接缝：归属失败后显式恢复（先修归属，再装配上下文，再回答）
# ---------------------------------------------------------------------------


async def test_f06_ownership_failure_can_be_recovered_from_recovery_record(
    ctx: AppContext, monkeypatch
):
    """归属失败 → 可恢复记录 → retry-answer：先修归属，再装配上下文，再回答；
    不重跑已成功话题工具、不复制原用户消息、最终只有一份答案。"""
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    _use_fake_approvals(ctx, FakeApprovals())
    question = "换到目标话题再回答"

    original_rebind = ctx.turn_orchestrator.rebind_turn_to_topic

    async def _rebind_boom(topic_id):  # noqa: ANN001
        raise RuntimeError("注入：本轮归属迁移失败")

    monkeypatch.setattr(ctx.turn_orchestrator, "rebind_turn_to_topic", _rebind_boom)
    _install(
        ctx,
        monkeypatch,
        [
            ("", [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n不应发布的正式回答", None),
        ],
    )
    _capture(ctx)
    failed = await ctx.run_turn(question, topic_id=old)

    records = [
        item for item in topic_recovery.list_records(ctx) if item.get("status") == "pending"
    ]
    assert len(records) == 1, records
    record = records[0]
    assert record["failed_stage"] == "ownership", record
    record_id = record["record_id"]
    assert _anchor(ctx) == target, "话题导航本身已成功，事实不回滚"

    # 恢复入口：修归属（rebind 重试）→ 装配上下文 → 生成回答。
    monkeypatch.setattr(
        ctx.turn_orchestrator, "rebind_turn_to_topic", original_rebind
    )

    async def _prepare(**_kwargs):  # noqa: ANN003
        return [
            ChatMessage(role="system", content="SYS_FOR:" + target),
            ChatMessage(role="user", content=question),
        ]

    monkeypatch.setattr(
        ctx.turn_orchestrator, "prepare_topic_answer_messages", _prepare
    )
    _install(
        ctx,
        monkeypatch,
        [(ANSWER + "\n归属修复后的正式回答", None)],
    )
    version_before_retry = current_nav_version(ctx.conn)

    retried = await topic_recovery.retry_topic_answer(ctx, record_id)

    assert retried.get("ok") is True, retried
    assert retried.get("answered") is True
    assert retried.get("answer_id"), retried
    # 恢复不重跑已成功的话题工具、不推进导航版本、不新增用户消息/轮次。
    assert current_nav_version(ctx.conn) == version_before_retry
    assert _user_message_count(ctx, question) == 1
    turn_rows = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM turn_bindings WHERE turn_id = ?",
        (str(record.get("turn_id") or ""),),
    ).fetchone()["c"]
    assert int(turn_rows) == 1
    # 归属已修复：原问题只搬一次，落在目标话题；答案出现在原问题之后且只有一份。
    user_row = ctx.conn.execute(
        "SELECT m.fragment_id, f.topic_id FROM messages m "
        "JOIN fragments f ON f.id = m.fragment_id "
        "WHERE m.role = 'user' AND m.content = ?",
        (question,),
    ).fetchone()
    assert user_row is not None and str(user_row["topic_id"]) == target, (
        "恢复必须先修归属：原问题应归属目标话题"
    )
    history = [item["content"] for item in ctx.session_messages(target)]
    assert any("归属修复后的正式回答" in text for text in history), history
    assert sum(1 for text in history if "归属修复后的正式回答" in text) == 1, history
    assert "不应发布的正式回答" not in "".join(history)
    stored = topic_recovery.get_record(ctx, record_id)
    assert stored is not None and stored.get("status") == "answered", stored
    assert failed.get("ok") is not None
