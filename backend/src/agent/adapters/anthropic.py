"""Anthropic official API adapter (/v1/messages protocol).

Key conversion differences vs OpenAI:
- system is a top-level request field, not a message role;
- tool results are content blocks inside a user message (tool_result),
  merged after the assistant message that issued the tool_use;
- max_tokens is required;
- tool_use.input arrives as a structured object (not a JSON string).
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from agent.adapters.base import (
    STREAM_DONE,
    STREAM_TEXT,
    STREAM_TOOL_CALL,
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    ModelUsage,
    StreamDelta,
    ToolCall,
    ToolSpec,
    ToolCallParseError,
    parse_arguments,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 4096
ANTHROPIC_VERSION = "2023-06-01"
MAX_PARSE_RETRIES = 2


class AnthropicAdapter(BaseAdapter):
    mode = "native"  # tool calling is native to the Anthropic protocol
    # Anthropic 的 SSE：content_block_delta / input_json_delta（见 stream()）。
    supports_stream = True

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
        payload = self._payload(messages, tools, temperature, max_tokens)

        attempt = 0
        while True:
            raw = await self._post(payload)
            try:
                return self._to_completion(raw)
            except ToolCallParseError as parse_error:
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

    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """一次请求的完整载荷（整段与流式共用，避免两条路径漂移）。"""
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
        return payload

    # -- real streaming (plan §2.1) ----------------------------------------

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """真 SSE 增量（/v1/messages stream=true）。

        Anthropic 的事件形状与 OpenAI 不同，但契约一致：

        * text_delta → 正文增量；
        * tool_use 的 input 以 input_json_delta 分片到达，**只在这里**拼接，
          攒成合法 JSON 才进入 kind="done" 的 completion；
        * usage 在 message_start（输入）与 message_delta（输出）分别给出，
          两者都没有时如实为 None，不伪造 0。
        """
        from agent.adapters import errors as e

        payload = self._payload(messages, tools, temperature, max_tokens)
        payload["stream"] = True

        # index → 内容块。文本块累积成 text，工具块累积 input 的 JSON 碎片。
        blocks: dict[int, dict[str, Any]] = {}
        input_tokens = 0
        output_tokens = 0
        stop_reason: str | None = None
        url = f"{self.endpoint}/messages"
        try:
            async with self._client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise self._http_error(resp.status_code, body)
                async for line in resp.aiter_lines():
                    raw = line.strip()
                    if not raw.startswith("data:"):
                        continue  # event: / 心跳 / 空行都不携带内容
                    body_text = raw[5:].strip()
                    if not body_text or body_text == "[DONE]":
                        continue
                    try:
                        event = json.loads(body_text)
                    except json.JSONDecodeError:
                        continue  # 半行：忽略，绝不猜
                    etype = event.get("type")
                    if etype == "message_start":
                        message = event.get("message") or {}
                        usage = message.get("usage") or {}
                        input_tokens = int(usage.get("input_tokens") or 0)
                        stop_reason = message.get("stop_reason") or stop_reason
                    elif etype == "content_block_start":
                        index = int(event.get("index") or 0)
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            blocks[index] = {
                                "type": "tool_use",
                                "id": str(block.get("id") or ""),
                                "name": str(block.get("name") or ""),
                                "json": "",
                            }
                            yield StreamDelta(
                                kind=STREAM_TOOL_CALL,
                                index=index,
                                call_id=str(block.get("id") or "") or None,
                                name=str(block.get("name") or "") or None,
                            )
                        elif block.get("type") == "text":
                            blocks[index] = {"type": "text", "text": ""}
                            initial = block.get("text") or ""
                            if initial:
                                blocks[index]["text"] += initial
                                yield StreamDelta(kind=STREAM_TEXT, text=initial)
                    elif etype == "content_block_delta":
                        index = int(event.get("index") or 0)
                        delta = event.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                entry = blocks.setdefault(index, {"type": "text", "text": ""})
                                entry["text"] += text
                                yield StreamDelta(kind=STREAM_TEXT, text=text)
                        elif dtype == "input_json_delta":
                            entry = blocks.setdefault(
                                index,
                                {"type": "tool_use", "id": "", "name": "", "json": ""},
                            )
                            entry["json"] += delta.get("partial_json") or ""
                    elif etype == "message_delta":
                        usage = event.get("usage") or {}
                        if usage.get("output_tokens") is not None:
                            output_tokens = int(usage.get("output_tokens") or 0)
                        delta = event.get("delta") or {}
                        if delta.get("stop_reason"):
                            stop_reason = str(delta["stop_reason"])
                    elif etype == "error":
                        detail = (event.get("error") or {}).get("message") or "anthropic stream error"
                        raise e.ProviderInternalError(str(detail)[:300])
        except e.ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise e.normalize_error(exc) from exc

        # 与 native 同一条兼容性口径：一个内容块都没解析出来、也没有 stop_reason、
        # 也没有任何 usage，说明这条服务很可能忽略了 stream=true（回了整段 JSON，
        # SSE 行里什么都没有）。如实声明用不了流式，让上层整段回退一次。
        if not blocks and stop_reason is None and not input_tokens and not output_tokens:
            from agent.adapters import errors as e

            raise e.UnsupportedCapability(
                "stream produced no content blocks (provider likely ignored stream=true)"
            )

        # 组装放在异常处理之外：ToolCallParseError 是解析错误，不该被归一化掉。
        text_parts = [
            blocks[i]["text"]
            for i in sorted(blocks)
            if blocks[i].get("type") == "text" and blocks[i].get("text")
        ]
        tool_calls: list[ToolCall] | None = None
        from agent.core.narrative import split_narrative_arguments

        for index in sorted(blocks):
            entry = blocks[index]
            if entry.get("type") != "tool_use":
                continue
            raw_json = entry.get("json") or "{}"
            try:
                arguments = parse_arguments(raw_json)
            except Exception:
                raise ToolCallParseError(
                    tool_call_id=str(entry.get("id") or ""),
                    name=str(entry.get("name") or ""),
                    raw_arguments=raw_json,
                ) from None
            arguments, narrative = split_narrative_arguments(arguments)
            if tool_calls is None:
                tool_calls = []
            tool_calls.append(
                ToolCall(
                    id=str(entry.get("id") or ""),
                    name=str(entry.get("name") or ""),
                    arguments=arguments,
                    narrative=narrative,
                )
            )
        usage = None
        if input_tokens or output_tokens:
            usage = ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        yield StreamDelta(
            kind=STREAM_DONE,
            completion=Completion(
                message=ChatMessage(
                    role="assistant",
                    content="\n".join(text_parts) if text_parts else None,
                    tool_calls=tool_calls,
                ),
                raw=None,
                finish_reason=stop_reason,
                usage=usage,
            ),
        )

    def _http_error(self, status: int, body: str) -> Exception:
        """HTTP 状态 → 内部错误分类（整段与流式共用同一套口径）。"""
        from agent.adapters import errors as e

        if status in (401, 403):
            return e.AuthenticationError(f"anthropic {status}: {body}")
        if status == 429:
            return e.RateLimitError(f"anthropic 429: {body}")
        if 400 <= status < 500:
            return e.InvalidToolCall(f"anthropic {status}: {body}")
        return e.ProviderInternalError(f"anthropic {status}: {body}")

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        from agent.adapters import errors as e

        try:
            resp = await self._client.post(f"{self.endpoint}/messages", json=payload)
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise e.NetworkError(str(exc)[:300]) from exc
        if resp.status_code >= 400:
            raise self._http_error(resp.status_code, resp.text[:500])
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
) -> str:
    """Probe an Anthropic endpoint with a minimal tool-calling request.

    Returns "native" on success; raises on auth/network errors.
    """
    adapter = AnthropicAdapter(api_key=api_key, model=model, endpoint=endpoint, max_tokens=8)
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
