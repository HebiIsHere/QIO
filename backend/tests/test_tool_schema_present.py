"""输出 schema 校验 + 呈现（present_call/present_result）测试。"""
from __future__ import annotations

import asyncio
import json

from agent.adapters.base import ToolCall
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry
from agent.tools.schema import validate


class JsonTool(Tool):
    name = "json_out"
    description = "json"
    parameters = {"type": "object", "properties": {}}
    output_schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "score": {"type": "number"},
            "active": {"type": "boolean"},
        },
    }

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=json.dumps(self.payload, ensure_ascii=False))


class PresentTool(Tool):
    name = "present"
    description = "present"
    parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="raw content")

    def present_call(self, arguments):
        return {"title": f"查询「{arguments.get('q', '')}」"}

    def present_result(self, result):
        return {"status": "ok", "summary": "已找到 3 条相关记忆"}


def make_call(name: str = "echo", arguments: dict | None = None) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments or {})


# ---- 轻量校验器 -----------------------------------------------------------

def test_validate_object_ok():
    schema = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}, "n": {"type": "integer"}},
    }
    assert validate({"name": "a", "n": 1}, schema) == []


def test_validate_missing_required():
    schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
    errors = validate({}, schema)
    assert any("required" in e for e in errors)


def test_validate_wrong_type():
    assert validate(3, {"type": "string"}, "$") != []
    assert validate("3", {"type": "integer"}, "$") != []
    assert validate(3.5, {"type": "integer"}, "$") != []
    assert validate(True, {"type": "integer"}, "$") != []  # bool 不是 int
    assert validate("x", {"type": "array"}, "$") != []
    assert validate(1, {"type": "object"}, "$") != []


def test_validate_array_items():
    schema = {"type": "array", "items": {"type": "string"}}
    assert validate(["a", "b"], schema) == []
    assert validate(["a", 1], schema) != []


def test_validate_nested():
    schema = {
        "type": "object",
        "properties": {"inner": {"type": "object", "required": ["x"], "properties": {"x": {"type": "number"}}}},
    }
    assert validate({"inner": {"x": 1.5}}, schema) == []
    assert validate({"inner": {}}, schema) != []


def test_validate_no_schema_passes():
    assert validate({"anything": 1}, None) == []


# ---- 管线集成 -------------------------------------------------------------

async def test_output_schema_valid_passes():
    reg = ToolRegistry()
    reg.register(JsonTool({"name": "a", "age": 3, "tags": ["x"], "score": 0.5, "active": True}))
    result = await reg.execute(make_call("json_out"))
    assert result.ok


async def test_output_schema_invalid_fails():
    reg = ToolRegistry()
    reg.register(JsonTool({"name": "a"}))  # 缺 age
    result = await reg.execute(make_call("json_out"))
    assert not result.ok
    assert "不符合 schema" in result.error
    assert "age" in result.error


async def test_output_schema_non_json_content_fails():
    class PlainTool(Tool):
        name = "plain"
        description = "plain"
        parameters = {"type": "object", "properties": {}}
        output_schema = {"type": "object", "properties": {}}

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="not json")

    reg = ToolRegistry()
    reg.register(PlainTool())
    result = await reg.execute(make_call("plain"))
    assert not result.ok
    assert "不符合 schema" in result.error


async def test_presentation_reaches_tool_end():
    reg = ToolRegistry()
    reg.register(PresentTool())
    seen: list[dict] = []

    async def on_end(data):
        seen.append(data)

    reg.register_policy("tool/end", on_end)
    await reg.execute(make_call("present", {"q": "鹅"}))
    presentation = seen[0]["presentation"]
    # 工具自定义的展示信息优先保留；registry 只补中文标题与原始工具名
    assert presentation["title"] == "查询「鹅」"
    assert presentation["status"] == "ok"
    assert presentation["summary"] == "已找到 3 条相关记忆"
    assert presentation["tool"] == "present"


async def test_presentation_always_carries_display_title():
    """界面文案统一中文：即使工具没提供 presentation，也要带上展示标题与原始工具名。"""
    reg = ToolRegistry()
    reg.register(JsonTool({"name": "a", "age": 1}))
    seen: list[dict] = []

    def on_end(data):
        seen.append(data)

    reg.register_policy("tool/end", on_end)
    await reg.execute(make_call("json_out"))
    presentation = seen[0]["presentation"]
    assert presentation is not None
    assert presentation["title"]  # 已登记工具是中文名，未登记回落原始名
    assert presentation["tool"] == "json_out"


async def test_sse_tool_end_carries_presentation():
    """AgentLoop 把 tool/end 内部事件转发为 SSE TOOL_END，含 presentation。"""
    from agent.adapters.base import ChatMessage, Completion
    from agent.adapters.native import NativeAdapter

    class Scripted:
        mode = "native"
        model = "m"

        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[ToolCall(id="c1", name="present", arguments={"q": "鹅"})],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content="done"))

    reg = ToolRegistry()
    reg.register(PresentTool())
    bus = EventBus()
    loop = AgentLoop(Scripted(), reg, bus)
    collected: list[str] = []
    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)
    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)
    await loop.run("hi")
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    joined = "\n".join(collected)
    assert "event: TOOL_END" in joined
    assert "presentation" in joined
    assert "查询«鹅»" in joined or "查询「鹅」" in joined
