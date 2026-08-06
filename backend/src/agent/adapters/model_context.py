"""Model context-length resolution: built-in table -> runtime downgrade -> fallback.

Design (per plan):
- built-in table covers mainstream families (DeepSeek, GPT, Claude, Qwen, etc.);
- runtime downgrade tiers (256K -> 128K -> 64K) fire when a model call reports
  a context-length error; the resolved value is cached per (endpoint, model);
- fallback is 256K when nothing matches.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Context probe tiers for runtime downgrade when a context-length error hits.
CONTEXT_PROBE_TIERS = [256_000, 128_000, 64_000]
DEFAULT_FALLBACK_CONTEXT = CONTEXT_PROBE_TIERS[0]

# Model family -> context window. Substring matching: longest match wins.
DEFAULT_CONTEXT_LENGTHS: dict[str, int] = {
    # DeepSeek
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-chat": 131_072,
    "deepseek-reasoner": 131_072,
    # GPT family
    "gpt-5.6-luna": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.4": 1_050_000,
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4": 128_000,
    # Claude family
    "claude-sonnet": 1_000_000,
    "claude-opus": 1_000_000,
    "claude-haiku": 200_000,
    "claude": 200_000,
    # Qwen
    "qwen3-coder-plus": 1_000_000,
    "qwen3-coder": 262_144,
    "qwen3-max": 262_144,
    "qwen3": 262_144,
    "qwen": 131_072,
    # MiniMax / GLM / Kimi
    "minimax-m3": 1_000_000,
    "glm-5": 200_000,
    "glm": 131_072,
    "kimi": 131_072,
    # Grok
    "grok-4": 262_144,
    "grok": 131_072,
    # Llama / Mistral (common hosted sizes)
    "llama-4": 262_144,
    "llama": 131_072,
    "mistral-large": 131_072,
    "mistral": 131_072,
}

_CONTEXT_CACHE_TTL = 3600.0


def resolve_context_length(model: str | None) -> int:
    """Resolve the model's context window from the built-in table.

    Longest substring match wins; unknown models fall back to 256K. The
    runtime downgrade path (on context-length errors) is handled by
    ContextLengthRegistry below.
    """
    if not model:
        return DEFAULT_FALLBACK_CONTEXT
    lowered = model.lower()
    best: tuple[int, int] = (0, DEFAULT_FALLBACK_CONTEXT)
    for key, length in DEFAULT_CONTEXT_LENGTHS.items():
        if key in lowered and len(key) > best[0]:
            best = (len(key), length)
    return best[1]


class ContextLengthRegistry:
    """Per (endpoint, model) context length with runtime downgrade + cache."""

    def __init__(self, ttl: float = _CONTEXT_CACHE_TTL) -> None:
        self._data: dict[tuple[str | None, str], tuple[int, float]] = {}
        self.ttl = ttl

    def get(self, endpoint: str | None, model: str) -> int:
        cached = self._data.get((endpoint, model))
        if cached is not None and time.time() - cached[1] < self.ttl:
            return cached[0]
        length = resolve_context_length(model)
        self._data[(endpoint, model)] = (length, time.time())
        return length

    def downgrade(self, endpoint: str | None, model: str) -> int:
        """Step down one probe tier after a context-length error."""
        current = self.get(endpoint, model)
        for tier in CONTEXT_PROBE_TIERS:
            if tier < current:
                self._data[(endpoint, model)] = (tier, time.time())
                logger.warning("context downgraded for %s/%s: %d -> %d", endpoint, model, current, tier)
                return tier
        return current

    def is_context_length_error(self, exc: Any) -> bool:
        text = str(exc).lower()
        return any(
            token in text
            for token in (
                "context_length",
                "context length",
                "maximum context",
                "exceeded the context",
                "context window",
                "max context",
            )
        )