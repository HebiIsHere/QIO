# -*- coding: utf-8 -*-
"""压力语料生成器：确定性、标注自洽、离线。

这些断言守住的是「标注可信」这件事：如果生成器给出的正确答案和它自己的构造规则
不一致（例如事实更新把旧值标成正确），后面所有实验结论都会是错的。
"""

from __future__ import annotations

from agent.eval.stress_corpus import CATEGORIES, content_overlap, generate
from agent.selector.tokenize import tokenize


def test_same_seed_gives_identical_corpus():
    a = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    b = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    assert a.memories == b.memories
    assert a.recall_cases == b.recall_cases


def test_every_recall_case_points_at_existing_memory():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    known = {m.id for m in c.memories}
    for case in c.recall_cases:
        assert case.expected and set(case.expected) <= known
        assert set(case.stale) <= known


def test_fact_update_gold_is_newest_and_stale_is_older():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    updates = [x for x in c.recall_cases if x.category == "fact_update"]
    assert updates
    for case in updates:
        gold = by_id[case.expected[0]]
        assert case.stale, "事实更新用例必须带一个旧值"
        for old in case.stale:
            assert by_id[old].topic_id == gold.topic_id
            assert by_id[old].age_days > gold.age_days


def test_same_topic_distractor_shares_topic_but_is_not_gold():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    cases = [x for x in c.recall_cases if x.category == "same_topic"]
    assert cases
    for case in cases:
        gold = by_id[case.expected[0]]
        assert any(
            m.topic_id == gold.topic_id and m.id not in case.expected for m in c.memories
        )


def test_every_category_is_present_and_sizes_match_request():
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=60)
    assert len(c.memories) == 200
    assert len(c.recall_cases) == 60
    assert {x.category for x in c.recall_cases} == set(CATEGORIES)


def test_keyword_answerable_flag_matches_content_overlap():
    """这个标记必须由内容词重叠实算：它决定「模型在关键词答不对的子集上有多少增量」。"""
    c = generate(seed=7, n_memories=200, n_topics=10, n_queries=40)
    by_id = {m.id: m for m in c.memories}
    for case in c.recall_cases:
        overlap = any(content_overlap(case.query, by_id[m].text) for m in case.expected)
        assert case.keyword_answerable == overlap


def test_hard_categories_are_really_not_keyword_answerable():
    """同名但词不同的类别里，必须有一批真的「字面答不出」——否则模型没有增量空间。"""
    c = generate(seed=7, n_memories=1400, n_topics=20, n_queries=120)
    for category in ("cross_topic", "paraphrase", "recent_noise"):
        cases = [x for x in c.recall_cases if x.category == category]
        assert cases, f"{category} 一条用例都没有"
        disjoint = [x for x in cases if x.tier == "disjoint"]
        assert len(disjoint) >= len(cases) * 0.4, f"{category} 的零重合用例太少"
        assert all(not x.keyword_answerable for x in disjoint)


def test_mix_contains_both_easy_and_hard_queries():
    """两个子集都要有：只留简单题测不出上限，只留难题测不出真实可用性。"""
    c = generate(seed=7, n_memories=1400, n_topics=20, n_queries=120)
    hard = [x for x in c.recall_cases if not x.keyword_answerable]
    easy = [x for x in c.recall_cases if x.keyword_answerable]
    assert len(hard) >= len(c.recall_cases) * 0.3
    assert len(easy) >= len(c.recall_cases) * 0.3

    # 事实更新类必须两种问法都有：它同时承担「陈旧知识是否被取代」的检验
    updates = [x for x in c.recall_cases if x.category == "fact_update"]
    assert sum(not x.keyword_answerable for x in updates) >= len(updates) * 0.25
    assert sum(x.keyword_answerable for x in updates) >= len(updates) * 0.25


def test_difficulty_tiers_are_populated_and_consistent():
    """三档都要有，否则「从哪一档开始掉」就说不清；分层口径必须与重叠率一致。"""
    c = generate(seed=7, n_memories=1400, n_topics=20, n_queries=180)
    assert {x.tier for x in c.recall_cases} == {"literal", "partial", "disjoint"}
    for case in c.recall_cases:
        assert case.keyword_answerable == (case.overlap > 0)
        if case.tier == "literal":
            assert case.overlap >= 0.4
        elif case.tier == "partial":
            assert 0 < case.overlap < 0.4
        else:
            assert case.overlap == 0
