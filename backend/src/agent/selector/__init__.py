"""M5 memory selector: rule layer + pluggable recall + optional rerank."""

from agent.selector.base import IndexedDoc, MemoryCandidate, RecallBackend, ScoredDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector

__all__ = ["Selector", "BM25Backend", "RecallBackend", "IndexedDoc", "ScoredDoc", "MemoryCandidate"]