# -*- coding: utf-8 -*-
"""R17 · B：取消命令的目标身份 + 一次点击一次原子导航（契约 §2.2/§2.4/§5，验收 B2/B3）。

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + fastapi.testclient.TestClient
（会执行 lifespan 启动流程，含启动期 PositionStateService.ensure_identity()）。
模型不参与（本文件不触发主循环）；数据库一律 tmp_path 临时库，不触碰真实应用库。

B2 取消目标：
* 取消 A 不能取消 B：A 被 B 替换后，用 A 的 intent_id/version 取消 → 409
  continuation_request_inactive，B 的位置/接续/两类版本/广播数零变化；
* 目标不存在 / 版本不一致 → 409 continuation_cancel_target_mismatch，零业务副作用；
* 缺目标 → 400 continuation_cancel_target_required；目标字段类型错误 → 400
  continuation_cancel_target_invalid（零命令/零位置/零广播）；
* 正常取消（正确 id/version+base）→ 200、pending 清空、nav_version 不推进、
  position_version +1、一次 ANCHOR；同 request_id 重放零版本推进/零广播。

B3 原子导航：
* 目标不可用（404 话题/片段不存在/历史片段走错入口）→ 位置/pending/版本/广播全不变；
* 版本冲突（旧 base）→ 409 navigation_command_stale，全不变；
* 事务中途失败（monkeypatch 事务内写命令行的最后一步 / Anchor 写入）→ 异常且整份回滚；
* 成功 → 一次状态变化、pending 按规则清理、一次广播；
* 响应丢失后用同 request_id 重试 → 既有结果重放、不重新落位/广播。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import navigation_version
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

REQUEST_PREFIX = "req-r17b-"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _now() -> str:
    return "2026-11-11T00:00:00+00:00"


def _new_request_id(tag: str = "") -> str:
    return REQUEST_PREFIX + (tag or "nav") + "-" + uuid.uuid4().hex


@contextmanager
def _backend(tmp_path: Path, name: str = "r17b_cancel_atomic.db"):
    """真实 create_app + 真实迁移 + 临时 SQLite；TestClient 上下文触发 lifespan。"""
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    try:
        with TestClient(app) as client:
            yield client, ctx
    finally:
        conn.close()


@pytest.fixture()
def backend(tmp_path: Path):
    with _backend(tmp_path) as pair:
        yield pair


def _seed_topic(ctx, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _seed_fragment(
    ctx,
    topic_id: str,
    *,
    closed: bool = True,
    title: str = "历史片段",
    payload: str = "历史原文",
) -> str:
    """直接建一个片段（封存与否可选）+ 一条消息（+标题索引），返回 fragment_id。"""
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (frag_id, topic_id, f"{title} 的摘要", now, now if closed else None),
    )
    msg_id = f"{frag_id}_m1"
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (msg_id, frag_id, payload, now),
    )
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (msg_id, msg_id, frag_id),
    )
    if closed:
        ctx.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
            "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (new_id("idx"), frag_id, topic_id, title, now),
        )
    ctx.conn.commit()
    return frag_id


def _anchor(client: TestClient) -> dict:
    resp = client.get("/api/runtime/state")
    assert resp.status_code == 200, resp.text
    return resp.json()["anchor"]


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径：直接读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


def _snapshot(ctx) -> dict:
    """业务副作用快照：完整位置 wire（同源 READ）+ 命令行总数。"""
    row = ctx.conn.execute("SELECT COUNT(*) AS c FROM navigation_commands").fetchone()
    return {
        "position": ctx.position_state.read(),
        "commands": int(row["c"]),
    }


def _assert_same_snapshot(after: dict, before: dict, *, allow_new_command_rows: int = 0) -> None:
    assert after["position"] == before["position"], (
        f"拒绝/回滚不得改变完整位置状态：before={before['position']} after={after['position']}"
    )
    assert after["commands"] == before["commands"] + allow_new_command_rows, (
        f"命令行数不符（拒绝类只允许首次被拒 +1 行）：before={before['commands']} "
        f"after={after['commands']} allowed={allow_new_command_rows}"
    )


def _navigate(
    client: TestClient,
    *,
    topic_id: str,
    fragment_id: str | None = None,
    continue_from_history: bool = False,
    request_id: str | None = None,
    base_nav_version: int | None = None,
):
    """按 §14.2 发一条手动导航命令（默认补齐 request_id + 当前 base_nav_version）。"""
    if base_nav_version is None:
        base_nav_version = int(_anchor(client)["nav_version"])
    if request_id is None:
        request_id = _new_request_id()
    return client.post(
        "/api/anchor",
        json={
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "continue_from_history": continue_from_history,
            "request_id": request_id,
            "base_nav_version": base_nav_version,
        },
    )


def _cancel(
    client: TestClient,
    *,
    request_id: str | None = None,
    base_nav_version: int | None = None,
    pending_intent_id: str | None = None,
    pending_intent_version: int | None = None,
):
    if base_nav_version is None:
        base_nav_version = int(_anchor(client)["nav_version"])
    return client.post(
        "/api/anchor/continue/cancel",
        json={
            "request_id": request_id or _new_request_id("cancel"),
            "base_nav_version": base_nav_version,
            "pending_intent_id": pending_intent_id,
            "pending_intent_version": pending_intent_version,
        },
    )


def _register(
    client: TestClient,
    ctx,
    topic_id: str,
    fragment_id: str,
    *,
    request_id: str,
):
    """点击历史：select_history 登记接续（pending=register），返回意图事实。"""
    response = _navigate(
        client,
        topic_id=topic_id,
        fragment_id=fragment_id,
        continue_from_history=True,
        request_id=request_id,
    )
    assert response.status_code == 200, response.text
    intent = ctx.bindings.intent_by_request(request_id)
    assert intent is not None, "登记请求必须留下接续意图"
    return intent


# ---------------------------------------------------------------------------
# B2 取消目标身份
# ---------------------------------------------------------------------------


def test_cancel_stale_target_cannot_cancel_newer_selection(backend):
    """取消 A 的（已过期）目标绝不隐式取消后来选中的 B：409 inactive，B 零变化。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "取消目标甲")
    hist_a = _seed_fragment(ctx, topic, closed=True, title="历史A")
    hist_b = _seed_fragment(ctx, topic, closed=True, title="历史B")

    intent_a = _register(client, ctx, topic, hist_a, request_id="req-target-a")
    intent_b = _register(client, ctx, topic, hist_b, request_id="req-target-b")
    assert ctx.bindings.intent_by_id(intent_a.intent_id).state == "replaced"
    assert ctx.bindings.peek_intent().intent_id == intent_b.intent_id

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))

    # 旧客户端/迟到响应仍拿着 A 的 id+version 来取消：目标已不是当前 registered。
    stale = _cancel(
        client,
        request_id="req-cancel-stale-a",
        base_nav_version=before["position"]["nav_version"],
        pending_intent_id=intent_a.intent_id,
        pending_intent_version=intent_a.version,
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["ok"] is False
    assert body["inactive"] is True
    assert body["conflict"] is False
    assert body["code"] == "continuation_request_inactive", body
    assert body["changed"] is False
    assert body["position"] == ctx.position_state.read(), "拒绝响应返回当前只读 position"

    # B 的 pending / 位置 / 两类版本 / 广播数全部保持；只有一行拒绝事实。
    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=1)
    assert len(_anchor_events(ctx)) == events_before, "错误目标取消不得广播 ANCHOR"
    assert ctx.bindings.peek_intent().intent_id == intent_b.intent_id
    assert ctx.bindings.intent_by_id(intent_b.intent_id).state == "registered"
    assert ctx.bindings.intent_by_id(intent_a.intent_id).state == "replaced"

    # 不带 version 的同类旧目标也必须是同一语义（状态优先于版本）：仍然 409 inactive。
    stale_no_version = _cancel(
        client,
        request_id="req-cancel-stale-a-nover",
        base_nav_version=before["position"]["nav_version"],
        pending_intent_id=intent_a.intent_id,
    )
    assert stale_no_version.status_code == 409, stale_no_version.text
    assert stale_no_version.json()["code"] == "continuation_request_inactive"
    assert ctx.bindings.peek_intent().intent_id == intent_b.intent_id


def test_cancel_unknown_or_wrong_version_is_mismatch_with_zero_side_effects(backend):
    """目标不存在 / 版本不一致 → 409 continuation_cancel_target_mismatch，零业务副作用。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "取消目标乙")
    hist = _seed_fragment(ctx, topic, closed=True, title="历史C")
    intent = _register(client, ctx, topic, hist, request_id="req-target-c")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    base = before["position"]["nav_version"]

    wrong_version = _cancel(
        client,
        request_id="req-cancel-wrong-version",
        base_nav_version=base,
        pending_intent_id=intent.intent_id,
        pending_intent_version=intent.version + 1,
    )
    assert wrong_version.status_code == 409, wrong_version.text
    wrong_body = wrong_version.json()
    assert wrong_body["conflict"] is False
    assert wrong_body["code"] == "continuation_cancel_target_mismatch", wrong_body
    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=1)

    unknown = _cancel(
        client,
        request_id="req-cancel-unknown",
        base_nav_version=base,
        pending_intent_id="intent_does_not_exist",
        pending_intent_version=None,
    )
    assert unknown.status_code == 409, unknown.text
    assert unknown.json()["code"] == "continuation_cancel_target_mismatch", unknown.text
    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=2)
    assert len(_anchor_events(ctx)) == events_before, "错误目标取消不得广播 ANCHOR"
    assert ctx.bindings.peek_intent().intent_id == intent.intent_id
    assert ctx.bindings.intent_by_id(intent.intent_id).state == "registered"


def test_cancel_requires_valid_target_identity(backend):
    """缺 pending_intent_id → 400 target_required；目标字段类型错误 → 400 target_invalid。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "取消目标丙")
    hist = _seed_fragment(ctx, topic, closed=True, title="历史D")
    intent = _register(client, ctx, topic, hist, request_id="req-target-d")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    base = before["position"]["nav_version"]

    missing = client.post(
        "/api/anchor/continue/cancel",
        json={"request_id": "req-cancel-missing-target", "base_nav_version": base},
    )
    assert missing.status_code == 400, missing.text
    assert missing.json()["code"] == "continuation_cancel_target_required", missing.text

    bad_version_type = client.post(
        "/api/anchor/continue/cancel",
        json={
            "request_id": "req-cancel-bad-version-type",
            "base_nav_version": base,
            "pending_intent_id": intent.intent_id,
            "pending_intent_version": "1",
        },
    )
    assert bad_version_type.status_code == 400, bad_version_type.text
    assert bad_version_type.json()["code"] == "continuation_cancel_target_invalid", (
        bad_version_type.text
    )

    bool_version = client.post(
        "/api/anchor/continue/cancel",
        json={
            "request_id": "req-cancel-bool-version",
            "base_nav_version": base,
            "pending_intent_id": intent.intent_id,
            "pending_intent_version": True,
        },
    )
    assert bool_version.status_code == 400, bool_version.text
    assert bool_version.json()["code"] == "continuation_cancel_target_invalid"

    bad_id_type = client.post(
        "/api/anchor/continue/cancel",
        json={
            "request_id": "req-cancel-bad-id-type",
            "base_nav_version": base,
            "pending_intent_id": 123,
        },
    )
    assert bad_id_type.status_code == 400, bad_id_type.text
    assert bad_id_type.json()["code"] == "continuation_cancel_target_invalid"

    # 请求级拒绝：零命令行、零位置写入、零广播，pending 原样。
    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=0)
    assert len(_anchor_events(ctx)) == events_before
    assert ctx.bindings.peek_intent().intent_id == intent.intent_id


def test_cancel_current_target_succeeds_once_and_replay_is_zero_side_effect(backend):
    """正常取消：清 pending、nav 不推进、position +1、一次 ANCHOR；同 id 重放零副作用。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "取消成功")
    hist = _seed_fragment(ctx, topic, closed=True, title="待取消历史")
    intent = _register(client, ctx, topic, hist, request_id="req-target-ok")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    base = before["position"]["nav_version"]
    request_id = "req-cancel-ok"
    body = {
        "request_id": request_id,
        "base_nav_version": base,
        "pending_intent_id": intent.intent_id,
        "pending_intent_version": intent.version,
    }

    cancelled = client.post("/api/anchor/continue/cancel", json=body)
    assert cancelled.status_code == 200, cancelled.text
    payload = cancelled.json()
    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["replay"] is False
    assert payload["cancelled"] is True
    assert payload["cancelled_intent_id"] == intent.intent_id
    assert payload["command_id"] == "request:" + request_id
    assert payload["nav_version"] == base, "取消不是新的导航选择：nav_version 不推进（§14.9）"
    assert payload["position_version"] == before["position"]["position_version"] + 1
    assert payload["position"]["pending_intent_id"] is None
    assert payload["position"]["pending_intent_version"] is None
    assert ctx.bindings.peek_intent() is None
    assert ctx.bindings.intent_by_id(intent.intent_id).state == "cancelled"

    after_first = _snapshot(ctx)
    assert after_first["position"]["position_version"] == before["position"]["position_version"] + 1
    assert after_first["position"]["nav_version"] == base
    events = _anchor_events(ctx)
    assert len(events) == events_before + 1, "停用提示必须广播一次 ANCHOR"
    assert events[-1]["position"] == payload["position"], "ANCHOR 必须携带同源完整 position"
    assert events[-1]["command_id"] == payload["command_id"]
    assert _anchor(client) == payload["position"], "runtime 快照与命令结果同源"
    assert ctx.position_state.read() == payload["position"]

    # 同 request_id 同参数重放：返回既有结果、零版本推进、零广播。
    replay = client.post("/api/anchor/continue/cancel", json=body)
    assert replay.status_code == 200, replay.text
    replay_payload = replay.json()
    assert replay_payload["replay"] is True
    assert replay_payload["changed"] is False
    assert replay_payload["cancelled"] is True
    assert replay_payload["command_id"] == "request:" + request_id
    _assert_same_snapshot(_snapshot(ctx), after_first)
    assert len(_anchor_events(ctx)) == events_before + 1
    assert replay_payload["position"] == ctx.position_state.read()

    # 同一 request_id 换目标 = 同身份不同参数（I05）：409 conflict，不得借重放取消 B。
    intent_b = _register(client, ctx, topic, hist, request_id="req-target-after-cancel")
    events_after_register = len(_anchor_events(ctx))
    conflict = client.post(
        "/api/anchor/continue/cancel",
        json={
            "request_id": request_id,
            "base_nav_version": int(_anchor(client)["nav_version"]),
            "pending_intent_id": intent_b.intent_id,
            "pending_intent_version": intent_b.version,
        },
    )
    assert conflict.status_code == 409, conflict.text
    conflict_body = conflict.json()
    assert conflict_body["conflict"] is True
    assert conflict_body["code"] == "navigation_command_conflict", conflict_body
    assert ctx.bindings.peek_intent().intent_id == intent_b.intent_id, "冲突不得取消 B"
    assert len(_anchor_events(ctx)) == events_after_register, "冲突不得广播 ANCHOR"


# ---------------------------------------------------------------------------
# B3 原子导航
# ---------------------------------------------------------------------------


def test_navigation_target_unavailable_leaves_everything_untouched(backend):
    """目标不可用（404 话题 / 400 片段不属于话题 / 400 历史片段走错入口）：全不变。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "原子目标甲")
    other_topic = _seed_topic(ctx, "原子目标乙")
    hist = _seed_fragment(ctx, topic, closed=True, title="历史E")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    base = before["position"]["nav_version"]

    missing_topic = client.post(
        "/api/anchor",
        json={
            "topic_id": "topic_does_not_exist",
            "request_id": _new_request_id("missing-topic"),
            "base_nav_version": base,
        },
    )
    assert missing_topic.status_code == 404, missing_topic.text

    foreign_fragment = client.post(
        "/api/anchor",
        json={
            "topic_id": other_topic,
            "fragment_id": hist,
            "request_id": _new_request_id("foreign-fragment"),
            "base_nav_version": base,
        },
    )
    assert foreign_fragment.status_code == 400, foreign_fragment.text

    historic_wrong_entry = client.post(
        "/api/anchor",
        json={
            "topic_id": topic,
            "fragment_id": hist,
            "request_id": _new_request_id("historic-wrong-entry"),
            "base_nav_version": base,
        },
    )
    assert historic_wrong_entry.status_code == 400, historic_wrong_entry.text
    assert historic_wrong_entry.json()["detail"] == "historic_fragment_requires_continuation"

    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=0)
    assert len(_anchor_events(ctx)) == events_before
    assert _anchor(client) == before["position"]


def test_navigation_stale_base_is_rejected_and_rejection_replays_zero_write(backend):
    """旧 base 依据 → 409 navigation_command_stale；拒绝事实稳定重放，零业务副作用。"""
    client, ctx = backend
    topic_a = _seed_topic(ctx, "依据甲")
    topic_b = _seed_topic(ctx, "依据乙")
    base0 = int(_anchor(client)["nav_version"])
    first = _navigate(client, topic_id=topic_a, base_nav_version=base0)
    assert first.status_code == 200, first.text

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    request_id = "req-stale-nav"
    stale = _navigate(
        client, topic_id=topic_b, request_id=request_id, base_nav_version=base0
    )
    assert stale.status_code == 409, stale.text
    stale_body = stale.json()
    assert stale_body["changed"] is False
    assert stale_body["inactive"] is True
    assert stale_body["code"] == "navigation_command_stale", stale_body
    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=1)
    assert len(_anchor_events(ctx)) == events_before
    assert _anchor(client)["topic_id"] == topic_a, "旧依据不得覆盖用户后来的选择"

    rejected = _snapshot(ctx)
    replay = _navigate(
        client, topic_id=topic_b, request_id=request_id, base_nav_version=base0
    )
    assert replay.status_code == 409, replay.text
    replay_body = replay.json()
    assert replay_body["replay"] is True
    assert replay_body["changed"] is False
    assert replay_body["position"] == ctx.position_state.read()
    _assert_same_snapshot(_snapshot(ctx), rejected)
    assert len(_anchor_events(ctx)) == events_before


def test_navigation_transaction_failure_at_command_write_rolls_back_everything(backend, monkeypatch):
    """事务内最后一步（写命令行）失败：Anchor/pending/两类版本/命令行整份回滚。"""
    client, ctx = backend
    topic_a = _seed_topic(ctx, "回滚甲")
    topic_b = _seed_topic(ctx, "回滚乙")
    hist = _seed_fragment(ctx, topic_a, closed=True, title="回滚历史")
    assert _navigate(client, topic_id=topic_a).status_code == 200
    intent = _register(client, ctx, topic_a, hist, request_id="req-rollback-pending")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试注入
        raise RuntimeError("injected navigation command write failure")

    monkeypatch.setattr(
        "agent.services.position_state.PositionStateService._insert_command", _boom
    )
    with pytest.raises(RuntimeError):
        client.post(
            "/api/anchor",
            json={
                "topic_id": topic_b,
                "request_id": "req-rollback-command",
                "base_nav_version": int(before["position"]["nav_version"]),
            },
        )

    after = _snapshot(ctx)
    _assert_same_snapshot(after, before, allow_new_command_rows=0)
    assert len(_anchor_events(ctx)) == events_before, "事务失败不得广播"
    assert _anchor(client)["topic_id"] == topic_a
    assert ctx.bindings.peek_intent().intent_id == intent.intent_id
    assert ctx.bindings.intent_by_id(intent.intent_id).state == "registered"


def test_navigation_transaction_failure_at_anchor_write_rolls_back_pending_replacement(backend, monkeypatch):
    """事务中途（Anchor 写入）失败：已被替换的旧 pending 也必须整份回滚。"""
    client, ctx = backend
    topic_a = _seed_topic(ctx, "锚回滚甲")
    topic_b = _seed_topic(ctx, "锚回滚乙")
    hist_a = _seed_fragment(ctx, topic_a, closed=True, title="锚历史A")
    hist_b = _seed_fragment(ctx, topic_b, closed=True, title="锚历史B")
    assert _navigate(client, topic_id=topic_a).status_code == 200
    intent_a = _register(client, ctx, topic_a, hist_a, request_id="req-anchor-rollback-a")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003 - 测试注入
        raise RuntimeError("injected anchor write failure")

    monkeypatch.setattr("agent.graph.anchors.AnchorService.set_active", _boom)
    with pytest.raises(RuntimeError):
        client.post(
            "/api/anchor",
            json={
                "topic_id": topic_b,
                "fragment_id": hist_b,
                "continue_from_history": True,
                "request_id": "req-anchor-rollback-b",
                "base_nav_version": int(before["position"]["nav_version"]),
            },
        )

    _assert_same_snapshot(_snapshot(ctx), before, allow_new_command_rows=0)
    assert len(_anchor_events(ctx)) == events_before
    assert ctx.bindings.intent_by_request("req-anchor-rollback-b") is None, (
        "事务内新建的接续意图必须随回滚消失"
    )
    assert ctx.bindings.peek_intent().intent_id == intent_a.intent_id
    assert ctx.bindings.intent_by_id(intent_a.intent_id).state == "registered"


def test_open_fragment_success_is_single_atomic_navigation_with_pending_cleanup(backend):
    """返回当前开放片段：一次事务内落位 + 清理旧 registered，一次广播。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "开放片段")
    open_fragment = _seed_fragment(ctx, topic, closed=False, title="当前开放")
    hist = _seed_fragment(ctx, topic, closed=True, title="旧历史")
    intent = _register(client, ctx, topic, hist, request_id="req-open-pending")
    assert ctx.bindings.peek_intent().intent_id == intent.intent_id

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    base = int(_anchor(client)["nav_version"])

    response = _navigate(
        client,
        topic_id=topic,
        fragment_id=open_fragment,
        continue_from_history=True,
        request_id="req-open-fragment",
        base_nav_version=base,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True and body["replay"] is False and body["code"] is None
    assert body["position"]["fragment_id"] == open_fragment
    assert body["position"]["historic"] is False
    assert body["position"]["pending_intent_id"] is None, "旧的 registered 接续必须被清理"
    assert body["nav_version"] == base + 1
    assert body["position_version"] == before["position"]["position_version"] + 1

    events = _anchor_events(ctx)
    assert len(events) == events_before + 1, "真实导航必须且只广播一次 ANCHOR"
    assert events[-1]["position"] == body["position"]
    assert _anchor(client) == body["position"]
    assert ctx.position_state.read() == body["position"]
    assert ctx.bindings.peek_intent() is None
    assert ctx.bindings.intent_by_id(intent.intent_id).state == "cancelled"


def test_enter_topic_latest_success_clears_pending_in_same_transaction(backend):
    """进入话题最新位置：落位 NULL + 取消不再适用的 registered 是同一个事务。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "进入最新")
    _seed_fragment(ctx, topic, closed=False, title="开放片段")
    hist = _seed_fragment(ctx, topic, closed=True, title="历史F")
    intent = _register(client, ctx, topic, hist, request_id="req-enter-pending")

    before = _snapshot(ctx)
    events_before = len(_anchor_events(ctx))
    base = int(_anchor(client)["nav_version"])

    response = _navigate(
        client, topic_id=topic, request_id="req-enter-latest", base_nav_version=base
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["changed"] is True
    assert body["position"]["fragment_id"] is None, "进入话题最新位置如实存 NULL"
    assert body["position"]["pending_intent_id"] is None
    assert body["nav_version"] == base + 1
    assert body["position_version"] == before["position"]["position_version"] + 1
    assert len(_anchor_events(ctx)) == events_before + 1
    assert ctx.bindings.peek_intent() is None
    assert ctx.bindings.intent_by_id(intent.intent_id).state == "cancelled"


def test_lost_response_retry_replays_without_relanding(backend):
    """响应丢失后用同 request_id 重试：既有结果重放，不重新落位、不重复广播。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "响应丢失")
    request_id = "req-lost-response"
    body = {
        "topic_id": topic,
        "request_id": request_id,
        "base_nav_version": int(_anchor(client)["nav_version"]),
    }

    first = client.post("/api/anchor", json=body)
    assert first.status_code == 200, first.text
    first_position = first.json()["position"]
    after_first = _snapshot(ctx)
    events_after_first = len(_anchor_events(ctx))
    assert int(_anchor(client)["position_version"]) == int(first_position["position_version"])

    retry = client.post("/api/anchor", json=body)
    assert retry.status_code == 200, retry.text
    retry_body = retry.json()
    assert retry_body["replay"] is True
    assert retry_body["changed"] is False
    assert retry_body["command_id"] == "request:" + request_id
    assert retry_body["position"] == first_position, "重放返回当前只读完整状态（此处未再变化）"
    _assert_same_snapshot(_snapshot(ctx), after_first)
    assert len(_anchor_events(ctx)) == events_after_first
    assert int(_anchor(client)["position_version"]) == int(first_position["position_version"])


# ---------------------------------------------------------------------------
# §2.1 受理依据只读助手（B 在 /api/turns 调用；冻结签名接缝，不跑 /api/turns HTTP）
# ---------------------------------------------------------------------------


def test_submission_basis_is_read_only_contract_snapshot(backend):
    """/api/turns 的 basis 快照：冻结 6 键 + 只读（调用前后位置状态零变化）。"""
    client, ctx = backend
    topic = _seed_topic(ctx, "受理依据")
    hist = _seed_fragment(ctx, topic, closed=True, title="依据历史")
    from agent.api.server import _submission_basis

    intent = _register(client, ctx, topic, hist, request_id="req-basis")
    position = ctx.position_state.read()
    before = _snapshot(ctx)

    basis = _submission_basis(ctx)

    assert set(basis) == {
        "topic_id",
        "fragment_id",
        "intent_id",
        "intent_version",
        "nav_version",
        "position_version",
    }, basis
    assert basis["topic_id"] == position["topic_id"]
    assert basis["fragment_id"] == position["fragment_id"]
    assert basis["intent_id"] == intent.intent_id
    assert basis["intent_version"] == intent.version
    assert basis["nav_version"] == position["nav_version"]
    assert basis["position_version"] == position["position_version"]
    _assert_same_snapshot(_snapshot(ctx), before)
