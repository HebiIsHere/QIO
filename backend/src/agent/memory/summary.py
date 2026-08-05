"""Fragment summary contract: JSON Schema + local validation + degradation.

The summary is produced by the MAIN model at chunk-close time. Local
validation is strict; on failure the caller degrades to direct citation of
the raw transcript (never lets the model rewrite persisted memory).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.adapters.base import BaseAdapter, ChatMessage

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class FragmentSummary(BaseModel):
    """Contract for fragment summaries (schema v1)."""

    title: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=2000)
    entities: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=50)


SUMMARY_PROMPT = (
    "You are writing the summary for a closed conversation fragment. "
    "Respond with a single JSON object only (no prose):\n"
    '{"title": "<short title, <=60 chars>", "summary": "<condensed record of facts, '
    'decisions, preferences, and commitments, <=2000 chars>", '
    '"entities": ["<mentioned people/objects, exact names>"], '
    '"keywords": ["<searchable keywords>"]}\n'
    "Keep the summary faithful to the transcript; do not add or infer facts."
)


def validate_summary_text(text: str) -> tuple[FragmentSummary | None, str | None]:
    """Parse and validate model output. Returns (summary, error)."""
    match = _JSON_BLOCK.search(text.strip())
    candidate = match.group(1) if match else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "summary is not a JSON object"
    try:
        return FragmentSummary(**data), None
    except ValidationError as exc:
        return None, f"schema violation: {exc.errors()[:3]}"


async def summarize_fragment(
    adapter: BaseAdapter,
    messages: list[Any],
    *,
    temperature: float = 0.2,
) -> tuple[FragmentSummary | None, str | None]:
    """Call the main model to summarize fragment messages.

    Returns (summary, error). Both degraded cases are handled by the
    caller: error is not None when the model output failed validation, or
    the model call itself failed.
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
        logger.warning("summarize call failed: %s", exc)
        return None, f"model call failed: {exc}"
    return validate_summary_text(completion.message.content or "")