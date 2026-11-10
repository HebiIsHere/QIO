# -*- coding: utf-8 -*-
"""F09：拒绝证据跟随同一讨论的容量分段（A 角色定向测试）。

只证明证据来源与接线（fake/mock provider，不联网），不做程序语义分类，
也不声称真实模型准确率。

固定住的验收点（契约 §3.7）：

* 复用 fragments 的 same_stage / source_fragment_id 来源链收集「同一段讨论」
  的片段集合：有界（64）、防环、去重；
* 容量分段（same_stage=1）继承原拒绝范围；same_stage 明确不同（0）/ 未知
  （NULL）不盲目继承；不同讨论的旧拒绝不进入本轮证据；
* 跨轮证据只读 C 的 guard 接口（refusals_within；旧替身退回 refused_attempts），
  纯读、不改状态；
* 具体冲突拒绝（conflict_declined）渲染成「用户拒绝过该对象」，且不因创建类
  记录的 topic_id 为空而丢证据；
* 真实驱动一轮时证据进入 system 的固定小节（topic_decision note）。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.credentials.store import MemoryKeyring
from agent.services import topic_decision
from agent.services.app import AppContext
from agent.services.predict import TopicPrediction
from agent.services.topic_application import get_topic_application_state
from agent.services.topic_decision import (
    build_topic_decision_state,
    collect_refusal_evidence,
    discussion_fragment_ids,
    normalize_refused_attempts,
    render_topic_decision_note,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER_MARKER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 1) 来源链收集：有界 / 防环 / 去重 / same_stage 语义
# ---------------------------------------------------------------------------


class _Fragments:
    def __init__(self, nodes: dict) -> None:
        self._nodes = nodes

    def get(self, fragment_id: str):
        return self._nodes.get(fragment_id)


def _node(source=None, same_stage=None):
    return SimpleNamespace(source_fragment_id=source, same_stage=same_stage)


def test_capacity_chain_inherits_sources_up_to_the_root():
    fragments = _Fragments(
        {
            "f1": _node(None, None),
            "f2": _node("f1", 1),
            "f3": _node("f2", 1),
        }
    )
    assert discussion_fragment_ids(fragments, "f3") == ["f3", "f2", "f1"]


def test_same_stage_false_stops_inheritance():
    fragments = _Fragments(
        {
            "a1": _node(None, None),
            "a2": _node("a1", 0),
            "a3": _node("a2", 1),
        }
    )
    assert discussion_fragment_ids(fragments, "a3") == ["a3", "a2"]
    assert discussion_fragment_ids(fragments, "a2") == ["a2"]


def test_unknown_same_stage_does_not_inherit():
    fragments = _Fragments({"b1": _node(None, None), "b2": _node("b1", None)})
    assert discussion_fragment_ids(fragments, "b2") == ["b2"]


def test_chain_is_bounded_and_cycle_safe():
    nodes = {}
    previous = None
    for index in range(100):
        fragment_id = f"c{index}"
        nodes[fragment_id] = _node(previous, 1)
        previous = fragment_id
    fragments = _Fragments(nodes)
    chain = discussion_fragment_ids(fragments, "c99")
    assert len(chain) == topic_decision.DISCUSSION_CHAIN_LIMIT == 64
    assert chain[0] == "c99" and len(set(chain)) == len(chain)

    cycle = _Fragments({"d1": _node("d2", 1), "d2": _node("d1", 1)})
    assert discussion_fragment_ids(cycle, "d1") == ["d1", "d2"]


def test_missing_service_and_empty_fragment_are_honest():
    # 片段读不到时至少如实保留「本轮绑定的那个片段事实」；没有片段 id 就空。
    assert discussion_fragment_ids(_Fragments({}), "nope") == ["nope"]
    assert discussion_fragment_ids(None, "nope") == ["nope"]
    assert discussion_fragment_ids(_Fragments({}), None) == []


# ---------------------------------------------------------------------------
# 2) 证据收集：只继承同一段讨论 + 本轮自己的记录；去重
# ---------------------------------------------------------------------------


class _Guard:
    def __init__(self, records: list[dict], *, with_reader: bool = True) -> None:
        self.records = records
        self.calls: list[list[str]] = []
        if with_reader:
            self.refusals_within = self._within

    def _within(self, fragment_ids, *, topic_id=None, limit=None):  # noqa: ANN001
        self.calls.append(list(fragment_ids))
        wanted = set(fragment_ids)
        return [item for item in self.records if item.get("fragment_id") in wanted]

    @property
    def refused_attempts(self) -> list[dict]:
        return list(self.records)


def test_collect_refusal_evidence_only_within_same_discussion():
    fragments = _Fragments({"f1": _node(None, None), "f2": _node("f1", 1)})
    guard = _Guard(
        [
            {"fragment_id": "f1", "topic_id": "t-old", "topic_name": "旧话题",
             "action": "switch", "reason": "rejected", "turn_id": "old"},
            # 与本轮记录完全相同的一条（模拟同一条记录被两个来源都看到）
            {"fragment_id": "f2", "topic_id": "t-new", "topic_name": "新方向",
             "action": "switch", "reason": "rejected", "turn_id": "cur"},
            {"fragment_id": "elsewhere", "topic_id": "t-x", "topic_name": "别处",
             "action": "create", "reason": "rejected", "turn_id": "x"},
        ]
    )
    app = SimpleNamespace(fragments=fragments, topic_application_state=guard)
    ctx = SimpleNamespace(bound_fragment_id="f2")
    same_turn = {"fragment_id": "f2", "topic_id": "t-new", "topic_name": "新方向",
                 "action": "switch", "reason": "rejected", "turn_id": "cur"}

    evidence = collect_refusal_evidence(
        app, ctx, fragment_id="f2", same_turn_scope=same_turn
    )

    names = [item.get("topic_name") for item in evidence]
    assert names == ["新方向", "旧话题"], "只继承同一条讨论来源链里的记录且去重"
    assert "别处" not in names
    assert guard.calls == [["f2", "f1"]], "来源链集合交给 guard 只读过滤"


def test_collect_refusal_evidence_falls_back_to_refused_attempts():
    fragments = _Fragments({"f1": _node(None, None), "f2": _node("f1", 1)})
    guard = _Guard(
        [
            {"fragment_id": "f1", "topic_id": "t1", "topic_name": "旧记录",
             "action": "switch", "reason": "rejected", "turn_id": "old"},
            {"fragment_id": "other", "topic_id": "t1", "topic_name": "别的片段",
             "action": "switch", "reason": "rejected", "turn_id": "x"},
        ],
        with_reader=False,
    )
    app = SimpleNamespace(fragments=fragments, topic_application_state=guard)

    evidence = collect_refusal_evidence(app, SimpleNamespace(), fragment_id="f2")

    assert [item["topic_name"] for item in evidence] == ["旧记录"]


def test_same_fragment_inherits_but_other_fragment_does_not():
    fragments = _Fragments(
        {"root": _node(None, None), "sibling": _node(None, None)}
    )
    guard = _Guard(
        [
            {"fragment_id": "root", "topic_id": "t1", "topic_name": "同一片段",
             "action": "switch", "reason": "rejected", "turn_id": "old"},
            {"fragment_id": "sibling", "topic_id": "t1", "topic_name": "另一段讨论",
             "action": "switch", "reason": "rejected", "turn_id": "old2"},
        ]
    )
    app = SimpleNamespace(fragments=fragments, topic_application_state=guard)
    evidence = collect_refusal_evidence(app, SimpleNamespace(), fragment_id="root")
    assert [item["topic_name"] for item in evidence] == ["同一片段"], (
        "当前片段自己的拒绝证据要继承；没有来源关系的别的片段不继承"
    )


# ---------------------------------------------------------------------------
# 3) 具体冲突拒绝：渲染成用户拒绝，且不因 topic_id 为空丢证据
# ---------------------------------------------------------------------------


def test_conflict_declined_renders_as_user_refusal_without_topic_id():
    entries = normalize_refused_attempts(
        [
            {
                "topic_id": "",
                "topic_name": "用户想建的新话题",
                "action": "create",
                "reason": "conflict_declined",
                "conflict": "conflict_declined",
                "fragment_id": "f1",
                "turn_id": "turn_prev",
            }
        ],
        topic_id="t-current",
    )
    assert len(entries) == 1, "创建类拒绝记录没有 topic_id 也不能丢"
    assert entries[0]["outcome"] == "user_refused"
    assert entries[0]["target"] == "用户想建的新话题"

    state = build_topic_decision_state(
        type("_App", (), {"topics": None, "fragments": None})(),
        type("_Ctx", (), {"message": "换话题", "bound_fragment_id": None})(),
        "t-current",
        TopicPrediction(main_topic_id=None, backend_used="rules"),
        refuse_scope=[
            {
                "topic_id": "",
                "topic_name": "用户想建的新话题",
                "action": "create",
                "reason": "conflict_declined",
                "fragment_id": "f1",
                "turn_id": "turn_prev",
            }
        ],
    )
    note = render_topic_decision_note(state)
    assert "用户想建的新话题" in note
    assert "用户选择在当前话题回答" in note


# ---------------------------------------------------------------------------
# 4) 真实驱动一轮：证据进入 system 固定小节（容量分段继承）
# ---------------------------------------------------------------------------


def _app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14_a_f09.db")
    apply_migrations(conn)
    app = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app.credentials._kr = MemoryKeyring()
    return app


class _RecordingAdapter:
    mode = "native"
    model = "fake-r14-a-f09"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        return Completion(message=ChatMessage(role="assistant", content=ANSWER_MARKER + "\n好的"))


async def test_capacity_segment_refusal_reaches_system_note(tmp_path, monkeypatch):
    app = _app(tmp_path)
    topic = app.topics.nodes.create_topic("讨论中的话题").id
    old_fragment = app.fragments.get_or_create_open(topic)
    # 容量分段：封存旧片段 → 下一段继承来源与 same_stage=1
    app.fragments.seal(old_fragment.id, reason="capacity")
    app.fragments.mark_continuation(topic, old_fragment.id, same_stage=True, reason="capacity")
    new_fragment = app.fragments.get_or_create_open(topic)
    assert new_fragment.source_fragment_id == old_fragment.id
    assert new_fragment.same_stage == 1

    guard = get_topic_application_state(app)
    guard.record_refusal(
        topic_id="",
        topic_name="用户想建的新话题",
        action="create",
        reason="conflict_declined",
        turn_id="turn_prev",
        fragment_id=old_fragment.id,
        conflict="conflict_declined",
    )
    # 另一个讨论里的拒绝：不能继承
    other_topic = app.topics.nodes.create_topic("别的话题").id
    other_fragment = app.fragments.get_or_create_open(other_topic)
    guard.record_refusal(
        topic_id="",
        topic_name="不该出现的旧对象",
        action="create",
        reason="rejected",
        turn_id="turn_other",
        fragment_id=other_fragment.id,
    )

    adapter = _RecordingAdapter()

    async def fake_build():
        return adapter

    monkeypatch.setattr(app, "build_adapter", fake_build)

    async def fake_predict(turn_ctx, message, topic_id):  # noqa: ANN001
        return TopicPrediction(main_topic_id=topic, backend_used="rules")

    monkeypatch.setattr(app.turn_orchestrator, "_predict_topics", fake_predict)

    result = await app.run_turn("继续这个话题", topic_id=topic)

    assert result.get("ok") is True
    system = adapter.requests[0]["messages"][0]
    assert system.role == "system"
    text = system.content or ""
    assert "用户想建的新话题" in text, "容量分段的原拒绝范围必须继承到本轮证据"
    assert "用户选择在当前话题回答" in text
    assert "不该出现的旧对象" not in text, "不同讨论的拒绝不得继承"
