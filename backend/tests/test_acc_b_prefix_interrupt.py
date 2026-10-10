"""B 独立验证：未声明/未判定前缀在中断时不得吞掉已收到文字（审计 F19）。

**反例（基线应为红）**：模型正文先到达一段仍可能是控制声明前缀的短文本
（如 [[QIO:AN），随后 EOF / 异常 / 取消。旧实现 _start_undeclared 会先清空
_probe 再把它当正文喂回去，结果是这段已收到文字被静默丢弃。

验证层级：
1. _AssistantStream 单元级——逐个前缀切分位置验证「不丢字」；
2. 真实本地 HTTP/SSE 假厂商 → 真 NativeAdapter → AgentLoop——中断时正文保留。

运行：cd backend; uv run --frozen pytest tests/test_acc_b_prefix_interrupt.py -q
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop, _AssistantStream
from agent.prompts import ANSWER_MARKER
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

from _acc_b_provider import AccSseProvider

DECL = "[[QIO:ANSWER]]"


def _collector():
    events: list[dict] = []

    async def emit(payload: dict) -> None:
        events.append(payload)

    return events, emit


# ---- 1. 逐个切分前缀位置：中断不丢字 ----------------------------------------------


@pytest.mark.parametrize("split", list(range(1, len(ANSWER_MARKER))))
async def test_strict_prefix_survives_an_interrupt(split):
    prefix = ANSWER_MARKER[:split]
    events, emit = _collector()
    stream = _AssistantStream(emit, delta_id="dl_prefix")
    await stream.note_text(prefix)
    assert events == [], "未判定前不得展示（也不得丢字）"
    await stream.finish(None)  # EOF / 异常 / 取消：completion=None

    delivered = "".join(str(e.get("content") or "") for e in events)
    assert prefix in delivered, (split, prefix, events)
    assert all(e["interim"] is True for e in events), events  # 未判定 → 过程区，不猜角色
    assert all(e["streaming"] is False for e in events)


async def test_strict_prefix_split_across_two_chunks_survives():
    events, emit = _collector()
    stream = _AssistantStream(emit, delta_id="dl_prefix2")
    await stream.note_text("[[Q")
    await stream.note_text("IO:AN")
    await stream.finish(None)
    delivered = "".join(str(e.get("content") or "") for e in events)
    assert delivered == "[[QIO:AN", events


# ---- 2. 完整控制声明不得泄露为正文 ------------------------------------------------


@pytest.mark.parametrize("tail", ["", "\r"])
async def test_complete_declaration_at_eof_is_not_body(tail):
    events, emit = _collector()
    stream = _AssistantStream(emit, delta_id="dl_decl")
    await stream.note_text(ANSWER_MARKER + tail)
    await stream.finish(
        Completion(message=ChatMessage(role="assistant", content=ANSWER_MARKER + tail))
    )
    joined = "".join(str(e.get("content") or "") for e in events)
    assert ANSWER_MARKER not in joined, ("控制声明泄露为正文", events)
    assert stream.answer_text == "", stream.answer_text
    assert stream.role == "answer", stream.role  # 完整声明 = 回答调用，只是正文为空


# ---- 3. 相似自然语言 / 工具路径 --------------------------------------------------


async def test_similar_natural_language_is_kept_verbatim():
    text = "[[QIO 是一个标记示例，不是控制声明。"
    events, emit = _collector()
    stream = _AssistantStream(emit, delta_id="dl_nat")
    await stream.note_text(text)
    await stream.finish(Completion(message=ChatMessage(role="assistant", content=text)))
    joined = "".join(str(e.get("content") or "") for e in events)
    assert text in joined, (text, events)
    assert stream.undeclared_answer_used is True


async def test_prefix_then_tool_call_is_released_to_process_area():
    events, emit = _collector()
    stream = _AssistantStream(emit, delta_id="dl_tool")
    await stream.note_text("[[QIO")
    await stream.note_tool_call()
    assert events, "工具调用出现后缓冲正文必须放行"
    assert events[-1]["interim"] is True
    assert "[[QIO" in str(events[-1]["content"])


# ---- 4. 真实 HTTP/SSE：前缀阶段中断 -------------------------------------------------


class _EchoTool(Tool):
    name = "echo"
    description = "echo（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def run(self, **kwargs):
        self.seen.append(dict(kwargs))
        return ToolResult(ok=True, content="echo")


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
            NativeAdapter(client=client, model="acc-b-native"), registry, bus, turn_id="turn_accb_pfx"
        )
        yield loop, bus
    finally:
        await client.close()


def _events(bus: EventBus, name: str) -> list[dict]:
    return [e.data for e in bus._history if e.type.value == name]


async def test_interrupted_prefix_over_real_sse_is_preserved(provider):
    registry = ToolRegistry()
    registry.register(_EchoTool())
    async with _native_loop(
        provider, [{"chunks": ["[[QIO:AN"], "abort": True}], registry
    ) as (loop, bus):
        result = await loop.run("回答我")

    assert result.final_content == "[[QIO:AN", (
        "中断时已收到文字被清空",
        result.final_content,
    )
    assistant = _events(bus, "ASSISTANT")
    assert any("[[QIO:AN" in str(e.get("content") or "") for e in assistant), assistant


async def test_prefix_interrupted_by_transport_error_is_preserved(provider):
    """传输错误走 finish(None)（没有 completion 可回填）：已收到前缀绝不能被清空。"""
    from agent.adapters.errors import ProviderError

    registry = ToolRegistry()
    registry.register(_EchoTool())
    async with _native_loop(
        provider, [{"chunks": ["[[QIO:AN"], "hard_abort": True}], registry
    ) as (loop, bus):
        with pytest.raises(ProviderError):
            await loop.run("回答我")

    assistant = _events(bus, "ASSISTANT")
    assert any("[[QIO:AN" in str(e.get("content") or "") for e in assistant), (
        "中断时仍在控制前缀阶段的已收到文字被清空",
        assistant,
    )


async def test_prefix_then_legitimate_stop_is_delivered_as_body(provider):
    registry = ToolRegistry()
    registry.register(_EchoTool())
    async with _native_loop(
        provider, [{"chunks": ["[[QIO:ANSWER"], "finish": "stop"}], registry
    ) as (loop, _bus):
        result = await loop.run("回答我")

    # 合法结束但声明不完整：按未声明正文一次性交付（不丢字、不冒充声明）
    assert result.final_content == "[[QIO:ANSWER", result.final_content
    assert result.stop_reason_code == "none", result.stop_reason_code
