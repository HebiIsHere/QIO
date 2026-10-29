"""Adapter / 模型 client 的生命周期。

历史缺陷：
* `build_adapter_for_credential` 每轮都新建 `AsyncOpenAI`（新连接池、新 TLS 会话）；
* Anthropic 端点每轮都真实打一次 capability probe（一次额外网络往返）。

要求：同一 (provider, endpoint, credential version, model) 复用同一个 adapter；
凭据变化立即失效；应用关闭时统一 close。
"""

from __future__ import annotations

import time

import pytest

from agent.adapters.base import AdapterMode
from agent.adapters.probe import ProbeResult
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import ADAPTER_CACHE_LIMIT, AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    yield app_ctx
    conn.close()


def _grant(ctx: AppContext, key_id: str = "k1", endpoint: str = "https://api.example.com/v1"):
    ctx.credentials.create(
        key_id,
        "sk-test-secret",
        ["main-loop"],
        endpoint,
        "gpt-x",
        verify_state="verified",
    )


@pytest.fixture()
def probe_counter(monkeypatch):
    calls: list[tuple] = []

    async def fake_probe(client, model, endpoint=None, cache=None):
        calls.append((model, endpoint))
        return ProbeResult(AdapterMode.NATIVE, "ok", time.time())

    monkeypatch.setattr("agent.services.app.probe_adapter", fake_probe)
    return calls


@pytest.fixture()
def anthropic_probe_counter(monkeypatch):
    calls: list[tuple] = []

    async def fake_probe(secret, model, endpoint, *, key_id=None, accounting_store=None):
        # A06：Anthropic 能力探测本身是一次真实请求，所以要带上「算在哪把凭据上」的身份
        # （第三个元素就是 key_id；缺了它这次调用既不受该凭据预算约束、也不进账本）。
        calls.append((model, endpoint, key_id))
        return "native"

    monkeypatch.setattr("agent.services.app.probe_anthropic", fake_probe)
    return calls


async def test_adapter_is_reused_across_turns(ctx, probe_counter):
    _grant(ctx)
    first = await ctx.build_adapter()
    second = await ctx.build_adapter()
    assert first is not None
    assert first is second, "同一个凭据/端点/模型必须复用同一个 adapter"
    assert len(probe_counter) == 1, "能力探测不应该每个 Turn 重做一次"
    await ctx.aclose()


async def test_credential_change_invalidates_cached_adapter(ctx, probe_counter):
    _grant(ctx)
    first = await ctx.build_adapter()
    ctx.credentials.update_secret("k1", "sk-rotated-secret")
    second = await ctx.build_adapter()
    assert first is not None and second is not None
    assert first is not second, "凭据版本变化后必须重建 adapter"
    assert len(probe_counter) == 2
    await ctx.aclose()


async def test_anthropic_probe_is_cached_per_credential(ctx, anthropic_probe_counter):
    _grant(ctx, endpoint="https://api.anthropic.com/v1")
    first = await ctx.build_adapter()
    second = await ctx.build_adapter()
    assert first is not None and second is not None
    assert first is second
    assert len(anthropic_probe_counter) == 1, "Anthropic 能力探测必须缓存，不能每轮实打"
    assert anthropic_probe_counter[0][2] == "k1", (
        "A06：能力探测也是一次真实请求，必须带上凭据身份 —— "
        "否则这次调用不受该凭据的预算约束，也不会记进它的账本"
    )
    await ctx.aclose()


async def test_aclose_closes_cached_clients(ctx, probe_counter):
    _grant(ctx)
    adapter = await ctx.build_adapter()
    assert adapter is not None
    await ctx.aclose()
    assert ctx._adapter_cache == {}, "关闭后不应该还留着缓存里的 adapter/client"


async def test_adapter_cache_does_not_grow_without_bound(ctx, probe_counter):
    _grant(ctx)
    for i in range(6):
        ctx.credentials.update_secret("k1", f"sk-rotated-{i}")
        await ctx.build_adapter()
    # A 修复后的语义：槽位是 (key_id, model)。**同一槽位**换配置只保留最新那份
    # （旧版本进有界暂留名单，不在这里重复计数，运行期也不关闭它 —— 见
    # tests/test_release_step1_a.py）；不同模型是不同槽位，可以同时存在。
    #
    # 旧断言是「同一 key_id 只保留最新版本的 adapter」——它锁死的正是缺陷行为：
    # 同一把 Key 下换一个模型会把别的模型正在用的连接关掉。
    assert len([k for k in ctx._adapter_cache if k[0] == "k1" and k[-1] == "gpt-x"]) == 1
    assert len(ctx._adapter_cache) <= ADAPTER_CACHE_LIMIT, "缓存总体必须有界"
    await ctx.aclose()
