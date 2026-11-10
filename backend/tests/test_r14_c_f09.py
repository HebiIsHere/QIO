"""R14-C / F09-record：拒绝证据记录接口 + 容量分段来源链支持。

契约 §3.7：拒绝记录保留真实对象（topic_id/name 或新名称）、动作、原因、范围
（fragment/turn/user_message），跨轮把证据注入 topic_decision 固定小节；
容量分段继承由 fragments 的 same_stage / source_fragment_id 来源链决定范围
（有界、防环、去重由读取方计算）。失效（stale）/ 超时（timeout）不写拒绝证据。

C 提供（A 消费）稳定只读接口：
* TopicDiscussionGuard.refusals_within(fragment_ids, *, topic_id=None, limit=None)
* TopicDiscussionGuard.refusal_entries(*, topic_id=None, limit=None)
* 旧属性 refused_attempts / refusals_for / refusal_scope 保持兼容。
"""

from __future__ import annotations

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.memory.fragment import FragmentManager
from agent.services.app import AppContext
from agent.services.topic_application import (
    RefusedAttempt,
    TopicDiscussionGuard,
    get_topic_application_state,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14c_f09.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    yield ctx


def _record(guard: TopicDiscussionGuard, **overrides) -> RefusedAttempt:
    values = {
        "topic_id": "topic_target",
        "topic_name": "目标话题",
        "action": "switch",
        "reason": "rejected",
        "turn_id": "turn_1",
        "user_message_id": "msg_1",
        "fragment_id": "frag_a",
    }
    values.update(overrides)
    return guard.record_refusal(**values)


# ---------------------------------------------------------------------------
# 1) 记录保留真实对象 / 动作 / 原因 / 范围 / 操作身份
# ---------------------------------------------------------------------------


def test_record_refusal_keeps_structured_scope(app: AppContext):
    guard = get_topic_application_state(app)
    attempt = guard.record_refusal(
        topic_id="topic_target",
        topic_name="项目计划2",
        action="create",
        reason="conflict_declined",
        turn_id="turn_42",
        user_message_id="msg_42",
        fragment_id="frag_42",
        operation_id="topop-42",
        param_version=2,
        conflict="conflict_declined",
        similar_topic_id="topic_similar",
    )
    data = attempt.as_dict()
    assert data["topic_id"] == "topic_target"
    assert data["topic_name"] == "项目计划2"
    assert data["action"] == "create"
    assert data["reason"] == "conflict_declined"
    assert data["turn_id"] == "turn_42"
    assert data["user_message_id"] == "msg_42"
    assert data["fragment_id"] == "frag_42"
    assert data["operation_id"] == "topop-42"
    assert data["param_version"] == 2
    assert data["conflict"] == "conflict_declined"
    assert data["similar_topic_id"] == "topic_similar"
    assert data["created_at"]

    entries = guard.refusal_entries()
    assert entries == [data]
    # 旧接口兼容（A 现有读取面不变）。
    assert guard.refused_attempts == [data]
    assert guard.refusals_for("topic_target") == [data]
    assert guard.refusal_scope("topic_target") == data


# ---------------------------------------------------------------------------
# 2) 容量分段继承范围：只返回来源链集合里的片段（不同类型不继承）
# ---------------------------------------------------------------------------


def test_refusals_within_filters_by_source_chain_fragments(app: AppContext):
    guard = TopicDiscussionGuard()
    _record(guard, fragment_id="frag_root", reason="rejected", turn_id="t1")
    _record(guard, fragment_id="frag_capacity_1", reason="duplicate", turn_id="t2")
    _record(
        guard,
        fragment_id="frag_other_discussion",
        topic_id="topic_other",
        turn_id="t3",
    )

    # A 沿 fragments 的 source_fragment_id 算出「同一段讨论」的片段集合（有界 / 防环 / 去重）：
    chain = {"frag_current", "frag_capacity_1", "frag_root"}
    selected = guard.refusals_within(chain, topic_id="topic_target")
    assert [item["fragment_id"] for item in selected] == [
        "frag_root",
        "frag_capacity_1",
    ], "容量分段的来源链继承原拒绝范围；无关讨论不继承"

    assert guard.refusals_within({"frag_other_discussion"})[0]["topic_id"] == "topic_other"
    assert guard.refusals_within(set()) == []
    assert guard.refusals_within({"frag_unknown"}) == []
    assert guard.refusals_within(chain, limit=1)[-1]["fragment_id"] == "frag_capacity_1"


def test_refusals_within_uses_real_fragment_source_chain(app: AppContext):
    """真实 fragments 链：容量分段（same_stage 来源）继承拒绝证据。"""
    topic = app.topics.nodes.create_topic("同一段讨论").id
    fragments = FragmentManager(app.conn)
    root = fragments.get_or_create_open(topic)
    app.conn.execute(
        "UPDATE fragments SET closed_at = ? WHERE id = ?",
        ("2026-11-10T00:00:01+00:00", root.id),
    )
    app.conn.commit()
    capacity_id = fragments.create_child(
        topic, source_fragment_id=root.id, relation_type="capacity", same_stage=True
    )
    other_topic = app.topics.nodes.create_topic("另一段讨论").id
    other = fragments.get_or_create_open(other_topic)

    guard = TopicDiscussionGuard()
    _record(guard, fragment_id=root.id, topic_id=topic)
    _record(guard, fragment_id=other.id, topic_id=other_topic)

    # 模拟 A 的来源链计算：当前片段 capacity → 来源 root（有界 / 防环 / 去重）。
    chain = {capacity_id} | {fid for fid, _depth in fragments.ancestors(capacity_id, max_depth=64)}
    assert root.id in chain
    selected = guard.refusals_within(chain, topic_id=topic)
    assert [item["fragment_id"] for item in selected] == [root.id]


# ---------------------------------------------------------------------------
# 3) 有界：只保留最近 N 条，旧记录丢弃（证据不无限增长）
# ---------------------------------------------------------------------------


def test_refusal_evidence_is_bounded():
    guard = TopicDiscussionGuard(max_refusals=3)
    for index in range(5):
        _record(guard, fragment_id=f"frag_{index}", topic_id=f"topic_{index}")
    attempts = guard.refused_attempts
    assert len(attempts) == 3
    assert [item["fragment_id"] for item in attempts] == ["frag_2", "frag_3", "frag_4"]
    assert guard.refusal_entries(limit=2) == attempts[-2:]


# ---------------------------------------------------------------------------
# 4) 跨轮有界：旧轮次（超过 max_turns）的证据按记录条数上限保留
# ---------------------------------------------------------------------------


def test_guard_reset_clears_evidence(app: AppContext):
    guard = get_topic_application_state(app)
    _record(guard)
    assert guard.refused_attempts
    guard.reset()
    assert guard.refused_attempts == []
    assert guard.refusal_entries() == []
    assert guard.refusals_within({"frag_a"}) == []
