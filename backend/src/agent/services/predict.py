"""Topic prediction: which topic does the current message belong to?

Judgement belongs to the (small) embedding model; execution (switching,
creating) belongs to the main model via tools. The predictor runs on the
critical path before injection: embed the message, compare against each
topic's representative vector, and emit main topic / auxiliary topics /
new-topic candidate flags. Falls back to fingerprint keyword overlap when
the embedding backend is unavailable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from agent.graph.topics import TopicService
from agent.selector.tokenize import tokenize

NEW_TOPIC_THRESHOLD = 0.45  # 真实 bge-small-zh 余弦分布：不相关约 0.23-0.35，相关 0.40+
AUX_TOPIC_THRESHOLD = 0.3  # 辅助话题低门槛：只需「有点相关」（onnx 余弦）
RULES_NEW_TOPIC_THRESHOLD = 0.2  # 规则重叠率分布：典型命中 0.2-0.5
RULES_AUX_TOPIC_THRESHOLD = 0.1
SWITCH_DELTA = 0.1
AUX_TOP_COUNT = 2


@dataclass(frozen=True)
class TopicPrediction:
    main_topic_id: str | None
    aux_topic_ids: list[str] = field(default_factory=list)
    is_new_topic_candidate: bool = False
    suggested_switch: bool = False
    scores: dict[str, float] = field(default_factory=dict)
    backend_used: str = "rules"


class TopicPredictor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        embedding,
        topics: TopicService,
        *,
        new_topic_threshold: float = NEW_TOPIC_THRESHOLD,
        switch_delta: float = SWITCH_DELTA,
        aux_top_count: int = AUX_TOP_COUNT,
        aux_topic_threshold: float = AUX_TOPIC_THRESHOLD,
        rules_new_topic_threshold: float = RULES_NEW_TOPIC_THRESHOLD,
        rules_aux_topic_threshold: float = RULES_AUX_TOPIC_THRESHOLD,
    ) -> None:
        self.conn = conn
        self.embedding = embedding
        self.topics = topics
        self.new_topic_threshold = new_topic_threshold
        self.switch_delta = switch_delta
        self.aux_top_count = aux_top_count
        self.aux_topic_threshold = aux_topic_threshold
        self.rules_new_topic_threshold = rules_new_topic_threshold
        self.rules_aux_topic_threshold = rules_aux_topic_threshold

    # -- fingerprint text -------------------------------------------------

    def _fingerprint_text(self, fingerprint) -> str:
        parts = [fingerprint.title or fingerprint.topic_id]
        if fingerprint.keywords:
            parts.append(" ".join(fingerprint.keywords))
        if fingerprint.summary_preview:
            parts.append(fingerprint.summary_preview)
        return " ".join(parts)

    # -- prediction -------------------------------------------------------

    def predict(
        self, query: str, *, current_topic_id: str | None = None
    ) -> TopicPrediction:
        fingerprints = self.topics.list_with_fingerprints()
        if not fingerprints:
            return TopicPrediction(
                main_topic_id=None,
                is_new_topic_candidate=True,
                backend_used=self.backend_name,
            )
        if self.embedding is not None and self.embedding.available():
            return self._predict_vectors(query, fingerprints, current_topic_id)
        return self._predict_rules(query, fingerprints, current_topic_id)

    def refresh_topic_vector(self, topic_id: str) -> None:
        """Re-embed a topic's fingerprint (called after a chunk close)."""
        if self.embedding is None or not self.embedding.available():
            return
        fp = self.topics.fingerprint(topic_id)
        self.embedding.update_topic_vector(topic_id, self._fingerprint_text(fp))

    @property
    def backend_name(self) -> str:
        if self.embedding is not None and self.embedding.available():
            return "onnx"
        return "rules"

    def _predict_vectors(self, query, fingerprints, current_topic_id) -> TopicPrediction:
        scores: dict[str, float] = {}
        query_vec = self.embedding.embed_texts([query])
        if query_vec is None:
            return self._predict_rules(query, fingerprints, current_topic_id)
        for fp in fingerprints:
            vec = self.embedding.topic_vector(fp.topic_id)
            if vec is None:
                # cold start: generate and persist the representative vector
                self.embedding.update_topic_vector(fp.topic_id, self._fingerprint_text(fp))
                vec = self.embedding.topic_vector(fp.topic_id)
            if vec is None:
                continue
            denom = (float(np_linalg_norm(vec)) * float(np_linalg_norm(query_vec[0]))) or 1e-9
            scores[fp.topic_id] = float(np_dot(vec, query_vec[0]) / denom)
        return self._rank(scores, current_topic_id, backend="onnx")

    def _predict_rules(self, query, fingerprints, current_topic_id) -> TopicPrediction:
        """Fallback: keyword overlap between query tokens and topic keywords."""
        query_tokens = set(tokenize(query))
        scores: dict[str, float] = {}
        if query_tokens:
            for fp in fingerprints:
                overlap = query_tokens & set(fp.keywords)
                scores[fp.topic_id] = len(overlap) / len(query_tokens)
        return self._rank(scores, current_topic_id, backend="rules")

    def _rank(self, scores, current_topic_id, *, backend) -> TopicPrediction:
        if not scores:
            return TopicPrediction(
                main_topic_id=None,
                is_new_topic_candidate=True,
                backend_used=backend,
            )
        # sort by score desc, tie-break by id
        ordered = sorted(scores.items(), key=lambda pair: (pair[1], pair[0]), reverse=True)
        new_th = self.new_topic_threshold if backend == "onnx" else self.rules_new_topic_threshold
        aux_th = self.aux_topic_threshold if backend == "onnx" else self.rules_aux_topic_threshold
        main_id, main_score = ordered[0]
        is_new = main_score < new_th
        if is_new:
            main_id = None
        aux: list[str] = []
        for tid, score in ordered:
            if tid == main_id:
                continue
            if score < aux_th:
                continue
            aux.append(tid)
            if len(aux) >= self.aux_top_count:
                break
        suggested = False
        if main_id is not None and current_topic_id is not None and main_id != current_topic_id:
            current_score = scores.get(current_topic_id, 0.0)
            suggested = main_score - current_score >= self.switch_delta
        return TopicPrediction(
            main_topic_id=main_id,
            aux_topic_ids=aux,
            is_new_topic_candidate=is_new,
            suggested_switch=suggested,
            scores=scores,
            backend_used=backend,
        )


def np_linalg_norm(v):
    import numpy as np

    return np.linalg.norm(v)


def np_dot(a, b):
    import numpy as np

    return float(np.dot(a, b))
