"""片段摘要：完整覆盖的分段摘要 + 兼容入口。

Phase 1 起：
* 封块摘要必须覆盖**完整片段**：按完整消息切块，单条超长消息按「可追溯分段」
  （消息 id + 段序号 / 字符偏移）切开；逐块用 prompts.structured_task_system(
  "CLOSED_SUMMARY_SYSTEM", schema=..., limits=...) 生成，再按顺序用
  MERGE_SUMMARY_SYSTEM 合成最终摘要。**不再保留尾部截断**（旧 [-12_000:]）。
* 每个分段的覆盖信息随结果返回，供派生任务写进 fragments.meta.summary_coverage。
* 契约类型 / 提示词参数 / 本地校验在 agent.memory.contract；滚动摘要与知识抽取
  分别在 agent.memory.rolling / agent.knowledge.extraction，本模块保留兼容导出。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Any, Sequence

from agent import prompts
from agent.adapters.base import BaseAdapter, ChatMessage
from agent.memory.contract import (
    FragmentSummary,
    closed_summary_task_system,
    merge_summary_task_system,
    validate_summary,
    validate_summary_text,
)
from agent.memory.limits import (
    MAX_CANDIDATES,
    MAX_CANDIDATE_CONTENT,
    MAX_ENTITIES,
    MAX_ENTITY_NAME,
    MAX_KEYWORDS,
    MAX_SUMMARY,
    MAX_TITLE,
    SUMMARY_CHUNK_MAX_CHARS,
    SUMMARY_CHUNK_MAX_TOKENS,
    SUMMARY_MERGE_MAX_SEGMENTS,
    SUMMARY_SEGMENT_MAX_CHARS,
    SUMMARY_SEGMENT_MAX_TOKENS,
)
from agent.memory.model_output import Outcome, RepairNote

logger = logging.getLogger(__name__)


def _safe_exc(exc: BaseException) -> str:
    """异常文本进日志前先过统一脱敏。"""
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _field(message: Any, name: str, default: Any = None) -> Any:
    """兼容 dict / sqlite3.Row 的只读取值（缺键返回 default）。"""
    try:
        return message[name]
    except (KeyError, IndexError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 覆盖信息：什么输入被摘要过（可追溯，供 fragments.meta 持久化）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SummaryCoverage:
    """一次封块摘要的覆盖情况（complete=True 只在本轮确实覆盖全部输入时）。"""

    input_version: int
    message_count: int
    covered_message_ids: tuple[str, ...]
    segments: tuple[dict[str, Any], ...]
    chunk_count: int
    complete: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_version": int(self.input_version),
            "message_count": int(self.message_count),
            "covered_message_ids": list(self.covered_message_ids),
            "segments": [dict(item) for item in self.segments],
            "chunk_count": int(self.chunk_count),
            "complete": bool(self.complete),
        }


@dataclass(frozen=True)
class SummaryRun:
    """摘要结果 + 覆盖信息（失败时 coverage.complete 必须为 False）。"""

    outcome: Outcome[FragmentSummary]
    coverage: SummaryCoverage


@dataclass(frozen=True)
class _Unit:
    """切块的最小单位：完整消息，或一条超长消息的可追溯分段。"""

    message_id: str
    role: str
    content: str
    segment_index: int | None
    char_start: int
    char_end: int


def _message_id(message: Any, index: int) -> str:
    value = _field(message, "id")
    return str(value) if value else f"msg_{index + 1}"


def _split_oversize(text: str, max_chars: int, max_tokens: int) -> list[tuple[int, int]]:
    """把超长文本切成若干连续区间：每段同时满足字符与 token 预算。

    用二分找到「不超过 token 预算的最长前缀」，而不是按固定字数硬切或只留尾部。
    单字符仍超预算时也前进一个字符，保证不会死循环。
    """
    from agent.memory.index import estimate_tokens

    bounds: list[tuple[int, int]] = []
    position = 0
    length = len(text)
    while position < length:
        limit = min(max_chars, length - position)
        low, high, best = 1, limit, 0
        while low <= high:
            middle = (low + high) // 2
            if estimate_tokens(text[position : position + middle]) <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best <= 0:
            best = 1
        bounds.append((position, position + best))
        position += best
    return bounds


def _units_for(messages: Sequence[Any]) -> list[_Unit]:
    """按完整消息构造切块单位；超长单条消息拆成可追溯分段。"""
    from agent.memory.index import estimate_tokens

    units: list[_Unit] = []
    for index, message in enumerate(messages):
        message_id = _message_id(message, index)
        role = str(_field(message, "role") or "user")
        content = str(_field(message, "content") or "")
        oversize = bool(content) and (
            len(content) > SUMMARY_SEGMENT_MAX_CHARS
            or estimate_tokens(content) > SUMMARY_SEGMENT_MAX_TOKENS
        )
        if not oversize:
            units.append(_Unit(message_id, role, content, None, 0, len(content)))
            continue
        for segment_index, (start, end) in enumerate(
            _split_oversize(
                content, SUMMARY_SEGMENT_MAX_CHARS, SUMMARY_SEGMENT_MAX_TOKENS
            )
        ):
            units.append(
                _Unit(message_id, role, content[start:end], segment_index, start, end)
            )
    return units


def _coverage_segments(units: Sequence[_Unit]) -> tuple[dict[str, Any], ...]:
    """被切开的消息分段清单（完整消息不需要逐条列出）。"""
    return tuple(
        {
            "message_id": unit.message_id,
            "segment_index": int(unit.segment_index),
            "char_start": int(unit.char_start),
            "char_end": int(unit.char_end),
        }
        for unit in units
        if unit.segment_index is not None
    )


def _build_chunks(
    units: Sequence[_Unit],
    *,
    max_chars: int,
    max_tokens: int,
) -> list[list[dict[str, Any]]]:
    """按顺序把单位装进块：每块同时受字符 / token 预算约束，不拆完整消息。"""
    from agent.memory.index import estimate_tokens

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    current_tokens = 0
    for unit in units:
        entry: dict[str, Any] = {
            "message_id": unit.message_id,
            "role": unit.role,
            "content": unit.content,
        }
        if unit.segment_index is not None:
            entry["segment"] = {
                "index": int(unit.segment_index),
                "char_start": int(unit.char_start),
                "char_end": int(unit.char_end),
            }
        cost_chars = len(unit.content)
        cost_tokens = estimate_tokens(unit.content)
        if current and (
            current_chars + cost_chars > max_chars
            or current_tokens + cost_tokens > max_tokens
        ):
            chunks.append(current)
            current = []
            current_chars = 0
            current_tokens = 0
        current.append(entry)
        current_chars += cost_chars
        current_tokens += cost_tokens
    if current:
        chunks.append(current)
    return chunks


def message_chunks(
    messages: Sequence[Any],
    *,
    max_chars: int = SUMMARY_CHUNK_MAX_CHARS,
    max_tokens: int = SUMMARY_CHUNK_MAX_TOKENS,
) -> list[list[dict[str, Any]]]:
    """把消息按「完整消息 + 可追溯分段」切块（滚动摘要复用同一口径）。"""
    return _build_chunks(
        _units_for(list(messages)), max_chars=max_chars, max_tokens=max_tokens
    )


# ---------------------------------------------------------------------------
# 模型调用：每次调用只负责「一段」或「一批合并」，动态数据全部走 json.dumps
# ---------------------------------------------------------------------------


async def structured_summary_call(
    adapter: BaseAdapter,
    system: str,
    data: dict[str, Any],
    *,
    temperature: float,
) -> Outcome[FragmentSummary]:
    """一次结构化摘要调用：system 只来自 prompts，data 整体 json.dumps 作为数据。"""
    payload = json.dumps(data, ensure_ascii=False)
    # 用量归因不在这里做：adapter 在每次实际请求上统一入账。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(
            [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=payload),
            ],
            tools=[],
            temperature=temperature,
        )
    except BudgetExhausted as exc:
        logger.warning("summarize skipped: %s", _safe_exc(exc))
        return Outcome(None, str(exc))
    except Exception as exc:  # noqa: BLE001 - 失败原因转成可读字符串交给调用方
        logger.warning("summarize call failed: %s", _safe_exc(exc))
        return Outcome(None, f"model call failed: {exc}")
    return validate_summary(completion.message.content or "")


async def _merge_partials(
    adapter: BaseAdapter,
    partials: Sequence[FragmentSummary],
    *,
    input_version: int,
    temperature: float,
) -> tuple[Outcome[FragmentSummary], list[RepairNote]]:
    """按顺序合成分段摘要：单批最多 SUMMARY_MERGE_MAX_SEGMENTS 条，逐层归并。"""
    system = merge_summary_task_system()
    pending = list(partials)
    notes: list[RepairNote] = []
    batch_index = 0
    while len(pending) > 1:
        merged: list[FragmentSummary] = []
        for start in range(0, len(pending), SUMMARY_MERGE_MAX_SEGMENTS):
            batch = pending[start : start + SUMMARY_MERGE_MAX_SEGMENTS]
            if len(batch) == 1:
                # 单条不需要合并调用；它会在下一轮和别的批次一起归并。
                merged.append(batch[0])
                continue
            batch_index += 1
            data = {
                "batch_index": batch_index,
                "input_version": int(input_version),
                "summaries": [item.model_dump() for item in batch],
            }
            outcome = await structured_summary_call(
                adapter, system, data, temperature=temperature
            )
            notes.extend(outcome.notes)
            if outcome.value is None:
                reason = outcome.error or "模型不可用"
                return (
                    Outcome(None, f"合并摘要失败（第 {batch_index} 批）：{reason}", tuple(notes)),
                    notes,
                )
            merged.append(outcome.value)
        pending = merged
    return Outcome(pending[0], None, tuple(notes)), notes


async def summarize_messages_outcome(
    adapter: BaseAdapter,
    messages: Sequence[Any],
    *,
    temperature: float = 0.2,
    input_version: int | None = None,
) -> SummaryRun:
    """封块摘要：覆盖**完整片段**，逐段摘要后按顺序合并。

    返回 SummaryRun：失败时 outcome.value 为 None 且 coverage.complete=False，
    调用方（派生任务）不得写入 complete=True，也不得用失败结果覆盖原文/旧摘要。
    """
    message_list = list(messages)
    version = int(input_version) if input_version is not None else len(message_list)
    units = _units_for(message_list)
    chunks = _build_chunks(
        units,
        max_chars=SUMMARY_CHUNK_MAX_CHARS,
        max_tokens=SUMMARY_CHUNK_MAX_TOKENS,
    )
    coverage = SummaryCoverage(
        input_version=version,
        message_count=len(message_list),
        covered_message_ids=tuple(
            _message_id(message, index) for index, message in enumerate(message_list)
        ),
        segments=_coverage_segments(units),
        chunk_count=len(chunks),
        complete=False,
    )
    if not chunks:
        return SummaryRun(Outcome(None, "没有可摘要的消息"), coverage)

    system = closed_summary_task_system()
    partials: list[FragmentSummary] = []
    notes: list[RepairNote] = []
    for chunk_index, chunk in enumerate(chunks):
        data = {
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
            "input_version": version,
            "messages": chunk,
        }
        outcome = await structured_summary_call(
            adapter, system, data, temperature=temperature
        )
        notes.extend(outcome.notes)
        if outcome.value is None:
            reason = outcome.error or "摘要模型不可用"
            return SummaryRun(
                Outcome(
                    None,
                    f"第 {chunk_index + 1}/{len(chunks)} 段摘要失败：{reason}",
                    tuple(notes),
                ),
                coverage,
            )
        partials.append(outcome.value)

    if len(partials) == 1:
        final: FragmentSummary = partials[0]
    else:
        merged_outcome, merge_notes = await _merge_partials(
            adapter, partials, input_version=version, temperature=temperature
        )
        notes.extend(merge_notes)
        if merged_outcome.value is None:
            return SummaryRun(merged_outcome, coverage)
        final = merged_outcome.value

    return SummaryRun(
        Outcome(final, None, tuple(notes)), replace(coverage, complete=True)
    )


# ---------------------------------------------------------------------------
# 兼容入口（旧调用方签名不变；内部已改为完整覆盖路径）
# ---------------------------------------------------------------------------


async def summarize_fragment_outcome(
    adapter: BaseAdapter,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> Outcome[FragmentSummary]:
    """Call the main model to summarize fragment messages (compat wrapper).

    Returns an Outcome carrying the summary, a readable failure reason and the
    local repair notes. Coverage metadata is available via
    summarize_messages_outcome; callers that only need the value keep using this.
    """
    run = await summarize_messages_outcome(
        adapter, messages, temperature=temperature
    )
    return run.outcome


async def summarize_fragment(
    adapter: BaseAdapter,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> tuple[FragmentSummary | None, str | None]:
    """兼容入口：见 summarize_fragment_outcome。"""
    outcome = await summarize_fragment_outcome(
        adapter, messages, temperature=temperature
    )
    return outcome.value, outcome.error


# 兼容导出：旧调用方 / 既有测试按名字引用。正文只有 prompts 一个来源：这里
# 直接等于结构化任务 system（提示词正文 + 程序生成的 schema 与 limits），
# 不再保留旧的英文提示词正文。
SUMMARY_PROMPT = closed_summary_task_system()

# -- 兼容导出：旧调用方仍可从本模块导入滚动摘要 / 知识抽取 -------------------
from agent.knowledge.extraction import (  # noqa: E402,F401
    KNOWLEDGE_EXTRACTION_PROMPT,
    KnowledgeCandidate,
    KnowledgeExtraction,
    extract_knowledge_candidates,
    extract_knowledge_candidates_outcome,
    validate_knowledge,
    validate_knowledge_extraction,
)
from agent.memory.rolling import (  # noqa: E402,F401
    ROLLING_SUMMARY_PROMPT,
    summarize_rolling,
    summarize_rolling_outcome,
)
