"""Native adapter: OpenAI-compatible tool calling via the openai SDK.

Parse-failure chain (agreed design): retry up to 2 times by feeding the
parse error back to the model; if it still fails, surface the raw text and
a WARNING event (handled by the loop).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent.adapters.base import (
    BaseAdapter,
    ChatMessage,
    Completion,
    ToolCall,
    ToolSpec,
    ToolCallParseError,
    parse_arguments,
)

logger = logging.getLogger(__name__)

MAX_PARSE_RETRIES = 2


class NativeAdapter(BaseAdapter):
    mode = "native"

    def __init__(
        self,
        client: Any,
        model: str,
        endpoint: str | None = None,
        parse_retries: int = MAX_PARSE_RETRIES,
    ) -> None:
        self._client = client
        self.model = model
        self.endpoint = endpoint
        self.parse_retries = parse_retries

    # -- serialization ----------------------------------------------------

    def to_openai_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            item: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
            if msg.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            out.append(item)
        return out

    def to_openai_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

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
            "messages": self.to_openai_messages(messages),
            "tools": self.to_openai_tools(tools),
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        attempt = 0
        while True:
            raw = await self._client.chat.completions.create(**kwargs)
            try:
                return self._to_completion(raw)
            except ToolCallParseError as parse_error:
                attempt += 1
                if attempt > self.parse_retries:
                    raise
                kwargs["messages"] = self.to_openai_messages(
                    messages
                    + [
                        # assistant must carry the tool_calls reference for the
                        # tool error message to be valid
                        ChatMessage(
                            role="assistant",
                            content=None,
                            tool_calls=[
                                ToolCall(
                                    id=parse_error.tool_call_id,
                                    name=parse_error.name,
                                    arguments={},
                                )
                            ],
                        ),
                        ChatMessage(
                            role="tool",
                            tool_call_id=parse_error.tool_call_id,
                            content=f"ERROR: could not parse tool arguments: {parse_error}",
                        ),
                    ]
                )
                logger.warning("tool-call parse retry %d/%d", attempt, self.parse_retries)

    def _to_completion(self, raw: Any) -> Completion:
        message = raw.choices[0].message
        tool_calls: list[ToolCall] | None = None
        if getattr(message, "tool_calls", None):
            tool_calls = []
            for tc in message.tool_calls:
                raw_arguments = tc.function.arguments or "{}"
                try:
                    arguments = parse_arguments(raw_arguments)
                except Exception:
                    raise ToolCallParseError(
                        tool_call_id=tc.id,
                        name=tc.function.name,
                        raw_arguments=raw_arguments,
                    ) from None
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=arguments)
                )
        usage = None
        if getattr(raw, "usage", None) is not None:
            usage = raw.usage.model_dump()
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=message.content,
                tool_calls=tool_calls,
            ),
            raw=raw,
            usage=usage,
        )