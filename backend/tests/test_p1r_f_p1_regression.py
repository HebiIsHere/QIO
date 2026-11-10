# -*- coding: utf-8 -*-
"""P1R-F 独立回归（集成层）：五项 P1 的端到端行为（真实 AppContext / 编排器 / 主循环）。

只证明接线与分支衔接，不声称真实模型准确率。模型一律 fake/mock；用户决定用
可编程审批替身（FakeApprovals）模拟，数据库/SQLite/话题/片段/绑定/锚点/导航版本
全部走真实实现。

覆盖：
* P1-01：明确请求（intent=user_request）直接执行原工具、不建普通确认；非请求句不导航；
  同轮同一操作的重复工具调用至多执行一次；
* P1-02：正式回答只在 ANSWERABLE 之后发布（阶段顺序，事件级断言）；
* P1-03：上下文装配失败 → 不发布正式回答 + 可恢复记录；恢复入口只重做未完成步骤、
  幂等；重试后仍只有一份用户消息/一份回答；
* P1-04：等待确认期间用户手动导航 → 申请失效、不执行、不覆盖新位置；
* P1-05：查重冲突后同操作同参数版本的合法重试只创建一次（改名贯穿）。

运行：
    cd backend; .venv\\Scripts\\python.exe -m pytest tests/test_p1r_f_p1_regression.py -q
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
    conn = connect(tmp_path / "p1r_f_p1.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


class ScriptedAdapter:
    """非流式脚本 adapter：按顺序返回（正文, 工具调用）；脚本用尽后给补答。"""

    mode = "native"
    model = "fake-p1r-f"
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
    """可编程审批替身：记录申请，按脚本返回决定，可选「决定前先做别的事」。"""

    def __init__(self, *, decisions: list[dict] | None = None, on_request=None) -> None:
        self.decisions = list(decisions or [])
        self.on_request = on_request
        self.requests: list[dict] = []
        self.executions: list[dict] = []

    async def request(self, kind, payload):  # noqa: ANN001
        self.requests.append(
            {"kind": str(kind), "payload": json.loads(json.dumps(payload, default=str))}
        )
        if self.on_request is not None:
            await self.on_request(kind, payload)
        item = self.decisions.pop(0) if self.decisions else {"decision": "rejected"}
        return ApprovalResult(
            str(item.get("approval_id") or f"appr_f_{len(self.requests)}"),
            str(item.get("decision") or "rejected"),
            edits=item.get("edits"),
        )

    async def record_legacy_execution(
        self, approval_id, *, ok, error=None, summary=None
    ):  # noqa: ANN001
        self.executions.append(
            {"approval_id": approval_id, "ok": bool(ok), "error": error, "summary": summary}
        )
        return {"phase": "executed" if ok else "failed"}

    # 编排器每轮会把 turn/session 身份同步进审批服务（真实服务的方法；替身保持同形）。
    def set_context(self, *, turn_id=None, session_id=None):  # noqa: ANN001
        self.context = {"turn_id": turn_id, "session_id": session_id}


def _use_fake_approvals(ctx: AppContext, fake: "FakeApprovals") -> "FakeApprovals":
    """替换本轮的审批通道（桥从 app.approvals 取），并同步工具实例的引用。

    注意：`CreateTopicTool` 在 AppContext 构造时就持有一份 ApprovalService 引用
    （用于「查重冲突的二次人工审批」）。只替换 `app.approvals` 不会影响工具内部
    那次申请 —— 会走真实服务并一直等用户（测试表现为挂起）。这里两处一起换。
    """
    ctx.approvals = fake
    try:
        tool = ctx.registry.get("create_topic")
    except Exception:  # noqa: BLE001 - 老注册表没有 get 时按无工具处理
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


def _switch(call_id: str, topic_id: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"topic_id": topic_id, "reason": "这段内容属于该话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="switch_topic", arguments=arguments)


def _create(call_id: str, name: str, *, intent: str | None = None) -> ToolCall:
    arguments: dict = {"name": name, "reason": "这是独立的新话题"}
    if intent is not None:
        arguments["intent"] = intent
    return ToolCall(id=call_id, name="create_topic", arguments=arguments)


def _topic_names(ctx: AppContext) -> list[str]:
    return [node.name for node in ctx.topics.nodes.list_topics()]


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
# P1-01 / P1-02：明确请求直达原工具、非请求不导航、同轮至多一次、正文门顺序
# ---------------------------------------------------------------------------


async def test_p1_01_user_request_switch_executes_directly_with_one_formal_answer(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    approvals = _use_fake_approvals(ctx, FakeApprovals())  # 不允许任何普通审批
    _install(
        ctx,
        monkeypatch,
        [
            (None, [_switch("c1", target, intent="user_request")]),
            (ANSWER + "\n目标话题里的正式回答", None),
        ],
    )
    events = _capture(ctx)
    question = "我们换到目标话题继续"

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    assert approvals.requests == [], "intent=user_request 不得建立普通话题确认申请"
    assert _anchor(ctx) == target, "明确请求应当由原工具直接完成导航"
    formal = _formal(events)
    assert formal, "必须发布正式回答"
    assert {item["content"] for item in formal} == {"目标话题里的正式回答"}
    phases = _phases(events)
    assert phases and phases[0] == "decision_open", phases
    assert "executing" in phases and "context_preparing" in phases and "answerable" in phases, phases
    answerable_at = _index_of(
        events,
        lambda e: getattr(e, "type", None) == EventType.TOPIC_PHASE
        and (getattr(e, "data", None) or {}).get("phase") == "answerable",
    )
    first_formal_at = _index_of(
        events,
        lambda e: getattr(e, "type", None) == EventType.ASSISTANT
        and (getattr(e, "data", None) or {}).get("interim") is False,
    )
    assert answerable_at < first_formal_at, "正式回答必须在 ANSWERABLE 之后发布（契约 §3/§8.4）"
    ends = [
        item
        for item in _of(events, EventType.TOOL_END)
        if item.get("tool") == "switch_topic"
    ]
    assert len([item for item in ends if item.get("ok")]) == 1, "话题切换只能真的执行一次"
    assert _user_message_count(ctx, question) == 1, "原问题只能存一份"


async def test_p1_01_non_request_text_does_not_navigate_or_apply(ctx: AppContext, monkeypatch):
    old = _topic(ctx, "原神")
    approvals = _use_fake_approvals(ctx, FakeApprovals())
    ctx.navigation.enter_topic(old)
    _install(ctx, monkeypatch, [("好的，我继续在这里回答。", None)])
    events = _capture(ctx)

    result = await ctx.run_turn("不要切换到原神话题，继续回答这里的问题", topic_id=old)

    assert result.get("ok") is True, result
    assert approvals.requests == [], "非请求句不得建立话题申请"
    assert _anchor(ctx) == old, "非请求句不得移动对话位置"
    assert _formal(events), "在当前位置给出正式回答"
    assert not [
        item for item in _of(events, EventType.TOOL_END) if item.get("tool") in {
            "switch_topic", "create_topic",
        }
    ], "程序/模型都不应执行话题工具"


async def test_p1_01_duplicate_same_turn_topic_calls_execute_at_most_once(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    approvals = _use_fake_approvals(ctx, FakeApprovals())
    _install(
        ctx,
        monkeypatch,
        [
            (
                None,
                [
                    _switch("c1", target, intent="user_request"),
                    _switch("c2", target, intent="user_request"),
                ],
            ),
            (ANSWER + "\n换好了", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn("换到目标话题", topic_id=old)

    assert result.get("ok") is True, result
    assert _anchor(ctx) == target
    ok_ends = [
        item
        for item in _of(events, EventType.TOOL_END)
        if item.get("tool") == "switch_topic" and item.get("ok")
    ]
    assert len(ok_ends) == 1, "同轮同一操作的重复工具调用至多执行一次：%s" % ok_ends
    assert _user_message_count(ctx, "换到目标话题") == 1


# ---------------------------------------------------------------------------
# P1-03：上下文装配失败可恢复（无旧上下文回答、记录不丢、重试幂等）
# ---------------------------------------------------------------------------


async def test_p1_03_context_prepare_failure_is_recoverable_and_retry_is_idempotent(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    ctx.fragments.get_or_create_open(target)
    ctx.navigation.enter_topic(old)
    approvals = _use_fake_approvals(ctx, FakeApprovals(decisions=[{"decision": "approved"}]))
    adapter = _install(
        ctx,
        monkeypatch,
        [(None, [_switch("c1", target)]), (ANSWER + "\n不应出现的旧上下文回答", None)],
    )

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("注入：目标话题上下文装配失败")

    monkeypatch.setattr(ctx.turn_orchestrator, "rebuild_system_after_topic_application", _boom)
    events = _capture(ctx)
    question = "换成目标话题再回答"

    result = await ctx.run_turn(question, topic_id=old)

    assert _formal(events) == [], "上下文装配失败不得发布正式回答（也不能用旧 system 回答）"
    errors = [item for item in _of(events, EventType.ERROR) if item.get("code") == "topic_context_prepare_failed"]
    assert errors, "必须发出可观察的 topic_context_prepare_failed 错误事件"
    records = [
        item for item in topic_recovery.list_records(ctx) if item.get("status") == "pending"
    ]
    assert len(records) == 1, records
    record = records[0]
    assert record["failed_stage"] == "context_prepare"
    assert record["topic_id"] == target
    assert record.get("user_message_id"), "恢复记录必须保存原用户消息 id（不重发消息）"
    assert _anchor(ctx) == target, "工具真实切换已发生；失败的是回答准备"
    assert _user_message_count(ctx, question) == 1
    assert result.get("ok") is not None  # 不假装是普通成功；具体形态由上层决定

    # 恢复：只重做上下文装配 + 回答；不重跑话题工具、不新增用户消息。
    # 用户没有在失败后再导航 → 恢复应当成功（不得被「切换前」的旧版本卡成 stale）。
    retry = await topic_recovery.retry_topic_answer(ctx, record["record_id"])
    assert retry.get("ok") is True, (
        "上下文失败后、用户没有新导航时，恢复入口必须能继续；实际=%s" % retry
    )
    assert retry.get("answered") is True
    again = await topic_recovery.retry_topic_answer(ctx, record["record_id"])
    assert again.get("answered") is True
    assert again.get("answer_id") == retry.get("answer_id"), "重复恢复必须返回既有结果（幂等）"
    assert _user_message_count(ctx, question) == 1, "恢复不得新增用户消息"
    assert adapter.requests, "恢复必须真的调用模型生成回答"


# ---------------------------------------------------------------------------
# P1-04：等待期间用户手动导航 → 申请失效，不执行、不覆盖新位置
# ---------------------------------------------------------------------------


async def test_p1_04_manual_navigation_while_waiting_invalidates_application(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    target = _topic(ctx, "目标话题")
    other = _topic(ctx, "用户后来点开的话题")
    for topic_id in (target, other):
        ctx.fragments.get_or_create_open(topic_id)
    ctx.navigation.enter_topic(old)
    question = "换成目标话题说"

    async def _navigate_during_wait(kind, payload):  # noqa: ANN001
        # 模拟另一个请求里用户手动导航：Anchor 变了，本轮 binding 不变。
        ctx.navigation.enter_topic(other)

    approvals = _use_fake_approvals(
        ctx,
        FakeApprovals(decisions=[{"decision": "approved"}], on_request=_navigate_during_wait),
    )
    _install(
        ctx,
        monkeypatch,
        [(None, [_switch("c1", target)]), (ANSWER + "\n在原话题回答", None)],
    )
    events = _capture(ctx)

    result = await ctx.run_turn(question, topic_id=old)

    assert _anchor(ctx) == other, "批准后的旧申请不得覆盖用户后来的手动导航"
    assert _formal(events), "失效不是拒绝：应当在当前（新）位置给出回答"
    assert {item["content"] for item in _formal(events)} == {"在原话题回答"}
    payload = approvals.requests[0]["payload"]
    assert payload.get("base_nav_version") is not None, "申请必须带真实起点快照"
    from agent.services import navigation_version

    assert navigation_version.current(ctx.conn) != payload["base_nav_version"], (
        "用户手动导航必须推进导航版本（否则旧申请无法判定失效）"
    )
    # 不执行：目标话题没有被切入，也没有归属搬迁
    assert not [
        item for item in _of(events, EventType.TOOL_END) if item.get("tool") == "switch_topic" and item.get("ok")
    ], "失效的申请不得执行话题工具"
    assert result.get("ok") is not None


# ---------------------------------------------------------------------------
# P1-05：改名 A→B、查重冲突、同操作同版本合法重试只创建一次
# ---------------------------------------------------------------------------


async def test_p1_05_renamed_create_conflict_retry_creates_only_once(
    ctx: AppContext, monkeypatch
):
    old = _topic(ctx, "旧话题")
    existing = _topic(ctx, "布局与间距")  # 与用户后来确认的名称相近/相同，触发查重冲突
    ctx.navigation.enter_topic(old)
    before = _topic_names(ctx)
    question = "把讨论拆成一个新话题"

    approvals = _use_fake_approvals(
        ctx,
        FakeApprovals(
            decisions=[
                {"decision": "approved", "edits": {"name": "布局与间距"}},  # 用户在弹窗里改名
                {"decision": "approved"},  # 查重冲突的二次审批
            ]
        ),
    )
    _install(
        ctx,
        monkeypatch,
        [
            (None, [_create("c1", "布局间距问题")]),
            (None, [_create("c2", "布局与间距")]),  # 模型按用户确认后的名称合法重试
            (ANSWER + "\n新话题里的回答", None),
        ],
    )
    events = _capture(ctx)

    result = await ctx.run_turn(question, topic_id=old)

    assert result.get("ok") is True, result
    topic_requests = [item for item in approvals.requests if item["kind"] == "topic_create"]
    assert len(topic_requests) == 1, "一次逻辑操作只能建立一条普通话题申请：%s" % approvals.requests
    inner = [item for item in approvals.requests if item["kind"] == "create_topic"]
    assert inner, "查重冲突必须保留工具内部的二次人工审批"
    after = _topic_names(ctx)
    assert len(after) == len(before) + 1, "只创建一次：before=%s after=%s" % (before, after)
    assert len([name for name in after if name == "布局与间距"]) == 2, "用户确认后的名称是实际创建名称"
    assert _formal(events), "话题创建后仍要给出正式回答"
    assert _user_message_count(ctx, question) == 1
