"""Cross-session retrieval with **one** ranking entry.

Pipeline:
1. first hop: topic fingerprints (session-level meta summaries) match the
   query — topics that score get an affinity factor;
2. candidate pool: the M5 selector over memory_index fragments returns
   candidates ordered by raw recall relevance only (no business rewards, no
   reward-driven truncation);
3. ranking: **here, once** — relevance (normalized) x relevance_weight +
   recency decay x recency_weight + topic affinity x affinity_weight +
   rule signals x rule_weight. All weights come from services.params.RETRIEVAL
   and default to pure relevance.

历史缺陷（2026-10-02 修）：奖励以前被算了两遍 —— Selector 先把
anchor/entity/keyword/recency 加进分数并据此截断候选池，Retriever 再加
recency + affinity 排序。第一层淘汰掉的候选第二层救不回来，而且同一维度
（时效）被计了两次。臂对比见 backend/evals/retrieval_ranking/。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.graph.topics import TopicService
from agent.services.decay import EPHEMERAL, DecayPolicy
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.selector.tokenize import tokenize

from agent.services.params import CROSS_TOPIC as _CROSS_TOPIC
from agent.services.params import RETRIEVAL as _RETRIEVAL

DEFAULT_RECENCY_HALF_LIFE_DAYS = _RETRIEVAL.recency_half_life_days


@dataclass
class RetrievalConfig:
    """排序权重载体。默认值**直接取自** services.params.RETRIEVAL（唯一权威）。

    显式传入的 config 用于评测/测试扫描权重；生产路径不传 config，
    因此改 params 就改生产行为（旧版这里有一份独立的数字，
    改 params 对生产完全无效）。
    """

    relevance_weight: float = _RETRIEVAL.relevance_weight
    recency_weight: float = _RETRIEVAL.recency_weight
    affinity_weight: float = _RETRIEVAL.affinity_weight
    rule_weight: float = _RETRIEVAL.rule_weight
    recency_half_life_days: float = _RETRIEVAL.recency_half_life_days
    fingerprint_top: int = 3


@dataclass
class RetrievalHit:
    doc_id: str
    topic_id: str | None
    title: str | None
    preview: str
    score: float
    sources: tuple[str, ...]
    token_estimate: int
    created_at: str | None
    # 片段身份：memory_index 的 doc_id 是索引行 id，Agent 需要的是它对应的
    # fragment_id（才能 continue_from_fragment / 去重）。实体卡等非片段命中为 None。
    fragment_id: str | None = None

    @property
    def age_days(self) -> float:
        if not self.created_at:
            return float("inf")
        try:
            created = datetime.fromisoformat(self.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
        except ValueError:
            return float("inf")


@dataclass
class MessageHit:
    """一条**保存的对话原文**的命中（不经过摘要、不依赖索引）。"""

    message_id: str
    fragment_id: str | None
    topic_id: str | None
    topic_name: str | None
    role: str
    created_at: str | None
    content: str
    score: float
    truncated: bool = False


class Retriever:
    # 原文检索一次最多扫多少条候选：它是「把说过的话找回来」，不是全库导出。
    # 先按时间取最近的一批，再按命中覆盖率排序取前几条。
    MESSAGE_SCAN_LIMIT = 200
    # 单条原文最多回多少字：够看清「当时到底说了什么」，又不至于把上下文塞满。
    MESSAGE_TEXT_LIMIT = 600

    def __init__(
        self,
        selector: Selector,
        topics: TopicService,
        config: RetrievalConfig | None = None,
        conn=None,
        decay: DecayPolicy | None = None,
        cross_topic=None,
    ) -> None:
        self.selector = selector
        self.topics = topics
        self.config = config or RetrievalConfig()
        # 跨话题候选生成策略（默认全关 = 与现状逐位一致）；排序权重不受它影响
        self.cross_topic = cross_topic or _CROSS_TOPIC
        self.conn = conn
        # 默认策略与旧行为一致（ephemeral half-life == recency_half_life_days）
        self.decay = decay or DecayPolicy({EPHEMERAL: self.config.recency_half_life_days})
        self._fragment_cache: dict[str, str | None] = {}

    def search_messages(
        self,
        query: str,
        *,
        topic_id: str | None = None,
        top_k: int = 3,
    ) -> list[MessageHit]:
        """在**保存的对话原文**里检索（`messages` 表本身，不经过摘要）。

        为什么需要这条路：`search()` 只看 `memory_index`，也就是「封存并摘要成功」
        的片段。还没封存的片段（以及摘要失败的内容）在那儿根本不存在 ——
        用户明明说过，Agent 却回「未找到相关记忆」。

        中文靠 CJK 1/2-gram 分词（见 `selector/tokenize.py`）：单字不参与匹配
        （否则「的」这种字会命中一切），二字词足够认出一句话。
        `topic_id` 是**过滤**而不是偏向：指定了话题就只在这个话题里找。

        这里**故意不缓存**：`Retriever` 与进程同寿，缓存会把「已经删掉的消息」
        一直答出来。每次都读当前的行，删除与修改自然一致。
        """
        if self.conn is None:
            return []
        terms = sorted({t for t in tokenize(query) if len(t) >= 2})
        if not terms:
            return []
        sql = (
            "SELECT m.id AS message_id, m.fragment_id, m.role, m.content, m.created_at, "
            "f.topic_id AS topic_id, n.name AS topic_name "
            "FROM messages m "
            "LEFT JOIN fragments f ON f.id = m.fragment_id "
            "LEFT JOIN nodes n ON n.id = f.topic_id "
            "WHERE m.content <> '' AND ("
            + " OR ".join("m.content LIKE ?" for _ in terms)
            + ")"
        )
        params: list[object] = [f"%{term}%" for term in terms]
        if topic_id:
            sql += " AND f.topic_id = ?"
            params.append(topic_id)
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(self.MESSAGE_SCAN_LIMIT)
        rows = self.conn.execute(sql, params).fetchall()

        hits: list[MessageHit] = []
        for row in rows:
            content = row["content"] or ""
            low = content.lower()
            matched = sum(1 for term in terms if term in low)
            if not matched:
                continue
            text = content[: self.MESSAGE_TEXT_LIMIT]
            hits.append(
                MessageHit(
                    message_id=row["message_id"],
                    fragment_id=row["fragment_id"],
                    topic_id=row["topic_id"],
                    topic_name=row["topic_name"],
                    role=row["role"],
                    created_at=row["created_at"],
                    content=text,
                    score=matched / len(terms),
                    truncated=len(content) > self.MESSAGE_TEXT_LIMIT,
                )
            )
        # rows 已按时间倒序；稳定排序让「命中一样多」时新的排在前面
        hits.sort(key=lambda hit: -hit.score)
        return hits[:top_k]

    def fragment_of(self, doc_id: str) -> str | None:
        """索引行 → 片段 id（没有 DB 连接时返回 None，例如离线 eval）。"""
        if self.conn is None:
            return None
        if doc_id in self._fragment_cache:
            return self._fragment_cache[doc_id]
        row = self.conn.execute(
            "SELECT fragment_id FROM memory_index WHERE id = ?", (doc_id,)
        ).fetchone()
        value = row["fragment_id"] if row is not None else None
        self._fragment_cache[doc_id] = value
        return value

    def kind_of(self, doc_id: str) -> str:
        """信息种类（供差异化衰减）。当前 memory_index 无可信分类 → ephemeral。

        未来若有可靠 category metadata，可在此按 doc_id 返回对应 kind，
        无需改动排序主逻辑（见 agent/services/decay.py）。
        """
        return EPHEMERAL

    # -- first hop: topic fingerprints ------------------------------------

    def _fingerprint_scores(self, query: str) -> dict[str, float]:
        query_tokens = set(tokenize(query))
        scores: dict[str, float] = {}
        if not query_tokens:
            return scores
        for fingerprint in self.topics.list_with_fingerprints():
            # 已结束的话题不再参与联想（不给 affinity 加分）；
            # 它的片段仍然留在记忆里，照样能被检索到。
            node = self.topics.nodes.get_topic(fingerprint.topic_id)
            if node is not None and node.meta.get("ended_at"):
                continue
            overlap = query_tokens & set(fingerprint.keywords)
            if overlap:
                scores[fingerprint.topic_id] = len(overlap) / len(query_tokens)
        return scores

    # -- 跨话题候选生成（只影响候选池，不动排序） --------------------------

    def _hint_topics(self, query: str, explicit: list[str] | None) -> list[str]:
        """候选话题：调用方显式给的优先（例如话题层算出的 aux topics），否则用指纹匹配。"""
        limit = max(0, int(self.cross_topic.topics_per_query))
        if not limit:
            return []
        ordered: list[str] = []
        for topic_id in explicit or []:
            if topic_id and topic_id not in ordered:
                ordered.append(topic_id)
        if not ordered and (self.cross_topic.expand_enabled or self.cross_topic.rewrite_enabled):
            scores = self._fingerprint_scores(query)
            for topic_id, _score in sorted(scores.items(), key=lambda pair: (-pair[1], pair[0])):
                if topic_id not in ordered:
                    ordered.append(topic_id)
        return ordered[:limit]

    def _rewrite_query(self, query: str, topics: list[str]) -> str:
        """给查询补上候选话题的关键词（确定性；不调用任何模型）。"""
        terms: list[str] = []
        for topic_id in topics:
            try:
                fingerprint = self.topics.fingerprint(topic_id)
            except Exception:  # noqa: BLE001 - 指纹取不到就不改写，不影响主路径
                continue
            for keyword in list(fingerprint.keywords)[: max(0, int(self.cross_topic.terms_per_topic))]:
                if keyword and keyword not in terms:
                    terms.append(str(keyword))
        if not terms:
            return query
        return f"{query} {' '.join(terms)}"

    def _relation_topics(self, anchor_topic_id: str | None) -> list[str]:
        """来源链（source_fragment_id）上的话题；同话题校验由 FragmentManager 保证。"""
        if self.conn is None or not anchor_topic_id:
            return []
        from agent.graph.anchors import AnchorService
        from agent.memory.fragment import FragmentManager

        fragment_id = AnchorService(self.conn).position_fragment(anchor_topic_id)
        if not fragment_id:
            return []
        fragments = FragmentManager(self.conn)
        topics: list[str] = []
        for ancestor_id, _depth in fragments.ancestors(fragment_id, max_depth=3):
            fragment = fragments.get(ancestor_id)
            if fragment is not None and fragment.topic_id and fragment.topic_id not in topics:
                topics.append(fragment.topic_id)
        current = fragments.get(fragment_id)
        if current is not None and current.topic_id and current.topic_id not in topics:
            topics.append(current.topic_id)
        return topics

    def _expand_candidates(self, query: str, anchor_topic_id, topics: list[str], pool: int):
        """候选扩充：用**同一个查询**取更宽的候选池，只保留候选话题的记忆。

        关键点（第一版踩过的坑）：扩充出来的候选必须和主召回**同源打分**——
        都相对用户的查询算相关度。若改用「话题指纹文本」去召回，候选分数是相对
        指纹算的，把它和主召回的分数放进同一个排序里就是在比两把不同的尺子，
        结果是指纹文本命中的文档被顶到最前面，普通集直接塌掉
        （实测普通集 R@1 0.847 → 0.347，见 EXPERIMENTS-CROSSTOPIC.md 的教训一节）。

        这里只做「放宽候选池 + 按话题过滤」，排序公式一行没动。
        """
        if not topics:
            return []
        from agent.selector.base import MemoryCandidate

        wide = self.selector.select(
            query,
            top_k=max(pool * 4, pool + 8),
            anchor_topic_id=anchor_topic_id,
        )
        wanted = set(topics)
        out: list[MemoryCandidate] = [c for c in wide if c.topic_id in wanted]
        return out

    # -- main search ------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        anchor_topic_id: str | None = None,
        top_k: int = 6,
        topic_hints: list[str] | None = None,
    ) -> list[RetrievalHit]:
        """检索记忆。

        `topic_hints`：调用方已知的候选话题（例如话题层算出的 aux topics / 评测里的
        oracle）。给了就把它当作**候选生成**的提示，排序公式一行不变。
        """
        pool = top_k * 2
        hint_topics = self._hint_topics(query, topic_hints)
        if self.cross_topic.relation_enabled:
            # 关系感知：沿 fragment 来源链把「路径上话题」的记忆也纳入候选。
            # 注意：来源链在写入时就被校验为**同话题**（FragmentManager.validate_source），
            # 所以它只能补当前话题的记忆，跨不出话题边界 —— 这一点由评测证实。
            for topic_id in self._relation_topics(anchor_topic_id):
                if topic_id not in hint_topics:
                    hint_topics.append(topic_id)
            hint_topics = hint_topics[: max(1, int(self.cross_topic.topics_per_query))]
        effective_query = (
            self._rewrite_query(query, hint_topics) if self.cross_topic.rewrite_enabled else query
        )
        candidates = self.selector.select(
            effective_query,
            top_k=pool,
            anchor_topic_id=anchor_topic_id,
        )
        if hint_topics and (self.cross_topic.expand_enabled or self.cross_topic.relation_enabled):
            merged = {c.doc_id: c for c in candidates}
            for candidate in self._expand_candidates(
                effective_query, anchor_topic_id, hint_topics, pool
            ):
                merged.setdefault(candidate.doc_id, candidate)
            candidates = list(merged.values())
        if not candidates:
            # 无记忆候选时仍允许实体卡命中（交流锚点）
            entity_hits = self._entity_card_hits(query, top_k=top_k)
            return [hit for _, hit in entity_hits[:top_k]]
        fingerprint_scores = self._fingerprint_scores(query)
        max_relevance = max((c.score for c in candidates), default=1.0) or 1.0

        ranked: list[tuple[float, RetrievalHit]] = []
        for candidate in candidates:
            relevance_norm = candidate.score / max_relevance
            recency = 0.0
            created_at = self._created_at(candidate.doc_id)
            if created_at is not None:
                age_days = self._age_days(created_at)
                if math.isfinite(age_days):
                    recency = self.decay.weight(age_days, self.kind_of(candidate.doc_id))
            affinity = 0.0
            if candidate.topic_id is not None:
                if anchor_topic_id and candidate.topic_id == anchor_topic_id:
                    affinity = 1.0
                elif candidate.topic_id in fingerprint_scores:
                    affinity = fingerprint_scores[candidate.topic_id]
            # 规则分项里 anchor/entity/keyword 是「业务奖励」；
            # recency 已经由上面的 decay 表示，不在这里重复计入（同一维度只算一次）。
            rule_signals = sum(
                value for name, value in (candidate.signals or {}).items() if name != "recency"
            )
            score = (
                self.config.relevance_weight * relevance_norm
                + self.config.recency_weight * recency
                + self.config.affinity_weight * affinity
                + self.config.rule_weight * rule_signals
            )
            ranked.append(
                (
                    score,
                    RetrievalHit(
                        doc_id=candidate.doc_id,
                        topic_id=candidate.topic_id,
                        title=candidate.title,
                        preview=self._preview(candidate.doc_id, candidate.title),
                        score=score,
                        sources=candidate.sources,
                        token_estimate=candidate.token_estimate,
                        created_at=created_at,
                        fragment_id=self.fragment_of(candidate.doc_id),
                    ),
                )
            )
        # 实体卡检索（交流锚点）：名称命中（强）+ 向量命中（增强）
        entity_hits = self._entity_card_hits(query, top_k=top_k)
        ranked = ranked + entity_hits
        ranked.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
        return [hit for _, hit in ranked[:top_k]]

    def _entity_card_hits(self, query: str, top_k: int) -> list[tuple[float, RetrievalHit]]:
        """消息命中实体卡（名称/别名，或向量相似）→ 返回卡内容作为检索命中。"""
        if self.conn is None:
            return []
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(self.conn)
        hits: list[tuple[float, RetrievalHit]] = []
        seen: set[str] = set()
        # 名称命中（强信号）
        for card in svc.match_cards(query):
            seen.add(card.id)
            hits.append(
                (
                    1.0,
                    RetrievalHit(
                        doc_id=card.id,
                        topic_id=None,
                        title=card.name,
                        preview=card.summary or "",
                        score=1.0,
                        sources=("entity_card",),
                        token_estimate=0,
                        created_at=None,
                    ),
                )
            )
        # 向量命中（增强；无向量/embedding 时跳过）
        recall = self.selector.recall
        if recall is not None and hasattr(recall, "entity_card_search") and recall.available():
            for card_id, score in recall.entity_card_search(query, top_k=top_k):
                if card_id in seen:
                    continue
                card = svc.get(card_id)
                if card is None:
                    continue
                seen.add(card_id)
                hits.append(
                    (
                        score,
                        RetrievalHit(
                            doc_id=card.id,
                            topic_id=None,
                            title=card.name,
                            preview=card.summary or "",
                            score=score,
                            sources=("entity_card",),
                            token_estimate=0,
                            created_at=None,
                        ),
                    )
                )
        return hits[:top_k]

    # -- internals --------------------------------------------------------

    def _created_at(self, doc_id: str) -> str | None:
        return self.selector.created_at(doc_id)

    def _preview(self, doc_id: str, title: str | None) -> str:
        # inject summary content (index doc text) instead of just the title
        text = self.selector.text_of(doc_id)
        if text and text.strip():
            return text
        return title or doc_id

    def _age_days(self, iso: str) -> float:
        try:
            created = datetime.fromisoformat(iso)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
        except ValueError:
            return float("inf")
