"""Selector interfaces.

The selector answers one question: which memory fragments are relevant to
the current user request. It must be fast and deterministic-first:
- rule layer: always on (anchor topic, entities, recency);
- recall layer: pluggable (BM25 / ONNX embeddings / remote API);
- rerank layer: optional, off by default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class IndexedDoc:
    doc_id: str
    text: str
    topic_id: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    created_at: str | None = None


@dataclass(frozen=True)
class ScoredDoc:
    doc_id: str
    score: float
    source: str


@dataclass(frozen=True)
class MemoryCandidate:
    """候选记忆。

    `score` 是**底层召回的相关度**（多来源相加），不是最终业务分 —— 业务奖励
    （话题亲和 / 时效 / 关键词 / 实体）只在 agent/services/retrieval.py 的统一排序
    入口里加一次，默认权重全部为 0（纯相关性）。2026-10-02 之前这里是「召回分 +
    规则奖励」并在 Selector 里截断，Retriever 再加权一次 —— 两层排序会先淘汰掉
    第二层本来能救回来的候选，且规则奖励实际上被算了两次
    （见 backend/evals/retrieval_ranking/arms_before_refactor.json）。

    `signals` 只是把规则层算出的**分项**带出来供统一排序按权重使用，不参与排序本身。
    """

    doc_id: str
    score: float
    sources: tuple[str, ...]
    topic_id: str | None = None
    title: str | None = None
    token_estimate: int = 0
    signals: dict[str, float] = field(default_factory=dict)

    def merge_score(self, extra: float, source: str) -> "MemoryCandidate":
        return MemoryCandidate(
            doc_id=self.doc_id,
            score=self.score + extra,
            sources=self.sources + (source,),
            topic_id=self.topic_id,
            title=self.title,
            token_estimate=self.token_estimate,
            signals=dict(self.signals),
        )


class RecallBackend(ABC):
    """A pluggable recall layer over the memory index."""

    name: str
    #: 是否支持**真正的增量** upsert / remove。
    #: False 时 Selector 会退化为全量重建（行为仍然正确，只是没有性能收益）。
    supports_incremental: bool = False

    @abstractmethod
    def available(self) -> bool:
        """Self-check; called at startup to pick the backend tier."""

    @abstractmethod
    def index(self, docs: list[IndexedDoc]) -> None:
        """Rebuild or update the in-memory index."""

    @abstractmethod
    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        raise NotImplementedError

    # -- 增量更新（可选能力） --------------------------------------------
    #
    # 历史缺陷：每新增一条 memory，业务层都重新读全部 memory_index、
    # 重新构造全部文档、重建整个 recall 索引 —— 成本随历史条数线性增长。
    # 支持增量的后端把 `supports_incremental` 置为 True 并实现这两个方法；
    # 其余后端保持原样即可（Selector 自动回退到全量重建）。

    def upsert(self, doc: IndexedDoc) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support incremental upsert")

    def remove(self, doc_id: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support incremental remove")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
