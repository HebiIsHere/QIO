"""TextAdapter 的成功率统计必须只反映**协议格式损坏**。

历史缺陷：`ok = parsed is not None` —— 把「合法普通文本回答」也算成解析失败。
于是 provider 健康度 / 自动降级会拿一份错误的数据做判断：
一个正常聊天、只是没调工具的模型会被判成「端点不支持文本协议」。

正确语义：
    合法 Tool JSON → success
    合法普通文本   → success
    协议格式损坏   → failure
"""

from __future__ import annotations

from typing import Any

from agent.adapters.base import ChatMessage, ToolSpec
from agent.adapters.text import TextAdapter

TOOLS = [
    ToolSpec(
        name="get_weather",
        description="Get weather",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    )
]


class _Client:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    @property
    def chat(self) -> "_Client":
        return self

    @property
    def completions(self) -> "_Client":
        return self

    async def create(self, **kwargs: Any):
        content = self._responses.pop(0)
        message = type("Message", (), {"content": content, "tool_calls": None})()
        choice = type("Choice", (), {"message": message})()
        return type("Completion", (), {"choices": [choice], "usage": None})()


async def _run(responses: list[str]) -> TextAdapter:
    adapter = TextAdapter(client=_Client(responses), model="m1")
    for _ in responses:
        await adapter.complete([ChatMessage(role="user", content="hi")], TOOLS)
    return adapter


async def test_plain_text_answer_is_success():
    adapter = await _run(["今天天气不错，我没有需要调用的工具。"])
    assert adapter.success_rate == 1.0


async def test_empty_answer_is_success():
    adapter = await _run([""])
    assert adapter.success_rate == 1.0


async def test_valid_tool_json_is_success():
    adapter = await _run(['{"tool_calls": [{"name": "get_weather", "arguments": {"city": "上海"}}]}'])
    assert adapter.success_rate == 1.0


async def test_malformed_protocol_attempt_is_failure():
    # 想按协议回（提到 tool_calls）但 JSON 坏了 → 这才是真正的协议失败
    adapter = await _run(['{"tool_calls": [{"name": "get_weather", '])
    assert adapter.success_rate == 0.0


async def test_broken_json_fence_is_failure():
    adapter = await _run(["```json\n{\"tool_calls\": [}\n```"])
    assert adapter.success_rate == 0.0


async def test_health_signal_only_reflects_protocol_damage():
    adapter = await _run(
        [
            "普通回答一",                                  # 合法文本
            '{"tool_calls": []}',                          # 合法协议
            "```json\n{oops\n```",                         # 协议损坏
            "普通回答二",                                  # 合法文本
        ]
    )
    # 4 次尝试、1 次协议损坏 → 0.75；旧语义会算成 0.5（两次纯文本被误判）
    assert adapter.success_rate == 0.75
