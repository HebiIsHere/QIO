# -*- coding: utf-8 -*-
"""话题归属分类：把消息归类为 延续 / 切换 / 新建。

决策信号（**唯一判定入口**，predict.py 只负责给出每条话题的相似度）：
- 硬信号：相似度分数 + 当前话题；三种决定用**三个不同的门槛**，不再共用一条
  绝对阈值（这是 2026-10-02 修掉的根因）；
- 软信号：消息命中的实体卡关联话题（entity_topics）只作提示（hints），不改变硬判定。

为什么不能只看一条阈值：真实 ONNX（bge-small-zh-v1.5）分数带很窄，而且
「真新话题」与「措辞变化后的延续」在词面上会互相靠近（对抗样本：
当前是数据库话题时「Excel 透视表怎么用」同样像数据库）。旧实现用
new_topic_threshold=0.7 一条线同时管「延续 / 切换 / 新建」，真实语料上
in_topic_recall 0.044、false_new 0.941（见 backend/evals/topic_threshold/）。

现在的规则（owner-first）：

1. 「有归属」的定义：最高分 >= new_topic_threshold ⇒ 这条消息有主人（owner）。
   owner 就是当前话题 → 延续；
2. owner 是别的话题：分数 >= switch_threshold **且**领先当前一个 switch_delta
   → SWITCH（仍只发「待确认切换」，绝不自行移锚）；
3. 没有 owner，或者 owner 证据不足：只要当前话题还有信号
   （current_score >= incumbent_threshold）**或**输入短到撑不起一个新话题
   （< min_new_topic_chars，例如「嗯 / 继续 / ok」）→ 延续；
4. 其余 → 新建。

阈值与默认值来自 backend/evals/topic_threshold/（生产路径 + 真实 embedding 的
离线评测），全部集中在 agent/services/params.py，禁止在此新增魔数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TopicMode(str, Enum):
    IN_TOPIC = "in_topic"
    SWITCH = "switch"
    NEW_TOPIC = "new_topic"


# 阈值集中管理（见 agent/services/params.py 与 backend/evals/topic_threshold/）
from agent.services.params import TOPIC as _TOPIC
from agent.services.params import TopicPolicy

# 切换到其他已有话题的预测分数门槛
SWITCH_THRESHOLD = _TOPIC.switch_threshold
# 真正「新建」的严格上限：top score < 该值才允许 create_topic；[strict, switch) 引导 switch
NEW_TOPIC_STRICT = _TOPIC.new_topic_strict
# 留在现任话题所需的最低分数（低于切换门槛）
INCUMBENT_THRESHOLD = _TOPIC.incumbent_threshold


@dataclass
class TopicDecision:
    mode: TopicMode
    switch_to: str | None = None      # SWITCH 的目标话题 id
    closest_topic: str | None = None  # 最相似话题 id（NEW_TOPIC 引导用）
    closest_score: float = 0.0
    entity_hints: list[str] = field(default_factory=list)  # 实体软提示（关联话题 id）


def classify(
    message: str,
    prediction,
    current_topic_id: str | None,
    entity_topics: list[str] | None = None,
    *,
    policy: TopicPolicy | None = None,
) -> TopicDecision:
    """按分数 + 当前话题 + 实体软信号归类消息归属。

    `policy` 可注入（默认取生产 TOPIC），评测与测试用它扫阈值，
    不需要 monkeypatch 模块常量 —— 被评测的就是产品跑的这一份逻辑。
    """
    pol = policy or _TOPIC
    scores = dict(getattr(prediction, "scores", None) or {})
    hints = [t for t in (entity_topics or []) if t and t != current_topic_id]

    top_topic = max(scores, key=scores.get) if scores else None
    top_score = scores.get(top_topic, 0.0) if top_topic else 0.0
    current_score = scores.get(current_topic_id, 0.0) if current_topic_id else 0.0

    text = (message or "").strip()
    # 「有归属」由**预测器**判定（它知道后端用的是哪条阈值：onnx 用
    # new_topic_threshold，关键词兜底用 rules_new_topic_threshold，两者量纲不同）。
    # 这里不重新用 onnx 阈值算一遍，否则兜底路径的 0.22 会被 0.42 判成「没有主人」。
    backend = getattr(prediction, "backend_used", "rules")
    owner = getattr(prediction, "main_topic_id", None)
    incumbent_threshold = (
        pol.incumbent_threshold if backend == "onnx" else pol.rules_incumbent_threshold
    )

    # 1) 主人就是当前话题 → 延续
    if owner is not None and owner == current_topic_id:
        return TopicDecision(TopicMode.IN_TOPIC, entity_hints=hints)

    # 2) 主人是别的话题：够强 + 领先当前一个 margin → 待确认切换
    #    门槛按后端取孪生值：onnx 余弦与兜底重合比例不同量纲，共用一条会让兜底
    #    永远切不动（兜底分数 0~0.22 < 0.55，实测切换召回 0.000）。
    switch_threshold = pol.switch_threshold if backend == "onnx" else pol.rules_switch_threshold
    switch_delta = pol.switch_delta if backend == "onnx" else pol.rules_switch_delta
    if (
        owner is not None
        and owner != current_topic_id
        and top_score >= switch_threshold
        and top_score - current_score >= switch_delta
    ):
        return TopicDecision(
            TopicMode.SWITCH,
            switch_to=owner,
            closest_topic=owner,
            closest_score=top_score,
            entity_hints=hints,
        )

    # 3) 没有主人 / 证据不足：当前话题还有信号，或输入短到撑不起新话题 → 延续
    if current_topic_id and (
        current_score >= incumbent_threshold or len(text) < pol.min_new_topic_chars
    ):
        return TopicDecision(TopicMode.IN_TOPIC, entity_hints=hints)

    # 4) 其余 → 新建
    return TopicDecision(
        TopicMode.NEW_TOPIC,
        closest_topic=top_topic,
        closest_score=top_score,
        entity_hints=hints,
    )


def related_topics(conn, topic_id: str, top_n: int = 2) -> list[str]:
    """沿 related 边取当前话题的一跳邻话题（按权重降序，供联想激活）。"""
    rows = conn.execute(
        "SELECT src, dst FROM edges WHERE type = 'related' AND (src = ? OR dst = ?) "
        "ORDER BY weight DESC",
        (topic_id, topic_id),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        other = r["dst"] if r["src"] == topic_id else r["src"]
        if other != topic_id and other not in out:
            out.append(other)
            if len(out) >= top_n:
                break
    return out


def relate_shared_entities(conn, topic_id: str, entity_ids: list[str]) -> None:
    """同一实体出现在多个话题 → 这些话题对加 related 边（实体是话题间的桥梁）。"""
    from agent.graph.edges import EdgeService

    edges = EdgeService(conn)
    for entity_id in entity_ids:
        rows = conn.execute(
            "SELECT src FROM edges WHERE dst = ? AND type = 'mention'",
            (entity_id,),
        ).fetchall()
        for r in rows:
            other = r["src"]
            if other == topic_id:
                continue
            a, b = sorted([topic_id, other])
            edges.add(a, b, "related")
