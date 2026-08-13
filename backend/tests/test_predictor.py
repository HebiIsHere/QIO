from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np

from agent.graph.topics import TopicService
from agent.services.predict import (
    AUX_TOP_COUNT,
    NEW_TOPIC_THRESHOLD,
    SWITCH_DELTA,
    TopicPredictor,
)


class FakeEmbedding:
    """Deterministic orthogonal token basis: similarity follows token overlap."""

    def __init__(self) -> None:
        self.saved: dict[str, np.ndarray] = {}

    def available(self) -> bool:
        return True

    def _basis_vec(self, token: str) -> np.ndarray:
        idx = abs(hash(token)) % 4096
        v = np.zeros(4096, dtype=np.float32)
        v[idx] = 1.0
        return v

    def embed_texts(self, texts: list[str]) -> np.ndarray | None:
        from agent.selector.tokenize import tokenize

        out = []
        for text in texts:
            tokens = set(tokenize(text))
            vec = np.zeros(4096, dtype=np.float32)
            for t in tokens:
                vec += self._basis_vec(t)
            norm = np.linalg.norm(vec)
            out.append(vec / (norm + 1e-9))
        return np.stack(out)

    def topic_vector(self, topic_id: str) -> np.ndarray | None:
        return self.saved.get(topic_id)

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        vec = self.embed_texts([text])
        if vec is not None:
            self.saved[topic_id] = vec[0]


def _topics(db_conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for tid, name in (("t_diet", "饮食偏好"), ("t_fit", "健身计划"), ("t_sql", "数据库设计")):
        db_conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'topic', ?, '{}', ?, ?)",
            (tid, name, now, now),
        )
    # 已关闭片段 + 索引：提供指纹关键词（规则兜底依赖）
    for fid, tid, summary, keywords in (
        ("frag_diet", "t_diet", "用户偏好清淡饮食", '["饮食", "清淡", "吃辣"]'),
        ("frag_fit", "t_fit", "每周三次健身跑步", '["健身", "跑步", "力量"]'),
        ("frag_sql", "t_sql", "SQLite 表结构设计", '["sqlite", "schema"]'),
    ):
        db_conn.execute(
            "INSERT OR IGNORE INTO fragments (id, topic_id, summary, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (fid, tid, summary, now, now),
        )
        db_conn.execute(
            "INSERT OR IGNORE INTO memory_index (id, fragment_id, topic_id, entity_ids, "
            "keywords, title, token_estimate, created_at) VALUES (?, ?, ?, '[]', ?, ?, 10, ?)",
            (f"idx_{fid}", fid, tid, keywords, summary, now),
        )


def _make_predictor(db_conn: sqlite3.Connection, embedding, **kwargs) -> TopicPredictor:
    return TopicPredictor(db_conn, embedding, TopicService(db_conn), **kwargs)


def test_main_topic_ranking_and_aux(db_conn: sqlite3.Connection):
    _topics(db_conn)
    emb = FakeEmbedding()
    pred = _make_predictor(db_conn, emb, new_topic_threshold=0.45).predict(
        "饮食 清淡 吃辣 健身 跑步", current_topic_id="t_diet"
    )
    assert pred.main_topic_id == "t_diet"
    assert pred.is_new_topic_candidate is False
    assert pred.suggested_switch is False
    assert "t_fit" in pred.aux_topic_ids
    assert len(pred.aux_topic_ids) <= AUX_TOP_COUNT
    assert pred.backend_used == "onnx"


def test_suggested_switch_requires_delta(db_conn: sqlite3.Connection):
    _topics(db_conn)
    emb = FakeEmbedding()
    pred = _make_predictor(db_conn, emb, new_topic_threshold=0.45).predict("健身 跑步 力量", current_topic_id="t_fit")
    assert pred.main_topic_id == "t_fit"
    assert pred.suggested_switch is False
    pred2 = _make_predictor(db_conn, emb, new_topic_threshold=0.45).predict("饮食 清淡 吃辣", current_topic_id="t_fit")
    assert pred2.main_topic_id == "t_diet"
    assert pred2.suggested_switch is True
    assert pred2.scores["t_diet"] - pred2.scores["t_fit"] >= SWITCH_DELTA


def test_new_topic_candidate_when_below_threshold(db_conn: sqlite3.Connection):
    _topics(db_conn)
    emb = FakeEmbedding()
    pred = _make_predictor(db_conn, emb).predict("量子 物理 弦理论", current_topic_id="t_diet")
    assert pred.is_new_topic_candidate is True
    assert pred.main_topic_id is None


def test_cold_start_generates_topic_vectors(db_conn: sqlite3.Connection):
    _topics(db_conn)
    emb = FakeEmbedding()
    assert emb.topic_vector("t_diet") is None
    pred = _make_predictor(db_conn, emb, new_topic_threshold=0.45).predict("饮食 清淡", current_topic_id="t_diet")
    assert emb.topic_vector("t_diet") is not None
    assert pred.main_topic_id == "t_diet"


def test_default_threshold_0_7_marks_moderate_similarity_as_new_topic(db_conn: sqlite3.Connection):
    """默认阈值 0.7：中度相关（~0.56-0.6）也判为新话题候选，交给主模型决定。"""
    _topics(db_conn)
    emb = FakeEmbedding()
    pred = _make_predictor(db_conn, emb).predict(
        "饮食 清淡 吃辣 健身 跑步", current_topic_id="t_diet"
    )
    assert pred.is_new_topic_candidate is True
    assert pred.main_topic_id is None


def test_rules_fallback_without_embedding(db_conn: sqlite3.Connection):
    _topics(db_conn)
    pred = _make_predictor(db_conn, None).predict("饮食 清淡 吃辣", current_topic_id="t_fit")
    assert pred.backend_used == "rules"
    assert pred.main_topic_id == "t_diet"
    assert pred.suggested_switch is True
    pred2 = _make_predictor(db_conn, None).predict("量子 物理 弦理论", current_topic_id="t_diet")
    assert pred2.is_new_topic_candidate is True
    assert pred2.main_topic_id is None
