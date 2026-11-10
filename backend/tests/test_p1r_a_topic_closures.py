# -*- coding: utf-8 -*-
"""P1-03 orchestrator 侧闭包（A 角色定向测试）。

覆盖三件事（全部用 fake adapter / 真实 AppContext，不联网）：

* rebind_turn_to_topic：事务化（任一步失败整体回滚）+ 幂等（重复调用不产生
  第二条用户消息、不重复搬归属）；收尾后的轮次拒绝改归属；
* rebuild_system_after_topic_application：目标话题重新装配成功；异常 / 空结果
  **必须抛结构化失败**（B 的 TopicContextPrepareError: stage=context_prepare +
  topic_id + operation_id；turn_orchestrator 保留旧别名 TopicContextRebuildError），
  不得返回旧 system；
* prepare_topic_answer_messages：恢复入口只读装配（system + 原用户消息），
  不写库、不发事件、不新建用户消息、不复制原话题历史；失败 raise。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.services.turn_orchestrator import (
    TopicContextRebuildError,
    TurnOrchestrator,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class FakeAdapter:
    mode = "native"
    model = "fake-model"

    def mode_rules_text(self, specs):  # noqa: ANN001, ARG002
        return ""


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "closures.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


@pytest.fixture()
def orch(app: AppContext) -> TurnOrchestrator:
    return TurnOrchestrator(app)


def _topic(app: AppContext, name: str) -> str:
    return app.topics.nodes.create_topic(name).id


def _turn_facts(app: AppContext, monkeypatch):
    """建一个进行中的 turn：旧话题 + 片段 + 用户消息 + 绑定。"""
    old_topic = _topic(app, "旧话题")
    target_topic = _topic(app, "目标话题")
    fragment = app.fragments.get_or_create_open(old_topic)
    message_id, _ = app.memory.append_message(
        topic_id=old_topic,
        role="user",
        content="原来那个问题怎么处理",
        model="fake-model",
        turn_id="turn-p1r-a",
    )
    app.bindings.record_binding("turn-p1r-a", old_topic, fragment_id=fragment.id)
    ctx = SimpleNamespace(
        turn_id="turn-p1r-a",
        user_message_id=message_id,
        message="原来那个问题怎么处理",
        bound_topic=old_topic,
        bound_fragment_id=fragment.id,
        bound_intent_version=None,
        current_topic=old_topic,
        topic_phase=None,
        trace=None,
        loop=None,
        mode="normal",
    )
    monkeypatch.setattr(app, "build_adapter", _fake_build_adapter)
    return SimpleNamespace(
        old_topic=old_topic,
        target_topic=target_topic,
        fragment=fragment,
        message_id=message_id,
        ctx=ctx,
    )


async def _fake_build_adapter():
    return FakeAdapter()


def _message_fragment(app: AppContext, message_id: str) -> str | None:
    row = app.conn.execute(
        "SELECT fragment_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    return row["fragment_id"] if row is not None else None


def _count(app: AppContext, table: str) -> int:
    return int(app.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])


# -- 1. rebind：事务化 ------------------------------------------------------


async def test_rebind_moves_message_and_binding_together(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)

    applied = await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)

    assert applied == facts.target_topic
    binding = app.bindings.binding_for("turn-p1r-a")
    assert binding is not None and binding.topic_id == facts.target_topic
    assert binding.fragment_id is not None
    target_fragment = app.fragments.get(binding.fragment_id)
    assert target_fragment is not None and target_fragment.topic_id == facts.target_topic
    assert _message_fragment(app, facts.message_id) == binding.fragment_id
    assert facts.ctx.bound_topic == facts.target_topic
    assert facts.ctx.bound_fragment_id == binding.fragment_id
    assert facts.ctx.topic_application_rebound is True


async def test_rebind_rolls_back_every_write_when_a_step_fails(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)
    messages_before = _count(app, "messages")

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("move failed")

    monkeypatch.setattr(app.memory, "move_message", _boom)

    with pytest.raises(RuntimeError, match="move failed"):
        await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)

    # 事务回滚：绑定、消息位置、上下文归属都保持旧值。
    binding = app.bindings.binding_for("turn-p1r-a")
    assert binding is not None and binding.topic_id == facts.old_topic
    assert _message_fragment(app, facts.message_id) == facts.fragment.id
    assert facts.ctx.bound_topic == facts.old_topic
    assert _count(app, "messages") == messages_before


async def test_rebind_is_idempotent(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)

    first = await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)
    messages_after_first = _count(app, "messages")
    fragment_after_first = _message_fragment(app, facts.message_id)

    second = await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)

    assert first == second == facts.target_topic
    assert _count(app, "messages") == messages_after_first, "重复调用不得新增消息"
    assert _message_fragment(app, facts.message_id) == fragment_after_first
    binding = app.bindings.binding_for("turn-p1r-a")
    assert binding is not None and binding.topic_id == facts.target_topic


async def test_rebind_refuses_after_turn_write_closed(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)
    app.bindings.mark_write_closed("turn-p1r-a")

    with pytest.raises(RuntimeError, match="收尾"):
        await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)


async def test_rebind_rejects_missing_target_topic(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)

    with pytest.raises(RuntimeError, match="目标话题不存在"):
        await orch.rebind_turn_to_topic(facts.ctx, "topic-does-not-exist")

    binding = app.bindings.binding_for("turn-p1r-a")
    assert binding is not None and binding.topic_id == facts.old_topic


# -- 2. rebuild：失败必须显式抛出，不能返回旧 system ------------------------


async def test_rebuild_assembles_target_topic_context(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)
    await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)

    system = await orch.rebuild_system_after_topic_application(
        facts.ctx, FakeAdapter(), SimpleNamespace(prediction=None), facts.target_topic
    )

    assert isinstance(system, str) and system.strip()
    assert "目标话题" in system
    assert "本轮转到话题" in system


async def test_rebuild_raises_structured_failure_instead_of_old_system(
    app, orch, monkeypatch
):
    facts = _turn_facts(app, monkeypatch)
    await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("assembler exploded")

    # heavy.run 在线程里执行**同步** builder：异常会原样传回事件循环。
    monkeypatch.setattr(app.context_assembler, "build_context_blocks", _boom)

    with pytest.raises(TopicContextRebuildError) as captured:
        await orch.rebuild_system_after_topic_application(
            facts.ctx, FakeAdapter(), SimpleNamespace(prediction=None), facts.target_topic
        )

    error = captured.value
    assert error.stage == "context_prepare"
    assert error.topic_id == facts.target_topic
    assert error.turn_id == "turn-p1r-a"
    assert "assembler exploded" in error.detail
    assert error.as_dict()["stage"] == "context_prepare"
    # 失败时 ctx 上不能出现「上下文已就绪」之类的假事实。
    assert getattr(facts.ctx, "topic_phase", None) is None


async def test_rebuild_treats_empty_result_as_failure(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)
    await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)

    monkeypatch.setattr(
        app.context_assembler, "build_context_blocks", lambda *a, **k: []
    )

    with pytest.raises(TopicContextRebuildError, match="为空"):
        await orch.rebuild_system_after_topic_application(
            facts.ctx, FakeAdapter(), SimpleNamespace(prediction=None), facts.target_topic
        )


# -- 3. prepare_topic_answer_messages：恢复入口只读装配 ---------------------


async def test_prepare_topic_answer_messages_is_read_only_and_target_scoped(
    app, orch, monkeypatch
):
    facts = _turn_facts(app, monkeypatch)
    await orch.rebind_turn_to_topic(facts.ctx, facts.target_topic)
    before = {name: _count(app, name) for name in ("messages", "fragments", "turn_bindings")}
    events: list[dict] = []

    async def _capture(event) -> None:  # noqa: ANN001
        events.append({"type": str(getattr(event, "type", ""))})

    monkeypatch.setattr(app.bus, "publish", _capture)

    messages = await orch.prepare_topic_answer_messages(
        turn_id="turn-p1r-a",
        topic_id=facts.target_topic,
        user_message_id=facts.message_id,
    )

    assert isinstance(messages, list) and len(messages) == 2
    system = messages[0]
    user = messages[1]
    assert getattr(system, "role", "") == "system"
    assert getattr(user, "role", "") == "user"
    assert getattr(user, "content", "") == "原来那个问题怎么处理"
    assert "目标话题" in getattr(system, "content", "")
    assert "本轮已经转到话题" in getattr(system, "content", "")
    # 只读：不写库、不发事件、不复制出第二条用户消息。
    assert {name: _count(app, name) for name in ("messages", "fragments", "turn_bindings")} == before
    assert events == []
    assert [m for m in messages if getattr(m, "role", "") == "user"] == [user]


async def test_prepare_topic_answer_messages_raises_on_missing_target(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)

    with pytest.raises(TopicContextRebuildError) as captured:
        await orch.prepare_topic_answer_messages(
            turn_id="turn-p1r-a",
            topic_id="topic-does-not-exist",
            user_message_id=facts.message_id,
        )

    assert captured.value.stage == "context_prepare"
    assert captured.value.topic_id == "topic-does-not-exist"


async def test_prepare_topic_answer_messages_raises_on_missing_message(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)

    with pytest.raises(TopicContextRebuildError) as captured:
        await orch.prepare_topic_answer_messages(
            turn_id="turn-p1r-a",
            topic_id=facts.target_topic,
            user_message_id="msg-does-not-exist",
        )

    assert captured.value.turn_id == "turn-p1r-a"


async def test_prepare_topic_answer_messages_propagates_assembler_failure(
    app, orch, monkeypatch
):
    facts = _turn_facts(app, monkeypatch)

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise TimeoutError("context timeout")

    monkeypatch.setattr(app.context_assembler, "build_context_blocks", _boom)

    with pytest.raises(TopicContextRebuildError) as captured:
        await orch.prepare_topic_answer_messages(
            turn_id="turn-p1r-a",
            topic_id=facts.target_topic,
            user_message_id=facts.message_id,
        )

    assert "context timeout" in captured.value.detail


# -- 4. persist：没有正式回答的轮次不写空助手消息（P1-02 接缝）------------


class _Result:
    def __init__(self, *, final_content="", formal_answer=True):
        self.final_content = final_content
        self.final_answer = None
        self.verification = None
        self.iteration = None
        self.formal_answer = formal_answer
        self.final_annotation = None


def _persist_ctx(app: AppContext, facts) -> SimpleNamespace:
    ctx = SimpleNamespace(
        turn_id="turn-p1r-a",
        user_message_id=facts.message_id,
        message=facts.ctx.message,
        bound_topic=facts.old_topic,
        bound_fragment_id=facts.fragment.id,
        bound_intent_version=None,
        trace=_TraceStub(),
        mode="normal",
        status="running",
        cancelled=False,
    )
    return ctx


class _TraceStub:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def write(self, kind: str, value: str, **kwargs) -> None:  # noqa: ANN003, ARG002
        self.writes.append((kind, value))


async def test_persist_skips_assistant_message_without_formal_answer(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)
    ctx = _persist_ctx(app, facts)
    messages_before = _count(app, "messages")
    plan = SimpleNamespace(topic=facts.old_topic)

    final_topic = await orch.persist(
        ctx, FakeAdapter(), plan, _Result(final_content="过程说明", formal_answer=False)
    )

    assert final_topic == facts.old_topic
    assert _count(app, "messages") == messages_before, "不得写空/过程助手消息"
    assert ("messages", "no_formal_answer") in ctx.trace.writes
    binding = app.bindings.binding_for("turn-p1r-a")
    assert binding is not None and binding.write_state == "closed"


async def test_persist_writes_assistant_message_for_formal_answer(app, orch, monkeypatch):
    facts = _turn_facts(app, monkeypatch)
    ctx = _persist_ctx(app, facts)
    messages_before = _count(app, "messages")
    plan = SimpleNamespace(topic=facts.old_topic)

    await orch.persist(ctx, FakeAdapter(), plan, _Result(final_content="正式回答"))

    assert _count(app, "messages") == messages_before + 1
    row = app.conn.execute(
        "SELECT role, content FROM messages ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    assert row["role"] == "assistant"
    assert row["content"] == "正式回答"

