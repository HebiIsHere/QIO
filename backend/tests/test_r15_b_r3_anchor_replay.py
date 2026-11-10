# -*- coding: utf-8 -*-
"""R15-B / R3（P1）：历史旧请求不得绕过服务层的失效结果。

契约：docs/topic-review-r1r4-contract-20261111.md §5（R3）、§4.2(b)（/api/runtime/state 的 anchor 快照）。

真实 FastAPI create_app + 临时 SQLite + TestClient 走真实 HTTP；每个场景同时断言：
* 权威 Anchor 行（AnchorService.get_active）；
* 待落实接续来源（TurnBindingService.peek_intent）；
* 每一条接续意图的真实状态（intent_by_request / intent_by_id）；
* 导航版本（navigation_version.current）；
* ANCHOR 事件广播次数（幂等重放 / 失效请求必须零广播）；
* 片段数量（点击路径绝不创建片段）。

不用 sleep 造时序：TestClient 的请求返回时路由已经写完库/发完事件，断言是同步事实。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.memory.fragment import new_id
from agent.services import navigation_version
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

#: 契约 §4.2(b) 冻结的 anchor 快照字段（顶层另有 instance_id）。
RUNTIME_ANCHOR_FIELDS = {
    "topic_id",
    "topic_name",
    "fragment_id",
    "fragment_title",
    "historic",
    "nav_version",
    "pending_intent_id",
    "pending_intent_version",
    "pending_source_fragment_id",
    "pending_source_title",
}


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """真实 create_app（真实迁移 + 临时 SQLite）的 HTTP 客户端。"""
    conn = connect(tmp_path / "r15_b_r3.db")
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
    """写入一个真实片段（已封存的同时写 memory_index 标题行），返回 fragment_id。"""
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


def _continue(
    client: TestClient,
    topic_id: str,
    fragment_id: str,
    *,
    request_id: str | None = None,
):
    payload: dict = {
        "topic_id": topic_id,
        "fragment_id": fragment_id,
        "continue_from_history": True,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    return client.post("/api/anchor", json=payload)


# -- 权威事实读取（断言一律看库，不只看状态码）------------------------------


def _anchor(ctx: AppContext):
    return AnchorService(ctx.conn).get_active()


def _anchor_fragment(ctx: AppContext) -> str | None:
    anchor = _anchor(ctx)
    return anchor.fragment_id if anchor is not None else None


def _anchor_topic(ctx: AppContext) -> str | None:
    anchor = _anchor(ctx)
    return anchor.topic_id if anchor is not None else None


def _pending(ctx: AppContext):
    return ctx.bindings.peek_intent()


def _pending_source(ctx: AppContext) -> str | None:
    pending = _pending(ctx)
    return pending.source_fragment_id if pending is not None else None


def _intent_state(ctx: AppContext, request_id: str) -> str | None:
    intent = ctx.bindings.intent_by_request(request_id)
    return intent.state if intent is not None else None


def _nav(ctx: AppContext) -> int:
    return navigation_version.current(ctx.conn)


def _fragment_count(ctx: AppContext) -> int:
    return int(ctx.conn.execute("SELECT COUNT(*) AS c FROM fragments").fetchone()["c"])


def _intent_count(ctx: AppContext) -> int:
    return int(
        ctx.conn.execute("SELECT COUNT(*) AS c FROM continuation_intents").fetchone()["c"]
    )


def _anchor_event_count(ctx: AppContext) -> int:
    # 测试直接读总线历史：TestClient 里再开一条 SSE 会与同步请求共用 portal 死锁。
    return sum(1 for event in list(ctx.bus._history) if event.type == EventType.ANCHOR)  # noqa: SLF001


def _position_facts(ctx: AppContext) -> dict:
    """一次只读快照：Anchor / 待接续 / 导航版本 / 片段数 —— 用来证明「零写入」。"""
    anchor = _anchor(ctx)
    pending = _pending(ctx)
    return {
        "anchor_topic_id": anchor.topic_id if anchor is not None else None,
        "anchor_fragment_id": anchor.fragment_id if anchor is not None else None,
        "anchor_historic": (
            ctx.navigation.anchors.is_historic_position(anchor.topic_id)
            if anchor is not None and anchor.topic_id
            else False
        ),
        "pending_intent_id": pending.intent_id if pending is not None else None,
        "pending_source_fragment_id": pending.source_fragment_id if pending is not None else None,
        "nav_version": _nav(ctx),
        "fragment_count": _fragment_count(ctx),
    }


def _assert_zero_write(ctx: AppContext, before: dict, *, anchor_events: int) -> None:
    """零写入的统一断言：位置/待接续/版本/片段数完全不变，且没有新 ANCHOR 广播。"""
    assert _position_facts(ctx) == before
    assert _anchor_event_count(ctx) == anchor_events


# ---------------------------------------------------------------------------
# §4.2(b)：/api/runtime/state 的权威 anchor 快照（C 的依赖）
# ---------------------------------------------------------------------------


def test_runtime_state_anchor_snapshot_matches_anchor_event_facts(client: TestClient):
    """快照字段必须与 _publish_anchor_event 同源：位置 + 待接续 + 真实导航版本。"""
    ctx = _ctx(client)
    topic = _topic(ctx, "快照话题")
    historic = _seed_fragment(ctx, topic, title="历史A标题")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    # 还没有 Anchor：字段如实为 null/False，不伪造位置。
    fresh = client.get("/api/runtime/state").json()
    assert "anchor" in fresh, "契约 §4.2(b)：/api/runtime/state 必须返回 anchor 对象"
    assert set(fresh["anchor"]) == RUNTIME_ANCHOR_FIELDS
    assert fresh["anchor"]["topic_id"] is None
    assert fresh["anchor"]["fragment_id"] is None
    assert fresh["anchor"]["historic"] is False
    assert isinstance(fresh["anchor"]["nav_version"], int)
    assert fresh["anchor"]["pending_intent_id"] is None

    # 点击历史：登记 + 落位之后，快照必须同时反映位置与待接续事实。
    first = _continue(client, topic, historic, request_id="req-snapshot")
    assert first.status_code == 200, first.text
    intent = ctx.bindings.intent_by_request("req-snapshot")
    assert intent is not None
    anchor = client.get("/api/runtime/state").json()["anchor"]

    assert anchor["topic_id"] == topic
    assert anchor["topic_name"] == "快照话题"
    assert anchor["fragment_id"] == historic
    assert anchor["fragment_title"] == "历史A标题"
    assert anchor["historic"] is True
    assert anchor["nav_version"] == _nav(ctx)
    assert anchor["pending_intent_id"] == intent.intent_id
    assert anchor["pending_intent_version"] == intent.version
    assert anchor["pending_source_fragment_id"] == historic
    assert anchor["pending_source_title"] == "历史A标题"

    # 与最近一次 ANCHOR 事件逐字段一致（同一权威来源，不允许两份事实漂移）。
    event = [e for e in list(ctx.bus._history) if e.type == EventType.ANCHOR][-1]  # noqa: SLF001
    for key in RUNTIME_ANCHOR_FIELDS:
        assert anchor[key] == event.data[key], key

    # 取消待接续：pending 字段清空，位置与版本不变（仅取消动作本身不推进版本）。
    version_before = _nav(ctx)
    cancelled = client.post("/api/anchor/continue/cancel")
    assert cancelled.status_code == 200, cancelled.text
    after_cancel = client.get("/api/runtime/state").json()["anchor"]
    assert after_cancel["pending_intent_id"] is None
    assert after_cancel["pending_intent_version"] is None
    assert after_cancel["pending_source_fragment_id"] is None
    assert after_cancel["pending_source_title"] is None
    assert after_cancel["fragment_id"] == historic
    assert after_cancel["nav_version"] == version_before


# ---------------------------------------------------------------------------
# §5.3 场景 1：A → 重复 A（同 request_id / 重复点同一来源）= 零写入幂等重放
# ---------------------------------------------------------------------------


def test_a_to_repeat_a_is_zero_write_replay(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 场景一")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    version0 = _nav(ctx)
    events0 = _anchor_event_count(ctx)
    fragments0 = _fragment_count(ctx)

    first = _continue(client, topic, hist_a, request_id="req-a")
    assert first.status_code == 200, first.text
    body1 = first.json()
    # 既有响应字段保持兼容（契约 §5.2.7），并追加 changed/replay/inactive。
    for key in (
        "ok",
        "topic_id",
        "fragment_id",
        "fragment_title",
        "historic",
        "created_fragment_id",
        "source_fragment_id",
        "intent_id",
        "intent_version",
        "pending",
    ):
        assert key in body1, key
    assert body1["ok"] is True
    assert body1["changed"] is True
    assert body1["replay"] is False
    assert body1["inactive"] is False
    assert body1["fragment_id"] == hist_a
    assert body1["source_fragment_id"] == hist_a
    assert body1["historic"] is True
    assert body1["created_fragment_id"] is None
    assert body1["pending"] is True
    assert body1["intent_id"]

    intent1 = ctx.bindings.intent_by_request("req-a")
    assert intent1 is not None
    assert intent1.intent_id == body1["intent_id"]
    assert intent1.state == "registered"
    assert intent1.source_fragment_id == hist_a
    assert _anchor_topic(ctx) == topic and _anchor_fragment(ctx) == hist_a
    assert _pending_source(ctx) == hist_a
    assert _nav(ctx) == version0 + 1, "一次真实改选只推进一次导航版本"
    assert _anchor_event_count(ctx) == events0 + 1, "真实改选广播一次 ANCHOR"
    assert _fragment_count(ctx) == fragments0, "点击路径不创建片段"

    before = _position_facts(ctx)
    events_after_first = _anchor_event_count(ctx)

    # 同 request_id 重放（传输重试）。
    replay = _continue(client, topic, hist_a, request_id="req-a")
    assert replay.status_code == 200, replay.text
    body2 = replay.json()
    assert body2["ok"] is True
    assert body2["replay"] is True
    assert body2["changed"] is False
    assert body2["inactive"] is False
    assert body2["fragment_id"] == hist_a
    assert body2["source_fragment_id"] == hist_a
    assert body2["intent_id"] == body1["intent_id"]
    assert body2["intent_version"] == body1["intent_version"]
    assert body2["pending"] is True
    assert ctx.bindings.intent_by_request("req-a").state == "registered"
    _assert_zero_write(ctx, before, anchor_events=events_after_first)

    # 重复点同一来源（不带 request_id）：仍是零写入幂等复用。
    repeat = _continue(client, topic, hist_a)
    assert repeat.status_code == 200, repeat.text
    body3 = repeat.json()
    assert body3["replay"] is True and body3["changed"] is False
    assert body3["intent_id"] == body1["intent_id"]
    _assert_zero_write(ctx, before, anchor_events=events_after_first)


# ---------------------------------------------------------------------------
# §5.3 场景 2：A → B → 重放 A（request_id A）→ 409 inactive，B 保持不变
# ---------------------------------------------------------------------------


def test_replay_of_replaced_request_is_inactive_and_keeps_newer_selection(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 场景二")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    hist_b = _seed_fragment(ctx, topic, title="历史B")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    version0 = _nav(ctx)
    first = _continue(client, topic, hist_a, request_id="req-a")
    assert first.status_code == 200, first.text
    second = _continue(client, topic, hist_b, request_id="req-b")
    assert second.status_code == 200, second.text

    intent_b = ctx.bindings.intent_by_request("req-b")
    assert intent_b is not None
    assert _anchor_fragment(ctx) == hist_b
    assert _pending_source(ctx) == hist_b
    assert _intent_state(ctx, "req-a") == "replaced"
    assert _intent_state(ctx, "req-b") == "registered"
    assert _nav(ctx) == version0 + 2, "两次真实改选各推进一次"

    before = _position_facts(ctx)
    events_before = _anchor_event_count(ctx)
    fragments_before = _fragment_count(ctx)

    replay = _continue(client, topic, hist_a, request_id="req-a")
    assert replay.status_code == 409, replay.text
    body = replay.json()
    assert body["ok"] is False
    assert body["conflict"] is True
    assert body["code"] == "continuation_request_inactive"
    assert body["state"] == "replaced"

    # 旧请求重放不得把 Anchor / pending / 版本 / 片段带回 A。
    assert _anchor_fragment(ctx) == hist_b
    assert _anchor_topic(ctx) == topic
    assert _pending_source(ctx) == hist_b
    assert ctx.bindings.peek_intent().intent_id == intent_b.intent_id
    assert _intent_state(ctx, "req-a") == "replaced"
    assert _intent_state(ctx, "req-b") == "registered"
    assert _fragment_count(ctx) == fragments_before
    _assert_zero_write(ctx, before, anchor_events=events_before)


# ---------------------------------------------------------------------------
# §5.3 场景 3：A 取消后重放 A → 409 inactive，零写入
# ---------------------------------------------------------------------------


def test_replay_after_cancel_is_inactive_and_changes_nothing(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 场景三")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    first = _continue(client, topic, hist_a, request_id="req-cancel")
    assert first.status_code == 200, first.text
    version_after_click = _nav(ctx)
    cancelled = client.post("/api/anchor/continue/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"ok": True, "cancelled": True}
    assert _pending(ctx) is None
    assert _intent_state(ctx, "req-cancel") == "cancelled"
    assert _nav(ctx) == version_after_click, "取消待接续不推进导航版本"

    before = _position_facts(ctx)
    events_before = _anchor_event_count(ctx)
    fragments_before = _fragment_count(ctx)

    replay = _continue(client, topic, hist_a, request_id="req-cancel")
    assert replay.status_code == 409, replay.text
    body = replay.json()
    assert body["ok"] is False and body["conflict"] is True
    assert body["code"] == "continuation_request_inactive"
    assert body["state"] == "cancelled"

    assert _anchor_fragment(ctx) == hist_a
    assert _pending(ctx) is None
    assert _intent_state(ctx, "req-cancel") == "cancelled"
    assert _fragment_count(ctx) == fragments_before
    _assert_zero_write(ctx, before, anchor_events=events_before)


# ---------------------------------------------------------------------------
# §5.3 场景 4：A 已消费后重放 A（无更新选择）→ 200 replay，零写入
# ---------------------------------------------------------------------------


def test_replay_after_consumed_selection_replays_without_writes(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 场景四")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    first = _continue(client, topic, hist_a, request_id="req-consumed")
    assert first.status_code == 200, first.text
    intent_id = first.json()["intent_id"]
    assert intent_id

    # 与生产一致的两阶段：真正有消息执行时才落实（创建片段），首轮成功后消费。
    applied = ctx.navigation.apply_continuation(topic, intent_id)
    assert applied.created_fragment_id is not None
    ctx.bindings.consume_intent(intent_id)
    assert _intent_state(ctx, "req-consumed") == "consumed"
    assert _pending(ctx) is None

    before = _position_facts(ctx)
    events_before = _anchor_event_count(ctx)
    fragments_before = _fragment_count(ctx)
    version_before = _nav(ctx)

    replay = _continue(client, topic, hist_a, request_id="req-consumed")
    assert replay.status_code == 200, replay.text
    body = replay.json()
    assert body["ok"] is True
    assert body["replay"] is True
    assert body["changed"] is False
    assert body["inactive"] is False
    assert body["pending"] is False
    assert body["intent_id"] == intent_id
    # 已消费的选择不在「已替换 / 取消」里：仍按既有结果回放，不重新激活。
    assert _intent_state(ctx, "req-consumed") == "consumed"
    assert _nav(ctx) == version_before
    assert _fragment_count(ctx) == fragments_before
    _assert_zero_write(ctx, before, anchor_events=events_before)


# ---------------------------------------------------------------------------
# §5.3 场景 5：A → B 之后用户重新主动选择 A（新 request_id）→ 真实改选
# ---------------------------------------------------------------------------


def test_user_reasserts_previous_choice_after_switch_is_a_real_new_selection(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 场景五")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    hist_b = _seed_fragment(ctx, topic, title="历史B")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    assert _continue(client, topic, hist_a, request_id="req-a").status_code == 200
    assert _continue(client, topic, hist_b, request_id="req-b").status_code == 200
    version_before = _nav(ctx)
    events_before = _anchor_event_count(ctx)
    fragments_before = _fragment_count(ctx)
    old_intent_a = ctx.bindings.intent_by_request("req-a")
    old_intent_b = ctx.bindings.intent_by_request("req-b")
    assert old_intent_a is not None and old_intent_b is not None

    # 用户真的又点了 A：新 request_id = 新选择，不是旧请求重放。
    third = _continue(client, topic, hist_a, request_id="req-c")
    assert third.status_code == 200, third.text
    body = third.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert body["replay"] is False
    assert body["inactive"] is False
    assert body["fragment_id"] == hist_a
    assert body["historic"] is True
    assert body["pending"] is True

    new_intent = ctx.bindings.intent_by_request("req-c")
    assert new_intent is not None
    assert new_intent.state == "registered"
    assert new_intent.source_fragment_id == hist_a
    assert new_intent.intent_id not in (old_intent_a.intent_id, old_intent_b.intent_id)
    assert _intent_state(ctx, "req-b") == "replaced"
    assert _anchor_fragment(ctx) == hist_a
    assert _pending_source(ctx) == hist_a
    assert _nav(ctx) == version_before + 1, "真实改选只推进一次版本"
    assert _anchor_event_count(ctx) == events_before + 1
    assert _fragment_count(ctx) == fragments_before


# ---------------------------------------------------------------------------
# §5.3 场景 6：同一 request_id 参数与历史不一致 → 409 conflict，零写入
# ---------------------------------------------------------------------------


def test_request_id_reused_with_different_params_is_conflict(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 场景六")
    hist_a = _seed_fragment(ctx, topic, title="历史A")
    hist_b = _seed_fragment(ctx, topic, title="历史B")
    _seed_fragment(ctx, topic, title="当前开放", closed=False)

    first = _continue(client, topic, hist_a, request_id="req-shared")
    assert first.status_code == 200, first.text
    intent_a = ctx.bindings.intent_by_request("req-shared")
    assert intent_a is not None

    before = _position_facts(ctx)
    events_before = _anchor_event_count(ctx)
    fragments_before = _fragment_count(ctx)

    conflict = _continue(client, topic, hist_b, request_id="req-shared")
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["ok"] is False
    assert body["conflict"] is True
    assert body["code"] == "continuation_request_conflict"

    # 不得返回旧意图冒充成功，也不得留下任何写入。
    assert _intent_state(ctx, "req-shared") == "registered"
    assert ctx.bindings.intent_by_request("req-shared").intent_id == intent_a.intent_id
    assert _anchor_fragment(ctx) == hist_a
    assert _pending_source(ctx) == hist_a
    assert _fragment_count(ctx) == fragments_before
    _assert_zero_write(ctx, before, anchor_events=events_before)


# ---------------------------------------------------------------------------
# 边界：点当前开放片段 = 普通继续（不登记意图、不建片段、落位 + 一次版本）
# ---------------------------------------------------------------------------


def test_clicking_open_fragment_is_a_plain_continue(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 开放片段")
    _seed_fragment(ctx, topic, title="历史A")
    open_fragment = _seed_fragment(ctx, topic, title="当前开放", closed=False)

    version0 = _nav(ctx)
    events0 = _anchor_event_count(ctx)
    fragments0 = _fragment_count(ctx)
    intents0 = _intent_count(ctx)

    resp = _continue(client, topic, open_fragment, request_id="req-open")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["inactive"] is False
    assert body["replay"] is False
    assert body["fragment_id"] == open_fragment
    assert body["fragment_title"]
    assert body["historic"] is False
    assert body["intent_id"] is None
    assert body["pending"] is False

    assert _anchor_fragment(ctx) == open_fragment
    assert _pending(ctx) is None
    assert _intent_count(ctx) == intents0, "点当前开放片段不登记接续意图"
    assert _fragment_count(ctx) == fragments0, "点击路径不创建片段"
    assert _nav(ctx) == version0 + 1
    assert _anchor_event_count(ctx) == events0 + 1


# ---------------------------------------------------------------------------
# 边界：非 continue 的 /api/anchor 行为不变（进入话题 / 最新位置）
# ---------------------------------------------------------------------------


def test_non_continue_anchor_path_is_unchanged(client: TestClient):
    ctx = _ctx(client)
    topic = _topic(ctx, "R3 非接续")
    hist_a = _seed_fragment(ctx, topic, title="历史A")

    version0 = _nav(ctx)
    events0 = _anchor_event_count(ctx)
    entered = client.post("/api/anchor", json={"topic_id": topic, "fragment_id": None})
    assert entered.status_code == 200, entered.text
    assert entered.json()["ok"] is True
    assert _anchor_topic(ctx) == topic
    assert _anchor_fragment(ctx) is None, "进入话题＝从最新位置继续，不恢复旧位置"
    assert _nav(ctx) == version0 + 1
    assert _anchor_event_count(ctx) == events0 + 1

    explicit = client.post("/api/anchor", json={"topic_id": topic, "fragment_id": hist_a})
    assert explicit.status_code == 200, explicit.text
    assert _anchor_fragment(ctx) == hist_a
    assert explicit.json()["historic"] is True
    assert _nav(ctx) == version0 + 2
    assert _anchor_event_count(ctx) == events0 + 2
