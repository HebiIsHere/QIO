# -*- coding: utf-8 -*-
"""R16-B / task-2：导航命令身份、重放、参数冲突与事务接缝。

契约：docs/topic-unify-20261111-contract.md §3.1/§3.3/§5/§14（v2 delta，含 §14.1/§14.2/§14.4/§14.5/§14.8）。

真实 FastAPI create_app + 临时 SQLite + TestClient 走真实 HTTP；工具路径用真实 Tool 实例 +
operation_scope 绑定真实 TopicOperation 身份。每个场景同时断言：
* 权威位置事实（ctx.position_state.read()，同一 wire 对象）；
* Anchor 行 / 待接续意图（AnchorService / TurnBindingService）不变；
* 两类版本（navigation_version / position_state.position_version）不变；
* navigation_commands 命令行事实（身份、参数摘要、状态、数量）；
* ANCHOR 事件广播次数与载荷（position/command_id 同源）。

不用 sleep 造时序：TestClient 请求返回时路由已同步写完库/发完事件；工具调用是同步 run_sync。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.memory.fragment import new_id
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.services.topic_operation import (
    IntentSource,
    TopicOperation,
    operation_scope,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 契约 §3.1 冻结的 position wire 字段集（事件/快照/HTTP 结果通用）。
POSITION_FIELDS = {
    "topic_id",
    "topic_name",
    "fragment_id",
    "fragment_title",
    "historic",
    "pending_intent_id",
    "pending_intent_version",
    "pending_source_fragment_id",
    "pending_source_title",
    "nav_version",
    "position_version",
    "position_identity",
}

#: 契约 §5 冻结的导航命令结果字段（HTTP/工具响应均可在既有字段上追加）。
COMMAND_RESULT_FIELDS = {
    "changed",
    "replay",
    "inactive",
    "conflict",
    "code",
    "command_id",
    "nav_version",
    "position_version",
    "position",
}


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """真实 create_app（真实迁移 + 临时 SQLite）的 HTTP 客户端。"""
    conn = connect(tmp_path / "r16_b_nav.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _ctx(client: TestClient) -> AppContext:
    return client.app.state.ctx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _seed_fragment(
    ctx: AppContext,
    topic_id: str,
    *,
    title: str = "历史片段",
    closed: bool = True,
) -> str:
    """写入一个真实片段（封存的同时写 memory_index 标题行），返回 fragment_id。"""
    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments "
        "(id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (frag_id, topic_id, f"{title} 的摘要", now, now if closed else None),
    )
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (new_id("msg"), frag_id, f"{title} 的历史原文", now),
    )
    if closed:
        ctx.conn.execute(
            "INSERT INTO memory_index "
            "(id, fragment_id, topic_id, entity_ids, keywords, title, token_estimate, created_at) "
            "VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (new_id("idx"), frag_id, topic_id, title, now),
        )
    ctx.conn.commit()
    return frag_id


def _rid(prefix: str = "req") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# -- 权威事实读取（断言一律看库 + 同源 wire，不只看状态码）------------------


def _pos(ctx: AppContext) -> dict:
    return ctx.position_state.read()


def _nav(ctx: AppContext) -> int:
    return navigation_version.current(ctx.conn)


def _posver(ctx: AppContext) -> int:
    row = ctx.conn.execute(
        "SELECT position_version FROM position_state WHERE id = 1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _anchor(ctx: AppContext):
    return AnchorService(ctx.conn).get_active()


def _anchor_facts(ctx: AppContext) -> tuple[str | None, str | None]:
    anchor = _anchor(ctx)
    return (
        (anchor.topic_id, anchor.fragment_id) if anchor is not None else (None, None)
    )


def _pending(ctx: AppContext):
    return ctx.bindings.peek_intent()


def _command_rows(ctx: AppContext) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT * FROM navigation_commands ORDER BY created_at, command_id"
    ).fetchall()
    return [dict(row) for row in rows]


def _commands_for(ctx: AppContext, identity_key: str) -> list[dict]:
    return [row for row in _command_rows(ctx) if row["identity_key"] == identity_key]


def _anchor_event_count(ctx: AppContext) -> int:
    return sum(1 for event in list(ctx.bus._history) if event.type == EventType.ANCHOR)  # noqa: SLF001


def _anchor_events(ctx: AppContext) -> list[dict]:
    return [
        event.data
        for event in list(ctx.bus._history)  # noqa: SLF001
        if event.type == EventType.ANCHOR
    ]


def _position_facts(ctx: AppContext) -> dict:
    """一次只读快照：完整位置状态 + 两类版本 + 命令行数 + ANCHOR 数。"""
    return {
        "position": _pos(ctx),
        "anchor": _anchor_facts(ctx),
        "pending_intent_id": (
            pending.intent_id if (pending := _pending(ctx)) is not None else None
        ),
        "nav_version": _nav(ctx),
        "position_version": _posver(ctx),
        "command_count": len(_command_rows(ctx)),
        "anchor_events": _anchor_event_count(ctx),
    }


def _business_facts(ctx: AppContext) -> dict:
    """业务副作用快照（不含命令行审计数量）：位置/Anchor/pending/两类版本/广播。"""
    facts = _position_facts(ctx)
    facts.pop("command_count", None)
    return facts


def _assert_zero_write(ctx: AppContext, before: dict) -> None:
    assert _position_facts(ctx) == before


def _base(ctx: AppContext) -> int:
    return navigation_version.current(ctx.conn)


def _post_anchor(
    client: TestClient,
    ctx: AppContext,
    *,
    request_id: str | None = None,
    base_nav_version: int | None = None,
    **payload,
):
    body = dict(payload)
    if request_id is not None:
        body["request_id"] = request_id
    if base_nav_version is not None:
        body["base_nav_version"] = base_nav_version
    return client.post("/api/anchor", json=body)


def _continue(
    client: TestClient,
    ctx: AppContext,
    topic_id: str,
    fragment_id: str,
    *,
    request_id: str | None = None,
    base_nav_version: int | None = None,
):
    return _post_anchor(
        client,
        ctx,
        request_id=request_id,
        base_nav_version=base_nav_version,
        topic_id=topic_id,
        fragment_id=fragment_id,
        continue_from_history=True,
    )


def _enter(
    client: TestClient,
    ctx: AppContext,
    topic_id: str,
    *,
    request_id: str | None = None,
    base_nav_version: int | None = None,
    fragment_id: str | None = None,
):
    return _post_anchor(
        client,
        ctx,
        request_id=request_id,
        base_nav_version=base_nav_version,
        topic_id=topic_id,
        fragment_id=fragment_id,
    )


def _cancel(
    client: TestClient,
    ctx: AppContext,
    *,
    request_id: str | None = None,
    base_nav_version: int | None = None,
):
    body = {}
    if request_id is not None:
        body["request_id"] = request_id
    if base_nav_version is not None:
        body["base_nav_version"] = base_nav_version
    return client.post("/api/anchor/continue/cancel", json=body)


def _assert_command_result_fields(body: dict) -> None:
    assert COMMAND_RESULT_FIELDS <= set(body), sorted(COMMAND_RESULT_FIELDS - set(body))
    assert isinstance(body["position"], dict)
    assert POSITION_FIELDS <= set(body["position"]), sorted(
        POSITION_FIELDS - set(body["position"])
    )


# ---------------------------------------------------------------------------
# §3.1：runtime 快照 / 事件 / HTTP 命令结果同源 wire
# ---------------------------------------------------------------------------


def test_runtime_anchor_is_complete_position_wire(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "快照话题")
    historic = _seed_fragment(ctx, topic, title="历史A")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)
    response = _continue(
        client,
        ctx,
        topic,
        historic,
        request_id="req-snapshot",
        base_nav_version=_base(ctx),
    )
    assert response.status_code == 200, response.text

    anchor = client.get("/api/runtime/state").json()["anchor"]
    assert set(anchor) == POSITION_FIELDS, (
        "契约 §3.1：runtime anchor 必须等于完整 position wire（不得多字段/少字段）"
    )
    assert anchor == _pos(ctx), "runtime anchor 必须来自 ctx.position_state.read() 同源读取"
    assert isinstance(anchor["position_version"], int)
    assert anchor["position_identity"]

    latest = _anchor_events(ctx)[-1]
    assert latest.get("position") == _pos(ctx), "ANCHOR 事件必须携带同源完整 position"
    assert latest.get("position_version") == anchor["position_version"]
    assert latest.get("position_identity") == anchor["position_identity"]
    assert latest.get("command_id") == "request:req-snapshot"
    for key in (
        "topic_id",
        "fragment_id",
        "historic",
        "nav_version",
        "position_version",
        "pending_intent_id",
    ):
        assert latest.get(key) == anchor[key], key


# ---------------------------------------------------------------------------
# §5 / §14.2：手动导航请求身份 + 依据 + 重放
# ---------------------------------------------------------------------------


def test_enter_topic_request_replay_is_zero_write(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "重放话题")
    _seed_fragment(ctx, topic, title="开放", closed=False)

    base = _base(ctx)
    first = _enter(client, ctx, topic, request_id="req-enter", base_nav_version=base)
    assert first.status_code == 200, first.text
    body = first.json()
    _assert_command_result_fields(body)
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["replay"] is False
    assert body["inactive"] is False
    assert body["conflict"] is False
    assert body["code"] is None
    assert body["command_id"] == "request:req-enter"
    assert body["position"]["topic_id"] == topic
    assert body["nav_version"] == base + 1
    assert _anchor_facts(ctx) == (topic, body["position"]["fragment_id"])
    assert _nav(ctx) == base + 1
    assert len(_commands_for(ctx, "request:req-enter")) == 1
    row = _commands_for(ctx, "request:req-enter")[0]
    assert row["identity_kind"] == "request"
    assert row["command_kind"] == "enter_topic"
    assert row["status"] == "applied"
    assert row["nav_version"] == body["nav_version"]
    assert row["position_version"] == body["position_version"]
    assert _anchor_event_count(ctx) == 1

    before = _position_facts(ctx)
    replay = _enter(client, ctx, topic, request_id="req-enter", base_nav_version=base)
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    _assert_command_result_fields(replay_body)
    assert replay_body["ok"] is True
    assert replay_body["replay"] is True
    assert replay_body["changed"] is False
    assert replay_body["conflict"] is False
    assert replay_body["command_id"] == "request:req-enter"
    assert replay_body["position"] == _pos(ctx), "重放返回当前只读 position"
    assert replay_body["nav_version"] == before["nav_version"]
    _assert_zero_write(ctx, before)


def test_same_request_id_different_params_is_conflict_without_writes(client: TestClient):
    ctx = _ctx(client)
    topic_a = _topic(ctx, "请求A")
    topic_b = _topic(ctx, "请求B")
    first = _enter(client, ctx, topic_a, request_id="req-shared", base_nav_version=_base(ctx))
    assert first.status_code == 200, first.text
    before = _position_facts(ctx)

    conflict = _enter(client, ctx, topic_b, request_id="req-shared", base_nav_version=_base(ctx))
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["ok"] is False
    assert body["conflict"] is True
    assert body["changed"] is False
    assert body["replay"] is False
    assert body["code"] == "navigation_command_conflict"
    assert body["command_id"] == "request:req-shared"
    assert body["position"] == _pos(ctx)
    # I05/§14.4：零写入、不新增行。
    _assert_zero_write(ctx, before)


def test_manual_navigation_requires_request_identity_and_basis(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "依据话题")

    missing_id = _enter(client, ctx, topic, base_nav_version=_base(ctx))
    assert missing_id.status_code == 400, missing_id.text
    assert missing_id.json()["code"] == "navigation_request_id_required"

    missing_base = _post_anchor(
        client, ctx, request_id="req-no-base", topic_id=topic
    )
    assert missing_base.status_code == 400, missing_base.text
    assert missing_base.json()["code"] == "navigation_basis_required"

    before = _business_facts(ctx)
    stale = _enter(
        client,
        ctx,
        topic,
        request_id="req-stale",
        base_nav_version=_base(ctx) + 5,
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["code"] == "navigation_command_stale"
    assert body["inactive"] is True
    assert body["command_id"] == "request:req-stale"
    assert body["position"] == _pos(ctx)
    # 零业务副作用；§14.4/§14.8：首次被拒的身份允许（并要求）落一行拒绝事实供重放。
    assert _business_facts(ctx) == before
    rows = _commands_for(ctx, "request:req-stale")
    assert len(rows) == 1
    assert rows[0]["status"] == "superseded"


# ---------------------------------------------------------------------------
# §10.2-D / §5：开放片段 A → 历史 B → 重放原请求 A：B 保持不变
# ---------------------------------------------------------------------------


def test_open_fragment_then_history_then_replay_keeps_newer_selection(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "D 场景")
    open_fragment = _seed_fragment(ctx, topic, title="开放A", closed=False)
    historic = _seed_fragment(ctx, topic, title="历史B")

    first = _enter(
        client,
        ctx,
        topic,
        fragment_id=open_fragment,
        request_id="req-open",
        base_nav_version=_base(ctx),
    )
    assert first.status_code == 200, first.text
    assert first.json()["changed"] is True
    assert first.json()["command_id"] == "request:req-open"
    assert _anchor_facts(ctx) == (topic, open_fragment)
    assert _anchor_event_count(ctx) == 1
    x = _commands_for(ctx, "request:req-open")[0]
    assert x["status"] == "applied"
    assert x["command_kind"] == "open_fragment"

    second = _continue(
        client,
        ctx,
        topic,
        historic,
        request_id="req-hist",
        base_nav_version=_base(ctx),
    )
    assert second.status_code == 200, second.text
    assert second.json()["changed"] is True
    pending = _pending(ctx)
    assert pending is not None and pending.source_fragment_id == historic
    assert _anchor_facts(ctx) == (topic, historic)
    assert _anchor_event_count(ctx) == 2

    before = _position_facts(ctx)
    replay = _enter(
        client,
        ctx,
        topic,
        fragment_id=open_fragment,
        request_id="req-open",
        base_nav_version=_base(ctx),
    )
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["replay"] is True
    assert body["changed"] is False
    assert body["position"] == _pos(ctx)
    # 用户后来的选择（历史 B + pending）原样保留；旧请求重放不得把位置改回 A。
    assert _anchor_facts(ctx) == (topic, historic)
    assert _pending(ctx).source_fragment_id == historic
    assert body["position"]["fragment_id"] == historic
    assert body["position"]["historic"] is True
    _assert_zero_write(ctx, before)


def test_open_fragment_cancels_stale_pending_intent(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "取消旧接续")
    open_fragment = _seed_fragment(ctx, topic, title="开放", closed=False)
    historic = _seed_fragment(ctx, topic, title="历史")

    assert _continue(
        client, ctx, topic, historic, request_id="req-h", base_nav_version=_base(ctx)
    ).status_code == 200
    assert _pending(ctx) is not None

    resp = _enter(
        client,
        ctx,
        topic,
        fragment_id=open_fragment,
        request_id="req-o",
        base_nav_version=_base(ctx),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["position"]["pending_intent_id"] is None
    assert _pending(ctx) is None, "契约 §3.3：open_fragment 结束不再适用的旧 registered"
    assert _anchor_facts(ctx) == (topic, open_fragment)


# ---------------------------------------------------------------------------
# §5：取消后重放 / 已失效请求
# ---------------------------------------------------------------------------


def test_cancel_then_replay_is_inactive_and_replay_of_cancel_is_idempotent(
    client: TestClient,
):
    ctx = _ctx(client)
    topic = _topic(ctx, "取消场景")
    historic = _seed_fragment(ctx, topic, title="历史A")
    _seed_fragment(ctx, topic, title="开放", closed=False)

    selected = _continue(
        client, ctx, topic, historic, request_id="req-sel", base_nav_version=_base(ctx)
    )
    assert selected.status_code == 200, selected.text
    intent = _pending(ctx)
    assert intent is not None

    before_cancel = _position_facts(ctx)
    cancelled = _cancel(
        client, ctx, request_id="req-cancel", base_nav_version=_base(ctx)
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    _assert_command_result_fields(body)
    assert body["ok"] is True
    assert body["cancelled"] is True
    assert body["changed"] is True
    assert body["command_id"] == "request:req-cancel"
    assert _pending(ctx) is None
    # §14.9：取消不是新的导航选择，nav_version 不推进；pending 变化 → position_version +1 + 一次广播。
    assert _nav(ctx) == before_cancel["nav_version"]
    assert _posver(ctx) == before_cancel["position_version"] + 1
    assert _anchor_event_count(ctx) == before_cancel["anchor_events"] + 1
    row = _commands_for(ctx, "request:req-cancel")[0]
    assert row["command_kind"] == "cancel_continuation"
    assert row["status"] == "applied"
    assert row["nav_version"] == _nav(ctx)

    before = _position_facts(ctx)
    replay_selected = _continue(
        client,
        ctx,
        topic,
        historic,
        request_id="req-sel",
        base_nav_version=_base(ctx),
    )
    assert replay_selected.status_code == 409, replay_selected.text
    inactive_body = replay_selected.json()
    assert inactive_body["ok"] is False
    assert inactive_body["inactive"] is True
    assert inactive_body["code"] == "continuation_request_inactive"
    assert inactive_body["state"] == "cancelled"
    assert inactive_body["position"] == _pos(ctx)
    _assert_zero_write(ctx, before)

    replay_cancel = _cancel(
        client, ctx, request_id="req-cancel", base_nav_version=_base(ctx)
    )
    assert replay_cancel.status_code == 200, replay_cancel.text
    replay_body = replay_cancel.json()
    assert replay_body["replay"] is True
    assert replay_body["changed"] is False
    assert replay_body["cancelled"] is True
    assert replay_body["position"] == _pos(ctx)
    _assert_zero_write(ctx, before)

    # 新的取消请求：没有可取消的 registered → 真实新命令但零变化，留行、零版本、零广播。
    noop = _cancel(client, ctx, request_id="req-cancel-2", base_nav_version=_base(ctx))
    assert noop.status_code == 200, noop.text
    noop_body = noop.json()
    assert noop_body["cancelled"] is False
    assert noop_body["changed"] is False
    assert noop_body["replay"] is False
    after = _position_facts(ctx)
    assert after["nav_version"] == before["nav_version"]
    assert after["position_version"] == before["position_version"]
    assert after["anchor_events"] == before["anchor_events"]
    assert len(_commands_for(ctx, "request:req-cancel-2")) == 1


def test_replaced_continuation_request_replay_is_inactive(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "替换场景")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    hist_b = _seed_fragment(ctx, topic, title="历史B")
    _seed_fragment(ctx, topic, title="开放", closed=False)

    assert _continue(
        client, ctx, topic, hist_a, request_id="req-a", base_nav_version=_base(ctx)
    ).status_code == 200
    assert _continue(
        client, ctx, topic, hist_b, request_id="req-b", base_nav_version=_base(ctx)
    ).status_code == 200
    intent_b = _pending(ctx)
    assert intent_b is not None and intent_b.source_fragment_id == hist_b

    before = _position_facts(ctx)
    replay = _continue(
        client, ctx, topic, hist_a, request_id="req-a", base_nav_version=_base(ctx)
    )
    assert replay.status_code == 409, replay.text
    body = replay.json()
    assert body["inactive"] is True
    assert body["code"] == "continuation_request_inactive"
    assert body["state"] == "replaced"
    assert _anchor_facts(ctx) == (topic, hist_b)
    assert _pending(ctx).source_fragment_id == hist_b
    _assert_zero_write(ctx, before)


def test_historic_fragment_without_continuation_is_rejected(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "封存片段")
    historic = _seed_fragment(ctx, topic, title="历史A")
    before = _position_facts(ctx)

    resp = _post_anchor(
        client,
        ctx,
        request_id="req-hist-enter",
        base_nav_version=_base(ctx),
        topic_id=topic,
        fragment_id=historic,
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "historic_fragment_requires_continuation"
    _assert_zero_write(ctx, before)


# ---------------------------------------------------------------------------
# §5/§14.5：工具 operation 身份（switch / create / continue）
# ---------------------------------------------------------------------------


def _switch_run(ctx: AppContext, operation: TopicOperation, topic_id: str):
    from agent.tools.topic_tools import SwitchTopicTool

    with operation_scope(operation):
        return SwitchTopicTool(ctx.conn).run_sync(topic_id=topic_id)


def _create_run(ctx: AppContext, operation: TopicOperation, name: str):
    from agent.tools.topic_tools import CreateTopicTool

    with operation_scope(operation):
        return asyncio.run(CreateTopicTool(ctx.conn).run(name=name))


def _continue_tool_run(ctx: AppContext, operation: TopicOperation, fragment_id: str):
    from agent.tools.continue_tool import ContinueFromFragmentTool

    with operation_scope(operation):
        return ContinueFromFragmentTool(ctx.conn).run_sync(fragment_id=fragment_id)


def _switch_operation(
    ctx: AppContext,
    *,
    operation_id: str = "topop-r16b-switch",
    topic_id: str,
    from_topic: str | None = None,
    base_nav_version: int | None = None,
) -> TopicOperation:
    return TopicOperation(
        operation_id=operation_id,
        turn_id="turn-r16b",
        kind="topic_switch",
        intent_source=IntentSource.USER_REQUEST,
        params={"topic_id": topic_id},
        route="direct",
        from_topic=from_topic,
        base_nav_version=(
            navigation_version.current(ctx.conn)
            if base_nav_version is None
            else base_nav_version
        ),
    )


def test_tool_switch_operation_identity_replay_and_broadcast(client: TestClient):
    ctx = _ctx(client)
    topic_a = _topic(ctx, "工具当前")
    topic_b = _topic(ctx, "工具目标")
    _seed_fragment(ctx, topic_a, title="A 开放", closed=False)
    _seed_fragment(ctx, topic_b, title="B 开放", closed=False)
    anchor = AnchorService(ctx.conn)
    anchor.set_active(topic_a, None)
    ctx.conn.commit()

    operation = _switch_operation(ctx, topic_id=topic_b, from_topic=topic_a)
    first = _switch_run(ctx, operation, topic_b)
    assert first.ok is True, first.error
    facts = dict(first.facts or {})
    assert facts["changed"] is True
    assert facts["replay"] is False
    assert facts["command_id"] == "operation:topop-r16b-switch:1"
    _assert_command_result_fields(facts)
    assert facts["position"]["topic_id"] == topic_b
    assert _anchor_facts(ctx) == (topic_b, facts["position"]["fragment_id"])
    assert _nav(ctx) == operation.base_nav_version + 1
    rows = _commands_for(ctx, "operation:topop-r16b-switch:1")
    assert len(rows) == 1
    assert rows[0]["identity_kind"] == "operation"
    assert rows[0]["command_kind"] == "enter_topic"
    assert rows[0]["status"] == "applied"
    assert rows[0]["operation_id"] == "topop-r16b-switch"
    assert int(rows[0]["param_version"]) == 1

    # 真实成功由调用方（app 事件钩子）发布一次 ANCHOR；replay 不得重复广播。
    asyncio.run(
        ctx._on_tool_anchor_result({"tool": "switch_topic", "result": first})
    )
    assert _anchor_event_count(ctx) == 1
    published = _anchor_events(ctx)[-1]
    assert published.get("command_id") == facts["command_id"]
    assert published.get("position") == _pos(ctx)

    before = _position_facts(ctx)
    replay = _switch_run(ctx, operation, topic_b)
    assert replay.ok is True, replay.error
    replay_facts = dict(replay.facts or {})
    assert replay_facts["replay"] is True
    assert replay_facts["changed"] is False
    assert replay_facts["command_id"] == facts["command_id"]
    assert replay_facts["position"] == _pos(ctx)
    _assert_zero_write(ctx, before)

    asyncio.run(
        ctx._on_tool_anchor_result({"tool": "switch_topic", "result": replay})
    )
    assert _anchor_event_count(ctx) == 1, "replay 不得重复广播 ANCHOR"


def test_tool_switch_cas_conflict_is_reported_without_writes(client: TestClient):
    ctx = _ctx(client)
    topic_a = _topic(ctx, "CAS 当前")
    topic_b = _topic(ctx, "CAS 目标")
    anchor = AnchorService(ctx.conn)
    anchor.set_active(topic_a, None)
    ctx.conn.commit()

    stale_version = navigation_version.current(ctx.conn)
    operation = _switch_operation(
        ctx, topic_id=topic_b, from_topic=topic_a, base_nav_version=stale_version
    )
    # 起点快照之后用户又导航了：CAS 必须整体拒绝，Anchor/版本零变化。
    ctx.navigation.enter_topic(topic_b)
    before = _position_facts(ctx)

    result = _switch_run(ctx, operation, topic_b)
    assert result.ok is False
    assert result.code == "nav_version_conflict"
    assert _anchor_facts(ctx) == (topic_b, None)
    after = _position_facts(ctx)
    assert after["nav_version"] == before["nav_version"]
    assert after["position_version"] == before["position_version"]
    assert after["anchor_events"] == before["anchor_events"]


def test_tool_create_operation_identity_replay(client: TestClient):
    ctx = _ctx(client)
    operation = TopicOperation(
        operation_id="topop-r16b-create",
        turn_id="turn-r16b",
        kind="topic_create",
        intent_source=IntentSource.USER_REQUEST,
        params={"name": "R16B 新话题"},
        route="direct",
        base_nav_version=navigation_version.current(ctx.conn),
    )
    nodes_before = ctx.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    first = _create_run(ctx, operation, "R16B 新话题")
    assert first.ok is True, first.error
    facts = dict(first.facts or {})
    assert facts["changed"] is True
    assert facts["replay"] is False
    assert facts["command_id"] == "operation:topop-r16b-create:1"
    _assert_command_result_fields(facts)
    created_topic = facts["position"]["topic_id"]
    assert facts["topic_id"] == created_topic
    rows = _commands_for(ctx, "operation:topop-r16b-create:1")
    assert len(rows) == 1
    assert rows[0]["command_kind"] == "create_topic"
    assert rows[0]["status"] == "applied"

    before = _position_facts(ctx)
    replay = _create_run(ctx, operation, "R16B 新话题")
    assert replay.ok is True, replay.error
    replay_facts = dict(replay.facts or {})
    assert replay_facts["replay"] is True
    assert replay_facts["changed"] is False
    assert replay_facts["position"] == _pos(ctx)
    _assert_zero_write(ctx, before)
    topic_count = ctx.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    assert topic_count == nodes_before + 1, "replay must not create the topic again"


def test_tool_continue_registers_pending_without_moving_anchor(client: TestClient):
    ctx = _ctx(client)
    topic_a = _topic(ctx, "工具当前话题")
    topic_b = _topic(ctx, "工具历史话题")
    historic = _seed_fragment(ctx, topic_b, title="要接续的历史")
    _seed_fragment(ctx, topic_b, title="B 当前开放", closed=False)
    anchor = AnchorService(ctx.conn)
    anchor.set_active(topic_a, None)
    ctx.conn.commit()

    operation = TopicOperation(
        operation_id="topop-r16b-continue",
        turn_id="turn-r16b",
        kind="history_continue",
        intent_source=IntentSource.USER_REQUEST,
        params={"fragment_id": historic},
        route="direct",
        from_topic=topic_a,
        base_nav_version=navigation_version.current(ctx.conn),
    )
    first = _continue_tool_run(ctx, operation, historic)
    assert first.ok is True, first.error
    facts = dict(first.facts or {})
    assert facts["changed"] is True
    assert facts["replay"] is False
    assert facts["command_id"] == "operation:topop-r16b-continue:1"
    _assert_command_result_fields(facts)
    # §14.5：工具登记路径只登记/替换 pending，不动 Anchor。
    assert _anchor_facts(ctx) == (topic_a, None)
    pending = _pending(ctx)
    assert pending is not None
    assert (pending.topic_id, pending.source_fragment_id) == (topic_b, historic)
    assert facts["position"]["pending_intent_id"] == pending.intent_id
    assert facts["position"]["pending_source_fragment_id"] == historic
    rows = _commands_for(ctx, "operation:topop-r16b-continue:1")
    assert len(rows) == 1
    assert rows[0]["command_kind"] == "select_history"
    assert rows[0]["status"] == "applied"

    before = _position_facts(ctx)
    replay = _continue_tool_run(ctx, operation, historic)
    assert replay.ok is True, replay.error
    replay_facts = dict(replay.facts or {})
    assert replay_facts["replay"] is True
    assert replay_facts["changed"] is False
    _assert_zero_write(ctx, before)

    # 工具 continue 落在开放片段：open_fragment 身份，仍不搬 Anchor。
    topic_b_open = AnchorService(ctx.conn).open_fragment_id(topic_b)
    assert topic_b_open is not None
    open_op = TopicOperation(
        operation_id="topop-r16b-continue-open",
        turn_id="turn-r16b-2",
        kind="history_continue",
        intent_source=IntentSource.USER_REQUEST,
        params={"fragment_id": topic_b_open},
        route="direct",
        from_topic=topic_a,
        base_nav_version=navigation_version.current(ctx.conn),
    )
    opened = _continue_tool_run(ctx, open_op, topic_b_open)
    assert opened.ok is True, opened.error
    opened_facts = dict(opened.facts or {})
    assert opened_facts["command_id"] == "operation:topop-r16b-continue-open:1"
    # §14.4：工具 continue 返回开放片段 = open_fragment（真实导航：Anchor 落到该开放片段）。
    assert _anchor_facts(ctx) == (topic_b, topic_b_open)
    assert _commands_for(ctx, "operation:topop-r16b-continue-open:1")[0][
        "command_kind"
    ] == "open_fragment"
    assert _pending(ctx) is None, "工具 continue 回当前开放片段：结束不再适用的旧 registered"


def test_apply_conflict_same_identity_different_params_never_writes(client: TestClient):
    """I05 服务层/公开入口：同身份不同参数 → conflict，零写入、不新增行。"""
    ctx = _ctx(client)
    from agent.services.position_state import (
        NavCommandIdentity,
        PositionTransition,
    )

    topic_a = _topic(ctx, "I05 A")
    topic_b = _topic(ctx, "I05 B")
    identity = NavCommandIdentity(
        kind="operation",
        identity_key="operation:topop-i05:1",
        request_id=None,
        operation_id="topop-i05",
        param_version=1,
        internal_scope=None,
    )
    first = ctx.position_state.apply(
        PositionTransition(
            kind="enter_topic",
            source="test.i05",
            identity=identity,
            initiator="tool",
            target_topic_id=topic_a,
            target_fragment_id=None,
            nav_decision=True,
            operation_id="topop-i05",
            param_version=1,
        )
    )
    assert first.status == "applied"
    before = _position_facts(ctx)
    commands_before = len(_command_rows(ctx))

    conflict = ctx.position_state.apply(
        PositionTransition(
            kind="enter_topic",
            source="test.i05",
            identity=identity,
            initiator="tool",
            target_topic_id=topic_b,
            target_fragment_id=None,
            nav_decision=True,
            operation_id="topop-i05",
            param_version=1,
        )
    )
    assert conflict.status == "conflict"
    assert conflict.conflict is True
    assert conflict.code == "navigation_command_conflict"
    assert conflict.command_id == identity.identity_key
    assert conflict.position == _pos(ctx)
    assert len(_command_rows(ctx)) == commands_before, "I05 不得新增行"
    _assert_zero_write(ctx, before)


# ---------------------------------------------------------------------------
# §14.8 / 审批接缝：工具的事实经 APPROVAL_RESULT 透传 position/changed
# ---------------------------------------------------------------------------


def test_approval_settle_forwards_navigation_facts():
    from agent.tools.approval_results import begin_collection, register_approved, settle
    from agent.tools.base import ToolResult

    class FakeApprovals:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def record_legacy_execution(self, approval_id, **kwargs):
            self.calls.append({"approval_id": approval_id, **kwargs})
            return {"approval_id": approval_id, **kwargs}

    approval_id = "appr-r16b"
    position = {
        "topic_id": "topic-x",
        "fragment_id": None,
        "historic": False,
        "nav_version": 3,
        "position_version": 5,
        "position_identity": "db-test",
    }
    token = begin_collection()
    try:
        register_approved(approval_id)
        result = ToolResult(
            ok=True,
            content="ok",
            facts={
                "command_id": "operation:topop-x:1",
                "position": position,
                "position_version": 5,
                "nav_version": 3,
                "changed": True,
                "replay": False,
            },
        )
        calls = asyncio.run(settle(FakeApprovals(), result))
    finally:
        from agent.tools.approval_results import reset_collection

        reset_collection(token)
    assert calls, "settle 必须收口已批准审批"
    forwarded = calls[0]
    assert forwarded["navigation"]["command_id"] == "operation:topop-x:1"
    assert forwarded["navigation"]["position"] == position
    assert forwarded["navigation"]["changed"] is True
