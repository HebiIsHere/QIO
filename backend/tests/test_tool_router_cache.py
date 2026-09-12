from __future__ import annotations

from agent.adapters.base import ToolSpec
from agent.services.tool_router import ToolRouter, wants_web


class CountingEmbedding:
    """Deterministic counting backend for router cache assertions."""

    def __init__(self, name: str = "fake", model_name: str = "m1", dims: int = 32) -> None:
        self.name = name
        self.model_name = model_name
        self.dims = dims
        self.calls: list[int] = []  # batch sizes per embed_texts call

    def available(self) -> bool:
        return True

    def embed_texts(self, texts):
        import hashlib

        import numpy as np

        self.calls.append(len(texts))
        out = []
        for t in texts:
            v = np.zeros(self.dims, dtype=np.float32)
            for tok in (t or "").split():
                b = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dims
                v[b] += 1.0
            n = float(np.linalg.norm(v)) or 1e-9
            out.append(v / n)
        return np.stack(out)


def _specs(n: int) -> list[ToolSpec]:
    specs = [
        ToolSpec(name="memory_search", description="memory", parameters={}),
        ToolSpec(name="switch_topic", description="switch", parameters={}),
        ToolSpec(name="create_topic", description="create", parameters={}),
    ]
    for i in range(n):
        specs.append(ToolSpec(name=f"tool_{i}", description=f"does thing {i}", parameters={}))
    return specs


def test_query_embedded_once_and_tools_cached():
    emb = CountingEmbedding()
    router = ToolRouter(top_n=40, embedding=emb)
    specs = _specs(30)

    router.route("查一下天气", specs)
    first = list(emb.calls)
    assert first[-1] == 1  # query：每次 planning 只 1 次
    assert len(first) == 2  # 工具批量 1 次 + query 1 次
    assert sum(first[:-1]) == 30  # 工具描述批量算一次

    router.route("另一个问题", specs)
    after = list(emb.calls)
    assert len(after) == len(first) + 1  # 只多一次 query
    assert after[-1] == 1


def test_cache_invalidated_on_definition_change():
    emb = CountingEmbedding()
    router = ToolRouter(embedding=emb)
    router.route("q", _specs(5))
    n1 = len(emb.calls)
    router.invalidate()
    router.route("q", _specs(5))
    assert len(emb.calls) == n1 + 2  # 缓存失效 → 工具重新批量 + query


def test_backend_key_change_reembeds():
    emb1 = CountingEmbedding(model_name="m1")
    router = ToolRouter(embedding=emb1)
    specs = _specs(5)
    router.route("q", specs)
    n1 = len(emb1.calls)

    emb2 = CountingEmbedding(model_name="m2")
    router.embedding = emb2  # backend/model 变化 → cache key 变化
    router.route("q", specs)
    assert len(emb2.calls) == 2  # 新 backend 上重新批量 + query
    assert n1 == 2


def test_conditional_task_tools():
    router = ToolRouter(embedding=None)
    specs = _specs(3) + [
        ToolSpec(name="await_task", description="await", parameters={}),
        ToolSpec(name="read_task_result", description="read", parameters={}),
    ]
    names_off = {s.name for s in router.route("随便聊聊", specs, pending_tasks=False)}
    assert "await_task" not in names_off and "read_task_result" not in names_off
    names_on = {s.name for s in router.route("随便聊聊", specs, pending_tasks=True)}
    assert "await_task" in names_on and "read_task_result" in names_on


def test_web_search_conditional_and_intent_boost():
    router = ToolRouter(embedding=None)
    specs = _specs(3) + [ToolSpec(name="web_search", description="search the web", parameters={})]
    # capability 不允许 → 完全不暴露
    assert "web_search" not in {s.name for s in router.route("q", specs, web_allowed=False)}
    # 明确联网意图 → 稳定出现
    assert "web_search" in {s.name for s in router.route("帮我联网搜一下最新消息", specs)}
    assert wants_web("帮我联网搜一下最新消息") is True
