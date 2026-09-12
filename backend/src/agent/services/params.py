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


@dataclass(frozen=True)
class FocusPolicy:
    """锚点 Focus 块（「从这里开始」/ Agent 显式 continue）的构造参数。

    Semantics:
        head_messages — 片段开头保留几条（交待这段历史在讨论什么）
        tail_messages — 片段结尾保留几条（最终结论 / 用户最后的问题在这里）
        max_message_chars — 单条消息进入 Focus 的字符上限（截断而非整条塞满）
        max_summary_chars — 摘要进入 Focus 的字符上限
        max_tokens — Focus 块硬上限（同时受 TokenBudgetPlanner 的注入预算约束）

    Default source: 2 + 3 是对现有「摘要 + 前 3 条」的修正：旧实现拿不到
        片段尾部的最终决定（见 tests/test_focus.py 的 tail 断言）。
    Eval: agent/eval/anchor_eval.py（Anchor Continuation Eval）观察
        Focus + semantic retrieval 的 Recall / 重复注入。
    """

    head_messages: int = 2
    tail_messages: int = 3
    max_message_chars: int = 500
    max_summary_chars: int = 800
    max_tokens: int = 1200


FOCUS = FocusPolicy()


@dataclass(frozen=True)
class SearchPolicy:
    """联网搜索通道策略。

    Semantics:
        provider_cooldown_seconds — 某通道被判定为「反爬 / 人机验证」后，
            在该时长内不再尝试它（避免每次搜索都去撞同一堵墙）。

    Default source: 10 分钟是「反爬通常不会秒解」与「不要永久放弃」之间的折中；
        用户在设置里配置 SearXNG / 博查后仍然优先走自己的通道。
    Eval: 无（通道可用性由 tests/test_search_providers.py 覆盖）。
    """

    provider_cooldown_seconds: int = 600


SEARCH = SearchPolicy()
