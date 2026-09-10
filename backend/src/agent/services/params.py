"""Central policy parameters (thresholds / weights) + their provenance.

Every tunable that used to be a scattered magic number lives here, with:
    - semantics: what the value means,
    - default source: why this default,
    - eval: which eval observes/adjusts it (see agent/eval/).

Rule: do not add a heuristic threshold anywhere else. If a value needs a new
home, add a field here with the three notes above.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopicPolicy:
    """Topic prediction / classification thresholds.

    Semantics:
        new_topic_threshold   — onnx cosine below this ⇒ not an existing topic
        rules_new_topic_threshold — same, on the keyword-overlap fallback
        aux_topic_threshold   — onnx cosine above this ⇒ related (aux) topic
        rules_aux_topic_threshold — same, on the fallback layer
        switch_delta          — margin over current topic to suggest switching
        new_topic_strict      — classifier: below this ⇒ eligible to create
        switch_threshold      — classifier: at/above this ⇒ switch to that topic
        aux_top_count         — how many aux topics to surface

    Default source: current production values (unchanged behaviour), to be
    re-derived from `evals/topic_prediction/` before any future change.
    Eval: agent/eval/topic_eval.py (switch/new-topic/in-topic metrics).
    """

    new_topic_threshold: float = 0.7
    rules_new_topic_threshold: float = 0.2
    aux_topic_threshold: float = 0.3
    rules_aux_topic_threshold: float = 0.1
    switch_delta: float = 0.1
    new_topic_strict: float = 0.5
    switch_threshold: float = 0.55
    aux_top_count: int = 2


@dataclass(frozen=True)
class RetrievalPolicy:
    """Retrieval ranking weights + recency half-life.

    Semantics:
        relevance_weight / recency_weight / affinity_weight — the three-way
            ranking blend (sum to 1.0 by convention)
        recency_half_life_days — default (ephemeral) memory half-life;
            per-kind half-lives live in agent/services/decay.py

    Default source: current production values (unchanged behaviour).
    Eval: agent/eval/retrieval_eval.py (Recall@k / MRR / stale-injection).
    """

    relevance_weight: float = 0.4
    recency_weight: float = 0.25
    affinity_weight: float = 0.35
    recency_half_life_days: float = 30.0


TOPIC = TopicPolicy()
RETRIEVAL = RetrievalPolicy()
