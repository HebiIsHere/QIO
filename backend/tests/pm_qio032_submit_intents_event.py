# -*- coding: utf-8 -*-
"""PM-QIO032 #26：生成待确认任务后发事件（去重：以 intent_id 为身份）。

BRIEF #26：后端生成待确认任务后，原页面无需刷新即可同步虚线卡和任务数；
事件重复或重连后不会生成重复卡。

后端契约（与 lead / i-state 约定，只增不改）：
- 事件名 EventType.INTERACTIVE_INTENTS_CHANGED，归类为关键事件（背压时不静默丢）；
- payload：{board_id, submission_id, intent_ids: [str], statuses: {intent_id: status},
  reason: proposed|updated|confirmed|changed}；
- 客户端按 intent_id 幂等应用 —— 服务端保证事件里同一 intent 只出现一次，
  重复投递 / 重连补发由前端按 intent_id 覆盖（不会生成重复卡）；
- 没有宿主应用 / 总线时静默跳过；通知失败不影响主流程。

本文件覆盖：事件分类、payload 事实、去重身份、没有总线/总线失败时的诚实降级、
HTTP 确认路径真的会发事件。工具路径（board_propose_intents）的接线由 i-material
在 board_tools.py 调用同一个 helper，见其定向用例。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import bus as bus_module
from agent.api import events as events_module
from agent.api import interactive_intents as api_module
from agent.interactive import board_store, intent_service, submission

from test_p3_b_support import (
    add_submission,
    add_visible_card,
    intent_scope,
    intent_spec,
    make_board,
    prepare,
)

BOARD = "board_p3"


class FakeBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event) -> None:
        self.events.append(event)


class FakeOwner:
    def __init__(self, bus: FakeBus) -> None:
        self.bus = bus


@pytest.fixture()
def env(db_conn):
    prepare(db_conn)
    make_board(db_conn, BOARD)
    add_submission(db_conn, BOARD)
    owner = FakeOwner(FakeBus())
    submission.bind_background_owner(db_conn, owner)
    yield db_conn, owner
    submission.unbind_background_owner(db_conn)


def _propose(env, count: int = 2) -> tuple[object, list[str]]:
    conn, _owner = env
    card_id = add_visible_card(conn, BOARD, content="材料一")
    specs = [
        intent_spec(f"a{index}", scope=intent_scope(read_card_ids=[card_id]))
        for index in range(count)
    ]
    proposed = intent_service.propose_intents(
        conn, board_id=BOARD, submission_id="sub_p3_1", intents=specs
    )
    assert proposed["ok"] is True, proposed
    ids = [view["intent_id"] for view in proposed["intents"]]
    return conn, ids


def test_event_type_exists_and_is_critical():
    assert events_module.EventType.INTERACTIVE_INTENTS_CHANGED.value == (
        "INTERACTIVE_INTENTS_CHANGED"
    )
    assert (
        events_module.EventType.INTERACTIVE_INTENTS_CHANGED
        in bus_module.CRITICAL_EVENTS
    )


def test_publish_carries_real_intent_ids_and_statuses(env):
    conn, ids = _propose(env, count=2)
    _conn, owner = env
    published = asyncio.run(
        intent_service.publish_intents_changed(
            conn,
            board_id=BOARD,
            submission_id="sub_p3_1",
            intent_ids=ids,
            reason="proposed",
        )
    )
    assert published is True
    assert len(owner.bus.events) == 1
    event = owner.bus.events[0]
    assert event.type is events_module.EventType.INTERACTIVE_INTENTS_CHANGED
    data = event.data
    assert data["board_id"] == BOARD
    assert data["submission_id"] == "sub_p3_1"
    assert data["reason"] == "proposed"
    assert set(data["intent_ids"]) == set(ids)
    # 每个 intent 的状态是**真实读库结果**，不是调用方编的。
    assert data["statuses"] == {intent_id: "awaiting_confirmation" for intent_id in ids}


def test_duplicate_delivery_is_idempotent_by_intent_id(env):
    """重复事件 / 重连补发：客户端按 intent_id 覆盖，不会生成重复卡。"""
    conn, ids = _propose(env, count=2)
    _conn, owner = env
    for _ in range(2):  # 模拟同一事实重复投递
        assert asyncio.run(
            intent_service.publish_intents_changed(
                conn,
                board_id=BOARD,
                submission_id="sub_p3_1",
                intent_ids=ids,
                reason="proposed",
            )
        )
    # 事件身份各自唯一（前端去重表可用），但应用后卡片身份不重复。
    assert len({event.id for event in owner.bus.events}) == 2
    cards: dict[str, str] = {}
    for event in owner.bus.events:
        for intent_id in event.data["intent_ids"]:
            cards[intent_id] = event.data["statuses"].get(intent_id, "")
    assert set(cards) == set(ids)
    assert len(cards) == len(ids)
    # 同一事件内部也不得出现重复 intent。
    for event in owner.bus.events:
        assert len(event.data["intent_ids"]) == len(set(event.data["intent_ids"]))


def test_unknown_intent_is_not_given_a_fake_status(env):
    conn, ids = _propose(env, count=1)
    _conn, owner = env
    assert asyncio.run(
        intent_service.publish_intents_changed(
            conn,
            board_id=BOARD,
            submission_id="sub_p3_1",
            intent_ids=[ids[0], "intent_missing"],
            reason="proposed",
        )
    )
    statuses = owner.bus.events[0].data["statuses"]
    assert statuses == {ids[0]: "awaiting_confirmation"}
    assert "intent_missing" not in statuses


def test_foreign_board_intent_is_not_reported(env):
    conn, ids = _propose(env, count=1)
    _conn, owner = env
    assert asyncio.run(
        intent_service.publish_intents_changed(
            conn,
            board_id="board_other",
            intent_ids=[ids[0]],
            reason="changed",
        )
    )
    assert owner.bus.events[0].data["statuses"] == {}


def test_no_bus_is_quiet(env):
    conn, ids = _propose(env, count=1)
    submission.unbind_background_owner(conn)
    assert (
        asyncio.run(
            intent_service.publish_intents_changed(
                conn, board_id=BOARD, intent_ids=ids, reason="proposed"
            )
        )
        is False
    )


def test_bus_failure_never_raises(env):
    conn, ids = _propose(env, count=1)
    _conn, owner = env

    class BrokenBus:
        async def publish(self, event):
            raise RuntimeError("sse down")

    owner.bus = BrokenBus()
    assert (
        asyncio.run(
            intent_service.publish_intents_changed(
                conn, board_id=BOARD, intent_ids=ids, reason="proposed"
            )
        )
        is False
    )


def test_http_confirm_publishes_changed_event(env, monkeypatch):
    conn, ids = _propose(env, count=1)
    _conn, owner = env
    # 主循环不可用：只验证「确认成功后发事件」，续作启动由其它用例覆盖。
    monkeypatch.setattr(
        api_module,
        "_start_intent_turn",
        lambda *args, **kwargs: {"ok": False, "turnId": None, "resume": False},
    )
    app = FastAPI()
    app.state.ctx = SimpleNamespace(conn=conn)
    app.include_router(api_module.router)
    client = TestClient(app)

    response = client.post(
        f"/api/interactive/intents/{ids[0]}/confirm", json={"boardId": BOARD}
    )
    assert response.status_code == 200, response.text
    assert response.json().get("ok") is True
    assert owner.bus.events, "确认成功后必须发出意图清单变化事件"
    event = owner.bus.events[-1]
    assert event.type is events_module.EventType.INTERACTIVE_INTENTS_CHANGED
    assert event.data["reason"] == "confirmed"
    assert ids[0] in event.data["intent_ids"]
    assert event.data["statuses"][ids[0]] == "approved"
