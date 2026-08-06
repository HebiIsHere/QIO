from __future__ import annotations

import asyncio

from agent.memory.summary import (
    FragmentSummary,
    KnowledgeCandidate,
    KnowledgeExtraction,
    extract_knowledge_candidates,
    summarize_rolling,
    validate_knowledge_extraction,
    validate_summary_text,
)


def test_validate_extraction_ok():
    extraction, error = validate_knowledge_extraction(
        '```json\n{"candidates": ['
        '{"content": "用户偏好清淡饮食", "category": "user_profile", "attach": "user", "entity": null},'
        '{"content": "SQLite 适合本地存储", "category": "general_fact", "attach": "topic", "entity": null}'
        "]}\n```"
    )
    assert error is None
    assert extraction is not None
    assert len(extraction.candidates) == 2
    assert extraction.candidates[0].attach == "user"


def test_validate_extraction_rejects_bad_category():
    extraction, error = validate_knowledge_extraction(
        '{"candidates": [{"content": "x", "category": "bad_cat"}]}'
    )
    assert extraction is None
    assert error is not None


def test_validate_extraction_empty_ok():
    extraction, error = validate_knowledge_extraction('{"candidates": []}')
    assert error is None
    assert extraction is not None and extraction.candidates == []


def test_knowledge_candidate_defaults():
    cand = KnowledgeCandidate(content="事实")
    assert cand.category == "general_fact"
    assert cand.attach is None


def test_extract_knowledge_candidates_success():
    class FakeAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion

            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content='{"candidates": [{"content": "用户喜欢清淡", "category": "user_profile", "attach": "user"}]}',
                )
            )

    summary = FragmentSummary(title="t", summary="s", entities=[], keywords=[])
    extraction, error = asyncio.run(extract_knowledge_candidates(FakeAdapter(), summary))
    assert error is None
    assert extraction is not None and extraction.candidates[0].category == "user_profile"


def test_extract_failure_degrades():
    class BrokenAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            raise RuntimeError("boom")

    summary = FragmentSummary(title="t", summary="s", entities=[], keywords=[])
    extraction, error = asyncio.run(extract_knowledge_candidates(BrokenAdapter(), summary))
    assert extraction is None and error is not None


def test_summarize_rolling_merges_old():
    class FakeAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion

            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content='{"title": "t", "summary": "合并后的摘要", "entities": [], "keywords": []}',
                )
            )

    summary, error = asyncio.run(
        summarize_rolling(FakeAdapter(), "旧摘要", [{"role": "user", "content": "新内容"}])
    )
    assert error is None
    assert summary is not None and summary.summary == "合并后的摘要"


def test_rolling_failure_degrades():
    class BrokenAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            raise RuntimeError("boom")

    summary, error = asyncio.run(
        summarize_rolling(BrokenAdapter(), "旧", [{"role": "user", "content": "x"}])
    )
    assert summary is None and error is not None