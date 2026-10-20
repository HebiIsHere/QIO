"""知识提炼契约与模型调用（片段摘要 → 可复用知识候选）。

Phase 1 起：输出包含 category / attach / epistemic_status / evidence_ids；
来源必须能在输入提供的证据目录里找到，审批状态与事实确定程度分开。
生成、schema、修正共用 :mod:`agent.memory.limits` 的同一组上限。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.memory.limits import (
    MAX_CANDIDATE_CONTENT,
    MAX_CANDIDATES,
    MAX_ENTITY_NAME,
)
from agent.memory.model_output import (
    ObjectListField,
    Outcome,
    TextField,
    parse_json_object,
    repair_output,
)

logger = logging.getLogger(__name__)

_CATEGORY_CONTRACT = "user_profile|agent_self|goal|general_fact|tool_experience"
_ATTACH_CONTRACT = "user|topic|entity"
_CATEGORY_VALUES = frozenset(_CATEGORY_CONTRACT.split("|"))
_ATTACH_VALUES = frozenset(_ATTACH_CONTRACT.split("|"))


def _safe_exc(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


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
    # 记账在 adapter 的请求层统一完成；这里只把预算耗尽转成简短真实原因。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)],
            tools=[],
            temperature=temperature,
        )
    except BudgetExhausted as exc:
        logger.warning("knowledge extraction skipped: %s", _safe_exc(exc))
        return Outcome(None, str(exc))
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
