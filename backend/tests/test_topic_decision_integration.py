# -*- coding: utf-8 -*-
"""统一话题判断的接线集成测试（契约 §2.3 / §4）——前半（不依赖 B 的申请流程）。

覆盖：
1. 延续 / 纠正 / 追问轮不产生任何审批请求，正常回答；
2. 强制高相似 prediction 时不发 TOPIC_SWITCH_SUGGESTED、Anchor 不移动、
   pending_switch 为 None；
3. system 里确实注入了 topic_decision note（真实 note + monkeypatch sentinel 两种）；
4. ctx.topic_refusal_scope 作为证据传入 build_topic_decision_state 并进入 system。

申请 / 确认 / 拒绝的完整流程（B 的 bridge + loop）等 Lead 通知后补后半。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services import topic_decision
from agent.services.app import AppContext
from agent.services.predict import TopicPrediction
from agent.services.topic_application import get_topic_application_state
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult, TopicNameRequired

# 本文件验证话题申请的真实审批交互：关闭 conftest 的旧测试自动批准兜底。
pytestmark = pytest.mark.topic_application


# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "topic_decision_integration.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class RecordingAdapter:
    """记录主模型实际收到的 system / tools，并返回一段正式回答。"""

    mode = "native"
    model = "fake-model"

    def __init__(self, content: str = "[[QIO:ANSWER]]好的。") -> None:
        self.content = content
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        return Completion(message=ChatMessage(role="assistant", content=self.content))


def _install_adapter(ctx: AppContext, monkeypatch, adapter: RecordingAdapter) -> None:
    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)


def _force_prediction(ctx: AppContext, monkeypatch, prediction: TopicPrediction) -> None:
    """固定本轮话题预判（不跑 embedding；预测器只是证据来源）。"""

    async def fake_predict(turn_ctx, message, topic):
        return prediction

    monkeypatch.setattr(ctx.turn_orchestrator, "_predict_topics", fake_predict)


def _system_text(adapter: RecordingAdapter) -> str:
    assert adapter.requests, "模型没有被调用"
    system = adapter.requests[0]["messages"][0]
    assert system.role == "system"
    return system.content


async def _collect(ctx: AppContext, event_type: str):
    """收集某类总线事件（与既有 test_topic_switch_policy 相同写法）。"""
    collected: list[dict] = []

    async def consumer():
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] == event_type:
                        collected.append(evt["data"])

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish():
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return collected, finish


def _no_approval(ctx: AppContext, monkeypatch) -> list[str]:
    """记录审批请求；本轮不应出现任何话题审批。"""
    calls: list[str] = []

    async def fake_request(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        calls.append(str(kind))
        return SimpleNamespace(decision="rejected", approval_id=None)

    monkeypatch.setattr(ctx.approvals, "request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# 1. 延续 / 纠正 / 追问：不申请、正常回答
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "把刚才的结论再整理一下",
        "不对，温度应该是 150 度",
        "那第二步具体怎么做？",
    ],
)
async def test_continuation_correction_followup_do_not_request_topic_change(
    ctx: AppContext, monkeypatch, message: str
):
    topic = ctx.topics.nodes.create_topic("当前事项").id
    ctx.navigation.enter_topic(topic)
    adapter = RecordingAdapter()
    _install_adapter(ctx, monkeypatch, adapter)
    _force_prediction(
        ctx,
        monkeypatch,
        TopicPrediction(
            main_topic_id=None,
            is_new_topic_candidate=False,
            scores={},
            backend_used="rules",
        ),
    )
    approval_calls = _no_approval(ctx, monkeypatch)
    collected, finish = await _collect(ctx, "TOPIC_SWITCH_SUGGESTED")

    result = await ctx.run_turn(message, topic_id=topic)
    await finish()

    assert result["ok"] is True
    assert result["turn"]["final_content"] == "好的。"
    assert approval_calls == [], "延续/纠正/追问不得发起话题申请"
    assert collected == [], "旧预测器路径不得再发布可执行切换建议"
    assert ctx.navigation.pending_switch() is None
    assert AnchorService(ctx.conn).get_active().topic_id == topic


# ---------------------------------------------------------------------------
# 2. 高相似度：只是证据，不发布可执行建议、不移动 Anchor
# ---------------------------------------------------------------------------


async def test_high_similarity_prediction_does_not_publish_executable_switch(
    ctx: AppContext, monkeypatch
):
    current = ctx.topics.nodes.create_topic("当前事项").id
    other = ctx.topics.nodes.create_topic("橡胶实验").id
    ctx.navigation.enter_topic(current)
    adapter = RecordingAdapter()
    _install_adapter(ctx, monkeypatch, adapter)
    _force_prediction(
        ctx,
        monkeypatch,
        TopicPrediction(
            main_topic_id=other,
            aux_topic_ids=[other],
            is_new_topic_candidate=False,
            suggested_switch=True,
            scores={other: 0.97},
            backend_used="onnx",
        ),
    )
    approval_calls = _no_approval(ctx, monkeypatch)
    collected, finish = await _collect(ctx, "TOPIC_SWITCH_SUGGESTED")

    result = await ctx.run_turn("之前那个橡胶实验后来怎么样了", topic_id=current)
    await finish()

    assert result["ok"] is True
    assert approval_calls == []
    assert collected == [], "预测器/分类器不得再发布可执行 TOPIC_SWITCH_SUGGESTED"
    assert ctx.navigation.pending_switch() is None, "高相似度不等于自动触发切换"
    assert AnchorService(ctx.conn).get_active().topic_id == current

    system = _system_text(adapter)
    # note 正文（注入形状由 Lead/契约测试管；这里只断言判断证据确实进了 system）
    assert "话题判断（本轮证据与规则）：" in system
    assert "橡胶实验" in system, "候选话题作为证据进入 system"
    assert "相似度只是证据" in system


# ---------------------------------------------------------------------------
# 3. note 注入：orchestrator 把渲染结果放进 system
# ---------------------------------------------------------------------------


async def test_orchestrator_injects_rendered_note_into_system(ctx: AppContext, monkeypatch):
    topic = ctx.topics.nodes.create_topic("当前事项").id
    ctx.navigation.enter_topic(topic)
    adapter = RecordingAdapter()
    _install_adapter(ctx, monkeypatch, adapter)
    _force_prediction(
        ctx,
        monkeypatch,
        TopicPrediction(main_topic_id=None, scores={}, backend_used="rules"),
    )
    sentinel = "A-SENTINEL：本轮判断证据已由 orchestrator 注入。"
    monkeypatch.setattr(
        topic_decision, "render_topic_decision_note", lambda *a, **k: sentinel
    )

    result = await ctx.run_turn("继续当前讨论", topic_id=topic)

    assert result["ok"] is True
    assert sentinel in _system_text(adapter)


async def test_unable_to_judge_answers_normally_with_evidence(ctx: AppContext, monkeypatch):
    topic = ctx.topics.nodes.create_topic("当前事项").id
    ctx.navigation.enter_topic(topic)
    adapter = RecordingAdapter()
    _install_adapter(ctx, monkeypatch, adapter)
    _force_prediction(
        ctx,
        monkeypatch,
        TopicPrediction(
            main_topic_id=None,
            is_new_topic_candidate=True,
            scores={},
            backend_used="rules",
        ),
    )

    result = await ctx.run_turn("嗯", topic_id=topic)

    assert result["ok"] is True, "无法判断时也必须正常回答"
    assert result["turn"]["final_content"] == "好的。"
    system = _system_text(adapter)
    assert "话题判断（本轮证据与规则）：" in system
    assert "当前没有可切换的已有话题候选" in system
    assert "不代表应该新建" in system
    assert "暂时无法判断" in system


# ---------------------------------------------------------------------------
# 4. 拒绝范围：ctx.topic_refusal_scope 传入 build_state 并进入 system
# ---------------------------------------------------------------------------


async def test_turn_refusal_scope_is_passed_and_rendered(ctx: AppContext, monkeypatch):
    current = ctx.topics.nodes.create_topic("当前事项").id
    other = ctx.topics.nodes.create_topic("橡胶实验").id
    ctx.navigation.enter_topic(current)
    adapter = RecordingAdapter()
    _install_adapter(ctx, monkeypatch, adapter)
    _force_prediction(
        ctx,
        monkeypatch,
        TopicPrediction(
            main_topic_id=other,
            aux_topic_ids=[other],
            scores={other: 0.9},
            suggested_switch=True,
            backend_used="onnx",
        ),
    )
    refused_scope = {
        # B 的 RefusedAttempt.as_dict() 形状：topic_id/topic_name 是目标
        "topic_id": other,
        "topic_name": "橡胶实验",
        "action": "switch",
        "reason": "rejected",
        "turn_id": "turn-refused",
        "user_message_id": "msg-refused",
        "fragment_id": "frag-refused",
        "created_at": "2026-11-10T00:00:00+00:00",
    }
    recorded: list = []
    real_build_state = topic_decision.build_topic_decision_state

    def spy_build_state(app, turn_ctx, topic_id, prediction, refuse_scope=None):
        recorded.append(refuse_scope)
        return real_build_state(
            app, turn_ctx, topic_id, prediction, refuse_scope=refuse_scope
        )

    monkeypatch.setattr(topic_decision, "build_topic_decision_state", spy_build_state)
    original_build_context = ctx.turn_orchestrator.build_context

    async def wrapped_build_context(turn_ctx, turn_adapter):
        turn_ctx.topic_refusal_scope = refused_scope
        return await original_build_context(turn_ctx, turn_adapter)

    monkeypatch.setattr(ctx.turn_orchestrator, "build_context", wrapped_build_context)

    result = await ctx.run_turn("继续当前这个", topic_id=current)

    assert result["ok"] is True
    assert recorded == [refused_scope], "拒绝范围必须作为证据传入 build_topic_decision_state"
    system = _system_text(adapter)
    assert "申请切换到已有话题「橡胶实验」，用户选择在当前话题回答" in system
    # 当前话题仍是原话题（拒绝不等于切走）
    assert AnchorService(ctx.conn).get_active().topic_id == current


# ---------------------------------------------------------------------------
# 后半：申请 → 确认 / 拒绝 → 执行与归属（B 的 bridge + loop）
# ---------------------------------------------------------------------------


class ScriptedAdapter:
    """按脚本返回工具调用 / 最终回答的假主模型。"""

    mode = "native"
    model = "fake-model"

    def __init__(self, script) -> None:
        self.script = list(script)
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.script:
            return self.script.pop(0)
        return _final_answer("好的。")


def _tool_call(name: str, arguments: dict, call_id: str = "call-1") -> Completion:
    return Completion(
        message=ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        )
    )


def _final_answer(text: str = "好的。") -> Completion:
    return Completion(
        message=ChatMessage(role="assistant", content="[[QIO:ANSWER]]" + text)
    )


def _approval_stub(
    ctx: AppContext,
    monkeypatch,
    decision: str,
    *,
    edits: dict | None = None,
    on_request=None,
):
    """确定性的审批桩：记录 (kind, payload)，按给定决定返回 ApprovalResult。"""
    recorded: list[dict] = []

    async def fake_request(kind, payload=None, **kwargs):  # noqa: ANN001, ANN003
        recorded.append({"kind": str(kind), "payload": dict(payload or {})})
        if on_request is not None:
            await on_request(kind, payload)
        return ApprovalResult(
            approval_id="appr-integration", decision=decision, edits=edits
        )

    monkeypatch.setattr(ctx.approvals, "request", fake_request)
    return recorded


def _topic_messages(ctx: AppContext, topic_id: str) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT m.role AS role, m.content AS content FROM messages m "
        "JOIN fragments f ON m.fragment_id = f.id "
        "WHERE f.topic_id = ? ORDER BY m.created_at, m.id",
        (topic_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _topic_fragment_count(ctx: AppContext, topic_id: str) -> int:
    row = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def _has_tool_message(requests: list[dict], needle: str) -> bool:
    for request in requests:
        for message in request["messages"]:
            if getattr(message, "role", "") == "tool" and needle in str(
                getattr(message, "content", "") or ""
            ):
                return True
    return False


async def _collect_events(ctx: AppContext):
    """收集本轮全部总线事件（用于断言未确认前没有正式回答事件）。"""
    events: list[dict] = []

    async def consumer():
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish():
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return events, finish


async def test_approved_switch_executes_tool_rebinds_and_answers_in_target(
    ctx: AppContext, monkeypatch
):
    current = ctx.topics.nodes.create_topic("当前事项").id
    target = ctx.topics.nodes.create_topic("橡胶实验").id
    ctx.navigation.enter_topic(current)
    adapter = ScriptedAdapter(
        [
            _tool_call(
                "switch_topic",
                {"topic_id": target, "reason": "这段内容属于橡胶实验"},
            ),
            _final_answer("已转到橡胶实验，结论如下。"),
        ]
    )
    _install_adapter(ctx, monkeypatch, adapter)
    approvals = _approval_stub(ctx, monkeypatch, "approved")

    result = await ctx.run_turn("之前那个橡胶实验的结论整理一下", topic_id=current)

    assert result["ok"] is True
    assert result["turn"]["stop_reason_code"] == "none"
    # 申请载荷严格按契约，且不放原问题
    assert len(approvals) == 1
    request = approvals[0]
    assert request["kind"] == "topic_switch"
    payload = request["payload"]
    assert payload["action"] == "switch"
    assert payload["from_topic_id"] == current
    assert payload["target_topic_id"] == target
    assert payload["target_topic_name"] == "橡胶实验"
    assert payload.get("source") == "model"
    assert "之前那个橡胶实验的结论整理一下" not in json.dumps(
        payload, ensure_ascii=False
    )
    # 工具真实执行：Anchor 与绑定都到目标话题
    assert AnchorService(ctx.conn).get_active().topic_id == target
    # 提问与回答都只在目标话题各一份；原话题不重复
    target_messages = _topic_messages(ctx, target)
    assert [m["role"] for m in target_messages] == ["user", "assistant"]
    assert "之前那个橡胶实验的结论整理一下" in target_messages[0]["content"]
    assert "已转到橡胶实验" in target_messages[1]["content"]
    assert _topic_messages(ctx, current) == []
    # 重建后的 system 是目标话题上下文（不是原话题上下文）
    assert len(adapter.requests) >= 2
    rebuilt_system = adapter.requests[1]["messages"][0].content
    assert "用户已确认把本轮转到话题「橡胶实验」" in rebuilt_system


async def test_rejected_switch_keeps_current_topic_and_records_refusal(
    ctx: AppContext, monkeypatch
):
    current = ctx.topics.nodes.create_topic("当前事项").id
    target = ctx.topics.nodes.create_topic("橡胶实验").id
    ctx.navigation.enter_topic(current)
    adapter = ScriptedAdapter(
        [
            _tool_call("switch_topic", {"topic_id": target, "reason": "看起来属于橡胶实验"}),
            _final_answer("好，就在当前话题继续回答。"),
        ]
    )
    _install_adapter(ctx, monkeypatch, adapter)
    _approval_stub(ctx, monkeypatch, "rejected")

    result = await ctx.run_turn("之前那个橡胶实验的结论整理一下", topic_id=current)

    assert result["ok"] is True
    assert result["turn"]["stop_reason_code"] == "none", "拒绝不得把整轮标成用户停止"
    # 工具没有执行：Anchor 不动、目标话题没有任何片段/消息
    assert AnchorService(ctx.conn).get_active().topic_id == current
    assert _topic_fragment_count(ctx, target) == 0
    assert _topic_messages(ctx, target) == []
    # 正式回答留在原话题
    current_messages = _topic_messages(ctx, current)
    assert [m["role"] for m in current_messages] == ["user", "assistant"]
    assert "就在当前话题继续回答" in current_messages[1]["content"]
    # 拒绝范围进入 guard（下一轮证据来源）
    guard = get_topic_application_state(ctx)
    refused = guard.refused_attempts
    assert refused, "拒绝必须留下范围证据"
    assert refused[-1]["action"] == "switch"
    assert refused[-1]["topic_name"] == "橡胶实验"
    assert refused[-1]["reason"] == "rejected"
    # 模型收到的是「不要重复申请、直接回答」，且第二段 system 没有确认换题文案
    assert _has_tool_message(adapter.requests, "请直接在当前话题")
    rebuilt_system = adapter.requests[1]["messages"][0].content
    assert "用户已确认把本轮转到话题" not in rebuilt_system


async def test_approved_create_uses_user_edited_name(ctx: AppContext, monkeypatch):
    current = ctx.topics.nodes.create_topic("当前事项").id
    ctx.navigation.enter_topic(current)
    adapter = ScriptedAdapter(
        [
            _tool_call("create_topic", {"name": "量子计算", "reason": "全新方向"}),
            _final_answer("已新建量子计算实验话题。"),
        ]
    )
    _install_adapter(ctx, monkeypatch, adapter)
    approvals = _approval_stub(
        ctx, monkeypatch, "approved", edits={"name": "量子计算实验"}
    )

    result = await ctx.run_turn("另起一个量子计算的讨论", topic_id=current)

    assert result["ok"] is True
    names = {node.name for node in ctx.topics.nodes.list_topics()}
    assert "量子计算实验" in names, "必须用用户改过的新名称创建"
    assert "量子计算" not in names, "不得按模型原名创建"
    created = next(n for n in ctx.topics.nodes.list_topics() if n.name == "量子计算实验")
    assert AnchorService(ctx.conn).get_active().topic_id == created.id
    messages = _topic_messages(ctx, created.id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "另起一个量子计算的讨论" in messages[0]["content"]
    # 申请载荷给的是模型原名，可编辑；真实落库用 edits
    assert approvals[0]["kind"] == "topic_create"
    assert approvals[0]["payload"]["action"] == "create"
    assert approvals[0]["payload"]["new_name"] == "量子计算"


async def test_empty_edited_name_is_rejected_and_approval_stays_pending(
    ctx: AppContext, monkeypatch
):
    """契约 §2.2：topic_create 空名称 → 400 topic_name_required，申请保持 pending、不执行。

    这里不 monkeypatch approvals.request：走真实 ApprovalService（身份 / 校验 /
    600s 有效期 / 单次消费），只在旁路调用 respond 驱动决定。
    """
    current = ctx.topics.nodes.create_topic("当前事项").id
    ctx.navigation.enter_topic(current)
    adapter = ScriptedAdapter(
        [
            _tool_call("create_topic", {"name": "量子计算", "reason": "全新方向"}),
            _final_answer("已创建并转到量子计算话题。"),
        ]
    )
    _install_adapter(ctx, monkeypatch, adapter)
    events, finish = await _collect_events(ctx)
    turn_task = asyncio.create_task(
        ctx.run_turn("另起一个量子计算的讨论", topic_id=current)
    )
    approval = None
    for _ in range(500):
        for event in list(events):
            if event["type"] == "APPROVAL_REQUIRED":
                approval = event["data"]["approval"]
                break
        if approval is not None:
            break
        await asyncio.sleep(0.01)
    assert approval is not None, "create_topic 应建立话题申请并发布 APPROVAL_REQUIRED"
    assert approval["kind"] == "topic_create"
    approval_id = approval["approval_id"]
    before = {node.id for node in ctx.topics.nodes.list_topics()}

    # 空名称在 future.set_result 之前被拒：400 语义、申请不被消费、工具不执行
    with pytest.raises(TopicNameRequired):
        await ctx.approvals.respond(approval_id, "approved", edits={"name": "   "})
    after_invalid = {
        "topics": {node.id for node in ctx.topics.nodes.list_topics()},
        "anchor": AnchorService(ctx.conn).get_active().topic_id,
    }
    # 申请仍在等待：不加 edits 正常批准可以成功，工具按模型原名执行
    approved_again = await ctx.approvals.respond(approval_id, "approved")
    result = await asyncio.wait_for(turn_task, timeout=30)
    await finish()

    assert after_invalid["topics"] == before, "空名称不得创建话题"
    assert after_invalid["anchor"] == current
    assert approved_again is True, "空名称被拒后申请必须保持 pending（可再次正常批准）"
    assert result["ok"] is True
    names = {node.name for node in ctx.topics.nodes.list_topics()}
    assert "量子计算" in names
    created = next(n for n in ctx.topics.nodes.list_topics() if n.name == "量子计算")
    assert AnchorService(ctx.conn).get_active().topic_id == created.id
    assert [m["role"] for m in _topic_messages(ctx, created.id)] == ["user", "assistant"]


async def test_no_tool_side_effect_or_answer_before_approval_decision(
    ctx: AppContext, monkeypatch
):
    current = ctx.topics.nodes.create_topic("当前事项").id
    target = ctx.topics.nodes.create_topic("橡胶实验").id
    ctx.navigation.enter_topic(current)
    adapter = ScriptedAdapter(
        [
            _tool_call("switch_topic", {"topic_id": target, "reason": "属于橡胶实验"}),
            _final_answer("已转到橡胶实验。"),
        ]
    )
    _install_adapter(ctx, monkeypatch, adapter)
    gate = asyncio.Event()
    requested: list[dict] = []

    async def on_request(kind, payload):
        requested.append({"kind": str(kind), "payload": dict(payload or {})})
        await gate.wait()

    _approval_stub(ctx, monkeypatch, "approved", on_request=on_request)
    events, finish = await _collect_events(ctx)
    turn_task = asyncio.create_task(
        ctx.run_turn("之前那个橡胶实验的结论整理一下", topic_id=current)
    )
    for _ in range(500):
        if requested:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    observed = {
        "requested": bool(requested),
        "anchor": AnchorService(ctx.conn).get_active().topic_id,
        "target_fragments": _topic_fragment_count(ctx, target),
        "target_messages": _topic_messages(ctx, target),
        "current_assistant": [
            m for m in _topic_messages(ctx, current) if m["role"] == "assistant"
        ],
        "answer_events": [e for e in events if e["type"] == "ASSISTANT"],
        "turn_end_events": [e for e in events if e["type"] == "TURN_END"],
    }
    gate.set()
    result = await turn_task
    await finish()

    assert observed["requested"], "模型调用话题工具后应建立申请"
    # 未确认前：工具没执行（Anchor/目标片段不变）、没有正式回答、本轮未结束
    assert observed["anchor"] == current
    assert observed["target_fragments"] == 0
    assert observed["target_messages"] == []
    assert observed["current_assistant"] == [], "未确认前不得写出正式回答"
    assert observed["answer_events"] == [], "未确认前不得发布正式回答事件"
    assert observed["turn_end_events"] == [], "未确认前本轮不应结束"
    # 确认之后才真正执行并落到目标话题
    assert result["ok"] is True
    assert AnchorService(ctx.conn).get_active().topic_id == target
    assert [m["role"] for m in _topic_messages(ctx, target)] == ["user", "assistant"]

    assert [m["role"] for m in _topic_messages(ctx, target)] == ["user", "assistant"]


async def test_cross_turn_refusal_scope_reaches_next_turn_evidence(
    ctx: AppContext, monkeypatch
):
    """同一开放片段 = 同一段讨论：上一轮被拒绝的换题范围应进入下一轮 system 证据。"""
    current = ctx.topics.nodes.create_topic("当前事项").id
    target = ctx.topics.nodes.create_topic("橡胶实验").id
    ctx.navigation.enter_topic(current)
    first_adapter = ScriptedAdapter(
        [
            _tool_call("switch_topic", {"topic_id": target, "reason": "属于橡胶实验"}),
            _final_answer("好，就在当前话题继续回答。"),
        ]
    )
    _install_adapter(ctx, monkeypatch, first_adapter)
    _approval_stub(ctx, monkeypatch, "rejected")

    first = await ctx.run_turn("之前那个橡胶实验的结论整理一下", topic_id=current)
    assert first["ok"] is True
    assert get_topic_application_state(ctx).refused_attempts

    second_adapter = ScriptedAdapter([_final_answer("继续当前话题。")])
    _install_adapter(ctx, monkeypatch, second_adapter)

    second = await ctx.run_turn("继续刚才那个", topic_id=current)

    assert second["ok"] is True
    system = second_adapter.requests[0]["messages"][0].content
    assert "话题判断（本轮证据与规则）：" in system
    assert "申请切换到已有话题「橡胶实验」，用户选择在当前话题回答" in system
    assert "同一段讨论不要重复申请" in system
