# -*- coding: utf-8 -*-
"""R17-D：普通正式回答接入统一答案身份（P2-4 / R17 契约 §2.5 / §5 D1—D4）。

目标（契约 §2.5 冻结接口）：
* 普通正式回答与恢复回答共用 :func:`answer_key_for` 身份；
* 正式保存唯一性：同一 answer_key 已存在 -> 复用原 answer_id、不新增第二条；
* 保存成功时**同一事务**收口同 answer_key 的 pending 恢复记录与执行行（I12）；
* 保存失败时先把正文写进共享暂存唯一权威（topic_answer_runs.staged_*，phase=staged），
  再抛出**原异常**；任意同 answer_key 的恢复记录之后只补存、不再调用模型（I10）；
* 旧库 answer_key 为空的正式答案：只有**可靠归属**（同一 turn、同一原用户消息、该轮恰好
  一条正式 assistant 候选、同片段/同话题）才关联并同事务补写 answer_key；歧义当作不存在、
  不猜、不改旧行（D4）；
* 既有恢复路径（_execute_claim 的「已有答案」与 _save_answer 的既有答案查找）也使用同一
  可靠查找，共享暂存/写入权/superseded 语义不变（D5 回归）。

测试缝：真实 AppContext + 真实临时 SQLite（apply_migrations）+ fake 模型（调用计数器，
绝不联网）；不按真实时间等待。D1 走直接调用 save_normal_answer 证明语义（完整 HTTP 轮次的
交叉验收由 Lead 的集成测试覆盖 A 的 persist 改动）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import TopicStages
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

CONTENT = "普通正式回答正文"
TURN_ID = "turn_r17_d"
RECOVERY_TABLE = "topic_recovery_records"
RUN_TABLE = "topic_answer_runs"


# ── fake 模型 / 编排器（绝不联网；只计数）────────────────────────────


class CountingAdapter:
    mode = "native"
    model = "fake-r17-d"
    protocol_in_prompt = False

    def __init__(self, *, text: str = "[[QIO:ANSWER]]\n恢复后的回答") -> None:
        self.text = text
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class GatedAdapter(CountingAdapter):
    """可挂在事件闸门上的 fake 模型：用于制造「另一个活跃执行者」的交错（不用 sleep）。"""

    def __init__(self, *, text: str = "[[QIO:ANSWER]]\n恢复后的回答") -> None:
        super().__init__(text=text)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self, *, messages=None) -> None:
        self.messages = messages
        self.calls: list = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append(
            {"turn_id": turn_id, "topic_id": topic_id, "user_message_id": user_message_id}
        )
        if self.messages is not None:
            return list(self.messages)
        return [
            ChatMessage(role="system", content="SYS_FOR:" + str(topic_id)),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire(app: AppContext, orchestrator=None, adapter=None) -> None:
    if orchestrator is not None:
        app.turn_orchestrator = orchestrator  # type: ignore[assignment]
    if adapter is not None:
        async def _build_adapter():
            return adapter

        app.build_adapter = _build_adapter  # type: ignore[method-assign]


# ── 数据准备 / 读取 ──────────────────────────────────────────────────


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r17_d_answer_identity.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _prepare(app: AppContext, *, turn_id: str = TURN_ID, topic_name: str = "普通回答话题"):
    topic_id = app.topics.nodes.create_topic(topic_name).id
    fragment = app.fragments.get_or_create_open(topic_id)
    app.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="原问题",
        content_type="text",
        model="fake-user",
        turn_id=turn_id,
    )
    return topic_id, fragment.id, message_id


def _make_record(
    app: AppContext,
    topic_id: str,
    message_id: str,
    *,
    turn_id: str = TURN_ID,
    record_id: str = "trec_r17_d",
    **overrides,
):
    payload = {
        "turn_id": turn_id,
        "topic_id": topic_id,
        "user_message_id": message_id,
        "operation_id": "op-r17-d",
        "param_version": 1,
        "base_nav_version": None,
        "recovery_nav_version": current_nav_version(app.conn),
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    return topic_recovery.record_topic_failure(app, **payload, record_id=record_id)


def _append_legacy_assistant(
    app: AppContext,
    topic_id: str,
    *,
    turn_id: str = TURN_ID,
    content: str = "旧库里的正式回答",
    content_type: str = "text",
    model: str = "legacy-model",
    raw: dict | None = None,
    fragment_id: str | None = None,
) -> str:
    """模拟迁移 79 之前的旧库：正式 assistant 行存在，但 messages.answer_key 为空。"""
    message_id, _ = app.memory.append_message(
        topic_id=topic_id,
        role="assistant",
        content=content,
        content_type=content_type,
        model=model,
        raw=raw if raw is not None else {"legacy": True},
        fragment_id=fragment_id,
        turn_id=turn_id,
    )
    return message_id


def _message_row(app: AppContext, message_id: str) -> dict:
    row = app.conn.execute(
        "SELECT * FROM messages WHERE id = ?", (str(message_id),)
    ).fetchone()
    return dict(row) if row is not None else {}


def _assistant_rows(app: AppContext) -> list[dict]:
    return [
        dict(row)
        for row in app.conn.execute(
            "SELECT id, content, answer_key, fragment_id, turn_id, content_type, "
            "model, raw, late_answer_of_message_id FROM messages "
            "WHERE role = 'assistant' ORDER BY created_at, id"
        )
    ]


def _record_row(app: AppContext, record_id: str) -> dict:
    row = app.conn.execute(
        f"SELECT * FROM {RECOVERY_TABLE} WHERE record_id = ?", (str(record_id),)
    ).fetchone()
    return dict(row) if row is not None else {}


def _run_row(app: AppContext, answer_key: str) -> dict:
    row = app.conn.execute(
        f"SELECT * FROM {RUN_TABLE} WHERE answer_key = ?", (str(answer_key),)
    ).fetchone()
    return dict(row) if row is not None else {}


# ── D1：普通回答成功 — 统一 answer_key + 字段/归属保持 + 唯一性 ────────


def test_d1_normal_answer_uses_unified_answer_key(app):
    topic_id, fragment_id, user_message_id = _prepare(app)
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)
    assert expected_key

    result = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        content_type="text",
        model="fake-r17-d",
        raw={"verification": {"checked": True}, "note": "保留原语义"},
    )

    assert set(result) >= {
        "answer_id",
        "created",
        "existing",
        "closed_record_ids",
        "answer_key",
    }, result
    assert result["answer_key"] == expected_key
    assert result["created"] is True and result["existing"] is False
    assert result["closed_record_ids"] == []

    row = _message_row(app, result["answer_id"])
    assert row, "必须真的写入一条正式 answer 消息"
    assert row["role"] == "assistant"
    assert row["content"] == CONTENT
    assert row["content_type"] == "text"
    assert row["model"] == "fake-r17-d"
    assert json.loads(row["raw"]) == {
        "verification": {"checked": True},
        "note": "保留原语义",
    }, "raw/验证元数据必须保持普通回答原有语义"
    assert row["turn_id"] == TURN_ID, "必须保留原轮次归属"
    assert row["fragment_id"] == fragment_id, "片段必须按轮次绑定解析"
    assert row["answer_key"] == expected_key, "正式回答必须带统一 answer_key"
    assert row["late_answer_of_message_id"] is None


def test_d1b_duplicate_normal_save_reuses_single_formal_answer(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    first = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        model="fake-r17-d",
    )
    second = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content="第二次生成的另一段正文",
        model="fake-r17-d",
    )

    assert second["answer_id"] == first["answer_id"]
    assert second["created"] is False and second["existing"] is True
    rows = _assistant_rows(app)
    assert len(rows) == 1, "同一逻辑答案只允许一条正式消息：%s" % rows
    assert rows[0]["id"] == first["answer_id"]
    assert rows[0]["content"] == CONTENT, "重复保存不得改写既有正式答案"


def test_d1c_missing_user_message_uses_degraded_key_and_logs(app, caplog):
    topic_id, fragment_id, _user_message_id = _prepare(app)
    caplog.set_level(logging.WARNING, logger="agent.services.topic_recovery")

    result = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=None,
        content=CONTENT,
        fragment_id=fragment_id,
    )

    degraded_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=None)
    assert result["answer_key"] == degraded_key, "缺原用户消息按既有退化规则，不静默造假"
    row = _message_row(app, result["answer_id"])
    assert row["answer_key"] == degraded_key
    assert row["turn_id"] == TURN_ID
    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name == "agent.services.topic_recovery"
    ]
    assert any("缺少原用户消息" in record.getMessage() for record in warnings), (
        "退化路径必须留下可观察记录（不能静默）"
    )


# ── D2：普通答案已存在时恢复只收口、不调模型 ─────────────────────────


def test_d2a_normal_save_closes_same_answer_key_pending_records(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    record = _make_record(
        app, topic_id, user_message_id, record_id="trec_d2_before"
    )
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)
    assert record["answer_key"] == expected_key

    result = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        model="fake-r17-d",
    )

    assert result["closed_record_ids"] == [record["record_id"]], (
        "普通回答成功必须在同一事务收口同 answer_key 的 pending 恢复记录（I12）"
    )
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == result["answer_id"]
    run = _run_row(app, expected_key)
    assert run.get("phase") == "answered" and run.get("status") == "answered", run
    assert run.get("answer_id") == result["answer_id"]


async def test_d2b_retry_after_normal_answer_finalizes_without_model(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    saved = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        model="fake-r17-d",
    )
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, user_message_id, record_id="trec_d2_after")

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])

    assert result.get("ok") is True and result.get("answered") is True, result
    assert result.get("answer_id") == saved["answer_id"]
    assert adapter.calls == 0, "普通答案已存在时恢复不得再调用模型"
    assert orchestrator.calls == [], "普通答案已存在时恢复不得重新装配上下文"
    assert len(_assistant_rows(app)) == 1
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == saved["answer_id"]


# ── D3：保存失败 -> 共享暂存 -> 另一条记录只补存 ────────────────────


async def test_d3_failed_normal_save_stashes_then_other_record_only_persists(app, monkeypatch):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)
    first = _make_record(app, topic_id, user_message_id, record_id="trec_d3_first")

    real_insert = topic_recovery._insert_normal_answer_message
    attempts = {"n": 0}

    def _flaky_insert(conn, **kwargs):  # noqa: ANN001, ANN003
        attempts["n"] += 1
        raise RuntimeError("注入：普通回答保存失败")

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _flaky_insert)

    with pytest.raises(RuntimeError, match="注入：普通回答保存失败"):
        topic_recovery.save_normal_answer(
            app,
            topic_id=topic_id,
            turn_id=TURN_ID,
            user_message_id=user_message_id,
            content=CONTENT,
            model="fake-r17-d",
        )
    assert attempts["n"] == 1

    # 保存失败必须先把正文写进共享暂存唯一权威（run 行），再抛出原异常。
    assert _assistant_rows(app) == [], "保存失败不得留下半截正式答案"
    run = _run_row(app, expected_key)
    assert run, "保存失败必须建立共享暂存（topic_answer_runs 行）"
    assert run.get("phase") == "staged", run
    assert run.get("status") == "pending", run
    assert run.get("staged_answer") == CONTENT, run
    assert run.get("staged_at"), "暂存必须记录时间"
    assert run.get("staged_model") == "fake-r17-d"
    assert _record_row(app, first["record_id"])["status"] == "pending"

    # 之后另一条同 answer_key 的恢复记录：命中共享暂存 -> 只补存、不调模型。
    second = _make_record(app, topic_id, user_message_id, record_id="trec_d3_second")
    assert second["answer_key"] == expected_key
    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", real_insert)
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)

    done = await topic_recovery.retry_topic_answer(app, second["record_id"])

    assert done.get("ok") is True and done.get("answered") is True, done
    assert adapter.calls == 0, "共享暂存命中时不得再调用模型"
    assert orchestrator.calls == [], "共享暂存命中时不得重新装配上下文"
    rows = _assistant_rows(app)
    assert len(rows) == 1, "同一逻辑答案只能补存一条正式消息"
    assert rows[0]["content"] == CONTENT
    assert rows[0]["answer_key"] == expected_key
    for record_id in (first["record_id"], second["record_id"]):
        stored = topic_recovery.get_record(app, record_id)
        assert stored["status"] == "answered", stored
        assert stored["answer_id"] == done["answer_id"], stored
    run = _run_row(app, expected_key)
    assert run.get("phase") == "answered" and run.get("status") == "answered", run
    assert run.get("staged_answer") == "" and run.get("staged_at") is None, (
        "补存成功必须同事务清空共享暂存"
    )


def test_d3b_stash_failure_does_not_mask_original_exception(app, monkeypatch):
    topic_id, _fragment_id, user_message_id = _prepare(app)

    def _boom_insert(conn, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("原始保存异常")

    def _boom_stash(*args, **kwargs):  # noqa: ANN001, ANN003
        raise ValueError("暂存也失败：不得掩盖原异常")

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _boom_insert)
    monkeypatch.setattr(topic_recovery, "_stash_normal_answer_failure", _boom_stash)

    with pytest.raises(RuntimeError, match="原始保存异常"):
        topic_recovery.save_normal_answer(
            app,
            topic_id=topic_id,
            turn_id=TURN_ID,
            user_message_id=user_message_id,
            content=CONTENT,
            model="fake-r17-d",
        )


# ── D4：旧库 answer_key 为空 — 可靠归属才关联，歧义不猜 ─────────────


def test_d4a_reliable_legacy_answer_is_reused_and_key_backfilled(app):
    topic_id, fragment_id, user_message_id = _prepare(app)
    legacy_id = _append_legacy_assistant(
        app,
        topic_id,
        content="旧库里的正式回答",
        content_type="text",
        model="legacy-model",
        raw={"legacy": True, "answer": 1},
    )
    before = _message_row(app, legacy_id)
    assert before["answer_key"] is None
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)

    result = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content="新生成的回答（不应写入）",
        model="fake-r17-d",
    )

    assert result["answer_id"] == legacy_id, "可靠旧答案必须被复用（不新增第二条）"
    assert result["created"] is False and result["existing"] is True
    rows = _assistant_rows(app)
    assert len(rows) == 1, "旧答案已可靠关联时不得再写第二条正式答案"
    after = _message_row(app, legacy_id)
    assert after["answer_key"] == expected_key, "可靠命中必须在同一事务补写 answer_key"
    # 旧行其余内容/元数据/归属一律不改（只补 answer_key）。
    assert (after["content"], after["content_type"], after["model"]) == (
        before["content"],
        before["content_type"],
        before["model"],
    )
    assert after["raw"] == before["raw"]
    assert (after["fragment_id"], after["turn_id"]) == (
        before["fragment_id"],
        before["turn_id"],
    )
    assert after["fragment_id"] == fragment_id


def test_d4b_ambiguous_legacy_candidates_are_not_associated(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    first_id = _append_legacy_assistant(app, topic_id, content="旧回答 A")
    second_id = _append_legacy_assistant(app, topic_id, content="旧回答 B")
    before = {rid: _message_row(app, rid) for rid in (first_id, second_id)}

    result = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        model="fake-r17-d",
    )

    assert result["created"] is True, "歧义时不得关联旧答案，必须写新的正式答案"
    rows = _assistant_rows(app)
    assert len(rows) == 3
    keyed = [row for row in rows if row["answer_key"]]
    assert [row["id"] for row in keyed] == [result["answer_id"]]
    # 歧义 -> 两条旧行一个字段都不改（不猜、不篡改）。
    for rid in (first_id, second_id):
        assert _message_row(app, rid) == before[rid], "歧义时不得改写旧答案行"


def test_d4c_mismatched_turn_legacy_answer_is_not_associated(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    other_id = _append_legacy_assistant(
        app, topic_id, content="别的轮次的回答", turn_id="turn_other"
    )
    before = _message_row(app, other_id)

    result = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        model="fake-r17-d",
    )

    assert result["created"] is True
    assert _message_row(app, other_id) == before, "不同轮次的旧答案不得被关联/篡改"


async def test_d4d_recovery_path_uses_reliable_legacy_answer(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    legacy_id = _append_legacy_assistant(app, topic_id, content="旧库里的正式回答")
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)
    record = _make_record(app, topic_id, user_message_id, record_id="trec_d4d")
    assert record["answer_key"] == expected_key

    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])

    assert result.get("ok") is True and result.get("answered") is True, result
    assert result.get("answer_id") == legacy_id
    assert adapter.calls == 0, "既有恢复路径必须使用可靠旧答案查找，不得重复调用模型"
    assert orchestrator.calls == []
    assert len(_assistant_rows(app)) == 1
    after = _message_row(app, legacy_id)
    assert after["answer_key"] == expected_key, "恢复路径可靠命中也要补写 answer_key"
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered" and stored["answer_id"] == legacy_id


# ── 共享写入权边界：普通保存赢 -> 活跃执行者迟到写 superseded（I12/I11）──


async def test_d2c_normal_save_wins_over_active_executor(app):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)
    record = _make_record(app, topic_id, user_message_id, record_id="trec_d2c")
    adapter = GatedAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)

    task = asyncio.create_task(topic_recovery.retry_topic_answer(app, record["record_id"]))
    await asyncio.wait_for(adapter.started.wait(), timeout=10)
    run = _run_row(app, expected_key)
    assert run.get("status") == "running" and run.get("owner"), run

    saved = topic_recovery.save_normal_answer(
        app,
        topic_id=topic_id,
        turn_id=TURN_ID,
        user_message_id=user_message_id,
        content=CONTENT,
        model="fake-r17-d",
    )

    # 普通保存的同一事务已收口执行行与记录（I12）：只剩一份正式答案。
    run = _run_row(app, expected_key)
    assert run.get("status") == "answered" and run.get("phase") == "answered", run
    assert run.get("answer_id") == saved["answer_id"]
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered" and stored["answer_id"] == saved["answer_id"]

    adapter.release.set()
    late = await asyncio.wait_for(task, timeout=10)
    assert late.get("superseded") is True, late
    assert late.get("code") == "recovery_execution_superseded", late
    rows = _assistant_rows(app)
    assert len(rows) == 1, "旧执行者迟到写不得再生成第二份正文"
    assert rows[0]["id"] == saved["answer_id"]


async def test_d3c_save_failure_does_not_overwrite_active_executor_stash(app, monkeypatch):
    topic_id, _fragment_id, user_message_id = _prepare(app)
    expected_key = topic_recovery.answer_key_for(TURN_ID, user_message_id=user_message_id)
    record = _make_record(app, topic_id, user_message_id, record_id="trec_d3c")
    adapter = GatedAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)

    task = asyncio.create_task(topic_recovery.retry_topic_answer(app, record["record_id"]))
    await asyncio.wait_for(adapter.started.wait(), timeout=10)
    before = _run_row(app, expected_key)
    assert before.get("status") == "running" and before.get("owner"), before

    def _boom_insert(conn, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("注入：普通回答保存失败（活跃执行者场景）")

    monkeypatch.setattr(topic_recovery, "_insert_normal_answer_message", _boom_insert)
    with pytest.raises(RuntimeError, match="活跃执行者场景"):
        topic_recovery.save_normal_answer(
            app,
            topic_id=topic_id,
            turn_id=TURN_ID,
            user_message_id=user_message_id,
            content=CONTENT,
            model="fake-r17-d",
        )

    # 失败暂存不得覆盖另一个活跃执行者的暂存 / 租约（I11），原异常照抛。
    after = _run_row(app, expected_key)
    assert after.get("staged_answer") == "", after
    assert after.get("status") == "running", after
    assert after.get("phase") == "generating", after
    assert after.get("owner") == before.get("owner")
    assert after.get("generation") == before.get("generation")
    assert _assistant_rows(app) == []
    assert _record_row(app, record["record_id"])["status"] == "pending"

    # 活跃执行者继续按自己的执行权完成：唯一一份正式答案。
    adapter.release.set()
    done = await asyncio.wait_for(task, timeout=10)
    assert done.get("ok") is True and done.get("answered") is True, done
    rows = _assistant_rows(app)
    assert len(rows) == 1
    assert rows[0]["id"] == done["answer_id"]
    run = _run_row(app, expected_key)
    assert run.get("phase") == "answered" and run.get("status") == "answered", run



