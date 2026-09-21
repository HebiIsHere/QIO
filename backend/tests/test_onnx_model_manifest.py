"""模型清单与向量身份（阶段 4 收尾的模型线）。

两件事必须成立，否则「内置 fp32」只是一个文件名：

1. **按清单加载**：模型目录里有 `model_manifest.json` 时按它说的默认文件加载
   （内置默认是 fp32 `model.onnx`，量化版作为可选档）；
2. **向量身份过滤**：缓存里只认「身份 + 维度」都一致的向量 —— 换了档位就等于换了
   身份，旧向量必须重新编码，不能两个模型空间的向量混着比。

这个测试用假的模型文件与打桩的 session/tokenizer，不依赖真实 ONNX（CI 里没有模型）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from agent.selector.onnx import (
    load_model_manifest,
    resolve_model_file,
)
from agent.selector.onnx import OnnxEmbeddingBackend


def _write(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def test_resolve_prefers_manifest_default(tmp_path: Path):
    _write(tmp_path / "model.onnx", b"x" * 100)
    _write(tmp_path / "model_quantized.onnx", b"y" * 50)
    manifest = {
        "name": "bge-small-zh-v1.5",
        "dims": 512,
        "default": "model.onnx",
        "files": [
            {"file": "model_quantized.onnx", "precision": "int8"},
            {"file": "model.onnx", "precision": "fp32", "sha256": "abcdef1234567890"},
        ],
    }
    (tmp_path / "model_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    path, identity = resolve_model_file(tmp_path, load_model_manifest(tmp_path))

    assert path is not None and path.name == "model.onnx", "清单里的默认档优先"
    assert identity == "onnx:bge-small-zh-v1.5:fp32:abcdef123456", "身份要带精度与哈希"


def test_resolve_falls_back_to_quantized_when_fp32_missing(tmp_path: Path):
    _write(tmp_path / "model_quantized.onnx", b"y" * 50)
    path, identity = resolve_model_file(tmp_path, None)

    assert path is not None and path.name == "model_quantized.onnx"
    assert identity.endswith(":int8:50"), f"没有哈希时用字节数兜底：{identity}"


def test_resolve_returns_none_when_no_model(tmp_path: Path):
    path, identity = resolve_model_file(tmp_path, None)
    assert path is None and identity == ""


def test_broken_manifest_is_ignored(tmp_path: Path):
    _write(tmp_path / "model_quantized.onnx", b"y" * 10)
    (tmp_path / "model_manifest.json").write_text("{ not json", encoding="utf-8")

    assert load_model_manifest(tmp_path) is None
    path, _ = resolve_model_file(tmp_path, None)
    assert path is not None and path.name == "model_quantized.onnx"


class _StubBackend(OnnxEmbeddingBackend):
    """把真正的 ONNX 推理替换成确定性桩：测的是身份与缓存逻辑，不是模型。"""

    def __init__(self, conn, model_dir: Path, identity: str, dims: int = 512) -> None:
        self._stub_identity = identity
        self._stub_dims = dims
        super().__init__(conn, model_dir)

    def _load(self) -> None:  # 不加载真实模型
        self.model_identity = self._stub_identity
        self.dims = self._stub_dims
        self.model_file = "stub.onnx"
        self._session = object()
        self._tokenizer = object()
        self._inputs = ["input_ids", "attention_mask", "token_type_ids"]

    def embed_texts(self, texts):  # 确定性桩：按文本生成可区分的向量
        if not self.available() or not texts:
            return None
        out = []
        for text in texts:
            vec = np.zeros(self.dims, dtype=np.float32)
            vec[hash(text) % self.dims] = 1.0
            out.append(vec)
        return np.stack(out)


def _seed_embedding(conn, doc_type: str, ref_id: str, identity: str, dims: int = 512) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO embeddings "
        "(id, doc_type, ref_id, model, dims, vector, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (
            f"emb_{ref_id}",
            doc_type,
            ref_id,
            identity,
            dims,
            np.ones(dims, dtype=np.float32).tobytes(),
        ),
    )
    conn.commit()


def test_cached_vectors_from_another_identity_are_not_reused(db_conn, tmp_path: Path):
    """换了档位之后，旧身份的向量必须被重新编码，而不是当成有效缓存。"""
    from agent.selector.base import IndexedDoc

    backend = _StubBackend(db_conn, tmp_path, "onnx:bge-small-zh-v1.5:int8:22")
    _seed_embedding(db_conn, "memory_index", "doc_1", "onnx:bge-small-zh-v1.5:fp32:90")

    backend.index([IndexedDoc(doc_id="doc_1", text="一段历史")])

    row = db_conn.execute(
        "SELECT model FROM embeddings WHERE doc_type = 'memory_index' AND ref_id = 'doc_1'"
    ).fetchone()
    assert row["model"] == "onnx:bge-small-zh-v1.5:int8:22", "缓存应被新身份的向量覆盖"


def test_cached_vectors_with_same_identity_are_reused(db_conn, tmp_path: Path):
    from agent.selector.base import IndexedDoc

    calls: list[list[str]] = []
    backend = _StubBackend(db_conn, tmp_path, "onnx:same:fp32:1")
    _seed_embedding(db_conn, "memory_index", "doc_1", "onnx:same:fp32:1", dims=512)
    original = backend.embed_texts

    def counting(texts):
        calls.append(list(texts))
        return original(texts)

    backend.embed_texts = counting  # type: ignore[assignment]
    backend.index([IndexedDoc(doc_id="doc_1", text="一段历史")])

    assert calls == [], "身份一致时必须直接复用缓存，不重新编码"


def test_entity_card_search_filters_by_identity(db_conn, tmp_path: Path):
    backend = _StubBackend(db_conn, tmp_path, "onnx:now:fp32:1")
    _seed_embedding(db_conn, "entity_card", "card_old", "onnx:old:fp32:9")
    _seed_embedding(db_conn, "entity_card", "card_now", "onnx:now:fp32:1")
    backend.embed_texts = lambda texts: np.stack([np.ones(512, dtype=np.float32) for _ in texts])  # type: ignore[assignment]

    hits = backend.entity_card_search("随便问一句", top_k=5)

    assert [card for card, _ in hits] == ["card_now"], "旧身份下的实体卡向量不参与比较"
