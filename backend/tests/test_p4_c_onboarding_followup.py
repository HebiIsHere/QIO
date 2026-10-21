# -*- coding: utf-8 -*-
"""阶段四 C：首次引导追问——最多两问 / 可空 / 跳过不阻塞 / 结构化无回答标记。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.services.followups import (
    MAX_QUESTIONS,
    onboarding_questions_schema,
    suggest_follow_ups,
    validate_questions,
)


class _StubAdapter:
    model = "stub-model"

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[list] = []

    async def complete(self, messages, tools=None, temperature=0.0):
        self.calls.append(messages)
        return SimpleNamespace(message=SimpleNamespace(content=self.payload))


def test_validate_keeps_at_most_two_and_allows_empty():
    questions, error = validate_questions(
        json.dumps({"questions": ["一?", "二?", "三?"]})
    )
    assert error is None
    assert questions == ["一?", "二?"]
    assert len(questions) == MAX_QUESTIONS
    empty, empty_error = validate_questions('{"questions": []}')
    assert empty == [] and empty_error is None


def test_validate_broken_output_means_no_questions():
    questions, error = validate_questions("我觉得可以问三个问题")
    assert questions == [] and error
    odd, odd_error = validate_questions('{"questions": ["好的", ""]}')
    assert odd == ["好的"] and odd_error is None


@pytest.mark.asyncio
async def test_structured_call_has_no_chat_answer_marker():
    adapter = _StubAdapter('{"questions": ["你希望我先讲逻辑还是先给代码？"]}')
    questions, error = await suggest_follow_ups(
        adapter, "讨论方案时希望先解释思路", language="zh"
    )
    assert error is None
    assert questions == ["你希望我先讲逻辑还是先给代码？"]
    system_text = adapter.calls[0][0].content
    user_text = adapter.calls[0][1].content
    assert "首次设置" in system_text
    assert '"questions"' in system_text
    assert "[[QIO:ANSWER]]" not in system_text
    data = json.loads(user_text)  # 独立 JSON 数据，不是聊天拼接
    assert data["description"] == "讨论方案时希望先解释思路"
    assert data["interface_language"] == "zh"


@pytest.mark.asyncio
async def test_nullable_and_non_blocking():
    assert await suggest_follow_ups(None, "最近在开发 QIO") == ([], None)
    assert await suggest_follow_ups(_StubAdapter('{"questions": ["x"]}'), "   ") == ([], None)

    class _Boom:
        async def complete(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("model down")

    questions, error = await suggest_follow_ups(_Boom(), "最近在开发 QIO")
    assert questions == []
    assert "model down" in (error or "")


def test_schema_limits_match_rules():
    schema = onboarding_questions_schema()
    assert schema["properties"]["questions"]["maxItems"] == MAX_QUESTIONS
