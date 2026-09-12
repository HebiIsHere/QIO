"""Adapter protocol and shared data types."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AdapterMode(str, Enum):
    NATIVE = "native"
    TEXT = "text"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    role: str  # user / assistant / tool / system
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


@dataclass
class Completion:
    message: ChatMessage
    raw: Any = None
    usage: dict[str, Any] | None = None
    # 归一化的结束原因（stop / tool_calls / length / content_filter / None）
    finish_reason: str | None = None

    @property
    def tool_calls(self) -> list[ToolCall] | None:
        return self.message.tool_calls


class ParseError(Exception):
    """Raised when tool-call arguments cannot be parsed."""


class ToolCallParseError(ParseError):
    """The model produced a tool call with invalid arguments."""

    def __init__(self, tool_call_id: str, name: str, raw_arguments: str) -> None:
        super().__init__(f"invalid arguments for tool '{name}': {raw_arguments[:200]!r}")
        self.tool_call_id = tool_call_id
        self.name = name
        self.raw_arguments = raw_arguments


def parse_arguments(raw: str | None) -> dict[str, Any]:
    """Parse JSON tool arguments; tolerate empty strings."""
    if raw is None or raw.strip() == "":
        return {}
    return json.loads(raw)


class BaseAdapter(ABC):
    """Uniform completion interface over native and text modes."""

    mode: AdapterMode
    model: str
    endpoint: str | None

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Run one completion step; returns parsed tool calls (may be empty)."""
        raise NotImplementedError

    def to_chat(self, messages: list[ChatMessage]) -> Completion:
        raise NotImplementedError
