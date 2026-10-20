"""片段摘要：模型调用 + 兼容入口。

契约（类型 / 提示词 / 本地校验）在 :mod:`agent.memory.contract`；
滚动摘要与知识抽取分别放在 :mod:`agent.memory.rolling` 与
:mod:`agent.knowledge.extraction`，本模块保留兼容导出，旧调用方无需改动。
"""

from __future__ import annotations

import logging

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.memory.contract import (
    SUMMARY_PROMPT,
    FragmentSummary,
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
)
from agent.memory.model_output import Outcome

logger = logging.getLogger(__name__)


def _safe_exc(exc: BaseException) -> str:
    """异常文本进日志前先过统一脱敏。"""
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


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
    # 用量归因不在这里做：adapter 在**每次实际请求**上统一入账（含内部重试）。
    # 这里只负责把「用量上限已耗尽」如实转成简短原因，不重试、不换配置。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)],
            tools=[],
            temperature=temperature,
        )
    except BudgetExhausted as exc:
        logger.warning("summarize skipped: %s", _safe_exc(exc))
        return Outcome(None, str(exc))
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
