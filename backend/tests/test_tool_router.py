"""Tool routing: selective tool spec delivery per turn."""

from __future__ import annotations

import pytest

from agent.adapters.base import ToolSpec
from agent.services.tool_router import CORE_TOOLS, ToolRouter


def _specs() -> list[ToolSpec]:
    return [
        ToolSpec("memory_search", "检索历史记忆", {"type": "object", "properties": {}}),
        ToolSpec("switch_topic", "切换话题", {"type": "object", "properties": {}}),
        ToolSpec("create_topic", "创建话题", {"type": "object", "properties": {}}),
        ToolSpec("await_task", "等待子任务", {"type": "object", "properties": {}}),
        ToolSpec("read_task_result", "读取子任务结果", {"type": "object", "properties": {}}),
        ToolSpec("create_tool", "创建工具开发任务", {"type": "object", "properties": {}}),
        ToolSpec("dev_write_file", "写入工作区文件", {"type": "object", "properties": {}}),
        ToolSpec("weather_query", "查询天气", {"type": "object", "properties": {}}),
        ToolSpec("translate_text", "翻译文本", {"type": "object", "properties": {}}),
    ]


def test_core_tools_always_included():
    router = ToolRouter()
    specs = router.route("帮我查天气", _specs())
    names = [s.name for s in specs]
    for core in CORE_TOOLS:
        assert core in names


def test_route_ranks_by_similarity():
    router = ToolRouter()
    specs = router.route("今天天气怎么样", _specs())
    names = [s.name for s in specs]
    # weather_query 应排在最前面（核心工具之后）
    assert names.index("weather_query") < names.index("translate_text")


def test_route_top_n_limit():
    router = ToolRouter(top_n=6)
    specs = router.route("今天天气怎么样", _specs())
    assert len(specs) <= 6
    for core in CORE_TOOLS:
        assert any(s.name == core for s in specs)


def test_route_empty_query_fallback():
    router = ToolRouter()
    specs = router.route("", _specs())
    names = [s.name for s in specs]
    assert len(names) > 0
    for core in CORE_TOOLS:
        assert core in names


def test_route_no_embedding_uses_token_overlap(tmp_path):
    router = ToolRouter()
    specs = router.route("帮我开发一个工具计算两个数", _specs())
    names = [s.name for s in specs]
    # 开发类工具应靠前
    assert names.index("create_tool") < names.index("translate_text")


async def test_agent_loop_uses_tool_selector():
    import asyncio

    from agent.adapters.base import ChatMessage, Completion
    from agent.api.bus import EventBus
    from agent.core.loop import AgentLoop

    seen: list[list[str]] = []

    class Adapter:
        mode = "native"
        model = "m"

        async def complete(self, messages, tools, **kwargs):
            seen.append([t.name for t in tools])
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    class Registry:
        def specs(self):
            return _specs()

        async def execute(self, call):
            raise AssertionError("no tool calls expected")

    router = ToolRouter(top_n=6)
    loop = AgentLoop(
        Adapter(), Registry(), EventBus(),
        tool_selector=lambda q: router.route(q, Registry().specs()),
    )
    await loop.run("查一下今天天气")
    assert seen, "tools should be delivered"
    routed = seen[0]
    assert "weather_query" in routed
    assert len(routed) <= 6 < len(_specs())  # 子集
    for core in CORE_TOOLS:
        assert core in routed
