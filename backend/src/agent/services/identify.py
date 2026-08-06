"""API Key auto-detection: prefix fast-path + endpoint probing + model list.

Detection strategy:
1. prefix fast-path (sk-ant-, sk-proj-, gsk_, sk-or-, AIza, xai-);
2. probe prefix-hit providers first, then the remaining candidates in order
   via GET /models (auth check) - the fallback matters for shared prefixes
   like `sk-` (DeepSeek, MiMo, ...);
3. on hit, fetch the model list for the UI selector.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 8.0


class ProviderCandidate:
    def __init__(
        self,
        name: str,
        base_url: str,
        kind: str,
        default_model: str,
        prefixes: list[str] | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.kind = kind  # openai | anthropic
        self.default_model = default_model
        self.prefixes = prefixes or []


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
    ProviderCandidate("OpenRouter", "https://openrouter.ai/api/v1", "openai", "deepseek/deepseek-v4-flash", ["sk-or-"]),
    ProviderCandidate("SiliconFlow（硅基流动）", "https://api.siliconflow.cn/v1", "openai", "Qwen/Qwen3-32B"),
    ProviderCandidate("AIHubMix", "https://api.aihubmix.com/v1", "openai", ""),
    ProviderCandidate("DeepBricks", "https://api.deepbricks.ai/v1", "openai", ""),
]


async def _probe_openai(base_url: str, key: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


async def _probe_anthropic(base_url: str, key: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            return resp.status_code == 200
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
    # Prefix hits go first, but a failed hit must not block the rest of the
    # table: `sk-` is shared by DeepSeek and MiMo, so we fall through.
    candidates = prefix_hits + [p for p in PROVIDERS if p not in prefix_hits]

    for provider in candidates:
        if provider.kind == "anthropic":
            ok = await _probe_anthropic(provider.base_url, key)
        else:
            ok = await _probe_openai(provider.base_url, key)
        if ok:
            models = await _list_models(provider, key)
            return {
                "provider": provider.name,
                "base_url": provider.base_url,
                "kind": provider.kind,
                "default_model": provider.default_model,
                "models": models,
            }
    return None