# -*- coding: utf-8 -*-
"""话题归属分类：延续 / 切换 / 新建 + 实体软信号。"""
from types import SimpleNamespace

from agent.services.affinity import (
    NEW_TOPIC_STRICT,
    SWITCH_THRESHOLD,
    TopicMode,
    classify,
)


def _pred(main=None, scores=None, new=False):
    return SimpleNamespace(main_topic_id=main, scores=scores or {}, is_new_topic_candidate=new)


def test_in_topic_when_main_is_current():
    d = classify("继续聊 sqlite", _pred(main="t_sql", scores={"t_sql": 0.8}), "t_sql")
    assert d.mode == TopicMode.IN_TOPIC


def test_switch_when_other_topic_high_score():
    d = classify("聊一下健身", _pred(main="t_fit", scores={"t_fit": 0.6, "t_sql": 0.4}), "t_sql")
    assert d.mode == TopicMode.SWITCH
    assert d.switch_to == "t_fit"


def test_new_when_main_score_below_switch_threshold():
    d = classify("鹅的伤口", _pred(main="t_fit", scores={"t_fit": 0.5}), "t_sql")
    assert d.mode == TopicMode.NEW_TOPIC
    assert d.closest_topic == "t_fit"
    assert d.closest_score == 0.5


def test_new_when_no_main():
    d = classify("完全不相关的新话题", _pred(scores={}), "t_sql")
    assert d.mode == TopicMode.NEW_TOPIC
    assert d.closest_topic is None


def test_entity_hints_are_soft_only():
    # 实体关联话题只是提示，不改变硬判定（仍是延续）
    d = classify("sqlite 继续", _pred(main="t_sql", scores={"t_sql": 0.8}), "t_sql",
                 entity_topics=["t_goose"])
    assert d.mode == TopicMode.IN_TOPIC
    assert d.entity_hints == ["t_goose"]
