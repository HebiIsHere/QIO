"""三条臂：把同一份压力语料喂进 qio 的真实生产入口。

A 臂 = 规则 + 关键词；B 臂 = 规则 + 本地 embedding（生产上模型可用时的完整链路）。
两臂共用同一批用例，所以每个数字都是同题对照。

几处刻意的做法：

- 检索只建一次索引、跑全部查询 —— 生产也是这样（记忆库是常驻索引），
  逐个用例重建索引会把"每条查询的真实代价"算歪。
- 本地模型后端**整个工作跑一个实例**：话题向量与实体卡向量在生产里是持久化复用的，
  每个用例重建既慢又不真实。
- 实体匹配的规则臂用"名称/别名包含"（生产 `EntityCardService.match_cards` 的规则），
  去重的规则臂用名称 Jaccard ≥ 0.8（生产 `CreateTopicTool.NAME_SIM_THRESHOLD`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from agent.eval.embedding_backend import build_backend
from agent.eval.stress_corpus import Memory, StressCorpus
from agent.selector.base import IndexedDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.services import params
from agent.services.affinity import classify
from agent.services.decay import DecayPolicy
from agent.services.predict import TopicPredictor
from agent.services.retrieval import RetrievalConfig, Retriever
from agent.services.tool_router import ToolRouter

NAME_SIM_THRESHOLD = 0.8
EMBED_LO = 0.5
EMBED_HI = 0.7


@dataclass
class _Fingerprint:
    topic_id: str
    title: str
    keywords: list[str]
    summary_preview: str = ""


class _TopicsStub:
    """把语料里的全部话题喂给 TopicPredictor（生产里这来自 TopicService）。"""

    def __init__(self, topics) -> None:
        self._fps = [_Fingerprint(t.id, t.title, list(t.keywords)) for t in topics]
        self.nodes = _NodesStub([t.id for t in topics])

    def list_with_fingerprints(self):
        return self._fps

    def fingerprint(self, topic_id: str):
        for fp in self._fps:
            if fp.topic_id == topic_id:
                return fp
        return None


class _TopicNode:
    """话题节点替身：`meta` 里在生产中有 `ended_at` 等状态，这里一律视为未结束。"""

    def __init__(self) -> None:
        self.meta: dict[str, Any] = {}


class _NodesStub:
    def __init__(self, ids: list[str]) -> None:
        self._ids = set(ids)

    def get_topic(self, topic_id: str):
        return _TopicNode() if topic_id in self._ids else None


def _iso(age_days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()


def _index_docs(memories: list[Memory]) -> tuple[list[IndexedDoc], dict[str, str], dict[str, str]]:
    docs: list[IndexedDoc] = []
    ages: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for m in memories:
        docs.append(
            IndexedDoc(
                doc_id=m.id,
                text=m.text,
                topic_id=m.topic_id,
                keywords=[],
                created_at=_iso(m.age_days),
            )
        )
        ages[m.id] = _iso(m.age_days)
        kinds[m.id] = m.kind
    return docs, ages, kinds


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class RulesArm:
    name = "rules"

    def __init__(self, corpus: StressCorpus) -> None:
        self.corpus = corpus
        self._docs, self._ages, self._kinds = _index_docs(corpus.memories)
        self._selector: Selector | None = None

    def _retriever(self) -> Retriever:
        if self._selector is None:
            self._selector = Selector()
            self._selector.load(self._docs)
        rp = params.RETRIEVAL
        retriever = Retriever(
            self._selector,
            _TopicsStub(self.corpus.topics),
            config=RetrievalConfig(
                relevance_weight=rp.relevance_weight,
                recency_weight=rp.recency_weight,
                affinity_weight=rp.affinity_weight,
                recency_half_life_days=rp.recency_half_life_days,
            ),
            conn=None,
            decay=DecayPolicy(),
        )
        retriever._created_at = lambda doc_id: self._ages.get(doc_id)
        retriever._preview = lambda doc_id, title: ""
        retriever.kind_of = lambda doc_id: self._kinds.get(doc_id, "ephemeral")
        return retriever

    def recall(self, cases, k: int = 5):
        retriever = self._retriever()
        ranked: list[list[str]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            hits = retriever.search(case.query, top_k=k)
            latencies.append((time.perf_counter() - started) * 1000)
            ranked.append([h.doc_id for h in hits])
        return ranked, latencies

    def topic(self, cases):
        predictor = TopicPredictor(None, None, _TopicsStub(self.corpus.topics))
        return _run_topic(predictor, cases)

    def entity(self, cases):
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            predicted = None
            for card in self.corpus.entities:
                if case.query.find(card.name) >= 0 or any(
                    a and case.query.find(a) >= 0 for a in card.aliases
                ):
                    predicted = card.id
                    break
            latencies.append((time.perf_counter() - started) * 1000)
            rows.append(
                {"id": case.id, "expected": case.expected_card_id, "predicted": predicted}
            )
        return rows, latencies

    def tool(self, cases):
        specs = [
            SimpleNamespace(name=t.name, description=t.description) for t in self.corpus.tools
        ]
        router = ToolRouter(embedding=None)
        return _run_tool(router, specs, cases)

    def dedup(self, cases):
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            predicted = None
            for topic in self.corpus.topics:
                if _jaccard(case.candidate_name, topic.title) >= NAME_SIM_THRESHOLD:
                    predicted = topic.id
                    break
            latencies.append((time.perf_counter() - started) * 1000)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_duplicate,
                    "expected_duplicate_of": case.duplicate_of,
                    "predicted": predicted is not None,
                    "predicted_duplicate_of": predicted,
                }
            )
        return rows, latencies


class LocalEmbeddingArm(RulesArm):
    """在规则之上把本地模型接进判定（生产上模型可用时的链路）。"""

    name = "local"

    def __init__(self, corpus: StressCorpus, model_dir=None) -> None:
        super().__init__(corpus)
        backend, note = build_backend(model_dir)
        if backend is None:
            raise RuntimeError(f"本地嵌入模型不可用：{note}")
        self.backend = backend
        self.note = note
        self._topics_warm = False
        self._entities_warm = False

    def _retriever(self) -> Retriever:
        if self._selector is None:
            self._selector = Selector(
                recall=self.backend, fallback_recall=BM25Backend()
            )
            self._selector.load(self._docs)
        rp = params.RETRIEVAL
        retriever = Retriever(
            self._selector,
            _TopicsStub(self.corpus.topics),
            config=RetrievalConfig(
                relevance_weight=rp.relevance_weight,
                recency_weight=rp.recency_weight,
                affinity_weight=rp.affinity_weight,
                recency_half_life_days=rp.recency_half_life_days,
            ),
            conn=None,
            decay=DecayPolicy(),
        )
        retriever._created_at = lambda doc_id: self._ages.get(doc_id)
        retriever._preview = lambda doc_id, title: ""
        retriever.kind_of = lambda doc_id: self._kinds.get(doc_id, "ephemeral")
        return retriever

    def topic(self, cases):
        return _run_topic(self._predictor(), cases)

    def dedup(self, cases):
        predictor = self._predictor()
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            predicted = None
            for topic in self.corpus.topics:
                if _jaccard(case.candidate_name, topic.title) >= NAME_SIM_THRESHOLD:
                    predicted = topic.id
                    break
            if predicted is None:
                prediction = predictor.predict(case.candidate_name, current_topic_id=None)
                scores = dict(getattr(prediction, "scores", None) or {})
                if scores:
                    top_id = max(scores, key=scores.get)
                    top = scores[top_id]
                    if EMBED_LO <= top < EMBED_HI:
                        predicted = top_id
            latencies.append((time.perf_counter() - started) * 1000)
            rows.append(
                {
                    "id": case.id,
                    "expected": case.expected_duplicate,
                    "expected_duplicate_of": case.duplicate_of,
                    "predicted": predicted is not None,
                    "predicted_duplicate_of": predicted,
                }
            )
        return rows, latencies

    def entity(self, cases):
        if not self._entities_warm:
            for card in self.corpus.entities:
                self.backend.save_entity_card_vector(
                    card.id, f"{card.name} {' '.join(card.aliases)} {card.summary}"
                )
            self._entities_warm = True
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        for case in cases:
            started = time.perf_counter()
            hits = self.backend.entity_card_search(case.query, top_k=1)
            latencies.append((time.perf_counter() - started) * 1000)
            predicted = hits[0][0] if hits else None
            rows.append(
                {"id": case.id, "expected": case.expected_card_id, "predicted": predicted}
            )
        return rows, latencies

    def tool(self, cases):
        specs = [
            SimpleNamespace(name=t.name, description=t.description) for t in self.corpus.tools
        ]
        router = ToolRouter(embedding=self.backend)
        return _run_tool(router, specs, cases)

    def _predictor(self) -> TopicPredictor:
        predictor = TopicPredictor(None, self.backend, _TopicsStub(self.corpus.topics))
        if not self._topics_warm:
            for topic in self.corpus.topics:
                fingerprint = predictor.topics.fingerprint(topic.id)
                self.backend.update_topic_vector(
                    topic.id, predictor._fingerprint_text(fingerprint)
                )
            self._topics_warm = True
        return predictor


def _run_topic(predictor: TopicPredictor, cases):
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        prediction = predictor.predict(case.message, current_topic_id=case.current_topic_id)
        decision = classify(case.message, prediction, case.current_topic_id, [])
        latencies.append((time.perf_counter() - started) * 1000)
        rows.append(
            {
                "id": case.id,
                "expected": case.expected_mode,
                "predicted": decision.mode.value,
                "expected_topic": case.expected_topic_id,
                "predicted_topic": prediction.main_topic_id,
                "backend": prediction.backend_used,
            }
        )
    return rows, latencies


def _run_tool(router: ToolRouter, specs, cases):
    order = [s.name for s in specs]
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        picked = router.route(case.query, specs)
        latencies.append((time.perf_counter() - started) * 1000)
        picked_names = [s.name for s in picked]
        rest = [n for n in order if n not in picked_names]
        rows.append(
            {
                "id": case.id,
                "expected": case.expected_tool,
                "ranked": picked_names + rest,
                "predicted": picked_names[0] if picked_names else None,
            }
        )
    return rows, latencies
