"""ONNX embedding recall backend (bge-small-zh-v1.5, CPU).

Implements the pluggable RecallBackend: at startup self-check, if the
model files are present and onnxruntime/tokenizers importable, vector
recall replaces BM25. Vectors are persisted in the `embeddings` table
(doc_type memory_index / topic) so re-indexing skips already-embedded
documents. Falls back gracefully (available()=False) without the model.

**模型与精度由清单决定**：模型目录里可以放一份 `model_manifest.json`：

    {
      "name": "bge-small-zh-v1.5",
      "dims": 512,
      "default": "model.onnx",
      "files": [
        {"file": "model.onnx",           "precision": "fp32", "bytes": 94851877, "sha256": "…"},
        {"file": "model_quantized.onnx", "precision": "int8", "bytes": 24010842, "sha256": "…"}
      ]
    }

没有清单时按「先 fp32、再 int8」的顺序找。

**每个向量都带身份**（`onnx:<模型>:<精度>:<哈希前 12 位 或 字节数>`）：
读缓存时必须身份一致才复用。否则换了档位（fp32 ↔ int8）之后，两个模型空间的
向量会被混着比较，结果不可信而且从界面上看不出来。`selector/remote.py`
本来就有这条判断，这里补上同样的一条。
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
# 清单缺省时的查找顺序：内置默认是 fp32；没有它再退回量化版
MODEL_FILENAMES = ("model.onnx", "model_quantized.onnx")
TOKENIZER_FILENAME = "tokenizer.json"
MANIFEST_FILENAME = "model_manifest.json"


def load_model_manifest(model_dir: Path) -> dict | None:
    """读取模型清单；不存在或坏掉就返回 None（调用方退回默认查找顺序）。"""
    path = Path(model_dir) / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("onnx embedding backend: 模型清单无法解析，按默认顺序查找：%s", path)
        return None
    return payload if isinstance(payload, dict) else None


def _guess_precision(filename: str) -> str:
    if "quantized" in filename or "int8" in filename:
        return "int8"
    if "fp16" in filename:
        return "fp16"
    return "fp32"


def resolve_model_file(model_dir: Path, manifest: dict | None) -> tuple[Path | None, str]:
    """决定这次加载哪个模型文件，并给出**向量身份**。

    身份里带精度与（清单提供的）哈希：换档位 = 换身份，旧向量不会被当成有效缓存。
    """
    model_dir = Path(model_dir)
    entries: list[dict] = []
    if manifest and isinstance(manifest.get("files"), list):
        entries = [e for e in manifest["files"] if isinstance(e, dict) and e.get("file")]
    if manifest and manifest.get("default"):
        default = str(manifest["default"])
        entries.sort(key=lambda e: 0 if str(e.get("file")) == default else 1)
    if not entries:
        entries = [{"file": name} for name in MODEL_FILENAMES]

    model_name = str((manifest or {}).get("name") or MODEL_NAME)
    for entry in entries:
        path = model_dir / str(entry["file"])
        if not path.exists():
            continue
        precision = str(entry.get("precision") or _guess_precision(path.name))
        digest = str(entry.get("sha256") or "")[:12]
        return path, f"onnx:{model_name}:{precision}:{digest or path.stat().st_size}"
    return None, ""


def cosine_similarity(query: np.ndarray, vectors: np.ndarray) -> np.ndarray:
    """Cosine similarity between a (d,) query and (n, d) vectors."""
    norms = np.linalg.norm(vectors, axis=1)
    denom = norms * np.linalg.norm(query)
    denom = np.where(denom == 0, 1e-9, denom)
    return (vectors @ query) / denom


class OnnxEmbeddingBackend(RecallBackend):
    name = "onnx"
    supports_incremental = True
    # 类级默认值：手工构造（测试里用 __new__ 建实例）时也不会因为缺属性而崩
    model_identity = ""
    model_file = ""
    _inputs: list[str] = []

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
        # 向量身份：换模型或换精度都会换它，用于缓存校验（见模块头）
        self.model_identity = ""
        self.model_file = ""
        self.dims = MODEL_DIMS
        self._session: Any | None = None
        self._tokenizer: Any | None = None
        self._vectors: dict[str, np.ndarray] = {}
        # 检索矩阵缓存：向量集合变化才重建，避免每次 search 都 np.stack 全量重组
        self._matrix: np.ndarray | None = None
        self._matrix_keys: list[str] = []
        self._matrix_dirty = True
        self._load()

    # -- availability -----------------------------------------------------

    def _load(self) -> None:
        if self.model_dir is None:
            return
        manifest = load_model_manifest(self.model_dir)
        onnx_path, identity = resolve_model_file(self.model_dir, manifest)
        tokenizer_path = self.model_dir / TOKENIZER_FILENAME
        if onnx_path is None or not tokenizer_path.exists():
            logger.info(
                "onnx embedding backend: model files missing; falling back（找过 %s）",
                ", ".join(MODEL_FILENAMES),
            )
            return
        if manifest and manifest.get("name"):
            self.model_name = str(manifest["name"])
        if manifest and manifest.get("dims"):
            try:
                self.dims = int(manifest["dims"])
            except (TypeError, ValueError):
                pass
        if manifest and manifest.get("max_len"):
            try:
                self.max_len = int(manifest["max_len"])
            except (TypeError, ValueError):
                pass
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
            self.model_identity = identity
            self.model_file = onnx_path.name
            logger.info(
                "onnx embedding backend: loaded %s（%s，%d dims，identity=%s）",
                self.model_name,
                onnx_path.name,
                self.dims,
                identity,
            )
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
        # 只认「身份 + 维度」都一致的缓存：换模型 / 换精度之后，
        # 旧向量必须重新编码，不能混着比（remote.py 早就是这么做的）。
        rows = self.conn.execute(
            "SELECT ref_id, dims, vector FROM embeddings "
            "WHERE doc_type = ? AND model = ? AND dims = ? "
            "AND ref_id IN (%s)" % ",".join("?" * len(ref_ids)),
            [doc_type, self.model_identity, int(self.dims), *ref_ids],
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
                (new_id("emb"), doc_type, ref_id, self.model_identity or self.model_name, int(self.dims),
                 vec.astype(np.float32).tobytes(), now, now),
            )

    # -- RecallBackend ----------------------------------------------------

    def index(self, docs: list[IndexedDoc]) -> None:
        self._vectors = {}
        self._matrix_dirty = True
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
        self._matrix_dirty = True

    # -- 增量更新 ---------------------------------------------------------

    def upsert(self, doc: IndexedDoc) -> None:
        """只嵌入新增 / 变化的那一条，不再重建全部向量索引。"""
        if not self.available():
            return
        vectors = self.embed_texts([doc.text])
        if vectors is None:
            return
        self._save("memory_index", [doc.doc_id], vectors)
        self._vectors[doc.doc_id] = vectors[0]
        self._matrix_dirty = True

    def remove(self, doc_id: str) -> None:
        if self._vectors.pop(doc_id, None) is not None:
            self._matrix_dirty = True

    def _search_matrix(self) -> tuple[np.ndarray | None, list[str]]:
        """(矩阵, doc_id 顺序)。向量集合没变就直接复用上一份矩阵。"""
        if self._matrix_dirty or self._matrix is None:
            self._matrix_keys = list(self._vectors.keys())
            self._matrix = (
                np.stack([self._vectors[k] for k in self._matrix_keys]).astype(np.float32)
                if self._matrix_keys
                else None
            )
            self._matrix_dirty = False
        return self._matrix, self._matrix_keys

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        if not self.available() or not self._vectors:
            return []
        query_vec = self.embed_texts([query])
        if query_vec is None:
            return []
        matrix, keys = self._search_matrix()
        if matrix is None:
            return []
        scores = cosine_similarity(query_vec[0], matrix)
        order = np.argsort(-scores)
        hits: list[ScoredDoc] = []
        for i in order[:top_k]:
            doc_id = keys[i]
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
        # 同样按身份过滤：换档位之后，旧身份下的实体卡向量不能参与比较
        rows = self.conn.execute(
            "SELECT ref_id, dims, vector FROM embeddings "
            "WHERE doc_type = 'entity_card' AND model = ? AND dims = ?",
            (self.model_identity, int(self.dims)),
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
