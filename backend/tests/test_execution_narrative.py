"""Execution Narrative：模型文案与工具事实分离（spec 2026-09-22）。"""
from __future__ import annotations

from agent.core.narrative import (
    NARRATIVE_KEY,
    Narrative,
    narrative_event_payload,
    parse_narrative,
    split_narrative_arguments,
)


def test_parse_narrative_accepts_known_kinds():
    out = parse_narrative({"kind": "announce", "text": "  我先确认审批链路。  "})
    assert out == Narrative(kind="announce", text="我先确认审批链路。")


def test_parse_narrative_rejects_unknown_kind_and_empty_text():
    assert parse_narrative({"kind": "speak", "text": "x"}) is None
    assert parse_narrative({"kind": "announce", "text": "   "}) is None
    assert parse_narrative("not-a-dict") is None
    assert parse_narrative(None) is None


def test_parse_narrative_keeps_explanation_only():
    out = parse_narrative({"explanation": "为了写入叙事记录，需要新增一个模块文件。"})
    assert out is not None
    assert out.silent is True
    assert out.text == ""
    assert out.explanation.startswith("为了写入叙事记录")


def test_parse_narrative_drops_unknown_keys_and_caps_length():
    out = parse_narrative(
        {
            "kind": "progress",
            "text": "甲" * 400,
            "explanation": "乙" * 500,
            "risk": "danger",
            "capabilities": ["写入文件：是"],
            "text_override": "骗你的",
        }
    )
    assert out is not None
    assert len(out.text) <= 120
    assert len(out.explanation) <= 200
    assert not hasattr(out, "risk")


def test_parse_narrative_redacts_secret_shaped_text():
    out = parse_narrative({"kind": "warning", "text": "api_key=sk-abcdef123456"})
    assert out is not None
    assert "sk-abcdef123456" not in out.text


def test_split_narrative_arguments_removes_reserved_key():
    clean, raw = split_narrative_arguments(
        {"path": "a.txt", NARRATIVE_KEY: {"kind": "announce", "text": "先看文件"}}
    )
    assert clean == {"path": "a.txt"}
    assert raw == {"kind": "announce", "text": "先看文件"}
    assert split_narrative_arguments({"path": "a.txt"}) == ({"path": "a.txt"}, None)


def test_narrative_event_payload_carries_system_provenance():
    payload = narrative_event_payload(
        "msg_1",
        "turn_1",
        Narrative(kind="announce", text="先看审批链路"),
        tool="grep_search",
        call_id="call_a",
        call_ids=["call_a", "call_b"],
        created_at="2026-09-22T09:41:09+00:00",
    )
    assert payload["narrative_id"] == "msg_1"
    assert payload["tool"] == "grep_search"
    assert payload["call_ids"] == ["call_a", "call_b"]
    assert payload["text"] == "先看审批链路"


# ---- 适配器：把 _qio 剥离成 ToolCall.narrative ---------------------------------


def test_native_adapter_strips_narrative_from_arguments():
    from agent.adapters.native import NativeAdapter

    class _Fn:
        name = "fs_read"
        arguments = '{"path": "a.txt", "_qio": {"kind": "announce", "text": "先读它"}}'

    class _Call:
        id = "call_1"
        function = _Fn()

    class _Msg:
        content = None
        tool_calls = [_Call()]

    class _Choice:
        message = _Msg()
        finish_reason = "tool_calls"

    class _Raw:
        choices = [_Choice()]
        usage = None

    completion = NativeAdapter(client=None, model="m")._to_completion(_Raw())
    call = completion.tool_calls[0]
    assert call.arguments == {"path": "a.txt"}
    assert call.narrative == {"kind": "announce", "text": "先读它"}


def test_text_adapter_parses_narrative_from_json_block():
    from agent.adapters.text import TextAdapter

    adapter = TextAdapter(client=None, model="m")
    parsed = adapter._parse(
        '```json\n{"tool_calls": [{"name": "fs_read", "arguments": '
        '{"path": "a.txt", "_qio": {"kind": "progress", "text": "继续核对"}}}]}\n```'
    )
    assert parsed is not None
    raw = parsed["tool_calls"][0]["arguments"]
    clean, narrative = split_narrative_arguments(raw)
    assert clean == {"path": "a.txt"}
    assert narrative["kind"] == "progress"


def test_text_adapter_tool_call_carries_narrative():
    import asyncio

    from agent.adapters.text import TextAdapter

    class _Choice:
        message = type("M", (), {"content": '{"tool_calls": [{"name": "fs_read", "arguments": {"path": "a.txt", "_qio": {"kind": "announce", "text": "先读它"}}}]}'})()

    class _Raw:
        choices = [_Choice()]
        usage = None

    class _Client:
        class chat:  # noqa: N801 - 模拟 openai 客户端形状
            class completions:  # noqa: N801
                @staticmethod
                async def create(**kwargs):
                    return _Raw()

    adapter = TextAdapter(client=_Client(), model="m")
    completion = asyncio.run(adapter.complete([], []))
    call = completion.tool_calls[0]
    assert call.arguments == {"path": "a.txt"}
    assert call.narrative == {"kind": "announce", "text": "先读它"}


def test_registry_specs_declare_narrative_field():
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    class _Echo(Tool):
        name = "echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="ok")

    reg = ToolRegistry()
    reg.register(_Echo())
    spec = reg.specs()[0]
    assert NARRATIVE_KEY in spec.parameters["properties"]
    assert spec.parameters["required"] == ["q"]
