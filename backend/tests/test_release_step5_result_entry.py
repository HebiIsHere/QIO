# -*- coding: utf-8 -*-
"""R5 定向：确认路径下「结果入口」的只读账本回退（结果定位修复 A）。

背景：确认→board 工具路径只写 board_operations、不写 board_intents.applied，
导致 records._result_entry 在真实确认后没有结果入口。修复只在读取侧回退到权威账本：
* 意图 completed 且 applied 为空，但账本有 applied 的 create_card → 结果入口非空；
* 没有任何真实结果对象 → 仍为 None（不编造入口）；
* group_cards / add_link 的创建字段按 board_tools 真实 result 形状读取。

不联网、不调用真实模型；运行：
cd backend; ./.venv/Scripts/python.exe -m pytest tests/test_release_step5_result_entry.py -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agent.interactive import models

from test_release_step2_d import (
    BOARD,
    _append_record_message,
    _journal,
    _make_app,
    _record_of,
    _seed_approved_intents,
    _set_intent,
    _topic,
)


@pytest.fixture()
def env(db_conn, settings):
    app = _make_app(db_conn, settings)
    with TestClient(app) as client:
        yield client, app.state.ctx


def _insert_operation(
    conn,
    intent_id: str,
    *,
    action: str,
    result: dict,
    operation_key: str = "op_r5_1",
    status: str = "applied",
) -> None:
    """按真实账本列写一条 applied 操作（result 形状与 board_tools 的写工具一致）。"""
    stamp = models.now_iso()
    conn.execute(
        "INSERT INTO board_operations"
        " (id, board_id, intent_id, operation_key, request_hash, action, status,"
        "  before, after, result, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "bo_" + operation_key,
            BOARD,
            intent_id,
            operation_key,
            "hash_r5",
            action,
            status,
            "{}",
            "{}",
            json.dumps(result, ensure_ascii=False),
            stamp,
            stamp,
        ),
    )


def _record(client, ctx, intent_ids, turn_id: str = "turn_r5_1") -> dict:
    topic, _fragment = _topic(ctx)
    message_id = _append_record_message(
        ctx, topic, intent_ids=intent_ids, turn_id=turn_id, source="confirm"
    )
    _journal(ctx.conn, turn_id, "completed")
    messages = client.get(
        "/api/session/messages", params={"topic_id": topic}
    ).json()["messages"]
    return _record_of(messages, message_id)


def test_ledger_create_card_gives_result_entry_when_applied_empty(env):
    """①完成意图 applied={}，账本有 applied create_card → 结果入口非空且计数正确。"""
    client, ctx = env
    intent_id = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))[0]
    _set_intent(ctx.conn, intent_id, status="completed", outcome="completed", applied={})
    _insert_operation(
        ctx.conn,
        intent_id,
        action="create_card",
        result={"card_id": "c_r5_1", "card": {"id": "c_r5_1"}, "action": "create_card"},
    )

    record = _record(client, ctx, [intent_id])

    assert record["status"] == "completed"
    assert record["result_entry"] == {
        "kind": "board",
        "label": "板面结果：1 张卡片",
        "target": BOARD,
    }
    assert record["intents"][0]["result_ref"] == {
        "kind": "board",
        "label": "板面结果：1 张卡片",
        "target": BOARD,
    }


def test_no_real_result_objects_still_returns_none(env):
    """②没有任何真实结果操作 → 结果入口仍为 None（不编造可点入口）。"""
    client, ctx = env
    intent_id = _seed_approved_intents(ctx.conn, titles=("没有产物",))[0]
    _set_intent(ctx.conn, intent_id, status="completed", outcome="completed", applied={})

    record = _record(client, ctx, [intent_id], turn_id="turn_r5_2")

    assert record["status"] == "completed"
    assert record["result_entry"] is None
    assert record["intents"][0]["result_ref"] is None


def test_ledger_group_and_link_result_shapes(env):
    """③group_cards 取 created_group_id、add_link 取 created=true 的 link_id。"""
    client, ctx = env
    intent_id = _seed_approved_intents(ctx.conn, titles=("建组与连线",))[0]
    _set_intent(ctx.conn, intent_id, status="completed", outcome="completed", applied={})
    _insert_operation(
        ctx.conn,
        intent_id,
        action="create_card",
        result={"card_id": "c_r5_2", "card": {"id": "c_r5_2"}},
        operation_key="op_r5_card",
    )
    _insert_operation(
        ctx.conn,
        intent_id,
        action="group_cards",
        result={"group_id": "g_r5", "created_group_id": "g_r5", "action": "group_cards"},
        operation_key="op_r5_group",
    )
    # add_link：已有关系（created=false）不得计入结果入口。
    _insert_operation(
        ctx.conn,
        intent_id,
        action="add_link",
        result={"link_id": "l_existing", "created": False, "action": "add_link"},
        operation_key="op_r5_link_old",
    )
    _insert_operation(
        ctx.conn,
        intent_id,
        action="add_link",
        result={"link_id": "l_r5", "created": True, "action": "add_link"},
        operation_key="op_r5_link_new",
    )

    record = _record(client, ctx, [intent_id], turn_id="turn_r5_3")

    assert record["result_entry"]["label"] == "板面结果：1 张卡片、1 个分组、1 条关系"
    assert record["result_entry"]["target"] == BOARD
