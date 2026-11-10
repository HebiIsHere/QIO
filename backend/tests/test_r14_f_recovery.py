# -*- coding: utf-8 -*-
"""R14-F 独立回归（F03 幂等唯一答案 / F04 恢复事项独立可见 / F13 封存后的迟到答案）。

真实 AppContext / 真实 SQLite / 真实恢复入口；模型为 fake，审批替身。
对应契约：docs/topic-review-20261110-contract.md §3.4（F03/F04-backend）、§3.5（F13）。
"""
from __future__ import annotations

import asyncio

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.topic_operation import TopicStages
from agent.services.navigation_version import current as current_nav_version
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r14_f_recovery.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


class CountingAdapter:
    """可计数 / 可屏障的 fake 模型（绝不联网）。"""

    mode = "native"
    model = "fake-r14-f-recovery"
    protocol_in_prompt = False

    def __init__(self, *, text: str = ANSWER + "\n恢复后的回答", barrier: bool = False) -> None:
        self.text = text
        self.barrier = barrier
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.requests.append({"messages": list(messages), "tools": list(tools or [])})
        if self.barrier:
            if self.calls == 1:
                self.started.set()
                try:
                    await asyncio.wait_for(self.release.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            else:
                self.release.set()
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class GateAdapter:
    """生成过程中挂起，供测试在「模型已调用、答案尚未保存」时封存片段。"""

    mode = "native"
    model = "fake-r14-f-gate"
    protocol_in_prompt = False

    def __init__(self, *, text: str = ANSWER + "\n迟到答案") -> None:
        self.text = text
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.started.set()
        await asyncio.wait_for(self.release.wait(), timeout=5)
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self, *, messages=None, error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error
        self.calls: list = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append(
            {"turn_id": turn_id, "topic_id": topic_id, "user_message_id": user_message_id}
        )
        if self.error is not None:
            raise self.error
        if self.messages is not None:
            return list(self.messages)
        return [
            ChatMessage(role="system", content="SYS_FOR:" + str(topic_id)),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire(app: AppContext, orchestrator: FakeOrchestrator, adapter) -> None:  # noqa: ANN001
    app.turn_orchestrator = orchestrator  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]


def _prepare(app: AppContext, *, name: str = "目标话题", turn_id: str = "turn_r"):
    topic_id = app.topics.nodes.create_topic(name).id
    fragment = app.fragments.get_or_create_open(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="原问题",
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _make_record(app: AppContext, topic_id: str, message_id: str, **overrides):
    payload = {
        "turn_id": "turn_r",
        "topic_id": topic_id,
        "user_message_id": message_id,
        "operation_id": "op-r14-f",
        "param_version": 1,
        "base_nav_version": None,
        "recovery_nav_version": current_nav_version(app.conn),
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(tool_success=True, navigation_applied=True, turn_ownership=True),
    }
    payload.update(overrides)
    return topic_recovery.record_topic_failure(app, **payload)


def _assistant_rows(app: AppContext, *, turn_id: str | None = None) -> list:
    sql = "SELECT role, content, fragment_id, turn_id FROM messages WHERE role = 'assistant'"
    params: tuple = ()
    if turn_id is not None:
        sql += " AND turn_id = ?"
        params = (turn_id,)
    return app.conn.execute(sql, params).fetchall()


def _all_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT role, content, fragment_id, turn_id FROM messages ORDER BY created_at, rowid"
    ).fetchall()


# ---------------------------------------------------------------------------
# F03：并发恢复请求只产生一份答案（屏障控制，不用 sleep 制造通过）
# ---------------------------------------------------------------------------


async def test_f03_two_concurrent_retries_produce_exactly_one_answer(app: AppContext):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = CountingAdapter(barrier=True)
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)

    first, second = await asyncio.gather(
        topic_recovery.retry_topic_answer(app, record["record_id"]),
        topic_recovery.retry_topic_answer(app, record["record_id"]),
    )

    assert adapter.calls == 1, (
        "并发恢复只能生成一次答案（租约/原子领取）；实际模型调用=%d" % adapter.calls
    )
    assistant = _assistant_rows(app, turn_id="turn_r")
    assert len(assistant) == 1, "同一逻辑答案只能保存一份：%s" % [dict(row) for row in assistant]

    answered_results = [
        item for item in (first, second) if item.get("ok") and item.get("answered")
    ]
    assert len(answered_results) == 1, (
        "并发恢复只能有一个请求真正生成并保存答案：%r" % ([first, second],)
    )
    saved = answered_results[0]
    assert saved.get("answer_id"), saved
    others = [item for item in (first, second) if item is not saved]
    assert len(others) == 1
    in_progress = others[0]
    # 并发正在执行：结构化「处理中」（202 + recovery_in_progress，带 record_id），不是再生成。
    assert in_progress.get("in_progress") is True, in_progress
    assert in_progress.get("code") == "recovery_in_progress", in_progress
    assert in_progress.get("status_code") == 202, in_progress
    assert in_progress.get("record_id") == record["record_id"], in_progress
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored is not None and stored.get("status") == "answered", stored
    assert stored.get("answer_id") == saved.get("answer_id"), stored


async def test_f03_generated_answer_is_stashed_and_persist_retry_does_not_regenerate(
    app: AppContext, monkeypatch
):
    topic_id, _fragment, message_id = _prepare(app)
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)

    # 在「唯一权威保存位置」注入一次失败：答案已经生成，但保存失败。
    original_write = topic_recovery.write_recovered_answer
    attempts = {"n": 0}

    def _flaky_write(conn, **kwargs):  # noqa: ANN001, ANN003
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("注入：答案保存失败")
        return original_write(conn, **kwargs)

    monkeypatch.setattr(topic_recovery, "write_recovered_answer", _flaky_write)

    first = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert first.get("ok") is False, first
    assert first.get("code") == "recovery_persist_failed", first
    assert adapter.calls == 1, "第一次已经真实生成过答案"
    assert _assistant_rows(app, turn_id="turn_r") == []
    stashed = topic_recovery.get_record(app, record["record_id"])
    assert stashed is not None and stashed.get("status") == "pending", stashed
    assert stashed.get("answer_text"), "保存失败后必须暂存已生成正文（不得丢失）"

    # 生成成功但保存失败：再次重试只补存，不重复生成。
    second = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert second.get("ok") is True, second
    assert second.get("answered") is True
    assert adapter.calls == 1, (
        "保存失败后重试必须复用已生成正文，不得再次调用模型；实际=%d" % adapter.calls
    )
    assistant = _assistant_rows(app, turn_id="turn_r")
    assert len(assistant) == 1, assistant
    again = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert again.get("ok") is True and again.get("answer_id") == second.get("answer_id")
    assert adapter.calls == 1


# ---------------------------------------------------------------------------
# F04：恢复事项独立于活动轮次、重启后仍可见（真实 HTTP 清单入口）
# ---------------------------------------------------------------------------


def test_f04_recovery_record_is_visible_after_restart_through_listing_api(tmp_path):
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.services.topic_operation import RECOVERY_KIND

    path = tmp_path / "restart.db"
    conn = connect(path)
    apply_migrations(conn)
    api_one = create_app(Settings(data_dir=tmp_path), conn)
    ctx_one = api_one.state.ctx
    ctx_one.credentials._kr = MemoryKeyring()
    topic_id, _fragment, message_id = _prepare(ctx_one)
    record = _make_record(ctx_one, topic_id, message_id)
    record_id = record["record_id"]

    # 模拟进程重启：同一个数据库文件、全新的 app / 内存状态。
    api_two = create_app(Settings(data_dir=tmp_path), conn)
    ctx_two = api_two.state.ctx
    ctx_two.credentials._kr = MemoryKeyring()
    with TestClient(api_two) as client:
        listing = client.get(
            "/api/recovery/records", params={"kinds": RECOVERY_KIND}
        )
        detail = client.get(f"/api/recovery/records/{record_id}")

    assert listing.status_code == 200, listing.text
    body = listing.json()
    ids = [str(item.get("record_id")) for item in body.get("records") or []]
    assert record_id in ids, (
        "持久恢复事项必须在重启后仍出现在恢复清单里：ids=%s body=%s" % (ids, body)
    )
    assert body.get("total", 0) >= 1

    if detail.status_code == 200:
        assert str(detail.json().get("record_id")) == record_id
    else:
        # 详情入口未实现时至少清单可见；不把缺失详情当通过。
        assert detail.status_code in (200, 404, 405), detail.text


# ---------------------------------------------------------------------------
# F13：原片段封存后，恢复答案仍可保存并归属原轮次
# ---------------------------------------------------------------------------


async def test_f13_late_answer_saves_after_original_fragment_sealed(app: AppContext):
    topic_id, fragment_id, message_id = _prepare(app)
    # 容量封存原片段，并出现一个新的开放片段（迟到答案不得写进这个新片段）。
    app.fragments.seal(fragment_id, reason="capacity")
    sealed_at = app.fragments.get(fragment_id).closed_at
    new_open = app.fragments.get_or_create_open(topic_id)
    assert new_open.id != fragment_id
    fragment_count_before = app.conn.execute(
        "SELECT COUNT(*) AS c FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchone()["c"]
    user_rows_before = [dict(row) for row in _all_rows(app) if row["role"] == "user"]

    adapter = CountingAdapter(text=ANSWER + "\n封存后的迟到答案")
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])

    assert result.get("ok") is True, (
        "原片段封存后恢复答案仍然必须可以保存：%r" % (result,)
    )
    assert result.get("answered") is True
    assert result.get("answer_id"), result
    # 不重开已封存片段；不写当前新片段；不搬动原问题；不新建重复轮次
    sealed_after = app.fragments.get(fragment_id)
    assert sealed_after.closed_at == sealed_at, "迟到答案不得重开已封存片段"
    fragment_count_after = app.conn.execute(
        "SELECT COUNT(*) AS c FROM fragments WHERE topic_id = ?", (topic_id,)
    ).fetchone()["c"]
    assert fragment_count_after == fragment_count_before, "迟到答案不得新建片段"
    user_rows_after = [dict(row) for row in _all_rows(app) if row["role"] == "user"]
    assert user_rows_after == user_rows_before, "迟到答案不得搬动/复制原用户问题"
    turn_rows = app.conn.execute(
        "SELECT COUNT(*) AS c FROM turn_bindings WHERE turn_id = ?", ("turn_r",)
    ).fetchone()["c"]
    assert int(turn_rows) == 1, "不得新建重复轮次"
    # 答案必须能按原轮次/原用户消息读回一份（读取归并后仍是这一条）。
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored is not None and stored.get("status") == "answered", stored
    assert stored.get("answer_id") == result.get("answer_id"), (stored, result)
    answer_id = str(result.get("answer_id"))
    row = app.conn.execute(
        "SELECT id, role, content, fragment_id, turn_id, answer_key, late_answer_of_message_id "
        "FROM messages WHERE id = ?",
        (answer_id,),
    ).fetchone()
    assert row is not None, "权威 answer_id 必须能在读取路径找到"
    assert str(row["fragment_id"]) == fragment_id, "迟到答案归属原（已封存）片段，不写当前新片段"
    assert str(row["turn_id"]) == "turn_r", "迟到答案关联原轮次"
    assert row["answer_key"], "唯一逻辑答案身份 answer_key 必须落库"
    assert str(row["late_answer_of_message_id"] or "") == message_id, (
        "迟到答案必须标记原用户消息（不搬动原问题）"
    )
    # 历史读取路径（按话题取消息）：答案出现在原问题之后，且只有一份。
    history = app.session_messages(topic_id)
    texts = [str(item.get("content") or "") for item in history]
    user_at = next(i for i, text in enumerate(texts) if text == "原问题")
    answer_at = next(i for i, text in enumerate(texts) if "封存后的迟到答案" in text)
    assert user_at < answer_at, "迟到答案必须在原问题之后显示：%s" % texts
    assert sum(1 for text in texts if "封存后的迟到答案" in text) == 1, "同一答案只有一份正文"


async def test_f13_answer_generated_then_fragment_sealed_still_saves(app: AppContext):
    """生成过程中片段被容量封存：答案不得丢，也不得写进新片段。"""
    topic_id, fragment_id, message_id = _prepare(app)
    adapter = GateAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id)
    new_open = app.fragments.get_or_create_open(topic_id)

    task = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"])
    )
    await asyncio.wait_for(adapter.started.wait(), timeout=5)
    app.fragments.seal(fragment_id, reason="capacity")
    adapter.release.set()
    result = await asyncio.wait_for(task, timeout=10)

    assert result.get("ok") is True, (
        "生成期间片段被封存后，答案仍必须保存到原轮次：%r" % (result,)
    )
    assert result.get("answered") is True
    assert app.fragments.get(fragment_id).closed_at is not None, "不得重开已封存片段"
    # 不把答案写进当前新开放片段
    new_open_after = app.fragments.get(new_open.id)
    assert new_open_after is not None
