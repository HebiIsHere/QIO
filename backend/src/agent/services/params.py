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
        new_topic_threshold   — onnx cosine below this ⇒ no existing topic is a
            plausible owner (predictor: main_topic_id=None / is_new_topic_candidate)
        rules_new_topic_threshold — same, on the keyword-overlap fallback
        aux_topic_threshold   — onnx cosine above this ⇒ related (aux) topic
        rules_aux_topic_threshold — same, on the fallback layer
        incumbent_threshold   — classifier: at/above this the message still belongs
            to the *current* topic (onnx cosine scale)
        rules_incumbent_threshold — same, on the keyword-overlap fallback scale
            (两个后端的分数不同量纲：onnx 是余弦，兜底是关键词重合比例，
            所以每条门槛都要有兜底孪生值 —— 与 new_topic_threshold /
            rules_new_topic_threshold 的既有做法一致)
        switch_threshold      — classifier: at/above this a *different* topic may be
            suggested (still only a suggestion; the user confirms)
        switch_delta          — margin over the current topic needed for that suggestion
        new_topic_strict      — below this ⇒ eligible to create a new topic
        min_new_topic_chars   — 短于这个长度（去空白后的字符数）的输入不足以开新话题：
            真实语料里「嗯 / 继续 / ok」这类短确认的分数落在噪声带里（0.27~0.43），
            与「真新话题」和「延续」都重叠 —— 长度是唯一可靠的区分信号
        aux_top_count         — how many aux topics to surface

    Default source: **真实 embedding 评测**，不是历史默认值。
        eval:  backend/evals/topic_threshold_curve.py   （生产路径 + 真实 ONNX 余弦曲线）
               backend/evals/topic_threshold_analysis.py（分层 5 折 + 敏感度）
        data:  backend/evals/topic_threshold/{cases.jsonl, scores_onnx.json, curve_onnx.json}
        model: onnx:bge-small-zh-v1.5:fp32（真实 fp32 权重，离线，无网络）
        原始数据里的两条边界：真新话题对当前话题 ≤0.32；有内容的延续 ≥0.45。
        旧值 0.7 在这份语料上 in_topic_recall 0.044 / false_new 0.941，
        且只调数值无法收敛（短确认与无信号和两个分数带都重叠），
        所以拆成 incumbent / switch 两条门槛，见 affinity.classify。

    改这里的值必须同时更新上面两份 eval 的结论，否则默认值会重新变成
    「看起来有出处、实际没数据」的魔数。
    """

    new_topic_threshold: float = 0.42
    rules_new_topic_threshold: float = 0.2
    aux_topic_threshold: float = 0.3
    rules_aux_topic_threshold: float = 0.1
    switch_delta: float = 0.15
    new_topic_strict: float = 0.5
    switch_threshold: float = 0.55
    incumbent_threshold: float = 0.42
    rules_incumbent_threshold: float = 0.2
    min_new_topic_chars: int = 8
    aux_top_count: int = 2


@dataclass(frozen=True)
class RetrievalPolicy:
    """Retrieval ranking weights + recency half-life.

    Semantics:
        relevance_weight — 底层召回相关度（归一化后）在最终分里的权重
        recency_weight   — 时效衰减（agent/services/decay.py）的权重
        affinity_weight  — 话题亲和（锚点话题 / 话题指纹命中）的权重
        rule_weight      — 规则分项（anchor / entity / keyword，不含 recency）的权重
        recency_half_life_days — default (ephemeral) memory half-life；
            per-kind half-lives live in agent/services/decay.py

    这四项是**唯一**的排序权重来源：agent/services/retrieval.py 的 RetrievalConfig
    默认值直接取自这里，Selector 不再自己加一遍奖励分（2026-10-02 收敛为单层排序）。

    Default source: backend/evals/retrieval_ranking/ 的臂对比实验（生产代码 + 真实
        embedding，72 条带标签查询 / 67 条记忆）。
        实测（同一份 72 查询语料）：
          A 两层排序(rel .4/rec .25/aff .35)       R@1 0.597 R@5 0.750 MRR 0.664 wrong 0.403
          B/C 单层 + 纯相关性                       R@1 0.792 R@5 0.903 MRR 0.840 wrong 0.208
          单层 + 时效(害, 旧比例 .625)              R@1 0.389
          单层 + 话题亲和(害, 旧比例 .875)          R@1 0.722
          单层 + 规则分项(anchor/entity/keyword .25) R@1 0.847 R@5 0.917 MRR 0.873 wrong 0.153
          权重扫描 top：rec=0 / aff=0 / rule=0.25；分层 5 折折内选权的诚实估计 R@1=0.833。
          结论：时效与话题亲和两项**默认关掉**（没有收益的维度不留在配置里「看起来完整」），
          规则分项保留 0.25 —— 它在单层里只加一次时确实有效（同一维度不再被计两遍）。
          rule 里的 anchor 项与 affinity 项含义重叠，后者已按数据关掉，不要同时打开。
    Eval: backend/evals/retrieval_ranking/{run_arms.py,sweep_weights.py}、agent/eval/retrieval_eval.py。
    """

    relevance_weight: float = 1.0
    recency_weight: float = 0.0
    affinity_weight: float = 0.0
    rule_weight: float = 0.25
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
