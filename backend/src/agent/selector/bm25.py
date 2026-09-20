"""Okapi BM25 recall backend, pure Python.

Always available (no model download), which makes it the default tier for
machines without a GPU. index() rebuilds from the given docs.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from agent.selector.base import IndexedDoc, RecallBackend, ScoredDoc
from agent.selector.tokenize import tokenize

K1 = 1.5
B = 0.75


class BM25Backend(RecallBackend):
    name = "bm25"
    supports_incremental = True

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self._docs: list[IndexedDoc] = []
        self._token_counts: list[Counter[str]] = []
        self._doc_len: list[int] = []
        self._avgdl = 0.0
        self._df: Counter[str] = Counter()
        self._built = False
        # doc_id → 在列表中的位置：增量更新靠它做 O(1) 定位
        self._pos: dict[str, int] = {}
        # 全库总词数（avgdl 的分子）：增量维护，避免每次重新求和
        self._total_len = 0

    def available(self) -> bool:
        return True

    def index(self, docs: list[IndexedDoc]) -> None:
        self._docs = list(docs)
        self._token_counts = []
        self._doc_len = []
        self._df = Counter()
        self._pos = {}
        total = 0
        for doc in self._docs:
            counts = Counter(tokenize(doc.text))
            self._token_counts.append(counts)
            self._doc_len.append(sum(counts.values()))
            total += sum(counts.values())
            for token in counts:
                self._df[token] += 1
        for i, doc in enumerate(self._docs):
            self._pos[doc.doc_id] = i
        self._total_len = total
        self._avgdl = total / len(self._docs) if self._docs else 0.0
        self._built = True

    # -- 增量更新 ---------------------------------------------------------
    #
    # 与全量重建**逐位一致**的前提：
    # 文档顺序 = 「按到达顺序追加，已有 doc_id 原位替换，删除后保持相对顺序」；
    # df / doc_len / avgdl 都按同一套规则增减，不改变打分公式与排序键。

    def upsert(self, doc: IndexedDoc) -> None:
        counts = Counter(tokenize(doc.text))
        length = sum(counts.values())
        pos = self._pos.get(doc.doc_id)
        if pos is None:
            self._docs.append(doc)
            self._token_counts.append(counts)
            self._doc_len.append(length)
            self._pos[doc.doc_id] = len(self._docs) - 1
        else:
            # 替换：先撤掉旧文档对 df 与总长度的贡献
            for token in self._token_counts[pos]:
                self._df[token] -= 1
                if self._df[token] <= 0:
                    del self._df[token]
            self._total_len -= self._doc_len[pos]
            self._docs[pos] = doc
            self._token_counts[pos] = counts
            self._doc_len[pos] = length
        for token in counts:
            self._df[token] += 1
        self._total_len += length
        self._avgdl = self._total_len / len(self._docs) if self._docs else 0.0
        self._built = True

    def remove(self, doc_id: str) -> None:
        pos = self._pos.pop(doc_id, None)
        if pos is None:
            return
        counts = self._token_counts.pop(pos)
        self._total_len -= self._doc_len.pop(pos)
        self._docs.pop(pos)
        for token in counts:
            self._df[token] -= 1
            if self._df[token] <= 0:
                del self._df[token]
        # 后面的文档整体前移一位：位置索引必须同步，否则顺序语义会漂
        for i in range(pos, len(self._docs)):
            self._pos[self._docs[i].doc_id] = i
        self._avgdl = self._total_len / len(self._docs) if self._docs else 0.0

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        if not self._built or not self._docs:
            return []
        # single CJK chars are too noisy as query terms; bigrams and words only
        query_tokens = {t for t in tokenize(query) if len(t) >= 2}
        if not query_tokens:
            return []
        n = len(self._docs)
        scores: list[tuple[float, int]] = []
        for idx, counts in enumerate(self._token_counts):
            score = 0.0
            for token in query_tokens:
                tf = counts.get(token, 0)
                if tf == 0:
                    continue
                df = self._df.get(token, 0)
                idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
                denom = tf + self.k1 * (1 - self.b + self.b * self._doc_len[idx] / self._avgdl)
                score += idf * (tf * (self.k1 + 1.0)) / denom
            scores.append((score, idx))
        scores.sort(key=lambda pair: (-pair[0], pair[1]))
        return [
            ScoredDoc(doc_id=self._docs[idx].doc_id, score=max(0.0, score), source=self.name)
            for score, idx in scores[:top_k]
            if score > 0
        ]
