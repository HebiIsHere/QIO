r"""D 独立验证：输出角色（plan §1.1 回答阶段协议）。

**旧语义（本文件 round3/round4 的写法）已被 plan §1.1 取代**（R5 内容角色协议：模型用
[[QIO:ANSWER]] 声明最终回答，声明之后正文真流式进正式回答区；未声明走有界缓冲降级；
tools=[] 只在「整轮完全没有回答内容」时兜底一次）：以前是「工作调用的正文先
interim=true 进过程区，等这次调用结束、且没有工具调用时再用一条快照把它提升成正式回答」。
现在冻结的协议是：

* tools=[...] = **工作调用**：正文是进度说明 → 过程区（interim=true）；
* tools=[] = **回答调用**：正文是正式回答 → **从第一个可发布增量起**就是
  interim=false / streaming=true，直接进正式回答区（provider 还在流时就已经可见）；
* 调用结束时的 {interim:false, streaming:false, content=累计} 只是**校准**，不是首次展示来源；
* 同一 delta_id、累计单调、已进入回答区的文字**永不移动**、过程区不留正式回答副本、不重复显示。

基线（ccb5734，A 未实现前）：普通回答只在结束时给一条 streaming=false 快照 →
本文件的「回答区在调用结束前就有字」「首个回答增量 streaming=true」应当是**红的**。
运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_audit_stream_role_verify.py -q
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
DECL = "[[QIO:ANSWER]]"  #: 角色声明（plan §1.1）：声明之后才是正式回答正文



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


async def _poll(predicate: Callable[[], bool], *, timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


# ---- 1. 回答调用还没结束，正式回答区就应该已经有字 -------------------------------


async def test_answer_area_has_text_before_answer_call_ends(provider):
    provider.script.set(
        [{"chunks": []}, {"chunks": [DECL + "\n", "正式第一句。", "正式第二句。"], "chunk_delay_ms": 2500}]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r3_answer_live")
    task = asyncio.create_task(loop.run("直接回答我"))

    hit = await _poll(
        lambda: any(
            e.get("interim") is False and str(e.get("content") or "").strip()
            for e in _assistant(loop)
        ),
        timeout=20,
    )
    still_running = not task.done()
    result = await asyncio.wait_for(task, timeout=60)

    assert hit, (
        "provider 还在流（回答调用未结束）时，正式回答区一个字都没有",
        [e.get("content") for e in _non_empty(_assistant(loop))],
    )
    assert still_running, "命中时这一轮已经结束 —— 证据不成立（不是「调用结束前」）"
    assert result.final_content == "正式第一句。正式第二句。", result


# ---- 2. 流式 + 单一 delta_id + 结束快照只做校准 --------------------------------------


async def test_answer_is_streaming_single_delta_and_calibrated(provider):
    provider.script.set(
        [{"chunks": []}, {"chunks": [DECL + "\n", "第一段。", "第二段。"], "chunk_delay_ms": 30}]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r3_answer_stream")
    result = await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    answers = _answer_events(_assistant(loop))
    assert answers, (
        "整轮没有任何正式回答事件",
        [e.get("content") for e in _non_empty(_assistant(loop))],
    )
    assert answers[0].get("streaming") is True, (
        "第一个正式回答事件不是流式增量（回答是调用结束后一次性出现的）", answers[0]
    )
    delta_ids = {str(e.get("delta_id")) for e in answers}
    assert len(delta_ids) == 1, ("正式回答必须落在同一个 delta_id 上（不许换 id 重打一遍）", sorted(delta_ids))
    contents = [str(e.get("content")) for e in answers]
    for earlier, later in zip(contents, contents[1:]):
        assert later.startswith(earlier), ("同一 delta_id 的累计文字回退了", earlier, later)
    closing = [e for e in answers if e.get("streaming") is False]
    assert closing, ("缺少调用结束时的累计校准快照", answers)
    assert str(closing[-1].get("content")) == result.final_content, (closing[-1], result.final_content)
    assert any(e.get("streaming") is True for e in answers if e is not closing[-1]), (
        "校准快照成了首次展示来源（它之前没有任何流式回答事件）", answers
    )


# ---- 3. 不撤回、不跨区搬动、过程区无副本 -------------------------------------------


async def test_no_retraction_and_no_cross_area_move(provider):
    answer = "这段文字进了回答区就不许再动。"
    provider.script.set(
        [{"chunks": []}, {"chunks": [DECL + "\n", answer], "chunk_delay_ms": 320}]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r3_answer_move")
    await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    events = _non_empty(_assistant(loop))
    first_answer: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.get("interim") is False:
            first_answer.setdefault(str(event.get("delta_id") or ""), index)
        else:
            assert str(event.get("delta_id") or "") not in first_answer, (
                "文字从回答区被移回过程区（plan §1.1：永不移动已进入回答区的文字）", event
            )
    process_copies = [
        e for e in events if e.get("interim") is not False and answer[:6] in str(e.get("content"))
    ]
    assert not process_copies, (
        "正式回答在过程区出现了副本（用户会看到两遍）", [e.get("content") for e in process_copies]
    )


# ---- 4. 工具轮的正文始终是过程说明（迟到 320ms / 1.2s） -----------------------------


@pytest.mark.parametrize("chunk_delay_ms", [320, 1200])
async def test_tool_turn_text_stays_in_process_area(provider, chunk_delay_ms):
    tool_turn_text = "我先说明一下。这一步马上要调用工具。"
    provider.script.set(
        [
            {
                "chunks": ["我先说明一下。", "这一步马上要调用工具。"],
                "chunk_delay_ms": chunk_delay_ms,
                "tool_chunks": [
                    {"id": "r3_late", "name": "echo", "args_fragments": ['{"text": "late"}']}
                ],
            },
            {"chunks": [DECL + "\n", "工具跑完了，这是正式回答。"]},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r3_late_tool")
    result = await asyncio.wait_for(loop.run("请先说明再调用工具"), timeout=60)

    events = _non_empty(_assistant(loop))
    working = [e for e in events if tool_turn_text[:6] in str(e.get("content"))]
    assert working, ("工具轮的说明一个字都没发出来", [e.get("content") for e in events])
    assert all(e.get("interim") is not False for e in working), (
        "工具轮的正文被当成正式回答发布过（结束时有工具调用 → 是过程说明）",
        [e.get("content") for e in working],
    )
    assert tool.seen == [{"text": "late"}], tool.seen
    assert result.final_content == "工具跑完了，这是正式回答。", result.final_content


# ---- 5. 多工具轮之后进入回答阶段 -----------------------------------------------------


async def test_multi_tool_rounds_then_answer(provider):
    provider.script.set(
        [
            {
                "chunks": ["第一轮说明。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [
                    {"id": "r3_m1", "name": "echo", "args_fragments": ['{"text": "one"}']}
                ],
            },
            {
                "chunks": ["第二轮说明。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [
                    {"id": "r3_m2", "name": "echo", "args_fragments": ['{"text": "two"}']}
                ],
            },
            {"chunks": [DECL + "\n", "两轮工具之后的正式回答。"], "chunk_delay_ms": 20},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r3_multi")
    result = await asyncio.wait_for(loop.run("两轮工具后回答"), timeout=60)

    assert tool.seen == [{"text": "one"}, {"text": "two"}], tool.seen
    # R5：2 轮工具 + 1 次声明回答 = 3 次调用（不再有「结束后再生成一次」的额外回答调用）
    assert len(getattr(provider, "log", []) or []) == 3, (
        "同一个答案被生成了两次（调用台账）",
        [e.get("step_kind") for e in (getattr(provider, "log", []) or [])],
    )
    answers = _answer_events(_non_empty(_assistant(loop)))
    assert any(e.get("streaming") is True for e in answers), (
        "多轮工具之后正式回答仍然只在结束时一次性出现", answers
    )
    assert result.final_content == "两轮工具之后的正式回答。", result.final_content


# ---- 6. 取消：已显示的回答文字必须保留 -----------------------------------------------


async def test_cancel_mid_answer_keeps_published_text(provider):
    provider.script.set(
        [
            {"chunks": []},
            {"chunks": [DECL + "\n", "已经显示的第一段。", "取消时还没到的第二段。"], "chunk_delay_ms": 3000},
        ]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r3_cancel")
    task = asyncio.create_task(loop.run("回答我，然后被取消"))

    hit = await _poll(
        lambda: any(
            e.get("interim") is False and str(e.get("content") or "").strip()
            for e in _assistant(loop)
        ),
        timeout=20,
    )
    assert hit, ("取消之前回答区一个字都没有：无从谈「保留已显示文字」")
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
        assert published[delta_id].startswith(str(event.get("content"))), (
            "取消之后的累计文字回退了", published[delta_id]
        )
