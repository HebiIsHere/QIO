"""M5 memory selector：只按原始相关程度取候选 + 可插拔召回后端。

业务排序（可选奖励 / 重排）不在这一层，见 `agent/services/ranking.py`。
"""

from agent.selector.base import IndexedDoc, MemoryCandidate, RecallBackend, ScoredDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector

__all__ = ["Selector", "BM25Backend", "RecallBackend", "IndexedDoc", "ScoredDoc", "MemoryCandidate"]
