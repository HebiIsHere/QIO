"""C01：text 兼容档的请求结构 —— 工具请求与工具结果必须是合法的普通文本消息。

真实的坏行为（origin/main 基线）：``TextAdapter._to_text_messages`` 只是
``{"role": msg.role, "content": msg.content}`` 原样透传，于是

* 工具结果以 ``role="tool"`` 发出，却**没有** ``tool_call_id`` —— 这类请求会被
  普通 chat 协议拒绝（或行为未定义）；
* assistant 的工具调用信息（工具名 / 参数 / call_id）完全丢失，模型看不到自己
  刚刚请求过什么。

这里用脚本化文本模型跑**真实循环**（含工具执行），并用一个「本地严格请求校验
替身」逐条检查第二次请求的结构。

诚实边界：这是**本地严格请求校验替身验证**，不是对所有真实厂商端点的实测；
本地替身只按普通 chat 协议的角色/字段规则判定合法与否。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pytest

from agent.adapters.base import ChatMessage, ToolCall, ToolSpec
from agent.adapters.text import TextAdapter
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

TOOLS = [
    ToolSpec(
        name="echo",
        description="原样回显",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    )
]

# 普通 chat 协议允许的角色：text 档不得发送 role=tool（它需要 tool_call_id）。
ALLOWED_ROLES = ("system", "user", "assistant")


@dataclass
class FakeMessage:
    content: str | None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    usage: Any = None


class ScriptedTextClient:
    """脚本化文本模型：按顺序返回文本内容（最后一条会一直复用）。"""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    @property
    def chat(self) -> "ScriptedTextClient":
        return self

    @property
    def completions(self) -> "ScriptedTextClient":
        return self

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        content = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return FakeCompletion([FakeChoice(FakeMessage(content))])


class BoomTool(Tool):
    name = "boom"
    description = "总是失败的工具"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=False, error="磁盘已满：无法写入")


def tool_json(name: str, arguments: dict[str, Any]) -> str:
    import json

    return "```json\n" + json.dumps(
        {"tool_calls": [{"name": name, "arguments": arguments}]}, ensure_ascii=False
    ) + "\n```"


def multi_tool_json(calls: list[tuple[str, dict[str, Any]]]) -> str:
    import json

    return "```json\n" + json.dumps(
        {"tool_calls": [{"name": n, "arguments": a} for n, a in calls]}, ensure_ascii=False
    ) + "\n```"


def assert_legal_text_request(messages: list[dict[str, Any]]) -> None:
    """本地严格请求校验替身：只按普通 chat 协议的字段规则判定。

    判定规则（原缺陷正是踩了第一条）：

    * 角色必须是 system / user / assistant；
    * 每条消息的 content 必须是字符串；
    * 不得出现 role=tool 才需要的 ``tool_call_id``，也不得出现 text 档不支持的
      ``tool_calls`` 字段。
    """
    for index, msg in enumerate(messages):
        assert msg["role"] in ALLOWED_ROLES, f"第 {index} 条消息角色非法：{msg['role']}"
        assert isinstance(msg.get("content"), str), f"第 {index} 条消息 content 不是文本"
        assert "tool_call_id" not in msg, f"第 {index} 条消息带了 role=tool 专用字段"
        assert "tool_calls" not in msg, f"第 {index} 条消息带了 text 档不支持的字段"


def _make_loop(client: ScriptedTextClient, *, tools: list[Tool] | None = None):
    adapter = TextAdapter(client=client, model="fake-text")
    registry = ToolRegistry()
    for tool in tools or [EchoTool()]:
        registry.register(tool)
    bus = EventBus()
    return AgentLoop(adapter, registry, bus)


async def test_text_tool_call_round_trip_produces_a_legal_second_request():
    """C01 验收：假文本模型调用工具 → 真实循环执行 → 第二次请求结构合法且含正确结果。"""
    client = ScriptedTextClient([tool_json("echo", {"text": "你好世界"}), "最终回答"])
    loop = _make_loop(client)

    result = await loop.run("请回显")

    assert result.phase.value == "done"
    assert result.final_content == "最终回答"
    assert result.tool_calls_made == 1
    assert len(client.calls) == 2

    second = client.calls[1]["messages"]
    assert_legal_text_request(second)
    roles = [m["role"] for m in second]
    assert roles == ["system", "user", "assistant", "user"], roles

    assistant_text = second[2]["content"]
    tool_result_text = second[3]["content"]
    call_id = re.search(r"call_id=([A-Za-z0-9_]+)", assistant_text)
    assert call_id is not None, assistant_text
    assert "调用工具 echo" in assistant_text
    assert '"text"' in assistant_text and "你好世界" in assistant_text, "工具名与参数必须在请求里"

    assert "工具 echo 的结果" in tool_result_text
    assert f"call_id={call_id.group(1)}" in tool_result_text, "结果必须与对应调用关联"
    assert "你好世界" in tool_result_text, "工具的真实结果必须在请求里"

    # 工具定义仍然拼在 system prompt 里（text 档的唯一工具来源）
    assert "echo" in second[0]["content"]


async def test_multiple_tools_in_one_round_keep_order_and_correlation():
    """多工具：一次请求两个调用，结果逐条保留、名字与调用一一对应。"""
    client = ScriptedTextClient(
        [
            multi_tool_json([("echo", {"text": "甲"}), ("echo", {"text": "乙"})]),
            "最终回答",
        ]
    )
    loop = _make_loop(client)

    result = await loop.run("两个都回显")
    assert result.tool_calls_made == 2
    assert result.final_content == "最终回答"

    second = client.calls[1]["messages"]
    assert_legal_text_request(second)
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user", "user"]

    assistant_text = second[2]["content"]
    assert assistant_text.count("调用工具 echo") == 2
    ids = re.findall(r"call_id=([A-Za-z0-9_]+)", assistant_text)
    assert len(ids) == 2 and len(set(ids)) == 2

    first_result, second_result = second[3]["content"], second[4]["content"]
    assert "工具 echo 的结果" in first_result and "甲" in first_result
    assert "工具 echo 的结果" in second_result and "乙" in second_result
    assert ids[0] in first_result and ids[1] in second_result, "顺序必须稳定且一一对应"


async def test_multi_round_tools_accumulate_in_the_request():
    """多轮：第三次请求里两轮的工具结果都在，顺序稳定。"""
    client = ScriptedTextClient(
        [
            tool_json("echo", {"text": "第一轮"}),
            tool_json("echo", {"text": "第二轮"}),
            "最终回答",
        ]
    )
    loop = _make_loop(client)

    result = await loop.run("连着两轮")
    assert result.tool_calls_made == 2
    assert len(client.calls) == 3

    third = client.calls[2]["messages"]
    assert_legal_text_request(third)
    joined = "\n".join(m["content"] for m in third)
    assert joined.count("工具 echo 的结果") == 2
    assert joined.index("第一轮") < joined.index("第二轮")
    assert "最终回答" not in joined


async def test_failed_tool_result_is_kept_as_plain_text_not_dropped():
    """失败结果同样不能丢：必须以普通文本把失败原因交给模型。"""
    client = ScriptedTextClient([tool_json("boom", {}), "我知道失败了"])
    loop = _make_loop(client, tools=[BoomTool()])

    result = await loop.run("调一下会失败的工具")

    # 失败工具会让后端在答复后补一段「系统核对」事实说明（既有语义，与本项无关）
    assert (result.final_content or "").startswith("我知道失败了")
    second = client.calls[1]["messages"]
    assert_legal_text_request(second)
    result_text = second[-1]["content"]
    assert "工具 boom 的结果" in result_text
    assert "磁盘已满" in result_text, "失败原因必须如实进入下一次请求"


async def test_plain_text_answer_needs_no_tool_messages():
    """普通回答路径不受影响（没有工具消息时结构就是 system + user）。"""
    client = ScriptedTextClient(["直接回答", "直接回答"])
    loop = _make_loop(client)

    result = await loop.run("你好")
    assert result.final_content == "直接回答"
    assert [m["role"] for m in client.calls[0]["messages"]] == ["system", "user"]


def test_history_restoration_maps_tool_names_and_never_emits_role_tool():
    """历史恢复：含工具调用的历史要能完整、合法地重放。"""
    adapter = TextAdapter(client=ScriptedTextClient(["x", "x"]), model="fake-text")
    adapter.key_id = None

    history = [
        ChatMessage(role="user", content="帮我看看天气"),
        ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "上海"})],
        ),
        ChatMessage(role="tool", tool_call_id="call_1", content="晴，26 度"),
        ChatMessage(role="assistant", content="今天晴。"),
        ChatMessage(role="user", content="那明天呢？"),
    ]

    messages = adapter._to_text_messages(history, TOOLS)

    assert_legal_text_request(messages)
    assert len(messages) == len(history) + 1, "一条输入对应一条输出，不能丢消息"
    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert "调用工具 echo" in messages[2]["content"]
    assert '"上海"' in messages[2]["content"]
    assert "工具 echo 的结果" in messages[3]["content"]
    assert "晴，26 度" in messages[3]["content"]
    assert messages[4]["content"] == "今天晴。"
    assert messages[5]["content"] == "那明天呢？"


def test_tool_message_without_matching_call_keeps_content_and_is_legal():
    """关联缺失（call_id 没有对应调用）也必须合法，并且绝不丢内容。"""
    adapter = TextAdapter(client=ScriptedTextClient(["x", "x"]), model="fake-text")

    messages = adapter._to_text_messages(
        [ChatMessage(role="tool", tool_call_id="call_missing", content="孤立的结果")],
        TOOLS,
    )

    assert_legal_text_request(messages)
    assert len(messages) == 2
    assert "孤立的结果" in messages[1]["content"]
    assert messages[1]["role"] == "user"

    # 连 tool_call_id 都没有的 tool 消息（原缺陷的直接触发形态）也不能原样发出
    bare = adapter._to_text_messages([ChatMessage(role="tool", content="没有 id 的结果")], TOOLS)
    assert_legal_text_request(bare)
    assert "没有 id 的结果" in bare[1]["content"]


def test_empty_tool_result_is_preserved_with_an_explicit_placeholder():
    """空结果也要保留一条明确的占位，不能靠删消息让请求「看起来合法」。"""
    adapter = TextAdapter(client=ScriptedTextClient(["x", "x"]), model="fake-text")

    messages = adapter._to_text_messages(
        [
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={})],
            ),
            ChatMessage(role="tool", tool_call_id="call_1", content=""),
        ],
        TOOLS,
    )

    assert_legal_text_request(messages)
    assert len(messages) == 3
    assert "工具 echo 的结果" in messages[2]["content"]
    assert "（没有输出）" in messages[2]["content"]
