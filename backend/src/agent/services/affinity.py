# -*- coding: utf-8 -*-
"""话题归属分类：把消息归类为 延续 / 切换 / 新建。

决策信号：
- 硬信号：预测器 main_topic（==当前→延续；其他且 score≥SWITCH_THRESHOLD→切换）→ new_candidate→新建；
- 软信号：消息命中的实体卡关联话题（entity_topics）只作提示（hints），不改变硬判定。
新建再收紧：top score < NEW_TOPIC_STRICT 才真正新建，[strict, switch) 区间引导切换到最相似话题。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TopicMode(str, Enum):
    IN_TOPIC = "in_topic"
    SWITCH = "switch"
    NEW_TOPIC = "new_topic"


# 切换到其他已有话题的预测分数门槛
SWITCH_THRESHOLD = 0.55
# 真正「新建」的严格上限：top score < 该值才允许 create_topic；[strict, switch) 引导 switch
NEW_TOPIC_STRICT = 0.5


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
) -> TopicDecision:
    """按预测结果 + 实体软信号归类消息归属。"""
    scores = dict(getattr(prediction, "scores", None) or {})
    main = getattr(prediction, "main_topic_id", None)
    hints = [t for t in (entity_topics or []) if t and t != current_topic_id]

    if main and main == current_topic_id:
        return TopicDecision(TopicMode.IN_TOPIC, entity_hints=hints)
    if main and main != current_topic_id and scores.get(main, 0.0) >= SWITCH_THRESHOLD:
        return TopicDecision(TopicMode.SWITCH, switch_to=main, entity_hints=hints)

    top_topic = max(scores, key=scores.get) if scores else None
    top_score = scores.get(top_topic, 0.0) if top_topic else 0.0
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
