r"""D 独立验证（R5 问题二）：内容角色协议 —— **同一个答案不得生成两次、不得显示两份**。

契约来源：docs/plans/2026-10-07-final-convergence.md §1.1（冻结）+ §3。
只依据用户可见规则：

1. 模型给最终回答时，正文**以 [[QIO:ANSWER]] 开头**（大小写不敏感，声明本身不展示）；
   声明之后的所有正文**实时**进正式回答区（interim=false, streaming=true）；
2. 缓冲与声明前缀不再匹配 → 该调用是**工作调用**：正文实时进过程区（interim=true）；
3. **未声明**：正文先不展示（有界缓冲 ≤64 KB）；调用结束有工具调用 → 放行到过程区；
   无工具调用 → **一次性**交付到正式回答区（不重新生成、不搬动、过程区不留副本）；
4. 声明之后的迟到工具调用：不执行 + 可见 WARNING；
5. 冲突/非法（不在开头、被拆坏、重复、出现在已放行正文之后）→ 按「未声明」处理；
6. 兜底：整轮完全没有回答内容时，最多**一次** tools=[] 调用。

**当前反例（基线 84f79a2 必须红）**：工作调用（带工具）直接写出完整答案时，基线把正文放进过程区，
**再发一次 tools=[] 的回答调用重写一遍**（core/loop.py:1093-1109）→ 同一个答案生成两次、
过程区与回答区各显示一份。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r5_answer_duplication_verify.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from pathlib import Path

import pytest

from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSWER_MARK = "[[QIO:ANSWER]]"


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
        base_url="http://127.0.0.1:%d/v1" % server.server_port, api_key="sk-r5-fake-0001"
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


def _warnings(loop: AgentLoop) -> list[dict]:
    return [e.data for e in loop.bus._history if e.type.value == "WARNING"]


def _requests(provider) -> list[dict]:
    """provider 台账：这一轮到底发起了几次调用、每次吃到的脚本步骤是什么。"""
    return [
        {"step": e.get("step_kind"), "tools": e.get("tool_count"), "msgs": e.get("message_count")}
        for e in (getattr(provider, "log", []) or [])
    ]


def _texts(events: list[dict], *, interim: bool | None = None) -> list[str]:
    out = []
    for event in events:
        content = str(event.get("content") or "")
        if not content.strip():
            continue
        # 修：原写法 (event.get("interim") is False) is not interim 对 interim=False 会算出 True，
        # 把**正是要找的事件**跳过（Lead 2026-10-07 用合成事件复现）。按语义用真值比较：
        if interim is not None and bool(event.get("interim")) is not interim:
            continue
        out.append(content)
    return out


def _occurrences(haystack: str, needle: str) -> int:
    return haystack.count(needle) if needle else 0


# ---- 1. 当前反例：工作调用直接给出完整答案 → 不得再生成、过程区不得留副本 -------------


async def test_working_call_direct_answer_is_not_generated_twice(provider):
    """工作调用（带工具）直接写出完整答案：只交付一次，过程区不留副本，不再多一次生成调用。

    基线：正文进过程区 + 再发一次 tools=[] 调用重写 → 两次生成、两份显示（红）。
    """
    answer = "结论：这个问题的答案是 42。"
    provider.script.set([{"chunks": [answer]}, {"chunks": [answer]}])
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_dup_direct")
    result = await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    events = _assistant(loop)
    requests = _requests(provider)
    in_answer = [t for t in _texts(events, interim=False) if answer[:6] in t]
    in_process = [t for t in _texts(events, interim=True) if answer[:6] in t]

    assert in_answer, ("完整答案没有进正式回答区", [e.get("content") for e in events])
    assert not in_process, (
        "完整答案在过程区留了副本（同一个答案显示两份）",
        {"process": in_process, "requests": requests},
    )
    assert len(requests) == 1, (
        "为同一个答案又发起了一次生成调用（基线：工作调用 + tools=[] 回答调用）",
        {"requests": requests, "tool_calls": tool.seen},
    )
    assert _occurrences(result.final_content, answer[:6]) <= 1, result.final_content


# ---- 2. 合规直接回答：声明开头 → 真流式进正式回答区，声明本身不展示 -------------------


async def test_declared_direct_answer_streams_once_without_marker(provider):
    provider.script.set([{"chunks": [ANSWER_MARK + "\n", "这是正式答案。", "第二段。"], "chunk_delay_ms": 20}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_declared_direct")
    result = await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    events = _assistant(loop)
    requests = _requests(provider)
    shown = "\n".join(str(e.get("content") or "") for e in events)
    assert "这是正式答案。" in shown, ("声明之后的正文没有展示", shown[:200])
    assert ANSWER_MARK not in shown, ("角色声明本身被展示了（契约 §1.1：声明不作为正文展示）", shown[:200])
    assert any(e.get("interim") is False and e.get("streaming") is True for e in events), (
        "声明之后的正文没有以流式增量进正式回答区",
        [e for e in events if e.get("interim") is False][:3],
    )
    assert len(requests) == 1, ("合规直接回答只需要一次调用", requests)
    assert "这是正式答案。" in result.final_content


# ---- 3. 声明被拆成多个分块 / 大小写不敏感 -------------------------------------------


@pytest.mark.parametrize(
    "chunks",
    [
        ["[[QIO", ":ANSWER]]", "拆开的声明之后是正文。"],
        ["[[qio:answer]]\n", "小写声明之后是正文。"],
    ],
)
async def test_split_or_lowercase_declaration_still_recognised(provider, chunks):
    provider.script.set([{"chunks": chunks, "chunk_delay_ms": 10}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_declared_split")
    await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    shown = "\n".join(str(e.get("content") or "") for e in _assistant(loop))
    assert "之后是正文。" in shown, ("拆开/小写的声明没有被识别成回答调用", shown[:200])
    assert "[[QIO" not in shown and "[[qio" not in shown, ("声明片段被展示了", shown[:200])


# ---- 4. 非法/冲突声明 → 按「未声明」处理（不猜、不改写、不重发） ---------------------


@pytest.mark.parametrize(
    "chunks",
    [
        ["先说一句。", "[[QIO:ANSWER]]", "声明不在开头。"],
        ["[[QIO:ANSWER]][[QIO:ANSWER]]", "重复声明。"],
    ],
)
async def test_illegal_declaration_falls_back_without_duplication(provider, chunks):
    provider.script.set([{"chunks": chunks, "chunk_delay_ms": 10}])
    registry, _tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_declared_illegal")
    await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    events = _assistant(loop)
    joined = "\n".join(str(e.get("content") or "") for e in events)
    body = "声明不在开头。" if "声明不在开头。" in joined else "重复声明。"
    assert body in joined, ("非法声明把正文弄丢了", joined[:200])
    assert joined.count(body) <= 2, ("同一个答案出现了多份", joined[:300])


# ---- 5. 迟到工具调用：不执行 + 可见 WARNING -----------------------------------------


async def test_late_tool_call_after_declaration_warns_and_is_not_executed(provider):
    provider.script.set(
        [
            {
                "chunks": [ANSWER_MARK + "\n", "答案先到。"],
                "chunk_delay_ms": 10,
                "tool_chunks": [{"id": "r5_late", "name": "echo", "args_fragments": ['{"text": "late"}']}],
            }
        ]
    )
    registry, tool = _registry()
    loop = AgentLoop(_native_adapter(provider), registry, EventBus(), turn_id="r5_declared_late_tool")
    await asyncio.wait_for(loop.run("回答我"), timeout=60)

    assert tool.seen == [], ("已声明回答之后的工具调用不得执行", tool.seen)
    warnings = _warnings(loop)
    assert warnings, ("迟到工具调用必须给一条可见警告（契约 §1.1 第 4 条）", [w for w in warnings])


# ---- 6. text 兼容档：一次交付、不重复 ------------------------------------------------


class _TextAdapter:
    """声明不支持流式的兼容档（text 模式），只回一条完整正文。"""

    mode = "text"
    model = "r5-text"
    supports_stream = False

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=self.text))


async def test_text_compat_adapter_delivers_answer_once():
    answer = "text 兼容档的完整答案。"
    adapter = _TextAdapter(answer)
    registry, _tool = _registry()
    loop = AgentLoop(adapter, registry, EventBus(), turn_id="r5_text_compat")
    result = await asyncio.wait_for(loop.run("直接回答我"), timeout=60)

    events = _assistant(loop)
    joined = "\n".join(str(e.get("content") or "") for e in events)
    assert answer in joined, ("兼容档的正文没有交付", joined[:200])
    assert adapter.calls == 1, ("兼容档也被要求生成第二次", adapter.calls)
    assert result.final_content.count(answer) == 1, result.final_content
