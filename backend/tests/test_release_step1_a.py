"""发布前第一轮修复 · A：同一凭据不同模型的 adapter 缓存不能互相关闭。

原缺陷（`AppContext._remember_adapter`）：每次记住一个新的 adapter，会把
**同一 key_id 的其它所有缓存项** pop 并 `_close_adapter_soon`。于是同一把 Key 下：

* 记忆整理用模型 M1、知识提炼用模型 M2 → 建 M2 时把 M1 的连接关掉，
  那条已经开始的派生任务打到已关闭的 client；
* 主对话一轮还没结束，另一个用途/子任务用同一把 Key 换个模型 → 主对话连接被关。

要求语义（本文件逐条断言）：

1. 同一凭据下不同模型是不同槽位，可同时使用，谁也不能关闭谁的连接；
2. 相同有效配置（key_id+version+updated_at+base_url+model）复用同一个 adapter；
3. 配置变化后新工作用新配置；旧 adapter 不被立即关闭（已开始的工作仍能完成）；
4. 缓存/暂留名单都有边界，被引用的旧 adapter 运行期不关闭，没人引用的旧连接会被回收；
5. `aclose()` 关闭所有缓存与被顶替的 adapter；
6. 用量归属不变：`adapter.key_id` 正确、`bind_request_accounting` 绑定不被重建。

观测手段（不联网、不用真实 Key）：
* 能力探测换成受控替身（`probe_adapter`）；
* 「旧工作仍能 complete」「关闭是否发生」用受控 fake adapter + 替换
  `_create_adapter`（新建 adapter 的唯一入口），close 计数就是 `close()` 是否被调用；
* 派生任务用例走**真实** `build_derived_adapters()`（真实创建路径），只把
  `_close_adapter`（运行期唯一关闭入口）换成会转调原实现的记录器，断言它一次都没被调用。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import weakref

import pytest

from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion
from agent.adapters.probe import ProbeResult
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import app as app_module
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ENDPOINT = "https://api.example.com/v1"


# ---------------------------------------------------------------------------
# 受控环境（不联网、不用真实 Key）
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    yield app_ctx
    conn.close()


@pytest.fixture(autouse=True)
def fake_capability_probe(monkeypatch):
    """能力探测换受控替身：真实创建路径也不联网、不付费。"""

    async def fake_probe(client, model, endpoint=None, cache=None):  # noqa: ANN001, ANN202
        return ProbeResult(AdapterMode.NATIVE, "ok", time.time())

    monkeypatch.setattr("agent.services.app.probe_adapter", fake_probe)
    return fake_probe


@pytest.fixture()
def close_recorder(monkeypatch):
    """记录运行期每一次 `_close_adapter`（唯一关闭入口），并照常执行原实现。"""
    calls: list[BaseAdapter] = []
    original = app_module._close_adapter

    async def recorder(adapter):  # noqa: ANN202
        calls.append(adapter)
        await original(adapter)

    monkeypatch.setattr(app_module, "_close_adapter", recorder)
    return calls


def _grant(ctx: AppContext, key_id: str = "k1", model: str = "gpt-x") -> None:
    ctx.credentials.create(
        key_id,
        "sk-test-secret",
        ["main-loop"],
        ENDPOINT,
        model,
        verify_state="verified",
    )


class _FakeAdapter(BaseAdapter):
    """可观察的 adapter：`closed` 就是底层连接有没有被关。

    关闭后再 `complete()` 会失败（真实 adapter 的 client 关闭后正是如此），
    这样「旧工作仍能完成」就不是靠字符串/函数存在来断言。
    """

    mode = AdapterMode.NATIVE

    def __init__(self, model: str, endpoint: str, key_id: str) -> None:
        self.model = model
        self.endpoint = endpoint
        self.key_id = key_id
        self.closed = 0
        self.completed = 0

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):  # noqa: ANN001, ANN202
        if self.closed:
            raise RuntimeError("底层的模型连接已经被关闭")
        self.completed += 1
        return Completion(message=ChatMessage(role="assistant", content="ok"))

    async def close(self) -> None:
        self.closed += 1


def _fake_creation(
    monkeypatch,
    ctx: AppContext,
    *,
    keep: list | None = None,
    weakrefs: list | None = None,
):  # noqa: ANN001, ANN202
    """把新建 adapter 的唯一入口换成受控替身（缓存/淘汰逻辑走真实实现）。"""

    async def fake_create(secret, model, base_url, key_id, meta):  # noqa: ANN001, ANN202
        adapter = _FakeAdapter(model=model, endpoint=base_url, key_id=key_id)
        if keep is not None:
            keep.append(adapter)
        if weakrefs is not None:
            weakrefs.append(weakref.ref(adapter))
        return adapter

    monkeypatch.setattr(ctx, "_create_adapter", fake_create)
    return fake_create


async def _flush() -> None:
    """让事件循环把已经排队的任务（例如旧的 `_close_adapter_soon`）跑完。

    只做确定性的让出，不依赖时间/随机 sleep：转几次 `sleep(0)` 足以让
    `create_task` 排下的关闭任务执行完。
    """
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 1 + 2：同一凭据下三个不同模型共存；同配置复用
# ---------------------------------------------------------------------------


async def test_three_models_under_one_credential_do_not_close_each_other(ctx, monkeypatch):
    _grant(ctx)
    created: list[_FakeAdapter] = []
    _fake_creation(monkeypatch, ctx, keep=created)

    first = await ctx.build_adapter_for_credential("k1", "model-a")
    second = await ctx.build_adapter_for_credential("k1", "model-b")
    third = await ctx.build_adapter_for_credential("k1", "model-c")
    again = await ctx.build_adapter_for_credential("k1", "model-a")

    await _flush()

    assert first is not None and second is not None and third is not None
    assert len({id(first), id(second), id(third)}) == 3, "三个不同模型必须是三个不同的 adapter"
    assert again is first, "同一有效配置必须复用同一个 adapter 对象"
    assert [adapter.closed for adapter in created] == [0, 0, 0], (
        "同一把凭据下的不同模型可以同时使用：建 B/C 不能关闭 A 的连接"
        "（历史缺陷：_remember_adapter 会 pop 掉同一 key_id 的所有其它缓存项并关闭）"
    )
    completion = await first.complete([], [])
    assert completion is not None and first.completed == 1, "A 的连接必须还能正常完成一次调用"


async def test_same_configuration_reuses_same_adapter_object(ctx, monkeypatch):
    _grant(ctx)
    created: list[_FakeAdapter] = []
    _fake_creation(monkeypatch, ctx, keep=created)

    first = await ctx.build_adapter_for_credential("k1", "model-a")
    second = await ctx.build_adapter_for_credential("k1", "model-a")

    assert second is first
    assert len(created) == 1, "相同有效配置不得重建连接（缓存命中必须复用）"
    assert len(ctx._adapter_cache) == 1


# ---------------------------------------------------------------------------
# 3：配置变化后新工作用新配置，旧工作不被中断
# ---------------------------------------------------------------------------


async def test_credential_rotation_does_not_close_adapter_already_in_use(ctx, monkeypatch):
    _grant(ctx)
    created: list[_FakeAdapter] = []
    _fake_creation(monkeypatch, ctx, keep=created)

    started = await ctx.build_adapter_for_credential("k1")
    assert started is not None

    ctx.credentials.update_secret("k1", "sk-rotated-secret")
    rebuilt = await ctx.build_adapter_for_credential("k1")

    await _flush()

    assert rebuilt is not None and rebuilt is not started, "配置变化后新工作必须用新配置的 adapter"
    assert rebuilt.model == started.model and rebuilt.key_id == "k1"
    assert started.closed == 0, "已开始的工作不能被换配置的行为中断（旧连接不得立即关闭）"
    retired = [adapter for _key, adapter in getattr(ctx, "_adapter_retired", [])]
    assert started in retired, "被顶替的旧 adapter 应进暂留名单，而不是立刻被关掉"
    completion = await started.complete([], [])
    assert completion is not None and started.completed == 1, "旧工作必须仍能收尾"


# ---------------------------------------------------------------------------
# 4：有界缓存 + 被引用的不关 + 没人引用的真的回收
# ---------------------------------------------------------------------------


async def test_cache_stays_bounded_without_closing_referenced_adapters(ctx, monkeypatch):
    _grant(ctx)
    weakrefs: list = []
    _fake_creation(monkeypatch, ctx, weakrefs=weakrefs)

    first = await ctx.build_adapter_for_credential("k1", "model-00")
    assert first is not None
    for index in range(1, 60):
        await ctx.build_adapter_for_credential("k1", f"model-{index:02d}")

    await _flush()

    cache_limit = getattr(app_module, "ADAPTER_CACHE_LIMIT", None)
    retired_limit = getattr(app_module, "ADAPTER_RETIRED_LIMIT", None)
    assert isinstance(cache_limit, int) and cache_limit > 0, (
        "adapter 缓存必须有一个明确的上限（module-level ADAPTER_CACHE_LIMIT）"
    )
    assert isinstance(retired_limit, int) and retired_limit >= 0, (
        "被顶替的旧 adapter 暂留名单也必须有一个明确上限（ADAPTER_RETIRED_LIMIT）"
    )
    assert len(ctx._adapter_cache) <= cache_limit, (
        f"当前缓存必须有界：{len(ctx._adapter_cache)} > {cache_limit}"
    )
    assert len(ctx._adapter_retired) <= retired_limit, (
        f"被顶替的旧 adapter 也只能暂留有限个：{len(ctx._adapter_retired)} > {retired_limit}"
    )
    assert first.closed == 0, "仍被引用的旧 adapter（有人还在用）运行期不得被关闭"
    completion = await first.complete([], [])
    assert completion is not None and first.completed == 1

    alive = sum(1 for ref in weakrefs if ref() is not None)
    retained_limit = cache_limit + retired_limit
    assert alive <= retained_limit + 1, (
        "同时活着的 adapter 必须被上限约束（+1 是本用例自己持有的那个）："
        f"{alive} > {retained_limit + 1}"
    )
    reclaimed = len(weakrefs) - alive
    assert reclaimed >= 10, (
        "没人引用的历史连接必须被真的回收（不能无限保留）："
        f"60 个里只回收了 {reclaimed} 个"
    )


# ---------------------------------------------------------------------------
# 5：应用退出统一关闭
# ---------------------------------------------------------------------------


async def test_aclose_closes_cached_and_replaced_adapters(ctx, monkeypatch):
    _grant(ctx)
    created: list[_FakeAdapter] = []
    _fake_creation(monkeypatch, ctx, keep=created)

    await ctx.build_adapter_for_credential("k1", "model-a")
    ctx.credentials.update_secret("k1", "sk-rotated-secret")
    await ctx.build_adapter_for_credential("k1", "model-a")
    await ctx.build_adapter_for_credential("k1", "model-b")

    assert [adapter.closed for adapter in created] == [0, 0, 0], "运行期不得关闭它们"
    await ctx.aclose()

    assert ctx._adapter_cache == {}
    assert getattr(ctx, "_adapter_retired", []) == []
    assert [adapter.closed for adapter in created] == [1, 1, 1], (
        "应用关闭必须关闭所有缓存 adapter 与被顶替仍暂留的 adapter"
    )


# ---------------------------------------------------------------------------
# 真实创建路径：派生三类同一凭据不同模型
# ---------------------------------------------------------------------------

_MODEL_CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS credential_models (
    id                 TEXT PRIMARY KEY,
    credential_id      TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    display_name       TEXT,
    source             TEXT NOT NULL DEFAULT 'discovered',
    capabilities       TEXT NOT NULL DEFAULT '[]',
    verify_state       TEXT NOT NULL DEFAULT 'unverified',
    verified_at        TEXT,
    verify_error       TEXT,
    verify_fingerprint TEXT,
    context_window     INTEGER,
    discovered_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (credential_id, model_id)
)
"""

_MODEL_USAGES_DDL = """
CREATE TABLE IF NOT EXISTS model_usages (
    id            TEXT PRIMARY KEY,
    usage_key     TEXT NOT NULL UNIQUE,
    credential_id TEXT,
    model_id      TEXT,
    mode          TEXT NOT NULL DEFAULT 'inherit',
    updated_at    TEXT NOT NULL
)
"""


def _catalog_tables(conn: sqlite3.Connection) -> None:
    conn.execute(_MODEL_CATALOG_DDL)
    conn.execute(_MODEL_USAGES_DDL)
    conn.commit()


def _assign(conn: sqlite3.Connection, usage_key: str, credential_id: str, model_id: str) -> None:
    _catalog_tables(conn)
    now = "2026-10-28T00:00:00+00:00"
    conn.execute(
        "INSERT INTO credential_models (id, credential_id, model_id, source, capabilities,"
        " verify_state, discovered_at, updated_at)"
        " VALUES (?, ?, ?, 'manual', '[]', 'verified', ?, ?)"
        " ON CONFLICT(credential_id, model_id) DO NOTHING",
        (f"cm_{credential_id}_{model_id}", credential_id, model_id, now, now),
    )
    conn.execute(
        "INSERT INTO model_usages (id, usage_key, credential_id, model_id, mode, updated_at)"
        " VALUES (?, ?, ?, ?, 'explicit', ?)"
        " ON CONFLICT(usage_key) DO UPDATE SET credential_id=excluded.credential_id,"
        " model_id=excluded.model_id, mode='explicit', updated_at=excluded.updated_at",
        (f"mu_{usage_key}", usage_key, credential_id, model_id, now),
    )
    conn.commit()


async def test_derived_adapters_same_credential_different_models_coexist(ctx, close_recorder):
    """真实触发条件：记忆整理 / 知识提炼 / 实体提炼同一把 Key、三个不同模型。"""
    _grant(ctx)
    _assign(ctx.conn, "memory-organize", "k1", "model-mem")
    _assign(ctx.conn, "knowledge-distill", "k1", "model-know")
    _assign(ctx.conn, "entity-extract", "k1", "model-ent")

    adapters = await ctx.build_derived_adapters()

    await _flush()
    summary, knowledge, entities = (
        adapters["summary"],
        adapters["knowledge"],
        adapters["entities"],
    )
    assert all(isinstance(item, BaseAdapter) for item in adapters.values()), (
        f"三条派生任务都必须建出可用的 adapter：{adapters}"
    )
    assert [(item.key_id, item.model) for item in (summary, knowledge, entities)] == [
        ("k1", "model-mem"),
        ("k1", "model-know"),
        ("k1", "model-ent"),
    ]
    assert len({id(summary), id(knowledge), id(entities)}) == 3
    assert close_recorder == [], (
        "三条派生工作同时使用同一把凭据的不同模型：建后一个不得关闭前面那个的连接"
        f"（实际被关闭了 {len(close_recorder)} 次）"
    )
    assert len([key for key in ctx._adapter_cache if key[0] == "k1"]) == 3


# ---------------------------------------------------------------------------
# 6：用量归属不变（key_id + bind_request_accounting 不被重建）
# ---------------------------------------------------------------------------


async def test_adapter_key_id_and_accounting_binding_survive_cache_reuse(ctx):
    from agent.credentials.usage import request_accounting

    _grant(ctx)
    first = await ctx.build_adapter_for_credential("k1", "model-a")
    assert first is not None
    assert (first.key_id, first.model) == ("k1", "model-a")
    binding = request_accounting(first)
    assert binding is not None and binding.store is ctx.credentials
    assert binding.key_id == "k1" and binding.is_explicit

    again = await ctx.build_adapter_for_credential("k1", "model-a")
    assert again is first
    assert request_accounting(again) is binding, "缓存复用不得重建/改写归属绑定"

    other = await ctx.build_adapter_for_credential("k1", "model-b")
    assert other is not None and other is not first
    assert other.key_id == "k1"
    other_binding = request_accounting(other)
    assert other_binding is not None and other_binding.store is ctx.credentials
    assert other_binding.key_id == "k1"
    assert other_binding is not binding
    assert binding.requests == other_binding.requests == 0, "新建 adapter 不复制/重置任何用量计数"
