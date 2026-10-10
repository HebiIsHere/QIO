"""Anthropic official API adapter (/v1/messages protocol).

Key conversion differences vs OpenAI:
- system is a top-level request field, not a message role;
- tool results are content blocks inside a user message (tool_result),
  merged after the assistant message that issued the tool_use;
- max_tokens is required;
- tool_use.input arrives as a structured object (not a JSON string).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agent.adapters.base import (
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    ModelUsage,
    ToolCall,
    ToolSpec,
    ToolCallParseError,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
ANTHROPIC_VERSION = "2023-06-01"
MAX_PARSE_RETRIES = 2


class AnthropicAdapter(BaseAdapter):
    mode = "native"  # tool calling is native to the Anthropic protocol
    # 每次实际请求自己记账（含内部解析重试的每次响应）—— 只记一处，见 credentials/usage.py。
    accounts_requests = True

    def __init__(
        self,
        api_key: str,
        model: str,
        endpoint: str = "https://api.anthropic.com/v1",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        parse_retries: int = MAX_PARSE_RETRIES,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.max_tokens = max_tokens
        self.parse_retries = parse_retries
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- message conversion ----------------------------------------------

    def _extract_system(self, messages: list[ChatMessage]) -> str:
        parts = [m.content or "" for m in messages if m.role == "system"]
        return "\n".join(p for p in parts if p)

    def to_anthropic_messages(
        self, messages: list[ChatMessage]
    ) -> list[dict[str, Any]]:
        """Convert our message list into Anthropic messages.

        Rules enforced:
        - system messages are dropped (handled by _extract_system);
        - consecutive user messages are merged;
        - tool results merge into a single user message with tool_result
          blocks, emitted right after the assistant message that had the
          tool_use (Anthropic requires tool_result in the message
          immediately following the tool_use).
        """
        out: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        def flush_tool_results() -> None:
            if pending_tool_results:
                out.append({"role": "user", "content": pending_tool_results[:]})
                pending_tool_results.clear()

        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool":
                pending_tool_results.append(
                    {"type": "tool_result", "tool_use_id": msg.tool_call_id or "", "content": msg.content or ""}
                )
                continue
            if msg.role == "assistant":
                flush_tool_results()
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            # user
            flush_tool_results()
            content: Any = msg.content or ""
            if out and out[-1]["role"] == "user":
                # merge consecutive user messages
                prev = out[-1]["content"]
                if isinstance(prev, str) and isinstance(content, str):
                    out[-1]["content"] = prev + "\n\n" + content
                else:
                    # tool_result messages keep their block shape; a fresh
                    # user message after them stays separate
                    out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": content})
        flush_tool_results()
        return out

    def to_anthropic_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
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
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "messages": self.to_anthropic_messages(messages),
        }
        system = self._extract_system(messages)
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self.to_anthropic_tools(tools)
        if temperature is not None:
            payload["temperature"] = temperature

        attempt = 0
        # 函数内导入：adapter 层不在导入期依赖凭据库（既有导入顺序约束）。
        from agent.credentials import usage as accounting

        while True:
            # 每次实际请求（含重试）之前核对累计用量；耗尽抛 BudgetExhausted，不再发新请求。
            accounting.ensure_adapter_request_allowed(self)
            try:
                raw = await self._post(payload)
            except Exception as exc:  # noqa: BLE001 - 失败且没有用量：标 incomplete，不造数
                accounting.account_adapter_failure(self, exc)
                raise
            usage = self._usage_of(raw)
            try:
                completion = self._to_completion(raw)
            except ToolCallParseError as parse_error:
                # 这次响应带用量就如实记下（失败也记已知用量）；没有用量才标 incomplete。
                accounting.account_adapter_request(
                    self,
                    usage,
                    failed=True,
                    reason=None
                    if usage is not None
                    else f"工具输入解析失败：{type(parse_error).__name__}",
                )
                attempt += 1
                if attempt > self.parse_retries:
                    raise
                # corrective retry: feed the tool error back to the model
                payload["messages"] = self.to_anthropic_messages(
                    messages
                    + [
                        ChatMessage(role="assistant", content=None, tool_calls=[
                            ToolCall(id=parse_error.tool_call_id, name=parse_error.name, arguments={})
                        ]),
                        ChatMessage(
                            role="tool",
                            tool_call_id=parse_error.tool_call_id,
                            content=f"ERROR: could not parse tool input: {parse_error}",
                        ),
                    ]
                )
                logger.warning("anthropic tool parse retry %d/%d", attempt, self.parse_retries)
                continue
            # 成功的一次响应：记一次真实用量（进 / 出分开）。
            accounting.account_adapter_request(self, usage)
            return completion

    @staticmethod
    def _usage_of(raw: Any) -> ModelUsage | None:
        """Anthropic 的 usage 在响应体里；没有就不猜（不造数）。"""
        if not isinstance(raw, dict):
            return None
        return ModelUsage.from_provider(raw.get("usage"))

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        from agent.adapters import errors as e

        try:
            resp = await self._client.post(f"{self.endpoint}/messages", json=payload)
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise e.NetworkError(str(exc)[:300]) from exc
        if resp.status_code >= 400:
            body = resp.text[:500]
            if resp.status_code in (401, 403):
                raise e.AuthenticationError(f"anthropic {resp.status_code}: {body}")
            if resp.status_code == 429:
                raise e.RateLimitError(f"anthropic 429: {body}")
            if 400 <= resp.status_code < 500:
                raise e.InvalidToolCall(f"anthropic {resp.status_code}: {body}")
            raise e.ProviderInternalError(f"anthropic {resp.status_code}: {body}")
        return resp.json()

    def _to_completion(self, raw: dict[str, Any]) -> Completion:
        content_blocks = raw.get("content") or []
        tool_calls: list[ToolCall] | None = None
        text_parts: list[str] = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    raise ToolCallParseError(
                        tool_call_id=block.get("id") or "",
                        name=block.get("name") or "",
                        raw_arguments=str(tool_input),
                    )
                tool_calls.append(
                    ToolCall(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        arguments=tool_input,
                    )
                )
        usage = raw.get("usage") or {}
        return Completion(
            message=ChatMessage(
                role="assistant",
                content="\n".join(text_parts) if text_parts else None,
                tool_calls=tool_calls,
            ),
            raw=raw,
            finish_reason=raw.get("stop_reason"),
            # Anthropic 的 input/output 在这里归一化为统一语义
            usage=ModelUsage.from_provider(usage),
        )


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def is_anthropic_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint and "anthropic.com" in endpoint.lower())


async def probe_anthropic(
    api_key: str,
    model: str,
    endpoint: str = "https://api.anthropic.com/v1",
    *,
    key_id: str | None = None,
    accounting_store: Any = None,
) -> str:
    """Probe an Anthropic endpoint with a minimal tool-calling request.

    Returns "native" on success; raises on auth/network errors.

    A06：这次探测**本身就是一次真实请求**，所以它要和其它直接调用路径一样归因到
    建它的那把凭据 —— 传了 `key_id` + `accounting_store` 就显式绑定（受同一份预算与
    账本约束）；缺任一参数时保持旧行为（不假装绑定，也不把账记到别的上下文头上）。
    """
    adapter = AnthropicAdapter(api_key=api_key, model=model, endpoint=endpoint, max_tokens=8)
    if key_id:
        adapter.key_id = str(key_id)
    if key_id and accounting_store is not None:
        from agent.credentials.usage import bind_request_accounting

        bind_request_accounting(adapter, accounting_store)
    try:
        completion = await adapter.complete(
            [ChatMessage(role="user", content="ping")],
            [
                ToolSpec(
                    name="ping",
                    description="Returns pong.",
                    parameters={"type": "object", "properties": {}},
                )
            ],
            max_tokens=8,
        )
        return "native"
    finally:
        await adapter.close()
