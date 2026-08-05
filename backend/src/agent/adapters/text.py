"""Text-mode adapter: no tool-calling API; the model emits JSON in text.

This is a fallback tier. The prompt asks for a JSON block describing tool
calls; parsing tolerates ```json fences and trailing prose. Parse success
rate is tracked so the loop can flag the endpoint as unsupported when the
rate drops below the threshold.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from agent.adapters.base import (
    BaseAdapter,
    ChatMessage,
    Completion,
    ToolCall,
    ToolSpec,
)

logger = logging.getLogger(__name__)

SUCCESS_RATE_THRESHOLD = 0.6

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class TextAdapter(BaseAdapter):
    mode = "text"

    def __init__(
        self,
        client: Any,
        model: str,
        endpoint: str | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self.endpoint = endpoint
        self._attempts = 0
        self._successes = 0

    # -- tracking ---------------------------------------------------------

    @property
    def success_rate(self) -> float:
        if self._attempts == 0:
            return 1.0
        return self._successes / self._attempts

    def _record(self, ok: bool) -> None:
        self._attempts += 1
        if ok:
            self._successes += 1

    # -- prompt building --------------------------------------------------

    def build_text_tools(self, tools: list[ToolSpec]) -> str:
        lines = []
        for t in tools:
            lines.append(
                f"- {t.name}: {t.description}\n"
                f"  parameters (JSON Schema): {json.dumps(t.parameters, ensure_ascii=False)}"
            )
        return "\n".join(lines)

    def build_system_prompt(self, tools: list[ToolSpec]) -> str:
        return (
            "You are a tool-calling assistant without a native tool API. "
            "You MUST respond with a single JSON object in a ```json block "
            "using exactly this shape when you want to call a tool:\n"
            '{"tool_calls": [{"name": "<tool>", "arguments": {}}]}\n'
            "If no tool is needed, respond with plain text only.\n\n"
            "Available tools:\n"
            f"{self.build_text_tools(tools)}"
        )

    # -- completion -------------------------------------------------------

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_text_messages(messages, tools),
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        raw = await self._client.chat.completions.create(**kwargs)
        content = raw.choices[0].message.content or ""
        parsed = self._parse(content)
        ok = parsed is not None
        self._record(ok)

        tool_calls = None
        if ok:
            assert parsed is not None
            tool_calls = [
                ToolCall(
                    id=f"tc_{uuid.uuid4().hex[:8]}",
                    name=item["name"],
                    arguments=item.get("arguments") or {},
                )
                for item in parsed["tool_calls"]
            ]
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=tool_calls),
            raw=raw,
            usage=None,
        )

    def _to_text_messages(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(tools)}
        ]
        for msg in messages:
            out.append({"role": msg.role, "content": msg.content or ""})
        return out

    def _parse(self, content: str) -> dict[str, Any] | None:
        text = content.strip()
        match = _JSON_BLOCK.search(text)
        candidate = match.group(1) if match else text
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or "tool_calls" not in data:
            return None
        calls = data["tool_calls"]
        if not isinstance(calls, list) or not all(
            isinstance(c, dict) and isinstance(c.get("name"), str) for c in calls
        ):
            return None
        return data