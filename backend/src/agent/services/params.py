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
from enum import Enum


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


class RankingStrategy(str, Enum):
    """业务排序策略。

    - `relevance`：只按底层检索相关程度排序（默认）。所有可选奖励必须为 0，
      否则构造时就报错 —— 不允许「配置可调、实际被忽略」。
    - `weighted`：在相关度之上叠加显式开启的奖励。这些权重**尚未在生产验证**，
      只能在对照实验里显式开启，默认不启用。
    """

    RELEVANCE = "relevance"
    WEIGHTED = "weighted"


@dataclass(frozen=True)
class RankingPolicy:
    """记忆检索排序策略（唯一的一处：所有业务奖励与重排开关都在这里）。

    Semantics:
        strategy            — relevance（默认，纯相关度）| weighted（显式实验用）
        affinity_weight     — 话题亲和：当前锚点话题 1.0，否则用话题指纹重合度；默认 0
        recency_weight      — 时效：DecayPolicy 按信息种类给出的权重（0..1）；默认 0
        keyword_weight      — 关键词：查询词与记忆关键词的重合比例；默认 0
        entity_weight       — 实体：查询命中的实体卡出现在该记忆里；默认 0
        recency_half_life_days — 仅在复现旧行为时用作默认（ephemeral）半衰期
        rerank_candidate_cap   — 若启用重排，只对前 N 条候选做重排；单独表达，不与
                                 候选池 / 返回条数混用

    Default source: 目标行为 = 「只按相关程度排序」。旧的双层加权（Selector 内
        anchor .5 / entity .3 / keyword .2 / recency ≤.4，再乘 0.4/0.25/0.35）
        作为评测参考保留在 `agent/eval/ranking_eval.py`，不再是生产默认。
    Eval: agent/eval/ranking_eval.py（新旧对照）、retrieval_eval.py（Recall@k / MRR）。
    """

    strategy: RankingStrategy = RankingStrategy.RELEVANCE
    affinity_weight: float = 0.0
    recency_weight: float = 0.0
    keyword_weight: float = 0.0
    entity_weight: float = 0.0
    recency_half_life_days: float = 30.0
    rerank_candidate_cap: int = 12

    def __post_init__(self) -> None:
        strategy = RankingStrategy(self.strategy)
        object.__setattr__(self, "strategy", strategy)
        weights = {
            "affinity_weight": self.affinity_weight,
            "recency_weight": self.recency_weight,
            "keyword_weight": self.keyword_weight,
            "entity_weight": self.entity_weight,
        }
        if min(weights.values()) < 0:
            raise ValueError(f"排序奖励权重不得为负：{weights}")
        if strategy is RankingStrategy.RELEVANCE:
            nonzero = {k: v for k, v in weights.items() if v != 0}
            if nonzero:
                raise ValueError(
                    "纯相关性策略(relevance)不允许非零奖励权重，"
                    f"请用 strategy='weighted' 显式开启：{nonzero}"
                )
        if self.rerank_candidate_cap <= 0:
            raise ValueError("rerank_candidate_cap 必须为正整数")

    @property
    def weights(self) -> dict[str, float]:
        """生效的奖励权重（供 Trace / 评测记录「本次用的什么配置」）。"""
        return {
            "affinity": self.affinity_weight,
            "recency": self.recency_weight,
            "keyword": self.keyword_weight,
            "entity": self.entity_weight,
        }

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "weights": self.weights,
            "recency_half_life_days": self.recency_half_life_days,
            "rerank_candidate_cap": self.rerank_candidate_cap,
        }


@dataclass(frozen=True)
class RetrievalLimits:
    """记忆检索的数量参数：候选池 / 最终返回 / 主动检索上下限。

    Semantics:
        candidate_pool     — 候选池大小：按**原始相关度**从底层召回里截取的条数。
                             它是召回必需的候选选择，不叠加任何业务奖励。
        inject_return_limit — 自动注入（主 Turn）最终返回多少条记忆。
        memory_search_default_k / memory_search_max_k — 主动 `memory_search` 的
                             默认值与上限（调用方显式传 top_k 时以其为准）。

    Default source: 与改造前的实际调用一致 —— 自动注入最终 6 条、候选池 12 条；
        主动检索默认 5、上限 20。改造前召唤链里还隐含
        「候选池 ×3 条底层召回」与「返回 ×2 得候选池」两个乘数，本次取消：
        底层召回请求数 = 候选池，不再隐藏放大。
    Eval: agent/eval/ranking_eval.py（候选池 / 返回数 / 预算分别对照）。
    """

    candidate_pool: int = 12
    inject_return_limit: int = 6
    memory_search_default_k: int = 5
    memory_search_max_k: int = 20

    def __post_init__(self) -> None:
        for name in (
            "candidate_pool",
            "inject_return_limit",
            "memory_search_default_k",
            "memory_search_max_k",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正整数")
        if self.candidate_pool < self.inject_return_limit:
            raise ValueError("候选池不得小于自动注入的返回条数")
        if self.memory_search_max_k < self.memory_search_default_k:
            raise ValueError("memory_search_max_k 不得小于默认值")

    def pool_for(self, limit: int) -> int:
        """给定最终返回条数，返回本次候选池大小（至少能装下最终输出）。"""
        return max(self.candidate_pool, int(limit))

    def as_dict(self) -> dict:
        return {
            "candidate_pool": self.candidate_pool,
            "inject_return_limit": self.inject_return_limit,
            "memory_search_default_k": self.memory_search_default_k,
            "memory_search_max_k": self.memory_search_max_k,
        }


TOPIC = TopicPolicy()
RANKING = RankingPolicy()
LIMITS = RetrievalLimits()


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
