# -*- coding: utf-8 -*-
"""R18-B：普通回答保存失败的共享暂存 + 待恢复记录（P2 / R18 契约 §2.2 / §5 B1—B4）。

本轮要修的缺口（基线 6b3f8be5）：普通正式回答生成成功但保存失败时，
`_stash_normal_answer_failure` 只把正文写进 `topic_answer_runs.staged_*`，
**不建** `topic_recovery_records` 记录，于是既有「重试回答」入口
（POST /api/recovery/records/{record_id}/retry-answer）看不到任何事项，用户无法补存。

本文件从契约行为出发（真实 create_app + 真实迁移 + 真实临时 SQLite + fake provider，
绝不联网；只轮询事件/数据库事实，不按真实时间等模型）：

* B1 真实 HTTP：注入一次普通回答写入失败 → 共享暂存（phase=staged、status=pending、
  释放 owner/租约）+ 同事务 pending 恢复记录（kind/failed_stage=answer/answer_key 同源/
  turn/topic/user_message/fragment/recovery_nav_version 如实）；无正式 assistant 消息、
  原 user 消息只有一条；GET /api/recovery/records 出现 answer_pending + retry_answer。
* B1b 同一 answer_key 的重复保存失败复用同一条 pending 记录（不产生多条）。
* B2 解除故障后从既有入口 retry-answer：200 answered；只补存一条正式回答、内容=暂存正文；
  adapter 调用数不增；原问题不重复；记录 answered + answer_id；run 行 answered、暂存清空。
* B3a 重复 retry-answer 返回同一 answer_id（不新增消息 / 不新增模型调用）。
* B3b 并发 retry-answer：只写一条正式回答，同一 answer_id，非执行者不重复生成。
* B3c 执行权被另一执行者持有时 retry-answer 结构化 202 recovery_in_progress、零消息、
  零模型调用；释放后同一共享暂存补存为同一份答案。
* B4a 恢复记录写入失败时 run 行暂存一起回滚（同事务原子性），原异常照抛。
* B4b 另一活跃执行者持有 run 行时不覆盖、不建记录、仍抛原异常（I11 边界）。
* B4c 已 answered 不复活（迟到保存失败不得新建 pending 记录）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.navigation_version import current as current_nav_version
from agent.storage.db import transaction

DECL = "[[QIO:ANSWER]]\n"
BODY = "普通回答正文：保存失败后应能从恢复入口补存"
QUESTION = "请直接回答这个普通问题"
FAILURE = "注入：普通回答保存失败"
RECOVERY_TABLE = "topic_recovery_records"
RUN_TABLE = "topic_answer_runs"
TURN = "turn_r18_b_direct"
TIMEOUT = 60.0


# ── 装置：真实 app + fake provider + 数据库读取 ───────────────────────


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    return application


def _ctx(application):
    return application.state.ctx


def _client(application):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://qio.test",
        timeout=120.0,
    )


def _install_fake_adapter(application) -> FakeStreamAdapter:
    adapter = FakeStreamAdapter([StreamScript(text=DECL + BODY)])
    ctx = _ctx(application)

    async def _build():
        return adapter

    ctx.build_adapter = _build  # type: ignore[method-assign]
    return adapter


def _events(application, kind: str) -> list[dict]:
    return [
        event.data
        for event in list(_ctx(application).bus._history)  # noqa: SLF001 - 与既有 HTTP 用例同一只读取法
        if event.type.value == kind
    ]


async def _wait_turn_end(application, turn_id: str, timeout: float = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for data in _events(application, "TURN_END"):
            if str(data.get("turn_id")) == str(turn_id):
                return data
        await asyncio.sleep(0.02)
    raise AssertionError("TURN_END 没有到达（turn_id=%s）" % turn_id)


async def _start_turn(client, message: str = QUESTION) -> tuple[str, dict]:
    resp = await client.post("/api/turns", json={"message": message})
    assert resp.status_code == 200, resp.text[:500]
    payload = resp.json()
    assert payload.get("accepted") is True, payload
    return str(payload["turn_id"]), payload


def _rows(ctx, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in ctx.conn.execute(sql, params)]


def _user_messages(ctx, turn_id: str | None = None) -> list[dict]:
    if turn_id is None:
        return _rows(
            ctx,
            "SELECT id, content, fragment_id, role, turn_id FROM messages "
            "WHERE role = 'user' ORDER BY created_at, id",
        )
    return _rows(
        ctx,
        "SELECT id, content, fragment_id, role, turn_id FROM messages "
        "WHERE role = 'user' AND turn_id = ? ORDER BY created_at, id",
        (str(turn_id),),
    )


def _assistant_messages(ctx, turn_id: str | None = None) -> list[dict]:
    if turn_id is None:
        return _rows(
            ctx,
            "SELECT id, content, answer_key, fragment_id, turn_id, "
            "late_answer_of_message_id FROM messages WHERE role = 'assistant' "
            "ORDER BY created_at, id",
        )
    return _rows(
        ctx,
        "SELECT id, content, answer_key, fragment_id, turn_id, "
        "late_answer_of_message_id FROM messages "
        "WHERE role = 'assistant' AND turn_id = ? ORDER BY created_at, id",
        (str(turn_id),),
    )


def _topic_of_fragment(ctx, fragment_id: str | None) -> str | None:
    if not fragment_id:
        return None
    row = ctx.conn.execute(
        "SELECT topic_id FROM fragments WHERE id = ?", (str(fragment_id),)
    ).fetchone()
    return str(row["topic_id"]) if row is not None else None


def _run_row(ctx, answer_key: str) -> dict:
    rows = _rows(
        ctx, f"SELECT * FROM {RUN_TABLE} WHERE answer_key = ?", (str(answer_key),)
    )
    return rows[0] if rows else {}


def _record_rows(ctx, answer_key: str | None = None) -> list[dict]:
    if answer_key is None:
        return _rows(ctx, f"SELECT * FROM {RECOVERY_TABLE} ORDER BY created_at, record_id")
    return _rows(
        ctx,
        f"SELECT * FROM {RECOVERY_TABLE} WHERE answer_key = ? "
        "ORDER BY created_at, record_id",
        (str(answer_key),),
    )


def _pending_record_for_turn(ctx, turn_id: str) -> dict:
    """该普通轮保存失败后必须恰好有一条 pending 待恢复记录（直接读权威表）。"""
    rows = [
        row
        for row in _record_rows(ctx)
        if str(row.get("turn_id") or "") == str(turn_id)
        and str(row.get("status") or "") == "pending"
    ]
    assert len(rows) == 1, "保存失败必须自动建出一条 pending 待恢复记录：%s" % rows
    return rows[0]


@pytest.fixture()
def failing_insert(monkeypatch):
    """注入一次普通回答写入失败（保存失败路径的触发点）。"""
    real = topic_recovery._insert_normal_answer_message
    state = {"calls": 0, "real": real}

    def _boom(conn, **kwargs):  # noqa: ANN001, ANN003
        state["calls"] += 1
        raise RuntimeError(FAILURE)

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _boom)
    return state


def _restore_insert(monkeypatch, state) -> None:
    monkeypatch.setattr(
        topic_recovery, "_insert_normal_answer_message", state["real"]
    )


def _prepare_direct(ctx) -> tuple[str, str, str]:
    """直接建一个普通轮的真实素材：话题 + 开放片段 + 绑定 + 原用户消息。"""
    topic_id = ctx.topics.nodes.create_topic("R18-B 普通回答话题").id
    fragment = ctx.fragments.get_or_create_open(topic_id)
    ctx.bindings.record_binding(TURN, topic_id, fragment_id=fragment.id)
    message_id, _closed = ctx.memory.append_message(
        topic_id=topic_id,
        role="user",
        content=QUESTION,
        content_type="text",
        model="fake-user",
        turn_id=TURN,
    )
    return topic_id, fragment.id, message_id


async def _staged_failure_turn(client, application, failing_insert) -> tuple[str, str, str, str]:
    """跑一轮真实普通对话并让它保存失败；返回 (turn_id, answer_key, user_message_id, record_id)。"""
    ctx = _ctx(application)
    turn_id, _accept = await _start_turn(client)
    end = await _wait_turn_end(application, turn_id)
    assert end.get("status") == "failed", end
    assert failing_insert["calls"] == 1, failing_insert
    users = _user_messages(ctx, turn_id)
    assert len(users) == 1, users
    user = users[0]
    answer_key = topic_recovery.answer_key_for(turn_id, user_message_id=user["id"])
    record = _pending_record_for_turn(ctx, turn_id)
    items = await _list_topic_items(client)
    assert [item for item in items if item.get("record_id") == record["record_id"]], (
        "GET /api/recovery/records 必须出现该 pending 记录：%s" % items
    )
    return turn_id, answer_key, user["id"], record["record_id"]


# ── B1：保存失败 → 暂存 + pending 记录（真实 HTTP）────────────────────


async def test_b1_save_failure_stages_and_creates_pending_record(app, failing_insert):
    ctx = _ctx(app)
    adapter = _install_fake_adapter(app)
    async with _client(app) as client:
        turn_id, _accept = await _start_turn(client)
        end = await _wait_turn_end(app, turn_id)
        assert end.get("status") == "failed", end
        assert failing_insert["calls"] == 1, failing_insert

        users = _user_messages(ctx, turn_id)
        assert len(users) == 1, "原用户消息必须只有一条：%s" % users
        user = users[0]
        assert user["content"] == QUESTION
        answer_key = topic_recovery.answer_key_for(
            turn_id, user_message_id=user["id"]
        )

        # (a) 共享暂存唯一权威：run 行 staged_*，phase=staged / status=pending / 释放执行权
        run = _run_row(ctx, answer_key)
        assert run, "保存失败必须建立共享暂存（topic_answer_runs 行）"
        assert run["phase"] == topic_recovery.PHASE_STAGED, run
        assert run["status"] == "pending", run
        assert run["staged_answer"] == BODY, run
        assert run["staged_at"], run
        assert run["staged_model"] == adapter.model, run
        assert run["owner"] is None and run["claim_until"] is None, run
        # 无正式回答半截
        assert _assistant_messages(ctx, turn_id) == [], "保存失败不得留下半截正式回答"

        # (b) GET /api/recovery/records 必须出现该 pending 记录（无需任何手工建记录）
        record = _pending_record_for_turn(ctx, turn_id)
        items = await _list_topic_items(client)
    matched = [item for item in items if item.get("record_id") == record["record_id"]]
    assert len(matched) == 1, "保存失败必须自动建出一条待恢复记录：%s" % items
    item = matched[0]
    assert record["record_id"].startswith("trec_"), record
    assert item["kind"] == topic_recovery.RECOVERY_KIND, item
    assert item["state_class"] == "answer_pending", item
    assert item["status"] == "pending", item
    assert item["failed_stage"] == "answer", item
    assert item["turn_id"] == turn_id, item
    assert item["fragment_id"] == user["fragment_id"], item
    assert item["topic_id"] == _topic_of_fragment(ctx, user["fragment_id"]), item
    assert item["recovery_nav_version"] == current_nav_version(ctx.conn), (
        "recovery_nav_version 必须是失败时刻真实导航版本：%s" % item
    )
    assert isinstance(item["recovery_nav_version"], int), item
    message = str(item.get("message") or "")
    assert "保存失败" in message and "重试" in message, message
    actions = item.get("actions") or []
    assert any(action.get("id") == topic_recovery.ACTION_RETRY_ANSWER for action in actions), item
    assert item["retryable"] is True, item

    # 记录字段真相（清单协议不暴露 answer_key/user_message_id，读权威表核对）
    assert record["kind"] == topic_recovery.RECOVERY_KIND, record
    assert record["answer_key"] == answer_key, record
    assert record["user_message_id"] == user["id"], record
    assert record["fragment_id"] == user["fragment_id"], record
    assert record["topic_id"] == _topic_of_fragment(ctx, user["fragment_id"]), record
    assert record["recovery_nav_version"] == current_nav_version(ctx.conn), record


async def _list_topic_items(client) -> list[dict]:
    resp = await client.get("/api/recovery/records")
    assert resp.status_code == 200, resp.text[:500]
    return [
        item
        for item in resp.json().get("records") or []
        if item.get("kind") == topic_recovery.RECOVERY_KIND
    ]


# ── B1b：同一 answer_key 的重复保存失败复用同一条 pending 记录 ────────


def test_b1b_repeated_save_failures_reuse_single_pending_record(app, failing_insert):
    ctx = _ctx(app)
    topic_id, _fragment_id, user_message_id = _prepare_direct(ctx)
    answer_key = topic_recovery.answer_key_for(TURN, user_message_id=user_message_id)

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match=FAILURE):
            topic_recovery.save_normal_answer(
                ctx,
                topic_id=topic_id,
                turn_id=TURN,
                user_message_id=user_message_id,
                content=BODY,
                model="fake-r18-b",
            )

    rows = _record_rows(ctx, answer_key)
    assert len(rows) == 1, "同一 answer_key 的重复保存失败不得产生多条 pending 记录：%s" % rows
    assert rows[0]["status"] == "pending", rows[0]
    assert rows[0]["failed_stage"] == "answer", rows[0]
    assert rows[0]["answer_key"] == answer_key, rows[0]


# ── B2：故障解除后从既有入口只补存（真实 HTTP）────────────────────────


async def test_b2_retry_answer_only_persists_without_model_call(
    app, failing_insert, monkeypatch
):
    ctx = _ctx(app)
    adapter = _install_fake_adapter(app)
    async with _client(app) as client:
        turn_id, answer_key, _user_message_id, record_id = await _staged_failure_turn(
            client, app, failing_insert
        )
        calls_before = len(adapter.requests)
        assert calls_before == 1, "初始普通轮恰好一次模型调用"

        # 解除故障（真实生成失败已过去，恢复入口不应再碰模型/装配）
        _restore_insert(monkeypatch, failing_insert)
        resp = await client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert resp.status_code == 200, resp.text[:500]
        payload = resp.json()
        assert payload.get("ok") is True and payload.get("answered") is True, payload
        answer_id = payload.get("answer_id")
        assert isinstance(answer_id, str) and answer_id, payload

        # 只补存：模型调用不增
        assert len(adapter.requests) == calls_before, "命中共享暂存后不得再调用模型"
        # 恰好一条正式回答，内容 = 暂存正文，归属原轮次 / 原话题 / 原片段
        assistants = _assistant_messages(ctx, turn_id)
        assert len(assistants) == 1, "同一逻辑答案只能补存一条正式消息：%s" % assistants
        saved = assistants[0]
        assert saved["id"] == answer_id, saved
        assert saved["content"] == BODY, saved
        assert saved["answer_key"] == answer_key, saved
        assert saved["turn_id"] == turn_id, saved
        # 原问题不重复写入
        assert len(_user_messages(ctx, turn_id)) == 1, "恢复不得重复写原用户消息"

        # 记录 answered + answer_id；run answered 且暂存清空
        record = topic_recovery.get_record(ctx, record_id)
        assert record["status"] == "answered", record
        assert record["answer_id"] == answer_id, record
        run = _run_row(ctx, answer_key)
        assert run["status"] == "answered" and run["phase"] == topic_recovery.PHASE_ANSWERED, run
        assert run["answer_id"] == answer_id, run
        assert run["staged_answer"] == "", run
        assert run["staged_at"] is None and run["staged_model"] is None, run

        # answered 后不再出现在未完成清单
        assert await _list_topic_items(client) == []


# ── B3a：重复 retry-answer 返回同一 answer_id ──────────────────────────


async def test_b3a_repeated_retry_returns_same_answer_id(
    app, failing_insert, monkeypatch
):
    ctx = _ctx(app)
    adapter = _install_fake_adapter(app)
    async with _client(app) as client:
        turn_id, answer_key, _user_message_id, record_id = await _staged_failure_turn(
            client, app, failing_insert
        )
        calls_before = len(adapter.requests)
        _restore_insert(monkeypatch, failing_insert)
        first = await client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert first.status_code == 200, first.text[:500]
        first_id = first.json()["answer_id"]

        second = await client.post(f"/api/recovery/records/{record_id}/retry-answer")
        assert second.status_code == 200, second.text[:500]
        payload = second.json()
        assert payload.get("answered") is True, payload
        assert payload.get("answer_id") == first_id, payload
        assert len(_assistant_messages(ctx, turn_id)) == 1, "重复重试不得新增消息"
        assert len(adapter.requests) == calls_before, "重复重试不得新增模型调用"
        record = topic_recovery.get_record(ctx, record_id)
        assert record["status"] == "answered" and record["answer_id"] == first_id


# ── B3b：并发 retry-answer 只写一条正式回答 ────────────────────────────


async def test_b3b_concurrent_retry_writes_single_answer(
    app, failing_insert, monkeypatch
):
    ctx = _ctx(app)
    adapter = _install_fake_adapter(app)
    async with _client(app) as client:
        turn_id, answer_key, _user_message_id, record_id = await _staged_failure_turn(
            client, app, failing_insert
        )
        calls_before = len(adapter.requests)
        _restore_insert(monkeypatch, failing_insert)
        url = f"/api/recovery/records/{record_id}/retry-answer"
        first, second = await asyncio.gather(client.post(url), client.post(url))

        answer_ids: list[str] = []
        for resp in (first, second):
            assert resp.status_code in (200, 202), resp.text[:500]
            payload = resp.json()
            if resp.status_code == 202:
                # 非执行者不重复生成：结构化「处理中」
                assert payload.get("code") == "recovery_in_progress", payload
            else:
                assert payload.get("answered") is True, payload
                answer_ids.append(str(payload["answer_id"]))
        if len(answer_ids) < 2:
            final = await client.post(url)
            assert final.status_code == 200, final.text[:500]
            answer_ids.append(str(final.json()["answer_id"]))

        assert len(set(answer_ids)) == 1, "并发/重复重试必须返回同一份正式答案：%s" % answer_ids
        assistants = _assistant_messages(ctx, turn_id)
        assert len(assistants) == 1, assistants
        assert assistants[0]["id"] == answer_ids[0]
        assert len(adapter.requests) == calls_before, "并发重试不得新增模型调用"


# ── B3c：执行权被另一执行者持有时非执行者不重复生成 ────────────────────


async def test_b3c_non_executor_gets_in_progress_then_same_answer(
    app, failing_insert, monkeypatch
):
    ctx = _ctx(app)
    adapter = _install_fake_adapter(app)
    async with _client(app) as client:
        turn_id, answer_key, _user_message_id, record_id = await _staged_failure_turn(
            client, app, failing_insert
        )
        calls_before = len(adapter.requests)
        _restore_insert(monkeypatch, failing_insert)
        url = f"/api/recovery/records/{record_id}/retry-answer"

        # 模拟另一个真实执行者正在生成：持有执行权 + 记录租约
        execution, disposition = topic_recovery._acquire_execution(
            ctx.conn,
            ctx,
            answer_key=answer_key,
            owner="other-instance|r18b-test",
            lease_seconds=topic_recovery.LEASE_SECONDS,
        )
        assert disposition == "acquired" and execution is not None, disposition
        execution["record_id"] = record_id  # 与 retry_topic_answer 相同的绑定前置
        topic_recovery._bind_record_execution(ctx.conn, execution, record_id)

        blocked = await client.post(url)
        assert blocked.status_code == 202, blocked.text[:500]
        payload = blocked.json()
        assert payload.get("code") == "recovery_in_progress", payload
        assert _assistant_messages(ctx, turn_id) == [], "非执行者不得写正式回答"
        assert len(adapter.requests) == calls_before, "非执行者不得重复生成回答"

        # 另一执行者释放（保留共享暂存）后，同一暂存补存为同一份答案
        assert (
            topic_recovery._release_execution(
                ctx.conn, execution, phase=topic_recovery.PHASE_STAGED
            )
            is True
        )
        done = await client.post(url)
        assert done.status_code == 200, done.text[:500]
        result = done.json()
        assert result.get("answered") is True, result
        assistants = _assistant_messages(ctx, turn_id)
        assert len(assistants) == 1, assistants
        assert assistants[0]["id"] == result["answer_id"]
        assert assistants[0]["content"] == BODY
        assert len(adapter.requests) == calls_before, "补存共享暂存不得调用模型"


# ── B4a：暂存与记录同一事务（记录写失败 → run 行一起回滚）──────────────


def test_b4a_record_write_failure_rolls_back_staged_run(app, monkeypatch):
    ctx = _ctx(app)
    topic_id, _fragment_id, user_message_id = _prepare_direct(ctx)
    answer_key = topic_recovery.answer_key_for(TURN, user_message_id=user_message_id)

    def _boom_insert(conn, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError(FAILURE)

    def _boom_record(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("注入：恢复记录写入失败")

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _boom_insert)
    monkeypatch.setattr(topic_recovery, "record_topic_failure", _boom_record)

    with pytest.raises(RuntimeError, match=FAILURE):
        topic_recovery.save_normal_answer(
            ctx,
            topic_id=topic_id,
            turn_id=TURN,
            user_message_id=user_message_id,
            content=BODY,
            model="fake-r18-b",
        )

    assert _run_row(ctx, answer_key) == {}, (
        "记录写入失败必须让暂存一起回滚（不得留下 staged 半截）"
    )
    assert _record_rows(ctx, answer_key) == []
    assert _assistant_messages(ctx) == []


# ── B4b：另一活跃执行者持有 run 行 → 不覆盖、不建记录、原异常照抛 ─────

def test_b4b_active_executor_boundary_writes_nothing(app, failing_insert):
    ctx = _ctx(app)
    topic_id, _fragment_id, user_message_id = _prepare_direct(ctx)
    answer_key = topic_recovery.answer_key_for(TURN, user_message_id=user_message_id)
    existing = topic_recovery.record_topic_failure(
        ctx,
        turn_id=TURN,
        topic_id=topic_id,
        failed_stage="context_prepare",
        message="先有的一条待恢复记录",
        user_message_id=user_message_id,
        recovery_nav_version=current_nav_version(ctx.conn),
    )

    execution, disposition = topic_recovery._acquire_execution(
        ctx.conn,
        ctx,
        answer_key=answer_key,
        owner="other-instance|r18b-holder",
        lease_seconds=topic_recovery.LEASE_SECONDS,
    )
    assert disposition == "acquired" and execution is not None, disposition
    topic_recovery._bind_record_execution(ctx.conn, execution, existing["record_id"])
    before = _run_row(ctx, answer_key)

    with pytest.raises(RuntimeError, match=FAILURE):
        topic_recovery.save_normal_answer(
            ctx,
            topic_id=topic_id,
            turn_id=TURN,
            user_message_id=user_message_id,
            content=BODY,
            model="fake-r18-b",
        )

    after = _run_row(ctx, answer_key)
    assert after["status"] == "running", after
    assert after["owner"] == before["owner"], "不得覆盖另一活跃执行者的租约"
    assert after["generation"] == before["generation"], after
    assert after["staged_answer"] == "", "不得写别人执行权下的暂存"
    rows = _record_rows(ctx, answer_key)
    assert len(rows) == 1, "活跃执行者持有时不得另建恢复记录：%s" % rows
    assert rows[0]["record_id"] == existing["record_id"], rows
    assert rows[0]["status"] == "pending", rows[0]
    assert _assistant_messages(ctx) == []


# ── B4c：已 answered 不复活 ───────────────────────────────────────────


def test_b4c_answered_run_does_not_revive_pending_record(app, monkeypatch):
    ctx = _ctx(app)
    topic_id, _fragment_id, user_message_id = _prepare_direct(ctx)
    answer_key = topic_recovery.answer_key_for(TURN, user_message_id=user_message_id)

    with transaction(ctx.conn):
        topic_recovery._ensure_execution_row(ctx.conn, answer_key)
        ctx.conn.execute(
            f"UPDATE {RUN_TABLE} SET status = 'answered', phase = ?, answer_id = ? "
            "WHERE answer_key = ?",
            (topic_recovery.PHASE_ANSWERED, "msg_already_answered", answer_key),
        )

    def _boom_insert(conn, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError(FAILURE)

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _boom_insert)
    with pytest.raises(RuntimeError, match=FAILURE):
        topic_recovery.save_normal_answer(
            ctx,
            topic_id=topic_id,
            turn_id=TURN,
            user_message_id=user_message_id,
            content=BODY,
            model="fake-r18-b",
        )

    run = _run_row(ctx, answer_key)
    assert run["status"] == "answered", run
    assert _record_rows(ctx, answer_key) == [], "已 answered 不得复活/新建 pending 恢复记录"
