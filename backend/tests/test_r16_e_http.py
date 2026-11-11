# -*- coding: utf-8 -*-
"""R16 · E：真实 HTTP 导航命令全链路（契约 v1 §2/§3.3/§4/§5 + §14 v2 delta）。

被测系统：真实 FastAPI create_app + 真实迁移 + 临时 SQLite + fastapi.testclient.TestClient
（会执行 lifespan 启动流程，含启动期 PositionStateService.ensure_identity()）。

覆盖（契约 §10.3 E 的真实 HTTP 部分）：
* I04 同身份同参重放：返回既有结果、零位置写入、零版本推进、零 ANCHOR 广播；
* I05 同身份不同参冲突：409 navigation_command_conflict、零写入（含不新增命令行）；
* I01/I07 nav 依据（base_nav_version）过期：409 navigation_command_stale、零业务副作用；
    已失效/已拒绝身份重放绝不重新执行；
* 两个版本域职责（§3.2/C1）：内部推进 position_version 不推进 nav_version，不使手动命令失效；
* 完整位置状态同源（I02）：HTTP 响应 position、ANCHOR 事件 position、runtime/state anchor 三者一致；
* §3.3 转换表：enter_topic / select_history(点击) / open_fragment / cancel_continuation /
    auto_segment（T1：open_fragment_id 内部事实）的版本推进与 pending 收敛；
* §14.2 手动导航必填 request_id + base_nav_version（缺失 400 冻结码）；
* I07 同一数据库重启（新进程实例身份）：旧 request_id 重放返回既有结果，不重新导航。

纪律：不用 sleep 造时序；模型不参与（本文件不触发主循环）；证据写 D:/qio-dev/r16-evidence/e。
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services import navigation_version
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

REQUEST_PREFIX = "req-r16e-"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _now() -> str:
    return "2026-11-11T00:00:00+00:00"


def _new_request_id(tag: str = "") -> str:
    return REQUEST_PREFIX + (tag or "nav") + "-" + uuid.uuid4().hex


@contextmanager
def _backend(tmp_path: Path, name: str = "r16e_http.db"):
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
    """直接建一个片段（封存与否可选）+一条消息（+标题索引），返回 fragment_id。"""
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


def _runtime(client: TestClient) -> dict:
    resp = client.get("/api/runtime/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _anchor(client: TestClient) -> dict:
    return _runtime(client)["anchor"]


def _versions(anchor: dict) -> tuple[int, int]:
    return int(anchor["nav_version"]), int(anchor["position_version"])


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径：直接读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


def _facts(ctx) -> dict:
    """业务副作用快照：位置 / pending / 两版本 / 命令行数（拒绝类允许 +1 行，会单独注明）。"""
    anchor = AnchorService(ctx.conn).get_active()
    pending = ctx.bindings.peek_intent()
    nav = int(navigation_version.current(ctx.conn))
    row = ctx.conn.execute(
        "SELECT position_version FROM position_state WHERE id = 1"
    ).fetchone()
    assert row is not None, "position_state 单行种子缺失（启动期 ensure_identity 未生效）"
    commands = ctx.conn.execute(
        "SELECT COUNT(*) AS c FROM navigation_commands"
    ).fetchone()["c"]
    return {
        "topic_id": anchor.topic_id if anchor else None,
        "fragment_id": anchor.fragment_id if anchor else None,
        "pending_intent_id": pending.intent_id if pending else None,
        "pending_state": pending.state if pending else None,
        "nav_version": nav,
        "position_version": int(row["position_version"]),
        "commands": int(commands),
    }


def _assert_same_facts(after: dict, before: dict, *, allow_new_command_row: bool = False) -> None:
    expected = dict(before)
    if allow_new_command_row:
        expected["commands"] = before["commands"] + 1
    assert after == expected, f"拒绝/重放不得有业务副作用：before={before} after={after}"


def _post_anchor(client: TestClient, body: dict):
    return client.post("/api/anchor", json=body)


def _navigate(
    client: TestClient,
    *,
    topic_id: str,
    fragment_id: str | None = None,
    continue_from_history: bool = False,
    request_id: str | None = None,
    base_nav_version: int | None = None,
    extra: dict | None = None,
):
    """按 §14.2 发一条手动导航命令（默认补齐 request_id + 当前 base_nav_version）。"""
    if base_nav_version is None:
        base_nav_version = int(_anchor(client)["nav_version"])
    if request_id is None:
        request_id = _new_request_id()
    body = {
        "topic_id": topic_id,
        "fragment_id": fragment_id,
        "continue_from_history": continue_from_history,
        "request_id": request_id,
        "base_nav_version": base_nav_version,
    }
    if extra:
        body.update(extra)
    return _post_anchor(client, body)


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
    body = {
        "request_id": request_id or _new_request_id("cancel"),
        "base_nav_version": base_nav_version,
    }
    # R17 §2.2：取消必须明确关联目标选择（id 必填、版本可选）。
    if pending_intent_id is not None:
        body["pending_intent_id"] = pending_intent_id
    if pending_intent_version is not None:
        body["pending_intent_version"] = pending_intent_version
    return client.post("/api/anchor/continue/cancel", json=body)


# ---------------------------------------------------------------------------
# ① §14.2 手动导航依据：request_id / base_nav_version 必填
# ---------------------------------------------------------------------------


def test_manual_navigation_requires_request_id_and_base(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "必填字段话题")

    missing_request = client.post("/api/anchor", json={"topic_id": topic})
    assert missing_request.status_code == 400, missing_request.text
    assert missing_request.json()["code"] == "navigation_request_id_required"

    missing_base = client.post(
        "/api/anchor", json={"topic_id": topic, "request_id": _new_request_id()}
    )
    assert missing_base.status_code == 400, missing_base.text
    assert missing_base.json()["code"] == "navigation_basis_required"

    cancel_missing = client.post("/api/anchor/continue/cancel", json={})
    assert cancel_missing.status_code == 400, cancel_missing.text
    assert cancel_missing.json()["code"] in (
        "navigation_request_id_required",
        "navigation_basis_required",
    )

    # 全部拒绝：不存在任何位置写入
    facts = _facts(ctx)
    assert facts["topic_id"] is None and facts["commands"] == 0, facts


# ---------------------------------------------------------------------------
# ② enter_topic：版本推进、wire 形状、同源（I02）
# ---------------------------------------------------------------------------


def test_enter_topic_advances_versions_and_returns_same_source_position(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "进入话题")
    _seed_fragment(ctx, topic, closed=False)  # 话题有开放片段；进入最新位置仍必须写 NULL

    before = _anchor(client)
    nav0, pos0 = _versions(before)
    identity = before.get("position_identity")
    assert identity, "启动期 ensure_identity 必须写入 position_identity"

    request_id = _new_request_id("enter")
    events_before = len(_anchor_events(ctx))
    resp = _navigate(client, topic_id=topic, request_id=request_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 冻结字段（§4/§5/§14.1）
    for key in (
        "changed",
        "replay",
        "inactive",
        "conflict",
        "code",
        "command_id",
        "nav_version",
        "position_version",
        "position",
    ):
        assert key in body, f"缺少冻结字段 {key}: {body}"
    assert body["changed"] is True and body["replay"] is False and body["code"] is None
    assert body["command_id"] == "request:" + request_id
    assert body["nav_version"] == nav0 + 1
    assert body["position_version"] == pos0 + 1

    position = body["position"]
    required = {
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
    assert required <= set(position), f"position wire 缺少字段：{required - set(position)}"
    assert position["topic_id"] == topic
    assert position["fragment_id"] is None, "进入话题最新位置不得恢复/解析成旧片段"
    assert position["historic"] is False
    assert position["nav_version"] == nav0 + 1
    assert position["position_version"] == pos0 + 1
    assert position["position_identity"] == identity

    # 同源（I02）：HTTP 响应 position == runtime/state anchor == 唯一一条 ANCHOR 事件的 position
    snapshot = _anchor(client)
    assert snapshot == position, f"runtime/state 与命令响应不同源：{snapshot} != {position}"
    events = _anchor_events(ctx)
    assert len(events) == events_before + 1, "真实新命令必须且只广播一次 ANCHOR"
    event = events[-1]
    assert event["position"] == position, "ANCHOR 事件的 position 必须与命令结果同源"
    assert event["command_id"] == body["command_id"]
    assert event["position_version"] == body["position_version"]
    assert event["position_identity"] == identity
    assert event["topic_id"] == topic and event["fragment_id"] is None

    # 服务层权威读取与 HTTP 同源（§14.7 A 冻结接缝）
    assert ctx.position_state.read() == position


# ---------------------------------------------------------------------------
# ③ I04 重放：既有结果 + 当前 position，零写入零广播
# ---------------------------------------------------------------------------


def test_replay_same_identity_same_params_returns_result_and_zero_side_effects(backend):
    client, ctx = backend
    topic_a = _seed_topic(ctx, "重放甲")
    topic_b = _seed_topic(ctx, "重放乙")
    base = int(_anchor(client)["nav_version"])
    request_id = _new_request_id("replay")

    first = _navigate(
        client, topic_id=topic_a, request_id=request_id, base_nav_version=base
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    after_first = _facts(ctx)

    events_before = len(_anchor_events(ctx))
    replay = _navigate(
        client, topic_id=topic_a, request_id=request_id, base_nav_version=base
    )
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["replay"] is True and replay_body["changed"] is False
    assert replay_body["command_id"] == first_body["command_id"]
    assert replay_body["nav_version"] == after_first["nav_version"]
    assert replay_body["position_version"] == after_first["position_version"]
    assert _facts(ctx) == after_first, "重放不得有任何位置/版本/命令写入"
    assert len(_anchor_events(ctx)) == events_before, "重放不得广播 ANCHOR"

    # C3：命中的是既有结局，但 position 是当前（只读）完整状态
    assert replay_body["position"] == _anchor(client)

    # 之后有新的真实导航：再重放原请求仍然 replay，position 返回**当前**状态
    _navigate(client, topic_id=topic_b)
    current = _anchor(client)
    later = _navigate(
        client, topic_id=topic_a, request_id=request_id, base_nav_version=base
    )
    assert later.status_code == 200, later.text
    later_body = later.json()
    assert later_body["replay"] is True and later_body["changed"] is False
    assert later_body["position"] == current, "重放的 position 必须是当前完整状态"
    assert later_body["nav_version"] == current["nav_version"]
    assert later_body["position_version"] == current["position_version"]


# ---------------------------------------------------------------------------
# ④ I05 同身份不同参数：明确冲突、零写入
# ---------------------------------------------------------------------------


def test_same_identity_different_params_is_conflict_with_zero_writes(backend):
    client, ctx = backend
    topic_a = _seed_topic(ctx, "冲突甲")
    topic_b = _seed_topic(ctx, "冲突乙")
    request_id = _new_request_id("conflict")

    applied = _navigate(client, topic_id=topic_a, request_id=request_id)
    assert applied.status_code == 200, applied.text
    applied_body = applied.json()
    facts = _facts(ctx)
    events_before = len(_anchor_events(ctx))

    conflict = _navigate(client, topic_id=topic_b, request_id=request_id)
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["conflict"] is True, body
    assert body["code"] == "navigation_command_conflict", body
    assert body["command_id"] == applied_body["command_id"], "冲突必须返回既有 command_id"
    assert body["position"] == _anchor(client), "冲突响应返回当前完整位置，不猜"
    _assert_same_facts(_facts(ctx), facts, allow_new_command_row=False)
    assert len(_anchor_events(ctx)) == events_before, "冲突不得广播 ANCHOR"


# ---------------------------------------------------------------------------
# ⑤ I01 依据过期：首次出现的新身份 → stale、零业务副作用；重放同一拒绝
# ---------------------------------------------------------------------------


def test_stale_base_is_rejected_and_rejection_is_stable_on_replay(backend):
    client, ctx = backend
    topic_a = _seed_topic(ctx, "过期甲")
    topic_b = _seed_topic(ctx, "过期乙")

    base = int(_anchor(client)["nav_version"])
    applied = _navigate(
        client, topic_id=topic_a, request_id=_new_request_id("stale-a"), base_nav_version=base
    )
    assert applied.status_code == 200, applied.text
    facts = _facts(ctx)

    # 用户后来真实改选（推进 nav_version），旧依据作废
    moved = _navigate(client, topic_id=topic_b)
    assert moved.status_code == 200, moved.text
    facts = _facts(ctx)
    events_before = len(_anchor_events(ctx))

    stale_request = _new_request_id("stale")
    stale = _navigate(
        client, topic_id=topic_a, request_id=stale_request, base_nav_version=base
    )
    assert stale.status_code == 409, stale.text
    stale_body = stale.json()
    assert stale_body["code"] == "navigation_command_stale", stale_body
    assert stale_body["position"] == _anchor(client)
    # 首次出现的拒绝身份允许写一条命令行事实；业务位置零副作用
    _assert_same_facts(_facts(ctx), facts, allow_new_command_row=True)
    assert len(_anchor_events(ctx)) == events_before, "被拒绝的命令不得广播 ANCHOR"

    # I07：同一失效身份重放 → 返回既有拒绝事实，零写入、绝不重新执行
    facts_after_reject = _facts(ctx)
    replay = _navigate(
        client, topic_id=topic_a, request_id=stale_request, base_nav_version=base
    )
    assert replay.status_code == 409, replay.text
    replay_body = replay.json()
    assert replay_body["code"] == "navigation_command_stale"
    assert replay_body["command_id"] == stale_body["command_id"]
    _assert_same_facts(_facts(ctx), facts_after_reject, allow_new_command_row=False)
    assert len(_anchor_events(ctx)) == events_before


# ---------------------------------------------------------------------------
# ⑥ §3.3 enter_topic：不恢复旧位置；select_history：点击路径落位+广播
# ---------------------------------------------------------------------------


def test_enter_latest_does_not_restore_history_position(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "不恢复旧位置")
    historic = _seed_fragment(ctx, topic, closed=True, title="旧历史")

    picked = _navigate(
        client, topic_id=topic, fragment_id=historic, continue_from_history=True
    )
    assert picked.status_code == 200, picked.text
    picked_body = picked.json()
    assert picked_body["historic"] is True
    assert picked_body["position"]["fragment_id"] == historic
    assert picked_body["position"]["pending_intent_id"], "点击历史必须登记接续意图"
    nav1 = picked_body["nav_version"]
    pos1 = picked_body["position_version"]

    latest = _navigate(client, topic_id=topic)
    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["changed"] is True
    assert body["position"]["fragment_id"] is None, "进入话题最新位置不得恢复旧片段"
    assert body["position"]["historic"] is False
    assert body["position"]["pending_intent_id"] is None, "真实导航决定必须清除全局唯一 registered"
    assert body["nav_version"] == nav1 + 1
    assert body["position_version"] == pos1 + 1


def test_select_history_click_positions_anchor_and_broadcasts_once(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "选择历史")
    historic = _seed_fragment(ctx, topic, closed=True, title="要接续的历史")
    base = int(_anchor(client)["nav_version"])
    pos0 = int(_anchor(client)["position_version"])
    events_before = len(_anchor_events(ctx))

    request_id = _new_request_id("select")
    resp = _navigate(
        client,
        topic_id=topic,
        fragment_id=historic,
        continue_from_history=True,
        request_id=request_id,
        base_nav_version=base,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True and body["replay"] is False
    assert body["command_id"] == "request:" + request_id
    assert body["nav_version"] == base + 1, "点击路径是真实导航决定：nav_version +1"
    assert body["position_version"] == pos0 + 1, "pending 出现属于真实状态变化"
    position = body["position"]
    assert position["topic_id"] == topic
    assert position["fragment_id"] == historic
    assert position["historic"] is True
    assert position["pending_intent_id"], position
    assert position["pending_source_fragment_id"] == historic
    assert position["pending_source_title"] == "要接续的历史"

    events = _anchor_events(ctx)
    assert len(events) == events_before + 1
    event = events[-1]
    assert event["position"] == position
    assert event["pending_intent_id"] == position["pending_intent_id"]
    assert event["fragment_id"] == historic


def test_historic_fragment_without_continuation_flag_is_400(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "历史必须走接续")
    historic = _seed_fragment(ctx, topic, closed=True, title="封存历史")
    facts = _facts(ctx)

    resp = _navigate(client, topic_id=topic, fragment_id=historic, continue_from_history=False)
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "historic_fragment_requires_continuation"
    _assert_same_facts(_facts(ctx), facts, allow_new_command_row=False)


# ---------------------------------------------------------------------------
# ⑦ open_fragment：返回当前开放片段必须结束旧 registered（I03/回归 E）
# ---------------------------------------------------------------------------


def test_open_fragment_clears_registered_pending_and_advances_once(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "返回开放")
    historic = _seed_fragment(ctx, topic, closed=True, title="旧历史")
    open_fragment = ctx.fragments.get_or_create_open(topic).id
    ctx.conn.commit()

    picked = _navigate(
        client, topic_id=topic, fragment_id=historic, continue_from_history=True
    )
    assert picked.status_code == 200, picked.text
    nav1 = picked.json()["nav_version"]
    pos1 = picked.json()["position_version"]
    assert picked.json()["position"]["pending_intent_id"]

    events_before = len(_anchor_events(ctx))
    opened = _navigate(client, topic_id=topic, fragment_id=open_fragment)
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["changed"] is True
    assert body["position"]["fragment_id"] == open_fragment
    assert body["position"]["historic"] is False
    assert body["position"]["pending_intent_id"] is None, "旧接续选择必须被结束"
    assert body["nav_version"] == nav1 + 1, "有 pending 被取消：nav_version 恰好 +1（不 +2）"
    assert body["position_version"] == pos1 + 1
    assert len(_anchor_events(ctx)) == events_before + 1


# ---------------------------------------------------------------------------
# ⑧ cancel_continuation：取消是真实决定；重复取消/重放语义
# ---------------------------------------------------------------------------


def test_cancel_continuation_advances_pending_and_is_idempotent(backend):
    client, ctx = backend
    topic = _seed_topic(ctx, "取消接续")
    historic = _seed_fragment(ctx, topic, closed=True, title="待取消历史")
    picked = _navigate(
        client, topic_id=topic, fragment_id=historic, continue_from_history=True
    )
    assert picked.status_code == 200, picked.text
    nav1 = picked.json()["nav_version"]
    pos1 = picked.json()["position_version"]

    intent = ctx.bindings.peek_intent()
    assert intent is not None, "登记成功后必须存在待落实接续（取消目标）"
    request_id = _new_request_id("cancel")
    events_before = len(_anchor_events(ctx))
    cancelled = _cancel(
        client,
        request_id=request_id,
        pending_intent_id=intent.intent_id,
        pending_intent_version=intent.version,
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["changed"] is True
    assert body["command_id"] == "request:" + request_id
    assert body["position"]["pending_intent_id"] is None
    # §14.9：取消不是新的导航选择 → nav_version 不推进；pending 事实变化 → position +1
    assert body["nav_version"] == nav1, "cancel_continuation 不得推进 nav_version（§14.9）"
    assert body["position_version"] == pos1 + 1
    assert len(_anchor_events(ctx)) == events_before + 1, "pending 消失必须广播一次 ANCHOR"

    facts = _facts(ctx)
    replay = _cancel(
        client,
        request_id=request_id,
        pending_intent_id=intent.intent_id,
        pending_intent_version=intent.version,
    )
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["replay"] is True and replay_body["changed"] is False
    assert _facts(ctx) == facts
    assert len(_anchor_events(ctx)) == events_before + 1

    # 携带已失效目标的再次取消（新动作）：R17 §2.2 冻结为 409 inactive，
    # 零变化，仍留一行拒绝事实（不再有无目标 no-op 200 旧路径）。
    again = _cancel(
        client,
        pending_intent_id=intent.intent_id,
        pending_intent_version=intent.version,
    )
    assert again.status_code == 409, again.text
    again_body = again.json()
    assert again_body["ok"] is False and again_body["inactive"] is True
    assert again_body["code"] == "continuation_request_inactive"
    assert again_body["changed"] is False and again_body["replay"] is False
    assert again_body["position"]["pending_intent_id"] is None
    after = _facts(ctx)
    assert after["nav_version"] == facts["nav_version"] == nav1
    assert after["position_version"] == facts["position_version"]


# ---------------------------------------------------------------------------
# ⑨ 两个版本域分离（C1/T1）：内部 auto_segment 推进 position_version、不动 nav_version，
#    且不使已批准的手动命令失效
# ---------------------------------------------------------------------------


def test_auto_segment_advances_position_only_and_does_not_invalidate_manual_basis(backend):
    client, ctx = backend
    topic_a = _seed_topic(ctx, "分段甲")
    topic_b = _seed_topic(ctx, "分段乙")
    _seed_fragment(ctx, topic_a, closed=False, title="待分段开放片段")

    entered = _navigate(client, topic_id=topic_a)
    assert entered.status_code == 200, entered.text
    nav1 = entered.json()["nav_version"]
    pos1 = entered.json()["position_version"]

    # 生产真实序列（A2 接线）：封存本身不写位置；orchestrator 在 post_turn/boundary
    # 封存完成后调用 navigation.apply_auto_segment 走统一入口推进 position_version。
    sealed = ctx.memory_lifecycle.seal_fragment(
        topic_a, reason="capacity", continue_same_stage=True
    )
    assert sealed is not None, "开放片段应可被容量分段封存（前置条件）"
    auto = ctx.navigation.apply_auto_segment(
        topic_a,
        sealed.id,
        turn_id="turn-e-auto-segment",
        expected_position_version=pos1,
    )
    assert auto.status == "applied" and auto.changed is True, auto

    anchor_now = _anchor(client)
    assert anchor_now["nav_version"] == nav1, "容量分段不是导航决定：nav_version 不推进"
    assert anchor_now["position_version"] == pos1 + 1, (
        "T1：当前话题 open_fragment_id 属于完整状态内部事实，auto_segment 一律 position +1"
    )
    assert auto.position == anchor_now, "统一入口返回的 position 必须与权威快照同源"

    # 手动命令依据仍是 nav1 → 必须仍然有效（两个版本域不交叉比较）
    applied = _navigate(
        client, topic_id=topic_b, request_id=_new_request_id("after-seal"), base_nav_version=nav1
    )
    assert applied.status_code == 200, (
        "内部 position 推进不得让已批准/已看到的手动命令 basis 失效（C1）：" + applied.text
    )


# ---------------------------------------------------------------------------
# ⑩ I07 重启（新实例身份、同一数据库）：旧 request_id 重放返回既有结果
# ---------------------------------------------------------------------------


def test_restart_replays_existing_result_without_renavigation(tmp_path: Path):
    topic = None
    request_id = _new_request_id("restart")
    base = None
    first_position = None
    identity1 = None
    instance1 = None
    with _backend(tmp_path, "restart.db") as (client, ctx):
        topic = _seed_topic(ctx, "重启话题")
        state = _runtime(client)
        identity1 = state["anchor"]["position_identity"]
        instance1 = state["instance_id"]
        base = int(state["anchor"]["nav_version"])
        first = _navigate(
            client, topic_id=topic, request_id=request_id, base_nav_version=base
        )
        assert first.status_code == 200, first.text
        first_position = first.json()["position"]
        assert first_position["topic_id"] == topic

    # 新 AppContext / 新连接 / 新实例身份，同一数据库文件（模拟进程重启）
    with _backend(tmp_path, "restart.db") as (client2, ctx2):
        state2 = _runtime(client2)
        anchor2 = state2["anchor"]
        assert state2["instance_id"] != instance1, "新 AppContext 必须有新进程实例身份"
        assert anchor2["position_identity"] == identity1, "同一数据库的 position_identity 必须跨重启稳定"
        assert anchor2["topic_id"] == topic
        assert anchor2["nav_version"] == first_position["nav_version"]
        assert anchor2["position_version"] == first_position["position_version"]
        assert ctx2.position_state.read() == anchor2

        facts = _facts(ctx2)
        events_before = len(_anchor_events(ctx2))
        replay = _navigate(
            client2, topic_id=topic, request_id=request_id, base_nav_version=base
        )
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["replay"] is True and body["changed"] is False
        assert body["command_id"] == "request:" + request_id
        assert body["position"] == anchor2
        assert _facts(ctx2) == facts, "重启后的重放不得重新执行/写位置"
        assert len(_anchor_events(ctx2)) == events_before, "重启后的重放不得广播 ANCHOR"
