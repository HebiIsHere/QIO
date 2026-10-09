"""Adapter protocol and shared data types."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator


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
    # 模型给的可选叙事信封（已从 arguments 剥离，见 core/narrative.py）。
    # compare=False：不进相等比较，也不会因为里面是 dict 而影响 hash。
    narrative: dict[str, Any] | None = field(default=None, compare=False)


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
    # 流式结束语义（冻结契约 C2）：True = 消费了一条流，但到 EOF 都没看到该协议
    # 的结束标记（OpenAI 兼容看 finish_reason，Anthropic 看 message_stop）。
    # 已确认文本保留，但**不得**当成正常完成，也**不得**执行未确认结束的工具调用。
    # 非流式路径（整段 JSON）与脚本化假 adapter 默认 False。
    stream_incomplete: bool = False

    def __post_init__(self) -> None:
        # 兼容：仍然允许直接传供应商形状的 dict，构造时归一化。
        if self.usage is not None and not isinstance(self.usage, ModelUsage):
            self.usage = ModelUsage.from_provider(self.usage)

    @property
    def tool_calls(self) -> list[ToolCall] | None:
        return self.message.tool_calls


# -- 流式增量（plan §2.1）-------------------------------------------------

STREAM_TEXT = "text"
STREAM_TOOL_CALL = "tool_call"
STREAM_DONE = "done"


@dataclass(frozen=True)
class StreamDelta:
    """一次模型调用流出的一段增量。

    契约（plan §2.1，调用方必须按这个边界使用）：

    * kind="text"：正文增量。它可能是半个词、半个字，调用方只允许**拼接**，
      不得把它当作完整语义去解析，更不得据此执行任何东西。
    * kind="tool_call"：出现了工具调用增量（可能只是一个碎片）。它的
      name / call_id 只用于「这条响应是工具轮」的判定与诊断；**碎片参数不在
      这里暴露** —— 参数只在 adapter 内部组装，永远不当作正文展示，也永远
      不执行未完成参数。
    * kind="done"：流结束，completion 是组装完成的整段结果，也是**唯一**
      可以交给工具执行与落库的入口。没有 done = 流被中断/出错：已确认文本
      保留，但不得执行任何调用，也不得把半截 JSON 当结果。

    adapter 必须保证 kind="done" 至多出现一次，且是最后一段。
    """

    kind: str
    text: str = ""
    # 工具调用序号（同一响应内从 0 开始）：碎片按它归档，不靠到达顺序猜。
    index: int = 0
    call_id: str | None = None
    name: str | None = None
    completion: "Completion | None" = None


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
    # 这条 adapter 用的是哪把凭据（用量归因用）。构造它的地方负责填；
    # 测试里的假 adapter 可以没有，此时用量不记账，而不是记到别人头上。
    key_id: str | None = None
    # 工具定义是否被拼进 system prompt（text 兼容档如此）。
    # 影响上下文预算：走 API tools 字段与拼进 prompt 只能算一次。
    tools_in_prompt: bool = False

    # 这条 adapter 是否**真的**能边生成边返回（plan §2.1）。
    # 默认 False：不支持就由 AgentLoop 走整段降级，**不假装流式**。
    supports_stream: bool = False

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

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """真流式增量（见 StreamDelta 的契约）。

        默认实现直接抛 NotImplementedError：调用方（AgentLoop）据此走整段降级，
        而不是误以为拿到了一条流。声明 supports_stream = True 的子类必须给出真实
        实现 —— 「声明支持但抛异常」同样会被降级，不会假装流式。
        """
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")
        # 下面这行不可达，只是让本方法保持异步生成器的形状（async for 可用）。
        yield StreamDelta(kind=STREAM_DONE)  # pragma: no cover

    def to_chat(self, messages: list[ChatMessage]) -> Completion:
        raise NotImplementedError

    # -- 请求开销估算（上下文预算用） --------------------------------------

    def system_prompt_text(self, tools: list[ToolSpec]) -> str:
        """本 Adapter 会**额外**拼进请求的 system prompt 文本。

        原生工具调用协议把 system prompt 交给调用方（messages 里自带），
        返回空串；text 兼容档需要把工具说明写进 prompt，必须如实返回，
        否则预算会高估可用空间。

        注意：内容角色协议（prompts.CONTENT_ROLE_PROTOCOL）不在这里返回 ——
        它不是本 Adapter 拼的：native / anthropic 由 core/loop.py 作为 system
        消息注入（见 protocol_in_prompt），text 档由 build_system_prompt 拼进去。
        """
        return ""

    # 本档位是否**自己**把内容角色协议拼进了 system prompt（第五轮契约 §1.1）。
    # False = 由 core/loop.py 每次调用作为最后一条 system 消息注入；
    # True  = 已经在自己的 system prompt 里（text 兼容档），循环不再重复注入。
    protocol_in_prompt: bool = False

    def protocol_overhead_tokens(self, tools: list[ToolSpec]) -> int:
        """协议本身带来的固定开销（角色标记、工具调用信封等）。"""
        return PROTOCOL_OVERHEAD_TOKENS


# chat 协议的角色标记 / 工具信封等固定开销的经验值（宁可略高，不可低估）。
PROTOCOL_OVERHEAD_TOKENS = 24
