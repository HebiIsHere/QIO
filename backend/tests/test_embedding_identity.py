from __future__ import annotations

from agent.adapters.base import ToolSpec
from agent.services.tool_router import ToolRouter


class _Backend:
    def __init__(self, name, model_name, dims):
        self.name = name
        self.model_name = model_name
        self.dims = dims

    def available(self):
        return True

    def embed_texts(self, texts):
        import numpy as np

        return np.ones((len(texts), self.dims), dtype=np.float32)


def test_cache_key_uses_embedding_identity_not_chat_provider():
    router = ToolRouter(embedding=_Backend("onnx", "bge-small-zh-v1.5", 512))
    k1 = router._backend_key()
    # 换 chat provider 不影响 embedding identity（router 不含 chat 模型信息）
    assert k1 == "onnx|bge-small-zh-v1.5|512"

    router.embedding = _Backend("remote", "text-embedding-3-small", 1536)
    assert router._backend_key() != k1  # backend/model 变化 → 新 cache key


def test_distinct_backends_have_distinct_keys():
    a = ToolRouter(embedding=_Backend("bm25", "", 0))._backend_key()
    b = ToolRouter(embedding=_Backend("onnx", "bge-small-zh-v1.5", 512))._backend_key()
    assert a != b
