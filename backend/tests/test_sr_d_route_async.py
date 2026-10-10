"""契约 4（D）：ToolRouter.route_async —— 纯 CPU 嵌入/余弦经有界执行器移出事件循环。

验收点（契约 4）：
- route_async(query, specs, *, pending_tasks, web_allowed) -> list[ToolSpec] 存在；
- embed_texts 批量与余弦计算在**非事件循环线程**上执行（线程身份断言）；
- 缓存 _tool_vecs 的写提交只发生在事件循环侧（线程内不写共享缓存）；
- 过时/晚到结果不覆盖缓存终态（backend 换掉后，旧结果不得写入）；
- 同步 route() 行为不变（结果与 route_async 一致）；
- 取消路径：取消后晚到的嵌入结果不得写入缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from contextlib import suppress

import numpy as np

from agent.adapters.base import ToolSpec
from agent.services.tool_router import CORE_TOOLS, ToolRouter


class RecordingEmbedding:
    """确定性嵌入后端：记录每次调用的批量大小与执行线程。"""

    def __init__(
        self,
        name: str = "fake",
        model_name: str = "m1",
        dims: int = 32,
        delay: float = 0.0,
        gate: threading.Event | None = None,
    ) -> None:
        self.name = name
        self.model_name = model_name
        self.dims = dims
        self.delay = delay
        self.gate = gate
        self.calls: list[tuple[int, int]] = []  # (batch_size, thread_ident)

    def available(self) -> bool:
        return True

    def embed_texts(self, texts):
        self.calls.append((len(texts), threading.get_ident()))
        if self.gate is not None:
            if not self.gate.wait(15):
                raise RuntimeError("gate timeout in test (should not happen)")
        if self.delay:
            time.sleep(self.delay)
        out = []
        for t in texts:
            v = np.zeros(self.dims, dtype=np.float32)
            for tok in (t or "").split():
                b = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dims
                v[b] += 1.0
            n = float(np.linalg.norm(v)) or 1e-9
            out.append(v / n)
        return np.stack(out)


class CacheObservingEmbedding(RecordingEmbedding):
    """额外观察共享缓存在线程执行期间的状态（线程内是否有人写缓存）。"""

    def __init__(self, router: ToolRouter, **kw) -> None:
        super().__init__(**kw)
        self.router = router
        self.observed_sizes: list[int] = []

    def embed_texts(self, texts):
        self.observed_sizes.append(len(self.router._tool_vecs))
        return super().embed_texts(texts)


def _specs(n: int = 12) -> list[ToolSpec]:
    specs = [
        ToolSpec(name="memory_search", description="检索历史记忆", parameters={}),
        ToolSpec(name="switch_topic", description="切换话题", parameters={}),
        ToolSpec(name="create_topic", description="创建话题", parameters={}),
        ToolSpec(name="continue_from_fragment", description="从片段继续", parameters={}),
    ]
    for i in range(n):
        specs.append(
            ToolSpec(name=f"tool_{i}", description=f"does thing {i}", parameters={})
        )
    return specs


async def test_route_async_exists_with_contract_signature():
    router = ToolRouter()
    specs = _specs(3)
    result = await router.route_async("随便聊聊", specs)
    assert all(isinstance(s, ToolSpec) for s in result)
    for core in CORE_TOOLS:
        assert any(s.name == core for s in result)


async def test_route_async_runs_embed_off_event_loop_thread():
    emb = RecordingEmbedding(delay=0.25)
    router = ToolRouter(top_n=40, embedding=emb)
    main_ident = threading.get_ident()
    t0 = time.perf_counter()
    result = await router.route_async("查一下今天天气", _specs(12))
    elapsed = time.perf_counter() - t0
    # 全部 embed 调用都发生在别的线程
    assert emb.calls, "embed_texts should have been called"
    for batch, ident in emb.calls:
        assert ident != main_ident, "embed_texts must not run on the event loop thread"
    # 确实是慢调用（sleep 生效），而不是被同步吞掉
    assert elapsed >= 0.25
    # 结果仍然正确
    names = [s.name for s in result]
    for core in CORE_TOOLS:
        assert core in names


async def test_route_async_matches_sync_route_result():
    emb1 = RecordingEmbedding(model_name="m1")
    emb2 = RecordingEmbedding(model_name="m1")
    specs = _specs(10)
    sync_names = [s.name for s in ToolRouter(top_n=40, embedding=emb1).route("查天气", specs)]
    async_names = [
        s.name for s in await ToolRouter(top_n=40, embedding=emb2).route_async("查天气", specs)
    ]
    assert sync_names == async_names


async def test_route_async_conditional_and_web_flags_parity():
    specs = _specs(3) + [
        ToolSpec(name="await_task", description="await", parameters={}),
        ToolSpec(name="web_search", description="search the web", parameters={}),
    ]
    router = ToolRouter(embedding=None)
    names_off = {
        s.name for s in await router.route_async("q", specs, pending_tasks=False, web_allowed=False)
    }
    assert "await_task" not in names_off and "web_search" not in names_off
    names_on = {s.name for s in await router.route_async("联网搜一下最新", specs, pending_tasks=True)}
    assert "await_task" in names_on and "web_search" in names_on


async def test_route_async_cache_populated_and_second_call_reuses():
    emb = RecordingEmbedding()
    router = ToolRouter(top_n=40, embedding=emb)
    specs = _specs(10)
    await router.route_async("第一问", specs)
    batches = [b for b, _ in emb.calls]
    assert batches[0] == 10  # 工具批量一次算完
    assert batches[1] == 1  # query 单独一次
    assert len(emb.calls) == 2
    await router.route_async("第二问", specs)
    assert len(emb.calls) == 3  # 只多了 query 一次
    assert emb.calls[-1][0] == 1


async def test_route_async_thread_does_not_write_shared_cache():
    """线程内只算新向量：embed 在线程里执行**期间**，共享缓存不得被写入。"""
    router = ToolRouter(top_n=40)
    emb = CacheObservingEmbedding(router, model_name="m1", delay=0.05)
    router.embedding = emb
    main_ident = threading.get_ident()

    await router.route_async("观察缓存", _specs(10))

    assert len(emb.calls) == 2  # 工具批量 + query
    for ident in (t for _, t in emb.calls):
        assert ident != main_ident, "embed must run off the loop thread"
    # 工具批量执行期间缓存还是空的 → 提交只可能发生在事件循环侧（此后）
    assert emb.observed_sizes[0] == 0, (
        f"cache was already written when the tools batch started: {emb.observed_sizes}"
    )
    # 提交完成后缓存是完整的（事件循环侧完成）
    assert len(router._tool_vecs) == 10


async def test_route_async_stale_result_does_not_overwrite_cache():
    """backend 换掉/缓存失效后，晚到的旧线程结果不得写进缓存。"""
    emb1 = RecordingEmbedding(model_name="m1", delay=0.3)
    emb2 = RecordingEmbedding(model_name="m2")
    router = ToolRouter(top_n=40, embedding=emb1)
    specs = _specs(6)

    task = asyncio.ensure_future(router.route_async("换后端", specs))
    await asyncio.sleep(0.05)  # 让 emb1 的线程任务已经启动
    router.invalidate()
    router.embedding = emb2  # backend 变化 → 旧结果必须作废
    await asyncio.sleep(1.2)  # 等 emb1 的晚到结果完整跑完并「试图」提交
    # 晚到的旧结果不得写入任何 key（事件循环侧提交时发现 backend 已换 → 丢弃）
    assert len(router._tool_vecs) == 0, f"stale results leaked into cache: {router._tool_vecs}"

    # 新 backend 正常路由，晚到的旧任务完成也不影响
    result = await router.route_async("换后端", specs)
    assert [s.name for s in result]
    with suppress(asyncio.CancelledError):
        await task


async def test_route_async_cancelled_late_result_not_in_cache():
    """取消 route_async：晚到的嵌入结果不得写入缓存，且不影响后续调用。"""
    emb = RecordingEmbedding(delay=0.4)
    router = ToolRouter(top_n=40, embedding=emb)
    task = asyncio.ensure_future(router.route_async("会被取消", _specs(8)))
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.5)  # 线程里的晚到结果在此期间完成
    assert len(router._tool_vecs) == 0, "cancelled route_async must not populate cache"
    # 取消后新的路由照常工作
    result = await router.route_async("再来一次", _specs(8))
    assert [s.name for s in result]


async def test_route_async_empty_query_fallback():
    router = ToolRouter(embedding=RecordingEmbedding())
    result = await router.route_async("", _specs(5))
    names = [s.name for s in result]
    for core in CORE_TOOLS:
        assert core in names


async def test_route_async_concurrent_calls_are_safe():
    """多个 route_async 并发（同一批 specs）：缓存终态正确、结果正确、无异常。"""
    emb = RecordingEmbedding(delay=0.05)
    router = ToolRouter(top_n=40, embedding=emb)
    specs = _specs(10)
    results = await asyncio.gather(
        *(router.route_async(f"并发问题 {i}", specs) for i in range(4))
    )
    for result in results:
        assert len(result) > 0
        assert any(s.name == "memory_search" for s in result)
    # 缓存终态完整、key 无重复、向量与确定性嵌入一致
    assert len(router._tool_vecs) == 10
    keys = {(router._backend_key(), f"tool_{i} does thing {i}") for i in range(10)}
    assert keys <= set(router._tool_vecs)
    direct = emb.embed_texts(["tool_3 does thing 3"])[0]
    assert (router._tool_vecs[(router._backend_key(), "tool_3 does thing 3")] == direct).all()


async def test_route_async_web_boost_parity():
    router = ToolRouter(embedding=None)
    specs = _specs(2) + [ToolSpec(name="web_search", description="search the web", parameters={})]
    sync_names = [s.name for s in router.route("帮我联网搜一下最新消息", specs)]
    async_names = [s.name for s in await router.route_async("帮我联网搜一下最新消息", specs)]
    # 联网意图下 web_search 稳定出现在 core 工具之后的第一位（index == len(CORE_TOOLS)）
    assert sync_names.index("web_search") == len(CORE_TOOLS)
    assert sync_names == async_names
