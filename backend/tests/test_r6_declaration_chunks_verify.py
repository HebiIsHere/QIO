r"""D 独立验证（R6 问题二 + 问题三）：增量前缀解析与未声明长正文的**分块无关性**。

契约：docs/plans/2026-10-08-four-remaining-fixes.md §1.2 / §1.3（冻结）+ §3。
基线 6507c64 的两处缺陷（本文件必须红）：
* §0.2 \`core/loop.py:271\`：\`if len(probe) > MARKER_PROBE_CHARS(32)\` 把「累计收到的正文长度」
  当成「声明是否有效」的证据 → **声明与长正文同一大分块**时判定失败，整段被当未声明，
  结束后连声明一起泄漏、角色也判错；
* §0.3 \`core/loop.py:314\`：未声明正文超 \`UNDECLARED_BUFFER_LIMIT\`（按**字符**计）即改判 \`interim\`
  放行到过程区；调用结束无工具 → 兜底再生成一次 → **2 次调用、两份显示**。

本文件只依据**用户可见规则**：同一段字节/文本序列，无论怎样拆分或合并，
角色判定、最终正文、声明隐藏、调用次数都必须一致。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r6_declaration_chunks_verify.py -q
"""

from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from agent.adapters.base import (
    STREAM_DONE,
    ToolCall,
    STREAM_TEXT,
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    StreamDelta,
)
from agent.api.bus import EventBus
from agent.core import answer_buffer as buffer_module
from agent.core.loop import AgentLoop
from agent.prompts import ANSWER_MARKER
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

DECL = ANSWER_MARKER
#: 契约 §1.3：内存上限按 **UTF-8 字节**计量（R6 起在 core/answer_buffer.py；不从实现里取值就不算验收）
LIMIT = int(buffer_module.UNDECLARED_MEMORY_LIMIT)
SPILL_LIMIT = int(buffer_module.UNDECLARED_SPILL_LIMIT)

#: 固定一份含声明正文（中文 + 代码块 + 表格）
BODY = (
    "这是正式回答的第一段，说明结论。\n\n"
    "第二段里有一点点细节。\n\n"
    "\`\`\`python\nprint('r6')\n\`\`\`\n\n"
    "| 列 A | 列 B |\n| --- | --- |\n| 甲 | 乙 |\n"
)
FULL = DECL + "\n" + BODY


class _ChunkAdapter(BaseAdapter):
    """按给定的分块吐字（可整段、可逐字、可含空块）；记录调用次数。"""

    mode = AdapterMode.NATIVE
    supports_stream = True

    def __init__(self, chunks: list[str], *, streaming: bool = True) -> None:
        self.chunks = list(chunks)
        self.calls = 0
        self.streaming = streaming

    def _text(self) -> str:
        return "".join(self.chunks)

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=self._text()))

    async def stream(self, messages, tools, **kwargs):  # noqa: ANN001
        self.calls += 1
        for chunk in self.chunks:
            if chunk:
                yield StreamDelta(kind=STREAM_TEXT, text=chunk)
        yield StreamDelta(
            kind=STREAM_DONE,
            completion=Completion(message=ChatMessage(role="assistant", content=self._text())),
        )


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


def _events(loop: AgentLoop) -> list[dict]:
    return [e.data for e in loop.bus._history if e.type.value == "ASSISTANT"]


def _non_empty(events: list[dict]) -> list[dict]:
    return [e for e in events if str(e.get("content") or "").strip()]


def _interims(events: list[dict]) -> set[bool]:
    return {bool(e.get("interim")) for e in _non_empty(events)}


def _joined(events: list[dict]) -> str:
    return "\n".join(str(e.get("content") or "") for e in events)


async def _run(adapter, *, registry=None, turn_id="r6_case"):
    registry = registry or _registry()[0]
    loop = AgentLoop(adapter, registry, EventBus(), turn_id=turn_id)
    result = await asyncio.wait_for(loop.run("请回答我"), timeout=90)
    return loop, result


def _assert_declared_contract(loop: AgentLoop, result, adapter, *, label: str) -> None:
    """声明的共同契约：正文一致、角色=回答、无声明泄漏、只一次调用、过程区无副本。"""
    events = _non_empty(_events(loop))
    joined = _joined(events)
    assert result.final_content == BODY, (
        "%s：最终正文与期望不一致" % label,
        {"got": result.final_content[:120], "want": BODY[:120]},
    )
    assert DECL not in result.final_content and DECL not in joined, (
        "%s：角色声明泄漏到了正文里（契约 §1.2：声明必须隐藏）" % label,
        {"final": result.final_content[:80], "events": joined[:120]},
    )
    assert _interims(events) == {False}, (
        "%s：角色判定错了（声明的正文必须全部落在正式回答区）" % label,
        {"interims": sorted(_interims(events)), "sample": joined[:120]},
    )
    assert adapter.calls == 1, (
        "%s：为同一个答案发生了 %d 次调用（不得重复生成）" % (label, adapter.calls)
    )
    assert any(e.get("streaming") is True for e in events), (
        "%s：合规声明路径必须真流式（要有 streaming=true 的增量）" % label,
        [e.get("streaming") for e in events],
    )


# ---- 分块策略（同一份 FULL 文本，不同拆法） -----------------------------------------


def _chunks_single_block() -> list[str]:
    return [FULL]


def _chunks_declaration_split_every_position() -> list[list[str]]:
    return [[DECL[:i], DECL[i:] + BODY] for i in range(1, len(DECL))]


def _chunks_declaration_alone() -> list[str]:
    return [DECL, BODY]


def _chunks_one_char() -> list[str]:
    return list(FULL)


def _chunks_empty_blocks() -> list[str]:
    return ["", FULL[:4], "", FULL[4:], ""]


def _chunks_random(seed: int = 20261008) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    index = 0
    while index < len(FULL):
        size = rng.randint(1, 9)
        out.append(FULL[index : index + size])
        index += size
    return out


def _cases() -> list[tuple[str, list[str]]]:
    cases: list[tuple[str, list[str]]] = [
        ("一个大分块（声明+全部正文）", _chunks_single_block()),
        ("声明单独一块+正文整块", _chunks_declaration_alone()),
        ("一字符一块", _chunks_one_char()),
        ("空块穿插", _chunks_empty_blocks()),
        ("确定性随机分块", _chunks_random()),
        ("小写声明", ["[[qio:answer]]\n" + BODY]),
        ("CRLF 声明", [DECL + "\r\n" + BODY]),
    ]
    for i, chunks in enumerate(_chunks_declaration_split_every_position(), start=1):
        cases.append(("声明在第 %d 个位置拆开" % i, chunks))
    return cases


@pytest.mark.parametrize("label,chunks", _cases(), ids=[c[0] for c in _cases()])
async def test_declaration_chunk_invariance(label, chunks):
    """同一份含声明正文，任意拆法 → 相同最终正文、相同角色、无泄漏、只一次调用。"""
    adapter = _ChunkAdapter(chunks)
    loop, result = await _run(adapter, turn_id="r6_chunk_" + str(abs(hash(label)) % 10_000))
    _assert_declared_contract(loop, result, adapter, label=label)
    print("[诊断] %s：分块数=%d，调用=%d，角色=%s" % (label, len(chunks), adapter.calls, sorted(_interims(_non_empty(_events(loop))))))


async def test_long_english_declared_body_is_not_truncated_or_leaked():
    """长英文正文（>32 字符）与声明同块：必须完整、不泄漏、不重复。"""
    body = "The final answer. " * 500
    adapter = _ChunkAdapter([DECL + "\n" + body])
    loop, result = await _run(adapter, turn_id="r6_long_en")
    events = _non_empty(_events(loop))
    assert result.final_content == body, (len(result.final_content), len(body))
    assert DECL not in _joined(events), "声明泄漏"
    assert _interims(events) == {False}, sorted(_interims(events))
    assert adapter.calls == 1, adapter.calls


# ---- 整段响应档（不支持流式）：同一契约 ---------------------------------------------


async def test_whole_response_mode_keeps_same_role_and_text():
    adapter = _ChunkAdapter([FULL], streaming=False)
    adapter.supports_stream = False
    loop, result = await _run(adapter, turn_id="r6_whole")
    events = _non_empty(_events(loop))
    assert result.final_content == BODY, result.final_content[:120]
    assert DECL not in _joined(events), ("整段响应档泄漏了声明", _joined(events)[:120])
    assert _interims(events) == {False}, sorted(_interims(events))
    assert adapter.calls == 1, adapter.calls


async def test_stream_break_keeps_chunk_invariance():
    """断流（没有 done）：不得执行任何工具、不得把半截当完整回答、正文如实保留。"""

    class _BreakAdapter(_ChunkAdapter):
        async def stream(self, messages, tools, **kwargs):  # noqa: ANN001
            self.calls += 1
            yield StreamDelta(kind=STREAM_TEXT, text=DECL + "\n第一段正文。")
            raise RuntimeError("受控错误：断流（没有 done）")

    adapter = _BreakAdapter([])
    registry, tool = _registry()
    loop = AgentLoop(adapter, registry, EventBus(), turn_id="r6_break")
    # 流被中断/出错时 loop 如实抛出（契约：已确认文本保留，但不得执行任何调用）
    with pytest.raises(Exception) as raised:
        await asyncio.wait_for(loop.run("请回答我"), timeout=90)
    assert "受控错误" in str(raised.value) or "断流" in str(raised.value), str(raised.value)[:200]
    assert tool.seen == [], tool.seen
    joined = _joined(_non_empty(_events(loop)))
    assert "第一段正文。" in joined, ("断流后已确认的正文必须保留", joined[:120])
    assert DECL not in joined, ("断流后声明泄漏", joined[:120])


# ---- 问题三：未声明长正文（无工具）→ 只一次调用、完整一次交付、过程区无副本 ----------


@pytest.mark.parametrize(
    "label,text_factory",
    [
        ("ASCII 超过上限（字节=字符）", lambda n: ("A" * n)),
        ("中文超过上限（字符少、UTF-8 字节多）", lambda n: ("甲" * n)),
    ],
)
async def test_undeclared_long_body_is_delivered_once(label, text_factory):
    """未声明长正文（超内存上限）无工具调用 → **只一次调用**、完整一次交付、过程区无副本。"""
    n = LIMIT + 1024 if label.startswith("ASCII") else LIMIT // 2  # 中文按字节已超上限
    text = text_factory(n)
    utf8_bytes = len(text.encode("utf-8"))
    adapter = _ChunkAdapter([text])
    registry, _tool = _registry()
    loop, result = await _run(adapter, registry=registry, turn_id="r6_long_undeclared")
    events = _non_empty(_events(loop))
    process = [e for e in events if e.get("interim") is not False]
    answers = [e for e in events if e.get("interim") is False]
    assert adapter.calls == 1, (
        "%s：未声明长正文只应发生 1 次调用（基线：超限改判 interim → 兜底再生成一次）" % label,
        {"calls": adapter.calls, "chars": len(text), "utf8_bytes": utf8_bytes, "limit": LIMIT},
    )
    assert result.final_content == text, (
        "%s：长正文没有完整、原样地交付" % label,
        {"got_len": len(result.final_content), "want_len": len(text)},
    )
    assert not process, (
        "%s：长正文被放进了过程区（契约 §1.3：缓冲上限只管理资源，不决定角色）" % label,
        {"process_events": [str(e.get("content"))[:40] for e in process]},
    )
    assert answers, ("%s：长正文没有进正式回答区" % label)
    assert all(e.get("streaming") is False for e in answers), (
        "%s：未声明降级路径不得假装流式（应一次性交付）" % label,
        [e.get("streaming") for e in answers],
    )
    print(
        "[诊断] %s：字符=%d，UTF-8 字节=%d，上限=%d，调用=%d，交付长度=%d，过程区事件=%d"
        % (label, len(text), utf8_bytes, LIMIT, adapter.calls, len(result.final_content), len(process))
    )


async def test_undeclared_at_exact_limit_boundary_keeps_role():
    """等号边界（恰好 LIMIT 个字符）仍必须按未声明处理并完整交付。"""
    text = "B" * LIMIT
    adapter = _ChunkAdapter([text])
    loop, result = await _run(adapter, turn_id="r6_boundary")
    assert result.final_content == text, (len(result.final_content), LIMIT)
    assert adapter.calls == 1, adapter.calls
    assert _interims(_non_empty(_events(loop))) == {False}, sorted(_interims(_non_empty(_events(loop))))


async def test_long_undeclared_note_with_real_tool_call_keeps_note_and_runs_tool(provider=None):
    """长说明 + 真实工具调用：工具必须执行，说明完整进过程区（不丢字）。"""
    note = "我先说明一下：" + ("细节 " * (LIMIT // 10 + 500))
    adapter = _ChunkAdapter([note + "\n"], streaming=False)
    adapter.supports_stream = False

    class _ToolAdapter(_ChunkAdapter):
        #: 走整段响应用例：必须显式关掉流式（否则 loop 走 stream()，永远拿不到 tool_calls）
        supports_stream = False

        def __init__(self, chunks):  # noqa: ANN001
            super().__init__(chunks)
            self.supports_stream = False
            self.rounds = 0

        async def complete(self, messages, tools, **kwargs):  # noqa: ANN001
            self.calls += 1
            self.rounds += 1
            if self.rounds == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=note,
                        tool_calls=[ToolCall(id="r6_t1", name="echo", arguments={"text": "long"})],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content=DECL + "\n" + BODY))

    adapter = _ToolAdapter([])
    adapter.supports_stream = False
    registry, tool = _registry()
    loop, result = await _run(adapter, registry=registry, turn_id="r6_long_note_tool")
    assert tool.seen == [{"text": "long"}], ("工具必须真的执行", tool.seen)
    process = [e for e in _non_empty(_events(loop)) if e.get("interim") is not False]
    assert any(note[:24] in str(e.get("content")) for e in process), (
        "长说明没有完整进过程区（丢字）",
        [str(e.get("content"))[:60] for e in process][:3],
    )
    assert result.final_content == BODY, result.final_content[:120]
