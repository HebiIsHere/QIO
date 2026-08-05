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

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self._docs: list[IndexedDoc] = []
        self._token_counts: list[Counter[str]] = []
        self._doc_len: list[int] = []
        self._avgdl = 0.0
        self._df: Counter[str] = Counter()
        self._built = False

    def available(self) -> bool:
        return True

    def index(self, docs: list[IndexedDoc]) -> None:
        self._docs = list(docs)
        self._token_counts = []
        self._doc_len = []
        self._df = Counter()
        total = 0
        for doc in self._docs:
            counts = Counter(tokenize(doc.text))
            self._token_counts.append(counts)
            self._doc_len.append(sum(counts.values()))
            total += sum(counts.values())
            for token in counts:
                self._df[token] += 1
        self._avgdl = total / len(self._docs) if self._docs else 0.0
        self._built = True

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