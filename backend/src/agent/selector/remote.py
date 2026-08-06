"""Remote embedding backend: OpenAI-compatible /v1/embeddings.

Uses a BYOK key tagged `embedding`. Vectors persist in the `embeddings`
table (model-tagged); consecutive failures degrade the backend so the
selector falls back (BM25 / rule layer).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import numpy as np

from agent.selector.base import IndexedDoc, RecallBackend, ScoredDoc
from agent.selector.onnx import cosine_similarity

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_MODEL = "text-embedding-3-small"
DEFAULT_TIMEOUT = 15.0


class RemoteEmbeddingBackend(RecallBackend):
    name = "remote"

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        api_key: str,
        base_url: str,
        model: str,
        http_client=None,
        timeout: float = DEFAULT_TIMEOUT,
        failure_threshold: int = 3,
    ) -> None:
        self.conn = conn
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.timeout = timeout
        self.failure_threshold = failure_threshold
        self._client = http_client
        self._failures = 0
        self._degraded = False
        self._dims: int | None = None
        self._vectors: dict[str, np.ndarray] = {}

    # -- availability -----------------------------------------------------

    def available(self) -> bool:
        return bool(self.api_key and self.base_url) and not self._degraded

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._degraded = True
            logger.warning("remote embedding degraded after %d failures", self._failures)

    # -- embedding --------------------------------------------------------

    def embed_texts(self, texts: list[str]) -> np.ndarray | None:
        """POST /embeddings; returns (n, dims) L2-normalized vectors or None."""
        if not self.available() or not texts:
            return None
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        try:
            resp = self._client.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model_name, "input": texts},
                timeout=self.timeout,
            )
            payload = resp.json()
            data = sorted(payload.get("data", []), key=lambda d: d.get("index", 0))
            matrix = np.stack([np.asarray(d["embedding"], dtype=np.float32) for d in data])
            self._dims = matrix.shape[1]
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            matrix = matrix / np.maximum(norms, 1e-9)
            self._failures = 0
            return matrix
        except Exception as exc:  # noqa: BLE001 - degrade instead of crashing
            logger.warning("remote embedding call failed: %s", exc)
            self._record_failure()
            return None

    # -- persistence (mirrors OnnxEmbeddingBackend) ------------------------

    def _load_persisted(self, doc_type: str, ref_ids: list[str]) -> dict[str, np.ndarray]:
        if not ref_ids:
            return {}
        rows = self.conn.execute(
            "SELECT ref_id, dims, vector, model FROM embeddings WHERE doc_type = ? "
            "AND ref_id IN (%s)" % ",".join("?" * len(ref_ids)),
            [doc_type, *ref_ids],
        ).fetchall()
        out: dict[str, np.ndarray] = {}
        for row in rows:
            if row["model"] != self.model_name:
                continue  # stale vectors from another model
            arr = np.frombuffer(row["vector"], dtype=np.float32)
            if arr.size == row["dims"]:
                out[row["ref_id"]] = arr.astype(np.float32)
        return out

    def _save(self, doc_type: str, ref_ids: list[str], vectors: np.ndarray) -> None:
        from agent.memory.fragment import new_id

        now = datetime.now(timezone.utc).isoformat()
        dims = int(vectors.shape[1])
        for ref_id, vec in zip(ref_ids, vectors):
            self.conn.execute(
                "INSERT INTO embeddings (id, doc_type, ref_id, model, dims, vector, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_type, ref_id) DO UPDATE SET vector = excluded.vector, "
                "model = excluded.model, dims = excluded.dims, updated_at = excluded.updated_at",
                (new_id("emb"), doc_type, ref_id, self.model_name, dims,
                 vec.astype(np.float32).tobytes(), now, now),
            )
        self.conn.commit()

    # -- RecallBackend ----------------------------------------------------

    def index(self, docs: list[IndexedDoc]) -> None:
        self._vectors = {}
        if not self.available():
            return
        ref_ids = [d.doc_id for d in docs]
        persisted = self._load_persisted("memory_index", ref_ids)
        missing = [d for d in docs if d.doc_id not in persisted]
        if missing:
            vectors = self.embed_texts([d.text for d in missing])
            if vectors is None:
                return
            self._save("memory_index", [d.doc_id for d in missing], vectors)
            for doc, vec in zip(missing, vectors):
                persisted[doc.doc_id] = vec
        for doc in docs:
            if doc.doc_id in persisted:
                self._vectors[doc.doc_id] = persisted[doc.doc_id]

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        if not self.available() or not self._vectors:
            return []
        query_vec = self.embed_texts([query])
        if query_vec is None:
            return []
        matrix = np.stack(list(self._vectors.values())).astype(np.float32)
        scores = cosine_similarity(query_vec[0], matrix)
        order = np.argsort(-scores)
        hits: list[ScoredDoc] = []
        keys = list(self._vectors.keys())
        for i in order[:top_k]:
            hits.append(ScoredDoc(doc_id=keys[i], score=float(scores[i]), source=self.name))
        return hits

    # -- topic vectors ----------------------------------------------------

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        if not self.available():
            return
        vectors = self.embed_texts([text])
        if vectors is None:
            return
        self._save("topic", [topic_id], vectors)

    def topic_vector(self, topic_id: str) -> np.ndarray | None:
        persisted = self._load_persisted("topic", [topic_id])
        return persisted.get(topic_id)
