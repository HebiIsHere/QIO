# -*- coding: utf-8 -*-
"""R16-D：逻辑答案执行权、共享暂存结果与正式保存（契约 §6 / §14.6）。

本文件先红后绿，覆盖：
* A 暂存成功但保存失败 → B（另一条同 answer_key 记录）只补存、不再生成（模型调用=1）；
* 暂存正文唯一权威 = topic_answer_runs.staged_answer/staged_at/staged_model（按 answer_key 共享）；
* phase 状态机 waiting → preparing → generating → staged → answered / failed，且与 status 同步；
* 保存事务原子性：唯一答案行 + 所有同 answer_key pending 记录 answered + run phase=answered
  且清 staged 同一事务；失败整体回滚；
* 并发/续租/接管/旧执行者迟到写（暂存、保存、收口、失败更新、释放、阶段推进）全部拒绝；
* 旧 record.answer_text 只读兜底：读到即同事务转写 run 行并清 record；
* 恢复入口不导航、不改 pending continuation、不推进 nav_version（I13）；
* 恢复记录读取/收件箱不把 staged 正文当正式正文；
* I14 冻结码：answer_attribution_mismatch / answer_context_missing（recoverable、503、零正式答案行）。

测试缝：fake 模型 + 真实临时 SQLite + 受控时钟（monkeypatch 模块级 _now_utc）+
heartbeat_wait 事件闸门；不用 sleep 造时序。

迁移 83（A 落地）未合并前，fixture 用 PRAGMA 幂等补列（与契约 §6/§14.6 冻结 DDL 一致）；
列已存在时无操作，最终以真实迁移为准。

诚实边界：外部模型调用**不**严格 exactly-once（崩溃在模型返回与结果持久化之间仍可能再次调用）；
这里只证明同一逻辑答案唯一持久化 + 执行权收口 + 共享暂存不重复生成。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import topic_recovery
from agent.services.app import AppContext
from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import TopicStages
from agent.storage.db import connect, transaction
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"
RECOVERY_TABLE = "topic_recovery_records"
RUN_TABLE = "topic_answer_runs"
T0 = datetime(2026, 11, 11, 0, 0, 0, tzinfo=timezone.utc)
LEASE = 600.0

#: 迁移 83（A 建）里 topic_answer_runs 的追加列；与契约 §6/§14.6 冻结 DDL 一致。
_R16_RUN_COLUMN_DDL = (
    ("phase", "ALTER TABLE topic_answer_runs ADD COLUMN phase TEXT NOT NULL DEFAULT 'waiting'"),
    (
        "staged_answer",
        "ALTER TABLE topic_answer_runs ADD COLUMN staged_answer TEXT NOT NULL DEFAULT ''",
    ),
    ("staged_at", "ALTER TABLE topic_answer_runs ADD COLUMN staged_at TEXT"),
    ("staged_model", "ALTER TABLE topic_answer_runs ADD COLUMN staged_model TEXT"),
)


def _ensure_r16_run_columns(conn: sqlite3.Connection) -> None:
    """迁移 83 未合并时的幂等补列（只给测试库；列已存在时无操作）。"""
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({RUN_TABLE})")}
    for name, ddl in _R16_RUN_COLUMN_DDL:
        if name not in existing:
            conn.execute(ddl)


# ── 受控时钟 / 事件闸门（不用 sleep）──────────────────────────────────


class FakeClock:
    def __init__(self, moment: datetime = T0) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> datetime:
        self.moment = self.moment + timedelta(seconds=float(seconds))
        return self.moment


class HeartbeatControl:
    """heartbeat_wait 测试缝：每次续租前等一个事件，测试按需放行。"""

    def __init__(self) -> None:
        self.waits = 0
        self._gates: list[asyncio.Event] = []

    async def wait(self) -> None:
        gate = asyncio.Event()
        self._gates.append(gate)
        self.waits = len(self._gates)
        await gate.wait()

    def release(self, index: int = 0) -> None:
        self._gates[index].set()


async def _drive_until(predicate, *, attempts: int = 2000) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0)
    return False


# ── fake 模型 / 编排器 ───────────────────────────────────────────────


class CountingAdapter:
    """可计数、可挂闸门、可注入指定次数异常的 fake 模型（绝不联网）。"""

    mode = "native"
    model = "fake-r16-d"
    protocol_in_prompt = False

    def __init__(
        self,
        *,
        text: str = ANSWER + "\n恢复后的回答",
        gates: int = 0,
        error_calls: dict[int, Exception] | None = None,
        phase_probe=None,
    ) -> None:
        self.text = text
        self.calls = 0
        self.started = [asyncio.Event() for _ in range(gates)]
        self.release = [asyncio.Event() for _ in range(gates)]
        self.error_calls = dict(error_calls or {})
        self.phase_probe = phase_probe
        self.phases: list = []

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        index = self.calls - 1
        if self.phase_probe is not None:
            self.phases.append(self.phase_probe())
        if index < len(self.started):
            self.started[index].set()
            await self.release[index].wait()
        error = self.error_calls.get(self.calls)
        if error is not None:
            raise error
        return Completion(message=ChatMessage(role="assistant", content=self.text))


class FakeOrchestrator:
    def __init__(self, *, messages=None, error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error
        self.calls: list = []
        self.phase_probe = None
        self.phases: list = []

    async def prepare_topic_answer_messages(self, *, turn_id, topic_id, user_message_id):
        self.calls.append(
            {"turn_id": turn_id, "topic_id": topic_id, "user_message_id": user_message_id}
        )
        if self.phase_probe is not None:
            self.phases.append(self.phase_probe())
        if self.error is not None:
            raise self.error
        if self.messages is not None:
            return list(self.messages)
        return [
            ChatMessage(role="system", content="SYS_FOR:" + str(topic_id)),
            ChatMessage(role="user", content="原问题"),
        ]


def _wire(app: AppContext, orchestrator, adapter) -> None:
    app.turn_orchestrator = orchestrator  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]


# ── 数据准备 / 读取 ──────────────────────────────────────────────────


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r16_d_answer_execution.db")
    apply_migrations(conn)
    # 迁移 83 未合并：幂等补列后按新权威语义测试（集成后为真实迁移）。
    _ensure_r16_run_columns(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


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
        "operation_id": "op-r16-d",
        "param_version": 1,
        "base_nav_version": None,
        "recovery_nav_version": current_nav_version(app.conn),
        "failed_stage": "context_prepare",
        "message": "话题已切换，回答准备失败",
        "stages": TopicStages(context_ready=True),
    }
    payload.update(overrides)
    return topic_recovery.record_topic_failure(app, **payload)


def _assistant_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT id, content, answer_key, fragment_id, turn_id "
        "FROM messages WHERE role = 'assistant'"
    ).fetchall()


def _user_rows(app: AppContext) -> list:
    return app.conn.execute(
        "SELECT id, content, fragment_id, turn_id FROM messages WHERE role = 'user'"
    ).fetchall()


def _record_row(app: AppContext, record_id: str):
    return app.conn.execute(
        f"SELECT * FROM {RECOVERY_TABLE} WHERE record_id = ?", (record_id,)
    ).fetchone()


def _run_row(app: AppContext, answer_key: str):
    return app.conn.execute(
        f"SELECT * FROM {RUN_TABLE} WHERE answer_key = ?", (answer_key,)
    ).fetchone()


def _run_dict(app: AppContext, answer_key: str) -> dict:
    row = _run_row(app, answer_key)
    return dict(row) if row is not None else {}


def _run_phase(app: AppContext, answer_key: str):
    row = _run_row(app, answer_key)
    return None if row is None else row["phase"]


def _raw_record_answer_text(app: AppContext, record_id: str):
    row = _record_row(app, record_id)
    return None if row is None else row["answer_text"]


def _fail_first_write(monkeypatch) -> dict:
    """在唯一权威保存位置注入一次失败：答案已生成但保存失败。"""
    original = topic_recovery.write_recovered_answer
    attempts = {"n": 0}

    def _flaky(conn, **kwargs):  # noqa: ANN001, ANN003
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("注入：答案保存失败")
        return original(conn, **kwargs)

    monkeypatch.setattr(topic_recovery, "write_recovered_answer", _flaky)
    return attempts


def _register_instance(app: AppContext, instance_id: str, *, exited_at: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    app.conn.execute(
        "INSERT OR REPLACE INTO instances "
        "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (instance_id, 999999, "test-host", now, now, exited_at),
    )


def _stub_clock(monkeypatch, clock: FakeClock) -> None:
    monkeypatch.setattr(topic_recovery, "_now_utc", clock.now)


# ── S1：共享暂存按 answer_key 共享；B 只补存不再生成 ─────────────────


async def test_s1_shared_staged_answer_second_record_only_persists(app, monkeypatch):
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    first = _make_record(app, topic_id, message_id, record_id="trec_a")
    second = _make_record(app, topic_id, message_id, record_id="trec_b")
    assert first["answer_key"] == second["answer_key"]
    _fail_first_write(monkeypatch)

    failed = await topic_recovery.retry_topic_answer(app, first["record_id"])
    assert failed.get("ok") is False, failed
    assert failed.get("code") == "recovery_persist_failed", failed
    assert adapter.calls == 1, "第一次已真实生成过答案"
    assert len(orchestrator.calls) == 1, "第一次生成前装配过一次上下文"

    run = _run_dict(app, first["answer_key"])
    assert run.get("phase") == "staged", run
    assert run.get("status") == "pending", run
    assert run.get("staged_answer") == "恢复后的回答", run
    assert run.get("staged_at"), "暂存必须记录时间"
    assert _raw_record_answer_text(app, first["record_id"]) in (None, ""), (
        "记录列 answer_text 不再是新暂存的权威（不得双写）"
    )
    assert _raw_record_answer_text(app, second["record_id"]) in (None, "")

    # B：另一条同 answer_key 的恢复记录 -> 取得共享暂存结果补存，不再生成。
    done = await topic_recovery.retry_topic_answer(app, second["record_id"])
    assert done.get("ok") is True and done.get("answered") is True, done
    assert adapter.calls == 1, "补存不得再次调用模型；实际=%d" % adapter.calls
    assert len(orchestrator.calls) == 1, "有共享暂存时不得重新装配上下文"
    rows = _assistant_rows(app)
    assert len(rows) == 1, "同一逻辑答案只能保存一份正文：%s" % [dict(r) for r in rows]
    assert rows[0]["content"] == "恢复后的回答"
    assert rows[0]["answer_key"] == first["answer_key"]

    run = _run_dict(app, first["answer_key"])
    assert run.get("phase") == "answered" and run.get("status") == "answered", run
    assert run.get("staged_answer") == "" and run.get("staged_at") is None, (
        "保存成功必须同事务清 staged"
    )
    for record_id in (first["record_id"], second["record_id"]):
        stored = topic_recovery.get_record(app, record_id)
        assert stored["status"] == "answered", stored
        assert stored["answer_id"] == done["answer_id"], stored


# ── S2：旧 record.answer_text 只读兜底 -> 同事务转写 run 并清 record ──


async def test_s2_legacy_record_answer_text_fallback_migrates_to_run(app, monkeypatch):
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_legacy")
    app.conn.execute(
        f"UPDATE {RECOVERY_TABLE} SET answer_text = ? WHERE record_id = ?",
        ("旧版本暂存正文", record["record_id"]),
    )

    captured: dict = {}
    real_save = topic_recovery._save_answer

    def _spy_save(conn, rec, answer, *, model, execution):  # noqa: ANN001
        run = conn.execute(
            f"SELECT phase, staged_answer, staged_at FROM {RUN_TABLE} WHERE answer_key = ?",
            (rec["answer_key"],),
        ).fetchone()
        raw = conn.execute(
            f"SELECT answer_text FROM {RECOVERY_TABLE} WHERE record_id = ?",
            (rec["record_id"],),
        ).fetchone()
        captured.update(
            answer=answer,
            phase=run["phase"],
            staged_answer=run["staged_answer"],
            staged_at=run["staged_at"],
            record_answer_text=raw["answer_text"],
        )
        return real_save(conn, rec, answer, model=model, execution=execution)

    monkeypatch.setattr(topic_recovery, "_save_answer", _spy_save)

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert result.get("ok") is True and result.get("answered") is True, result
    assert adapter.calls == 0, "旧暂存正文存在时不得调用模型"
    assert orchestrator.calls == [], "旧暂存正文存在时不得重新装配上下文"
    assert captured["answer"] == "旧版本暂存正文"
    assert captured["staged_answer"] == "旧版本暂存正文", (
        "读到旧 record.answer_text 必须先同事务转写 run 行"
    )
    assert captured["staged_at"], captured
    assert captured["record_answer_text"] in (None, ""), (
        "转写后必须清 record.answer_text（保证只有一份权威）"
    )
    rows = _assistant_rows(app)
    assert len(rows) == 1 and rows[0]["content"] == "旧版本暂存正文"
    run = _run_dict(app, record["answer_key"])
    assert run.get("phase") == "answered" and run.get("staged_answer") == "", run
    assert topic_recovery.get_record(app, record["record_id"])["status"] == "answered"


# ── S3：phase 状态机 + status 同步 ───────────────────────────────────


async def test_s3_phase_state_machine_and_status_sync(app, monkeypatch):
    topic_id, _fragment_id, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id, record_id="trec_phase")
    key = record["answer_key"]

    # waiting：执行权行首建（尚未领取）。
    with transaction(app.conn):
        row = topic_recovery._ensure_execution_row(app.conn, key)
    assert row is not None
    assert row["phase"] == "waiting", dict(row)
    assert row["status"] == "pending", dict(row)

    orchestrator = FakeOrchestrator()
    orchestrator.phase_probe = lambda: _run_phase(app, key)
    adapter = CountingAdapter(
        gates=0,
        error_calls={1: RuntimeError("注入：模型生成失败")},
        phase_probe=lambda: _run_phase(app, key),
    )
    _wire(app, orchestrator, adapter)

    # 第一次：preparing（装配）-> generating（模型）-> 失败 -> failed/pending。
    failed = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert failed.get("ok") is False and failed.get("code") == "topic_answer_failed", failed
    assert orchestrator.phases == ["preparing"], orchestrator.phases
    assert adapter.phases == ["generating"], adapter.phases
    run = _run_dict(app, key)
    assert run.get("phase") == "failed" and run.get("status") == "pending", run
    assert run.get("staged_answer") == "", run
    assert topic_recovery.get_record(app, record["record_id"])["status"] == "pending"

    # 第二次：preparing -> generating -> answered，phase 与 status 同步。
    adapter.error_calls.clear()
    orchestrator.phases.clear()
    adapter.phases.clear()
    done = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert done.get("ok") is True and done.get("answered") is True, done
    assert orchestrator.phases == ["preparing"], orchestrator.phases
    assert adapter.phases == ["generating"], adapter.phases
    run = _run_dict(app, key)
    assert run.get("phase") == "answered" and run.get("status") == "answered", run


# ── S4：保存事务原子性 + 所有同 answer_key pending 记录一起收口 ───────


async def test_s4_save_transaction_atomic_and_shared_across_records(app, monkeypatch):
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    records = [
        _make_record(app, topic_id, message_id, record_id="trec_%d" % index)
        for index in range(3)
    ]
    assert len({item["answer_key"] for item in records}) == 1

    real_finalize = topic_recovery._finalize_answer_locked
    state = {"fail": True}

    def _flaky_finalize(conn, execution, answer_id):  # noqa: ANN001
        if state["fail"]:
            raise RuntimeError("注入：收口失败（答案行与记录收口必须同一事务）")
        return real_finalize(conn, execution, answer_id)

    monkeypatch.setattr(topic_recovery, "_finalize_answer_locked", _flaky_finalize)

    first = await topic_recovery.retry_topic_answer(app, records[0]["record_id"])
    assert first.get("ok") is False and first.get("code") == "recovery_persist_failed", first
    assert adapter.calls == 1
    assert _assistant_rows(app) == [], "收口失败必须回滚已插入的答案行（同一事务）"
    for item in records:
        assert _record_row(app, item["record_id"])["status"] == "pending"
    run = _run_dict(app, records[0]["answer_key"])
    assert run.get("phase") == "staged" and run.get("status") == "pending", run
    assert run.get("staged_answer") == "恢复后的回答", run

    # 另一条记录只补存；同一事务把三条 pending 记录 + run 一起收口。
    state["fail"] = False
    done = await topic_recovery.retry_topic_answer(app, records[1]["record_id"])
    assert done.get("ok") is True and done.get("answered") is True, done
    assert adapter.calls == 1, "补存不得重复调用模型"
    rows = _assistant_rows(app)
    assert len(rows) == 1
    for item in records:
        stored = topic_recovery.get_record(app, item["record_id"])
        assert stored["status"] == "answered", stored
        assert stored["answer_id"] == done["answer_id"], stored
    run = _run_dict(app, records[0]["answer_key"])
    assert run.get("phase") == "answered" and run.get("status") == "answered"
    assert run.get("staged_answer") == "" and run.get("staged_at") is None
    assert run.get("answer_id") == done["answer_id"]


# ── S5：接管后旧执行者各类迟到写全部拒绝（零行改动）─────────────────


async def test_s5_late_writes_after_takeover_are_all_rejected(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    app.instance_id = "inst_old"
    _register_instance(app, "inst_old", exited_at=T0.isoformat())
    adapter = CountingAdapter(gates=2)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_late")
    key = record["answer_key"]

    task_old = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    old_run = _run_dict(app, key)
    stale_execution = {
        "answer_key": key,
        "generation": int(old_run["generation"]),
        "owner": str(old_run["owner"]),
        "owner_instance": str(old_run["owner_instance"]),
        "lease_seconds": LEASE,
        "record_id": record["record_id"],
    }

    # 旧实例确认退出 + 租约到期 -> 新实例接管（generation+1）。
    clock.advance(700)
    app.instance_id = "inst_new"
    task_new = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[1].wait(), timeout=10)
    run_after_takeover = _run_dict(app, key)
    record_after_takeover = dict(_record_row(app, record["record_id"]))
    assert int(run_after_takeover["generation"]) == 2
    assert run_after_takeover["phase"] == "generating", run_after_takeover

    # 1) 迟到暂存：不得写共享暂存 / 改阶段。
    assert (
        topic_recovery._stash_answer_failure(
            app.conn, stale_execution, "旧执行者迟到暂存正文", "late"
        )
        is False
    )
    # 2) 迟到保存：执行权校验失败 -> 整体回滚，零写入。
    stale_record = topic_recovery.get_record(app, record["record_id"])
    with pytest.raises(topic_recovery._ExecutionLost):
        topic_recovery._save_answer(
            app.conn, stale_record, "旧执行者迟到正文", model="fake", execution=stale_execution
        )
    # 3) 迟到收口：事务内 CAS 失败 -> 回滚。
    with pytest.raises(topic_recovery._ExecutionLost):
        with transaction(app.conn):
            topic_recovery._finalize_answer_locked(app.conn, stale_execution, "msg_stale")
    # 4) 迟到失败更新 / 释放 / 阶段推进：全部拒绝。
    assert (
        topic_recovery._update_record_under_execution(
            app.conn, stale_execution, record["record_id"], last_error="late"
        )
        is False
    )
    assert topic_recovery._release_execution(app.conn, stale_execution) is False
    assert topic_recovery._set_active_phase(app.conn, stale_execution, "generating") is False

    assert _run_dict(app, key) == run_after_takeover, "旧执行者迟到写不得改新执行权状态"
    assert dict(_record_row(app, record["record_id"])) == record_after_takeover, (
        "旧执行者迟到写不得改新任务记录"
    )
    assert _assistant_rows(app) == []

    # 旧执行者迟到成功：superseded，零写入；新执行者收口唯一答案。
    adapter.release[0].set()
    done_old = await asyncio.wait_for(task_old, timeout=10)
    assert done_old.get("ok") is False and done_old.get("superseded") is True, done_old
    assert done_old.get("code") == "recovery_execution_superseded", done_old
    assert _assistant_rows(app) == []
    adapter.release[1].set()
    done_new = await asyncio.wait_for(task_new, timeout=10)
    assert done_new.get("ok") is True and done_new.get("answered") is True, done_new
    assert len(_assistant_rows(app)) == 1
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered" and stored["answer_id"] == done_new["answer_id"]


# ── S6：活任务心跳续租期间 phase=generating，且不重复生成 ────────────


async def test_s6_heartbeat_keeps_phase_generating_and_lease(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = CountingAdapter(gates=1)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_live")
    control = HeartbeatControl()

    task = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app,
            record["record_id"],
            lease_seconds=LEASE,
            heartbeat_interval=LEASE,
            heartbeat_wait=control.wait,
        )
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    run = _run_dict(app, record["answer_key"])
    assert run["phase"] == "generating" and run["status"] == "running", run

    assert await _drive_until(lambda: control.waits >= 1)
    clock.advance(500)
    control.release(0)
    extended = T0 + timedelta(seconds=1100)
    assert await _drive_until(
        lambda: topic_recovery._parse_ts(_run_row(app, record["answer_key"])["claim_until"])
        == extended
    ), "心跳必须续租活任务"
    assert _run_dict(app, record["answer_key"])["phase"] == "generating"

    clock.advance(200)  # 超过原租约，但仍在续租后的有效期内
    waiter = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert waiter.get("code") == "recovery_in_progress", waiter
    assert waiter.get("status_code") == 202
    assert adapter.calls == 1, "活任务不得被重复生成"
    assert _run_dict(app, record["answer_key"])["phase"] == "generating"

    adapter.release[0].set()
    done = await asyncio.wait_for(task, timeout=10)
    assert done.get("ok") is True and done.get("answered") is True, done
    run = _run_dict(app, record["answer_key"])
    assert run["phase"] == "answered" and run["status"] == "answered"


# ── S7：恢复入口不导航 / 不改 pending continuation / 不推进 nav_version ─


async def test_s7_recovery_does_not_navigate_or_touch_pending(app):
    topic_id, fragment_id, message_id = _prepare(app)
    record = _make_record(app, topic_id, message_id, record_id="trec_no_nav")

    # 建立真实位置：封存原片段、开新片段、Anchor 落在新开放片段、登记接续意图。
    app.fragments.seal(fragment_id, reason="capacity")
    open_fragment = app.fragments.get_or_create_open(topic_id)
    app.navigation.enter_topic(topic_id, fragment_id=open_fragment.id)
    app.bindings.register_intent(topic_id, fragment_id)
    # 记录基线用「失败时刻」的真实导航版本（恢复只读复核它）。
    app.conn.execute(
        f"UPDATE {RECOVERY_TABLE} SET recovery_nav_version = ? WHERE record_id = ?",
        (current_nav_version(app.conn), record["record_id"]),
    )

    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)

    anchor_before = app.navigation.anchors.get_active()
    nav_before = current_nav_version(app.conn)
    intent_before = app.bindings.peek_intent()
    user_rows_before = [dict(row) for row in _user_rows(app)]
    topics_before = app.conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type = 'topic'"
    ).fetchone()["c"]
    fragments_before = app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
    bindings_before = app.conn.execute("SELECT COUNT(*) c FROM turn_bindings").fetchone()["c"]

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert result.get("ok") is True and result.get("answered") is True, result

    anchor_after = app.navigation.anchors.get_active()
    assert (anchor_after.topic_id, anchor_after.fragment_id) == (
        anchor_before.topic_id,
        anchor_before.fragment_id,
    ), "恢复入口不得移动当前位置（I13）"
    assert current_nav_version(app.conn) == nav_before, "恢复入口不得推进 nav_version（I13）"
    intent_after = app.bindings.peek_intent()
    assert intent_after is not None
    assert (intent_after.intent_id, intent_after.state, intent_after.version) == (
        intent_before.intent_id,
        intent_before.state,
        intent_before.version,
    ), "恢复入口不得改 pending continuation（I13）"
    assert [dict(row) for row in _user_rows(app)] == user_rows_before, (
        "恢复入口不得新增/搬动用户消息"
    )
    assert (
        app.conn.execute("SELECT COUNT(*) c FROM nodes WHERE type = 'topic'").fetchone()["c"]
        == topics_before
    )
    assert (
        app.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]
        == fragments_before
    ), "恢复入口不得新建片段"
    assert (
        app.conn.execute("SELECT COUNT(*) c FROM turn_bindings").fetchone()["c"]
        == bindings_before
    ), "恢复入口不得抢占/新建轮次绑定"
    assert _run_dict(app, record["answer_key"])["phase"] == "answered"


# ── S8：恢复记录读取/收件箱不把 staged 正文当正式正文 ─────────────────


async def test_s8_inbox_never_leaks_staged_body_as_formal_answer(app, monkeypatch):
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = CountingAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_inbox")
    _fail_first_write(monkeypatch)

    failed = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert failed.get("code") == "recovery_persist_failed", failed
    run = _run_dict(app, record["answer_key"])
    assert run["phase"] == "staged" and run["staged_answer"] == "恢复后的回答"

    items = topic_recovery.list_pending_inbox_records(app.conn)
    assert [item["record_id"] for item in items] == [record["record_id"]]
    item = items[0]
    assert item["state_class"] == "answer_pending" and item["retryable"] is True
    assert item["answered"] is False and item["answer_id"] is None
    assert "answer" not in item, "收件箱不得把暂存正文当正式答案字段"
    assert "恢复后的回答" not in json.dumps(item, ensure_ascii=False), (
        "收件箱不得泄漏暂存正文"
    )
    detail = topic_recovery.inbox_record(app.conn, record["record_id"])
    assert detail is not None and detail["record_id"] == record["record_id"]
    assert "恢复后的回答" not in json.dumps(detail, ensure_ascii=False)

    done = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert done.get("ok") is True and done.get("answered") is True
    assert topic_recovery.list_pending_inbox_records(app.conn) == []
    detail = topic_recovery.inbox_record(app.conn, record["record_id"])
    assert detail["answered"] is True and detail["retryable"] is False
    assert detail["answer_id"] == done["answer_id"]
    assert "恢复后的回答" not in json.dumps(detail, ensure_ascii=False)


# ── S9：I14 冻结码：归属不一致 / 上下文为空 ───────────────────────────


async def test_s9_attribution_mismatch_returns_frozen_code(app):
    topic_id, _fragment_id, message_id = _prepare(app)
    other_topic = app.topics.nodes.create_topic("别的话题").id
    other_fragment = app.fragments.get_or_create_open(other_topic).id
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator()
    _wire(app, orchestrator, adapter)
    record = _make_record(
        app,
        topic_id,
        message_id,
        record_id="trec_bad_attr",
        fragment_id=other_fragment,
    )

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert result.get("ok") is False, result
    assert result.get("code") == "answer_attribution_mismatch", result
    assert result.get("recoverable") is True
    assert result.get("status_code") == 503
    assert adapter.calls == 0, "归属前提不成立时不得调用模型"
    assert _assistant_rows(app) == [], "I14：不得落正式答案行"
    assert topic_recovery.get_record(app, record["record_id"])["status"] == "pending"
    run = _run_dict(app, record["answer_key"])
    assert run.get("phase") == "failed" and run.get("status") == "pending", run


async def test_s9b_empty_context_returns_frozen_code(app):
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = CountingAdapter()
    orchestrator = FakeOrchestrator(messages=[])
    _wire(app, orchestrator, adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_empty_ctx")

    result = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert result.get("ok") is False, result
    assert result.get("code") == "answer_context_missing", result
    assert result.get("recoverable") is True
    assert result.get("status_code") == 503
    assert result.get("failed_stage") == "context_prepare"
    assert adapter.calls == 0, "上下文为空时不得调用模型"
    assert _assistant_rows(app) == [], "I14：不得落正式答案行"
    assert topic_recovery.get_record(app, record["record_id"])["status"] == "pending"
