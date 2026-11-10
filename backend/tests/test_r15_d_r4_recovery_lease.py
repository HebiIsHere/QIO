# -*- coding: utf-8 -*-
"""R15-D / R4（契约 §6）：逻辑答案执行权、租约续期与写入权收口。

基线红（旧实现按 recovery record 领租约、无心跳）：
* A：同一 answer_key 的两条恢复记录并发 -> 两次模型调用（最终只有一份答案）；
* B：活任务的原固定租约到期 -> 第二个请求接管并再次调用模型。

本文件用 fake 模型 + 真实临时 SQLite + 受控时钟（monkeypatch 模块级
`_now_utc`）/ `heartbeat_wait` 事件闸门控制交错，**不用 sleep 造通过**。
覆盖契约 §6.4 八类场景，并逐项断言：模型调用次数、正式答案行数、answer_id、
执行权（generation/owner）、记录状态。

诚实边界：外部模型调用**不**严格 exactly-once（崩溃在模型返回与结果持久化之间
仍可能再次调用）；这里只证明同一逻辑答案唯一持久化 + 执行权收口。
"""

from __future__ import annotations

import asyncio
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
from agent.storage.migrate import (
    apply_migrations,
    current_version,
    missing_objects,
    verify_required_objects,
)
from agent.storage.schema import MIGRATIONS, COMPENSATION_VERSION, SCHEMA_VERSION

ANSWER = "[[QIO:ANSWER]]"
RECOVERY_TABLE = "topic_recovery_records"
RUN_TABLE = "topic_answer_runs"
NEW_RECORD_COLUMNS = (
    "execution_generation",
    "execution_owner",
    "last_heartbeat_at",
)
NEW_RUN_INDEXES = ("idx_topic_answer_runs_status", "idx_topic_answer_runs_owner")
T0 = datetime(2026, 11, 11, 0, 0, 0, tzinfo=timezone.utc)
LEASE = 600.0


# ── 受控时钟 / 事件闸门（不用 sleep）──────────────────────────────────


class FakeClock:
    """租约时间唯一来源 `_now_utc` 的替身：测试显式推进时间。"""

    def __init__(self, moment: datetime = T0) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> datetime:
        self.moment = self.moment + timedelta(seconds=float(seconds))
        return self.moment


class HeartbeatControl:
    """`heartbeat_wait` 测试缝：每次续租前等待一个事件，测试按需放行。

    返回前不做任何真实等待；测试用 `release(index)` 放行第 index 次续租。
    """

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
    """让出事件循环直到条件成立（事件驱动轮询，不用墙钟 sleep）。"""
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0)
    return False


# ── fake 模型 / 编排器 ───────────────────────────────────────────────


class GateAdapter:
    """可计数 fake 模型：每次调用可挂在事件闸门上，可注入指定次数的异常。"""

    mode = "native"
    model = "fake-r15-d-r4"
    protocol_in_prompt = False

    def __init__(
        self,
        *,
        text: str = ANSWER + "\n恢复后的回答",
        gates: int = 0,
        error_calls: dict[int, Exception] | None = None,
    ) -> None:
        self.text = text
        self.calls = 0
        self.started = [asyncio.Event() for _ in range(gates)]
        self.release = [asyncio.Event() for _ in range(gates)]
        self.error_calls = dict(error_calls or {})

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        index = self.calls - 1
        if index < len(self.started):
            self.started[index].set()
            await self.release[index].wait()
        error = self.error_calls.get(self.calls)
        if error is not None:
            raise error
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


def _wire(app: AppContext, orchestrator, adapter) -> None:
    app.turn_orchestrator = orchestrator  # type: ignore[assignment]

    async def _build_adapter():
        return adapter

    app.build_adapter = _build_adapter  # type: ignore[method-assign]


# ── 数据准备 / 读取 ──────────────────────────────────────────────────


@pytest.fixture()
def app(tmp_path) -> AppContext:
    conn = connect(tmp_path / "r15_d_r4.db")
    apply_migrations(conn)
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
        "operation_id": "op-r15-d",
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


def _record_row(app: AppContext, record_id: str):
    return app.conn.execute(
        "SELECT * FROM " + RECOVERY_TABLE + " WHERE record_id = ?", (record_id,)
    ).fetchone()


def _run_row(app: AppContext, answer_key: str):
    return app.conn.execute(
        "SELECT * FROM " + RUN_TABLE + " WHERE answer_key = ?", (answer_key,)
    ).fetchone()


def _register_instance(
    app: AppContext, instance_id: str, *, exited_at: str | None = None
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    app.conn.execute(
        "INSERT OR REPLACE INTO instances "
        "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (instance_id, 999999, "test-host", now, now, exited_at),
    )


def _stub_clock(monkeypatch, clock: FakeClock) -> None:
    monkeypatch.setattr(topic_recovery, "_now_utc", clock.now)


# ── 场景 1：同 answer_key、不同 record 并发 -> 只调一次模型 ──────────


async def test_s1_two_records_same_answer_key_only_one_model_call(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = GateAdapter(gates=1)
    _wire(app, FakeOrchestrator(), adapter)
    first = _make_record(app, topic_id, message_id, record_id="trec_a")
    second = _make_record(app, topic_id, message_id, record_id="trec_b")
    assert first["answer_key"] == second["answer_key"]

    task = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, first["record_id"])
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)

    waiter = await topic_recovery.retry_topic_answer(app, second["record_id"])
    assert adapter.calls == 1, "同一逻辑答案只能有一次模型调用；实际=%d" % adapter.calls
    assert waiter.get("in_progress") is True, waiter
    assert waiter.get("code") == "recovery_in_progress", waiter
    assert waiter.get("status_code") == 202, waiter

    adapter.release[0].set()
    done = await asyncio.wait_for(task, timeout=10)
    assert done.get("ok") is True and done.get("answered") is True, done
    assert adapter.calls == 1
    rows = _assistant_rows(app)
    assert len(rows) == 1, "同一逻辑答案只能保存一份：%s" % [dict(r) for r in rows]
    assert rows[0]["answer_key"] == first["answer_key"]

    for record_id in (first["record_id"], second["record_id"]):
        stored = topic_recovery.get_record(app, record_id)
        assert stored["status"] == "answered", stored
        assert stored["answer_id"] == done["answer_id"], stored
    run = _run_row(app, first["answer_key"])
    assert run["status"] == "answered" and run["answer_id"] == done["answer_id"]
    assert int(run["generation"]) == 1, "没有执行权竞争时不应平白推进世代"


# ── 场景 2：同一条 record 并发 -> 只调一次模型 ───────────────────────


async def test_s2_same_record_concurrent_only_one_model_call(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = GateAdapter(gates=1)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_same")

    task = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"])
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)

    waiter = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert adapter.calls == 1
    assert waiter.get("code") == "recovery_in_progress", waiter
    assert waiter.get("record_id") == record["record_id"]

    adapter.release[0].set()
    done = await asyncio.wait_for(task, timeout=10)
    assert done.get("answered") is True
    assert adapter.calls == 1
    assert len(_assistant_rows(app)) == 1
    assert _run_row(app, record["answer_key"])["status"] == "answered"


# ── 场景 3：活任务超过原租约周期（心跳续租）-> 仍 202 且模型仍 1 次 ──


async def test_s3_live_task_over_original_lease_stays_in_progress(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = GateAdapter(gates=1)
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
    run = _run_row(app, record["answer_key"])
    original_until = topic_recovery._parse_ts(run["claim_until"])
    assert original_until == T0 + timedelta(seconds=LEASE), original_until

    # 时间推进 500s（原租约尚未到期）后放行一次心跳续租：租约被续到 T0+1100。
    assert await _drive_until(lambda: control.waits >= 1)
    clock.advance(500)
    control.release(0)
    extended = T0 + timedelta(seconds=1100)
    assert await _drive_until(
        lambda: topic_recovery._parse_ts(_run_row(app, record["answer_key"])["claim_until"])
        == extended
    ), "心跳必须把租约续到原到期时间之后（活任务不因固定时长被重开）"
    assert _record_row(app, record["record_id"])["last_heartbeat_at"] is not None

    # 再推进到 T0+700：超过**原** 600s 租约周期，但仍在续租后的有效期内。
    clock.advance(200)
    waiter = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert waiter.get("code") == "recovery_in_progress", waiter
    assert waiter.get("status_code") == 202
    assert adapter.calls == 1, "活任务不得因固定租约到期被重新生成"
    run = _run_row(app, record["answer_key"])
    assert int(run["generation"]) == 1, "没有接管就不应推进世代"

    adapter.release[0].set()
    done = await asyncio.wait_for(task, timeout=10)
    assert done.get("answered") is True
    assert len(_assistant_rows(app)) == 1


# ── 场景 4：真正失效实例（owner_alive=False）可接管，generation+1 ──


async def test_s4_dead_instance_takeover_bumps_generation(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    app.instance_id = "inst_old"
    _register_instance(app, "inst_old", exited_at=T0.isoformat())
    adapter = GateAdapter(gates=2)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_takeover")

    task1 = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app, record["record_id"], lease_seconds=LEASE
        )
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    run = _run_row(app, record["answer_key"])
    assert int(run["generation"]) == 1 and run["owner_instance"] == "inst_old"

    # 旧实例被确认已退出 + 租约到期 -> 新实例可接管。
    clock.advance(700)
    app.instance_id = "inst_new"
    task2 = asyncio.create_task(
        topic_recovery.retry_topic_answer(
            app, record["record_id"], lease_seconds=LEASE
        )
    )
    await asyncio.wait_for(adapter.started[1].wait(), timeout=10)
    run = _run_row(app, record["answer_key"])
    assert int(run["generation"]) == 2, "接管必须递增执行世代"
    assert run["owner_instance"] == "inst_new"
    record_row = _record_row(app, record["record_id"])
    assert int(record_row["execution_generation"]) == 2
    assert str(record_row["execution_owner"]).startswith("inst_new|")
    assert record_row["status"] == "pending"

    adapter.release[1].set()
    done2 = await asyncio.wait_for(task2, timeout=10)
    assert done2.get("answered") is True, done2
    assert len(_assistant_rows(app)) == 1
    run = _run_row(app, record["answer_key"])
    assert run["status"] == "answered" and int(run["generation"]) == 2

    # 旧执行权作废：旧任务恢复后不得写答案 / 改记录。
    adapter.release[0].set()
    done1 = await asyncio.wait_for(task1, timeout=10)
    assert done1.get("ok") is False and done1.get("superseded") is True, done1
    assert done1.get("code") == "recovery_execution_superseded", done1
    assert adapter.calls == 2, "接管后模型调用总次数 = 旧 + 新（不声称 exactly-once）"
    assert len(_assistant_rows(app)) == 1
    stored = topic_recovery.get_record(app, record["record_id"])
    assert stored["status"] == "answered"
    assert stored["answer_id"] == done2["answer_id"]


# ── 场景 5：旧执行者迟到成功（新任务仍在跑）-> superseded 且零写入 ──


async def test_s5_old_executor_late_success_is_superseded(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    app.instance_id = "inst_old"
    _register_instance(app, "inst_old", exited_at=T0.isoformat())
    adapter = GateAdapter(gates=2)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_late_ok")

    task1 = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    clock.advance(700)
    app.instance_id = "inst_new"
    task2 = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[1].wait(), timeout=10)

    before_run = dict(_run_row(app, record["answer_key"]))
    before_record = dict(_record_row(app, record["record_id"]))
    before_messages = [dict(row) for row in _assistant_rows(app)]

    # 旧执行者（新执行者还没完成）迟到成功：必须整体放弃写入。
    adapter.release[0].set()
    done1 = await asyncio.wait_for(task1, timeout=10)
    assert done1.get("ok") is False and done1.get("superseded") is True, done1
    assert done1.get("code") == "recovery_execution_superseded", done1

    after_run = dict(_run_row(app, record["answer_key"]))
    after_record = dict(_record_row(app, record["record_id"]))
    after_messages = [dict(row) for row in _assistant_rows(app)]
    assert after_messages == before_messages, "旧执行者不得写入答案行"
    for key in ("status", "generation", "owner", "owner_instance", "claim_until", "answer_id"):
        assert after_run[key] == before_run[key], (
            "旧执行者不得改新执行权的 run 状态：%s" % key
        )
    for key in (
        "status",
        "execution_generation",
        "execution_owner",
        "claim_owner",
        "claim_until",
        "answer_text",
        "last_error",
        "answer_id",
    ):
        assert after_record[key] == before_record[key], (
            "旧执行者不得改新任务记录：%s" % key
        )

    adapter.release[1].set()
    done2 = await asyncio.wait_for(task2, timeout=10)
    assert done2.get("answered") is True
    assert adapter.calls == 2
    assert len(_assistant_rows(app)) == 1


# ── 场景 6：旧执行者迟到失败 -> 不得把新任务记录改成失败 / 清新租约 ──


async def test_s6_old_executor_late_failure_does_not_touch_new_lease(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    app.instance_id = "inst_old"
    _register_instance(app, "inst_old", exited_at=T0.isoformat())
    adapter = GateAdapter(
        gates=2, error_calls={1: RuntimeError("旧执行者模型失败(注入)")}
    )
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_late_fail")

    task1 = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    clock.advance(700)
    app.instance_id = "inst_new"
    task2 = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[1].wait(), timeout=10)

    before_run = dict(_run_row(app, record["answer_key"]))
    before_record = dict(_record_row(app, record["record_id"]))

    adapter.release[0].set()
    done1 = await asyncio.wait_for(task1, timeout=10)
    assert done1.get("ok") is False and done1.get("superseded") is True, done1
    assert done1.get("code") == "recovery_execution_superseded", done1

    after_run = dict(_run_row(app, record["answer_key"]))
    after_record = dict(_record_row(app, record["record_id"]))
    for key in ("status", "generation", "owner", "claim_until", "answer_id"):
        assert after_run[key] == before_run[key], "迟到失败不得清新租约：%s" % key
    for key in (
        "status",
        "failed_stage",
        "last_error",
        "execution_owner",
        "execution_generation",
        "answer_text",
    ):
        assert after_record[key] == before_record[key], "迟到失败不得改新任务记录：%s" % key

    adapter.release[1].set()
    done2 = await asyncio.wait_for(task2, timeout=10)
    assert done2.get("answered") is True, done2
    assert len(_assistant_rows(app)) == 1
    assert _assistant_rows(app)[0]["content"] == "恢复后的回答"


# ── 场景 7：保存失败后只补存、不再次调用模型（既有语义）─────────────


async def test_s7_persist_failure_retry_only_stashes(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    adapter = GateAdapter()
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_persist")

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
    assert adapter.calls == 1
    assert _assistant_rows(app) == []
    stashed = topic_recovery.get_record(app, record["record_id"])
    assert stashed["status"] == "pending"
    assert stashed.get("answer_text"), "保存失败必须暂存已生成正文（不得丢失）"
    run = _run_row(app, record["answer_key"])
    assert run["status"] == "pending", "保存失败后释放执行权，可再重试"

    second = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert second.get("ok") is True and second.get("answered") is True, second
    assert adapter.calls == 1, "补存不得再次调用模型；实际=%d" % adapter.calls
    assert len(_assistant_rows(app)) == 1
    assert _run_row(app, record["answer_key"])["status"] == "answered"


# ── 场景 8：接管与保存竞争 -> 只一份答案；保存事务不覆盖新执行权状态 ──


async def test_s8_save_race_does_not_overwrite_new_execution_state(app, monkeypatch):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    app.instance_id = "inst_old"
    _register_instance(app, "inst_old", exited_at=T0.isoformat())
    adapter = GateAdapter(gates=2)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_save_race")

    task1 = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    old_run = _run_row(app, record["answer_key"])
    stale_execution = {
        "answer_key": record["answer_key"],
        "generation": int(old_run["generation"]),
        "owner": str(old_run["owner"]),
        "owner_instance": str(old_run["owner_instance"]),
        "lease_seconds": LEASE,
        "record_id": record["record_id"],
    }

    # 新执行权接管并由它完成保存。
    clock.advance(700)
    app.instance_id = "inst_new"
    task2 = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[1].wait(), timeout=10)
    adapter.release[1].set()
    done2 = await asyncio.wait_for(task2, timeout=10)
    assert done2.get("answered") is True
    after_run = dict(_run_row(app, record["answer_key"]))
    assert int(after_run["generation"]) == 2
    assert after_run["status"] == "answered"
    assert after_run["answer_id"] == done2["answer_id"]
    rows_after_new = [dict(row) for row in _assistant_rows(app)]
    assert len(rows_after_new) == 1

    # 旧执行的保存事务与新执行权竞争：CAS 失败 -> 整体回滚，零写入。
    stale_record = topic_recovery.get_record(app, record["record_id"])
    with pytest.raises(topic_recovery._ExecutionLost):
        topic_recovery._save_answer(
            app.conn,
            stale_record,
            "旧执行者迟到的正文",
            model="fake",
            execution=stale_execution,
        )
    final_run = dict(_run_row(app, record["answer_key"]))
    assert final_run == after_run, "保存事务不得覆盖新执行权状态"
    assert [dict(row) for row in _assistant_rows(app)] == rows_after_new
    adapter.release[0].set()
    done1 = await asyncio.wait_for(task1, timeout=10)
    assert done1.get("superseded") is True, done1
    assert [dict(row) for row in _assistant_rows(app)] == rows_after_new


# ── 补充：owner_alive True / None 都不接管（只 202，不重开）───────────


@pytest.mark.parametrize("owner_mode", ["alive", "unknown"])
async def test_s9_alive_or_unknown_owner_is_never_taken_over(
    app, monkeypatch, owner_mode
):
    clock = FakeClock()
    _stub_clock(monkeypatch, clock)
    topic_id, _fragment_id, message_id = _prepare(app)
    if owner_mode == "alive":
        # ctx 自己的实例在 instances 表里且心跳新鲜 -> owner_alive=True。
        pass
    else:
        # 实例表里没有它 -> owner_alive=None（unknown 不改状态）。
        app.instance_id = "inst_ghost"
    adapter = GateAdapter(gates=1)
    _wire(app, FakeOrchestrator(), adapter)
    record = _make_record(app, topic_id, message_id, record_id="trec_owner_probe")

    task = asyncio.create_task(
        topic_recovery.retry_topic_answer(app, record["record_id"], lease_seconds=LEASE)
    )
    await asyncio.wait_for(adapter.started[0].wait(), timeout=10)
    # 让租约到期（无心跳续租），但旧实例既不是「确认已退出」也不是活任务续租。
    clock.advance(700)
    waiter = await topic_recovery.retry_topic_answer(app, record["record_id"])
    assert waiter.get("code") == "recovery_in_progress", waiter
    assert waiter.get("status_code") == 202
    assert adapter.calls == 1, "True/None 都不得接管并重新生成"
    assert int(_run_row(app, record["answer_key"])["generation"]) == 1

    adapter.release[0].set()
    done = await asyncio.wait_for(task, timeout=10)
    assert done.get("answered") is True
    assert len(_assistant_rows(app)) == 1


# ── 迁移 81 / 补偿 82：对象、补偿可补齐、只追加 ─────────────────────


def test_s10_migration_81_and_compensation_82_objects(tmp_path):
    targets = [target for target, _ in MIGRATIONS]
    assert targets == sorted(set(targets)), "迁移号必须唯一且递增"
    assert targets[-2:] == [81, 82], targets[-4:]
    assert SCHEMA_VERSION == COMPENSATION_VERSION == 82 == max(targets)

    statements = [
        str(statement)
        for target, items in MIGRATIONS
        if target == COMPENSATION_VERSION
        for statement in items
    ]
    for statement in statements:
        head = statement.strip().upper()
        assert not head.startswith(("DROP ", "DELETE ", "UPDATE ", "TRUNCATE")), (
            "补偿 82 必须是纯建对象 DDL：%s" % statement[:60]
        )
    blob = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS " + RUN_TABLE in blob
    for column in NEW_RECORD_COLUMNS:
        assert ("ADD COLUMN " + column) in blob, column
    for index in NEW_RUN_INDEXES:
        assert index in blob, index

    conn = connect(tmp_path / "r15_d_migration.db")
    try:
        assert apply_migrations(conn) == SCHEMA_VERSION
        assert current_version(conn) == 82
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert RUN_TABLE in tables
        run_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({RUN_TABLE})")
        }
        assert {
            "answer_key",
            "status",
            "generation",
            "owner",
            "owner_instance",
            "claim_until",
            "record_id",
            "answer_id",
            "created_at",
            "updated_at",
        } <= run_columns
        recovery_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({RECOVERY_TABLE})")
        }
        assert set(NEW_RECORD_COLUMNS) <= recovery_columns
        assert not any(missing_objects(conn).values())

        # 版本已到 82、对象却被删掉：按对象复核 + 补偿 82 能补回来（不动数据）。
        conn.execute(f"DROP TABLE {RUN_TABLE}")
        conn.execute("DROP INDEX IF EXISTS idx_topic_answer_runs_status")
        conn.execute("DROP INDEX IF EXISTS idx_topic_answer_runs_owner")
        assert RUN_TABLE in missing_objects(conn)["tables"]
        still_missing = verify_required_objects(conn)
        assert not any(still_missing.values()), still_missing
        assert RUN_TABLE in {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def test_s11_migration_module_apply_is_idempotent(tmp_path):
    from agent.storage.migration_r15_r4_recovery_lease import MIGRATION, apply

    conn = connect(tmp_path / "r15_d_module.db")
    try:
        apply_migrations(conn)
        executed = apply(conn)
        # 幂等：列已在时不再执行 ADD COLUMN（CREATE ... IF NOT EXISTS 是无害空操作）。
        assert all(" ADD COLUMN " not in str(statement).upper() for statement in executed)
        assert MIGRATION[0] == 81
        conn.execute(f"DROP TABLE {RUN_TABLE}")
        # 直接重放模块 DDL 能补回表（与补偿同一口径）。
        replayed = apply(conn)
        assert any(
            ("CREATE TABLE IF NOT EXISTS " + RUN_TABLE) in str(statement)
            for statement in replayed
        )
    finally:
        conn.close()
