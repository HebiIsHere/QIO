# -*- coding: utf-8 -*-
"""Phase 1 / B：封块摘要完整覆盖（分段 + 合并 + coverage + 失败语义）。

验收点：
* 多消息：开头 / 中间 / 结尾信息都进入分段输入，最终摘要由 MERGE_SUMMARY_SYSTEM 合成；
* 单条超长消息：按可追溯分段切开（消息 id + 段偏移），不得只保留尾部；
* coverage 记录 input_version / 覆盖消息 id / 分段 / 块数 / complete；
* 任何分段失败：任务可重试、原文保留、不得写入 complete=true。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent import prompts
from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.summary import summarize_messages_outcome
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _payload(summary: str) -> str:
    return json.dumps(
        {"title": "标题", "summary": summary, "entities": [], "keywords": []},
        ensure_ascii=False,
    )


def _content(message) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(message.content or "")


class _ScriptedAdapter:
    """结构化摘要 / 合并 / 旧知识 / 旧实体各给一份脚本输出，并记录输入。"""

    mode = "native"
    model = "fake-p1b-summary"

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.calls: list[tuple[str, dict | None, str]] = []

    async def complete(self, messages, tools, **kwargs) -> Completion:
        texts = [_content(m) for m in messages]
        system = texts[0]
        prompt = "\n".join(texts)
        data = None
        if len(texts) > 1:
            data = json.loads(texts[1])
        self.calls.append((system, data, prompt))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("fake summary failure")
        if prompts.PROMPTS["MERGE_SUMMARY_SYSTEM"] in system:
            parts = [item["summary"] for item in (data or {}).get("summaries", [])]
            return Completion(
                message=ChatMessage(role="assistant", content=_payload("合并:" + "|".join(parts)))
            )
        if prompts.PROMPTS["CLOSED_SUMMARY_SYSTEM"] in system:
            ids = ",".join(
                str(item.get("message_id")) for item in (data or {}).get("messages", [])
            )
            return Completion(message=ChatMessage(role="assistant", content=_payload(ids)))
        if "extracting durable knowledge" in prompt:
            return Completion(
                message=ChatMessage(role="assistant", content='{"candidates": []}')
            )
        if "你是实体提炼器" in prompt:
            return Completion(
                message=ChatMessage(role="assistant", content='{"entities": []}')
            )
        raise AssertionError(f"未知提示词：{prompt[:60]!r}")


def _chunk_calls(adapter: _ScriptedAdapter) -> list[dict]:
    return [
        data for system, data, _ in adapter.calls
        if prompts.PROMPTS["CLOSED_SUMMARY_SYSTEM"] in system
    ]


def _merge_calls(adapter: _ScriptedAdapter) -> list[dict]:
    return [
        data for system, data, _ in adapter.calls
        if prompts.PROMPTS["MERGE_SUMMARY_SYSTEM"] in system
    ]


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "summary_coverage.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _fragment_meta(ctx: AppContext, fragment_id: str) -> dict:
    row = ctx.conn.execute(
        "SELECT meta FROM fragments WHERE id = ?", (fragment_id,)
    ).fetchone()
    return json.loads(row["meta"] or "{}")


# ---------------------------------------------------------------------------
# 多消息：开头 / 中间 / 结尾都进入分段输入
# ---------------------------------------------------------------------------


def test_multi_message_full_coverage_including_start_middle_end(monkeypatch):
    # 缩小块预算，强制 3 条消息分成多块（验证的是「按块覆盖」，不是调大输入）。
    monkeypatch.setattr("agent.memory.summary.SUMMARY_CHUNK_MAX_CHARS", 70)
    monkeypatch.setattr("agent.memory.summary.SUMMARY_CHUNK_MAX_TOKENS", 70)
    messages = [
        {"id": "m1", "role": "user", "content": "开头信息甲" + "啊" * 36},
        {"id": "m2", "role": "assistant", "content": "中间信息乙" + "哦" * 36},
        {"id": "m3", "role": "user", "content": "结尾信息丙" + "嗯" * 36},
    ]
    adapter = _ScriptedAdapter()
    run = asyncio.run(
        summarize_messages_outcome(adapter, messages, input_version=7)
    )

    assert run.outcome.value is not None, run.outcome.error
    assert run.coverage.complete is True
    assert run.coverage.input_version == 7
    assert run.coverage.message_count == 3
    assert list(run.coverage.covered_message_ids) == ["m1", "m2", "m3"]

    chunk_calls = _chunk_calls(adapter)
    merge_calls = _merge_calls(adapter)
    assert len(chunk_calls) >= 2, "长输入必须分成多块，而不是只取尾部"
    assert merge_calls, "多个分段必须用 MERGE_SUMMARY_SYSTEM 合成"
    assert run.coverage.chunk_count == len(chunk_calls)

    covered = [
        item["message_id"] for data in chunk_calls for item in data["messages"]
    ]
    assert covered == ["m1", "m2", "m3"], "每个消息都被某一段覆盖，且顺序不变"
    all_input = json.dumps(chunk_calls, ensure_ascii=False)
    for marker in ("开头信息甲", "中间信息乙", "结尾信息丙"):
        assert marker in all_input, f"{marker} 没有进入任何分段输入（只保留尾部？）"
    # 合并按顺序收到全部段摘要
    merge_input = merge_calls[0]
    assert len(merge_input["summaries"]) == run.coverage.chunk_count
    assert merge_input["summaries"][0]["summary"].startswith("m1")
    assert merge_input["summaries"][-1]["summary"].startswith("m3")


def test_oversize_single_message_is_split_into_traceable_segments(monkeypatch):
    monkeypatch.setattr("agent.memory.summary.SUMMARY_SEGMENT_MAX_CHARS", 50)
    monkeypatch.setattr("agent.memory.summary.SUMMARY_SEGMENT_MAX_TOKENS", 50)
    monkeypatch.setattr("agent.memory.summary.SUMMARY_CHUNK_MAX_CHARS", 50)
    monkeypatch.setattr("agent.memory.summary.SUMMARY_CHUNK_MAX_TOKENS", 50)
    content = "头" * 30 + "中" * 30 + "尾" * 30
    messages = [{"id": "big", "role": "user", "content": content}]
    adapter = _ScriptedAdapter()
    run = asyncio.run(summarize_messages_outcome(adapter, messages, input_version=1))

    assert run.outcome.value is not None, run.outcome.error
    assert run.coverage.complete is True
    segments = list(run.coverage.segments)
    assert len(segments) >= 2, "超长单条消息必须切段，而不是整条丢弃 / 只留尾部"
    assert segments[0]["message_id"] == "big"
    assert segments[0]["char_start"] == 0
    assert segments[-1]["char_end"] == len(content)
    for previous, current in zip(segments, segments[1:]):
        assert previous["char_end"] == current["char_start"], "分段必须连续、无空洞"
    joined = "".join(
        item["content"] for data in _chunk_calls(adapter) for item in data["messages"]
    )
    assert joined == content, "所有分段拼起来必须等于原文（完整覆盖）"


def test_chunk_failure_keeps_coverage_incomplete(monkeypatch):
    monkeypatch.setattr("agent.memory.summary.SUMMARY_CHUNK_MAX_CHARS", 40)
    monkeypatch.setattr("agent.memory.summary.SUMMARY_CHUNK_MAX_TOKENS", 40)
    messages = [
        {"id": "m1", "role": "user", "content": "第一段" + "啊" * 30},
        {"id": "m2", "role": "user", "content": "第二段" + "哦" * 30},
    ]
    adapter = _ScriptedAdapter(fail_after=1)
    run = asyncio.run(summarize_messages_outcome(adapter, messages, input_version=2))

    assert run.outcome.value is None
    assert run.outcome.error and "段摘要失败" in run.outcome.error, run.outcome.error
    assert run.coverage.complete is False, "失败时不得写 complete=true"
    assert run.coverage.message_count == 2


# ---------------------------------------------------------------------------
# 派生任务接线：coverage 落 fragments.meta；失败可重试、原文保留
# ---------------------------------------------------------------------------


def _seal_pair(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("摘要覆盖").id
    ctx.memory.append_message(topic_id=topic, role="user", content="开头信息：我家的鹅生病了")
    ctx.memory.append_message(
        topic_id=topic, role="assistant", content="中间信息：记下了，观察两天"
    )
    ctx.memory.append_message(topic_id=topic, role="user", content="结尾信息：今天好多了")
    ctx.memory.append_message(
        topic_id=topic, role="assistant", content="好的，继续留意"
    )
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


def test_summary_task_writes_coverage_into_fragment_meta(tmp_path: Path):
    ctx = _app(tmp_path)
    sealed = _seal_pair(ctx)
    adapter = _ScriptedAdapter()

    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    meta = _fragment_meta(ctx, sealed.id)
    coverage = meta.get("summary_coverage")
    assert coverage is not None, meta
    assert coverage["complete"] is True
    assert coverage["input_version"] == sealed.content_version
    assert coverage["chunk_count"] == len(_chunk_calls(adapter)) >= 1
    expected_ids = [
        row["id"]
        for row in ctx.conn.execute(
            "SELECT id FROM messages WHERE fragment_id = ? ORDER BY rowid",
            (sealed.id,),
        ).fetchall()
    ]
    assert coverage["covered_message_ids"] == expected_ids
    # 原文始终保留（回查能力不变）
    assert ctx.memory_lifecycle.fragments.message_count(sealed.id) == len(expected_ids)


def test_summary_failure_is_retryable_and_never_marks_complete(tmp_path: Path):
    ctx = _app(tmp_path)
    sealed = _seal_pair(ctx)
    broken = _ScriptedAdapter(fail_after=0)

    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(broken, limit=5))

    task = ctx.conn.execute(
        "SELECT state, attempts, last_error FROM derived_tasks WHERE kind = 'summary'"
    ).fetchone()
    assert task["state"] == "failed" and int(task["attempts"]) >= 1
    row = ctx.conn.execute(
        "SELECT summary FROM fragments WHERE id = ?", (sealed.id,)
    ).fetchone()
    assert row["summary"] in (None, ""), "失败不得伪造摘要"
    meta = _fragment_meta(ctx, sealed.id)
    assert "summary_coverage" not in meta or meta["summary_coverage"].get("complete") is False
    assert ctx.memory_lifecycle.fragments.message_count(sealed.id) == 4

    # 修好故障、把退避拨回过去：同一条任务重试成功
    ctx.conn.execute("UPDATE derived_tasks SET run_after = NULL WHERE kind = 'summary'")
    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(_ScriptedAdapter(), limit=5))
    task_after = ctx.conn.execute(
        "SELECT state FROM derived_tasks WHERE kind = 'summary'"
    ).fetchone()
    assert task_after["state"] == "completed"
    coverage = _fragment_meta(ctx, sealed.id)["summary_coverage"]
    assert coverage["complete"] is True
    assert ctx.memory_lifecycle.fragments.message_count(sealed.id) == 4, "重试不得丢原文"
