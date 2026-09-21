"""Memory Selector 增量更新。

历史缺陷：每新增 / 关闭一个 Fragment 都会
「全表读 memory_index → 重建全部 documents → 全量重建 recall 索引」，
成本随历史条数线性增长。

硬要求：增量结果必须与全量重建**逐位一致**（检索语义完全不变），
且新增一条不能对全部历史重做工作。
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import numpy as np
import pytest

from agent.selector.base import IndexedDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector

# 规则层的时效项是「距离现在多久」的连续函数。比较两个 Selector 时必须把它钉住，
# 否则两次 select 相隔几微秒，得分就会在第 12 位小数量级上漂移（测试偶发变红）。
FIXED_NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)


def _random_docs(n: int, seed: int = 7) -> list[IndexedDoc]:
    rng = random.Random(seed)
    words = ["饮食", "清淡", "辣", "实验", "橡胶", "sqlite", "索引", "预算", "话题", "记忆", "plan", "loop"]
    docs = []
    for i in range(n):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(3, 12)))
        docs.append(
            IndexedDoc(
                doc_id=f"d{i:03d}",
                text=text,
                topic_id=f"t{i % 5}",
                entity_ids=[f"e{i % 3}"],
                keywords=rng.sample(words, 2),
                created_at=f"2026-01-{i % 28 + 1:02d}T00:00:00+00:00",
            )
        )
    return docs


def _hits(backend: BM25Backend, query: str, top_k: int = 8):
    return [(h.doc_id, round(h.score, 12)) for h in backend.search(query, top_k=top_k)]


def test_bm25_incremental_upsert_matches_full_rebuild():
    docs = _random_docs(60)
    backend = BM25Backend()
    backend.index(docs)

    added = IndexedDoc(doc_id="new1", text="橡胶 实验 记录", topic_id="t0")
    backend.upsert(added)

    rebuilt = BM25Backend()
    rebuilt.index([*docs, added])

    for query in ("橡胶 实验", "饮食 清淡", "sqlite 索引"):
        assert _hits(backend, query) == _hits(rebuilt, query)


def test_bm25_incremental_remove_matches_full_rebuild():
    docs = _random_docs(60)
    backend = BM25Backend()
    backend.index(docs)
    backend.remove(docs[3].doc_id)
    backend.remove(docs[20].doc_id)

    remaining = [d for i, d in enumerate(docs) if i not in (3, 20)]
    rebuilt = BM25Backend()
    rebuilt.index(remaining)

    for query in ("橡胶 实验", "饮食 清淡", "sqlite 索引"):
        assert _hits(backend, query) == _hits(rebuilt, query)


def test_bm25_upsert_replaces_existing_doc_in_place():
    docs = _random_docs(30)
    backend = BM25Backend()
    backend.index(docs)
    # 同 doc_id 换内容：必须替换而不是变成两条
    backend.upsert(IndexedDoc(doc_id=docs[5].doc_id, text="完全不同的内容 河马", topic_id="t9"))

    rebuilt = BM25Backend()
    replaced = [*docs[:5], IndexedDoc(doc_id=docs[5].doc_id, text="完全不同的内容 河马", topic_id="t9"), *docs[6:]]
    rebuilt.index(replaced)

    assert _hits(backend, "河马 内容") == _hits(rebuilt, "河马 内容")
    assert _hits(backend, "饮食 清淡") == _hits(rebuilt, "饮食 清淡")


def test_bm25_remove_restores_avgdl():
    docs = _random_docs(20)
    backend = BM25Backend()
    backend.index(docs)
    backend.remove(docs[0].doc_id)
    rebuilt = BM25Backend()
    rebuilt.index(docs[1:])
    assert backend._avgdl == pytest.approx(rebuilt._avgdl)
    assert dict(backend._df) == dict(rebuilt._df)


def test_upsert_does_not_reindex_every_document(monkeypatch):
    """新增一条时，只允许对新增的那条做分词，不允许重做全部历史。"""
    import agent.selector.bm25 as bm25_module

    docs = _random_docs(200)
    backend = BM25Backend()
    backend.index(docs)

    calls: list[str] = []
    real_tokenize = bm25_module.tokenize

    def spy(text: str):
        calls.append(text)
        return real_tokenize(text)

    monkeypatch.setattr(bm25_module, "tokenize", spy)
    backend.upsert(IndexedDoc(doc_id="new", text="橡胶 实验 记录", topic_id="t0"))

    assert len(calls) == 1, f"新增一条只该处理一条文本，实际 {len(calls)} 次"
    assert calls[0] == "橡胶 实验 记录"

    calls.clear()
    backend.remove(docs[0].doc_id)
    assert calls == [], "删除不应触发任何重新分词"


def test_selector_upsert_and_remove_keep_results_consistent():
    docs = _random_docs(40)
    selector = Selector()
    selector.load(
        docs,
        titles={d.doc_id: f"标题{d.doc_id}" for d in docs},
        token_estimates={d.doc_id: len(d.text) for d in docs},
    )

    added = IndexedDoc(doc_id="new1", text="橡胶 实验 记录", topic_id="t0")
    selector.upsert(added, title="新片段", token_estimate=12)
    selector.remove(docs[2].doc_id)

    final_docs = [d for d in docs if d.doc_id != docs[2].doc_id] + [added]
    rebuilt = Selector()
    rebuilt.load(
        final_docs,
        titles={**{d.doc_id: f"标题{d.doc_id}" for d in docs if d.doc_id != docs[2].doc_id}, "new1": "新片段"},
        token_estimates={**{d.doc_id: len(d.text) for d in docs if d.doc_id != docs[2].doc_id}, "new1": 12},
    )

    for query in ("橡胶 实验", "饮食 清淡", "预算 记忆"):
        a = [
            (c.doc_id, round(c.score, 12), c.title, c.token_estimate)
            for c in selector.select(query, top_k=5, now=FIXED_NOW)
        ]
        b = [
            (c.doc_id, round(c.score, 12), c.title, c.token_estimate)
            for c in rebuilt.select(query, top_k=5, now=FIXED_NOW)
        ]
        assert a == b

    # 旁路数据也要跟着走
    assert selector.text_of(docs[2].doc_id) is None
    assert selector.text_of("new1") == "橡胶 实验 记录"
    assert selector.created_at(docs[2].doc_id) is None


def test_selector_upsert_replaces_existing():
    docs = _random_docs(10)
    selector = Selector()
    selector.load(docs)
    selector.upsert(IndexedDoc(doc_id=docs[0].doc_id, text="完全不同的内容 河马"), title="改过", token_estimate=3)

    assert len(selector._docs) == 10
    assert selector.text_of(docs[0].doc_id) == "完全不同的内容 河马"
    assert selector.select("河马", top_k=3, now=FIXED_NOW)[0].doc_id == docs[0].doc_id


def test_select_can_pin_the_clock_for_reproducible_ranking():
    """规则层含时效项：允许调用方钉住「现在」，同一份索引的两次 select 才逐位一致。

    默认仍然是当前时间（行为不变）；这里只是把已经存在于 QueryContext 里的可注入时钟
    暴露到 Selector 上，让测试 / 评测能在同一时刻比较两次排序。
    """
    selector = Selector()
    selector.load(_random_docs(12))

    first = [(c.doc_id, c.score) for c in selector.select("饮食 清淡", top_k=5, now=FIXED_NOW)]
    second = [(c.doc_id, c.score) for c in selector.select("饮食 清淡", top_k=5, now=FIXED_NOW)]
    assert first == second


def test_onnx_search_reuses_matrix_instead_of_restacking(monkeypatch, db_conn):
    """向量集合没变时，重复 search 不该每次都 np.stack 重组矩阵。"""
    from agent.selector import onnx as onnx_module

    backend = onnx_module.OnnxEmbeddingBackend.__new__(onnx_module.OnnxEmbeddingBackend)
    backend.conn = db_conn
    backend.model_dir = None
    backend.max_len = 512
    backend.threads = 1
    backend.model_name = onnx_module.MODEL_NAME
    backend.dims = 4
    backend._session = object()
    backend._tokenizer = object()
    backend._vectors = {}

    def fake_embed(texts):
        out = []
        for i, t in enumerate(texts):
            vec = np.array([1.0, float(len(t) % 3), 0.5, 0.25], dtype=np.float32)
            out.append(vec / np.linalg.norm(vec))
        return np.stack(out).astype(np.float32)

    monkeypatch.setattr(backend, "embed_texts", fake_embed)
    backend.index([IndexedDoc(doc_id="a", text="aa"), IndexedDoc(doc_id="b", text="bbb")])

    stacks: list[int] = []
    real_stack = onnx_module.np.stack

    def spy_stack(arrays, *args, **kwargs):
        arrays = list(arrays)
        stacks.append(len(arrays))
        return real_stack(arrays, *args, **kwargs)

    monkeypatch.setattr(onnx_module.np, "stack", spy_stack)
    backend.search("查询一", 2)
    backend.search("查询二", 2)
    backend.search("查询三", 2)

    # 查询向量自身的 stack 每次 1 个是必需的；文档矩阵（>=2 个数组）只允许在最开始建一次。
    # 旧实现每次 search 都会重组文档矩阵 —— 那就是 [2, 2, 2]。
    rebuilds = [n for n in stacks if n > 1]
    assert len(rebuilds) <= 1, f"重复重组文档矩阵: {stacks}"

    # 向量集合变化后必须失效重建，结果不能是旧矩阵
    stacks.clear()
    backend.upsert(IndexedDoc(doc_id="c", text="cccc"))
    backend.search("查询四", 3)
    assert [n for n in stacks if n > 1] == [3], f"新增向量后没有重建矩阵: {stacks}"
