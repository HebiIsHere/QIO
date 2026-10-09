"""Native adapter: OpenAI-compatible tool calling via the openai SDK.

Parse-failure chain (agreed design): retry up to 2 times by feeding the
parse error back to the model; if it still fails, surface the raw text and
a WARNING event (handled by the loop).
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from agent.adapters.base import (
    STREAM_DONE,
    STREAM_TEXT,
    STREAM_TOOL_CALL,
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

MAX_PARSE_RETRIES = 2

# 供应商明确表示「不认识 stream_options」时才去掉它重试一次（窄路径）：
# 其它 4xx 一律按真实错误抛出，不靠重试掩盖。
_STREAM_OPTIONS_HINTS = ("stream_options", "stream options", "include_usage")


def _rejects_stream_options(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _STREAM_OPTIONS_HINTS)


def _stream_rejected(exc: Exception) -> bool:
    """「这条请求形状不被接受」：交给上层整段降级，而不是让整轮失败。

    只覆盖两类，都是有证据的「这里没法流式」：

    * 端点明确拒绝请求（400 家族的 InvalidToolCall）；
    * 客户端根本不返回异步流（UnsupportedCapability）。

    认证 / 限流 / 网络错误**不**在这里降级：它们换一条路径也照样失败，
    应当如实抛出去。
    """
    from agent.adapters.errors import InvalidToolCall, UnsupportedCapability

    return isinstance(exc, (UnsupportedCapability, InvalidToolCall))


def _unsupported_stream(exc: Exception) -> Exception:
    from agent.adapters.errors import UnsupportedCapability

    return UnsupportedCapability(f"streaming not usable on this endpoint: {exc}"[:300])


def _model_dump(obj: Any) -> dict[str, Any] | None:
    """SDK 对象 / dict / 普通对象 → dict（拿不到就 None，绝不猜）。

    真实 openai SDK 给的是 pydantic 模型（有 model_dump）；自定义兼容端点
    可能给 dict 或普通对象 —— 这里都按**同一个字段名**读取之后交给
    ModelUsage.from_provider 归一化，不新增任何猜测。
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    dumper = getattr(obj, "model_dump", None)
    if dumper is not None:
        try:
            payload = dumper()
        except Exception:  # noqa: BLE001 - 用量字段缺失不能影响回答
            payload = None
        if isinstance(payload, dict):
            return payload
    data = getattr(obj, "__dict__", None)
    return dict(data) if isinstance(data, dict) else None


class _ToolCallAccumulator:
    """把流式工具调用碎片攒成一次可执行的调用。

    碎片的到达顺序、id/name 是否分片、参数是否跨 chunk 都不确定；这里只按 index
    归档，最终一次性解析 JSON。中间状态永远不外泄 —— 没攒成合法 JSON 就没有工具
    调用，也就没有任何东西会被执行。
    """

    def __init__(self) -> None:
        self._items: dict[int, dict[str, Any]] = {}

    def add(
        self,
        index: int,
        *,
        call_id: Any = None,
        name: Any = None,
        arguments: Any = None,
    ) -> None:
        item = self._items.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call_id:
            item["id"] = str(call_id)
        if name:
            item["name"] += str(name)
        if arguments:
            item["arguments"] += str(arguments)

    @property
    def seen(self) -> bool:
        return bool(self._items)

    def build(self) -> list[ToolCall] | None:
        if not self._items:
            return None
        from agent.core.narrative import split_narrative_arguments

        calls: list[ToolCall] = []
        for index in sorted(self._items):
            item = self._items[index]
            raw = item["arguments"] or "{}"
            try:
                arguments = parse_arguments(raw)
            except Exception:
                raise ToolCallParseError(
                    tool_call_id=item["id"], name=item["name"], raw_arguments=raw
                ) from None
            arguments, narrative = split_narrative_arguments(arguments)
            calls.append(
                ToolCall(
                    id=item["id"], name=item["name"], arguments=arguments, narrative=narrative
                )
            )
        return calls


def _error_from_status(status: int, body: str) -> Any:
    """HTTP 状态 → 内部错误分类（整段路径由 SDK 抛，这里给裸响应补上同一口径）。"""
    from agent.adapters import errors as e

    if status in (401, 403):
        return e.AuthenticationError(f"provider {status}: {body}")
    if status == 429:
        return e.RateLimitError(f"provider {status}: {body}")
    if 400 <= status < 500:
        return e.InvalidToolCall(f"provider {status}: {body}")
    return e.ProviderInternalError(f"provider {status}: {body}")


class NativeAdapter(BaseAdapter):
    mode = "native"
    # OpenAI 兼容协议的真流式（见 stream()）。
    supports_stream = True

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

    def _request_client(self) -> Any:
        """发起请求用的客户端：**关掉 SDK 自己的自动重试**。

        openai SDK 默认 max_retries=2：5xx / 429 会被**静默重试**。后果不只是多花
        一次钱 —— 我们看到的会是重试后那一次的结果，一次明确的厂商错误可能因此
        变成一个「正常回答」（有状态端点 / 假厂商会把下一个脚本步骤当成功返回）。
        QIO 的语义是：明确的厂商/传输错误**原样上抛** → 整轮如实失败
        （provider_error），由用户决定要不要重试。形状问题（200 但不是 SSE）
        走的是 _stream_once 的 Content-Type 分支，不靠 SDK 重试。
        """
        with_options = getattr(self._client, "with_options", None)
        if with_options is None:
            return self._client  # 假客户端 / 兼容实现：没有这个 API 就原样用
        try:
            return with_options(max_retries=0)
        except Exception:  # noqa: BLE001 - 兼容客户端不认识这个参数时原样用
            return self._client

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
        }
        # 空工具列表**不发送** tools 字段（第四轮契约 §1.1）：空数组在部分兼容端点
        # 会被直接拒（400），而「没有 tools 字段」才是明确的「本次没有工具」——
        # 回答调用（tools=[]）必须能在 native 档正确下发。
        if tools:
            kwargs["tools"] = self.to_openai_tools(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        client = self._request_client()
        attempt = 0
        while True:
            try:
                raw = await client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - normalize provider errors
                from agent.adapters.errors import normalize_error

                raise normalize_error(exc) from exc
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

    # -- real streaming (plan §2.1) ---------------------------------------

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """真 SSE 增量（chat.completions stream=True）。

        边界（与 StreamDelta 的契约一致）：

        * 正文碎片逐片透出，不做任何改写；
        * 工具调用的 id / name / arguments 碎片**只在这里**累积，攒成合法 JSON 之后
          才出现在 kind="done" 的 completion 里 —— 没有 done 就没有可执行的调用；
        * usage 只在流结束时由供应商给出，所以显式要求 stream_options.include_usage；
          供应商不认这个参数时（窄判定）去掉它重试一次，此时用量如实为 None，
          而不是伪造一个 0。
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self.to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # 同 complete()：空工具列表不发 tools 字段（回答调用不带工具）。
        if tools:
            kwargs["tools"] = self.to_openai_tools(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        yielded = False
        try:
            async for delta in self._stream_once(kwargs):
                yielded = True
                yield delta
            return
        except Exception as exc:  # noqa: BLE001 - 两条窄降级路径
            if yielded:
                raise  # 已经透出正文就不能重来（会重复展示）
            if _rejects_stream_options(exc):
                logger.warning("provider rejected stream_options; retrying without usage")
            elif _stream_rejected(exc):
                raise _unsupported_stream(exc) from exc
            else:
                raise
        kwargs.pop("stream_options", None)
        try:
            async for delta in self._stream_once(kwargs):
                yield delta
        except Exception as exc:  # noqa: BLE001 - 同上；此时仍然什么都没发出去
            if _stream_rejected(exc):
                raise _unsupported_stream(exc) from exc
            raise

    async def _stream_once(self, kwargs: dict[str, Any]) -> AsyncIterator[StreamDelta]:
        from agent.adapters.errors import UnsupportedCapability, normalize_error

        # 有 with_raw_response 时用它：**只有这条路能先看到 Content-Type**。
        # 忽略 stream=true 的服务会回 application/json（整段），而 openai SDK 对这种
        # 响应会给出一条零 chunk 的流且不报错（实测 3.13.0）。先看到 Content-Type，
        # 就能把整段 JSON 直接当成这次调用的结果 —— 零额外请求，也不会把按请求
        # 消费脚本的假厂商/有状态端点打乱。
        client = self._request_client()
        completions = getattr(getattr(client, "chat", None), "completions", None)
        raw_sender = getattr(getattr(completions, "with_raw_response", None), "create", None)
        # 看得到 Content-Type（raw 路径）时，「零增量」只说明这一次没有输出，
        # **不能**当成「这条路径用不了流式」的证据（见下面的零增量判断）。
        saw_content_type = False
        try:
            if raw_sender is not None:
                saw_content_type = True
                response = await raw_sender(**kwargs)
                status = int(getattr(response.http_response, "status_code", 200) or 200)
                if status >= 400:
                    body = (await response.http_response.aread()).decode("utf-8", "replace")
                    raise _error_from_status(status, body[:500])
                content_type = str(
                    getattr(response.http_response, "headers", {}).get("content-type") or ""
                ).lower()
                if "text/event-stream" not in content_type:
                    # 这条服务没有按 SSE 回：整段 JSON 就是权威结果。
                    body = await response.http_response.aread()
                    yield StreamDelta(kind=STREAM_DONE, completion=self._completion_from_body(body))
                    return
                raw = response.parse()
            else:
                raw = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalize provider errors
            raise normalize_error(exc) from exc
        if not hasattr(raw, "__aiter__"):
            # 客户端不返回异步流（自定义兼容端点 / 假客户端）：如实降级，
            # 而不是在这里假装拿到了一条流。
            raise UnsupportedCapability("client did not return an async stream")

        accumulator = _ToolCallAccumulator()
        content_parts: list[str] = []
        usage: ModelUsage | None = None
        finish_reason: str | None = None
        try:
            async for chunk in raw:
                chunk_usage = ModelUsage.from_provider(
                    _model_dump(getattr(chunk, "usage", None))
                )
                if chunk_usage is not None:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue  # 有些供应商单独发一个只带 usage 的尾包
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    content_parts.append(text)
                    yield StreamDelta(kind=STREAM_TEXT, text=text)
                fragments = getattr(delta, "tool_calls", None) if delta is not None else None
                for fragment in fragments or []:
                    function = getattr(fragment, "function", None)
                    index = int(getattr(fragment, "index", 0) or 0)
                    accumulator.add(
                        index,
                        call_id=getattr(fragment, "id", None),
                        name=getattr(function, "name", None),
                        arguments=getattr(function, "arguments", None),
                    )
                    # 只通知「出现了工具调用」，碎片参数不往上走。
                    yield StreamDelta(
                        kind=STREAM_TOOL_CALL,
                        index=index,
                        call_id=getattr(fragment, "id", None),
                        name=getattr(function, "name", None),
                    )
                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish_reason = reason
        except Exception as exc:  # noqa: BLE001 - 传输层异常统一归一化
            raise normalize_error(exc) from exc

        if (
            not content_parts
            and not accumulator.seen
            and finish_reason is None
            and not saw_content_type
        ):
            # 一次流式调用**什么增量都没产生**，而且我们连 Content-Type 都没看到
            # （裸客户端 / 假客户端：走不到上面的 with_raw_response 分支）。真实
            # 事故形态：服务忽略 stream=true、直接回整段 JSON，openai SDK 对这种
            # 响应给出一条零 chunk 的流且**不报错**，于是每一轮都变成「没有工具
            # 调用」，而供应商其实回了完整的 tool_calls。
            #
            # 那是兼容性问题，不是「模型没说话」：如实声明这条路径用不了流式，
            # 由 AgentLoop 回退到整段 complete()（**只回退一次**），不假装流式。
            #
            # 已经看到 Content-Type（就是 text/event-stream）时不走这条兜底：
            # 零增量只说明这次没有输出，再打一次 complete() 只会多一个请求，
            # 而且会把「明确的错误」变成另一个请求的结果（第五轮复核 ②）。
            raise UnsupportedCapability(
                "stream produced no increments (provider likely ignored stream=true)"
            )

        # 结束语义（冻结契约 C2）：OpenAI 兼容协议的结束标记就是 finish_reason。
        # 到 EOF 都没有它 = 不完整结束（半截连接 / 缺结束包 / 只有 usage 的空流）。
        incomplete = finish_reason is None
        # 长度截断 / 内容策略：协议合法结束，但工具参数可能被切断 —— 同样不执行。
        truncated = isinstance(finish_reason, str) and finish_reason.lower() in (
            "length",
            "content_filter",
        )
        # 组装在 try 之外：ToolCallParseError 是解析错误，不能被归一化成 provider 错误。
        # 不完整结束 / 截断时**绝不**组装可执行调用：没有合法结束标记就没有合法调用。
        tool_calls = None if (incomplete or truncated) else accumulator.build()
        completion = Completion(
            message=ChatMessage(
                role="assistant",
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
            ),
            raw=None,
            usage=usage,
            finish_reason=finish_reason,
            stream_incomplete=incomplete,
        )
        yield StreamDelta(kind=STREAM_DONE, completion=completion)

    # -- 整段响应（供应商忽略 stream=true）-------------------------------

    def _completion_from_body(self, body: bytes) -> Completion:
        """把一条非 SSE 的整段响应解成 Completion（与 complete() 同一套语义）。"""
        from agent.adapters.errors import ProviderInternalError

        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ProviderInternalError(f"non-stream response is not JSON: {exc}"[:300]) from exc
        if not isinstance(payload, dict):
            raise ProviderInternalError("non-stream response is not a JSON object")
        return self._completion_from_payload(payload)

    @staticmethod
    def _completion_from_payload(payload: dict[str, Any]) -> Completion:
        """OpenAI ChatCompletion JSON → Completion；工具参数仍在这里才解析成 JSON。"""
        from agent.adapters.errors import ProviderInternalError
        from agent.core.narrative import split_narrative_arguments

        choices = payload.get("choices") or []
        if not choices:
            raise ProviderInternalError("non-stream response carries no choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        tool_calls: list[ToolCall] | None = None
        for item in message.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = parse_arguments(raw_arguments)
            except Exception:
                raise ToolCallParseError(
                    tool_call_id=str(item.get("id") or ""),
                    name=str(function.get("name") or ""),
                    raw_arguments=str(raw_arguments),
                ) from None
            arguments, narrative = split_narrative_arguments(arguments)
            if tool_calls is None:
                tool_calls = []
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    narrative=narrative,
                )
            )
        content = message.get("content")
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=content if isinstance(content, str) and content else None,
                tool_calls=tool_calls,
            ),
            usage=ModelUsage.from_provider(payload.get("usage")),
            finish_reason=choice.get("finish_reason"),
        )

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
        usage = None
        if getattr(raw, "usage", None) is not None:
            # 供应商字段在这里就归一化，上层只认 ModelUsage
            usage = ModelUsage.from_provider(raw.usage.model_dump())
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
