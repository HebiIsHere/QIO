"""D 独立验证 + 耗时口径取证（契约 §3 + task-4 第 2 条）。

两件事分开看：

1. **契约验证**：TURN_END 必须带权威总耗时字段（duration_ms / queue_ms /
   started_at / ended_at），且与 trace 台账一致；缺失 != 0；并行分项不得求和冒充总耗时。
2. **口径取证**：把一轮真 turn 的 duration / 顶层阶段合计 / 全部 span 原始合计 /
   各工具自身耗时合计 / queue_wait / after_turn 一起打出来（TIMING_EVIDENCE 行），
   用可复现命令给出数字，指出「并行分项求和」与「总耗时」差在哪。

基线（ee6bbff）现状：TURN_END 只有 status/final_content/error，没有耗时字段 ——
带契约字段的用例在实现合并前应当是**红的**；口径取证用例对既有 trace 台账成立，
应当是**绿的**（它测的是现在就能复现的事实）。

运行：
    cd backend
    uv run --frozen --extra dev pytest tests/test_timing_contract_verify.py -q
    uv run --frozen --extra dev pytest tests/test_timing_contract_verify.py -q -s -k parallel
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.turn import TurnManager
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult

TIMING_FIELDS = ("duration_ms", "queue_ms", "started_at", "ended_at")


def _app_ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "timing-verify.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


class _PlanAdapter:
    mode = "native"
    model = "fake-timing-verify"

    def __init__(self, steps: list[Completion]) -> None:
        self.steps = list(steps)

    async def complete(self, messages, tools, **kwargs):
        if self.steps:
            return self.steps.pop(0)
        return Completion(message=ChatMessage(role="assistant", content="完成"))


def _final(text: str) -> Completion:
    return Completion(message=ChatMessage(role="assistant", content=text))


def _tool_step(calls: list[tuple[str, str, dict]]) -> Completion:
    return Completion(
        message=ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id=call_id, name=name, arguments=args) for call_id, name, args in calls
            ],
        )
    )


async def _run(ctx: AppContext, topic: str, adapter: _PlanAdapter, message: str = "跑一轮"):
    async def _fake_build(*args, **kwargs):
        return adapter

    ctx.build_adapter = _fake_build  # type: ignore[method-assign]
    return await ctx.run_turn(message, topic_id=topic)


def _events(ctx: AppContext, type_name: str) -> list:
    return [event for event in ctx.bus._history if event.type.value == type_name]


def _parse_iso(value: object) -> datetime:
    assert isinstance(value, str) and value, f"时间戳必须是 ISO 字符串：{value!r}"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _span_ms(trace: dict, name: str, depth: int | None = None) -> int:
    spans = (trace.get("phases") or {}).get("spans") or []
    return sum(
        int(span["ms"])
        for span in spans
        if span["name"] == name and (depth is None or int(span.get("depth", 1)) == depth)
    )


# ---- 1. TURN_END 的权威耗时字段（契约 §3） --------------------------------------


async def test_turn_end_carries_authoritative_timing_fields(tmp_path):
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("耗时字段").id
    adapter = _PlanAdapter([_final("好")])
    await _run(ctx, topic, adapter)

    starts = _events(ctx, "TURN_START")
    ends = _events(ctx, "TURN_END")
    assert len(starts) == 1 and len(ends) == 1, (len(starts), len(ends))
    turn_id = str(starts[0].data["turn_id"])
    data = ends[0].data

    missing = [key for key in TIMING_FIELDS if key not in data]
    assert not missing, f"契约 §3：TURN_END 缺耗时字段 {missing}；实际键={sorted(data)}"

    duration = data["duration_ms"]
    queue = data["queue_ms"]
    assert isinstance(duration, int) and duration > 0, duration
    assert isinstance(queue, int) and queue >= 0, queue

    started = _parse_iso(data["started_at"])
    ended = _parse_iso(data["ended_at"])
    assert ended >= started, (data["started_at"], data["ended_at"])
    wall_ms = int((ended - started).total_seconds() * 1000)
    assert abs(wall_ms - int(duration)) <= 2000, (wall_ms, duration)

    trace = ctx.trace_store.get(turn_id)
    assert trace is not None, "这一轮没有 trace 台账"
    assert abs(int(trace["duration_ms"]) - int(duration)) <= 5, (
        "TURN_END 的 duration_ms 必须与台账同源",
        trace["duration_ms"],
        duration,
    )
    notes = (trace.get("phases") or {}).get("notes") or {}
    assert "queue_wait_ms" in notes, f"阶段账本的 notes 必须记录排队等待：{notes}"
    assert abs(int(notes["queue_wait_ms"]) - int(queue)) <= 5, (notes["queue_wait_ms"], queue)


async def test_queued_turn_reports_queue_ms_but_not_inside_duration(tmp_path):
    """排队是用户等的时间，但不是执行时长；两者都要如实给出（契约 §3）。"""
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("排队耗时").id

    class _SlowFirst(_PlanAdapter):
        def __init__(self) -> None:
            super().__init__([])
            self.first = True

        async def complete(self, messages, tools, **kwargs):
            if self.first:
                self.first = False
                await asyncio.sleep(0.5)
            return _final("好")

    adapter = _SlowFirst()

    async def _fake_build(*args, **kwargs):
        return adapter

    ctx.build_adapter = _fake_build  # type: ignore[method-assign]
    await asyncio.gather(
        ctx.run_turn("第一条", topic_id=topic),
        ctx.run_turn("第二条（排队）", topic_id=topic),
    )

    ends = _events(ctx, "TURN_END")
    assert len(ends) == 2, [e.data for e in ends]
    queued = [e.data for e in ends if int(e.data.get("queue_ms") or 0) >= 300]
    assert len(queued) == 1, [(e.data.get("turn_id"), e.data.get("queue_ms")) for e in ends]
    assert int(queued[0]["duration_ms"]) < int(queued[0]["queue_ms"]), queued[0]


# ---- 2. 口径取证：并行分项求和 vs 总耗时（task-4 第 2 条） ------------------------


class _SleepTool(Tool):
    """并发安全工具：三个一起跑，墙钟 ≈ 单个耗时，逐个求和 ≈ 三倍。"""

    is_concurrency_safe = True

    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.description = "sleep"
        self.parameters = {"type": "object", "properties": {}}
        self.delay = delay

    async def run(self, **kwargs):
        await asyncio.sleep(self.delay)
        return ToolResult(ok=True, content="ok")


async def test_parallel_tools_are_not_summed_into_total(tmp_path):
    ctx = _app_ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("并行工具耗时").id
    for name in ("sleep_a", "sleep_b", "sleep_c"):
        ctx.registry.register(_SleepTool(name, 0.25))

    adapter = _PlanAdapter(
        [
            _tool_step([(f"c{i}", name, {}) for i, name in enumerate(("sleep_a", "sleep_b", "sleep_c"))]),
            _final("完成"),
        ]
    )
    await _run(ctx, topic, adapter)

    turn_id = ctx.trace_store.list(limit=1)[0]["turn_id"]
    trace = ctx.trace_store.get(turn_id)
    phases = trace["phases"]
    spans = phases["spans"]
    top = [span for span in spans if int(span.get("depth", 1)) == 1]
    top_sum = sum(int(span["ms"]) for span in top)
    all_spans_sum = sum(int(span["ms"]) for span in spans)
    duration = int(trace["duration_ms"])
    tool_runs_sum = sum(int(run["duration_ms"]) for run in (trace.get("tool_runs") or []))
    tool_wait = _span_ms(trace, "tool_wait")
    after_turn = sum(int(item.get("ms") or 0) for item in (phases.get("after_turn") or []))
    evidence = {
        "turn_id": turn_id,
        "duration_ms": duration,
        "top_level_sum_ms": top_sum,
        "all_spans_raw_sum_ms": all_spans_sum,
        "residual_ms": phases.get("residual_ms"),
        "tool_runs_sum_ms": tool_runs_sum,
        "tool_wait_wall_ms": tool_wait,
        "model_wait_ms": _span_ms(trace, "model_wait"),
        "queue_wait_ms": (phases.get("notes") or {}).get("queue_wait_ms"),
        "after_turn_ms": after_turn,
        "tool_run_count": len(trace.get("tool_runs") or []),
    }
    print("TIMING_EVIDENCE " + json.dumps(evidence, ensure_ascii=False, sort_keys=True))

    # 事实 1：三个并行工具各自 250ms，逐个求和 ≈ 750ms，但墙钟批次只有 ~250ms
    assert len(trace.get("tool_runs") or []) == 3, evidence
    assert tool_runs_sum >= 600, ("三个工具各自耗时应被如实记录", evidence)
    assert tool_wait < tool_runs_sum - 200, (
        "并行批次墙钟不得等于各工具耗时之和（否则就是把并行算成串行）",
        evidence,
    )
    # 事实 2：总耗时是墙钟，顶层阶段 + residual 必须铺满它，且不得超过
    assert top_sum <= duration + 5, ("顶层阶段合计不得大于总耗时", evidence)
    assert abs(top_sum + int(phases["residual_ms"]) - duration) <= 25, (
        "顶层阶段 + residual 必须铺满总耗时",
        evidence,
    )
    # 事实 3：raw span 求和会因为嵌套而大于顶层合计 —— 界面不得拿它当总耗时
    assert all_spans_sum >= top_sum, ("嵌套细分只会让原始合计更大", evidence)


# ---- 3. 缺失 != 0：明细缺失不影响总耗时（契约 §3） -------------------------------


def test_phases_shape_is_complete_and_residual_is_clamped(db_conn):
    """旧记录/部分数据也必须给出完整形状；residual 不能是负数。"""
    from agent.trace.store import TraceStore

    store = TraceStore(db_conn)
    store.begin("turn_clamp")
    store.conn.execute(
        "UPDATE turn_traces SET duration_ms = ? WHERE turn_id = ?", (100, "turn_clamp")
    )
    store.set_phases("turn_clamp", {"version": 1, "total_ms": 100, "sum_ms": 140, "spans": []})
    row = store.get("turn_clamp")
    phases = row["phases"]
    assert "residual_ms" in phases, phases
    assert int(phases["residual_ms"]) == 0, ("分项之和超过总耗时时只能归零，不能为负", phases)

    store.begin("turn_legacy")
    legacy = store.get("turn_legacy")
    assert legacy["phases"] is None or legacy["phases"] == {} or "residual_ms" in (
        legacy["phases"] or {}
    ), legacy
    assert legacy["duration_ms"] is None, ("旧记录没有总耗时时必须是 None，不能伪造 0", legacy)
