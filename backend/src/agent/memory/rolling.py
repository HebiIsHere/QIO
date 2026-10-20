"""滚动摘要：已有摘要 + 真正新增的消息 → 更新后的摘要。

覆盖位置与输入版本由调用方持久化（见 services/memory_lifecycle.py 与契约）。
"""

from __future__ import annotations

import logging

from agent.adapters.base import BaseAdapter, ChatMessage
from agent.memory.contract import FragmentSummary, validate_summary
from agent.memory.limits import MAX_SUMMARY, MAX_TITLE
from agent.memory.model_output import Outcome

logger = logging.getLogger(__name__)


def _safe_exc(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


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
    # 记账在 adapter 的请求层统一完成；这里只把预算耗尽转成简短真实原因。
    from agent.credentials.policy import BudgetExhausted

    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content=prompt)],
            tools=[],
            temperature=temperature,
        )
    except BudgetExhausted as exc:
        logger.warning("rolling summary skipped: %s", _safe_exc(exc))
        return Outcome(None, str(exc))
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
