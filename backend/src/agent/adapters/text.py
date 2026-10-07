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
    ModelUsage,
    ToolCall,
    ToolSpec,
)
from agent.prompts import (
    CONTENT_ROLE_PROTOCOL,
    SYSTEM_PROMPT_TEXT_MODE,
    SYSTEM_PROMPT_TOOLS_HEADER,
    TEXT_TOOL_ENTRY,
)

logger = logging.getLogger(__name__)

SUCCESS_RATE_THRESHOLD = 0.6

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 出现这些迹象说明模型**想**按工具协议回复，但格式坏了 —— 这才是协议失败。
_PROTOCOL_HINTS = ("tool_calls", "tool_name", '"arguments"')


class TextAdapter(BaseAdapter):
    mode = "text"
    # 没有原生工具协议：工具定义只能拼进 system prompt
    tools_in_prompt = True
    # 明确降级（plan §2.1 第 8 条）：这条路径**不支持**实时生成，
    # AgentLoop 会一次性给出 {streaming: false}，前端如实提示
    # 「该模型路径不支持实时生成」，而不是假装一片一片地出字。
    supports_stream = False

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
                TEXT_TOOL_ENTRY.format(
                    name=t.name,
                    description=t.description,
                    schema=json.dumps(t.parameters, ensure_ascii=False),
                )
            )
        return "\n".join(lines)

    # text 档自己把内容角色协议拼进 system prompt（见 build_system_prompt）：
    # core/loop.py 据此不再重复注入（同一份措辞只出现一次）。
    protocol_in_prompt = True

    def build_system_prompt(self, tools: list[ToolSpec]) -> str:
        tools_block = self.build_text_tools(tools)
        if tools_block:
            tools_block = f"{SYSTEM_PROMPT_TOOLS_HEADER}\n{tools_block}"
        # 内容角色协议（第五轮契约 §1.1）：三档都要能看到，text 档拼在这里。
        return f"{SYSTEM_PROMPT_TEXT_MODE}\n\n{CONTENT_ROLE_PROTOCOL}\n\n{tools_block}"

    def system_prompt_text(self, tools: list[ToolSpec]) -> str:
        """text 档真正会发出去的 system prompt（含全部工具 schema）。"""
        return self.build_system_prompt(tools)

    def protocol_overhead_tokens(self, tools: list[ToolSpec]) -> int:
        # 除了统一的角色标记，text 档还要在提示里保留 JSON 输出格式说明的余量
        return super().protocol_overhead_tokens(tools) + 32

    # -- completion -------------------------------------------------------

    def _request_client(self) -> Any:
        """发起请求用的客户端：**关掉 SDK 自己的自动重试**（与 native 档同一口径）。

        openai SDK 默认 max_retries=2：明确的厂商/传输错误会被静默重试，我们看到
        的是重试后那一次的结果 —— 一次 5xx 可能因此变成一个「正常回答」。QIO 的
        语义是原样上抛 → 整轮如实失败（provider_error）。
        """
        with_options = getattr(self._client, "with_options", None)
        if with_options is None:
            return self._client
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
            "messages": self._to_text_messages(messages, tools),
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            raw = await self._request_client().chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalize provider errors
            from agent.adapters.errors import normalize_error

            raise normalize_error(exc) from exc
        content = raw.choices[0].message.content or ""
        parsed = self._parse(content)
        # 成功统计只反映「协议格式损坏」：
        # 合法 Tool JSON 与合法普通文本都是成功，只有看起来想按协议回却解析不出来才算失败。
        # 否则 provider 健康度 / 自动降级会拿一份错误的数据做判断。
        if parsed is not None:
            self._record(True)
        else:
            self._record(not self._looks_like_protocol_attempt(content))

        tool_calls = None
        if parsed is not None:
            from agent.core.narrative import split_narrative_arguments

            tool_calls = []
            for item in parsed["tool_calls"]:
                # 与 native 档同一套剥离逻辑：模型的过程说明信封不进 arguments。
                arguments, narrative = split_narrative_arguments(
                    dict(item.get("arguments") or {})
                )
                tool_calls.append(
                    ToolCall(
                        id=f"tc_{uuid.uuid4().hex[:8]}",
                        name=item["name"],
                        arguments=arguments,
                        narrative=narrative,
                    )
                )
        return Completion(
            message=ChatMessage(role="assistant", content=content, tool_calls=tool_calls),
            raw=raw,
            usage=self._usage_of(raw),
        )

    @staticmethod
    def _usage_of(raw: Any) -> ModelUsage | None:
        """文本兼容档同样要有用量：供应商给了 usage 就不能丢。"""
        raw_usage = getattr(raw, "usage", None)
        if raw_usage is None:
            return None
        if hasattr(raw_usage, "model_dump"):
            payload = raw_usage.model_dump()
        elif isinstance(raw_usage, dict):
            payload = raw_usage
        else:
            return None
        return ModelUsage.from_provider(payload)

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

    @staticmethod
    def _looks_like_protocol_attempt(content: str) -> bool:
        """这段文本像是「想做工具调用但格式坏了」吗？"""
        text = (content or "").strip()
        if not text:
            return False  # 空回答是合法结局（例如模型选择不说话），不是协议损坏
        lowered = text.lower()
        if any(hint in lowered for hint in _PROTOCOL_HINTS):
            return True
        if text.startswith("{") or text.startswith("["):
            # 整段想作为 JSON 返回，却解析不出合法结构
            return True
        match = _JSON_BLOCK.search(text)
        if match and match.group(1).strip().startswith(("{", "[")):
            return True
        return False
