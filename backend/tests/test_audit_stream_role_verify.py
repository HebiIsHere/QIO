"""D 独立验证：输出角色（审计问题 2 / plan §1.1）。

契约来源：docs/plans/2026-10-06-audit-seven-fixes.md §0 第 2 条 + §1.1。
只依据产品规则，不看实现方事后说明：

1. 正文增量**从一开始就以 interim=true** 实时到达（显示在过程区）—— 这就是
   「provider 结束前已可见」，也是边生成边显示；
2. 该次调用**结束且没有任何工具调用**时，用**同一 delta_id** 发一条
   {interim:false, streaming:false, content=累计全文} —— 这段文字**原样提升**到正式回答区
   （同一条消息，不重打、不重复）；
3. 调用结束时**有**工具调用 → 该段留在过程区；**任何情况下都不允许
   「正式回答 → 过程区」的移动**，也不允许已显示文字消失；
   发布本身是**按 ≥40ms 或 ≥24 字符合并**的（契约的节奏要求，不逐字符发），所以
   「最后一条已发布的 interim」**本来就可能比累计全文短** —— 提升的收尾快照会把还在
   待发窗口里的尾巴一起交付。**这不是改写、不是重复、不是丢字**，因此断言只能是
   「最后一条 interim 是提升内容的前缀 + 提升内容 == 累计全文」，不能要求逐字相等；
4. 工具阶段收尾的调用**没有产出任何正文**时，最多补**一次** tools=[] 的调用专门产出正式回答，
   它的正文从**第一个增量起**就是 interim=false；
5. 判据里不得出现：经过多少时间 / 文案像不像答案 / 暂未收到工具增量。

假厂商端点：scripts/verify_stream_provider.py（真 HTTP + 真 SSE，本地扮演，不联网、不需密钥）。
基线（e428bb9）现状：GUARD_MS=300 到期即判 answer（loop.py:212-218），随后在
note_tool_call() 里把文字从 answer **移回** interim（loop.py:183-189）；工具阶段没有正文时
直接以 final_content=None 收尾 —— 因此本文件在修复前应当是**红的**。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_audit_stream_role_verify.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    """按到达顺序的全部 ASSISTANT 事件载荷。"""
    return [e.data for e in loop.bus._history if e.type.value == "ASSISTANT"]


def _non_empty(events: list[dict]) -> list[dict]:
    return [e for e in events if str(e.get("content") or "").strip()]


async def _poll(predicate: Callable[[], bool], *, timeout: float, step: float = 0.01) -> bool:
    """轮询到 predicate 为真；超时返回 False（调用方负责给红证据）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _assert_no_text_leaves_answer_area(events: list[dict]) -> None:
    """核心不变式：同一 delta_id 的文字一旦进入答案区，就再也不得出现 interim=true。

    这是「没有任何文字从答案区移走」的可判定写法：对每条 delta_id 记住第一次
    interim=false 的下标，之后任何 interim=true 且内容与该 delta 已发布文字重叠的事件
    都是「移走」。

    同时断言「已显示的文字不会消失」：同一 delta_id 的累计快照必须逐条前缀单调。
    """
    first_answer: dict[str, int] = {}
    seen_content: dict[str, str] = {}
    for index, event in enumerate(events):
        delta_id = str(event.get("delta_id") or "")
        content = str(event.get("content") or "")
        if content.strip():
            previous = seen_content.get(delta_id)
            if previous is not None:
                assert content.startswith(previous), (
                    "同一 delta_id 的累计文字回退了（用户已经看到的字消失了）："
                    "第 %d 条 %r 不是前一条 %r 的前缀" % (index, content[:60], previous[:60])
                )
            seen_content[delta_id] = content
        if event.get("interim") is False:
            first_answer.setdefault(delta_id, index)
            continue
        published = first_answer.get(delta_id)
        if published is None:
            continue
        if not content.strip():
            continue
        pytest.fail(
            "文字从答案区被移回过程区（plan §1.1 第 3 条：永不移动已进入答案区的文字）："
            "delta_id=%s 在第 %d 条已判为正式回答，第 %d 条又变成 interim=true，content=%r"
            % (delta_id, published, index, content[:80])
        )


# ---- 1. 工具增量晚于正文：文字绝不从答案区移走（真 SSE） ---------------------------


@pytest.mark.parametrize("chunk_delay_ms", [320, 1200])
async def test_late_tool_call_never_moves_text_out_of_answer_area(provider, chunk_delay_ms):
    """工具增量晚于正文（>300ms 与 >1s 两档）时，工具轮的文字必须始终是过程说明。

    基线现状：守卫窗口先把它判成 answer，note_tool_call() 再把它移回 interim —— 红。
    """
    tool_turn_text = "我先说明一下。这一步马上要调用工具。"
    provider.script.set(
        [
            {
                "chunks": ["我先说明一下。", "这一步马上要调用工具。"],
                "chunk_delay_ms": chunk_delay_ms,
                "tool_chunks": [
                    {"id": "call_late", "name": "echo", "args_fragments": ['{"text": "late"}']}
                ],
            },
            {"chunks": ["工具跑完了，这是正式回答。"]},
        ]
    )

    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_late_tool")
    result = await asyncio.wait_for(loop.run("请先说明再调用工具"), timeout=40)

    events = _assistant(loop)
    assert events, "整轮没有任何 ASSISTANT 增量"
    _assert_no_text_leaves_answer_area(events)

    # 工具轮的正文（该次调用以工具调用收尾）必须只出现在过程区
    tool_turn_events = [
        e
        for e in _non_empty(events)
        if str(e.get("content") or "").startswith(tool_turn_text[:6])
        or str(e.get("content") or "") in tool_turn_text
    ]
    assert tool_turn_events, (
        "工具轮的正文一个字都没有发出来（边生成边显示失效）",
        [e.get("content") for e in _non_empty(events)],
    )
    answer_like = [e for e in tool_turn_events if e.get("interim") is False]
    assert not answer_like, (
        "工具轮的正文被当成正式回答发布过（plan §1.1：结束时有工具调用 → 是过程说明）",
        [e.get("content") for e in answer_like],
    )

    assert tool.seen == [{"text": "late"}], ("工具必须真的被执行", tool.seen)
    assert result.final_content == "工具跑完了，这是正式回答。", result.final_content
    answer_events = [e for e in _non_empty(events) if e.get("interim") is False]
    assert answer_events and str(answer_events[-1].get("content")) == result.final_content, (
        "正式回答必须以 delta 快照交付",
        [e.get("content") for e in answer_events],
    )


async def test_process_text_is_visible_before_provider_finishes(provider):
    """边生成边显示：工具轮的说明在 provider 结束之前就已经在过程区（interim=true）可见。"""
    provider.script.set(
        [
            {
                "chunks": ["先读一下文件。", "读完了，接下来要改代码。"],
                "chunk_delay_ms": 320,
                "tool_chunks": [
                    {"id": "call_live", "name": "echo", "args_fragments": ['{"text": "live"}']}
                ],
            },
            {"chunks": ["改好了。"]},
        ]
    )

    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_live_text")
    task = asyncio.create_task(loop.run("边做边说"))

    seen_live = False

    async def _watch() -> None:
        nonlocal seen_live
        deadline = time.monotonic() + 10
        while not task.done() and time.monotonic() < deadline:
            for event in _assistant(loop):
                if event.get("interim") is True and str(event.get("content") or "").strip():
                    timeline = [str(item["event"]) for item in provider.timeline]
                    if "stream_end" not in timeline:
                        seen_live = True
                        return
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(_watch())
    try:
        result = await asyncio.wait_for(task, timeout=40)
    finally:
        watcher.cancel()
        with contextlib.suppress(Exception):
            await watcher

    assert seen_live, (
        "provider 结束之前过程区一直是空的（答案被守卫窗口憋到 300ms 之后才出现，"
        "或者直接落在正式回答区）—— 这不是边生成边显示"
    )
    assert result.final_content == "改好了。", result.final_content


async def test_plain_answer_is_visible_in_process_area_before_provider_finishes(provider):
    """①：一次没有工具调用的普通回答，正文增量必须**在 provider 结束前**就出现在过程区。

    基线现状：守卫窗口把它们憋住，300ms 到期才以 interim=false（正式回答）放行 ——
    过程区在 provider 结束前一直是空的，这不符合「边生成边显示」—— 红。
    """
    chunks = ["第%d句。" % i for i in range(1, 7)]
    provider.script.set([{"chunks": chunks, "chunk_delay_ms": 60}])

    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_plain_live")
    task = asyncio.create_task(loop.run("请分多次回答"))

    early_interim: str | None = None
    early_timeline: list[str] = []
    while not task.done():
        for event in _assistant(loop):
            if event.get("interim") is True and str(event.get("content") or "").strip():
                early_interim = str(event.get("content"))
                early_timeline = [str(item["event"]) for item in provider.timeline]
                break
        if early_interim is not None:
            break
        await asyncio.sleep(0.01)

    result = await asyncio.wait_for(task, timeout=40)
    assert early_interim is not None, (
        "provider 结束前过程区没有任何 interim=true 的文字（正文没有实时到达）"
    )
    assert "stream_end" not in early_timeline, (
        "过程区的文字是在 provider 已经发完之后才出现的（假流式）：时间线=%r" % early_timeline
    )
    assert result.final_content == "".join(chunks), result.final_content


async def test_promotion_to_answer_happens_once_with_the_full_text(provider):
    """②：提升只发一条 {interim:false, streaming:false, content=累计全文}，原样、不重复。"""
    chunks = ["第一句。", "第二句。", "第三句。"]
    full = "".join(chunks)
    provider.script.set([{"chunks": chunks, "chunk_delay_ms": 30}])

    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="turn_promote")
    result = await asyncio.wait_for(loop.run("回答我"), timeout=40)

    events = _assistant(loop)
    _assert_no_text_leaves_answer_area(events)
    interim_events = [e for e in events if e.get("interim") is True and str(e.get("content") or "").strip()]
    assert interim_events, "正文增量必须先进过程区（interim=true）"
    assert all(str(e.get("streaming")) == "True" for e in interim_events), (
        "过程区的增量应当是 streaming=true（实时到达）",
        [(e.get("content"), e.get("streaming")) for e in interim_events],
    )

    answers = [e for e in events if e.get("interim") is False and str(e.get("content") or "").strip()]
    assert len(answers) == 1, (
        "提升必须只有一条事件（同一份文字只交付一次，不重打、不重复）",
        [(e.get("content"), e.get("streaming"), e.get("seq")) for e in answers],
    )
    promoted = answers[0]
    # (1) 提升的必须是**累计全文**（三段拼接），不是某个残缺前缀
    assert str(promoted.get("content")) == full, ("提升的必须是累计全文", promoted.get("content"))
    # (2) 最后一条已发布的 interim 必须是它的**前缀**：单调累积，绝不回退/改写。
    #     不能要求逐字相等 —— 合并发布（≥40ms / ≥24 字符）意味着尾巴可能还在待发窗口里。
    assert str(promoted.get("content")).startswith(str(interim_events[-1].get("content"))), (
        "提升只能在已显示文字之后追加（同一份文字：不重写、不回退）",
        (interim_events[-1].get("content"), promoted.get("content")),
    )
    # (3) 收尾事实：streaming=false、同一 delta_id
    assert promoted.get("streaming") is False, "提升事件是收尾事实（streaming=false），不是新打字"
    assert str(promoted.get("delta_id")) == str(interim_events[-1].get("delta_id")), (
        "提升必须用同一个 delta_id（同一条消息）",
        (interim_events[-1].get("delta_id"), promoted.get("delta_id")),
    )
    # (4) 这份文字在答案区**全局只有一条**（不得另起一条重复交付）
    assert len({str(e.get("delta_id") or "") for e in answers}) == 1, (
        "同一份回答只能有一条答案事件（不得另起 delta_id 重复交付）",
        [(e.get("delta_id"), e.get("content")) for e in answers],
    )
    # (5) 提升之后**不得**再有同 delta_id 的 interim=true 事件（§1.1 禁止反向移动）
    promotion_index = next(i for i, event in enumerate(events) if event is promoted)
    later_interim = [
        event
        for event in events[promotion_index + 1 :]
        if str(event.get("delta_id") or "") == str(promoted.get("delta_id"))
        and event.get("interim") is True
    ]
    assert not later_interim, (
        "提升之后不得再有同 delta_id 的 interim=true 事件（正式回答 → 过程区被永久废止）",
        [e.get("content") for e in later_interim],
    )
    assert result.final_content == full, result.final_content


# ---- 2. 工具阶段没有正文：必须再发一次不带工具的正式回答调用 -----------------------


class _TimedAdapter(FakeStreamAdapter):
    """记录每次 stream() 的调用时刻（用来量「第一个答案增量」的延迟）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.call_started: list[float] = []

    async def stream(self, messages, tools, **kwargs):  # type: ignore[override]
        self.call_started.append(time.monotonic())
        async for delta in super().stream(messages, tools, **kwargs):
            yield delta


async def test_tool_free_answer_call_streams_into_answer_area_from_first_delta():
    """工具阶段收尾的调用没有正文 → 再发一次**不带工具**的调用，其正文从第一个增量起进答案区。

    基线现状：第二次调用没有正文就直接 final_content=None 收尾（用户拿不到回答）—— 红。
    """
    answer_text = "这是正式回答。（流式）"
    adapter = _TimedAdapter(
        [
            StreamScript(tool_calls=[ScriptedToolCall(id="c1", name="echo", arguments={"text": "hi"})]),
            # 工具阶段收尾的这次调用：没有正文、也没有工具调用
            StreamScript(text=""),
            # 专门产出正式回答的调用：不带工具
            StreamScript(text_chunks=["这是", "正式", "回答。", "（流式）"], gap_ms=60),
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(adapter, registry, EventBus(), turn_id="turn_answer_phase")
    task = asyncio.create_task(loop.run("先做工具再回答"))

    first_answer_at: float | None = None
    while not task.done():
        for event in _assistant(loop):
            if event.get("interim") is False and str(event.get("content") or "").strip():
                first_answer_at = time.monotonic()
                break
        if first_answer_at is not None:
            break
        await asyncio.sleep(0.005)

    result = await asyncio.wait_for(task, timeout=20)

    assert tool.seen == [{"text": "hi"}], ("工具必须先被执行", tool.seen)
    assert len(adapter.requests) >= 3, (
        "工具阶段收尾的调用没有任何正文时，必须再发一次调用专门产出正式回答（plan §1.1 第 4 条）",
        [len(row["messages"]) for row in adapter.requests],
    )
    assert [t.name for t in adapter.requests[2]["tools"]] == [], (
        "产出正式回答的那次调用必须**不带工具**（否则它仍可能转去调用工具）",
        [t.name for t in adapter.requests[2]["tools"]],
    )
    tool_free_calls = [row for row in adapter.requests if not row["tools"]]
    assert len(tool_free_calls) <= 1, (
        "每轮最多补**一次** tools=[] 的正式回答调用（成本要如实、不能重复发）",
        len(tool_free_calls),
    )
    assert result.final_content == answer_text, (
        "工具阶段没有正文时用户必须仍然拿到正式回答",
        result.final_content,
    )

    events = _assistant(loop)
    _assert_no_text_leaves_answer_area(events)
    answer_events = [e for e in _non_empty(events) if e.get("interim") is False]
    assert len(answer_events) >= 2, (
        "正式回答必须真流式（多个累计增量），不能等 provider 结束再一次性给",
        [e.get("content") for e in answer_events],
    )
    streamed = [e for e in answer_events if e.get("streaming") is True]
    assert streamed, (
        "正式回答的增量必须是 streaming=true（真流式，不是整段落地）",
        [e.get("content") for e in answer_events],
    )

    # 第一个答案增量必须紧跟着那次调用的第一个分片出现（没有 300ms 守卫窗口）
    assert first_answer_at is not None, "整轮没有出现任何正式回答增量"
    assert adapter.call_started, "假 adapter 没有被调用"
    started = adapter.call_started[-1]
    latency_ms = (first_answer_at - started) * 1000
    assert latency_ms < 250, (
        "正式回答的第一个增量离那次调用的开始太远（守卫窗口还在）：%.0f ms" % latency_ms
    )
    assert any(str(e.get("content")) == answer_text for e in answer_events), (
        "最后一条答案增量必须是权威全文",
        [e.get("content") for e in answer_events],
    )


async def test_role_evidence_marks_the_reliable_criterion():
    """plan §1.1：新增可选字段 role_evidence，用于取证与测试断言。"""
    adapter = _TimedAdapter(
        [
            StreamScript(text_chunks=["直接回答。"]),
        ]
    )
    registry, _tool = _registry()
    loop = AgentLoop(adapter, registry, EventBus(), turn_id="turn_role_evidence")
    await asyncio.wait_for(loop.run("直接回答"), timeout=20)

    answers = [e for e in _assistant(loop) if e.get("interim") is False]
    assert answers, "没有正式回答事件"
    assert all(e.get("role_evidence") == "call_closed_without_tools" for e in answers), (
        "调用结束且无工具调用 → role_evidence 必须是 call_closed_without_tools",
        [(e.get("content"), e.get("role_evidence")) for e in answers],
    )
