from __future__ import annotations

from agent.adapters.model_context import (
    ContextLengthRegistry,
    DEFAULT_FALLBACK_CONTEXT,
    resolve_context_length,
)


def test_resolve_known_families():
    assert resolve_context_length("deepseek-v4-flash") == 1_000_000
    assert resolve_context_length("gpt-5.6-luna") == 1_050_000
    assert resolve_context_length("qwen3-coder-plus") == 1_000_000
    assert resolve_context_length("claude-sonnet-4.6") == 1_000_000


def test_resolve_substring_longest_match():
    # "gpt-5" family wins over "gpt" generic
    assert resolve_context_length("gpt-5.4-2026") == 1_050_000
    # "deepseek-chat" more specific than generic
    assert resolve_context_length("deepseek-chat") == 131_072


def test_resolve_unknown_fallback():
    assert resolve_context_length("totally-unknown-model") == DEFAULT_FALLBACK_CONTEXT
    assert resolve_context_length(None) == DEFAULT_FALLBACK_CONTEXT


def test_registry_cache_and_downgrade():
    reg = ContextLengthRegistry()
    assert reg.get("ep", "deepseek-v4-flash") == 1_000_000
    # cached
    assert reg.get("ep", "deepseek-v4-flash") == 1_000_000
    # downgrade steps down one tier
    assert reg.downgrade("ep", "deepseek-v4-flash") == 256_000
    assert reg.get("ep", "deepseek-v4-flash") == 256_000
    # second downgrade steps further
    assert reg.downgrade("ep", "deepseek-v4-flash") == 128_000


def test_registry_error_detection():
    reg = ContextLengthRegistry()
    assert reg.is_context_length_error("Error code: 400 - maximum context length exceeded")
    assert reg.is_context_length_error("this model's maximum context length is 131072 tokens")
    assert not reg.is_context_length_error("rate limit exceeded")
    assert not reg.is_context_length_error("invalid api key")