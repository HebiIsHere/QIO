"""F1: turn 阶段时间 —— 一轮的时间必须能被 Trace 解释。

真实事故（docs/release-qualification.md §9）：一次真实 turn `duration_ms=51498`，
其中模型调用只有 1688ms —— 49.8 秒在 Trace 里没有任何分区解释。以前只有两类耗时
被记录（model_calls / tool_runs），凭据与能力探测、上下文装配与检索、审批等待、
落库、收尾记忆处理、事件投递全都不可见。

这里用确定性的假 provider 复现同一形状（不联网、不需要真实 Key）：
一次「慢的非模型等待」（审批等待）+「慢的凭据/能力探测」把 turn 拉长到远超模型
耗时，然后断言这些时间必须落在具名阶段里，且
`sum(顶层阶段) + residual ≈ duration_ms`。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.turn import TurnManager
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.trace.phases import PhaseTimer
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _EchoTool(Tool):
    name = "f_echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=f"echo:{kwargs.get('text', '')}")


class _ToolThenAnswer:
    """永远先要一次工具调用：好让迭代预算耗尽，走到「继续/停止」审批。"""

    mode = "native"
    model = "deepseek-v4-flash"

    async def complete(self, messages, tools, **kwargs):
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="c1", name="f_echo", arguments={"text": "hi"})],
            ),
            usage={"prompt_tokens": 10, "completion_tokens": 4},
        )


class _SlowApproval:
    """假审批通道：模拟「用户在确认框前想了 delay 秒」。

    这是 loop 里唯一**无上界**的等待（真实 timeout 300s）：模型早就回来了，
    turn 却还在跑 —— 这正是 51.5s / 1.7s 那个形状最可能的来源。
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[str] = []

    def set_context(self, **_kwargs) -> None:
        return None

    def pending(self) -> list:
        return []

    def interrupted(self) -> list:
        return []

    async def request(self, kind, payload, **_kwargs):
        self.calls.append(kind)
        await asyncio.sleep(self.delay)
        return SimpleNamespace(decision="rejected")


def _phase_ms(trace: dict, name: str) -> int:
    return sum(
        int(s["ms"]) for s in trace["phases"]["spans"] if s["name"] == name
    )


async def test_turn_duration_is_explained_by_named_phases(ctx: AppContext, tmp_path):
    topic = ctx.topics.nodes.create_topic("阶段话题").id
    ctx.registry.register(_EchoTool())
    ctx.settings_store.set("loop.max_iterations", "1")
    ctx.approvals = _SlowApproval(delay=0.9)

    async def _slow_adapter():
        # 首次凭据/能力探测要打一次 provider（真实路径可能几秒到几十秒）
        await asyncio.sleep(0.6)
        return _ToolThenAnswer()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", _slow_adapter)
    try:
        await ctx.run_turn("请调用工具", topic_id=topic)
    finally:
        monkeypatch.undo()

    trace = ctx.trace_store.get(ctx.trace_store.list(limit=1)[0]["turn_id"])
    assert trace["status"] == "done"
    duration = int(trace["duration_ms"])

    model_ms = sum(int(c["latency_ms"]) for c in trace["model_calls"])
    tool_ms = sum(int(t["duration_ms"]) for t in trace["tool_runs"])
    # 前提：模型 + 工具远不足以解释这一轮 —— 与真实事故同形
    assert duration - model_ms - tool_ms >= 1200, (duration, model_ms, tool_ms)

    phases = trace["phases"]
    names = {s["name"] for s in phases["spans"]}
    for required in (
        "adapter_setup",
        "turn_setup",
        "context_assembly",
        "agent_loop",
        "persistence",
        "memory_post",
        "finalize",
        "model_wait",
        "tool_wait",
        "approval_wait",
    ):
        assert required in names, (required, names)

    # 那几十秒的真正去处，必须是具名阶段
    assert _phase_ms(trace, "adapter_setup") >= 500
    assert _phase_ms(trace, "approval_wait") >= 800
    assert ctx.approvals.calls == ["continue"]  # 确实走的是「继续/停止」审批

    # 铺满：顶层阶段 + residual ≈ duration
    top = [s for s in phases["spans"] if s["depth"] == 1]
    assert top, phases["spans"]
    # 各阶段之和不得大于 duration（否则就是把 TURN_END / 台账收尾算进了执行时间）
    assert sum(int(s["ms"]) for s in top) <= duration + 5, phases["spans"]
    assert abs(sum(int(s["ms"]) for s in top) + int(phases["residual_ms"]) - duration) <= 20
    # 不允许几十秒继续成为 unknown
    assert int(phases["residual_ms"]) <= 50, phases


async def test_queue_wait_is_recorded_separately_from_duration(ctx: AppContext):
    """排队等待也是用户等的时间，但它不属于「执行中」的 duration。"""
    topic = ctx.topics.nodes.create_topic("排队话题").id

    class _SlowFirst:
        mode = "native"
        model = "m"

        def __init__(self) -> None:
            self.first = True

        async def complete(self, messages, tools, **kwargs):
            if self.first:
                self.first = False
                await asyncio.sleep(0.5)
            return Completion(
                message=ChatMessage(role="assistant", content="好"),
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )

    adapter = _SlowFirst()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await asyncio.gather(
            ctx.run_turn("第一条", topic_id=topic),
            ctx.run_turn("第二条（排队）", topic_id=topic),
        )
    finally:
        monkeypatch.undo()

    traces = {t["turn_id"]: ctx.trace_store.get(t["turn_id"]) for t in ctx.trace_store.list(limit=10)}
    queued = [
        tr for tr in traces.values()
        if int((tr["phases"].get("notes") or {}).get("queue_wait_ms") or 0) >= 300
    ]
    assert len(queued) == 1, [
        (tr["turn_id"], (tr["phases"].get("notes") or {})) for tr in traces.values()
    ]
    only = queued[0]
    # 排队等待不计进 duration：第二条的执行时长明显小于它排队等的时间
    assert int(only["duration_ms"]) < only["phases"]["notes"]["queue_wait_ms"]


def test_phase_timer_tiles_the_timeline_with_explicit_other():
    """顶层阶段铺满时间轴；空档进显式的 other；嵌套不重复计入合计。"""
    import time as _time

    timer = PhaseTimer()
    with timer.phase("a"):
        _time.sleep(0.02)
    _time.sleep(0.02)  # 空档 → other
    with timer.phase("b"):
        _time.sleep(0.01)
        with timer.phase("b.inner"):
            _time.sleep(0.02)
    payload = timer.payload()
    names = [s["name"] for s in payload["spans"] if s["depth"] == 1]
    assert names == ["a", "other", "b"], names
    assert abs(payload["sum_ms"] - payload["total_ms"]) <= 5
    assert payload["residual_ms"] <= 5
    # 嵌套只作细分：b 的时长包含 b.inner，但合计只算一次
    assert payload["sum_ms"] < sum(s["ms"] for s in payload["spans"]) + 1
    assert any(s["depth"] == 2 and s["name"] == "b.inner" for s in payload["spans"])


def test_stop_while_a_stage_is_open_does_not_double_count():
    """收口时还有阶段开着（finish 在 finalize 阶段里调用）：不得补记一段 other。"""
    import time as _time

    timer = PhaseTimer()
    with timer.phase("a"):
        _time.sleep(0.01)
    opened = timer.phase("open_stage")
    opened.__enter__()
    _time.sleep(0.02)
    timer.stop()

    payload = timer.payload()
    top = [s for s in payload["spans"] if s["depth"] == 1]
    assert [s["name"] for s in top] == ["a", "open_stage"]
    # 每段各自取整，允许 1ms 级别的舍入差；关键是**没有**多记一段 other
    assert abs(sum(int(s["ms"]) for s in top) - payload["total_ms"]) <= 2


def test_phases_payload_shape_is_always_complete(db_conn):
    """读的人（界面 / 分析脚本）不该因为缺键而炸：账本形状必须始终完整。"""
    from agent.trace.store import TraceStore

    store = TraceStore(db_conn)
    store.begin("turn_shape")
    store.set_phases(
        "turn_shape",
        {"version": 1, "total_ms": 120, "sum_ms": 100, "spans": [{"name": "a", "ms": 100}]},
    )
    store.finish("turn_shape", "done")

    payload = store.get("turn_shape")["phases"]
    assert set(payload) >= {"version", "total_ms", "sum_ms", "residual_ms", "spans"}
    assert int(payload["residual_ms"]) >= 0
    # 旧行（迁移前写入）默认 {}，读取端容忍
    db_conn.execute(
        "INSERT INTO turn_traces (turn_id, status, started_at) VALUES ('legacy', 'done', "
        "'2026-01-01T00:00:00+00:00')"
    )
    assert store.get("legacy")["phases"] == {}


def test_phase_notes_are_redacted(ctx: AppContext):
    """阶段账本走存储层，同样不能出现密钥原文。"""
    from agent.trace.recorder import TurnTracer

    secret = "sk-FAKESecretPhases1234567890"
    tracer = TurnTracer(ctx.trace_store, "turn_secret")
    ctx.trace_store.begin("turn_secret")
    tracer.note("queue_wait_ms", 5)
    with tracer.phase("custom", f"about {secret}"):
        pass
    ctx.trace_store.finish("turn_secret", "done")

    row = ctx.conn.execute("SELECT * FROM turn_traces WHERE turn_id = 'turn_secret'").fetchone()
    blob = json.dumps(dict(row), ensure_ascii=False, default=str)
    assert secret not in blob
    assert "custom" in blob


async def test_derived_work_is_recorded_after_turn(ctx: AppContext):
    """次级任务（摘要/知识派生）在 turn 之后跑：耗时与失败都要看得见。"""
    from agent.trace.recorder import TurnTracer

    tracer = TurnTracer(ctx.trace_store, "turn_derived")
    ctx.trace_store.begin("turn_derived")

    async def _slow_drain(adapter, *, limit=3, tracer=None):
        await asyncio.sleep(0.05)
        if tracer is not None:
            tracer.write("summaries", "frag_x:标题")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        ctx.memory_lifecycle, "drain_derived_tasks", _slow_drain
    )
    try:
        ctx.turn_orchestrator._schedule_derived_work(object(), tracer)
        await asyncio.sleep(0.2)
    finally:
        monkeypatch.undo()
    ctx.trace_store.finish("turn_derived", "done")

    trace = ctx.trace_store.get("turn_derived")
    after = trace["phases"]["after_turn"]
    assert [a["name"] for a in after] == ["derived_work"]
    assert int(after[0]["ms"]) >= 40
    assert trace["writes"]["summaries"] == ["frag_x:标题"]


async def test_derived_work_failure_lands_in_trace(ctx: AppContext):
    from agent.trace.recorder import TurnTracer

    tracer = TurnTracer(ctx.trace_store, "turn_derived_fail")
    ctx.trace_store.begin("turn_derived_fail")

    async def _boom(adapter, *, limit=3, tracer=None):
        raise RuntimeError("provider 500: summary failed")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx.memory_lifecycle, "drain_derived_tasks", _boom)
    try:
        ctx.turn_orchestrator._schedule_derived_work(object(), tracer)
        await asyncio.sleep(0.2)
    finally:
        monkeypatch.undo()

    trace = ctx.trace_store.get("turn_derived_fail")
    codes = [w["code"] for w in trace["warnings"]]
    assert "derived_work_failed" in codes, trace["warnings"]
    assert trace["phases"]["after_turn"], trace["phases"]


async def test_phase_flush_happens_even_without_finish(ctx: AppContext):
    """没走到 finish 的路径（例如凭据不可用）也必须留下阶段时间。"""
    class _NoCredential:
        def set_context(self, **_kwargs):
            return None

    ctx.approvals = _NoCredential()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=None))
    try:
        result = await ctx.run_turn("没有凭据", topic_id=ctx.current_topic())
    finally:
        monkeypatch.undo()
    assert result["reason"] == "no_credential"

    trace = ctx.trace_store.get(ctx.trace_store.list(limit=1)[0]["turn_id"])
    assert trace["status"] == "unavailable"
    assert any(s["name"] == "adapter_setup" for s in trace["phases"]["spans"])
