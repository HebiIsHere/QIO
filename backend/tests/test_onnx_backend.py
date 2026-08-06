from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.selector.base import IndexedDoc
from agent.selector.onnx import OnnxEmbeddingBackend

def _resolve_model_dir() -> Path | None:
    root = Path(os.environ.get("QIO_MODELS_DIR", ""))
    if not root:
        return None
    for candidate in (root, root / "bge-small-zh-v1.5"):
        if (candidate / "model_quantized.onnx").exists():
            return candidate
    return None


MODEL_DIR = _resolve_model_dir()


def test_backend_unavailable_without_model(db_conn, tmp_path: Path):
    backend = OnnxEmbeddingBackend(db_conn, model_dir=tmp_path / "missing")
    assert backend.available() is False
    assert backend.search("饮食", 5) == []
    backend.index([])  # 不抛异常


def test_index_and_search_persist_vectors(db_conn, tmp_path: Path):
    backend = OnnxEmbeddingBackend(db_conn, model_dir=tmp_path / "missing")
    backend._session = None  # force unavailable path for pure persistence test
    docs = [IndexedDoc(doc_id="d1", text="x", topic_id="t1")]
    # 无模型时 index 不落向量，不抛异常
    backend.index(docs)
    assert backend._vectors == {}
    assert backend.search("x", 3) == []


@pytest.mark.skipif(MODEL_DIR is None, reason="QIO_MODELS_DIR with bge-small-zh model required")
def test_real_embedding_similarity_ranking(db_conn):
    backend = OnnxEmbeddingBackend(db_conn, model_dir=MODEL_DIR)
    assert backend.available()
    docs = [
        IndexedDoc(doc_id="diet", text="用户偏好清淡饮食，不喜欢吃辣", topic_id="t1"),
        IndexedDoc(doc_id="sql", text="SQLite 数据库 schema 设计与迁移", topic_id="t2"),
    ]
    backend.index(docs)
    hits = backend.search("用户最近饮食口味怎么样", top_k=2)
    assert hits, "expected hits"
    ranked = {h.doc_id: h.score for h in hits}
    assert ranked["diet"] > ranked["sql"], f"expected diet > sql, got {ranked}"
    # 话题向量读写
    backend.update_topic_vector("t1", "用户偏好清淡饮食")
    vec = backend.topic_vector("t1")
    assert vec is not None and vec.shape == (512,)
    assert backend.topic_vector("t_none") is None
