# -*- coding: utf-8 -*-
"""跨话题召回：默认关闭、候选扩充不改排序、来源链跨不出话题（E1~E3 的护栏）。

数据：backend/evals/cross_topic/{queries_cross_topic.json,results_*.json,diagnosis.json}
结论：backend/evals/EXPERIMENTS-CROSSTOPIC.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
CROSS = EVALS / "cross_topic"
sys.path.insert(0, str(EVALS / "retrieval_ranking"))
sys.path.insert(0, str(CROSS))

# CI 上没有 ONNX 模型，这里全部用 BM25 路径（与 run_cross_topic.py --arm P0_bm25 同口径）
CI_CROSS_RECALL_AT_1 = 0.04
CI_CROSS_RECALL_AT_5 = 0.08
CI_CROSS_TOPIC_HIT_AT_1 = 0.34
CI_CROSS_TOPIC_HIT_AT_5 = 0.56
CI_ORDINARY_RECALL_AT_1 = 0.68


def _dataset():
    ordinary = json.loads((EVALS / "retrieval_ranking" / "corpus.json").read_text(encoding="utf-8"))
    cross = json.loads((CROSS / "queries_cross_topic.json").read_text(encoding="utf-8"))
    return ordinary, cross


def test_dataset_is_big_enough_and_every_case_is_explained():
    ordinary, cross = _dataset()
    queries = cross["queries"]
    assert len(queries) >= 40, f"跨话题样本只有 {len(queries)} 条，不足以支撑结论"
    doc_ids = {d["id"] for d in ordinary["docs"]}
    by_topic: dict = {}
    for doc in ordinary["docs"]:
        if doc.get("topic"):
            by_topic.setdefault(doc["topic"], set()).add(doc["id"])
    subtypes: dict = {}
    for query in queries:
        assert query["note"], f"{query['id']} 缺少 note"
        assert query["subtype"]
        subtypes[query["subtype"]] = subtypes.get(query["subtype"], 0) + 1
        assert set(query["expected"]) <= doc_ids
        target = query["target_topic"]
        assert target and set(query["expected"]) <= by_topic[target]
        assert query["topic"] != target, "跨话题查询的锚点不能就是目标话题"
        for stale_id in query["stale"]:
            assert stale_id in by_topic[target]
    for required in (
        "explicit_title",
        "keyword_only",
        "pronoun_only",
        "conclusion_review",
        "multi_topic",
        "mixed_language",
        "long_span_old",
    ):
        assert subtypes.get(required, 0) >= 4, f"子类 {required} 样本太少：{subtypes}"


def test_cross_topic_switches_default_to_off():
    """方案 A/B/C 默认全关 —— 实测都没能在普通集不退化的前提下拿到收益。"""
    from agent.services.params import CROSS_TOPIC

    assert CROSS_TOPIC.rewrite_enabled is False
    assert CROSS_TOPIC.expand_enabled is False
    assert CROSS_TOPIC.relation_enabled is False


class _Fingerprints:
    """没有指纹的话题桩（本测试不用指纹路径，只用显式 topic_hints）。"""

    class _Node:
        meta: dict = {}

    class _Nodes:
        def get_topic(self, topic_id):
            return _Fingerprints._Node()

    def __init__(self):
        self.nodes = _Fingerprints._Nodes()

    def list_with_fingerprints(self):
        return []

    def fingerprint(self, topic_id):
        raise ValueError(topic_id)


def _small_retriever(expand: bool, relation: bool = False):
    """两话题的小语料：t_b 的记忆与查询有部分词面重叠（会被 BM25 召回）。"""
    from dataclasses import replace

    from agent.selector.base import IndexedDoc
    from agent.selector.bm25 import BM25Backend
    from agent.selector.selector import Selector
    from agent.services.params import CROSS_TOPIC
    from agent.services.retrieval import Retriever

    docs = [
        IndexedDoc(doc_id="a1", text="清淡 饮食 偏好", topic_id="t_a", keywords=["清淡", "饮食"]),
        IndexedDoc(doc_id="a2", text="清淡 饮食 记录", topic_id="t_a", keywords=["清淡", "饮食"]),
        IndexedDoc(doc_id="b1", text="清淡 索引 结构", topic_id="t_b", keywords=["索引", "结构"]),
        IndexedDoc(doc_id="b2", text="索引 结构 设计", topic_id="t_b", keywords=["索引", "结构"]),
        IndexedDoc(doc_id="c1", text="完全 无关 内容", topic_id="t_c", keywords=["无关"]),
    ]
    selector = Selector(recall=BM25Backend(), fallback_recall=BM25Backend())
    selector.load(docs)
    policy = replace(CROSS_TOPIC, expand_enabled=expand, relation_enabled=relation)
    return Retriever(selector, _Fingerprints(), cross_topic=policy)


def test_topic_hints_are_ignored_while_the_switch_is_off():
    retriever = _small_retriever(expand=False)
    plain = [h.doc_id for h in retriever.search("清淡 饮食", top_k=3)]
    hinted = [
        h.doc_id
        for h in retriever.search("清淡 饮食", top_k=3, topic_hints=["t_b"])
    ]
    assert plain == hinted, "开关关闭时 topic_hints 必须完全不生效"


def test_expansion_keeps_the_same_source_of_scores():
    """扩出来的候选必须用**同一个查询**打分，不能拿另一段文本的分数混进来。

    这是实测踩过的坑：用话题指纹文本召回时，候选分数是相对指纹算的，
    放进同一个排序后普通集 R@1 从 0.847 掉到 0.347（见 EXPERIMENTS-CROSSTOPIC.md）。
    """
    plain_retriever = _small_retriever(expand=False)
    expanded_retriever = _small_retriever(expand=True)
    plain = {h.doc_id: h.score for h in plain_retriever.search("清淡 饮食", top_k=5)}
    expanded = {h.doc_id: h.score for h in expanded_retriever.search("清淡 饮食", top_k=5, topic_hints=["t_b"])}
    common = set(plain) & set(expanded)
    assert common, "两次检索至少要有共同命中"
    for doc_id in common:
        assert plain[doc_id] == pytest.approx(expanded[doc_id], abs=1e-9), (
            f"{doc_id} 的分数在两次检索里不一致 —— 说明有候选用了别的查询打分"
        )
    assert set(plain) <= set(expanded), "开关打开后是**扩充**候选，不能丢掉主召回的命中"


def test_relation_expansion_cannot_cross_topic_boundaries(tmp_path):
    """来源链在写入时就被校验为同话题，所以关系扩展跨不出话题边界。"""
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.graph.anchors import AnchorService
    from agent.memory.fragment import FragmentManager
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "rel.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    topic_a = ctx.topics.nodes.create_topic("话题A").id
    topic_b = ctx.topics.nodes.create_topic("话题B").id
    fragments = FragmentManager(conn)
    old_a = fragments.get_or_create_open(topic_a)  # 话题的第一段（懒创建，生产同路径）
    fragments.seal(old_a.id, reason="capacity")  # 同一话题同时只能有一个开放片段
    new_a = fragments.create_child(topic_a, source_fragment_id=old_a.id)
    AnchorService(conn).set_active(topic_a, new_a)

    retriever = _small_retriever(expand=True, relation=True)
    retriever.conn = conn
    topics = retriever._relation_topics(topic_a)
    assert topics == [topic_a], f"来源链只能给出锚点话题，实际 {topics}"
    # 结构性原因：跨话题来源在写入时就会被拒绝
    other = fragments.get_or_create_open(topic_b)
    with pytest.raises(ValueError):
        fragments.create_child(topic_a, source_fragment_id=other.id)


def test_ci_baseline_metrics_are_recorded_and_do_not_regress():
    """CI 上的 BM25 基线（与 evals/cross_topic/results_*.json 同口径）。"""
    import run_cross_topic

    docs, ordinary_queries, cross_queries = run_cross_topic.load()
    docs_by_id = {d["id"]: d for d in docs}
    ranked = run_cross_topic.rank("P0_bm25", docs, ordinary_queries + cross_queries)
    ordinary = run_cross_topic.measure(ordinary_queries, docs_by_id, ranked)
    cross = run_cross_topic.measure(cross_queries, docs_by_id, ranked)
    assert cross["recall@1"] >= CI_CROSS_RECALL_AT_1, cross
    assert cross["recall@5"] >= CI_CROSS_RECALL_AT_5, cross
    assert cross["topic_hit@1"] >= CI_CROSS_TOPIC_HIT_AT_1, cross
    assert cross["topic_hit@5"] >= CI_CROSS_TOPIC_HIT_AT_5, cross
    assert ordinary["recall@1"] >= CI_ORDINARY_RECALL_AT_1, ordinary
