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
    ModelUsage,
    ToolCall,
    ToolSpec,
    ToolCallParseError,
    parse_arguments,
)

logger = logging.getLogger(__name__)

MAX_PARSE_RETRIES = 2


class NativeAdapter(BaseAdapter):
    mode = "native"
    # 这条 adapter 在**每次实际请求**上自己记账（见 credentials/usage.py）：
    # 主循环、子任务、后台维护、派生提炼共用同一个实例，所以记账只有一处。
    accounts_requests = True

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
        # 函数内导入：adapter 层不在导入期依赖凭据库（既有导入顺序约束）。
        from agent.credentials import usage as accounting

        while True:
            # 每次实际请求（含内部解析重试的每一次）之前先核对累计用量：
            # 已确认耗尽就不再发新请求（耗尽抛 BudgetExhausted，交给上层收口）。
            accounting.ensure_adapter_request_allowed(self)
            try:
                raw = await self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - normalize provider errors
                from agent.adapters.errors import normalize_error

                # 失败且没有用量：明确标 incomplete，不造数（结果照旧抛出）。
                accounting.account_adapter_failure(self, exc)
                raise normalize_error(exc) from exc
            usage = self._usage_of(raw)
            try:
                completion = self._to_completion(raw)
            except ToolCallParseError as parse_error:
                # 这次响应可能有真实用量：失败也要记下已知实际用量；
                # 没有用量时标记这次请求的记账不完整。
                accounting.account_adapter_request(
                    self,
                    usage,
                    failed=True,
                    reason=None
                    if usage is not None
                    else f"工具参数解析失败：{type(parse_error).__name__}",
                )
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
                continue
            # 成功的一次响应：记一次真实用量（进 / 出分开）。
            accounting.account_adapter_request(self, usage)
            return completion

    @staticmethod
    def _usage_of(raw: Any) -> ModelUsage | None:
        """把供应商形状的 usage 归一化；没有就返回 None（不猜）。"""
        raw_usage = getattr(raw, "usage", None)
        if raw_usage is None:
            return None
        if hasattr(raw_usage, "model_dump"):
            return ModelUsage.from_provider(raw_usage.model_dump())
        if isinstance(raw_usage, dict):
            return ModelUsage.from_provider(raw_usage)
        return None

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
                # 模型给的过程说明信封在这里剥离：工具与风险判断只看 arguments。
                from agent.core.narrative import split_narrative_arguments

                arguments, narrative = split_narrative_arguments(arguments)
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                        narrative=narrative,
                    )
                )
        usage = self._usage_of(raw)
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=message.content,
                tool_calls=tool_calls,
            ),
            raw=raw,
            usage=usage,
            finish_reason=getattr(raw.choices[0], "finish_reason", None),
        )
