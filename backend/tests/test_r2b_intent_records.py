# -*- coding: utf-8 -*-
"""R2 返修契约（D:/qio-dev/r2b-intent-contract.md）：确认版本 / 名称快照 / 分页 head。

契约 §四 反例逐条钉住：
1. 同一意图集重新确认（授权版本 +1）→ 新 record_key，不得合并；旧记录仍在且名称是
   旧确认时的名称，新记录用新名称；
2. 同一次确认的自动重试（授权版本不变）→ 同一 record_key，attempts 增加；
3. 分页：第二页只包含该 record 的后续消息时，该页仍有且**仅有**一条
   record_head=true，状态 / attempts 仍按整个话题派生；
4. 旧消息（旧 key、无名称快照）按它们自己的 key 归组，名称退回当前
   board_intents.title —— 不迁移、不重写、不按文本猜。

不联网、不用真实 Key（fake provider / 直接落库夹具）。
运行：cd backend; ./.venv/Scripts/python.exe -m pytest tests/test_r2b_intent_records.py -q
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.interactive import intent_service, records

from test_release_step2_d import (
    BOARD,
    SUBMISSION,
    _append_record_message,
    _bind_submission_topic,
    _get_messages,
    _make_app,
    _record_of,
    _RecordingAdapter,
    _seed_approved_intents,
    _topic,
    _wait_turn_end,
)


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_env(db_conn, settings):
    """只读派生夹具：消息经真实 HTTP 读取通道返回（/api/session/messages）。"""
    app = _make_app(db_conn, settings)
    with TestClient(app) as client:
        yield client, app.state.ctx


def _append(
    ctx,
    topic,
    *,
    intent_ids,
    turn_id,
    versions=None,
    titles=None,
    attempt=1,
    source="confirm",
    created_at=None,
):
    """按**新写侧**落一条记录消息：带确认版本段的 record_key + 名称快照。"""
    vs = dict(versions or {str(item): 1 for item in intent_ids})
    key = records.build_record_key(SUBMISSION, intent_ids, vs)
    snapshot = None
    if titles is not None:
        snapshot = [
            {"id": str(intent_id), "title": str(titles.get(str(intent_id), ""))}
            for intent_id in intent_ids
        ]
    return _append_record_message(
        ctx,
        topic,
        intent_ids=intent_ids,
        turn_id=turn_id,
        attempt=attempt,
        source=source,
        record_key=key,
        versions=vs,
        intent_titles=snapshot,
        created_at=created_at,
    )


def _fake_turn_submitter():
    calls: list[dict] = []

    def submit(message, topic_id=None):
        turn = {
            "turn_id": f"turn_r2b_{len(calls) + 1}",
            "message": message,
            "topic_id": topic_id,
        }
        calls.append(turn)
        return turn

    return submit, calls


def _write_turn_facts(ctx, topic: str, facts: dict) -> str:
    """模拟 turn_orchestrator 落库：raw 原样透传程序事实（content 不动）。"""
    message_id, _fragment = ctx.memory.append_message(
        topic_id=topic,
        role="user",
        content="【互动意图续作】这是程序绑定的一次真实续作，不是新的用户提交。",
        content_type=records.CONTENT_TYPE,
        raw={records.RAW_KEY: dict(facts)},
        turn_id=str(facts.get("turn_id") or ""),
    )
    return message_id


# ---------------------------------------------------------------------------
# 契约 §一：record_key 的确认版本段
# ---------------------------------------------------------------------------


def test_record_key_carries_authorization_version_signature():
    """新格式 ie:<sub>:<sig>:<ids>；sig 按 intent_id 排序；旧格式仍可生成。"""
    assert records.build_version_sig({"i2": 3, "i1": 2}) == "i1=v2,i2=v3"
    assert (
        records.build_record_key("sub1", ["i2", "i1"], {"i1": 2, "i2": 3})
        == "ie:sub1:i1=v2,i2=v3:i1,i2"
    )
    # 版本签名只包含本次意图集里的意图（不会把别处版本带进 key）
    assert (
        records.build_record_key("sub1", ["i1"], {"i1": 2, "i9": 9})
        == "ie:sub1:i1=v2:i1"
    )
    # 没有版本事实 → 旧格式（旧调用方 / 旧记录兼容）
    assert records.build_record_key("sub1", ["i2", "i1"]) == "ie:sub1:i1,i2"
    # 缺身份 / 空意图集 → 空串（调用方按「无法核对」处理）
    assert records.build_record_key("", ["i1"]) == ""
    assert records.build_record_key("sub1", []) == ""


def test_turn_record_facts_writes_version_segment_and_title_snapshot():
    """写侧事实：record_key 用真实授权版本；快照只留本次意图集、空名称丢掉。"""
    facts = records.turn_record_facts(
        board_id="b",
        submission_id="s",
        topic_id="t",
        intent_ids=["i2", "i1"],
        source="confirm",
        turn_id="turn",
        attempt=1,
        authorization_versions={"i1": 2, "i2": 3},
        intent_titles=[
            {"id": "i2", "title": "乙"},
            {"id": "i1", "title": "甲"},
            {"id": "i9", "title": "不属于本次记录的意图"},
            {"id": "i1", "title": ""},
        ],
    )
    assert facts["record_key"] == "ie:s:i1=v2,i2=v3:i1,i2"
    assert facts["intent_titles"] == [
        {"id": "i2", "title": "乙"},
        {"id": "i1", "title": "甲"},
    ]


# ---------------------------------------------------------------------------
# 契约 §四 反例 1：重新确认（授权版本变化）→ 新记录；旧记录保留旧名称
# ---------------------------------------------------------------------------


def test_reconfirm_with_new_authorization_version_creates_new_record(record_env):
    """同一意图集重新确认（版本 +1）：两条记录、两个 key、各自确认时的名称。"""
    client, ctx = record_env
    from agent.interactive import submission

    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    topic, _fragment = _topic(ctx)
    _bind_submission_topic(ctx.conn, topic)
    submit, calls = _fake_turn_submitter()

    first = submission.start_interactive_turn(
        ctx.conn,
        turn_submitter=submit,
        board_id=BOARD,
        intent_ids=ids,
        submission_id=SUBMISSION,
        source="confirm",
    )
    assert first["ok"] is True, first
    facts_v1 = calls[0]["board_intent_execution"]
    assert facts_v1["authorization_versions"] == {intent_id: 1}
    assert facts_v1["intent_titles"] == [{"id": intent_id, "title": "整理这组资料"}]
    first_id = _write_turn_facts(ctx, topic, facts_v1)
    submission.release_intent_turns(
        ctx.conn, intent_ids=ids, turn_id=str(facts_v1["turn_id"]), status="done"
    )

    # 用户改掉意图（名称 + 实质内容 → 旧授权失效），再重新确认 → 授权版本 +1
    updated = intent_service.update_intent(
        ctx.conn,
        board_id=BOARD,
        intent_id=intent_id,
        expected_version=1,
        changes={
            "title": "整理这组资料（新）",
            "goal": "按新的范围重新整理这组资料",
        },
    )
    assert updated.get("ok") is True, updated
    reconfirmed = intent_service.confirm_intents(
        ctx.conn, board_id=BOARD, intent_ids=ids
    )
    assert reconfirmed.get("ok") is True, reconfirmed

    second = submission.start_interactive_turn(
        ctx.conn,
        turn_submitter=submit,
        board_id=BOARD,
        intent_ids=ids,
        submission_id=SUBMISSION,
        source="confirm",
    )
    assert second["ok"] is True, second
    facts_v2 = calls[1]["board_intent_execution"]
    assert facts_v2["authorization_versions"] == {intent_id: 2}
    assert facts_v2["intent_titles"] == [{"id": intent_id, "title": "整理这组资料（新）"}]
    second_id = _write_turn_facts(ctx, topic, facts_v2)

    # 版本变化 → 不同 record_key，绝不合并
    assert facts_v1["record_key"] == "ie:" + SUBMISSION + ":" + intent_id + "=v1:" + intent_id
    assert facts_v2["record_key"] == "ie:" + SUBMISSION + ":" + intent_id + "=v2:" + intent_id
    assert facts_v1["record_key"] != facts_v2["record_key"]

    messages = _get_messages(client, topic)
    old = _record_of(messages, first_id)
    new = _record_of(messages, second_id)
    assert old["record_key"] == facts_v1["record_key"]
    assert new["record_key"] == facts_v2["record_key"]
    # 旧记录仍是旧确认时的名称；新记录用新名称
    assert old["title"] == "整理这组资料"
    assert new["title"] == "整理这组资料（新）"
    assert old["intents"][0]["title"] == "整理这组资料"
    assert new["intents"][0]["title"] == "整理这组资料（新）"
    assert old["scope"]["intent_titles"] == ["整理这组资料"]
    assert new["scope"]["intent_titles"] == ["整理这组资料（新）"]
    # 两次确认是两条独立记录：各自只有一条 attempt（不合并时间线）
    assert len(old["attempts"]) == 1
    assert len(new["attempts"]) == 1
    assert old["record_head"] is True and new["record_head"] is True

    # 旧消息 raw 原样保留：不按新版本重写、不清空旧快照
    row = ctx.conn.execute(
        "SELECT raw FROM messages WHERE id = ?", (first_id,)
    ).fetchone()
    raw_facts = json.loads(row["raw"])[records.RAW_KEY]
    assert raw_facts["record_key"] == facts_v1["record_key"]
    assert raw_facts["authorization_versions"] == {intent_id: 1}
    assert raw_facts["intent_titles"] == [{"id": intent_id, "title": "整理这组资料"}]


def test_reconfirm_through_http_api_writes_new_versioned_record(db_conn, settings):
    """端到端：真实确认接口跑两轮（改名 + 重新确认），落库事实与读取派生一致。"""
    app = _make_app(db_conn, settings)
    app.state.ctx.build_adapter = AsyncMock(return_value=_RecordingAdapter())
    with TestClient(app) as client:
        ctx = app.state.ctx
        ids = _seed_approved_intents(db_conn, titles=("整理这组资料",))
        intent_id = ids[0]
        topic, _fragment = _topic(ctx)
        _bind_submission_topic(db_conn, topic)

        first = client.post(
            f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
        ).json()
        assert first.get("started") is True, first
        _wait_turn_end(app, str(first["turnId"]))

        current_version = int(
            db_conn.execute(
                "SELECT version FROM board_intents WHERE id = ?", (intent_id,)
            ).fetchone()["version"]
        )
        updated = intent_service.update_intent(
            db_conn,
            board_id=BOARD,
            intent_id=intent_id,
            expected_version=current_version,
            changes={
                "title": "整理这组资料（新）",
                "goal": "按新的范围重新整理这组资料",
            },
        )
        assert updated.get("ok") is True, updated

        second = client.post(
            f"/api/interactive/intents/{intent_id}/confirm", json={"boardId": BOARD}
        ).json()
        assert second.get("started") is True, second
        _wait_turn_end(app, str(second["turnId"]))

        rows = db_conn.execute(
            "SELECT id, raw FROM messages WHERE role = 'user' AND content_type = ?"
            " ORDER BY created_at, id",
            (records.CONTENT_TYPE,),
        ).fetchall()
        assert len(rows) == 2, [dict(row) for row in rows]
        facts = [json.loads(row["raw"])[records.RAW_KEY] for row in rows]
        assert facts[0]["record_key"] == "ie:" + SUBMISSION + ":" + intent_id + "=v1:" + intent_id
        assert facts[1]["record_key"] == "ie:" + SUBMISSION + ":" + intent_id + "=v2:" + intent_id
        assert facts[0]["intent_titles"] == [{"id": intent_id, "title": "整理这组资料"}]
        assert facts[1]["intent_titles"] == [
            {"id": intent_id, "title": "整理这组资料（新）"}
        ]

        shown = [
            message
            for message in _get_messages(client, topic)
            if message.get("content_type") == records.CONTENT_TYPE
        ]
        assert len(shown) == 2, shown
        by_key = {
            message["intent_execution"]["record_key"]: message["intent_execution"]
            for message in shown
        }
        old = by_key[facts[0]["record_key"]]
        new = by_key[facts[1]["record_key"]]
        assert old["title"] == "整理这组资料"
        assert new["title"] == "整理这组资料（新）"
        assert old["record_head"] is True and new["record_head"] is True


# ---------------------------------------------------------------------------
# 契约 §四 反例 2/3：自动重试不变更 key；分页后每页仍有且仅有一条 head
# ---------------------------------------------------------------------------


def test_auto_retry_same_version_keeps_one_record_and_page_head(record_env):
    """授权版本不变 → 同一 record_key；整页只有最早一条 head；翻页后仍可见。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    topic, _fragment = _topic(ctx)
    versions = {intent_id: 1}
    titles = {intent_id: "整理这组资料"}

    first_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_auto_1",
        versions=versions,
        titles=titles,
        created_at="2026-10-29T06:00:00+00:00",
    )
    second_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_auto_2",
        versions=versions,
        titles=titles,
        attempt=2,
        source="retry",
        created_at="2026-10-29T06:01:00+00:00",
    )

    expected_key = "ie:" + SUBMISSION + ":" + intent_id + "=v1:" + intent_id
    full_page = _get_messages(client, topic)
    first = _record_of(full_page, first_id)
    second = _record_of(full_page, second_id)
    assert first["record_key"] == second["record_key"] == expected_key
    # 同一页里两条都在：只有最早那条 head
    assert first["record_head"] is True
    assert second["record_head"] is False
    assert [item["attempt"] for item in second["attempts"]] == [1, 2]
    assert second["intents"][0]["attempts"] == 2

    # 第二页只包含该 record 的后续消息：本页仍有且仅有一条 head（记录不被隐藏）
    latest_page = _get_messages(client, topic, limit=1)
    assert [item["id"] for item in latest_page] == [second_id]
    page_record = _record_of(latest_page, second_id)
    assert page_record["record_key"] == expected_key
    assert page_record["record_head"] is True
    # attempts 仍按整个话题派生（不受分页限制）
    assert [item["attempt"] for item in page_record["attempts"]] == [1, 2]

    cursor = latest_page[0]["created_at"] + "|" + latest_page[0]["id"]
    older_page = _get_messages(client, topic, limit=1, before=cursor)
    assert [item["id"] for item in older_page] == [first_id]
    older_record = _record_of(older_page, first_id)
    assert older_record["record_head"] is True
    assert [item["attempt"] for item in older_record["attempts"]] == [1, 2]


def test_paged_head_is_computed_per_page_and_per_record(record_env):
    """一页里两条记录各有一条 head；同 record 被拆到两页也各页可见。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    topic, _fragment = _topic(ctx)

    # record A（v1）两次尝试；record B（v2）一次尝试 —— 分页会切开 A
    a_first_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_a1",
        versions={intent_id: 1},
        titles={intent_id: "整理这组资料"},
        created_at="2026-10-29T08:00:00+00:00",
    )
    b_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_b1",
        versions={intent_id: 2},
        titles={intent_id: "整理这组资料（新）"},
        created_at="2026-10-29T08:01:00+00:00",
    )
    a_second_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_a2",
        versions={intent_id: 1},
        titles={intent_id: "整理这组资料"},
        attempt=2,
        source="retry",
        created_at="2026-10-29T08:02:00+00:00",
    )

    page = _get_messages(client, topic, limit=2)
    assert [item["id"] for item in page] == [b_id, a_second_id]
    a_here = _record_of(page, a_second_id)
    b_here = _record_of(page, b_id)
    # 两条记录在这一页里各有一条 head（同 key 不会出现两条）
    assert a_here["record_head"] is True
    assert b_here["record_head"] is True
    assert a_here["record_key"] != b_here["record_key"]
    # A 的 attempts 仍是整个话题的两条（第二页不影响派生口径）
    assert [item["attempt"] for item in a_here["attempts"]] == [1, 2]

    full_page = _get_messages(client, topic)
    assert _record_of(full_page, a_first_id)["record_head"] is True
    assert _record_of(full_page, a_second_id)["record_head"] is False
    assert _record_of(full_page, b_id)["record_head"] is True


# ---------------------------------------------------------------------------
# 契约 §四 反例 4：旧消息（旧 key、无快照）原样兼容
# ---------------------------------------------------------------------------


def test_legacy_key_without_snapshot_keeps_old_key_and_current_name(record_env):
    """旧 key 记录不被新逻辑改写 / 合并；没有快照 → 名称退回当前 board_intents.title。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    topic, _fragment = _topic(ctx)

    # 升级前写入：旧 key + raw 里没有 intent_titles 快照
    legacy_id = _append_record_message(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_legacy",
        created_at="2026-10-29T09:00:00+00:00",
    )
    legacy_key = "ie:" + SUBMISSION + ":" + intent_id

    # 升级后用户改名并重新确认，写出新版本段记录
    ctx.conn.execute(
        "UPDATE board_intents SET title = ?, authorization = ? WHERE id = ?",
        (
            "整理这组资料（新）",
            json.dumps({"version": 2, "scope": {}, "confirmed_by": "user"}),
            intent_id,
        ),
    )
    new_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_new",
        versions={intent_id: 2},
        titles={intent_id: "整理这组资料（新）"},
        created_at="2026-10-29T09:01:00+00:00",
    )

    messages = _get_messages(client, topic)
    legacy = _record_of(messages, legacy_id)
    new = _record_of(messages, new_id)
    # 旧 key 原样保留，且绝不与新记录合并
    assert legacy["record_key"] == legacy_key
    assert new["record_key"] == "ie:" + SUBMISSION + ":" + intent_id + "=v2:" + intent_id
    assert legacy["record_key"] != new["record_key"]
    # 旧记录没有快照 → 退回当前名称（不猜、不编造）
    assert legacy["title"] == "整理这组资料（新）"
    assert legacy["intents"][0]["title"] == "整理这组资料（新）"
    # 新记录用自己确认时的快照
    assert new["title"] == "整理这组资料（新）"
    # 旧消息 raw 一条都没被改写（key / content_type / 无快照）
    row = ctx.conn.execute(
        "SELECT raw, content_type FROM messages WHERE id = ?", (legacy_id,)
    ).fetchone()
    assert row["content_type"] == records.CONTENT_TYPE
    raw_facts = json.loads(row["raw"])[records.RAW_KEY]
    assert raw_facts["record_key"] == legacy_key
    assert "intent_titles" not in raw_facts
    assert len(legacy["attempts"]) == 1


def test_snapshot_beats_current_title_after_rename(record_env):
    """改名后，带快照的旧记录仍显示确认时的名称（快照优先）。"""
    client, ctx = record_env
    ids = _seed_approved_intents(ctx.conn, titles=("整理这组资料",))
    intent_id = ids[0]
    topic, _fragment = _topic(ctx)
    message_id = _append(
        ctx,
        topic,
        intent_ids=ids,
        turn_id="turn_r2b_snapshot",
        versions={intent_id: 1},
        titles={intent_id: "整理这组资料"},
    )
    ctx.conn.execute(
        "UPDATE board_intents SET title = ? WHERE id = ?",
        ("整理这组资料（改过名）", intent_id),
    )
    record = _record_of(_get_messages(client, topic), message_id)
    assert record["title"] == "整理这组资料"
    assert record["intents"][0]["title"] == "整理这组资料"
    assert record["scope"]["intent_titles"] == ["整理这组资料"]
