"""Retrieval evaluator (deterministic, offline).

Uses the production recall (Selector/BM25) + the production ranking entry
(`agent/services/ranking.py`, default = 只按相关程度) + production decay
(DecayPolicy). No DB and no network: timestamps and kinds come from the case
data via small overrides, so results are reproducible on any machine.

Metrics: Recall@1, Recall@5, MRR, wrong-memory injection rate,
stale-knowledge injection rate.

默认走关键词后端（不需要模型、任何机器上结果一致）。传入 `recall_factory`
时改用它生产的那条召回路径（例如内置 ONNX 嵌入模型）：工厂**每个用例调一次**，
因为后端实例会把向量缓存在自己身上，跨用例复用会串味。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agent.selector.base import IndexedDoc, RecallBackend
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.services.decay import DecayPolicy
from agent.services.retrieval import Retriever


class _StubTopics:
    def list_with_fingerprints(self):
        return []


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def rank_case(
    case: dict[str, Any],
    top_k: int = 5,
    *,
    recall_factory: Callable[[], RecallBackend | None] | None = None,
) -> list[str]:
    docs = [
        IndexedDoc(
            doc_id=d["id"],
            text=d["text"],
            topic_id=d.get("topic"),
            keywords=d.get("keywords", []),
        )
        for d in case["docs"]
    ]
    # 不传工厂 = 现状（关键词后端）；传了就用调用方给的召回后端，
    # 挂 BM25 作为后备 —— 与 app.py 里 Selector 的装配方式一致。
    selector = (
        Selector()
        if recall_factory is None
        else Selector(recall=recall_factory(), fallback_recall=BM25Backend())
    )
    selector.load(docs)
    retriever = Retriever(
        selector,
        _StubTopics(),
        conn=None,
        decay=DecayPolicy(),
    )
    ages = {d["id"]: d.get("created_days_ago", 0.0) for d in case["docs"]}
    kinds = {d["id"]: d.get("kind", "ephemeral") for d in case["docs"]}
    retriever._created_at = lambda doc_id: _iso(ages.get(doc_id, 0.0))  # type: ignore[assignment]
    retriever._preview = lambda doc_id, title: case["docs"][0]["text"][:0]  # type: ignore[assignment]
    retriever.kind_of = lambda doc_id: kinds.get(doc_id, "ephemeral")  # type: ignore[assignment]
    hits = retriever.search(case["query"], top_k=top_k)
    return [h.doc_id for h in hits]


def evaluate(
    cases: list[dict[str, Any]],
    top_k: int = 5,
    *,
    recall_factory: Callable[[], RecallBackend | None] | None = None,
) -> dict[str, Any]:
    recall1 = recall5 = 0
    rr_sum = 0.0
    wrong = 0
    stale = 0
    rows: list[dict[str, Any]] = []
    for case in cases:
        expected = set(case["expected"])
        stale_ids = set(case.get("stale", []))
        ranked = rank_case(case, top_k=top_k, recall_factory=recall_factory)
        top1 = set(ranked[:1])
        top5 = set(ranked[:top_k])
        if top1 & expected:
            recall1 += 1
        if top5 & expected:
            recall5 += 1
        rr = 0.0
        for i, doc_id in enumerate(ranked, start=1):
            if doc_id in expected:
                rr = 1.0 / i
                break
        rr_sum += rr
        if not (top1 & expected):
            wrong += 1
        # 有害情况：stale 文档排在正确文档之前（越权注入）
        first_expected = next((i for i, d in enumerate(ranked) if d in expected), None)
        stale_above = any(
            d in stale_ids and (first_expected is None or i < first_expected)
            for i, d in enumerate(ranked)
        )
        if stale_above:
            stale += 1
        rows.append(
            {
                "id": case.get("id"),
                "ranked": ranked,
                "expected": sorted(expected),
                "hit@1": bool(top1 & expected),
            }
        )
    n = len(cases) or 1
    return {
        "n": len(cases),
        "recall@1": round(recall1 / n, 4),
        "recall@5": round(recall5 / n, 4),
        "mrr": round(rr_sum / n, 4),
        "wrong_memory_injection_rate": round(wrong / n, 4),
        "stale_knowledge_injection_rate": round(stale / n, 4),
        "rows": rows,
    }
