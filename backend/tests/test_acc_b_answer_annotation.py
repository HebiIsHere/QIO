"""B 独立验证：系统核对注释与正式回答正文分层（审计 F11 后端）。

**反例（基线应为红）**：主循环把系统核对注释拼进 TURN_END.final_content（正文 +
注释），前端因「全文不等」再追加一条包含完整正文的重复回答。

修复后的契约（后端出口）：
* TURN_END.final_content = **纯正文**（模型已确认正文，不含系统注释）；
* 系统核对注释走独立追加字段 TURN_END.annotation（只追加，不改旧语义）；
* ASSISTANT 事件链里正文唯一，注释不出现在任何正文事件里；
* 落库 raw 同时带注释，刷新后仍可恢复。

验证层级：真实本地 HTTP/SSE 假厂商 → 真 NativeAdapter → AgentLoop → 事件 +
TurnManager → TURN_END。

运行：cd backend; uv run --frozen pytest tests/test_acc_b_answer_annotation.py -q
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.core.turn import TurnManager
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

from _acc_b_provider import AccSseProvider

DECL = "[[QIO:ANSWER]]"


class _FailingTool(Tool):
    name = "acc_b_failing_tool"
    description = "永远失败的工具（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    is_concurrency_safe = True

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="这一步失败了，但可以重试", category="verify")


@pytest.fixture()
def provider():
    server = AccSseProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@contextlib.asynccontextmanager
async def _native_loop(server: AccSseProvider, steps: list[dict], registry: ToolRegistry):
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    server.set(steps)
    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-acc-b-fake-0001",
        timeout=15.0,
    )
    try:
        bus = EventBus()
        loop = AgentLoop(
            NativeAdapter(client=client, model="acc-b-native"), registry, bus, turn_id="turn_accb_ann"
        )
        yield loop, bus
    finally:
        await client.close()


def _answer_events(bus: EventBus) -> list[dict]:
    return [
        e.data
        for e in bus._history
        if e.type.value == "ASSISTANT" and e.data.get("interim") is False
    ]


_STEPS = [
    {
        "tool_chunks": [
            {"id": "c_fail", "name": "acc_b_failing_tool", "args_fragments": ['{"text": "x"}']}
        ]
    },
    {"chunks": [DECL + "\n", "这是正式回答正文。"]},
]


async def test_body_stays_clean_and_annotation_is_separate(provider):
    registry = ToolRegistry()
    registry.register(_FailingTool())
    async with _native_loop(provider, _STEPS, registry) as (loop, bus):
        result = await loop.run("调用一个会失败的工具")

    assert result.final_content == "这是正式回答正文。", result.final_content
    assert result.final_annotation, "系统核对注释必须单独携带，而不是拼进 final_content"
    assert "系统核对" in result.final_annotation
    assert "acc_b_failing_tool" in result.final_annotation

    answers = _answer_events(bus)
    assert answers, "必须有正式回答事件"
    assert all("系统核对" not in str(e.get("content") or "") for e in answers), answers
    assert all(str(e.get("content") or "") == "这是正式回答正文。" for e in answers), answers


async def test_turn_end_carries_annotation_separately(provider):
    registry = ToolRegistry()
    registry.register(_FailingTool())
    async with _native_loop(provider, _STEPS, registry) as (loop, _bus):
        result = await loop.run("调用一个会失败的工具")

    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:
        ctx.final_content = result.final_content
        ctx.result = {"ok": True, "turn": result.__dict__}

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("调用一个会失败的工具")
    await manager.wait(ctx.turn_id, timeout=5)
    await manager.shutdown()
    ends = [data for name, data in events if name == "TURN_END"]
    assert ends, "没有发出 TURN_END"
    end = ends[-1]
    assert end["final_content"] == "这是正式回答正文。", end.get("final_content")
    assert "annotation" in end, ("TURN_END 必须单独携带系统核对注释", sorted(end))
    assert end["annotation"] == result.final_annotation
    assert end["annotation"].startswith("—— 系统核对"), end["annotation"]


async def test_persistence_raw_carries_the_annotation(provider):
    from agent.services.turn_orchestrator import verification_raw

    registry = ToolRegistry()
    registry.register(_FailingTool())
    async with _native_loop(provider, _STEPS, registry) as (loop, _bus):
        result = await loop.run("调用一个会失败的工具")

    raw = verification_raw(result)
    assert raw is not None
    assert raw.get("annotation") == result.final_annotation
