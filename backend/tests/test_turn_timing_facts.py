"""耗时事实（plan §3）：TURN_END 的 duration_ms / queue_ms / started_at / ended_at。

口径：

* core 侧用进程内单调钟测「执行窗口」，权威时长优先取 trace 台账的 duration_ms；
* queue_ms 是「受理 → 真正开跑」，是用户等的时间，不是执行时间；
* 拿不到台账时给**真实测量值**，不伪造 0、也不留 null。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from agent.core.turn import TurnManager
from agent.tools.base import Tool, ToolResult


class _LedgerStore:
    def __init__(self, row: dict | None = None, error: Exception | None = None) -> None:
        self.row = row
        self.error = error
        self.asked: list[str] = []

    def get(self, turn_id: str):
        self.asked.append(turn_id)
        if self.error is not None:
            raise self.error
        return self.row


class _LedgerTracer:
    def __init__(self, store: _LedgerStore) -> None:
        self.store = store


def _collector() -> tuple[list[tuple[str, dict]], object]:
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    return events, emitter


def _end_event(events: list[tuple[str, dict]]) -> dict:
    ends = [data for name, data in events if name == "TURN_END"]
    assert len(ends) == 1
    return ends[0]


async def test_turn_end_carries_real_timing_facts():
    events, emitter = _collector()

    async def runner(ctx):
        await asyncio.sleep(0.03)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["duration_ms"] >= 20  # 真实测得的执行窗口
    assert end["queue_ms"] >= 0
    assert end["started_at"] and end["ended_at"]
    assert datetime.fromisoformat(end["ended_at"]) >= datetime.fromisoformat(end["started_at"])


async def test_queue_ms_measures_waiting_not_execution():
    events, emitter = _collector()
    release = asyncio.Event()
    started: list[str] = []

    async def runner(ctx):
        started.append(ctx.turn_id)
        if len(started) == 1:
            await release.wait()

    manager = TurnManager(runner=runner, emitter=emitter)
    first = manager.submit("first")
    while not started:  # 第一轮真的开跑之后再提交第二个
        await asyncio.sleep(0.001)
    second = manager.submit("second")
    await asyncio.sleep(0.06)  # 第二个 turn 在队列里等着：这段时间就是 queue_ms
    release.set()
    await manager.wait(second.turn_id, timeout=3)
    await manager.shutdown()

    ends = [data for name, data in events if name == "TURN_END"]
    by_turn = {d["turn_id"]: d for d in ends}
    assert by_turn[second.turn_id]["queue_ms"] >= 50  # 排队等待被如实记下
    assert by_turn[first.turn_id]["queue_ms"] <= by_turn[second.turn_id]["queue_ms"]


async def test_trace_ledger_duration_is_authoritative():
    events, emitter = _collector()
    ledger = _LedgerStore(
        {
            "duration_ms": 1234,
            "started_at": "2026-10-06T08:00:00+00:00",
            "ended_at": "2026-10-06T08:00:01.234000+00:00",
        }
    )

    async def runner(ctx):
        ctx.trace = _LedgerTracer(ledger)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["duration_ms"] == 1234  # 台账是权威总时长
    assert end["started_at"] == "2026-10-06T08:00:00+00:00"
    assert end["ended_at"] == "2026-10-06T08:00:01.234000+00:00"
    assert ledger.asked == [ctx.turn_id]


async def test_ledger_failure_falls_back_to_measured_duration():
    events, emitter = _collector()

    async def runner(ctx):
        ctx.trace = _LedgerTracer(_LedgerStore(error=RuntimeError("ledger down")))
        await asyncio.sleep(0.02)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)  # 台账坏了也必须照常发 TURN_END
    assert end["duration_ms"] >= 10
    assert end["started_at"] and end["ended_at"]


# ---- 结束事实（plan §1.2）：reason_code / reason / stopped_by / actions --------


async def test_completed_turn_reports_none_reason_and_no_actions():
    events, emitter = _collector()

    async def runner(ctx):
        ctx.result = {
            "ok": True,
            "turn": {"stop_reason_code": "none", "stop_reason": None, "stopped_by": None},
        }

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["reason_code"] == "none"
    assert end["reason"] is None  # 正常跑完就不编一个理由
    assert end["stopped_by"] is None
    assert end["actions"] == []


async def test_completed_turn_carries_the_loop_stop_facts():
    """预算 / 无进展 / 护栏停下但整轮仍算完成：原因如实带出，且不给会再次失败的按钮。"""
    events, emitter = _collector()

    async def runner(ctx):
        ctx.result = {
            "ok": True,
            "turn": {
                "stop_reason_code": "budget",
                "stop_reason": "迭代次数达到上限（8/8）",
                "stopped_by": "system",
            },
        }

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "completed"
    assert end["reason_code"] == "budget"
    assert "迭代次数达到上限" in end["reason"]
    assert end["stopped_by"] == "system"
    assert end["actions"] == []  # 重发同样的请求会再次停下 → 不给按钮


async def test_provider_failure_is_a_provider_error_with_retry():
    from agent.adapters.errors import NetworkError

    events, emitter = _collector()

    async def runner(ctx):
        raise NetworkError("连接断了")

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "failed"
    assert end["reason_code"] == "provider_error"
    assert "连接断了" in end["reason"]
    assert end["stopped_by"] == "system"
    assert end["actions"] == ["retry"]


async def test_internal_failure_is_reported_as_internal_error():
    events, emitter = _collector()

    async def runner(ctx):
        raise ValueError("某个内部假设不成立")

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["reason_code"] == "internal_error"  # 不是供应商错误就不冒充
    assert "某个内部假设不成立" in end["reason"]
    assert end["stopped_by"] == "system"
    assert end["actions"] == ["retry"]


async def test_user_stop_is_reported_as_user_stopped():
    events, emitter = _collector()
    release = asyncio.Event()

    async def runner(ctx):
        await release.wait()

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    while manager.active is None:
        await asyncio.sleep(0.005)
    assert manager.cancel_active() is True
    release.set()
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "cancelled"
    assert end["reason_code"] == "user_stopped"
    assert end["stopped_by"] == "user"
    # 用户停止也必须给出人话原因（plan §1.2：除 none 外都要有事实原因），
    # 不能只重复状态词、更不能让前端拿到 null 而只能显示「已停止」。
    assert isinstance(end["reason"], str) and end["reason"].strip()
    assert "停止" in end["reason"]
    assert len(end["reason"]) <= 200
    assert end["actions"] == ["resend"]


async def test_shutdown_interruption_is_not_a_user_stop():
    events, emitter = _collector()
    running = asyncio.Event()

    async def runner(ctx):
        running.set()
        await asyncio.sleep(30)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await running.wait()
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "cancelled"
    assert end["reason_code"] == "interrupted"  # 进程掐断 ≠ 用户按的停止
    assert end["stopped_by"] == "system"
    assert isinstance(end["reason"], str) and end["reason"].strip()
    assert "中断" in end["reason"]
    assert end["actions"] == ["resend"]


async def test_missing_credential_turn_is_reported_honestly():
    events, emitter = _collector()

    async def runner(ctx):
        ctx.status = "unavailable"
        ctx.error = "no_credential"
        ctx.result = {"ok": False, "reason": "no_credential"}

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "unavailable"
    assert end["reason_code"] == "credential_unavailable"
    assert "凭据" in end["reason"]
    assert end["actions"] == []  # 真正的入口是「设置 → 凭据」，写在 reason 里


async def test_turn_end_reason_is_redacted():
    """新增输出路径必须过 redact：原因里不得出现密钥原文。"""
    from agent.trace import redact

    secret = "sk-live-turn-end-secret-0001"
    redact.register_secret(secret)
    try:
        events, emitter = _collector()

        async def runner(ctx):
            raise RuntimeError(f"upstream 400: {secret}")

        manager = TurnManager(runner=runner, emitter=emitter)
        ctx = manager.submit("hi")
        await manager.wait(ctx.turn_id, timeout=3)
        await manager.shutdown()

        end = _end_event(events)
        assert end["reason"]
        assert secret not in end["reason"]
    finally:
        redact.clear_registered_secrets()


def test_provider_error_names_cover_the_adapter_taxonomy():
    """类名表必须覆盖 adapters/errors.py 的全部归一化错误，否则失败会被误判。"""
    from agent.adapters import errors as adapter_errors
    from agent.core.turn import PROVIDER_ERROR_NAMES

    declared = {
        name
        for name, obj in vars(adapter_errors).items()
        if isinstance(obj, type) and issubclass(obj, adapter_errors.ProviderError)
    }
    assert declared
    assert declared <= set(PROVIDER_ERROR_NAMES)


async def test_unexpected_exception_from_the_model_call_is_internal_error():
    """未归一化的意外异常 = **内部故障**，不得冒充厂商故障（Lead 裁决 2026-10-06）。

    分类只看异常类名（系统事实）：ProviderError 家族 → provider_error；
    其它异常 → internal_error，reason 如实带异常类名与原文。
    """

    class _BugAdapter:
        mode = "text"
        model = "bug"
        supports_stream = False

        async def complete(self, messages, tools, **kwargs):
            raise RuntimeError("验证用的意外内部错误")

    events, emitter = _collector()

    async def runner(ctx):
        from agent.api.bus import EventBus
        from agent.core.loop import AgentLoop
        from agent.tools.registry import ToolRegistry

        loop = AgentLoop(_BugAdapter(), ToolRegistry(), EventBus(), turn_id=ctx.turn_id)
        ctx.loop = loop
        try:
            await loop.run("hi")
        finally:
            ctx.loop = None

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "failed"
    assert end["reason_code"] == "internal_error"  # 不是 provider_error
    assert "RuntimeError" in end["reason"]  # 系统事实：异常类名
    assert "验证用的意外内部错误" in end["reason"]
    assert end["stopped_by"] == "system"
    assert end["actions"] == ["retry"]


async def test_normalized_provider_error_from_the_model_call_is_provider_error():
    """对照：适配器**归一化过**的供应商错误才是 provider_error。"""
    from agent.adapters.errors import ProviderInternalError

    class _ProviderBoomAdapter:
        mode = "text"
        model = "fake-boom"
        supports_stream = False

        async def complete(self, messages, tools, **kwargs):
            raise ProviderInternalError("厂商返回 500：上游错误")

    events, emitter = _collector()

    async def runner(ctx):
        from agent.api.bus import EventBus
        from agent.core.loop import AgentLoop
        from agent.tools.registry import ToolRegistry

        loop = AgentLoop(
            _ProviderBoomAdapter(), ToolRegistry(), EventBus(), turn_id=ctx.turn_id
        )
        ctx.loop = loop
        try:
            await loop.run("hi")
        finally:
            ctx.loop = None

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["status"] == "failed"
    assert end["reason_code"] == "provider_error"
    assert "厂商返回 500" in end["reason"]
    assert end["stopped_by"] == "system"
    assert end["actions"] == ["retry"]


# ---- 服务层：TURN_END 出口用台账覆盖 core 的值 -------------------------------


def _app_ctx(tmp_path):
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "timing.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


async def test_app_turn_end_prefers_trace_ledger(tmp_path):
    ctx = _app_ctx(tmp_path)
    ctx.trace_store.begin("turn_9")
    ctx.trace_store.finish("turn_9", "completed")
    ledger_row = ctx.trace_store.get("turn_9")
    assert ledger_row is not None and ledger_row["duration_ms"] is not None

    await ctx._publish_turn_event(
        "TURN_END",
        {"turn_id": "turn_9", "status": "completed", "duration_ms": 5, "queue_ms": 7},
    )
    data = ctx.bus._history[-1].data
    assert data["duration_ms"] == ledger_row["duration_ms"]
    assert data["queue_ms"] == 7  # core 的值被保留（台账里没有这一项）
    assert data["started_at"] == ledger_row["started_at"]
    assert data["ended_at"] == ledger_row["ended_at"]


async def test_app_turn_end_without_ledger_keeps_core_values(tmp_path):
    ctx = _app_ctx(tmp_path)
    await ctx._publish_turn_event(
        "TURN_END",
        {"turn_id": "turn_missing", "status": "failed", "duration_ms": 42, "queue_ms": 3},
    )
    data = ctx.bus._history[-1].data
    assert data["duration_ms"] == 42  # 台账没有这一行：不覆盖成因 null / 0
    assert data["queue_ms"] == 3

# ---- 可恢复工具错误 ≠ 整轮失败（plan §1.2）-----------------------------------


class _BoomTool(Tool):
    name = "boom"
    description = "总是失败（可恢复）"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(
            ok=False, error="boom: 打不开这个文件", category="io", recoverable=True
        )


async def test_recoverable_tool_error_is_not_a_turn_failure():
    """一次可恢复的工具错误不等于整轮失败：完成就是完成，原因仍是 none。"""
    from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
    from agent.api.bus import EventBus
    from agent.core.loop import AgentLoop
    from agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_BoomTool())
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                text="我先试一下这个文件。",
                tool_calls=[ScriptedToolCall(id="c1", name="boom", arguments={})],
            ),
            StreamScript(text="工具失败了，我换个办法。"),  # 工作调用收尾
            StreamScript(text="这个文件打不开，我换个办法：这是最终回答。"),  # 回答调用
        ]
    )
    loop = AgentLoop(adapter, registry, EventBus(), turn_id="turn_1")
    result = await loop.run("hi")

    assert result.tool_calls_made == 1
    assert result.cancelled is False
    assert result.phase.value == "done"
    assert result.stop_reason_code == "none"  # 工具失败没有变成整轮失败
    assert result.stopped_by is None
    assert "这是最终回答" in (result.final_content or "")
