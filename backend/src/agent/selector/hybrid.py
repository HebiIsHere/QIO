"""Hybrid recall: reciprocal-rank fusion of several recall backends.

为什么用 RRF 而不是分数加权：BM25 的分数是无界的词频量，embedding 是余弦，
两者不能直接相加（相加等于把「词频高的文档」当成「语义近」）。RRF 只看名次，
对量纲无关，也不需要额外标定。

回退与可用性：任何一个后端不可用（例如没有 ONNX 模型）时只跳过它；
全部不可用时 available() 为 False，Selector 会走自己的兜底路径。
"""

from __future__ import annotations

from agent.selector.base import IndexedDoc, RecallBackend, ScoredDoc


class HybridBackend(RecallBackend):
    name = "hybrid"
    supports_incremental = True

    def __init__(self, backends: list[RecallBackend], *, rrf_k: int = 60) -> None:
        self.backends = [b for b in backends if b is not None]
        self.rrf_k = max(1, int(rrf_k))

    def available(self) -> bool:
        return any(b.available() for b in self.backends)

    def _active(self) -> list[RecallBackend]:
        return [b for b in self.backends if b.available()]

    def index(self, docs: list[IndexedDoc]) -> None:
        for backend in self._active():
            backend.index(docs)

    def upsert(self, doc: IndexedDoc) -> None:
        for backend in self._active():
            if getattr(backend, "supports_incremental", False):
                backend.upsert(doc)
            else:
                backend.index([doc])

    def remove(self, doc_id: str) -> None:
        for backend in self._active():
            if getattr(backend, "supports_incremental", False):
                backend.remove(doc_id)

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        """RRF：每个后端各取 top_k，按 1/(k + rank) 累加。"""
        fused: dict[str, float] = {}
        sources: dict[str, set] = {}
        for backend in self._active():
            for rank, hit in enumerate(backend.search(query, top_k=top_k), start=1):
                fused[hit.doc_id] = fused.get(hit.doc_id, 0.0) + 1.0 / (self.rrf_k + rank)
                sources.setdefault(hit.doc_id, set()).add(backend.name)
        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [
            ScoredDoc(doc_id=doc_id, score=score, source="+".join(sorted(sources[doc_id])))
            for doc_id, score in ordered[:top_k]
        ]
