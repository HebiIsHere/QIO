"""脚本化的假 provider：给测试、联调与演示用（**不联网、不读任何密钥**）。

真实 provider 的 SSE 解析在 native.py / anthropic.py 里；这里只提供一条受脚本控制
的流，让上层（AgentLoop / 服务层 / 前端联调）能在确定性的时间线上验证 plan §2.1
与验收第 3 条的每一条要求：分次返回正文、工具参数碎片组装、中途取消（hold）、
错误、usage、以及「声明支持流式但其实不支持」的降级路径。

用法：

    adapter = FakeStreamAdapter([
        StreamScript(text_chunks=["你好", "，世界"]),
        StreamScript(
            text="我先读一下文件。",
            tool_calls=[ScriptedToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})],
        ),
    ])

约定：

* 分片只影响「怎么发」，不影响「发什么」：`complete()` 与 `stream()` 得到同一段文本；
* 工具参数永远以碎片形式流出，但只有组装成合法 JSON 之后才出现在 `kind="done"` 的
  completion 里 —— 与真实 adapter 的边界一模一样；
* `hold` / `hold_after` 用来把「provider 还没结束」这一刻钉死，供测试断言前端已收到正文。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

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

# 参数碎片的默认切法：小到足以证明「碎片确实被拼起来了」。
DEFAULT_FRAGMENT_SIZE = 4


@dataclass
class ScriptedToolCall:
    """一条脚本化的工具调用。

    arguments 允许直接给 dict（内部转 JSON 再切碎）或给 JSON 文本（可以故意给坏 JSON，
    用来验证「非法 JSON 绝不执行」）。
    """

    id: str
    name: str
    arguments: Any = None
    fragment_size: int = DEFAULT_FRAGMENT_SIZE
    narrative: dict | None = None

    def raw_arguments(self) -> str:
        if isinstance(self.arguments, str):
            return self.arguments
        if self.arguments is None:
            return "{}"
        return json.dumps(self.arguments, ensure_ascii=False)

    def fragments(self) -> list[str]:
        raw = self.raw_arguments()
        size = max(1, int(self.fragment_size))
        return [raw[i : i + size] for i in range(0, len(raw), size)] or [""]


@dataclass
class StreamScript:
    """一次模型调用的完整脚本。"""

    text: str = ""
    # 显式分片优先于 text；空串分片会被原样跳过（不发空事件）。
    text_chunks: list[str] | None = None
    tool_calls: list[ScriptedToolCall] = field(default_factory=list)
    # 工具调用增量插在第几个正文分片之后（None = 正文全部发完之后）。
    tool_call_after: int | None = None
    # 每一段之间的间隔（毫秒）：用来制造「守卫窗口」与发布节奏的真实时间差。
    gap_ms: float = 0.0
    # 发到第 hold_after 段之后停住等 hold 事件（测试用它把「流未结束」钉死）。
    hold_after: int | None = None
    hold: asyncio.Event | None = None
    # 发到第 error_after 段之后抛 error（None = 不抛）。
    error: Exception | None = None
    error_after: int | None = None
    usage_in: int = 0
    usage_out: int = 0
    finish_reason: str | None = None

    def resolved_chunks(self) -> list[str]:
        if self.text_chunks is not None:
            return list(self.text_chunks)
        return [self.text] if self.text else []


class FakeStreamAdapter(BaseAdapter):
    """按脚本流式返回的假 adapter（也实现了 complete()，供降级路径对比）。"""

    mode = AdapterMode.NATIVE

    def __init__(
        self,
        scripts: list[StreamScript] | None = None,
        *,
        model: str = "fake-stream",
        stream_supported: bool = True,
    ) -> None:
        self._scripts = list(scripts or [])
        self._index = 0
        self.model = model
        self.endpoint = None
        self.supports_stream = bool(stream_supported)
        # 这条 adapter 收到的请求，测试用来断言「确实走了流式分支」。
        self.requests: list[dict] = []

    # -- script bookkeeping ------------------------------------------------

    def _next(self) -> StreamScript:
        if self._index < len(self._scripts):
            script = self._scripts[self._index]
            self._index += 1
            return script
        self._index += 1
        return StreamScript(text="", finish_reason="stop")

    def _usage(self, script: StreamScript) -> ModelUsage | None:
        if not script.usage_in and not script.usage_out:
            return None
        return ModelUsage(
            input_tokens=script.usage_in, output_tokens=script.usage_out
        )

    def _build_tool_calls(self, script: StreamScript) -> list[ToolCall] | None:
        """碎片组装：这里与真实 adapter 是同一套语义（合法 JSON 才成立）。"""
        if not script.tool_calls:
            return None
        calls: list[ToolCall] = []
        for item in script.tool_calls:
            raw = "".join(item.fragments())
            try:
                arguments = parse_arguments(raw)
            except Exception:
                raise ToolCallParseError(
                    tool_call_id=item.id, name=item.name, raw_arguments=raw
                ) from None
            from agent.core.narrative import split_narrative_arguments

            narrative = item.narrative
            if narrative is None:
                arguments, narrative = split_narrative_arguments(arguments)
            calls.append(
                ToolCall(id=item.id, name=item.name, arguments=arguments, narrative=narrative)
            )
        return calls

    def _completion(self, script: StreamScript) -> Completion:
        # 分片只影响「怎么发」，不影响「发什么」：complete() 与 stream() 得到同一段文本。
        text = script.text or "".join(script.resolved_chunks())
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=text or None,
                tool_calls=self._build_tool_calls(script),
            ),
            usage=self._usage(script),
            finish_reason=script.finish_reason,
        )

    # -- steps -------------------------------------------------------------

    @staticmethod
    def _steps(script: StreamScript) -> list[tuple[str, int]]:
        """(类型, 序号) 的完整发放计划：text / tool / done 各算一步。"""
        steps: list[tuple[str, int]] = [
            ("text", i) for i, _ in enumerate(script.resolved_chunks())
        ]
        insert_at = len(steps) if script.tool_call_after is None else max(0, script.tool_call_after + 1)
        tool_steps = [("tool", i) for i in range(len(script.tool_calls))]
        steps[insert_at:insert_at] = tool_steps
        steps.append(("done", 0))
        return steps

    async def _pause(self, script: StreamScript, emitted: int) -> None:
        if script.hold is not None and script.hold_after == emitted:
            await script.hold.wait()
        if script.gap_ms > 0:
            await asyncio.sleep(script.gap_ms / 1000.0)

    # -- completion --------------------------------------------------------

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        self.requests.append({"messages": messages, "tools": tools, "stream": False})
        script = self._next()
        if script.error is not None:
            raise script.error
        return self._completion(script)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamDelta]:
        self.requests.append({"messages": messages, "tools": tools, "stream": True})
        script = self._next()
        chunks = script.resolved_chunks()
        emitted = 0
        for kind, index in self._steps(script):
            if kind == "text":
                chunk = chunks[index]
                if chunk:
                    emitted += 1
                    yield StreamDelta(kind=STREAM_TEXT, text=chunk)
                    await self._pause(script, emitted)
                    self._maybe_fail(script, emitted)
                continue
            if kind == "tool":
                item = script.tool_calls[index]
                for i, fragment in enumerate(item.fragments()):
                    yield StreamDelta(
                        kind=STREAM_TOOL_CALL,
                        index=index,
                        call_id=item.id if i == 0 else None,
                        name=item.name if i == 0 else None,
                    )
                emitted += 1
                await self._pause(script, emitted)
                self._maybe_fail(script, emitted)
                continue
            # done：唯一携带可执行结果的增量
            yield StreamDelta(kind=STREAM_DONE, completion=self._completion(script))

    def _maybe_fail(self, script: StreamScript, emitted: int) -> None:
        if script.error is not None and script.error_after == emitted:
            raise script.error