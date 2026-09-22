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
