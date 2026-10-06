r"""D 独立验证（R4 问题一）：回答阶段协议 —— 正式回答**边生成边进正式回答区**。

契约来源：docs/plans/2026-10-07-three-remaining-fixes.md §1.1（冻结）。
只依据 plan §3 的用户可见规则，不采用实现方事后口径：

1. 判据只有一条：这次模型调用**带不带工具**（tools=[] = 回答调用）；
2. 回答调用的正文**从第一个可发布增量起**就是 interim=false / streaming=true，
   直接进正式回答区 —— provider 还在流的时候回答区就**已经有字**；
3. 调用结束时的 {interim:false, streaming:false, content=累计} 只是**校准**，不是首次展示来源；
4. 过程区**不得**出现正式回答的副本；已进入回答区的文字永不移动；
5. 工具调用（工作中）的正文始终是过程说明（interim=true）。

基线（ccb5734）现状：普通回答走「过程区 interim=true → 结束时一条 streaming=false 快照」，
且只有「工具阶段收尾零正文」这一稀少路径才补 tools=[] 调用（loop.py:1088-1091）——
因此「回答区在 provider 结束前就有字」与「首个回答增量 streaming=true」在修复前应当是**红的**。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r4_answer_phase_verify.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


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


async def _poll(predicate: Callable[[], bool], *, timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _answer_events(events: list[dict]) -> list[dict]:
    return [e for e in _non_empty(events) if e.get("interim") is False]


# ---- 1. 回答调用还没结束，正式回答区就应该已经有字（核心） ---------------------------


async def test_answer_text_visible_in_answer_area_before_call_ends(provider):
    """受控假 provider 在第一段正文后暂停（调用未结束）时，回答区必须已经有字。

    证据：命中时刻 provider 那一轮**还没结束**（loop 任务仍在跑），且文字在回答区（interim=false）。
    基线：普通回答只在结束时发一条 streaming=false 快照 → 本用例红（窗口内回答区是空的）。
    """
    provider.script.set(
        [
            {"chunks": []},
            {"chunks": ["正式回答第一句。", "正式回答第二句。"], "chunk_delay_ms": 2500},
        ]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_live")
    task = asyncio.create_task(loop.run("直接回答我"))

    seen: dict[str, Any] = {"at": None, "content": None}

    async def _watch() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for event in _answer_events(_assistant(loop)):
                if str(event.get("content") or "").strip():
                    seen["at"] = time.monotonic()
                    seen["content"] = str(event.get("content"))
                    return
            await asyncio.sleep(0.01)

    await _watch()
    still_running = not task.done()
    await asyncio.wait_for(task, timeout=60)
    result = task.result() if hasattr(task, "result") else None

    assert seen["content"], (
        "provider 还在流式输出（调用未结束）时，正式回答区一个字都没有："
        "回答不是边生成边进正式回答区，而是等调用结束才一次性出现",
        [e.get("content") for e in _non_empty(_assistant(loop))],
    )
    assert still_running, "命中时这一轮已经结束 —— 证据不成立（不是「provider 结束前」）"
    assert result is not None and result.final_content == "正式回答第一句。正式回答第二句。", result


async def test_first_answer_delta_is_interim_false_and_streaming(provider):
    """回答调用的第一个可发布增量：interim=false 且 streaming=true（不是结束快照）。"""
    provider.script.set(
        [{"chunks": []}, {"chunks": ["边生成边显示的第一段。"], "chunk_delay_ms": 10}]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_stream")
    await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    answers = _answer_events(_assistant(loop))
    assert answers, (
        "整轮没有任何正式回答事件",
        [e.get("content") for e in _non_empty(_assistant(loop))],
    )
    first = answers[0]
    assert first.get("streaming") is True, (
        "第一个正式回答事件不是流式增量（streaming != true）：回答是「调用结束后一次性出现」的",
        first,
    )


async def test_closing_snapshot_is_calibration_not_first_display(provider):
    """结束时的累计快照是校准：它之前必须已经有流式回答事件（同一 delta_id）。"""
    provider.script.set(
        [{"chunks": []}, {"chunks": ["第一段。", "第二段。"], "chunk_delay_ms": 30}]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_calib")
    result = await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    answers = _answer_events(_assistant(loop))
    streaming = [e for e in answers if e.get("streaming") is True]
    closing = [e for e in answers if e.get("streaming") is False]
    assert streaming, (
        "没有任何流式回答事件：正式回答只由结束时的累计快照交付（契约 §1.1：校准不是首次展示来源）",
        answers,
    )
    assert closing and str(closing[-1].get("content")) == result.final_content, (
        "结束校准快照缺失或内容不等于最终回答", closing, result.final_content
    )


async def test_answer_has_no_copy_in_process_area(provider):
    """正式回答不得在过程区留副本（interim=true）。"""
    answer = "这是正式回答，过程区不该有它的副本。"
    provider.script.set([{"chunks": []}, {"chunks": [answer], "chunk_delay_ms": 10}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_nocopy")
    await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    events = _non_empty(_assistant(loop))
    process_copies = [
        e for e in events if e.get("interim") is not False and answer[:8] in str(e.get("content"))
    ]
    assert not process_copies, (
        "正式回答在过程区出现了副本（用户会看到两遍）",
        [e.get("content") for e in process_copies],
    )


# ---- 2. 工具轮：过程说明留在过程区，回答随后流式进回答区 ---------------------------


async def test_working_call_text_stays_in_process_area_then_answer_streams(provider):
    """工具轮的说明始终是过程区；随后的一次 tools=[] 调用把正式回答流式写进回答区。"""
    provider.script.set(
        [
            {
                "chunks": ["我先说明一下，这一步要调用工具。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [
                    {"id": "r4_call_1", "name": "echo", "args_fragments": ['{"text": "r4"}']}
                ],
            },
            {"chunks": []},
            {"chunks": ["工具跑完了，这是正式回答。"], "chunk_delay_ms": 20},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_tools")
    result = await asyncio.wait_for(loop.run("先说明再调用工具，然后回答"), timeout=60)

    assert tool.seen == [{"text": "r4"}], ("工具必须真的执行", tool.seen)
    events = _non_empty(_assistant(loop))
    working = [e for e in events if "我先说明一下" in str(e.get("content"))]
    assert working, ("工具轮的说明一个字都没发出来（边生成边显示失效）", [e.get("content") for e in events])
    assert all(e.get("interim") is not False for e in working), (
        "工具轮的说明被当成正式回答发布过", [e.get("content") for e in working]
    )
    answers = _answer_events(events)
    assert answers and result.final_content == "工具跑完了，这是正式回答。", (answers, result.final_content)
    assert any(e.get("streaming") is True for e in answers), (
        "工具后的正式回答没有流式增量（只在结束时一次性出现）", answers
    )


@pytest.mark.parametrize("chunk_delay_ms", [320, 1200])
async def test_late_tool_increment_does_not_move_answer_text(provider, chunk_delay_ms):
    """迟到 320ms/1.2s 的工具增量不得把已进入回答区的文字移回过程区。"""
    provider.script.set(
        [
            {
                "chunks": ["我先说明一下。"],
                "chunk_delay_ms": chunk_delay_ms,
                "tool_chunks": [
                    {"id": "r4_call_late", "name": "echo", "args_fragments": ['{"text": "late"}']}
                ],
            },
            {"chunks": []},
            {"chunks": ["迟到的工具之后，这是正式回答。"], "chunk_delay_ms": 10},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_late")
    result = await asyncio.wait_for(loop.run("迟到工具轮的验证"), timeout=60)

    events = _non_empty(_assistant(loop))
    first_answer: dict[str, int] = {}
    for index, event in enumerate(events):
        if event.get("interim") is False and str(event.get("content") or "").strip():
            first_answer.setdefault(str(event.get("delta_id") or ""), index)
    for index, event in enumerate(events):
        if event.get("interim") is False or not str(event.get("content") or "").strip():
            continue
        if first_answer.get(str(event.get("delta_id") or "")) is not None:
            pytest.fail(
                "文字从回答区被移回过程区（契约 §1.1）：第 %d 条 %r"
                % (index, str(event.get("content"))[:60])
            )
    assert tool.seen == [{"text": "late"}], tool.seen
    assert result.final_content == "迟到的工具之后，这是正式回答。", result.final_content

# ---- 3. 多工具轮 / 取消 / 回答调用失败（阶段一补齐） -------------------------------


async def test_multi_tool_rounds_then_answer_streams(provider):
    """两轮以上工具之后进入回答阶段：两轮说明都在过程区，正式回答流式进回答区。"""
    provider.script.set(
        [
            {
                "chunks": ["第一轮说明。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [
                    {"id": "r4_m1", "name": "echo", "args_fragments": ['{"text": "one"}']}
                ],
            },
            {
                "chunks": ["第二轮说明。"],
                "chunk_delay_ms": 20,
                "tool_chunks": [
                    {"id": "r4_m2", "name": "echo", "args_fragments": ['{"text": "two"}']}
                ],
            },
            {"chunks": []},
            {"chunks": ["两轮工具之后的正式回答。"], "chunk_delay_ms": 20},
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_multi")
    result = await asyncio.wait_for(loop.run("两轮工具后回答"), timeout=60)

    assert tool.seen == [{"text": "one"}, {"text": "two"}], ("两轮工具都要真的执行", tool.seen)
    events = _non_empty(_assistant(loop))
    for text in ("第一轮说明。", "第二轮说明。"):
        hits = [e for e in events if text in str(e.get("content"))]
        assert hits, ("工具轮的说明没有边生成边显示", text, [e.get("content") for e in events])
        assert all(e.get("interim") is not False for e in hits), (
            "工具轮的说明被当成正式回答发布过", text, [e.get("content") for e in hits]
        )
    answers = _answer_events(events)
    assert any(e.get("streaming") is True for e in answers), (
        "多轮工具之后，正式回答仍然只在结束时一次性出现（没有流式增量）", answers
    )
    assert result.final_content == "两轮工具之后的正式回答。", result.final_content


async def test_cancel_mid_answer_keeps_published_text(provider):
    """回答调用流到一半取消：已经显示的回答文字必须保留，不得撤回、不得出现搬家事件。"""
    provider.script.set(
        [
            {"chunks": []},
            {"chunks": ["已经显示的第一段。", "取消时还没到的第二段。"], "chunk_delay_ms": 3000},
        ]
    )
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_cancel")
    task = asyncio.create_task(loop.run("回答我，然后被取消"))

    shown = await _poll(
        lambda: any(
            e.get("interim") is False and str(e.get("content") or "").strip()
            for e in _assistant(loop)
        ),
        timeout=20,
    )
    assert shown, (
        "取消之前（provider 还在流）回答区一个字都没有：无从谈「保留已显示文字」",
        [e.get("content") for e in _non_empty(_assistant(loop))],
    )
    before = [e for e in _assistant(loop) if e.get("interim") is False and str(e.get("content") or "").strip()]
    loop.cancel()
    await asyncio.wait_for(task, timeout=40)

    after = _assistant(loop)
    published = {
        str(e.get("delta_id")): str(e.get("content"))
        for e in after
        if e.get("interim") is False and str(e.get("content") or "").strip()
    }
    for event in before:
        delta_id = str(event.get("delta_id"))
        assert delta_id in published, ("取消把已经显示的回答整条弄丢了", delta_id, list(published))
        assert published[delta_id].startswith(str(event.get("content"))), (
            "取消之后的累计文字回退了（用户已经看到的字消失了）", published[delta_id]
        )
    for event in after:
        if event.get("interim") is False or not str(event.get("content") or "").strip():
            continue
        assert str(event.get("delta_id")) not in published, (
            "取消之后又出现 interim=true 的搬家事件（已进入回答区的文字被移回过程区）", event
        )


async def test_answer_call_failure_is_honest(provider):
    """回答调用失败（断流/厂商 500）必须如实失败，不得把空回答当成成功。"""
    # 回答调用失败：把所有可能的重试都钉成 500（FIFO 耗尽会落到 provider 的 default，
    # 那会掩盖「失败被当成成功」这一条 —— 实测踩过）
    provider.script.set([{"chunks": []}, {"status": 500, "body": "stream-aborted", "repeat": 6}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r4_answer_abort")
    # 循环层可以直接把不可恢复的 provider 错误抛出去（服务层负责落成 failed + 原因），
    # 也可以返回一个如实标失败的结果 —— 两种都不算错。这里只钉用户可见的那一条：
    # **绝不编造正式回答**，且失败原因如实出现。
    result = None
    failure: str | None = None
    try:
        result = await asyncio.wait_for(loop.run("回答我"), timeout=60)
    except Exception as exc:  # noqa: BLE001 - 失败冒泡是允许的
        failure = f"{type(exc).__name__}: {exc}"

    answers = _non_empty(_answer_events(_assistant(loop)))
    assert not answers, ("回答调用失败了却编出了正式回答", [e.get("content") for e in answers])
    if failure is not None:
        assert "500" in failure or "InternalServer" in failure, (
            "失败原因必须如实出现（不能是一句没头没尾的错）", failure[:200]
        )
    else:
        failed = (
            getattr(result, "status", None) not in ("done", "completed", None)
            or bool(getattr(result, "error", None))
            or not str(getattr(result, "final_content", "") or "").strip()
        )
        assert failed, ("回答调用 500，整轮却像正常完成一样交付了空回答", result)

