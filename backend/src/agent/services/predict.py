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

# 阈值集中管理（语义/默认来源/对应 eval 见 agent/services/params.py 与
# agent/eval/topic_eval.py）。此处仅做别名，禁止在此新增散落的 magic number。
from agent.services.params import TOPIC as _TOPIC

NEW_TOPIC_THRESHOLD = _TOPIC.new_topic_threshold
AUX_TOPIC_THRESHOLD = _TOPIC.aux_topic_threshold
RULES_NEW_TOPIC_THRESHOLD = _TOPIC.rules_new_topic_threshold
RULES_AUX_TOPIC_THRESHOLD = _TOPIC.rules_aux_topic_threshold
SWITCH_DELTA = _TOPIC.switch_delta
RULES_SWITCH_DELTA = _TOPIC.rules_switch_delta
AUX_TOP_COUNT = _TOPIC.aux_top_count


@dataclass(frozen=True)
class TopicPrediction:
    main_topic_id: str | None
    aux_topic_ids: list[str] = field(default_factory=list)
    is_new_topic_candidate: bool = False
    suggested_switch: bool = False
    scores: dict[str, float] = field(default_factory=dict)
    backend_used: str = "rules"


@dataclass(frozen=True)
class PredictionPlan:
    """预判的「输入」部分：读盘得到的指纹 + 这次要嵌入的文本（纯数据）。

    把预判拆成三段（契约 WS3 §3：先拆「输入读取 / 纯计算 / 结果提交」再迁执行
    位置）：

    * `plan_prediction` —— 输入读取（只读数据库，不推理），在事件循环一侧跑；
    * `compute_embedding` —— **纯计算**（ONNX 推理），可以搬到工作线程；
    * `finish_prediction` —— 结果提交（排序 + 冷启动向量写回），回到事件循环一侧。
    """

    fingerprints: list
    current_topic_id: str | None
    backend: str
    query_text: str
    # 还没有落盘向量、这次要补算的话题：topic_id → 要嵌入的指纹文本
    missing_topic_texts: dict[str, str] = field(default_factory=dict)
    # 已经落盘、这次直接复用的话题向量：topic_id → 向量
    cached_vectors: dict = field(default_factory=dict)


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
        rules_switch_delta: float = RULES_SWITCH_DELTA,
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
        self.rules_switch_delta = rules_switch_delta

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
        """同步预判（旧入口，语义不变）：三段拆分的顺序组合。"""
        plan = self.plan_prediction(query, current_topic_id=current_topic_id)
        if plan is None:
            return TopicPrediction(
                main_topic_id=None,
                is_new_topic_candidate=True,
                backend_used=self.backend_name,
            )
        embedded = (
            self.compute_embedding(plan) if plan.backend == "onnx" else None
        )
        return self.finish_prediction(plan, embedded)

    # -- 三段拆分（异步调用方用它把纯计算搬进工作线程）------------------

    def plan_prediction(
        self, query: str, *, current_topic_id: str | None = None
    ) -> PredictionPlan | None:
        """输入读取：读指纹与已有向量，决定这次要嵌入哪些文本（不推理）。

        返回 None = 一个话题都没有（调用方按「新话题候选」处理，与旧行为一致）。
        """
        fingerprints = self.topics.list_with_fingerprints()
        if not fingerprints:
            return None
        if self.embedding is not None and self.embedding.available():
            ids = [fp.topic_id for fp in fingerprints]
            # 一次批量读回（每个话题各查一次在话题多时是明显的浪费）
            if hasattr(self.embedding, "topic_vectors"):
                cached = self.embedding.topic_vectors(ids)
            else:  # 兼容替身/旧实现
                cached = {
                    tid: vec
                    for tid in ids
                    if (vec := self.embedding.topic_vector(tid)) is not None
                }
            missing = {
                fp.topic_id: self._fingerprint_text(fp)
                for fp in fingerprints
                if fp.topic_id not in cached
            }
            return PredictionPlan(
                fingerprints=list(fingerprints),
                current_topic_id=current_topic_id,
                backend="onnx",
                query_text=query,
                missing_topic_texts=missing,
                cached_vectors=cached,
            )
        return PredictionPlan(
            fingerprints=list(fingerprints),
            current_topic_id=current_topic_id,
            backend="rules",
            query_text=query,
        )

    def compute_embedding(self, plan: PredictionPlan):
        """纯计算：查询 + 需要补算的话题指纹一次算完（可以在工作线程里跑）。

        只碰嵌入后端，不碰数据库、不写缓存 —— 工作线程里做的只有推理。
        """
        if plan.backend != "onnx" or self.embedding is None:
            return None
        texts = [plan.query_text] + list(plan.missing_topic_texts.values())
        return self.embedding.embed_texts(texts)

    def finish_prediction(
        self, plan: PredictionPlan, embedded, *, commit: bool = True
    ) -> TopicPrediction:
        """结果提交：排序 + 冷启动向量写回（回到事件循环一侧）。

        `commit=False`（这一轮已经被取消）时**只算不写**：不落盘话题向量。
        过时的计算结果不许提交 —— 缓存写回也算提交。
        """
        if plan.backend != "onnx":
            return self._predict_rules(plan.query_text, plan.fingerprints, plan.current_topic_id)
        expected = 1 + len(plan.missing_topic_texts)
        if embedded is None or len(embedded) != expected:
            # 与旧行为一致：拿不到查询向量就退回规则层
            return self._predict_rules(plan.query_text, plan.fingerprints, plan.current_topic_id)
        query_vec = embedded[0]
        vectors = dict(plan.cached_vectors)
        missing_ids = list(plan.missing_topic_texts)
        for offset, topic_id in enumerate(missing_ids):
            vectors[topic_id] = embedded[1 + offset]
        if commit and missing_ids:
            # 冷启动补算的向量写回（一次批量写，不再每条一次推理 + 一次写）。
            # 替身 / 老后端没有批量接口时退回逐条 `update_topic_vector`（旧行为）。
            if hasattr(self.embedding, "save_topic_vectors"):
                self.embedding.save_topic_vectors(
                    missing_ids, embedded[1 : 1 + len(missing_ids)]
                )
            else:
                for topic_id in missing_ids:
                    self.embedding.update_topic_vector(
                        topic_id, plan.missing_topic_texts[topic_id]
                    )
        scores: dict[str, float] = {}
        for fp in plan.fingerprints:
            vec = vectors.get(fp.topic_id)
            if vec is None:
                continue
            denom = (float(np_linalg_norm(vec)) * float(np_linalg_norm(query_vec))) or 1e-9
            scores[fp.topic_id] = float(np_dot(vec, query_vec) / denom)
        return self._rank(scores, plan.current_topic_id, backend="onnx")

    def refresh_topic_vector(self, topic_id: str) -> None:
        """Re-embed a topic's fingerprint (called after a chunk close)."""
        if self.embedding is None or not self.embedding.available():
            return
        fp = self.topics.fingerprint(topic_id)
        self.embedding.update_topic_vector(topic_id, self._fingerprint_text(fp))

    def plan_topic_refresh(self, topic_id: str) -> tuple[str, str] | None:
        """话题向量刷新的「输入」一段：(topic_id, 要嵌入的文本)。"""
        if self.embedding is None or not self.embedding.available():
            return None
        fp = self.topics.fingerprint(topic_id)
        return topic_id, self._fingerprint_text(fp)

    def compute_topic_refresh(self, plan: tuple[str, str]):
        """话题向量刷新的「纯计算」一段：只嵌入文本（可在工作线程里跑）。"""
        if self.embedding is None:
            return None
        _topic_id, text = plan
        return self.embedding.embed_texts([text])

    def commit_topic_refresh(self, plan: tuple[str, str], vector) -> bool:
        """话题向量刷新的「结果提交」一段：把算好的向量写回（不重新推理）。"""
        topic_id, text = plan
        if vector is None or self.embedding is None:
            return False
        if hasattr(self.embedding, "save_topic_vector"):
            self.embedding.save_topic_vector(topic_id, vector[0])
            return True
        self.embedding.update_topic_vector(topic_id, text)
        return True

    @property
    def backend_name(self) -> str:
        if self.embedding is not None and self.embedding.available():
            return "onnx"
        return "rules"

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
        # 切换余量按后端取：onnx 余弦与兜底重合比例不同量纲（见 params.TopicPolicy）
        switch_delta = self.switch_delta if backend == "onnx" else self.rules_switch_delta
        suggested = False
        if main_id is not None and current_topic_id is not None and main_id != current_topic_id:
            current_score = scores.get(current_topic_id, 0.0)
            suggested = main_score - current_score >= switch_delta
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
