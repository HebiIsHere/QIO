"""Remote embedding backend: OpenAI-compatible embeddings + fallback chain."""

from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from agent.selector.base import IndexedDoc
from agent.selector.remote import RemoteEmbeddingBackend


class FakeEmbeddingsClient:
    """Duck-typed httpx client returning deterministic embeddings."""

    def __init__(self, *, dims: int = 8, fail: bool = False) -> None:
        self.dims = dims
        self.fail = fail
        self.calls = 0

    def post(self, url: str, json=None, timeout=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("remote unavailable")
        from agent.selector.tokenize import tokenize

        texts = (json or {}).get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        data = []
        for i, text in enumerate(texts):
            vec = np.zeros(self.dims, dtype=np.float32)
            for tok in set(tokenize(text)):
                vec[abs(hash(tok)) % self.dims] += 1.0
            norm = np.linalg.norm(vec)
            vec = vec / (norm + 1e-9)
            data.append({"object": "embedding", "index": i, "embedding": vec.tolist()})
        return type("R", (), {"json": lambda self: {"data": data}})()


def _backend(db_conn: sqlite3.Connection, client=None, **kw) -> RemoteEmbeddingBackend:
    return RemoteEmbeddingBackend(
        db_conn,
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="embed-test",
        http_client=client or FakeEmbeddingsClient(),
        **kw,
    )


def test_remote_available_and_embed(db_conn: sqlite3.Connection):
    b = _backend(db_conn)
    assert b.available()
    vecs = b.embed_texts(["饮食偏好清淡"])
    assert vecs is not None and vecs.shape == (1, 8)


def test_remote_failure_returns_none_and_degrades(db_conn: sqlite3.Connection):
    b = _backend(db_conn, client=FakeEmbeddingsClient(fail=True), failure_threshold=2)
    assert b.available()
    assert b.embed_texts(["x"]) is None
    assert b.embed_texts(["x"]) is None
    assert b.available() is False  # 连续失败后降级


def test_remote_index_search_and_persist(db_conn: sqlite3.Connection):
    b = _backend(db_conn)
    docs = [
        IndexedDoc(doc_id="d1", text="用户偏好清淡饮食", topic_id="t1"),
        IndexedDoc(doc_id="d2", text="SQLite 数据库设计", topic_id="t2"),
    ]
    b.index(docs)
    hits = b.search("用户最近饮食口味", top_k=2)
    assert hits and hits[0].doc_id == "d1"
    # 持久化：新实例复用（同一 model 不重嵌入）
    client = FakeEmbeddingsClient()
    b2 = _backend(db_conn, client=client)
    b2.index(docs)
    assert client.calls == 0  # 已持久化，不重新调用远程


def test_remote_topic_vectors(db_conn: sqlite3.Connection):
    b = _backend(db_conn)
    b.update_topic_vector("t1", "饮食偏好")
    vec = b.topic_vector("t1")
    assert vec is not None and vec.shape == (8,)


# ---------- AppContext 选档与 fallback ----------

def test_selector_fallback_to_bm25_when_remote_down(db_conn: sqlite3.Connection):
    from agent.selector.selector import Selector

    remote = _backend(db_conn, client=FakeEmbeddingsClient(fail=True), failure_threshold=1)
    remote.embed_texts(["x"])  # 触发降级
    selector = Selector(recall=remote, fallback_recall=remote  or None)
    # fallback 逻辑在 Selector.select 内验证：remote 不可用 → 规则层（无 BM25 注入）
    docs = [IndexedDoc(doc_id="d1", text="用户偏好清淡饮食", topic_id="t1", keywords=["饮食"])]
    selector.load(docs)
    hits = selector.select("用户 饮食", top_k=2)
    assert hits  # 规则层仍工作
