"""知识提炼契约与模型调用（片段摘要 → 可复用知识候选；Phase 1 C 升级）。

冻结契约（docs/qio-refactor-contract.md §4 / §6.2 / §7）：

* 固定文案只来自 agent.prompts：PROMPTS["KNOWLEDGE_EXTRACTION_SYSTEM"] +
  structured_task_system（结构化总则 + 任务规则 + 程序生成的 schema + limits）；
* schema 由 pydantic 模型经 json_schema_for 生成，本地校验 / 修正与展示同源；
* 动态数据（片段摘要、证据目录）统一 json.dumps 后作为数据传入，不拼进固定文案；
* 输出包含 epistemic_status 与 evidence_ids；来源必须真实存在（由证据目录校验），
  qio_observed 必须有真实工具结果 / 材料依据；
* 本地可修正层不能丢弃新增字段（repair_output 的字段定义同步扩展）。

兼容：validate_knowledge / extract_knowledge_candidates 等旧入口保留；
不传证据目录时只做枚举与结构校验（旧调用方行为不变）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.knowledge.evidence import (
    DEFAULT_EPISTEMIC_STATUS,
    EPISTEMIC_STATUSES,
    MAX_EVIDENCE_IDS,
    EvidenceCatalog,
    coerce_catalog,
    validate_epistemic_status,
)
from agent.memory.limits import (
    MAX_CANDIDATE_CONTENT,
    MAX_CANDIDATES,
    MAX_ENTITY_NAME,
)
from agent.memory.model_output import (
    ObjectListField,
    Outcome,
    TextField,
    TextListField,
    parse_json_object,
    repair_output,
)
from agent.prompts import PROMPTS, structured_task_system
from agent.tools.contracts import json_schema_for

logger = logging.getLogger(__name__)

_CATEGORY_CONTRACT = "user_profile|agent_self|goal|general_fact|tool_experience"
_ATTACH_CONTRACT = "user|topic|entity"
_CATEGORY_VALUES = frozenset(_CATEGORY_CONTRACT.split("|"))
_ATTACH_VALUES = frozenset(_ATTACH_CONTRACT.split("|"))
_EPISTEMIC_CONTRACT = "|".join(EPISTEMIC_STATUSES)
_MAX_EVIDENCE_ID_LENGTH = 80

#: 兼容别名：旧调用方（memory.summary 的导出）仍可从这里拿到固定文案。
KNOWLEDGE_EXTRACTION_PROMPT = PROMPTS["KNOWLEDGE_EXTRACTION_SYSTEM"]


def _safe_exc(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


class KnowledgeCandidate(BaseModel):
    """候选知识（后台抽取契约）。新增字段带默认值，兼容旧输出。"""

    content: str = Field(min_length=1, max_length=MAX_CANDIDATE_CONTENT)
    category: str = Field(
        default="general_fact",
        pattern=r"^(user_profile|agent_self|goal|general_fact|tool_experience)$",
    )
    attach: str | None = Field(default=None, pattern=r"^(user|topic|entity)$")
    entity: str | None = Field(default=None, max_length=MAX_ENTITY_NAME)
    epistemic_status: str = Field(
        default=DEFAULT_EPISTEMIC_STATUS,
        pattern=r"^(user_explicit|shared_understanding|qio_observed|qio_inference)$",
    )
    evidence_ids: list[str] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_IDS
    )


class KnowledgeExtraction(BaseModel):
    candidates: list[KnowledgeCandidate] = Field(
        default_factory=list, max_length=MAX_CANDIDATES
    )


def knowledge_schema() -> dict[str, Any]:
    """下发给模型、也用于本地校验的唯一 JSON Schema。"""
    return json_schema_for(KnowledgeExtraction)


def knowledge_limits() -> dict[str, Any]:
    """同一份类型定义派生的 limits（写进结构化任务输入）。"""
    return {
        "max_candidates": MAX_CANDIDATES,
        "max_content_chars": MAX_CANDIDATE_CONTENT,
        "max_entity_name_chars": MAX_ENTITY_NAME,
        "max_evidence_ids": MAX_EVIDENCE_IDS,
        "category_values": _CATEGORY_CONTRACT.split("|"),
        "attach_values": ["user", "topic", "entity", None],
        "epistemic_status_values": list(EPISTEMIC_STATUSES),
    }


def knowledge_task_system(*, repair: bool = False) -> str:
    """知识提炼的 system：结构化总则 + 任务规则 + schema + limits。"""
    return structured_task_system(
        "KNOWLEDGE_EXTRACTION_SYSTEM",
        schema=knowledge_schema(),
        limits=knowledge_limits(),
        repair=repair,
    )


def _candidate_invalid_reason(
    item: dict[str, Any], catalog: EvidenceCatalog | None
) -> str | None:
    """候选条目的领域校验：只判这一条能不能用，不影响别的候选。

    枚举越界 / 证据不存在 / qio_observed 无真实依据都属于「这一条坏」，丢掉并留痕；
    「整批一条都没保住」由可修正层判为无法安全恢复（schema failure）。
    """
    category = item.get("category")
    if category is not None and category not in _CATEGORY_VALUES:
        return f"category 不在契约内（应为 {_CATEGORY_CONTRACT}）"
    attach = item.get("attach")
    if attach is not None and attach not in _ATTACH_VALUES:
        return f"attach 不在契约内（应为 {_ATTACH_CONTRACT}）"
    status = item.get("epistemic_status")
    if status is not None and status not in EPISTEMIC_STATUSES:
        return f"epistemic_status 不在契约内（应为 {_EPISTEMIC_CONTRACT}）"
    if catalog is None:
        return None
    evidence_ids = item.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        evidence_ids = []
    _, missing = catalog.validate_evidence_ids(evidence_ids)
    if missing:
        return "evidence_ids 不在输入证据目录里：" + ", ".join(missing[:3])
    return validate_epistemic_status(
        catalog,
        str(item.get("epistemic_status") or DEFAULT_EPISTEMIC_STATUS),
        evidence_ids,
    )


def _knowledge_fields(catalog: EvidenceCatalog | None) -> tuple[Any, ...]:
    """按证据目录构造可修正层字段：新增字段必须在这里显式声明，不能被丢弃。"""

    def _invalid(item: dict[str, Any]) -> str | None:
        return _candidate_invalid_reason(item, catalog)

    return (
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
                TextField("epistemic_status", 32, required=False),
                TextListField(
                    "evidence_ids",
                    MAX_EVIDENCE_IDS,
                    max_item_length=_MAX_EVIDENCE_ID_LENGTH,
                    required=False,
                    strict=False,
                ),
            ),
            dedupe=True,
            item_validator=_invalid,
        ),
    )


#: 不传证据目录时的兼容字段定义（旧调用方 / 旧测试）。
_KNOWLEDGE_FIELDS = _knowledge_fields(None)


def validate_knowledge(
    text: str, *, evidence_catalog: Any | None = None
) -> Outcome[KnowledgeExtraction]:
    """解析 + 修正 + 校验知识抽取输出。"""
    payload, error = parse_json_object(text)
    if error is not None:
        return Outcome(None, f"知识抽取输出{error}")
    catalog = coerce_catalog(evidence_catalog) if evidence_catalog is not None else None
    repaired = repair_output(
        payload,
        _knowledge_fields(catalog),
        contract="知识抽取输出",
    )
    if not repaired.ok:
        return Outcome(None, repaired.error, repaired.notes)
    try:
        return Outcome(KnowledgeExtraction(**repaired.data), None, repaired.notes)
    except ValidationError as exc:
        return Outcome(None, f"schema violation: {exc.errors()[:3]}", repaired.notes)


def validate_knowledge_extraction(
    text: str, *, evidence_catalog: Any | None = None
) -> tuple[KnowledgeExtraction | None, str | None]:
    """兼容入口：见 validate_knowledge。"""
    outcome = validate_knowledge(text, evidence_catalog=evidence_catalog)
    return outcome.value, outcome.error


def _summary_payload(summary: Any) -> Any:
    if isinstance(summary, BaseModel):
        return summary.model_dump()
    if isinstance(summary, dict):
        return dict(summary)
    return summary


async def extract_candidates_outcome(
    adapter: BaseAdapter,
    *,
    summary: Any,
    evidence_catalog: Any | None = None,
    temperature: float = 0.2,
) -> Outcome[KnowledgeExtraction]:
    """问主模型抽取知识候选：固定 system + 数据（摘要 / 证据目录）json.dumps。"""
    catalog = coerce_catalog(evidence_catalog)
    data = {
        "summary": _summary_payload(summary),
        "evidence_catalog": catalog.prompt_entries(),
    }
    messages = [
        ChatMessage(role="system", content=knowledge_task_system()),
        ChatMessage(
            role="user",
            content=json.dumps(data, ensure_ascii=False, sort_keys=True),
        ),
    ]
    # 用量归因由 adapter 在每次实际请求上统一完成；这里只把预算耗尽转成简短真实原因。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(
            messages, tools=[], temperature=temperature
        )
    except BudgetExhausted as exc:
        logger.warning("knowledge extraction skipped: %s", _safe_exc(exc))
        return Outcome(None, str(exc))
    except Exception as exc:
        logger.warning("knowledge extraction call failed: %s", _safe_exc(exc))
        return Outcome(None, f"model call failed: {exc}")
    return validate_knowledge(
        completion.message.content or "", evidence_catalog=catalog
    )


async def extract_knowledge_candidates_outcome(
    adapter: BaseAdapter,
    summary: Any,
    *,
    temperature: float = 0.2,
    evidence_catalog: Any | None = None,
) -> Outcome[KnowledgeExtraction]:
    """兼容入口：旧调用方没有证据目录，只做结构 / 枚举校验。"""
    return await extract_candidates_outcome(
        adapter,
        summary=summary,
        evidence_catalog=evidence_catalog,
        temperature=temperature,
    )


async def extract_knowledge_candidates(
    adapter: BaseAdapter,
    summary: Any,
    *,
    temperature: float = 0.2,
) -> tuple[KnowledgeExtraction | None, str | None]:
    """兼容入口：见 extract_knowledge_candidates_outcome。"""
    outcome = await extract_knowledge_candidates_outcome(
        adapter, summary, temperature=temperature
    )
    return outcome.value, outcome.error
