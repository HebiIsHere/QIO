# -*- coding: utf-8 -*-
"""R15-R1 / P1：位置快照必须早于异步上下文装配（A 角色定向测试）。

契约 §3 固定住的验收点（真实 AppContext.run_turn + 真实临时 SQLite + fake
scripted adapter；时序全部用 asyncio.Event 屏障，不用 sleep）：

1. 装配中改选历史（主场景）：build_context 入口用事件暂停；暂停期间在同一话题
   执行真实的「改选历史片段」导航（推进版本）；释放装配与模型响应后，模型返回
   旧依据下的 switch_topic 调用。断言旧操作未执行（目标 Anchor 未被覆盖）、
   用户新选择仍在、导航版本未被旧操作推进、本轮正式回答仍按原绑定归属保存。
2. 装配期间切走再返回：真实 enter_topic 离开 + 返回（版本推进）→ 旧操作由写入前
   CAS 判 stale；回答原归属完成。
3. 装配期间待接续位置改变：register_continuation 改选另一历史片段（版本推进）
   → stale。
4. 正常无导航变化对照：快照版本=装配后当前版本，操作正常执行成功，快照做因果
   更新（decision_basis_state="causal"）。
5. 顺序与字段：spy 记录快照写入 vs build_context 完成顺序，证明快照早于装配；
   断言快照字段（turn/user_message/intent/topic/fragment/nav/queued）真实写入、
   位置字段装配后不再被改写。
6. 失效 ≠ 拒绝：superseded 后不弹申请、不写拒绝证据、不跳回旧位置。

全部使用 fake provider（不联网、不读真实 Key）。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.core.turn import TurnContext
from agent.graph.anchors import AnchorService
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.predict import TopicPrediction
from agent.services.topic_application import get_topic_application_state
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.approval import ApprovalResult

# 本文件验证话题申请 / 导航的真实交互：关闭 conftest 的旧测试自动批准兜底。
pytestmark = pytest.mark.topic_application

ANSWER_MARKER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 基础设施：真实 AppContext + 临时 SQLite + fake 模型 / 审批 / 事件屏障
# ---------------------------------------------------------------------------


def _app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r15_a_r1.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _anchor_topic(app: AppContext) -> str | None:
    active = AnchorService(app.conn).get_active()
    return active.topic_id if active is not None else None


def _seed_history(app: AppContext, topic_id: str) -> tuple[str, str, str]:
    """话题 A 的固定布局：两段已封存的历史片段 + 当前开放片段。

    先建历史再建开放片段：同一个话题在任一时刻只允许一个开放片段
    （idx_fragments_one_open_per_topic），所以历史必须先封存。
    """
    hist1 = app.fragments.create_child(topic_id, relation_type="normal")
    app.fragments.seal(hist1, reason="r15_a_test_history")
    hist2 = app.fragments.create_child(topic_id, relation_type="normal")
    app.fragments.seal(hist2, reason="r15_a_test_history")
    open_fragment = app.fragments.get_or_create_open(topic_id)
    return open_fragment.id, hist1, hist2


def _install_adapter(app: AppContext, monkeypatch, script) -> "ScriptedAdapter":
    adapter = ScriptedAdapter(script)

    async def _fake_build():
        return adapter

    monkeypatch.setattr(app, "build_adapter", _fake_build)
    return adapter


def _force_prediction(app: AppContext, monkeypatch, topic_id: str) -> None:
    async def _fake_predict(turn_ctx, message, topic):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic_id, backend_used="rules")

    monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", _fake_predict)


class ScriptedAdapter:
    """非流式假 adapter：按脚本返回（正文, 工具调用）。"""

    mode = "native"
    model = "fake-r15-a-r1"
    supports_stream = False

    def __init__(self, script: list[tuple[str | None, list[ToolCall] | None]]) -> None:
        self.script = list(script)
        self.calls = 0
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        # 每次请求都留一份快照：后续循环会继续往 messages 里追加。
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.script:
            content, calls = self.script.pop(0)
        else:
            content, calls = (ANSWER_MARKER + "\n收尾回答", None)
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )


class _ApprovalRecorder:
    """可编程审批通道：记录弹窗；默认批准（用来区分「有没有弹申请」）。"""

    def __init__(self, decisions: list[dict] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.requests: list[dict] = []
        self.executions: list[dict] = []

    async def request(self, kind: str, payload: dict) -> ApprovalResult:
        self.requests.append({"kind": kind, "payload": dict(payload or {})})
        item = self.decisions.pop(0) if self.decisions else {"decision": "approved"}
        return ApprovalResult(
            str(item.get("approval_id") or "appr_fake_r15_a"),
            str(item.get("decision") or "approved"),
            edits=item.get("edits"),
        )

    async def record_legacy_execution(
        self, approval_id, *, ok, error=None, summary=None
    ) -> dict:  # noqa: ANN001
        self.executions.append({"approval_id": approval_id, "ok": bool(ok), "error": error})
        return {"phase": "executed" if ok else "failed"}


def _install_approvals(
    app: AppContext, monkeypatch, decisions: list[dict] | None = None
) -> _ApprovalRecorder:
    recorder = _ApprovalRecorder(decisions)
    monkeypatch.setattr(app.approvals, "request", recorder.request)
    monkeypatch.setattr(
        app.approvals, "record_legacy_execution", recorder.record_legacy_execution
    )
    return recorder


class _BuildContextBarrier:
    """挂在真实 build_context 上：入口暂停（事件），释放后继续真实装配。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.order: list[str] = []

    def install(self, monkeypatch, orchestrator) -> None:  # noqa: ANN001
        original = orchestrator.build_context

        async def _paused(ctx, adapter):  # noqa: ANN001
            self.order.append("build_context_enter")
            self.entered.set()
            await self.release.wait()
            result = await original(ctx, adapter)
            self.order.append("build_context_exit")
            return result

        monkeypatch.setattr(orchestrator, "build_context", _paused)


def _install_snapshot_spy(orchestrator, monkeypatch, order, captured) -> None:  # noqa: ANN001
    """记录 _snapshot_decision_context 的调用时刻与写入字段（快照真实值）。"""
    original = orchestrator._snapshot_decision_context

    def _spy(ctx):  # noqa: ANN001
        original(ctx)
        order.append("snapshot")
        captured["ctx"] = ctx
        captured["snapshot_fields"] = {
            "turn_id": getattr(ctx, "decision_turn_id", None),
            "user_message_id": getattr(ctx, "decision_user_message_id", None),
            "topic_id": getattr(ctx, "decision_topic_id", None),
            "fragment_id": getattr(ctx, "decision_fragment_id", None),
            "intent_id": getattr(ctx, "decision_intent_id", None),
            "intent_version": getattr(ctx, "decision_intent_version", None),
            "nav_version": getattr(ctx, "decision_nav_version", None),
            "queued": bool(getattr(ctx, "decision_queued", False)),
        }

    monkeypatch.setattr(orchestrator, "_snapshot_decision_context", _spy)


def _install_verify_spy(orchestrator, monkeypatch, order, captured) -> None:  # noqa: ANN001
    original = getattr(orchestrator, "_verify_decision_basis", None)
    if not callable(original):
        return

    def _spy(ctx):  # noqa: ANN001
        state = original(ctx)
        order.append("verify")
        captured["basis_state_after_verify"] = getattr(ctx, "decision_basis_state", None)
        return state

    monkeypatch.setattr(orchestrator, "_verify_decision_basis", _spy)


async def _run_paused_turn(app, barrier, *, message, topic_id, action):  # noqa: ANN001
    """跑真实一轮：装配入口暂停 → 执行 action（真实导航）→ 释放 → 等本轮结束。"""
    task = asyncio.create_task(app.run_turn(message, topic_id=topic_id))
    released = False
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=20)
        try:
            action()
        finally:
            barrier.release.set()
            released = True
        return await asyncio.wait_for(task, timeout=60)
    finally:
        if not released:
            barrier.release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def _turn_messages(app: AppContext, turn_id: str) -> list:
    return app.conn.execute(
        "SELECT role, content, fragment_id FROM messages WHERE turn_id = ? "
        "ORDER BY created_at",
        (turn_id,),
    ).fetchall()


def _trace_writes(app: AppContext, turn_id: str) -> dict:
    record = app.trace_store.get(turn_id) or {}
    return dict(record.get("writes") or {})


def _last_tool_feedback(adapter: ScriptedAdapter) -> str:
    assert adapter.requests, "模型没有被调用"
    contents = [
        str(getattr(message, "content", "") or "")
        for message in adapter.requests[-1]["messages"]
        if getattr(message, "role", "") == "tool"
    ]
    assert contents, "模型收到的消息里没有工具结果（旧操作没有经过真实工具管线）"
    return contents[-1]


# ---------------------------------------------------------------------------
# 1) 装配中改选历史（主场景）：旧依据的工具调用不得覆盖用户新选择
# ---------------------------------------------------------------------------


async def test_mid_assembly_history_reselection_blocks_old_switch(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    topic_b = _topic(app, "旧依据目标话题")
    app.fragments.get_or_create_open(topic_b)
    open_fragment, hist1, _hist2 = _seed_history(app, topic_a)
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)

    adapter = _install_adapter(
        app,
        monkeypatch,
        [
            (
                None,
                [
                    ToolCall(
                        id="c1",
                        name="switch_topic",
                        arguments={"topic_id": topic_b, "intent": "user_request"},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n在装配前依据的原话题里的正式回答", None),
        ],
    )
    _force_prediction(app, monkeypatch, topic_a)
    approvals = _install_approvals(app, monkeypatch)
    barrier = _BuildContextBarrier()
    barrier.install(monkeypatch, app.turn_orchestrator)
    captured: dict = {}
    _install_snapshot_spy(app.turn_orchestrator, monkeypatch, barrier.order, captured)

    def _user_reselects_history() -> None:
        registration = app.navigation.register_continuation(topic_a, hist1)
        assert registration["changed"] is True
        assert registration["nav_version"] == version_before + 1

    result = await _run_paused_turn(
        app,
        barrier,
        message="先切到另一个话题再回答",
        topic_id=topic_a,
        action=_user_reselects_history,
    )

    ctx = captured["ctx"]
    assert result["ok"] is True
    assert result["turn"]["final_content"] == "在装配前依据的原话题里的正式回答"

    # 旧操作未执行：目标 Anchor 没有被覆盖，仍停在用户所在的原话题。
    assert _anchor_topic(app) == topic_a, "旧依据下的 switch_topic 覆盖了用户的新选择"
    # 用户新选择仍在（改选的历史片段仍是待落实的接续选择）。
    pending = app.bindings.peek_intent()
    assert pending is not None
    assert pending.source_fragment_id == hist1
    assert pending.state == "registered"
    # 导航版本未被旧操作推进（只被用户自己的改选推进一次）。
    user_version = version_before + 1
    assert navigation_version.current(app.conn) == user_version
    # 本轮正式回答仍按 begin 的原绑定归属保存。
    binding = app.bindings.binding_for(ctx.turn_id)
    assert binding.topic_id == topic_a
    assert binding.fragment_id == open_fragment
    rows = _turn_messages(app, ctx.turn_id)
    user_rows = [row for row in rows if row["role"] == "user"]
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert len(user_rows) == 1 and len(assistant_rows) == 1
    assert user_rows[0]["fragment_id"] == open_fragment
    assert assistant_rows[0]["fragment_id"] == open_fragment
    assert assistant_rows[0]["content"] == "在装配前依据的原话题里的正式回答"
    # 快照是装配前的旧依据；装配后复核为 superseded（只记录事实）。
    assert getattr(ctx, "decision_nav_version", None) == version_before
    assert getattr(ctx, "decision_basis_state", None) == "superseded"
    # 失效 ≠ 拒绝：不写拒绝证据，也不弹申请。
    assert getattr(ctx, "topic_refusal_scope", None) is None
    assert get_topic_application_state(app).refused_attempts == []
    assert approvals.requests == []
    # 旧工具调用确实经过既有写入前 CAS 被判失效（模型收到的工具反馈如实说明）。
    assert "没有执行" in _last_tool_feedback(adapter)
    writes = _trace_writes(app, ctx.turn_id)
    assert "decision_basis_superseded" in writes
    assert any("state=superseded" in str(item) for item in writes["decision_basis_superseded"])


# ---------------------------------------------------------------------------
# 2) 装配期间切走再返回：版本推进 → 旧操作 stale，回答仍按原归属完成
# ---------------------------------------------------------------------------


async def test_mid_assembly_leave_and_return_keeps_user_position(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    topic_b = _topic(app, "用户临时切走的话题")
    topic_target = _topic(app, "旧依据目标话题")
    app.fragments.get_or_create_open(topic_b)
    app.fragments.get_or_create_open(topic_target)
    open_fragment, _hist1, _hist2 = _seed_history(app, topic_a)
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)

    adapter = _install_adapter(
        app,
        monkeypatch,
        [
            (
                None,
                [
                    ToolCall(
                        id="c1",
                        name="switch_topic",
                        arguments={"topic_id": topic_target, "intent": "user_request"},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n回到原话题后的正式回答", None),
        ],
    )
    _force_prediction(app, monkeypatch, topic_a)
    _install_approvals(app, monkeypatch)
    barrier = _BuildContextBarrier()
    barrier.install(monkeypatch, app.turn_orchestrator)
    captured: dict = {}
    _install_snapshot_spy(app.turn_orchestrator, monkeypatch, barrier.order, captured)

    def _user_leaves_and_returns() -> None:
        app.navigation.enter_topic(topic_b)
        app.navigation.enter_topic(topic_a)
        assert navigation_version.current(app.conn) == version_before + 2

    result = await _run_paused_turn(
        app,
        barrier,
        message="先切到另一个话题再回答",
        topic_id=topic_a,
        action=_user_leaves_and_returns,
    )

    ctx = captured["ctx"]
    assert result["ok"] is True
    assert result["turn"]["final_content"] == "回到原话题后的正式回答"
    # 用户最终位置（离开又返回）没有被旧操作覆盖。
    assert _anchor_topic(app) == topic_a
    assert _anchor_topic(app) != topic_target
    user_version = version_before + 2
    assert navigation_version.current(app.conn) == user_version
    # 普通轮没有接续选择：只保证没有凭空冒出一个。
    assert app.bindings.peek_intent() is None
    # 回答按 begin 的原绑定归属保存。
    binding = app.bindings.binding_for(ctx.turn_id)
    assert binding.topic_id == topic_a
    assert binding.fragment_id == open_fragment
    rows = _turn_messages(app, ctx.turn_id)
    assert [row["fragment_id"] for row in rows if row["role"] == "user"] == [open_fragment]
    assert [row["fragment_id"] for row in rows if row["role"] == "assistant"] == [open_fragment]
    # 快照：旧依据 + superseded；旧工具反馈说明没有执行。
    assert getattr(ctx, "decision_nav_version", None) == version_before
    assert getattr(ctx, "decision_basis_state", None) == "superseded"
    assert "没有执行" in _last_tool_feedback(adapter)


# ---------------------------------------------------------------------------
# 3) 装配期间待接续位置改变：register_continuation 改选另一历史片段 → stale
# ---------------------------------------------------------------------------


async def test_mid_assembly_continuation_reselection_is_stale(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    topic_b = _topic(app, "旧依据目标话题")
    app.fragments.get_or_create_open(topic_b)
    _open_fragment, hist1, hist2 = _seed_history(app, topic_a)
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)
    first_registration = app.navigation.register_continuation(topic_a, hist1)
    assert first_registration["changed"] is True

    adapter = _install_adapter(
        app,
        monkeypatch,
        [
            (
                None,
                [
                    ToolCall(
                        id="c1",
                        name="switch_topic",
                        arguments={"topic_id": topic_b, "intent": "user_request"},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n按原接续归属回答", None),
        ],
    )
    _force_prediction(app, monkeypatch, topic_a)
    _install_approvals(app, monkeypatch)
    barrier = _BuildContextBarrier()
    barrier.install(monkeypatch, app.turn_orchestrator)
    captured: dict = {}
    _install_snapshot_spy(app.turn_orchestrator, monkeypatch, barrier.order, captured)

    def _user_moves_continuation() -> None:
        second = app.navigation.register_continuation(topic_a, hist2)
        assert second["changed"] is True

    result = await _run_paused_turn(
        app,
        barrier,
        message="切到另一个话题并回答",
        topic_id=topic_a,
        action=_user_moves_continuation,
    )

    ctx = captured["ctx"]
    assert result["ok"] is True
    assert result["turn"]["final_content"] == "按原接续归属回答"
    # begin 已按第一条接续意图落实了位置（新建片段），用户随后改选另一段历史。
    binding = app.bindings.binding_for(ctx.turn_id)
    applied_fragment = binding.fragment_id
    assert binding.topic_id == topic_a
    active = AnchorService(app.conn).get_active()
    assert active is not None and active.topic_id == topic_a
    assert active.fragment_id == applied_fragment
    # 用户新选择仍在（改选后的接续意图，未被旧操作消费）。
    pending = app.bindings.peek_intent()
    assert pending is not None and pending.source_fragment_id == hist2
    # 版本分三次真实推进：装配前登记接续（+1）、begin 落实接续（+1）、
    # 装配期间改选接续（+1）；旧操作没有推进。
    user_version = version_before + 3
    assert navigation_version.current(app.conn) == user_version
    # 回答落在 begin 落实的原绑定片段里。
    rows = _turn_messages(app, ctx.turn_id)
    assert [row["fragment_id"] for row in rows if row["role"] == "user"] == [applied_fragment]
    assert [row["fragment_id"] for row in rows if row["role"] == "assistant"] == [applied_fragment]
    # 快照：装配前（begin 落实接续之后）的版本与意图；复核 superseded。
    assert getattr(ctx, "decision_nav_version", None) == version_before + 2
    assert getattr(ctx, "decision_intent_id", None) == first_registration["intent_id"]
    assert getattr(ctx, "decision_intent_version", None) == first_registration["intent_version"]
    assert getattr(ctx, "decision_basis_state", None) == "superseded"
    assert "没有执行" in _last_tool_feedback(adapter)


# ---------------------------------------------------------------------------
# 4) 正常无导航变化对照：操作正常执行，快照因果更新
# ---------------------------------------------------------------------------


async def test_no_navigation_change_control_executes_and_updates_causally(
    tmp_path, monkeypatch
):
    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    topic_b = _topic(app, "目标话题")
    _seed_history(app, topic_a)
    target_fragment = app.fragments.get_or_create_open(topic_b).id
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)

    adapter = _install_adapter(
        app,
        monkeypatch,
        [
            (
                None,
                [
                    ToolCall(
                        id="c1",
                        name="switch_topic",
                        # 缺省 intent = suggestion：走真实申请（对照组验证正常路径未被破坏）。
                        arguments={"topic_id": topic_b},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n目标话题里的正式回答", None),
        ],
    )
    _force_prediction(app, monkeypatch, topic_a)
    approvals = _install_approvals(app, monkeypatch)
    barrier = _BuildContextBarrier()
    barrier.install(monkeypatch, app.turn_orchestrator)
    captured: dict = {}
    order = barrier.order
    _install_snapshot_spy(app.turn_orchestrator, monkeypatch, order, captured)
    _install_verify_spy(app.turn_orchestrator, monkeypatch, order, captured)

    result = await _run_paused_turn(
        app,
        barrier,
        message="换到目标话题再回答",
        topic_id=topic_a,
        action=lambda: None,
    )

    ctx = captured["ctx"]
    assert result["ok"] is True
    assert result["turn"]["final_content"] == "目标话题里的正式回答"
    # 正常无导航变化：申请照常弹出、批准后真实执行。
    assert len(approvals.requests) == 1
    assert approvals.requests[0]["payload"]["base_nav_version"] == version_before
    assert _anchor_topic(app) == topic_b
    assert navigation_version.current(app.conn) == version_before + 1
    # 快照因果更新到同轮真实导航结果（causal），不是装配后重读当前值。
    assert captured.get("basis_state_after_verify") == "fresh"
    assert getattr(ctx, "decision_basis_state", None) == "causal"
    assert getattr(ctx, "decision_nav_version", None) == version_before + 1
    assert getattr(ctx, "decision_topic_id", None) == topic_b
    # 回答写进本轮真实导航后的目标话题。
    binding = app.bindings.binding_for(ctx.turn_id)
    assert binding.topic_id == topic_b
    rows = _turn_messages(app, ctx.turn_id)
    assistant_rows = [row for row in rows if row["role"] == "assistant"]
    assert len(assistant_rows) == 1
    assert assistant_rows[0]["fragment_id"] == target_fragment
    # 顺序：快照 → 装配 → 复核。
    assert order.index("snapshot") < order.index("build_context_enter")
    assert order.index("build_context_exit") < order.index("verify")


# ---------------------------------------------------------------------------
# 5) 顺序断言与快照字段真实写入
# ---------------------------------------------------------------------------


async def test_snapshot_precedes_assembly_and_records_real_fields(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    _open_fragment, hist1, _hist2 = _seed_history(app, topic_a)
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)
    registration = app.navigation.register_continuation(topic_a, hist1)

    adapter = _install_adapter(
        app,
        monkeypatch,
        [(ANSWER_MARKER + "\n无导航变化的普通回答", None)],
    )
    _force_prediction(app, monkeypatch, topic_a)
    _install_approvals(app, monkeypatch)
    barrier = _BuildContextBarrier()
    barrier.install(monkeypatch, app.turn_orchestrator)
    captured: dict = {}
    order = barrier.order
    _install_snapshot_spy(app.turn_orchestrator, monkeypatch, order, captured)
    _install_verify_spy(app.turn_orchestrator, monkeypatch, order, captured)

    result = await _run_paused_turn(
        app,
        barrier,
        message="继续原来的问题",
        topic_id=topic_a,
        action=lambda: None,
    )

    ctx = captured["ctx"]
    assert result["ok"] is True
    assert result["turn"]["final_content"] == "无导航变化的普通回答"
    # 1) 顺序：快照写入发生在 build_context 入口之前、模型调用之前。
    assert "snapshot" in order and "build_context_enter" in order
    assert order.index("snapshot") < order.index("build_context_enter")
    assert order.index("build_context_exit") < order.index("verify")
    # 2) 快照位置字段：begin 落实接续后的真实绑定（版本 = 登记 +1、落实 +1）。
    snapshot = captured["snapshot_fields"]
    assert snapshot["turn_id"] == ctx.turn_id
    assert snapshot["topic_id"] == topic_a
    assert snapshot["fragment_id"] == ctx.bound_fragment_id
    assert snapshot["nav_version"] == version_before + 2
    assert snapshot["intent_id"] == registration["intent_id"]
    assert snapshot["intent_version"] == registration["intent_version"]
    assert snapshot["queued"] is False
    # 装配前用户消息尚未落库：快照如实 None；装配后补记身份事实（不改位置）。
    assert snapshot["user_message_id"] is None
    assert ctx.decision_user_message_id == ctx.user_message_id
    assert ctx.decision_user_message_id is not None
    # 3) 位置快照没有被装配完成后的当前值改写。
    assert ctx.decision_nav_version == version_before + 2
    assert ctx.decision_topic_id == topic_a
    assert ctx.decision_fragment_id == ctx.bound_fragment_id
    assert ctx.decision_basis_state == "fresh"
    # 4) trace 记录。
    writes = _trace_writes(app, ctx.turn_id)
    assert "decision_snapshot" in writes
    assert "decision_snapshot_user_message" in writes
    assert "decision_basis_verified" in writes
    assert any("state=fresh" in str(item) for item in writes["decision_basis_verified"])
    assert warnings_free(app, ctx.turn_id)


# ---------------------------------------------------------------------------
# 6) superseded 后不弹申请（建议路径）：失效 ≠ 拒绝
# ---------------------------------------------------------------------------


async def test_superseded_suggestion_does_not_open_application(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    topic_b = _topic(app, "旧依据目标话题")
    app.fragments.get_or_create_open(topic_b)
    open_fragment, hist1, _hist2 = _seed_history(app, topic_a)
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)

    adapter = _install_adapter(
        app,
        monkeypatch,
        [
            (
                None,
                [
                    ToolCall(
                        id="c1",
                        name="switch_topic",
                        # 缺省 intent = suggestion：旧依据下的建议申请
                        arguments={"topic_id": topic_b},
                    )
                ],
            ),
            (ANSWER_MARKER + "\n直接回答原问题", None),
        ],
    )
    _force_prediction(app, monkeypatch, topic_a)
    approvals = _install_approvals(app, monkeypatch, [{"decision": "approved"}])
    barrier = _BuildContextBarrier()
    barrier.install(monkeypatch, app.turn_orchestrator)
    captured: dict = {}
    _install_snapshot_spy(app.turn_orchestrator, monkeypatch, barrier.order, captured)

    def _user_reselects_history() -> None:
        registration = app.navigation.register_continuation(topic_a, hist1)
        assert registration["changed"] is True

    result = await _run_paused_turn(
        app,
        barrier,
        message="先换到另一个话题再回答",
        topic_id=topic_a,
        action=_user_reselects_history,
    )

    ctx = captured["ctx"]
    assert result["ok"] is True
    assert result["turn"]["final_content"] == "直接回答原问题"
    # superseded 的旧依据不再弹出申请（不弹申请），也没有执行。
    assert approvals.requests == [], "superseded 后仍弹出了基于旧依据的申请"
    assert _anchor_topic(app) == topic_a
    assert navigation_version.current(app.conn) == version_before + 1
    pending = app.bindings.peek_intent()
    assert pending is not None and pending.source_fragment_id == hist1
    binding = app.bindings.binding_for(ctx.turn_id)
    assert binding.topic_id == topic_a and binding.fragment_id == open_fragment
    # 失效不写拒绝证据（拒绝证据只来自真实拒绝）。
    assert getattr(ctx, "topic_refusal_scope", None) is None
    assert get_topic_application_state(app).refused_attempts == []
    assert getattr(ctx, "decision_basis_state", None) == "superseded"
    feedback = _last_tool_feedback(adapter)
    assert "失效" in feedback or "没有执行" in feedback


# ---------------------------------------------------------------------------
# 7) 复核三态（fresh / superseded / unknown）：只复核，绝不改写快照
# ---------------------------------------------------------------------------


def test_verify_decision_basis_states_never_rewrite_snapshot(tmp_path, monkeypatch):
    from agent.trace.recorder import TurnTracer

    app = _app(tmp_path)
    topic_a = _topic(app, "当前话题")
    topic_b = _topic(app, "用户后来去的话题")
    app.navigation.enter_topic(topic_a)
    version_before = navigation_version.current(app.conn)
    orchestrator = app.turn_orchestrator
    assert callable(getattr(orchestrator, "_verify_decision_basis", None)), (
        "缺少装配后复核入口 _verify_decision_basis"
    )

    ctx = TurnContext(turn_id="turn_verify_basis", message="问题")
    app.trace_store.ensure_started(ctx.turn_id, initial_topic=topic_a)
    ctx.trace = TurnTracer(app.trace_store, ctx.turn_id)
    ctx.decision_nav_version = version_before
    ctx.decision_topic_id = topic_a
    ctx.decision_fragment_id = None

    # fresh：版本相同。
    assert orchestrator._verify_decision_basis(ctx) == "fresh"
    assert ctx.decision_basis_state == "fresh"
    frozen = (
        ctx.decision_nav_version,
        ctx.decision_topic_id,
        ctx.decision_fragment_id,
    )

    # superseded：版本已变；快照不得被更新为新版本（禁止拿装配后的当前值补齐）。
    app.navigation.enter_topic(topic_b)
    assert orchestrator._verify_decision_basis(ctx) == "superseded"
    assert ctx.decision_basis_state == "superseded"
    assert (
        ctx.decision_nav_version,
        ctx.decision_topic_id,
        ctx.decision_fragment_id,
    ) == frozen, "复核把快照位置改写成了当前值"
    writes = _trace_writes(app, ctx.turn_id)
    assert "decision_basis_superseded" in writes

    # unknown：版本读不到时不伪装成 fresh / superseded。
    def _boom(conn):  # noqa: ANN001
        raise RuntimeError("navigation_version unavailable")

    monkeypatch.setattr(navigation_version, "current", _boom)
    assert orchestrator._verify_decision_basis(ctx) == "unknown"
    assert ctx.decision_basis_state == "unknown"
    assert (
        ctx.decision_nav_version,
        ctx.decision_topic_id,
        ctx.decision_fragment_id,
    ) == frozen


def warnings_free(app: AppContext, turn_id: str) -> bool:
    """本轮没有因「因果更新缺字段」等接线问题产生 WARNING 级 trace。"""
    record = app.trace_store.get(turn_id) or {}
    codes = {
        str(item.get("code"))
        for item in (record.get("warnings") or [])
        if isinstance(item, dict)
    }
    return "topic_causal_update_missing" not in codes
