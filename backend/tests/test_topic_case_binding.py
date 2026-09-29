# -*- coding: utf-8 -*-
"""话题用例必须绑在当前记忆库上：悬空的 id 一律丢弃，整批悬空则退回按标题构造。

这条守卫是因为踩过坑：用例在旧库上生成、换库后 id 全失效，
结果 in_topic 这类用例在定义上就不可能判对，白跑一轮还得出错误结论。
"""

from __future__ import annotations

from agent.eval.real_run import bind_topic_cases
from agent.eval.stress_corpus import Topic, TopicCase


class _Store:
    def __init__(self, ids: list[str]) -> None:
        self.topics = [Topic(id=i, title=f"话题{i}", keywords=()) for i in ids]


def _case(case_id: str, current: str | None, expected: str | None) -> TopicCase:
    return TopicCase(
        id=case_id,
        category="in_topic",
        message="继续",
        current_topic_id=current,
        expected_topic_id=expected,
        expected_mode="in_topic",
    )


def test_keeps_cases_whose_ids_exist():
    store = _Store(["t1", "t2"])
    generated = {"topic": [_case("a", "t1", "t1"), _case("b", "t2", "t1")]}
    bound = bind_topic_cases(store, generated)
    assert [c.id for c in bound] == ["a", "b"]


def test_drops_cases_with_dangling_ids():
    store = _Store(["t1"])
    generated = {"topic": [_case("ok", "t1", "t1"), _case("stale", "t_old", "t_old")]}
    bound = bind_topic_cases(store, generated)
    assert [c.id for c in bound] == ["ok"]


def test_falls_back_to_title_cases_when_everything_is_dangling():
    store = _Store(["t1", "t2", "t3"])
    generated = {"topic": [_case("stale1", "t_old", "t_old")]}
    bound = bind_topic_cases(store, generated)
    assert len(bound) == 3
    assert all(c.current_topic_id in {"t1", "t2", "t3"} for c in bound)
    assert all(c.expected_topic_id == c.current_topic_id for c in bound)


def test_new_topic_cases_allow_null_expected():
    store = _Store(["t1"])
    generated = {"topic": [_case("new", "t1", None)]}
    assert [c.id for c in bind_topic_cases(store, generated)] == ["new"]
