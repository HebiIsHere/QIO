"""Three-state capability probing, cached per (endpoint, model)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent.adapters.base import AdapterMode, ToolSpec

PING_TOOLS = [
    ToolSpec(
        name="ping",
        description="Returns pong. Used for capability probing.",
        parameters={"type": "object", "properties": {}},
    )
]


@dataclass(frozen=True)
class ProbeResult:
    mode: AdapterMode
    detail: str
    probed_at: float


class ProbeCache:
    def __init__(self, ttl_seconds: float = 3600.0, maxsize: int = 128) -> None:
        self._data: dict[tuple[str | None, str], tuple[ProbeResult, float]] = {}
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize

    def get(self, endpoint: str | None, model: str) -> ProbeResult | None:
        entry = self._data.get((endpoint, model))
        if entry is None:
            return None
        result, cached_at = entry
        if time.time() - cached_at > self.ttl_seconds:
            self._data.pop((endpoint, model), None)
            return None
        return result

    def set(self, endpoint: str | None, model: str, result: ProbeResult) -> None:
        if len(self._data) >= self.maxsize:
            self._data.clear()
        self._data[(endpoint, model)] = (result, time.time())


async def probe_adapter(
    client: Any,
    model: str,
    endpoint: str | None = None,
    cache: ProbeCache | None = None,
) -> ProbeResult:
    """Probe the endpoint/model pair and return its adapter mode.

    native:   a tool-calling request succeeds;
    text:     the API rejects tool calling (explicitly unsupported);
    others:   auth/network errors propagate to the caller.
    """
    if cache is not None:
        cached = cache.get(endpoint, model)
        if cached is not None:
            return cached

    result = await _probe_uncached(client, model)
    if cache is not None:
        cache.set(endpoint, model, result)
    return result


async def _probe_uncached(client: Any, model: str) -> ProbeResult:
    from openai import APIError, APIConnectionError, AuthenticationError, BadRequestError

    try:
        await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "Returns pong.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            max_tokens=8,
        )
        return ProbeResult(AdapterMode.NATIVE, "tool calling accepted", time.time())
    except BadRequestError as exc:
        message = str(exc)
        if any(token in message.lower() for token in ("tool", "function", "not supported", "invalid")):
            return ProbeResult(
                AdapterMode.TEXT, f"tool calling rejected: {message[:200]}", time.time()
            )
        raise
    except (AuthenticationError, APIConnectionError, APIError) as exc:
        raise