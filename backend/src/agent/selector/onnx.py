"""ONNX embedding recall backend (bge-small-zh-v1.5, CPU).

Implements the pluggable RecallBackend: at startup self-check, if the
model files are present and onnxruntime/tokenizers importable, vector
recall replaces BM25. Vectors are persisted in the `embeddings` table
(doc_type memory_index / topic) so re-indexing skips already-embedded
documents. Falls back gracefully (available()=False) without the model.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from agent.selector.base import IndexedDoc, RecallBackend, ScoredDoc

logger = logging.getLogger(__name__)

MODEL_NAME = "bge-small-zh-v1.5"
MODEL_DIMS = 512
MODEL_MAX_LEN = 512
MODEL_FILENAME = "model_quantized.onnx"
TOKENIZER_FILENAME = "tokenizer.json"


def cosine_similarity(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    """Cosine similarity between a (d,) query and (n, d) vectors."""
    norms = np.linalg.norm(vectors, axis=1)
    denom = norms * np.linalg.norm(query)
    denom = np.where(denom == 0, 1e-9, denom)
    return (vectors @ query) / denom


class OnnxEmbeddingBackend(RecallBackend):
    name = "onnx"

    def __init__(
        self,
        conn: sqlite3.Connection,
        model_dir: str | Path | None = None,
        *,
        max_len: int = MODEL_MAX_LEN,
        threads: int = 8,
    ) -> None:
        self.conn = conn
        self.model_dir = Path(model_dir) if model_dir else None
        self.max_len = max_len
        self.threads = threads
        self.model_name = MODEL_NAME
        self.dims = MODEL_DIMS
        self._session: Any | None = None
        self._tokenizer: Any | None = None
        self._vectors: dict[str, np.ndarray] = {}
        self._load()

    # -- availability -----------------------------------------------------

    def _load(self) -> None:
        if self.model_dir is None:
            return
        onnx_path = self.model_dir / MODEL_FILENAME
        tokenizer_path = self.model_dir / TOKENIZER_FILENAME
        if not onnx_path.exists() or not tokenizer_path.exists():
            logger.info("onnx embedding backend: model files missing; falling back")
            return
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError:
            logger.warning("onnx embedding backend: onnxruntime/tokenizers unavailable")
            return
        try:
            so = ort.SessionOptions()
            so.intra_op_num_threads = self.threads
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                str(onnx_path), sess_options=so, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._inputs = [i.name for i in self._session.get_inputs()]
            logger.info("onnx embedding backend: loaded %s (%d dims)", MODEL_NAME, self.dims)
        except Exception as exc:  # pragma: no cover - environment dependent
            logger.warning("onnx embedding backend load failed: %s", exc)
            self._session = None
            self._tokenizer = None

    def available(self) -> bool:
        return self._session is not None and self._tokenizer is not None

    # -- embedding --------------------------------------------------------

    def embed_texts(self, texts: list[str]) -> np.ndarray | None:
        """Return (n, dims) L2-normalized vectors, or None when unavailable."""
        if not self.available() or not texts:
            return None
        vectors = []
        for text in texts:
            enc = self._tokenizer.encode(text[:4000])
            ids = np.array([enc.ids[: self.max_len]], dtype=np.int64)
            mask = np.array([enc.attention_mask[: self.max_len]], dtype=np.int64)
            ttype = np.zeros_like(ids)
            feed = {self._inputs[0]: ids, self._inputs[1]: mask, self._inputs[2]: ttype}
            hidden = self._session.run(None, feed)[0]  # (1, L, dims)
            mask_f = mask.astype(np.float32)[:, :, None]
            pooled = (hidden * mask_f).sum(axis=1) / np.maximum(mask_f.sum(axis=1), 1e-9)
            vectors.append(pooled[0])
        matrix = np.stack(vectors).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.maximum(norms, 1e-9)
        return matrix

    # -- persistence ------------------------------------------------------

    def _load_persisted(self, doc_type: str, ref_ids: list[str]) -> dict[str, np.ndarray]:
        if not ref_ids:
            return {}
        rows = self.conn.execute(
            "SELECT ref_id, dims, vector FROM embeddings WHERE doc_type = ? "
            "AND ref_id IN (%s)" % ",".join("?" * len(ref_ids)),
            [doc_type, *ref_ids],
        ).fetchall()
        out: dict[str, np.ndarray] = {}
        for row in rows:
            arr = np.frombuffer(row["vector"], dtype=np.float32)
            if arr.size == row["dims"]:
                out[row["ref_id"]] = arr.astype(np.float32)
        return out

    def _save(self, doc_type: str, ref_ids: list[str], vectors: np.ndarray) -> None:
        from agent.memory.fragment import new_id

        now = "2026-01-01T00:00:00+00:00"  # placeholder; replaced below
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        for ref_id, vec in zip(ref_ids, vectors):
            self.conn.execute(
                "INSERT INTO embeddings (id, doc_type, ref_id, model, dims, vector, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_type, ref_id) DO UPDATE SET vector = excluded.vector, "
                "model = excluded.model, dims = excluded.dims, updated_at = excluded.updated_at",
                (new_id("emb"), doc_type, ref_id, self.model_name, int(self.dims),
                 vec.astype(np.float32).tobytes(), now, now),
            )

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
        for i in order[:top_k]:
            doc_id = list(self._vectors.keys())[i]
            hits.append(ScoredDoc(doc_id=doc_id, score=float(scores[i]), source=self.name))
        return hits

    # -- topic vectors ----------------------------------------------------

    def save_entity_card_vector(self, card_id: str, text: str) -> None:
        """Embed an entity card (summary/name) into embeddings doc_type=entity_card."""
        if not self.available():
            return
        vectors = self.embed_texts([text])
        if vectors is None:
            return
        self._save("entity_card", [card_id], vectors)

    def entity_card_search(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        """检索与查询最相似的实体卡向量；返回 [(card_id, score)]，低于阈值过滤。"""
        if not self.available():
            return []
        rows = self.conn.execute(
            "SELECT ref_id, vector FROM embeddings WHERE doc_type = 'entity_card'"
        ).fetchall()
        if not rows:
            return []
        q = self.embed_texts([query])
        if q is None:
            return []
        ids = [r["ref_id"] for r in rows]
        matrix = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
        scores = cosine_similarity(q[0], matrix)
        order = np.argsort(-scores)
        out: list[tuple[str, float]] = []
        for i in order[:top_k]:
            s = float(scores[i])
            if s >= 0.25:
                out.append((ids[i], s))
        return out

    def update_topic_vector(self, topic_id: str, text: str) -> None:
        """Embed a topic's fingerprint text and persist it (cold-start refresh)."""
        if not self.available():
            return
        vectors = self.embed_texts([text])
        if vectors is None:
            return
        self._save("topic", [topic_id], vectors)

    def topic_vector(self, topic_id: str) -> np.ndarray | None:
        persisted = self._load_persisted("topic", [topic_id])
        return persisted.get(topic_id)

