"""关键词 + 向量的混合召回：用 RRF（倒数排名融合）合并两路结果。

为什么用 RRF 而不是把两路分数相加：两个打分器的分数量纲完全不同
（BM25 可以到 2.x，余弦在 0–1），直接相加等于让 BM25 主导。RRF 只看名次，
不需要跨打分器校准，是这类混合最省事也最不容易搞错的做法。
"""

from __future__ import annotations

from typing import Any, Sequence

from agent.selector.base import IndexedDoc, RecallBackend, ScoredDoc


class HybridRecall(RecallBackend):
    name = "hybrid"
    supports_incremental = False

    def __init__(
        self,
        lexical: RecallBackend,
        vector: RecallBackend,
        *,
        depth: int = 20,
        rrf_k: int = 60,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
    ) -> None:
        self.lexical = lexical
        self.vector = vector
        self.depth = depth
        self.rrf_k = rrf_k
        # 等权融合有个隐含前提：两路水平相当。当一路明显更弱时，
        # 等权会把强的那一路拉下来，所以权重必须可调。
        self.lexical_weight = lexical_weight
        self.vector_weight = vector_weight

    def available(self) -> bool:
        return self.lexical.available() or self.vector.available()

    def index(self, docs: list[IndexedDoc]) -> None:
        if self.lexical.available():
            self.lexical.index(docs)
        if self.vector.available():
            self.vector.index(docs)

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        fused: dict[str, float] = {}
        for backend, weight in (
            (self.lexical, self.lexical_weight),
            (self.vector, self.vector_weight),
        ):
            if not backend.available():
                continue
            for rank, hit in enumerate(backend.search(query, top_k=self.depth), start=1):
                fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + weight / (self.rrf_k + rank)
        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [ScoredDoc(doc_id=doc_id, score=score, source=self.name) for doc_id, score in ordered[:top_k]]
