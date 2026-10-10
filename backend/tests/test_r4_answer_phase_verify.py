r"""D 独立验证（问题一：回答阶段协议 / 内容角色）—— **R5 新协议**（plan §1.1）。

**旧语义（本文件 round4 的写法）已被 plan §1.1 取代**：以前是「工作调用（带工具）的正文一律进过程区，
调用结束没有工具调用时再发一次 tools=[] 的回答调用**重写一遍**」——同一个答案会生成两次、
过程区与回答区各显示一份。现在冻结的协议：

* 模型给最终回答时正文**以 [[QIO:ANSWER]] 开头**（大小写不敏感，声明本身**不展示**）；
  声明之后的正文从**第一个可发布增量**起就以 interim=false, streaming=true 进正式回答区（真流式）；
* 未声明：正文先有界缓冲（≤64 KB）；结束**有工具调用** → 放行到过程区；**无工具调用** →
  **一次性**交付正式回答区（不重新生成、不搬动、过程区不留副本）；
* tools=[] 的额外调用**只**在「整轮完全没有回答内容」时兜底最多一次；
* 声明之后的迟到工具调用：不执行 + 可见 WARNING；非法/拆坏/重复声明按「未声明」处理。

本文件钉回答阶段的**时序与角色**（内容协议完整性见 tests/test_r5_answer_duplication_verify.py）：
①声明后正文在调用结束前进正式回答区；②首个回答事件是流式增量、结束快照只做校准；
③过程区不留完整答案副本、文字永不从回答区搬回；④同一答案不生成两次（调用台账）；
⑤工具轮说明留过程区；⑥取消保留已显示文字；⑦厂商错误如实抛出。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r4_answer_phase_verify.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
DECL = "[[QIO:ANSWER]]"  #: 角色声明（plan §1.1）


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
        base_url="http://127.0.0.1:%d/v1" % server.server_port, api_key="sk-r5-verify-0001"
    )
    return NativeAdapter(client=client, model="verify-model")


class _Echo(Tool):
    name = "echo"
    description = "echo（验证用）"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    is_concurrency_safe = True

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def run(self, **kwargs):
        self.seen.append(dict(kwargs))
        return ToolResult(ok=True, content="echo:%s" % kwargs.get("text", ""))


def _registry() -> tuple[ToolRegistry, _Echo]:
    registry = ToolRegistry()
    tool = _Echo()
    registry.register(tool)
    return registry, tool


def _assistant(loop: AgentLoop) -> list[dict]:
    return [e.data for e in loop.bus._history if e.type.value == "ASSISTANT"]


def _non_empty(events: list[dict]) -> list[dict]:
    return [e for e in events if str(e.get("content") or "").strip()]


def _answer_events(events: list[dict]) -> list[dict]:
    return [e for e in _non_empty(events) if e.get("interim") is False]


def _requests(provider) -> list[dict]:
    return [
        {"step": e.get("step_kind"), "tools": e.get("tool_count"), "msgs": e.get("message_count")}
        for e in (getattr(provider, "log", []) or [])
    ]


async def _poll(predicate: Callable[[], bool], *, timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


async def test_declared_answer_streams_into_answer_area_before_call_ends(provider):
    provider.script.set(
        [
            {"chunks": []},
            {"chunks": [DECL + "\n", "正式回答第一句。", "正式回答第二句。"], "chunk_delay_ms": 2500},
        ]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_live")
    task = asyncio.create_task(loop.run("直接回答我"))
    hit = await _poll(
        lambda: any(
            e.get("interim") is False and "正式回答第一句。" in str(e.get("content") or "")
            for e in _assistant(loop)
        ),
        timeout=25,
    )
    still_running = not task.done()
    await asyncio.wait_for(task, timeout=60)
    result = task.result()
    assert hit, (
        "回答调用还在流（没有结束）时，正式回答区没有出现正文 —— 声明之后的正文没有真流式",
        [e.get("content") for e in _non_empty(_assistant(loop))],
    )
    assert still_running, "命中时这一轮已经结束 —— 证据不成立（不是「调用结束前」）"
    assert result.final_content == "正式回答第一句。正式回答第二句。", result.final_content


async def test_first_answer_delta_is_streaming_and_closing_snapshot_is_calibration(provider):
    provider.script.set(
        [{"chunks": []}, {"chunks": [DECL + "\n", "第一段。", "第二段。"], "chunk_delay_ms": 30}]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_stream")
    result = await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    answers = _answer_events(_assistant(loop))
    assert answers, ("整轮没有任何正式回答事件", [e.get("content") for e in _non_empty(_assistant(loop))])
    assert answers[0].get("streaming") is True, (
        "第一个正式回答事件不是流式增量（契约：声明之后从第一个可发布增量起就是流式）", answers[0]
    )
    delta_ids = {str(e.get("delta_id")) for e in answers}
    assert len(delta_ids) == 1, ("正式回答必须落在同一个 delta_id 上", sorted(delta_ids))
    contents = [str(e.get("content")) for e in answers]
    for earlier, later in zip(contents, contents[1:]):
        assert later.startswith(earlier) or earlier.endswith(later), (
            "同一 delta_id 的累计文字回退了", earlier, later
        )
    closing = [e for e in answers if e.get("streaming") is False]
    assert closing and str(closing[-1].get("content")) == result.final_content, (
        "结束校准快照缺失或内容不等于最终回答", closing, result.final_content
    )
    assert any(e.get("streaming") is True for e in answers if e is not closing[-1]), (
        "校准快照成了首次展示来源（它之前没有任何流式回答事件）", answers
    )


async def test_process_area_has_no_copy_of_the_answer(provider):
    answer = "这是最终回答，过程区不该有它的副本。"
    provider.script.set([{"chunks": []}, {"chunks": [DECL + "\n", answer], "chunk_delay_ms": 10}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_nocopy")
    await asyncio.wait_for(loop.run("直接回答我"), timeout=60)
    events = _non_empty(_assistant(loop))
    process_copies = [
        e for e in events if e.get("interim") is not False and answer[:8] in str(e.get("content"))
    ]
    assert not process_copies, (
        "最终回答在过程区出现了副本（同一个答案显示两份）",
        [e.get("content") for e in process_copies],
    )


async def test_tool_round_text_stays_in_process_area_then_declared_answer_streams(provider):
    provider.script.set(
        [
            {
                "chunks": ["我先说明一下，这一步要调用工具。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [{"id": "r5_call_1", "name": "echo", "args_fragments": ['{"text": "r5"}']}],
            },
            {"chunks": [DECL + "\n", "工具跑完了，这是正式回答。"], "chunk_delay_ms": 20},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_tools")
    result = await asyncio.wait_for(loop.run("先说明再调用工具，然后回答"), timeout=60)

    assert tool.seen == [{"text": "r5"}], ("工具必须真的执行", tool.seen)
    events = _non_empty(_assistant(loop))
    working = [e for e in events if "我先说明一下" in str(e.get("content"))]
    assert working, ("工具轮的说明一个字都没发出来", [e.get("content") for e in events])
    assert all(e.get("interim") is not False for e in working), (
        "工具轮的说明被当成正式回答发布过", [e.get("content") for e in working]
    )
    answers = _answer_events(events)
    assert any(e.get("streaming") is True for e in answers), ("工具轮之后的正式回答没有流式增量", answers)
    assert result.final_content == "工具跑完了，这是正式回答。", result.final_content


@pytest.mark.parametrize("chunk_delay_ms", [320, 1200])
async def test_late_tool_increment_does_not_move_answer_text(provider, chunk_delay_ms):
    provider.script.set(
        [
            {
                "chunks": ["我先说明一下。"],
                "chunk_delay_ms": chunk_delay_ms,
                "tool_chunks": [{"id": "r5_late", "name": "echo", "args_fragments": ['{"text": "late"}']}],
            },
            {"chunks": [DECL + "\n", "迟到的工具之后，这是正式回答。"], "chunk_delay_ms": 10},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_late")
    result = await asyncio.wait_for(loop.run("迟到工具轮的验证"), timeout=60)

    events = _non_empty(_assistant(loop))
    published: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.get("interim") is False and str(event.get("content") or "").strip():
            published.setdefault(str(event.get("delta_id") or ""), index)
    for index, event in enumerate(events):
        if event.get("interim") is False or not str(event.get("content") or "").strip():
            continue
        assert published.get(str(event.get("delta_id") or "")) is None, (
            "文字从回答区被移回过程区：第 %d 条 %r" % (index, str(event.get("content"))[:60])
        )
    assert tool.seen == [{"text": "late"}], tool.seen
    assert result.final_content == "迟到的工具之后，这是正式回答。", result.final_content


async def test_same_answer_is_not_generated_twice_across_tool_rounds(provider):
    answer = "两轮工具之后的正式回答。"
    provider.script.set(
        [
            {
                "chunks": ["第一轮说明。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [{"id": "r5_m1", "name": "echo", "args_fragments": ['{"text": "one"}']}],
            },
            {
                "chunks": ["第二轮说明。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [{"id": "r5_m2", "name": "echo", "args_fragments": ['{"text": "two"}']}],
            },
            {"chunks": [DECL + "\n", answer], "chunk_delay_ms": 20},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_twice")
    result = await asyncio.wait_for(loop.run("两轮工具后回答"), timeout=60)

    assert tool.seen == [{"text": "one"}, {"text": "two"}], tool.seen
    requests = _requests(provider)
    events = _non_empty(_assistant(loop))
    answer_events = [e for e in events if answer[:6] in str(e.get("content"))]
    process_copies = [e for e in answer_events if e.get("interim") is not False]
    assert not process_copies, ("正式回答在过程区留了副本", [e.get("content") for e in process_copies])
    assert answer_events, ("正式回答没有交付", [e.get("content") for e in events])
    assert len(requests) == 3, (
        "为同一个答案又发起了一次生成调用（2 轮工具 + 1 次声明回答 = 3 次调用）", {"requests": requests}
    )
    assert result.final_content == answer, result.final_content


async def test_cancel_mid_answer_keeps_published_text(provider):
    provider.script.set(
        [
            {"chunks": []},
            {"chunks": [DECL + "\n", "已经显示的第一段。", "取消时还没到的第二段。"], "chunk_delay_ms": 3000},
        ]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_cancel")
    task = asyncio.create_task(loop.run("回答我，然后被取消"))
    hit = await _poll(
        lambda: any(
            e.get("interim") is False and "已经显示的第一段。" in str(e.get("content") or "")
            for e in _assistant(loop)
        ),
        timeout=25,
    )
    assert hit, ("取消之前回答区没有出现正文：无从谈「保留已显示文字」")
    before = [
        e for e in _assistant(loop) if e.get("interim") is False and str(e.get("content") or "").strip()
    ]
    loop.cancel()
    await asyncio.wait_for(task, timeout=40)

    published: dict[str, str] = {}
    for event in _assistant(loop):
        if event.get("interim") is False and str(event.get("content") or "").strip():
            published[str(event.get("delta_id"))] = str(event.get("content"))
    for event in before:
        delta_id = str(event.get("delta_id"))
        assert delta_id in published, ("取消把已经显示的回答整条弄丢了", delta_id, list(published))
        assert str(event.get("content")) in published[delta_id] or published[delta_id].startswith(
            str(event.get("content"))
        ), ("取消之后的累计文字回退了", published[delta_id])


async def test_answer_call_failure_is_honest(provider):
    provider.script.set([{"chunks": []}, {"status": 500, "body": "stream-aborted", "repeat": 6}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_answer_abort")
    from agent.adapters.errors import ProviderError

    with pytest.raises(ProviderError) as raised:
        await asyncio.wait_for(loop.run("回答我"), timeout=60)
    assert "500" in str(raised.value) or "InternalServer" in str(raised.value), (
        "失败原因必须如实出现", str(raised.value)[:200]
    )
    answers = _non_empty(_answer_events(_assistant(loop)))
    assert not answers, ("回答调用失败了却编出了正式回答", [e.get("content") for e in answers])
