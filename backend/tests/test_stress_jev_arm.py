# -*- coding: utf-8 -*-
"""Jev 臂的接线：用假客户端验证「答案 → 判定」的映射，不发真实请求。"""

from __future__ import annotations

import pytest

from agent.eval.jev_client import JevAnswer
from agent.eval.stress_corpus import (
    DedupCase,
    EntityCard,
    EntityCase,
    StressCorpus,
    Topic,
    TopicCase,
    ToolSpecLite,
)
from agent.eval.stress_jev_arm import JevArm


class _FakeClient:
    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        payload = self.answers
        if isinstance(payload, list):
            payload = payload[min(self.calls - 1, len(payload) - 1)]
        return JevAnswer(answers=payload, latency_ms=12.0)

    def summary(self):
        return {"calls": self.calls}


def _corpus() -> StressCorpus:
    return StressCorpus(
        seed=1,
        topics=[
            Topic(id="t_a", title="数据库迁移", keywords=("数据库",)),
            Topic(id="t_b", title="界面配色", keywords=("界面",)),
        ],
        entities=[EntityCard(id="e_1", name="小林", aliases=(), summary="负责后端")],
        tools=[ToolSpecLite(name="memory_search", description="检索历史记忆")],
    )


def test_recall_is_not_supported():
    arm = JevArm(_FakeClient({})).attach(_corpus())
    with pytest.raises(NotImplementedError):
        arm.recall([], k=5)


def test_topic_maps_choice_to_mode():
    client = _FakeClient(
        [
            {"topic": {"type": "choice", "choice": "t_a"}},
            {"topic": {"type": "choice", "choice": "t_b"}},
        ]
    )
    arm = JevArm(client).attach(_corpus())
    cases = [
        TopicCase(id="1", category="in_topic", message="继续", current_topic_id="t_a",
                  expected_topic_id="t_a", expected_mode="in_topic"),
        TopicCase(id="2", category="switch", message="换", current_topic_id="t_a",
                  expected_topic_id="t_b", expected_mode="switch"),
    ]
    rows, latencies = arm.topic(cases)
    assert [r["predicted"] for r in rows] == ["in_topic", "switch"]
    assert latencies == [12.0, 12.0]


def test_topic_maps_new_marker_to_new_topic():
    arm = JevArm(_FakeClient({"topic": {"type": "choice", "choice": "__new__"}})).attach(_corpus())
    case = TopicCase(id="3", category="new_topic", message="聊点别的",
                     current_topic_id="t_a", expected_topic_id=None, expected_mode="new_topic")
    rows, _ = arm.topic([case])
    assert rows[0]["predicted"] == "new_topic"
    assert rows[0]["predicted_topic"] is None


def test_entity_none_marker_becomes_null():
    arm = JevArm(_FakeClient({"card": {"type": "choice", "choice": "__none__"}})).attach(_corpus())
    rows, _ = arm.entity([EntityCase(id="1", query="谁负责后端", expected_card_id="e_1")])
    assert rows[0]["predicted"] is None


def test_dedup_thresholds_noul():
    for value, expected in ((0.9, True), (0.1, False)):
        client = _FakeClient({"duplicate": {"type": "noul", "noul": value}})
        arm = JevArm(client).attach(_corpus())
        case = DedupCase(
            id="1", candidate_name="数据库迁移", expected_duplicate=expected, duplicate_of="t_a"
        )
        rows, _ = arm.dedup([case])
        assert rows[0]["predicted"] is expected
