# -*- coding: utf-8 -*-
"""派生可靠性：模型输出的「统一可修正层」+ 失败隔离 + 故障注入（B1~B6）。

规格（与任务书 B1~B6 对应）：

* 模型返回**太多 / 太长 / 重复 / 多余空白**属于**可修正输出**：本地按契约收敛后
  继续派生，不让第 51 个实体名、超长标题或一条坏候选把整条链一起归零；
* 只有**无法解析 / 类型完全错误 / 必需结构缺失 / 无法安全恢复**才算 schema failure；
* 可修正字段的小毛病**不得**让「摘要 → 索引 → 实体卡 → 知识」整条链归零；
* 真正的结构错误**必须可见**：失败原因进 trace、进派生任务状态（可读、可重试），
  不允许悄悄吞掉；
* 故障注入覆盖：title 2× 上限 / summary 2× 上限 / 100 entities / 100 keywords /
  50 knowledge candidates / 重复项 / 空白值 / 混合坏条目。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.model_output import (
    NOTE_DEDUPED,
    NOTE_DROPPED_BLANK,
    NOTE_DROPPED_INVALID_ITEM,
    NOTE_TRUNCATED_ITEMS,
    NOTE_TRUNCATED_TEXT,
)
from agent.memory.summary import (
    KNOWLEDGE_EXTRACTION_PROMPT,
    MAX_CANDIDATES,
    MAX_ENTITIES,
    MAX_KEYWORDS,
    MAX_SUMMARY,
    MAX_TITLE,
    SUMMARY_PROMPT,
    validate_knowledge,
    validate_knowledge_extraction,
    validate_summary,
    validate_summary_text,
    summarize_rolling,
)
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.trace.recorder import TurnTracer
from agent.trace.store import TraceStore

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _assert_raw_contract_rejects(payload: dict) -> None:
    """红/绿对照：同一份输入直接过契约（pydantic）是失败的。

    故障注入是真的：这些用例通过是因为「统一可修正层」把可修正输出收敛了，
    而不是因为契约本来就不拦。
    """
    from agent.memory.summary import FragmentSummary, KnowledgeExtraction

    model = KnowledgeExtraction if "candidates" in payload else FragmentSummary
    with pytest.raises(ValidationError):
        model(**payload)


def _summary_payload(**overrides) -> str:
    payload = {
        "title": "标题",
        "summary": "这是一段摘要",
        "entities": [],
        "keywords": [],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _candidate(content: str, **overrides) -> dict:
    item = {"content": content, "category": "general_fact", "attach": "topic", "entity": None}
    item.update(overrides)
    return item


def _knowledge_payload(count: int = 2) -> str:
    return json.dumps(
        {"candidates": [_candidate(f"事实{i}") for i in range(count)]},
        ensure_ascii=False,
    )


_ENTITY_PAYLOAD = {
    "entities": [
        {
            "name": "我家的大鹅",
            "aliases": [],
            "kind": "动物",
            "summary": "用户养的鹅",
            "attributes": [{"key": "状态", "value": "生病"}],
            "relations": [],
        }
    ]
}


class _ScriptedAdapter:
    """按提示词分派的假 provider：摘要 / 实体卡 / 知识抽取各给一份脚本化输出。

    字符串按原样返回（用来注入坏 JSON），其它类型走 json.dumps。
    """

    mode = "native"
    model = "fake-derivation"

    def __init__(self, *, summary=None, entities=None, knowledge=None) -> None:
        self.summary = _summary_payload() if summary is None else summary
        self.entities = {"entities": []} if entities is None else entities
        self.knowledge = _knowledge_payload() if knowledge is None else knowledge
        self.calls = {"summary": 0, "entities": 0, "knowledge": 0}

    async def complete(self, messages, tools, **kwargs) -> Completion:
        first = messages[0]
        prompt = first["content"] if isinstance(first, dict) else first.content
        if "summarizing a closed conversation fragment" in prompt:
            key, payload = "summary", self.summary
        elif "extracting durable knowledge" in prompt:
            key, payload = "knowledge", self.knowledge
        elif "你是实体提炼器" in prompt:
            key, payload = "entities", self.entities
        else:
            raise AssertionError(f"未知提示词：{prompt[:40]!r}")
        self.calls[key] += 1
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return Completion(message=ChatMessage(role="assistant", content=content))


def _app(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "derivation.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seal(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("可靠性").id
    ctx.memory.append_message(topic_id=topic, role="user", content="我家的鹅最近有点生病")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="记下了，我留意着")
    sealed = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity")
    assert sealed is not None
    return sealed


def _tracer(ctx: AppContext, turn_id: str) -> TurnTracer:
    store = TraceStore(ctx.conn, enabled=True)
    store.begin(turn_id)
    return TurnTracer(store, turn_id)


def _trace(ctx: AppContext, turn_id: str) -> dict:
    trace = TraceStore(ctx.conn).get(turn_id)
    assert trace is not None, "trace 行应当存在"
    return trace


def _count(ctx: AppContext, table: str) -> int:
    return int(ctx.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"])


def _summary_row(ctx: AppContext, fragment_id: str) -> dict:
    return dict(
        ctx.conn.execute(
            "SELECT summary, summary_version FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
    )


def _index_rows(ctx: AppContext, fragment_id: str) -> list[dict]:
    return [
        dict(row)
        for row in ctx.conn.execute(
            "SELECT title, keywords FROM memory_index WHERE fragment_id = ?",
            (fragment_id,),
        ).fetchall()
    ]


def _task_row(ctx: AppContext) -> dict:
    return dict(
        ctx.conn.execute(
            "SELECT state, attempts, last_error FROM derived_tasks"
        ).fetchone()
    )


# ---------------------------------------------------------------------------
# B1 / B2：title、summary 超长 —— 可修正，不整条失败
# ---------------------------------------------------------------------------


def test_title_twice_the_limit_is_repaired_not_rejected():
    _assert_raw_contract_rejects({"title": "标" * (MAX_TITLE * 2), "summary": "s"})
    outcome = validate_summary(_summary_payload(title="标" * (MAX_TITLE * 2)))
    assert outcome.error is None, outcome.error
    assert outcome.value is not None
    assert len(outcome.value.title) == MAX_TITLE
    assert any(
        n.field == "title" and n.code == NOTE_TRUNCATED_TEXT for n in outcome.notes
    ), outcome.notes


def test_summary_twice_the_limit_is_repaired_not_rejected():
    _assert_raw_contract_rejects({"title": "t", "summary": "摘" * (MAX_SUMMARY * 2)})
    outcome = validate_summary(_summary_payload(summary="摘" * (MAX_SUMMARY * 2)))
    assert outcome.error is None, outcome.error
    assert outcome.value is not None
    assert len(outcome.value.summary) == MAX_SUMMARY
    assert any(
        n.field == "summary" and n.code == NOTE_TRUNCATED_TEXT for n in outcome.notes
    ), outcome.notes


def test_prompt_states_the_length_and_count_limits():
    """上限要写进给模型的要求里：模型先自己收敛，本地截断只是兜底。"""
    for limit in (MAX_TITLE, MAX_SUMMARY, MAX_ENTITIES, MAX_KEYWORDS):
        assert str(limit) in SUMMARY_PROMPT, limit
    assert str(MAX_CANDIDATES) in KNOWLEDGE_EXTRACTION_PROMPT
    from agent.memory.summary import MAX_CANDIDATE_CONTENT

    assert str(MAX_CANDIDATE_CONTENT) in KNOWLEDGE_EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# B6 故障注入：数量 / 重复 / 空白 / 混合坏条目
# ---------------------------------------------------------------------------


def test_hundred_entities_and_keywords_are_capped_not_failed():
    """100 个实体 + 100 个关键词（已修能力的回归 + 上限仍生效）。"""
    _assert_raw_contract_rejects(
        {"title": "t", "summary": "s", "entities": [f"实体{i}" for i in range(100)]}
    )
    outcome = validate_summary(
        _summary_payload(
            entities=[f"实体{i}" for i in range(100)],
            keywords=[f"关键词{i}" for i in range(100)],
        )
    )
    assert outcome.error is None, outcome.error
    assert outcome.value is not None
    assert len(outcome.value.entities) == MAX_ENTITIES == 50
    assert len(outcome.value.keywords) == MAX_KEYWORDS == 50
    assert outcome.value.entities[:2] == ["实体0", "实体1"], "保序"
    codes = {n.field: n.code for n in outcome.notes}
    assert codes["entities"] == NOTE_TRUNCATED_ITEMS
    assert codes["keywords"] == NOTE_TRUNCATED_ITEMS


def test_fifty_knowledge_candidates_are_capped_not_failed():
    _assert_raw_contract_rejects(
        {"candidates": [_candidate(f"事实{i}") for i in range(50)]}
    )
    outcome = validate_knowledge(_knowledge_payload(count=50))
    assert outcome.error is None, outcome.error
    assert outcome.value is not None
    assert len(outcome.value.candidates) == MAX_CANDIDATES == 10
    assert outcome.value.candidates[0].content == "事实0"
    assert any(
        n.field == "candidates" and n.code == NOTE_TRUNCATED_ITEMS
        for n in outcome.notes
    ), outcome.notes


def test_duplicates_and_blank_values_are_dropped():
    outcome = validate_summary(
        _summary_payload(
            entities=["牛奶", "牛奶", "", "   ", " 咖啡 ", "咖啡"],
            keywords=["饮食", " 饮食 ", "饮食"],
        )
    )
    assert outcome.error is None, outcome.error
    assert outcome.value is not None
    assert outcome.value.entities == ["牛奶", "咖啡"]
    assert outcome.value.keywords == ["饮食"]
    codes = {n.code for n in outcome.notes}
    assert NOTE_DEDUPED in codes and NOTE_DROPPED_BLANK in codes, outcome.notes


def test_duplicate_knowledge_candidates_are_deduped():
    payload = {"candidates": [_candidate("同一条事实"), _candidate("同一条事实"), _candidate("另一条")]}
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    assert outcome.error is None, outcome.error
    assert [c.content for c in outcome.value.candidates] == ["同一条事实", "另一条"]
    assert any(n.code == NOTE_DEDUPED for n in outcome.notes), outcome.notes


def test_mixed_malformed_items_are_dropped_and_valid_ones_kept():
    """混合坏条目：只丢坏的那几条，好的照常保留，不整批失败。"""
    outcome = validate_summary(
        _summary_payload(entities=["牛奶", 42, None, {"name": "咖啡"}, "咖啡"])
    )
    assert outcome.error is None, outcome.error
    assert outcome.value is not None
    assert outcome.value.entities == ["牛奶", "咖啡"]
    assert any(n.code == NOTE_DROPPED_INVALID_ITEM for n in outcome.notes), outcome.notes


def test_mixed_malformed_candidates_keep_the_usable_ones():
    payload = {
        "candidates": [
            _candidate("好事实"),
            {"content": "", "category": "general_fact"},          # 空白 content
            {"category": "general_fact"},                          # 缺 content
            "不是对象",                                             # 类型无效
            {"content": "坏类别", "category": "not_a_category"},    # 枚举不在契约内
            {"content": 123, "category": "general_fact"},          # content 类型无效
        ]
    }
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    assert outcome.error is None, outcome.error
    assert [c.content for c in outcome.value.candidates] == ["好事实"]
    assert any(n.code == NOTE_DROPPED_INVALID_ITEM for n in outcome.notes), outcome.notes


def test_over_long_candidate_content_and_entity_are_truncated():
    from agent.memory.summary import MAX_CANDIDATE_CONTENT, MAX_ENTITY_NAME

    payload = {
        "candidates": [
            _candidate("事" * (MAX_CANDIDATE_CONTENT * 2), attach="entity", entity="名" * (MAX_ENTITY_NAME * 2))
        ]
    }
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    assert outcome.error is None, outcome.error
    cand = outcome.value.candidates[0]
    assert len(cand.content) == MAX_CANDIDATE_CONTENT
    assert len(cand.entity) == MAX_ENTITY_NAME


def test_null_literal_optional_fields_are_treated_as_empty():
    """模型把 JSON null 写成字符串 "null" 是常见形态，不该让整次抽取失败。"""
    payload = {
        "candidates": [
            _candidate("事实", attach="null", entity="None"),
        ]
    }
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    assert outcome.error is None, outcome.error
    cand = outcome.value.candidates[0]
    assert cand.attach is None and cand.entity is None


# ---------------------------------------------------------------------------
# B4 的边界：不可修复的输出仍然明确失败，原因可读
# ---------------------------------------------------------------------------


def test_unparseable_output_fails_with_a_readable_reason():
    outcome = validate_summary("完全不是 JSON")
    assert outcome.value is None
    assert outcome.error and "JSON" in outcome.error, outcome.error


def test_non_object_top_level_fails():
    outcome = validate_summary("[1, 2, 3]")
    assert outcome.value is None
    assert "顶层不是 JSON 对象" in outcome.error, outcome.error


def test_missing_required_field_fails():
    outcome = validate_summary('{"title": "只有标题"}')
    assert outcome.value is None
    assert "缺少必需字段 summary" in outcome.error, outcome.error


def test_blank_required_field_fails():
    outcome = validate_summary(_summary_payload(title="   "))
    assert outcome.value is None
    assert "title" in outcome.error and "为空" in outcome.error, outcome.error


def test_wrong_container_type_fails():
    outcome = validate_summary(_summary_payload(entities="牛奶,咖啡"))
    assert outcome.value is None
    assert "类型错误" in outcome.error and "entities" in outcome.error, outcome.error


def test_all_items_of_wrong_type_fails_instead_of_silently_emptying():
    """列表里有内容却一条都不符合类型 = 类型完全错误，不能悄悄变成空列表。"""
    outcome = validate_summary(_summary_payload(entities=[1, 2, 3]))
    assert outcome.value is None
    assert "类型完全错误" in outcome.error, outcome.error

    knowledge = validate_knowledge(json.dumps({"candidates": ["a", "b"]}))
    assert knowledge.value is None
    assert "类型完全错误" in knowledge.error, knowledge.error


def test_all_invalid_candidates_fail_instead_of_silent_empty():
    """列表非空但一条都没保住 = 无法安全恢复：不能伪装成「没有知识」。"""
    payload = {"candidates": [{"content": ""}, {"content": 123}]}
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    assert outcome.value is None
    assert "无法安全恢复" in outcome.error, outcome.error


def test_empty_candidates_array_is_still_legal():
    outcome = validate_knowledge('{"candidates": []}')
    assert outcome.error is None, outcome.error
    assert outcome.value is not None and outcome.value.candidates == []


def test_compat_entrypoints_keep_the_old_shape():
    """老调用方拿到的仍然是 (值, 失败原因)。"""
    value, error = validate_summary_text(_summary_payload(title="标" * (MAX_TITLE * 2)))
    assert error is None and value is not None and len(value.title) == MAX_TITLE

    extraction, error = validate_knowledge_extraction(_knowledge_payload(count=50))
    assert error is None and extraction is not None
    assert len(extraction.candidates) == MAX_CANDIDATES

    broken, error = validate_summary_text("{坏 JSON")
    assert broken is None and error


def test_rolling_summary_over_long_title_is_repaired():
    class RollingAdapter:
        mode = "native"
        model = "fake-rolling"

        async def complete(self, messages, tools, **kwargs):
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=json.dumps(
                        {"title": "标" * (MAX_TITLE * 2), "summary": "合并后的摘要"},
                        ensure_ascii=False,
                    ),
                )
            )

    summary, error = asyncio.run(
        summarize_rolling(RollingAdapter(), "旧摘要", [{"role": "user", "content": "新内容"}])
    )
    assert error is None, error
    assert summary is not None and len(summary.title) == MAX_TITLE
    assert summary.summary == "合并后的摘要"


# ---------------------------------------------------------------------------
# B5：整链失败隔离 + 修正可见 + 真失败可见
# ---------------------------------------------------------------------------


def test_over_long_title_no_longer_zeroes_the_derivation_chain(tmp_path: Path):
    """title 2× 上限：摘要 / 索引 / 实体卡 / 知识 全部照常产出。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    adapter = _ScriptedAdapter(
        summary={
            "title": "标" * (MAX_TITLE * 2),
            "summary": "这是一段摘要",
            "entities": [],
            "keywords": [],
        },
        entities=_ENTITY_PAYLOAD,
    )

    done = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert done == 1, "派生任务应当完成，而不是失败重试"
    assert _summary_row(ctx, sealed.id)["summary"] == "这是一段摘要"
    rows = _index_rows(ctx, sealed.id)
    assert len(rows) == 1, "索引要一起生成"
    assert len(rows[0]["title"]) == MAX_TITLE, "索引里的标题是截断后的"
    assert _count(ctx, "entity_cards") == 1, "实体卡要一起生成"
    assert _count(ctx, "knowledge") == 2, "知识条目要一起生成"
    assert _task_row(ctx)["state"] == "completed"


def test_over_long_summary_no_longer_zeroes_the_derivation_chain(tmp_path: Path):
    """summary 2× 上限：同上，只截断，不整条失败。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    adapter = _ScriptedAdapter(
        summary={
            "title": "标题",
            "summary": "摘" * (MAX_SUMMARY * 2),
            "entities": [],
            "keywords": [],
        },
        entities=_ENTITY_PAYLOAD,
    )

    done = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert done == 1
    assert len(_summary_row(ctx, sealed.id)["summary"]) == MAX_SUMMARY
    assert len(_index_rows(ctx, sealed.id)) == 1
    assert _count(ctx, "entity_cards") == 1
    assert _count(ctx, "knowledge") == 2
    assert _task_row(ctx)["state"] == "completed"


def test_fifty_candidates_still_produce_knowledge(tmp_path: Path):
    """50 个知识候选：截断到上限后照常入库，而不是一条都没有。"""
    ctx = _app(tmp_path)
    _seal(ctx)
    adapter = _ScriptedAdapter(knowledge=_knowledge_payload(count=50))

    done = asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5))

    assert done == 1
    assert _count(ctx, "knowledge") == MAX_CANDIDATES
    assert _task_row(ctx)["state"] == "completed"


def test_repairs_are_recorded_in_trace(tmp_path: Path):
    """本地修正必须可见：trace 的 writes 里能读到改了什么。"""
    ctx = _app(tmp_path)
    _seal(ctx)
    tracer = _tracer(ctx, "turn_repairs")
    adapter = _ScriptedAdapter(
        summary={
            "title": "标" * (MAX_TITLE * 2),
            "summary": "摘要",
            "entities": [f"实体{i}" for i in range(100)],
            "keywords": [],
        }
    )

    asyncio.run(ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5, tracer=tracer))

    writes = _trace(ctx, "turn_repairs")["writes"]
    lines = [line for line in writes.get("derivation_repairs", [])]
    assert lines, f"修正没有留痕：{writes}"
    joined = " ".join(lines)
    assert "title:truncated_text" in joined, joined
    assert "entities:truncated_items" in joined, joined


def test_structurally_broken_summary_fails_visibly_and_isolates_entity_cards(
    tmp_path: Path,
):
    """无法解析的摘要：任务失败原因可读、进 trace，但实体卡仍然产出（失败隔离）。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    tracer = _tracer(ctx, "turn_broken")
    adapter = _ScriptedAdapter(summary="{这不是 JSON", entities=_ENTITY_PAYLOAD)

    done = asyncio.run(
        ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5, tracer=tracer)
    )

    assert done == 0, "结构错误不能被当成成功"
    task = _task_row(ctx)
    assert task["state"] == "failed" and task["attempts"] == 1
    assert task["last_error"] and "JSON" in task["last_error"], task["last_error"]
    assert _summary_row(ctx, sealed.id)["summary"] in (None, ""), "不得伪造摘要"
    assert _index_rows(ctx, sealed.id) == [], "不得留下索引"
    codes = [w["code"] for w in _trace(ctx, "turn_broken")["warnings"]]
    assert "summary_derivation_failed" in codes, codes
    assert _count(ctx, "entity_cards") == 1, "实体卡只依赖原文，不该被摘要失败带走"
    assert _count(ctx, "knowledge") == 0, "知识依赖摘要，摘要失败时确实没有"


def test_knowledge_schema_failure_is_recorded_and_keeps_summary(tmp_path: Path):
    """知识抽取结构性失败：摘要与索引仍完成，但失败原因必须进 trace。"""
    ctx = _app(tmp_path)
    sealed = _seal(ctx)
    tracer = _tracer(ctx, "turn_knowledge_bad")
    adapter = _ScriptedAdapter(
        knowledge='{"candidates": [{"content": ""}, {"content": 1}]}',
    )

    done = asyncio.run(
        ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5, tracer=tracer)
    )

    assert done == 1, "知识抽取失败不应把已完成的摘要任务判失败"
    assert _summary_row(ctx, sealed.id)["summary"] == "这是一段摘要"
    assert len(_index_rows(ctx, sealed.id)) == 1
    assert _count(ctx, "knowledge") == 0
    warnings = _trace(ctx, "turn_knowledge_bad")["warnings"]
    assert any(w["code"] == "knowledge_extraction_failed" for w in warnings), warnings
    message = [w["message"] for w in warnings if w["code"] == "knowledge_extraction_failed"][0]
    assert "无法安全恢复" in message, message


def test_knowledge_failure_reason_is_readable_without_model_text(tmp_path: Path):
    """失败原因里不夹带模型原文（新输出路径的脱敏与可读性要求）。"""
    ctx = _app(tmp_path)
    _seal(ctx)
    tracer = _tracer(ctx, "turn_knowledge_garbage")
    adapter = _ScriptedAdapter(knowledge="sk-secret-abcdef123456 不是 JSON")

    asyncio.run(
        ctx.memory_lifecycle.drain_derived_tasks(adapter, limit=5, tracer=tracer)
    )

    warnings = _trace(ctx, "turn_knowledge_garbage")["warnings"]
    message = [w["message"] for w in warnings if w["code"] == "knowledge_extraction_failed"][0]
    assert "JSON" in message, message


@pytest.mark.parametrize(
    "bad_input",
    [
        "{不是 JSON",
        "[1, 2]",
        '{"summary": "缺标题"}',
        '{"title": "", "summary": ""}',
    ],
)
def test_unrecoverable_summary_outputs_all_fail_with_a_reason(bad_input: str):
    outcome = validate_summary(bad_input)
    assert outcome.value is None
    assert outcome.error and len(outcome.error) > 6, outcome.error
