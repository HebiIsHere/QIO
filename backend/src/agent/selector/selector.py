"""Selector: 按原始检索相关程度取候选（M5）。

Startup self-check picks the tier: remote embeddings (if configured) >
ONNX quantized embeddings (if importable and model present) > BM25
(always available).

范围边界：这里**只**回答「哪些记忆和查询在检索上相关」——保留底层后端的
原始分数、来源与稳定身份，按相关度截候选池。话题 / 时效 / 关键词 / 实体等
业务奖励，以及任何重排，都属于唯一排序入口 `agent/services/ranking.py`。

历史缺陷（本次消除）：候选阶段曾给召回分叠加 anchor/entity/keyword/recency
奖励，Retriever 再按「相关性 × 时效 × 话题亲和」加权一次 —— 同一批信号算两遍，
并且「原始相关性」其实是已经被奖励修改过的分数。
"""

from __future__ import annotations

import logging

from agent.selector.base import IndexedDoc, MemoryCandidate, RecallBackend
from agent.selector.bm25 import BM25Backend
from agent.selector.tokenize import tokenize

logger = logging.getLogger(__name__)


class Selector:
    def __init__(
        self,
        recall: RecallBackend | None = None,
        fallback_recall: RecallBackend | None = None,
    ) -> None:
        self.recall = recall or BM25Backend()
        self.fallback_recall = fallback_recall
        self._docs: list[IndexedDoc] = []
        self._titles: dict[str, str] = {}
        self._token_estimates: dict[str, int] = {}
        self._created_at: dict[str, str | None] = {}
        self._texts: dict[str, str] = {}

    # -- indexing ---------------------------------------------------------

    def load(
        self,
        docs: list[IndexedDoc],
        titles: dict[str, str] | None = None,
        token_estimates: dict[str, int] | None = None,
    ) -> None:
        """Feed documents from the memory index; rebuilds the recall index."""
        self._docs = list(docs)
        self._titles = titles or {}
        self._token_estimates = token_estimates or {}
        self._created_at = {d.doc_id: d.created_at for d in self._docs}
        self._texts = {d.doc_id: d.text for d in self._docs}
        if self.recall.available():
            self.recall.index(self._docs)
            logger.info(
                "selector: recall backend '%s' indexed %d docs",
                self.recall.name,
                len(self._docs),
            )
        elif self.fallback_recall is not None and self.fallback_recall.available():
            self.fallback_recall.index(self._docs)
            logger.info(
                "selector: recall '%s' unavailable; fallback '%s' indexed %d docs",
                self.recall.name,
                self.fallback_recall.name,
                len(self._docs),
            )
        else:
            logger.warning(
                "selector: recall backend '%s' unavailable; rule layer only",
                self.recall.name,
            )

    # -- 增量更新 ---------------------------------------------------------
    #
    # 每关闭 / 新增一个 Fragment 都全量重建索引，会让单次新增的成本随历史条数
    # 线性增长。这里只处理变化的那一条；不支持增量的后端由 `_apply` 回退到
    # 全量重建 —— 正确性永远优先，性能是后端能力决定的。

    def upsert(
        self, doc: IndexedDoc, *, title: str = "", token_estimate: int = 0
    ) -> None:
        replaced = False
        for i, existing in enumerate(self._docs):
            if existing.doc_id == doc.doc_id:
                self._docs[i] = doc
                replaced = True
                break
        if not replaced:
            self._docs.append(doc)
        self._titles[doc.doc_id] = title
        self._token_estimates[doc.doc_id] = token_estimate
        self._created_at[doc.doc_id] = doc.created_at
        self._texts[doc.doc_id] = doc.text
        self._apply(lambda backend: backend.upsert(doc))

    def remove(self, doc_id: str) -> None:
        self._docs = [d for d in self._docs if d.doc_id != doc_id]
        self._titles.pop(doc_id, None)
        self._token_estimates.pop(doc_id, None)
        self._created_at.pop(doc_id, None)
        self._texts.pop(doc_id, None)
        self._apply(lambda backend: backend.remove(doc_id))

    def _active_recall(self) -> RecallBackend | None:
        """当前真正生效的召回后端（与 `select` 的选择逻辑保持一致）。"""
        recall = self.recall
        if (
            not recall.available()
            and self.fallback_recall is not None
            and self.fallback_recall.available()
        ):
            recall = self.fallback_recall
        return recall if recall.available() else None

    def _apply(self, op) -> None:
        backend = self._active_recall()
        if backend is None:
            return
        if getattr(backend, "supports_incremental", False):
            op(backend)
            return
        # 后端不支持增量：保持正确性，退化为全量重建
        backend.index(self._docs)

    def text_of(self, doc_id: str) -> str | None:
        return self._texts.get(doc_id)

    def created_at(self, doc_id: str) -> str | None:
        return self._created_at.get(doc_id)

    @property
    def backend_name(self) -> str:
        return self.recall.name if self.recall.available() else "rules-only"

    # -- selection --------------------------------------------------------

    def select(
        self,
        query: str,
        *,
        candidate_pool: int,
    ) -> list[MemoryCandidate]:
        """按原始检索相关程度取候选池。

        `candidate_pool` 是候选池大小（不是最终返回条数，也不是底层召回请求数 ——
        底层只被请求这么多条，没有隐藏乘数）。分数就是底层召回分；分数相同时
        按稳定身份 `doc_id` 确定顺序，保证同一份索引、同一查询逐位可复现。
        """
        pool = int(candidate_pool)
        if pool <= 0:
            return []
        scored: dict[str, list] = {}

        recall = self._active_recall()
        if recall is not None:
            for hit in recall.search(query, top_k=pool):
                entry = scored.setdefault(hit.doc_id, [0.0, set()])
                entry[0] += hit.score
                entry[1].add(hit.source)
        else:
            # 没有可用召回后端：用「查询词项与记忆关键词的重合度」当相关分。
            # 这是退化后的检索相关度（词面命中），不是业务奖励。
            query_tokens = set(tokenize(query))
            if query_tokens:
                for doc in self._docs:
                    overlap = query_tokens & set(doc.keywords)
                    if overlap:
                        scored[doc.doc_id] = [
                            len(overlap) / len(query_tokens),
                            {"lexical"},
                        ]

        candidates: list[MemoryCandidate] = []
        for doc in self._docs:
            if doc.doc_id not in scored:
                continue
            relevance, source_set = scored[doc.doc_id]
            candidates.append(
                MemoryCandidate(
                    doc_id=doc.doc_id,
                    relevance=relevance,
                    sources=tuple(sorted(source_set)),
                    topic_id=doc.topic_id,
                    title=self._titles.get(doc.doc_id),
                    token_estimate=self._token_estimates.get(doc.doc_id, 0),
                    created_at=doc.created_at,
                    keywords=tuple(doc.keywords),
                    entity_ids=tuple(doc.entity_ids),
                )
            )

        candidates.sort(key=lambda c: (-c.relevance, c.doc_id))
        return candidates[:pool]
