"""独立验证：真实流式 + 内容角色协议（第五轮契约 §1.1）。

契约来源：docs/plans/2026-10-07-final-convergence.md §1.1（内容角色协议）。
验证方式：**不走实现方的内部形状猜测**，而是用本地假厂商端点（真 HTTP + 真 SSE）
喂给真实的 NativeAdapter + AgentLoop，断言事件层与最终文本的契约。

假厂商端点：scripts/verify_stream_provider.py（本地扮演，不联网、不需要真实 Key）。
它只证明「QIO 自己的链路对」，不证明任何真实厂商的兼容性。

第五轮口径：角色**只由模型在正文开头的声明决定**（[[QIO:ANSWER]]，大小写不敏感，
声明本身不展示）：声明匹配 → 该调用是回答调用，正文从第一个可发布增量起就是
正式回答（interim=false、streaming=true），结束时同 delta_id 再补一条
{streaming:false} 做收尾校准；前缀不匹配 + 调用结束有工具调用 → 工作调用
（正文进过程区）；未声明且调用结束无工具调用 → 一次性交付到回答区（降级路径）。
本文件断言：正式回答在 provider 结束前就出现在回答区、按节奏合并发布、
同一份文字不重复、工具参数碎片不当正文。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_streaming_contract_verify.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import threading
import time
from pathlib import Path

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]

# 冻结协议串（测试里写死字面量：契约改了就应当红）
ANSWER_MARKER = "[[QIO:ANSWER]]"


# ---- 假厂商端点（与 scripts/verify_stream_provider.py 同源，避免两份实现漂移） ----


def _load_provider_module():
    path = REPO_ROOT / "scripts" / "verify_stream_provider.py"
    assert path.exists(), f"验证资产缺失：{path}"
    spec = importlib.util.spec_from_file_location("verify_stream_provider", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def provider():
    module = _load_provider_module()
    server = module.StreamingProvider(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _native_adapter(server):
    from openai import AsyncOpenAI

    from agent.adapters.native import NativeAdapter

    client = AsyncOpenAI(
        base_url="http://127.0.0.1:%d/v1" % server.server_port,
        api_key="sk-verify-fake-0001",
    )
    return NativeAdapter(client=client, model="verify-model")


class _RecordingEcho(Tool):
    name = "echo"
    description = "echo（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def run(self, **kwargs):
        self.seen.append(dict(kwargs))
        return ToolResult(ok=True, content="echo:%s" % kwargs.get("text", ""))


def _registry() -> tuple[ToolRegistry, _RecordingEcho]:
    registry = ToolRegistry()
    tool = _RecordingEcho()
    registry.register(tool)
    return registry, tool


def _assistant_events(loop: AgentLoop, *, interim: bool | None = None) -> list:
    out = []
    for event in loop.bus._history:
        if event.type.value != "ASSISTANT":
            continue
        if interim is None or bool(event.data.get("interim")) is interim:
            out.append(event)
    return out


def _timeline_events(server) -> list[str]:
    return [str(item["event"]) for item in server.timeline]


# ---- 1. 契约 §2.2：分层改造的公开面 ----------------------------------------------


async def _resolved(value):
    return await value if inspect.isawaitable(value) else value


async def test_base_adapter_declares_stream_surface():
    """契约 §2.2：supports_stream 默认 False，stream() 默认抛 NotImplementedError（不假装流式）。"""
    from agent.adapters.base import BaseAdapter

    class _Minimal(BaseAdapter):
        mode = "native"

        async def complete(self, messages, tools, **kwargs):  # pragma: no cover - 不会被调用
            raise AssertionError("complete 不该在这条用例里被调用")

    instance = _Minimal()
    declared = await _resolved(getattr(instance, "supports_stream", False))
    assert declared is False, "默认必须声明不支持流式，让 loop 走整段降级"

    stream_method = getattr(instance, "stream", None)
    assert stream_method is not None, "契约 §2.2：BaseAdapter 必须提供 stream()"
    outcome = "no-error"
    try:
        result = stream_method([], [])
        if inspect.isasyncgen(result):
            async for _item in result:
                break
        else:
            await _resolved(result)
    except NotImplementedError:
        outcome = "not-implemented"
    assert outcome == "not-implemented", "默认 stream() 必须明确抛 NotImplementedError"


async def test_stream_delta_carries_a_text_fragment():
    """契约 §2.2 明列新增 StreamDelta：正文增量必须能表达「一段文字」。

    实现形状（A 最终版，Lead 已确认）：StreamDelta(kind=..., text=...)，kind ∈
    {text, tool_call, done}；这里按这个形状直接构造，不再靠 **kwargs 猜参数。
    """
    from agent.adapters.base import StreamDelta

    text_delta = StreamDelta(kind="text", text="一段文字")
    assert text_delta.kind == "text"
    assert text_delta.text == "一段文字"

    # 工具调用增量只用于「这条响应是工具轮」的判定：碎片参数不在这里暴露
    tool_delta = StreamDelta(kind="tool_call", text="", call_id="call_1", name="echo")
    assert tool_delta.kind == "tool_call"
    assert tool_delta.call_id == "call_1"

    # 结束段携带组装完成的整段结果（唯一可以交给工具执行的入口）
    done = StreamDelta(kind="done")
    assert done.kind == "done"


# ---- 2. 契约 §2.1：provider 还没结束，回答已经到达（真 SSE 路径） -----------------


async def test_answer_streams_into_answer_area_before_provider_finishes(provider):
    """第五轮契约 §1.1：正文声明了回答 → **从第一个可发布增量起**就在正式回答区
    （interim=false、streaming=true），且早于 provider 结束；标记本身不展示。

    强度保持：provider 结束前已可见 + 按节奏合并发布 + 同一份文字不重复。
    """
    chunks = [f"第{i:02d}段。" for i in range(1, 21)]
    # 10ms 一片：分片到达比发布节奏（40ms）快 —— 合并发布必须发生，否则就是逐片推送。
    provider.script.set(
        [
            # ① 工作轮：正文是过程说明，并且真的调用一次工具
            {
                "chunks": ["我先看一下。"],
                "tool_chunks": [
                    {
                        "id": "call_live",
                        "name": "echo",
                        "args_fragments": ['{"text": "hi"}'],
                    }
                ],
                "chunk_delay_ms": 5,
            },
            # ② 声明回答：真流式（声明单独一片，正文按节奏合并）
            {"chunks": [ANSWER_MARKER + "\n", *chunks], "chunk_delay_ms": 10},
        ]
    )

    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_stream_verify")
    task = asyncio.create_task(loop.run("请分多次回答"))

    early_event = None
    early_timeline: list[str] = []
    still_streaming = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not task.done():
        for event in _assistant_events(loop, interim=False):
            if str(event.data.get("content") or "").strip():
                early_event = event
                # 钉住「这一刻」：provider 还没结束、时间线还没到 stream_end
                still_streaming = not task.done()
                early_timeline = _timeline_events(provider)
                break
        if early_event is not None:
            break
        await asyncio.sleep(0.01)

    result = await asyncio.wait_for(task, timeout=20)

    assert early_event is not None, (
        "契约 §1.1：正式回答必须在 provider 结束前就出现在回答区"
    )
    assert still_streaming, "不能等整段响应结束才展示正式回答"
    # 工作调用的流已经结束（它是上一个请求），但**回答调用还在流**：
    # 时间线最后一条必须是 chunk_sent，而不是回答调用的 stream_end。
    assert early_timeline and early_timeline[-1] == "chunk_sent", (
        "正式回答是在 provider 已经发完之后才出现的（假流式）：时间线=%r" % early_timeline
    )
    # 首次展示必须是**流式增量**：收尾校准不是首次展示来源
    assert early_event.data.get("streaming") is True, (
        "回答调用的第一个回答区事件必须是 streaming=true 的增量（不是收尾校准）",
        early_event.data,
    )
    assert early_event.data.get("role_evidence") == "declared_answer", early_event.data

    events = _assistant_events(loop)
    answer = [e for e in events if e.data.get("interim") is False]
    work = [e for e in events if e.data.get("interim") is True]
    contents = [str(event.data.get("content") or "") for event in answer]
    full = "".join(chunks)

    assert work, "工作调用的正文必须进过程区（interim=true）"
    assert contents, "回答调用必须推送 ASSISTANT 增量"
    assert result.final_content == full, (result.final_content, full)
    assert contents[-1] == full, ("最后一段必须是权威全文（累计快照）", contents[-1], full)
    for earlier, later in zip(contents, contents[1:]):
        assert later.startswith(earlier), ("累计快照不得回退/覆盖", earlier, later)
    assert len(contents) >= 2, ("真流式至少要推送两次", contents)
    assert len(contents) < len(chunks), (
        "按节奏合并发布，不能逐片推送",
        len(contents),
        len(chunks),
    )
    # 工作调用的文字永远不进正式回答区（不搬动、不重复）
    assert all("我先看一下。" not in c for c in contents), contents

    delta_ids = {str(event.data.get("delta_id") or "") for event in answer}
    assert len(delta_ids) == 1 and "" not in delta_ids, (
        "回答调用 = 一条流式消息，delta_id 必须稳定",
        delta_ids,
    )
    seqs = [int(event.data["seq"]) for event in answer]
    assert seqs == sorted(seqs), ("seq 必须单调", seqs)
    assert len(set(seqs)) == len(seqs), ("seq 必须唯一（前端据此丢重复）", seqs)
    # 增量必须是 streaming=true。最后一条是 streaming=false 的**收尾校准**
    # （停打字机、同一份文字）：同一个 delta_id、只多一条，且绝不能改内容。
    streamed = [event for event in answer if event.data.get("streaming") is True]
    settle = [event for event in answer if event.data.get("streaming") is not True]
    assert streamed, ("回答调用必须有 streaming=true 的增量", [e.data for e in answer])
    assert len(settle) == 1, ("回答调用结束时必须只有一条 streaming=false 收尾快照", [e.data for e in settle])
    assert answer[-1] is settle[0], ("收尾快照必须是最后一条", [e.data for e in answer])
    assert settle[0].data.get("delta_id") == streamed[-1].data.get("delta_id"), (
        "收尾快照必须属于同一个 delta_id",
        settle[0].data,
    )
    assert str(settle[0].data.get("content") or "") == full, (
        "收尾快照必须交付已确认全文（且不改内容）",
        settle[0].data.get("content"),
        full,
    )


async def test_tool_arguments_split_into_fragments_are_assembled_before_execution(provider):
    """契约 §2.1 规则 4：参数碎片只在 adapter 内组装，攒成合法 JSON 才执行。"""
    provider.script.set(
        [
            {
                "tool_chunks": [
                    {
                        "id": "call_v1",
                        "name": "echo",
                        "args_fragments": ['{"te', 'xt": "', '碎片组装"}'],
                    }
                ],
                "chunk_delay_ms": 5,
            },
            # ② 声明回答（工具轮之后）
            {"chunks": [ANSWER_MARKER + "\n收到"]},
        ]
    )

    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_tool_fragments")
    result = await asyncio.wait_for(loop.run("调一次工具"), timeout=20)

    assert tool.seen == [{"text": "碎片组装"}], ("工具必须收到组装完成的参数", tool.seen)
    assert result.final_content == "收到", result.final_content

    starts = [e for e in loop.bus._history if e.type.value == "TOOL_START"]
    assert starts and str(starts[0].data.get("call_id")) == "call_v1", [e.data for e in starts]

    for event in _assistant_events(loop):
        content = str(event.data.get("content") or "")
        assert '{"te' not in content and "args_fragments" not in content, (
            "工具参数碎片绝不能当正文展示",
            content,
        )


async def test_stream_that_ends_without_finish_reason_keeps_confirmed_text(provider):
    """断线保留已确认文本，且没有发出来的后缀不得凭空出现。

    第五轮起未声明的正文在断线时一次性交付到正式回答区（降级路径）：它是这一轮
    唯一拿到的文字，不能丢、不能凭空补后缀、也不能重新生成一遍。
    """
    confirmed = "前两句。第二句。"
    provider.script.set(
        [
            # ① 一条流：发两片后断线（没有 finish_reason）
            {"abort_after": 2, "chunks": ["前两句。", "第二句。", "永远不会发出的后缀"]},
        ]
    )

    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_stream_abort")
    result = await asyncio.wait_for(loop.run("请回答"), timeout=20)

    text = result.final_content or ""
    assert text == confirmed, ("断线保留已确认文本（不丢字、不补后缀）", text)

    events = _assistant_events(loop)
    delivered = "".join(
        str(e.data.get("content") or "") for e in events if e.data.get("interim") is False
    )
    assert "永远不会发出的后缀" not in delivered, ("未确认的内容不得出现", delivered)
    assert confirmed in delivered, ("已确认文本必须交付", delivered)
    assert delivered.count("前两句。") == 1, ("已确认文本不得重复", delivered)

    # 累计快照按 delta_id 各自单调（工作调用与回答调用是两条流，不能跨流比较）
    by_delta: dict[str, list[str]] = {}
    for event in events:
        by_delta.setdefault(str(event.data.get("delta_id") or ""), []).append(
            str(event.data.get("content") or "")
        )
    for delta_id, contents in by_delta.items():
        for earlier, later in zip(contents, contents[1:]):
            assert later.startswith(earlier), (
                "断流也不能回退已发布内容",
                delta_id,
                earlier,
                later,
            )


async def test_text_compatible_path_never_claims_streaming():
    """契约 §2.2 / §2.8：不支持流式的路径一次性给全文，明确 streaming=false。"""

    class _TextLike:
        mode = "text"
        model = "fake-text-verify"
        supports_stream = False

        async def complete(self, messages, tools, **kwargs):
            return Completion(message=ChatMessage(role="assistant", content="兼容档回答"))

    registry, _tool = _registry()
    loop = AgentLoop(_TextLike(), registry, EventBus(), turn_id="turn_text_verify")
    result = await asyncio.wait_for(loop.run("回答我"), timeout=20)

    assert result.final_content == "兼容档回答", result.final_content
    events = _assistant_events(loop)
    assert events, "契约 §2.8：不支持流式的路径要一次性给全文（streaming=false），不能什么都不发"
    assert not any(event.data.get("streaming") for event in events), (
        "text 档不得假装流式", [event.data for event in events]
    )
    assert str(events[-1].data.get("content") or "") == "兼容档回答", [e.data for e in events]
