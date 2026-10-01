"""Fragment summary contract: JSON Schema + local validation + degradation.

The summary is produced by the MAIN model at chunk-close time. Local
validation is strict; on failure the caller degrades to direct citation of
the raw transcript (never lets the model rewrite persisted memory).

「严格」指的是**契约严格**，不是「模型稍有偏差就整条作废」：模型输出里
「太多 / 太长 / 重复 / 多余空白」属于可修正输出，统一交给
:mod:`agent.memory.model_output` 收敛后继续派生；只有根本无法解析 /
类型完全错误 / 必需结构缺失 / 无法安全恢复才算 schema failure。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.memory.model_output import (
    ObjectListField,
    Outcome,
    TextField,
    TextListField,
    parse_json_object,
    repair_output,
)

logger = logging.getLogger(__name__)


def _safe_exc(exc: BaseException) -> str:
    """异常文本进日志前先过统一脱敏。

    适配器/网关的异常消息可能夹带请求原文（含用户贴过的密钥），
    而「日志 / Trace / 错误信息不得出现密钥原文」是硬性约束。
    """
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


# 派生字段的硬上限。同一组常量既写进提示词（让模型自己收敛），又用在本地契约
# （模型没收住时兜底），两处共用一个事实来源，避免各自漂移。
MAX_TITLE = 60
MAX_SUMMARY = 2000
# 实体/关键词的数量上限：超过就本地去重截断（见统一可修正层），
# 不把「模型给多了」当成摘要失败 —— 那会让整个片段的摘要、索引、
# 实体卡与知识条目一起消失。
MAX_ENTITIES = 50
MAX_KEYWORDS = 50
# 知识候选的上限与单条长度。同样是「可修正」：截断后继续，不整条失败。
MAX_CANDIDATES = 10
MAX_CANDIDATE_CONTENT = 500
MAX_ENTITY_NAME = 80

_CATEGORY_CONTRACT = "user_profile|agent_self|goal|general_fact|tool_experience"
_ATTACH_CONTRACT = "user|topic|entity"
_CATEGORY_VALUES = frozenset(_CATEGORY_CONTRACT.split("|"))
_ATTACH_VALUES = frozenset(_ATTACH_CONTRACT.split("|"))


class FragmentSummary(BaseModel):
    """Contract for fragment summaries (schema v1)."""

    title: str = Field(min_length=1, max_length=MAX_TITLE)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY)
    entities: list[str] = Field(default_factory=list, max_length=MAX_ENTITIES)
    keywords: list[str] = Field(default_factory=list, max_length=MAX_KEYWORDS)


SUMMARY_PROMPT = (
    "You are summarizing a closed conversation fragment. "
    "Return only a single JSON object, no prose or Markdown fences:\n"
    '{"title": "<short title, <=%(title)d chars>", "summary": "<condensed record of facts, decisions, preferences, and commitments, <=%(summary)d chars>", "entities": ["<mentioned people/objects, exact names, at most %(entities)d, no duplicates>"], "keywords": ["<searchable keywords, at most %(keywords)d, no duplicates>"]}\n'
    "Keep the summary faithful to the transcript; do not add or infer facts. entities and keywords may be empty arrays when none. "
    "Keep the most important %(entities)d entries at most for entities and for keywords; anything longer is dropped before it is stored."
) % {
    "title": MAX_TITLE,
    "summary": MAX_SUMMARY,
    "entities": MAX_ENTITIES,
    "keywords": MAX_KEYWORDS,
}

# 摘要契约：title/summary 必需且可截断；entities/keywords 是字符串数组。
_SUMMARY_FIELDS = (
    TextField("title", MAX_TITLE),
    TextField("summary", MAX_SUMMARY),
    TextListField("entities", MAX_ENTITIES),
    TextListField("keywords", MAX_KEYWORDS),
)


def validate_summary(text: str) -> Outcome[FragmentSummary]:
    """解析 + 修正 + 校验模型输出。返回带可读失败原因与修正留痕的 Outcome。"""
    payload, error = parse_json_object(text)
    if error is not None:
        return Outcome(None, f"摘要输出{error}")
    repaired = repair_output(payload, _SUMMARY_FIELDS, contract="摘要输出")
    if not repaired.ok:
        return Outcome(None, repaired.error, repaired.notes)
    try:
        return Outcome(FragmentSummary(**repaired.data), None, repaired.notes)
    except ValidationError as exc:
        # 修正层放过的形态问题在这里兜底：契约（pydantic）才是最终权威。
        return Outcome(None, f"schema violation: {exc.errors()[:3]}", repaired.notes)


def validate_summary_text(text: str) -> tuple[FragmentSummary | None, str | None]:
    """兼容入口：只关心 (值, 失败原因) 的调用方继续用它。

    需要把「本地修正过什么」写进 trace / 诊断的调用方用 :func:`validate_summary`。
    """
    outcome = validate_summary(text)
    return outcome.value, outcome.error


async def summarize_fragment_outcome(
    adapter: BaseAdapter,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> Outcome[FragmentSummary]:
    """Call the main model to summarize fragment messages.

    Returns an Outcome carrying the summary, a readable failure reason and the
    local repair notes. Both degraded cases are handled by the caller: error is
    not None when the model output failed validation, or the model call itself
    failed.
    """
    transcript = "\n".join(
        f"{m['role']}: {m['content'] or ''}" for m in messages
    )[-12_000:]
    prompt = (
        SUMMARY_PROMPT
        + "\n\nTranscript:\n"
        + transcript
    )
    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)],
            tools=[],
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning("summarize call failed: %s", _safe_exc(exc))
        return Outcome(None, f"model call failed: {exc}")
    return validate_summary(completion.message.content or "")


async def summarize_fragment(
    adapter: BaseAdapter,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> tuple[FragmentSummary | None, str | None]:
    """兼容入口：见 :func:`summarize_fragment_outcome`。"""
    outcome = await summarize_fragment_outcome(
        adapter, messages, temperature=temperature
    )
    return outcome.value, outcome.error


# ---------------------------------------------------------------------------
# Knowledge extraction (chunk-close refinement chain)
# ---------------------------------------------------------------------------

class KnowledgeCandidate(BaseModel):
    """Candidate extracted from a fragment summary for the knowledge domain."""

    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CONTENT)
    category: str = Field(
        default="general_fact",
        pattern=r"^(user_profile|agent_self|goal|general_fact|tool_experience)$",
    )
    attach: str | None = Field(default=None, pattern=r"^(user|topic|entity)$")
    entity: str | None = Field(default=None, max_length=MAX_ENTITY_NAME)


class KnowledgeExtraction(BaseModel):
    candidates: list[KnowledgeCandidate] = Field(
        default_factory=list, max_length=MAX_CANDIDATES
    )


KNOWLEDGE_EXTRACTION_PROMPT = (
    "You are extracting durable knowledge from a conversation fragment summary. "
    "Return only a single JSON object, no prose or Markdown fences:\n"
    '{"candidates": [{"content": "<stable fact, one sentence, <=%(content)d chars>", "category": "%(category)s", "attach": "user|topic|entity|null", "entity": "<exact entity name when attach=entity, else null>"}]}\n'
    "Extract only facts that remain true over time: preferences, decisions, commitments, reusable knowledge. Do not extract transient statements.\n"
    "category: user_profile=facts about the user; agent_self=facts about the assistant; goal=user objectives; general_fact=general knowledge; tool_experience=reusable tool/technique experience.\n"
    "attach: user for user facts, topic for topic-specific knowledge, entity for facts about a named entity. entity must match the attach=entity value exactly.\n"
    "Return at most %(candidates)d candidates (extra ones are dropped before they are stored). "
    "Return an empty candidates array when nothing is durable."
) % {
    "content": MAX_CANDIDATE_CONTENT,
    "category": _CATEGORY_CONTRACT,
    "candidates": MAX_CANDIDATES,
}


def _candidate_invalid_reason(item: dict[str, Any]) -> str | None:
    """候选条目的领域校验：只判这一条能不能用，不影响别的候选。

    枚举取值不在契约内属于「这一条坏」，不是「整批失败」—— 丢掉它并留痕，
    其余候选照常进入知识生命周期。
    """
    category = item.get("category")
    if category is not None and category not in _CATEGORY_VALUES:
        return f"category 不在契约内（应为 {_CATEGORY_CONTRACT}）"
    attach = item.get("attach")
    if attach is not None and attach not in _ATTACH_VALUES:
        return f"attach 不在契约内（应为 {_ATTACH_CONTRACT}）"
    return None


_KNOWLEDGE_FIELDS = (
    ObjectListField(
        "candidates",
        MAX_CANDIDATES,
        fields=(
            TextField("content", MAX_CANDIDATE_CONTENT),
            TextField("category", 64, required=False),
            TextField(
                "attach",
                max(len(_ATTACH_CONTRACT), 16),
                required=False,
                allow_null_literal=True,
            ),
            TextField(
                "entity",
                MAX_ENTITY_NAME,
                required=False,
                allow_null_literal=True,
            ),
        ),
        dedupe=True,
        item_validator=_candidate_invalid_reason,
    ),
)


def validate_knowledge(text: str) -> Outcome[KnowledgeExtraction]:
    """解析 + 修正 + 校验知识抽取输出。"""
    payload, error = parse_json_object(text)
    if error is not None:
        return Outcome(None, f"知识抽取输出{error}")
    repaired = repair_output(payload, _KNOWLEDGE_FIELDS, contract="知识抽取输出")
    if not repaired.ok:
        return Outcome(None, repaired.error, repaired.notes)
    try:
        return Outcome(KnowledgeExtraction(**repaired.data), None, repaired.notes)
    except ValidationError as exc:
        return Outcome(None, f"schema violation: {exc.errors()[:3]}", repaired.notes)


def validate_knowledge_extraction(
    text: str,
) -> tuple[KnowledgeExtraction | None, str | None]:
    """兼容入口：见 :func:`validate_knowledge`。"""
    outcome = validate_knowledge(text)
    return outcome.value, outcome.error


async def extract_knowledge_candidates_outcome(
    adapter: BaseAdapter,
    summary: FragmentSummary,
    *,
    temperature: float = 0.2,
) -> Outcome[KnowledgeExtraction]:
    """Ask the main model to extract durable knowledge candidates from a summary."""
    prompt = (
        KNOWLEDGE_EXTRACTION_PROMPT
        + "\n\nFragment summary:\n"
        + summary.model_dump_json(ensure_ascii=False)
    )
    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)],
            tools=[],
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning("knowledge extraction call failed: %s", _safe_exc(exc))
        return Outcome(None, f"model call failed: {exc}")
    return validate_knowledge(completion.message.content or "")


async def extract_knowledge_candidates(
    adapter: BaseAdapter,
    summary: FragmentSummary,
    *,
    temperature: float = 0.2,
) -> tuple[KnowledgeExtraction | None, str | None]:
    """兼容入口：见 :func:`extract_knowledge_candidates_outcome`。"""
    outcome = await extract_knowledge_candidates_outcome(
        adapter, summary, temperature=temperature
    )
    return outcome.value, outcome.error


# ---------------------------------------------------------------------------
# Rolling summary (budget-pressure consolidation)
# ---------------------------------------------------------------------------

ROLLING_SUMMARY_PROMPT = (
    "You are updating the rolling summary of an ongoing conversation fragment. "
    "Merge the previous summary with the new messages into one consolidated summary. "
    "Keep durable facts (preferences, decisions, commitments) and drop transient details. "
    "Return only a single JSON object, no prose or Markdown fences:\n"
    '{"title": "<short title, <=%(title)d chars>", "summary": "<consolidated, <=%(summary)d chars>", "entities": ["<names>"], "keywords": ["<searchable>"]}\n'
    "If no change is needed, return the previous summary unchanged."
) % {"title": MAX_TITLE, "summary": MAX_SUMMARY}


async def summarize_rolling_outcome(
    adapter: BaseAdapter,
    old_summary: str | None,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> Outcome[FragmentSummary]:
    """Rolling summary: old summary + new messages -> updated summary."""
    old = old_summary or "(no previous summary)"
    transcript = "\n".join(
        f"{m['role']}: {m['content'] or ''}" for m in messages
    )[-8_000:]
    prompt = (
        ROLLING_SUMMARY_PROMPT
        + "\n\nPrevious summary:\n"
        + old
        + "\n\nNew messages:\n"
        + transcript
    )
    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)],
            tools=[],
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning("rolling summary call failed: %s", _safe_exc(exc))
        return Outcome(None, f"model call failed: {exc}")
    return validate_summary(completion.message.content or "")


async def summarize_rolling(
    adapter: BaseAdapter,
    old_summary: str | None,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> tuple[FragmentSummary | None, str | None]:
    """兼容入口：见 :func:`summarize_rolling_outcome`。"""
    outcome = await summarize_rolling_outcome(
        adapter, old_summary, messages, temperature=temperature
    )
    return outcome.value, outcome.error
