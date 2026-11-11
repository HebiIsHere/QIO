# -*- coding: utf-8 -*-
"""R16 · E：两个**独立 SQLite 连接**下的并发竞争（契约 §10.3 / I01 / I04 / I09—I12）。

真实装置：同一数据库文件、两个独立 connect() 连接、两套 AppContext / FastAPI 应用；
并发用 threading.Barrier + 受控 Event（阻塞点用 asyncio.to_thread(release.wait, timeout)），
不使用 sleep 制造通过，也不无限等待。

覆盖：
1) 同一命令身份 + 同一参数在两条连接上同时提交：只执行一次，另一条拿到 replay；
   命令行只有一行、nav/position 只 +1、ANCHOR 只广播一次（I04/§5/§14.4）。
2) 两条连接拿同一 base_nav_version 并发发两条**不同**的真实导航：CAS 只允许一条成功，
   另一条 409 navigation_command_stale，位置由成功者决定，不产生双写（I01）。
3) 两条连接并发重试同一 answer_key 的两条恢复记录：只有一个执行者调用模型、
   只有一份正式答案；另一条结构化 202 recovery_in_progress（I09—I12/§14.6）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.api.events import EventType
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services import navigation_version, topic_recovery
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_request_id(tag: str) -> str:
    return f"req-r16e-{tag}-{uuid.uuid4().hex}"


def _anchor_events(ctx) -> list[dict]:
    return [
        dict(event.data or {})
        for event in list(ctx.bus._history)  # noqa: SLF001 - 与既有测试同口径读总线历史
        if getattr(event, "type", None) == EventType.ANCHOR
    ]


class _TwoBackends:
    """同一 DB 文件上的两套真实应用（两个独立 sqlite 连接 + 两个实例身份）。"""

    def __init__(self, tmp_path: Path, name: str = "r16e_race.db") -> None:
        self.path = tmp_path / name
        self.settings = Settings(data_dir=tmp_path)
        self.conn1 = connect(self.path)
        apply_migrations(self.conn1)
        self.conn2 = connect(self.path)
        # 两个独立连接 → 两套真实应用（各自 AppContext / EventBus / 实例身份）
        self.app1 = create_app(self.settings, self.conn1)
        self.app2 = create_app(self.settings, self.conn2)
        self.ctx1 = self.app1.state.ctx
        self.ctx2 = self.app2.state.ctx
        self.ctx1.credentials._kr = MemoryKeyring()
        self.ctx2.credentials._kr = MemoryKeyring()
        self.client1 = TestClient(self.app1)
        self.client2 = TestClient(self.app2)

    def __enter__(self):
        self.client1.__enter__()
        self.client2.__enter__()
        return self

    def __exit__(self, *exc):
        try:
            self.client2.__exit__(*exc)
        finally:
            self.client1.__exit__(*exc)
        self.conn1.close()
        self.conn2.close()
        return False

    # -- helpers ---------------------------------------------------------
    def anchor(self, which: int = 1) -> dict:
        client = self.client1 if which == 1 else self.client2
        body = client.get("/api/runtime/state").json()
        return body["anchor"]

    def command_rows(self) -> list[sqlite3.Row]:
        return self.conn1.execute(
            "SELECT identity_key, command_kind, status, param_digest FROM navigation_commands "
            "ORDER BY created_at"
        ).fetchall()


@pytest.fixture()
def two(tmp_path: Path):
    with _TwoBackends(tmp_path) as pair:
        yield pair


# ---------------------------------------------------------------------------
# 1) 同一命令身份并发：只执行一次
# ---------------------------------------------------------------------------


def test_two_connections_same_command_only_one_executes(two: _TwoBackends):
    topic = two.ctx1.topics.nodes.create_topic("并发同一命令").id
    before = two.anchor()
    nav0 = int(before["nav_version"])
    pos0 = int(before["position_version"])
    identity = before["position_identity"]
    request_id = _new_request_id("race-same")
    body = {
        "topic_id": topic,
        "fragment_id": None,
        "request_id": request_id,
        "base_nav_version": nav0,
    }
    barrier = threading.Barrier(2, timeout=20)

    def fire(client: TestClient):
        barrier.wait()
        return client.post("/api/anchor", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fire, two.client1), pool.submit(fire, two.client2)]
        responses = [future.result(timeout=60) for future in futures]

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    bodies = [r.json() for r in responses]
    changed = [b for b in bodies if b["changed"]]
    replay = [b for b in bodies if b["replay"]]
    assert len(changed) == 1, f"同一命令身份只能执行一次：{[b['changed'] for b in bodies]}"
    assert len(replay) == 1, f"另一条必须是 replay：{[b['replay'] for b in bodies]}"
    assert changed[0]["command_id"] == replay[0]["command_id"] == "request:" + request_id

    # 数据库事实：唯一命令行、版本只推进一次、位置只有一份
    rows = two.command_rows()
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["identity_key"] == "request:" + request_id
    assert rows[0]["status"] == "applied"
    after = two.anchor()
    assert after["nav_version"] == nav0 + 1, after
    assert after["position_version"] == pos0 + 1, after
    assert after["topic_id"] == topic
    assert after["position_identity"] == identity
    assert after == changed[0]["position"], "响应与库内权威位置必须一致"

    # 只广播一次 ANCHOR（两条连接的 bus 历史合起来）
    events = _anchor_events(two.ctx1) + _anchor_events(two.ctx2)
    assert len(events) == 1, f"重复执行/重复广播：{len(events)} 条 ANCHOR"
    assert events[0]["position"]["topic_id"] == topic


# ---------------------------------------------------------------------------
# 2) 同一 base 依据并发两条不同真实导航：CAS 只允许一条
# ---------------------------------------------------------------------------


def test_two_connections_same_base_only_one_navigation_wins(two: _TwoBackends):
    topic_a = two.ctx1.topics.nodes.create_topic("并发导航甲").id
    topic_b = two.ctx1.topics.nodes.create_topic("并发导航乙").id
    before = two.anchor()
    nav0 = int(before["nav_version"])
    pos0 = int(before["position_version"])
    bodies = [
        {
            "topic_id": topic_a,
            "fragment_id": None,
            "request_id": _new_request_id("race-a"),
            "base_nav_version": nav0,
        },
        {
            "topic_id": topic_b,
            "fragment_id": None,
            "request_id": _new_request_id("race-b"),
            "base_nav_version": nav0,
        },
    ]
    barrier = threading.Barrier(2, timeout=20)

    def fire(client: TestClient, body: dict):
        barrier.wait()
        return client.post("/api/anchor", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(fire, two.client1, bodies[0]),
            pool.submit(fire, two.client2, bodies[1]),
        ]
        responses = [future.result(timeout=60) for future in futures]

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], [r.text for r in responses]
    winner = next(r for r in responses if r.status_code == 200).json()
    loser = next(r for r in responses if r.status_code == 409).json()
    assert winner["changed"] is True
    assert loser["code"] == "navigation_command_stale", loser
    assert loser["position"] == winner["position"], "失败方返回当前权威位置，不猜"

    after = two.anchor()
    assert after["nav_version"] == nav0 + 1, "同一依据只能有一个真实导航生效"
    assert after["position_version"] == pos0 + 1
    assert after["topic_id"] == winner["position"]["topic_id"]
    # 成功一条 applied；失败方是该身份首次被拒 → 允许一条 superseded 事实行
    rows = [(r["identity_key"], r["status"]) for r in two.command_rows()]
    assert sum(1 for _, status in rows if status == "applied") == 1, rows
    assert sum(1 for _, status in rows if status == "superseded") == 1, rows
    events = _anchor_events(two.ctx1) + _anchor_events(two.ctx2)
    assert len(events) == 1, "被 CAS 拒绝的命令不得广播"


# ---------------------------------------------------------------------------
# 3) 两条连接并发执行同一 answer_key：只有一个执行者 / 一份正式答案
# ---------------------------------------------------------------------------


class _GatedAdapter:
    """第一次 complete 挂起，直到主线程放行；用于确定「谁在执行」而不是靠 sleep。"""

    mode = "native"
    model = "fake-r16e-race"
    supports_stream = False
    protocol_in_prompt = False

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.calls = 0
        self.started = threading.Event()

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.started.set()
        # 受控阻塞（有界），由主线程放行；不占用事件循环
        await asyncio.to_thread(self.release.wait, 20.0)
        return Completion(
            message=ChatMessage(role="assistant", content=ANSWER + "\n共享暂存后的唯一答案")
        )


def _seed_recovery_record(ctx: AppContext, *, record_id: str, turn_id: str, message_id: str, topic_id: str, fragment_id: str) -> str:
    record = topic_recovery.record_topic_failure(
        ctx,
        turn_id=turn_id,
        topic_id=topic_id,
        fragment_id=fragment_id,
        user_message_id=message_id,
        operation_id="op-r16e-race",
        param_version=1,
        recovery_nav_version=int(navigation_version.current(ctx.conn)),
        failed_stage="context_prepare",
        message="回答准备失败（E 并发用例）",
        record_id=record_id,
    )
    return str(record["record_id"])


def _prepare_answer_race(ctx: AppContext) -> tuple[str, str, str, str]:
    """真实建话题/开放片段/轮绑定/用户消息 + 两条同 answer_key 的恢复记录。"""
    topic_id = ctx.topics.nodes.create_topic("并发执行权话题").id
    fragment = ctx.fragments.get_or_create_open(topic_id)
    turn_id = "turn_r16e_race"
    ctx.bindings.record_binding(turn_id, topic_id, fragment_id=fragment.id)
    message_id, _ = ctx.memory.append_message(
        topic_id=topic_id,
        role="user",
        content="同一个逻辑答案的并发恢复",
        content_type="text",
        model="fake",
        turn_id=turn_id,
    )
    record_a = _seed_recovery_record(
        ctx,
        record_id="rec-r16e-race-a",
        turn_id=turn_id,
        message_id=message_id,
        topic_id=topic_id,
        fragment_id=fragment.id,
    )
    record_b = _seed_recovery_record(
        ctx,
        record_id="rec-r16e-race-b",
        turn_id=turn_id,
        message_id=message_id,
        topic_id=topic_id,
        fragment_id=fragment.id,
    )
    return topic_id, fragment.id, record_a, record_b


def test_two_connections_same_answer_key_single_executor(two: _TwoBackends):
    topic_id, fragment_id, record_a, record_b = _prepare_answer_race(two.ctx1)

    release = threading.Event()
    adapter_a = _GatedAdapter(release)
    adapter_b = _GatedAdapter(release)

    async def build_a():
        return adapter_a

    async def build_b():
        return adapter_b

    two.ctx1.build_adapter = build_a  # type: ignore[assignment]
    two.ctx2.build_adapter = build_b  # type: ignore[assignment]

    def retry(ctx: AppContext, record_id: str):
        return asyncio.run(topic_recovery.retry_topic_answer(ctx, record_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(retry, two.ctx1, record_a)
        future_b = pool.submit(retry, two.ctx2, record_b)
        # 有界等待：一定要有一个执行者进入模型调用（不 sleep）
        entered = adapter_a.started.wait(20.0) or adapter_b.started.wait(20.0)
        assert entered, "没有执行者进入模型调用：并发执行权没有被领取"
        done, _ = wait([future_a, future_b], timeout=20.0, return_when=FIRST_COMPLETED)
        assert done, "非执行者必须在执行期间返回结构化「处理中」，不能一起等待"
        loser_result = next(iter(done)).result()
        assert loser_result.get("in_progress") is True, loser_result
        assert loser_result.get("code") == "recovery_in_progress", loser_result
        assert int(loser_result.get("status_code", 0)) == 202, loser_result
        release.set()
        results = [f.result(timeout=30) for f in (future_a, future_b)]

    winner_result = next(r for r in results if not r.get("in_progress"))
    assert winner_result.get("ok") is True and winner_result.get("answered") is True, winner_result
    assert adapter_a.calls + adapter_b.calls == 1, (
        "同一 answer_key 只能有一次真实模型生成，实际调用=%d"
        % (adapter_a.calls + adapter_b.calls)
    )

    # 一份正式答案 + 两条记录同一 answer_id 收口 + 执行行 phase/暂存干净（I12/§14.6）
    messages = two.conn1.execute(
        "SELECT id, content, fragment_id FROM messages WHERE role = 'assistant' "
        "AND turn_id = ?",
        ("turn_r16e_race",),
    ).fetchall()
    assert len(messages) == 1, [dict(r) for r in messages]
    assert messages[0]["content"].endswith("共享暂存后的唯一答案")
    assert messages[0]["fragment_id"] == fragment_id, "正式答案必须写回原归属片段（I14）"

    answer_key = str(
        two.conn1.execute(
            "SELECT answer_key FROM messages WHERE id = ?", (messages[0]["id"],)
        ).fetchone()["answer_key"]
    )
    assert answer_key, "正式答案必须带 answer_key"
    run = two.conn1.execute(
        "SELECT phase, status, staged_answer FROM topic_answer_runs WHERE answer_key = ?",
        (answer_key,),
    ).fetchone()
    assert run is not None, "answer_key 的执行行必须存在"
    assert run["phase"] == "answered" and run["status"] == "answered", dict(run)
    assert not run["staged_answer"], "保存成功后必须清空共享暂存"

    recovery_table = topic_recovery.TOPIC_RECOVERY_TABLE
    record_rows = two.conn1.execute(
        f"SELECT record_id, status, answer_id FROM {recovery_table} "
        "WHERE record_id IN (?, ?)",
        (record_a, record_b),
    ).fetchall()
    assert len(record_rows) == 2, [dict(r) for r in record_rows]
    assert {row["status"] for row in record_rows} == {"answered"}, [dict(r) for r in record_rows]
    assert len({row["answer_id"] for row in record_rows}) == 1, [dict(r) for r in record_rows]
    assert record_rows[0]["answer_id"] == messages[0]["id"]
