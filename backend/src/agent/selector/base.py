"""Selector interfaces.

The selector answers one question: which memory fragments are relevant to
the current user request. It must be fast and deterministic-first:
- recall layer: pluggable (BM25 / ONNX embeddings / remote API);

候选阶段**只按底层检索相关程度**取值：不叠加话题 / 时效 / 关键词 / 实体等
业务奖励。业务排序（可选奖励与重排）的唯一入口是
`agent/services/ranking.py`，由 `agent/services/retrieval.py` 调用。
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
    """一条候选记忆：原始相关度 + 稳定身份 + 排序入口需要的元数据。"""

    doc_id: str
    #: 底层召回给出的原始相关分（没有被任何业务奖励修改过）。
    relevance: float
    sources: tuple[str, ...]
    topic_id: str | None = None
    title: str | None = None
    token_estimate: int = 0
    created_at: str | None = None
    keywords: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()


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
