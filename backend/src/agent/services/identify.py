"""API Key auto-detection: prefix fast-path + endpoint probing + model list.

Detection strategy:
1. prefix fast-path (sk-ant-, sk-proj-, gsk_, sk-or-, AIza, xai-);
2. probe prefix-hit providers first, then the remaining candidates in order
   via an auth-sensitive endpoint (GET {auth_path}) - the fallback matters for
   shared prefixes like `sk-` (DeepSeek, MiMo, ...). Providers whose /models is
   a public catalog (OpenRouter) set auth_path to an auth-sensitive endpoint
   (e.g. /key) so a valid key is still verified without a false positive hit;
3. on hit, fetch the model list for the UI selector.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 8.0
_INVALID_KEY = "invalid-probe"


class ProviderCandidate:
    def __init__(
        self,
        name: str,
        base_url: str,
        kind: str,
        default_model: str,
        prefixes: list[str] | None = None,
        auth_path: str = "/models",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.kind = kind  # openai | anthropic
        self.default_model = default_model
        self.prefixes = prefixes or []
        # Endpoint used to verify the key (auth-sensitive). Providers whose
        # /models is a PUBLIC catalog (e.g. OpenRouter) set an auth-sensitive
        # path here so a valid key can still be detected without a false hit.
        self.auth_path = auth_path


PROVIDERS: list[ProviderCandidate] = [
    ProviderCandidate("DeepSeek", "https://api.deepseek.com/v1", "openai", "deepseek-v4-flash", ["sk-"]),
    ProviderCandidate("OpenAI", "https://api.openai.com/v1", "openai", "gpt-5.6-luna", ["sk-proj-"]),
    ProviderCandidate("Anthropic", "https://api.anthropic.com/v1", "anthropic", "claude-sonnet-4", ["sk-ant-"]),
    ProviderCandidate("GLM（智谱）", "https://open.bigmodel.cn/api/paas/v4", "openai", "glm-5.2"),
    ProviderCandidate("Kimi（月之暗面）", "https://api.moonshot.cn/v1", "openai", "kimi-k2"),
    ProviderCandidate("MiniMax", "https://api.minimax.chat/v1", "openai", "minimax-m3"),
    ProviderCandidate("MiMo（小米）", "https://api.xiaomimimo.com/v1", "openai", "mimo-v2.5"),
    ProviderCandidate("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", "openai", "gemini-2.5-pro", ["AIza"]),
    ProviderCandidate("Qwen（DashScope）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai", "qwen3-max"),
    ProviderCandidate("Groq", "https://api.groq.com/openai/v1", "openai", "llama-4", ["gsk_"]),
    ProviderCandidate("xAI Grok", "https://api.x.ai/v1", "openai", "grok-4", ["xai-"]),
    ProviderCandidate("Mistral", "https://api.mistral.ai/v1", "openai", "mistral-large"),
    ProviderCandidate("OpenRouter", "https://openrouter.ai/api/v1", "openai", "deepseek/deepseek-v4-flash", ["sk-or-"], auth_path="/key"),
    ProviderCandidate("SiliconFlow（硅基流动）", "https://api.siliconflow.cn/v1", "openai", "Qwen/Qwen3-32B"),
    ProviderCandidate("AIHubMix", "https://api.aihubmix.com/v1", "openai", ""),
    ProviderCandidate("DeepBricks", "https://api.deepbricks.ai/v1", "openai", ""),
]


async def _probe_openai(base_url: str, key: str, auth_path: str = "/models") -> bool:
    """True only when the auth endpoint returns 200 for the real key AND rejects
    an invalid key (public catalogs return 200 either way)."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}{auth_path}",
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code != 200:
                return False
            # Distinguish a real auth check from a public catalog.
            no_auth = await client.get(
                f"{base_url}{auth_path}",
                headers={"Authorization": f"Bearer {_INVALID_KEY}"},
            )
            return no_auth.status_code != 200
    except Exception:
        return False


async def _probe_anthropic(base_url: str, key: str, auth_path: str = "/models") -> bool:
    """Same auth-sensitive check for Anthropic-style endpoints."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}{auth_path}",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            if resp.status_code != 200:
                return False
            no_auth = await client.get(
                f"{base_url}{auth_path}",
                headers={"x-api-key": _INVALID_KEY, "anthropic-version": "2023-06-01"},
            )
            return no_auth.status_code != 200
    except Exception:
        return False


async def _list_models(provider: ProviderCandidate, key: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            if provider.kind == "anthropic":
                resp = await client.get(
                    f"{provider.base_url}/models",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
            else:
                resp = await client.get(
                    f"{provider.base_url}/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data.get("data") or data.get("models") or []
            models = [
                m.get("id") or m.get("name")
                for m in items
                if isinstance(m, dict) and (m.get("id") or m.get("name"))
            ]
            return models[:200]
    except Exception:
        return []
    return []


async def identify_key(key: str) -> dict[str, Any] | None:
    """Detect the provider for an API key. Returns None when nothing matches."""
    key = key.strip()
    if not key:
        return None
    prefix_hits = [p for p in PROVIDERS if any(key.startswith(pre) for pre in p.prefixes)]
    # Shared prefixes (e.g. `sk-` for DeepSeek/MiMo) mean a failed prefix hit
    # must not block the rest of the table.
    fallback = [p for p in PROVIDERS if p not in prefix_hits]

    async def _probe(provider: ProviderCandidate):
        if provider.kind == "anthropic":
            ok = await _probe_anthropic(provider.base_url, key, provider.auth_path)
        else:
            ok = await _probe_openai(provider.base_url, key, provider.auth_path)
        if not ok:
            return None
        models = await _list_models(provider, key)
        return provider, models

    async def _result(provider: ProviderCandidate):
        hit = await _probe(provider)
        if hit is None:
            return None
        provider, models = hit
        return {
            "provider": provider.name,
            "base_url": provider.base_url,
            "kind": provider.kind,
            "default_model": provider.default_model,
            "models": models,
        }

    # Prefix hits first (small, common case): fast and does not leak the key
    # to unrelated providers.
    for provider in prefix_hits:
        result = await _result(provider)
        if result is not None:
            return result

    # Fallback table is probed in parallel (the key is already "unknown" here);
    # the response order follows PROVIDERS for determinism.
    outcomes = await asyncio.gather(*(_result(p) for p in fallback))
    for result in outcomes:
        if result is not None:
            return result
    return None
