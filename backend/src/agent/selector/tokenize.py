"""Lightweight tokenizer for BM25.

No external NLP dependency: latin words are lowercased; CJK runs produce
1-grams and 2-grams (bigrams capture compound terms without a segmenter).
"""

from __future__ import annotations

import re

_LATIN = re.compile(r"[a-zA-Z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _LATIN.finditer(text):
        token = match.group(0).lower()
        if len(token) >= 2:
            tokens.append(token)
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run)):
            tokens.append(run[i])
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    return tokens