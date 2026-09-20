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


_INPUT_KEYS = ("prompt_tokens", "input_tokens", "prompt_token_count", "input_tokens_total")
_OUTPUT_KEYS = ("completion_tokens", "output_tokens", "candidates_token_count")
_TOTAL_KEYS = ("total_tokens",)


def _pick_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class ModelUsage:
    """统一的模型用量语义（供应商差异只允许存在于 Adapter 层）。

    上层（AgentLoop / Trace / UI / 成本估算）只允许读这三个字段，
    不再区分 OpenAI 的 `prompt_tokens/completion_tokens` 与 Anthropic 的
    `input_tokens/output_tokens`。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if self.total_tokens <= 0:
            # 供应商没给 total 时由 input + output 推导，而不是留一个 0。
            object.__setattr__(self, "total_tokens", self.input_tokens + self.output_tokens)

    @classmethod
    def from_provider(cls, payload: Any) -> "ModelUsage | None":
        """把任意供应商形状的 usage 归一化；拿不到任何字段时返回 None。"""
        if payload is None:
            return None
        if isinstance(payload, ModelUsage):
            return payload
        if not isinstance(payload, dict):
            return None
        if not payload:
            return None
        input_tokens = _pick_int(payload, _INPUT_KEYS) or 0
        output_tokens = _pick_int(payload, _OUTPUT_KEYS) or 0
        total_tokens = _pick_int(payload, _TOTAL_KEYS) or 0
        if not any(key in payload for key in (*_INPUT_KEYS, *_OUTPUT_KEYS, *_TOTAL_KEYS)):
            return None
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


@dataclass
class Completion:
    message: ChatMessage
    raw: Any = None
    # 统一语义：Adapter 负责把供应商差异转换成 ModelUsage
    usage: "ModelUsage | None" = None
    # 归一化的结束原因（stop / tool_calls / length / content_filter / None）
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        # 兼容：仍然允许直接传供应商形状的 dict，构造时归一化。
        if self.usage is not None and not isinstance(self.usage, ModelUsage):
            self.usage = ModelUsage.from_provider(self.usage)

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
    # 工具定义是否被拼进 system prompt（text 兼容档如此）。
    # 影响上下文预算：走 API tools 字段与拼进 prompt 只能算一次。
    tools_in_prompt: bool = False

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

    # -- 请求开销估算（上下文预算用） --------------------------------------

    def system_prompt_text(self, tools: list[ToolSpec]) -> str:
        """本 Adapter 会**额外**拼进请求的 system prompt 文本。

        原生工具调用协议把 system prompt 交给调用方（messages 里自带），
        返回空串；text 兼容档需要把工具说明写进 prompt，必须如实返回，
        否则预算会高估可用空间。
        """
        return ""

    def protocol_overhead_tokens(self, tools: list[ToolSpec]) -> int:
        """协议本身带来的固定开销（角色标记、工具调用信封等）。"""
        return PROTOCOL_OVERHEAD_TOKENS


# chat 协议的角色标记 / 工具信封等固定开销的经验值（宁可略高，不可低估）。
PROTOCOL_OVERHEAD_TOKENS = 24
