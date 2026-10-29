"""用途解析接进真实调用链：主对话 / 子任务 / 看图 / 后台三类。

这里断言的是**调用真的换了目标**，不是「选择器看起来生效」：
* 主对话：`build_adapter()` 建出来的 adapter 属于用途解析出的那把 Key + 那个模型；
* 后台三类：记忆整理 / 知识提炼 / 实体提炼各自拿到自己的 adapter（互不影响）；
* 不可用：显式指定坏了就如实失败（有原因码），默认层坏了回退并留下降级事实；
* 已开始执行的调用保留原配置（冻结的 UsageTarget 不受后续保存影响）。

全部用 fake provider（`probe_adapter` 打桩），不联网。
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from agent.adapters.base import AdapterMode
from agent.adapters.probe import ProbeResult
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import usage_target as usage_module
from agent.services.app import AppContext
from agent.services.usage_target import (
    REASON_EXPLICIT_UNAVAILABLE,
    SOURCE_DEFAULT,
    SOURCE_EXPLICIT,
    SOURCE_POLICY,
    UsageUnavailable,
)

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


@pytest.fixture()
def ctx(tmp_path):
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    yield app_ctx
    conn.close()


@pytest.fixture()
def probe_counter(monkeypatch):
    calls: list[tuple] = []

    async def fake_probe(client, model, endpoint=None, cache=None):
        calls.append((model, endpoint))
        return ProbeResult(AdapterMode.NATIVE, "ok", time.time())

    monkeypatch.setattr("agent.services.app.probe_adapter", fake_probe)
    return calls


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


def _grant(ctx, key_id: str, *, tags=None, model: str = "gpt-x", budget=None) -> None:
    ctx.credentials.create(
        key_id,
        "sk-test-secret",
        list(tags or ["main-loop"]),
        "https://api.example.com/v1",
        model,
        budget=budget,
        verify_state="verified",
    )


def _set_default(ctx, credential_id: str, model_id: str) -> None:
    """写默认模型（同时补 credential_models 行：数据层按「这个模型有没有记录」判可用）。"""
    _catalog_tables(ctx.conn)
    now = "2026-10-28T00:00:00+00:00"
    ctx.conn.execute(
        "INSERT INTO credential_models (id, credential_id, model_id, source, capabilities,"
        " verify_state, discovered_at, updated_at)"
        " VALUES (?, ?, ?, 'manual', '[]', 'verified', ?, ?)"
        " ON CONFLICT(credential_id, model_id) DO NOTHING",
        (f"cm_{credential_id}_{model_id}", credential_id, model_id, now, now),
    )
    ctx.conn.commit()
    ctx.settings_store.set(
        "model.default",
        json.dumps({"credential_id": credential_id, "model_id": model_id}),
    )


# -- 主对话 --------------------------------------------------------------------


async def test_main_chat_uses_explicit_usage_assignment(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "pinned-key", model="gpt-pinned")
    _assign(ctx.conn, "main-chat", "pinned-key", "gpt-pinned")

    target = ctx.resolve_main_target()
    assert target.source == SOURCE_EXPLICIT
    assert (target.credential_id, target.model_id) == ("pinned-key", "gpt-pinned")

    adapter = await ctx.build_adapter()
    assert adapter is not None
    assert adapter.model == "gpt-pinned"
    assert adapter.key_id == "pinned-key"


async def test_main_chat_uses_default_model_when_no_assignment(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-old")
    _grant(ctx, "daily-key", model="gpt-daily")
    _set_default(ctx, "daily-key", "gpt-daily")

    adapter = await ctx.build_adapter()
    assert adapter is not None
    assert adapter.model == "gpt-daily"
    assert adapter.key_id == "daily-key"
    assert ctx.usage_targets["main-chat"].source == SOURCE_DEFAULT


async def test_default_model_unavailable_falls_back_and_marks_degraded(ctx, probe_counter):
    """默认模型不可用 → 保留基线回退，但必须留下「已降级」的事实。"""
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "daily-key", model="gpt-daily")
    ctx.credentials.set_enabled("daily-key", False)
    _set_default(ctx, "daily-key", "gpt-daily")

    adapter = await ctx.build_adapter()
    assert adapter is not None
    assert adapter.key_id == "main-key"
    target = ctx.usage_targets["main-chat"]
    assert target.source == SOURCE_POLICY
    assert target.degraded is True
    assert target.degrade_reason == "credential_disabled"
    assert "daily-key" in target.degrade_message


async def test_explicit_assignment_unavailable_does_not_fall_back(ctx, probe_counter):
    """显式指定的 Key 被停用：不静默换另一把可用的 Key，而是如实失败并说明原因。"""
    _grant(ctx, "spare-key", model="gpt-spare")
    _grant(ctx, "pinned-key", model="gpt-pinned")
    ctx.credentials.set_enabled("pinned-key", False)
    _assign(ctx.conn, "main-chat", "pinned-key", "gpt-pinned")

    adapter = await ctx.build_adapter()
    assert adapter is None
    error = ctx.usage_unavailable["main-chat"]
    assert error.reason_code == REASON_EXPLICIT_UNAVAILABLE
    assert error.detail_code == "credential_disabled"
    message = ctx.main_adapter_unavailable_message()
    assert "pinned-key" in message
    assert "spare-key" not in message


async def test_started_call_keeps_original_configuration(ctx, probe_counter):
    """已开始执行的调用保留原配置：冻结的 target 不受之后保存的新分配影响。"""
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "next-key", model="gpt-next")
    _set_default(ctx, "main-key", "gpt-main")

    frozen = ctx.resolve_main_target()
    first = await ctx.build_adapter_for_target(frozen)

    # 用户中途改成另一把 Key / 另一个模型
    _set_default(ctx, "next-key", "gpt-next")
    second = await ctx.build_adapter()

    assert first is not None and first.model == "gpt-main" and first.key_id == "main-key"
    assert second is not None and second.model == "gpt-next" and second.key_id == "next-key"
    assert first is not second


# -- 子任务 --------------------------------------------------------------------


def test_subagent_usage_assignment_is_honoured(ctx):
    _grant(ctx, "sub-key", tags=["subagent"], model="gpt-sub")
    _assign(ctx.conn, "subagent", "sub-key", "gpt-sub")
    assert ctx.subagent_usage_assignment() == ("sub-key", "gpt-sub")


def test_subagent_without_assignment_falls_back_to_baseline_selection(ctx):
    """没有显式分配、也没有默认模型 → 交给基线自主选择（不接管、不报错）。"""
    _grant(ctx, "sub-key", tags=["subagent"], model="gpt-sub")
    assert ctx.subagent_usage_assignment() is None


def test_subagent_explicit_assignment_unavailable_raises(ctx):
    _grant(ctx, "sub-key", tags=["subagent"], model="gpt-sub")
    ctx.credentials.set_enabled("sub-key", False)
    _assign(ctx.conn, "subagent", "sub-key", "gpt-sub")
    with pytest.raises(UsageUnavailable) as excinfo:
        ctx.subagent_usage_assignment()
    assert excinfo.value.reason_code == REASON_EXPLICIT_UNAVAILABLE


# -- 看图 ----------------------------------------------------------------------


async def test_vision_without_assignment_keeps_baseline_fallback(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    assert await ctx.build_vision_adapter() is None


async def test_vision_uses_vision_tag_credential(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "vision-key", tags=["vision"], model="gpt-vision")
    adapter = await ctx.build_vision_adapter()
    assert adapter is not None
    assert (adapter.key_id, adapter.model) == ("vision-key", "gpt-vision")


async def test_vision_explicit_assignment_wins(ctx, probe_counter):
    _grant(ctx, "vision-key", tags=["vision"], model="gpt-vision")
    _grant(ctx, "pinned-vision", tags=["main-loop"], model="gpt-pinned-vision")
    _assign(ctx.conn, "vision", "pinned-vision", "gpt-pinned-vision")
    adapter = await ctx.build_vision_adapter()
    assert adapter is not None
    assert (adapter.key_id, adapter.model) == ("pinned-vision", "gpt-pinned-vision")


async def test_vision_explicit_assignment_unavailable_is_recorded(ctx, probe_counter):
    _grant(ctx, "vision-key", tags=["vision"], model="gpt-vision")
    _grant(ctx, "pinned-vision", tags=["main-loop"], model="gpt-pinned-vision")
    ctx.credentials.set_enabled("pinned-vision", False)
    _assign(ctx.conn, "vision", "pinned-vision", "gpt-pinned-vision")
    assert await ctx.build_vision_adapter() is None
    assert ctx.usage_unavailable["vision"].reason_code == REASON_EXPLICIT_UNAVAILABLE


# -- 后台三类：各自独立 --------------------------------------------------------


async def test_derived_adapters_are_independent_per_usage(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "mem-key", model="gpt-mem")
    _grant(ctx, "know-key", model="gpt-know")
    _grant(ctx, "ent-key", model="gpt-ent")
    _assign(ctx.conn, "memory-organize", "mem-key", "gpt-mem")
    _assign(ctx.conn, "knowledge-distill", "know-key", "gpt-know")
    _assign(ctx.conn, "entity-extract", "ent-key", "gpt-ent")

    adapters = await ctx.build_derived_adapters()

    assert adapters["summary"].key_id == "mem-key"
    assert adapters["summary"].model == "gpt-mem"
    assert adapters["knowledge"].key_id == "know-key"
    assert adapters["knowledge"].model == "gpt-know"
    assert adapters["entities"].key_id == "ent-key"
    assert adapters["entities"].model == "gpt-ent"


async def test_derived_adapters_one_unavailable_does_not_affect_others(ctx, probe_counter):
    _grant(ctx, "mem-key", model="gpt-mem")
    _grant(ctx, "know-key", model="gpt-know")
    ctx.credentials.set_enabled("know-key", False)
    _assign(ctx.conn, "knowledge-distill", "know-key", "gpt-know")

    adapters = await ctx.build_derived_adapters()

    assert adapters["summary"].key_id == "mem-key"  # 跟随默认/政策，照常可用
    assert isinstance(adapters["knowledge"], UsageUnavailable)
    assert adapters["knowledge"].reason_code == REASON_EXPLICIT_UNAVAILABLE
    assert adapters["entities"].key_id == "mem-key"


async def test_drain_dispatches_per_kind_adapter(ctx, monkeypatch):
    """派生任务按 kind 拿自己的 adapter（不是同一个 adapter 传下去）。"""
    from agent.services import derived_tasks

    class _Task:
        def __init__(self, kind: str) -> None:
            self.kind = kind
            self.id = f"task_{kind}"
            self.fragment_id = "frag-1"
            self.content_version = 1
            self.claim_generation = 1

    tasks = [_Task(derived_tasks.KIND_SUMMARY), _Task(derived_tasks.KIND_KNOWLEDGE)]

    def _claim(conn, limit, kinds):
        # 认领即出队：真实实现会把任务标成 running，第二次 drain 不该再看到它
        taken = [t for t in tasks if t.kind in kinds]
        tasks[:] = [t for t in tasks if t not in taken]
        return taken

    monkeypatch.setattr(derived_tasks, "claim_due", _claim)
    monkeypatch.setattr(
        ctx.memory_lifecycle, "backfill_entity_tasks", lambda limit=2: 0
    )

    used: list[tuple[str, object]] = []

    async def summary_runner(task, adapter, *, tracer=None):
        used.append((task.kind, adapter))
        return True

    async def knowledge_runner(task, adapter, *, tracer=None):
        used.append((task.kind, adapter))
        return True

    monkeypatch.setattr(ctx.memory_lifecycle, "run_summary_task", summary_runner)
    monkeypatch.setattr(ctx.memory_lifecycle, "run_knowledge_task", knowledge_runner)

    mem_adapter = object()
    know_adapter = object()
    done = await ctx.memory_lifecycle.drain_derived_tasks(
        None,
        adapters={"summary": mem_adapter, "knowledge": know_adapter},
        limit=5,
    )

    assert done == 2
    assert ("summary", mem_adapter) in used
    assert ("knowledge", know_adapter) in used


async def test_drain_fails_task_honestly_when_usage_unavailable(ctx, monkeypatch):
    """某个后台用途不可用 → 这条任务如实失败（写原因），不借用主对话模型。"""
    from agent.services import derived_tasks

    class _Task:
        kind = derived_tasks.KIND_ENTITIES
        id = "task_entities"
        fragment_id = "frag-1"
        content_version = 1
        claim_generation = 7

    failures: list[tuple[str, str, int]] = []
    pending = [_Task()]

    def _claim(conn, limit, kinds):
        taken = [t for t in pending if t.kind in kinds]
        pending[:] = [t for t in pending if t not in taken]
        return taken

    monkeypatch.setattr(derived_tasks, "claim_due", _claim)
    monkeypatch.setattr(derived_tasks, "fail", lambda conn, task_id, reason, expected_generation: failures.append((task_id, reason, expected_generation)))
    monkeypatch.setattr(
        ctx.memory_lifecycle, "backfill_entity_tasks", lambda limit=2: 0
    )
    called: list[str] = []

    async def entities_runner(task, adapter, *, tracer=None):
        called.append(task.kind)
        return True

    monkeypatch.setattr(ctx.memory_lifecycle, "run_entities_task", entities_runner)

    unavailable = UsageUnavailable(
        "entity-extract", REASON_EXPLICIT_UNAVAILABLE, "实体提炼已显式指定的凭据现在不可用"
    )
    await ctx.memory_lifecycle.drain_derived_tasks(
        None, adapters={"entities": unavailable}, limit=5
    )

    assert called == [], "用途不可用时不应该真的调用模型"
    assert failures, "必须把这条任务写成可读失败"
    assert failures[0][0] == "task_entities"
    assert REASON_EXPLICIT_UNAVAILABLE in failures[0][1]


def test_main_loop_usage_tags_constant_is_shared():
    """主对话候选标签只有一处定义（policy.py），app.py 导出的是同一批。"""
    from agent.credentials.policy import MAIN_LOOP_USAGE_TAGS as POLICY_TAGS
    from agent.services.app import MAIN_LOOP_USAGE_TAGS as APP_TAGS

    assert APP_TAGS == POLICY_TAGS


# -- 后台维护路径（知识维护批次 / 维护问答）的兼容规则 --------------------------


async def test_background_adapter_uses_usage_assignment(ctx, probe_counter):
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "know-key", model="gpt-know")
    _assign(ctx.conn, "knowledge-distill", "know-key", "gpt-know")

    adapter = await ctx.build_background_adapter("knowledge-distill")
    assert adapter is not None
    assert (adapter.key_id, adapter.model) == ("know-key", "gpt-know")


async def test_background_adapter_without_any_credential_keeps_baseline(ctx):
    """库里一条凭据都没有（离线/单测注入主 adapter）→ 保持基线的「用主 adapter」。"""
    injected = object()

    async def _fake_build_adapter():
        return injected

    ctx.build_adapter = _fake_build_adapter  # type: ignore[assignment]
    assert await ctx.build_background_adapter("knowledge-distill") is injected


async def test_background_adapter_explicit_unavailable_does_not_fall_back(ctx):
    """有凭据、但显式指定的那把不可用 → 返回 None（调用方如实失败），绝不换主对话模型。"""
    _grant(ctx, "spare-key", model="gpt-spare")
    _grant(ctx, "know-key", model="gpt-know")
    ctx.credentials.set_enabled("know-key", False)
    _assign(ctx.conn, "knowledge-distill", "know-key", "gpt-know")

    assert await ctx.build_background_adapter("knowledge-distill") is None
    assert ctx.usage_unavailable["knowledge-distill"].reason_code == REASON_EXPLICIT_UNAVAILABLE


# -- 嵌入：只给事实，不给「看起来生效」的选择器 --------------------------------


def test_embedding_impact_reports_facts_only(ctx):
    from agent.services.usage_target import describe_embedding_impact

    facts = describe_embedding_impact(
        ctx.credentials, ctx.settings_store, backend_name="onnx:bge-small-zh-v1.5", backend_dimensions=512
    )
    assert facts["usage_key"] == "embedding"
    assert facts["follows_existing"] is True
    assert facts["assignment"] is None
    assert facts["reindex_required"] is False
    assert facts["dimensions"] == 512  # 当前真实维度，不是猜的

    _grant(ctx, "ext-key", model="text-embedding-3-small")
    _assign(ctx.conn, "embedding", "ext-key", "text-embedding-3-small")
    facts = describe_embedding_impact(
        ctx.credentials, ctx.settings_store, backend_name="onnx:bge-small-zh-v1.5"
    )
    assert facts["follows_existing"] is False
    assert facts["assignment"]["model_id"] == "text-embedding-3-small"
    assert facts["reindex_required"] is True


async def test_recovered_usage_clears_stale_unavailable_reason(ctx, probe_counter):
    """用户修好凭据后，旧原因必须消失（否则界面一直挂着过期失败）。"""
    _grant(ctx, "main-key", model="gpt-main")
    _grant(ctx, "pinned-key", model="gpt-pinned")
    ctx.credentials.set_enabled("pinned-key", False)
    _assign(ctx.conn, "main-chat", "pinned-key", "gpt-pinned")

    assert await ctx.build_adapter() is None
    assert "main-chat" in ctx.usage_unavailable

    ctx.credentials.set_enabled("pinned-key", True)
    adapter = await ctx.build_adapter()
    assert adapter is not None and adapter.key_id == "pinned-key"
    assert "main-chat" not in ctx.usage_unavailable
    assert ctx.usage_targets["main-chat"].source == SOURCE_EXPLICIT


async def test_drain_dispatches_entities_to_its_own_adapter(ctx, monkeypatch):
    """实体提炼同样拿到自己的 adapter（三条派生都按 kind 分派，不是只分两条）。"""
    from agent.services import derived_tasks

    class _Task:
        kind = derived_tasks.KIND_ENTITIES
        id = "task_entities"
        fragment_id = "frag-1"
        content_version = 1
        claim_generation = 1

    pending = [_Task()]

    def _claim(conn, limit, kinds):
        taken = [t for t in pending if t.kind in kinds]
        pending[:] = [t for t in pending if t not in taken]
        return taken

    monkeypatch.setattr(derived_tasks, "claim_due", _claim)
    monkeypatch.setattr(ctx.memory_lifecycle, "backfill_entity_tasks", lambda limit=2: 0)
    seen: list[object] = []

    async def entities_runner(task, adapter, *, tracer=None):
        seen.append(adapter)
        return True

    monkeypatch.setattr(ctx.memory_lifecycle, "run_entities_task", entities_runner)
    own = object()
    done = await ctx.memory_lifecycle.drain_derived_tasks(
        object(), adapters={"entities": own}, limit=5
    )
    assert done == 1
    assert seen == [own]
