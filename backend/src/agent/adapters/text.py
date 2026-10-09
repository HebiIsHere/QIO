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
    SYSTEM_PROMPT_TEXT_MODE,
    SYSTEM_PROMPT_TOOLS_HEADER,
    TEXT_TOOL_ENTRY,
)

logger = logging.getLogger(__name__)

SUCCESS_RATE_THRESHOLD = 0.6

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 出现这些迹象说明模型**想**按工具协议回复，但格式坏了 —— 这才是协议失败。
_PROTOCOL_HINTS = ("tool_calls", "tool_name", '"arguments"')

# text 档没有 role=tool：工具结果以普通 user 文本回给模型，格式在这里定义一次。
TOOL_RESULT_TEMPLATE = "工具 {name} 的结果（call_id={call_id}）：\n{content}"
TOOL_CALL_TEMPLATE = "调用工具 {name}（call_id={call_id}）：参数 {arguments}"
UNKNOWN_TOOL_NAME = "未知工具"


class TextAdapter(BaseAdapter):
    mode = "text"
    # 没有原生工具协议：工具定义只能拼进 system prompt
    tools_in_prompt = True
    # 每次实际请求自己记账 —— 与 native / anthropic 同一处（credentials/usage.py）。
    accounts_requests = True

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

    def build_system_prompt(self, tools: list[ToolSpec]) -> str:
        tools_block = self.build_text_tools(tools)
        if tools_block:
            tools_block = f"{SYSTEM_PROMPT_TOOLS_HEADER}\n{tools_block}"
        # 观测格式必须在 prompt 里说清楚：text 档没有 role=tool，工具结果是普通文本。
        observation_note = (
            "Tool results come back to you as ordinary user messages formatted as "
            '"工具 <name> 的结果（call_id=<id>）：<result>"; '
            'your tool calls are echoed as "调用工具 <name>（call_id=<id>）：参数 <json>".'
        )
        return f"{SYSTEM_PROMPT_TEXT_MODE}\n\n{observation_note}\n\n{tools_block}"

    def system_prompt_text(self, tools: list[ToolSpec]) -> str:
        """text 档真正会发出去的 system prompt（含全部工具 schema）。"""
        return self.build_system_prompt(tools)

    def protocol_overhead_tokens(self, tools: list[ToolSpec]) -> int:
        # 除了统一的角色标记，text 档还要在提示里保留 JSON 输出格式说明的余量
        return super().protocol_overhead_tokens(tools) + 32

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
            "messages": self._to_text_messages(messages, tools),
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        # 每次实际请求之前核对累计用量（耗尽抛 BudgetExhausted，不再发新请求）。
        from agent.credentials import usage as accounting

        accounting.ensure_adapter_request_allowed(self)
        try:
            raw = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - normalize provider errors
            from agent.adapters.errors import normalize_error

            # 失败且没有用量：明确标 incomplete，不造数（结果照旧抛出）。
            accounting.account_adapter_failure(self, exc)
            raise normalize_error(exc) from exc
        content = raw.choices[0].message.content or ""
        # 供应商给了 usage 就必须记（进 / 出分开）；没有就明确标这次记账不完整。
        accounting.account_adapter_request(self, self._usage_of(raw), failed=False)
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
        """把完整对话压成**合法的普通文本消息**（text 档没有工具协议）。

        C9/C01 的硬规则：

        * assistant 的工具调用 ⇒ 普通 assistant 文本，含工具名、参数与 call_id 关联；
        * 工具结果 ⇒ 普通 **user** 文本「工具 <name> 的结果（call_id=…）：…」，
          工具名从前面 assistant 消息的 tool_calls 关联出来；
        * 任何 ``role=tool`` 的消息都不原样发出（那需要一个本档位不存在的
          ``tool_call_id`` 字段），但结果内容**绝不丢弃**；
        * 一条输入消息对应一条输出消息，顺序严格保持（多工具 / 多轮 / 失败结果都稳定）。
        """
        call_names = self._tool_call_names(messages)
        out: list[dict[str, Any]] = [
            {"role": "system", "content": self.build_system_prompt(tools)}
        ]
        for msg in messages:
            if msg.role == "tool":
                # 工具结果：普通文本观测，附工具名与调用关联（不丢内容、不换成 role=tool）。
                out.append(
                    {"role": "user", "content": self._tool_result_text(msg, call_names)}
                )
                continue
            if msg.role == "assistant" and msg.tool_calls:
                out.append({"role": "assistant", "content": self._assistant_text(msg)})
                continue
            # system / user / 普通 assistant：原样保留（空内容也保留，顺序不乱）
            out.append({"role": msg.role, "content": msg.content or ""})
        return out

    @staticmethod
    def _tool_call_names(messages: list[ChatMessage]) -> dict[str, str]:
        """call_id → 工具名：让工具结果能指出「这是哪个工具」的结果。"""
        names: dict[str, str] = {}
        for msg in messages:
            for call in msg.tool_calls or []:
                if call.id:
                    names[str(call.id)] = str(call.name or "")
        return names

    @staticmethod
    def _assistant_text(msg: ChatMessage) -> str:
        """assistant 的工具请求 → 普通文本（含工具名 / 参数 / call_id 关联）。"""
        blocks: list[str] = []
        if msg.content and msg.content.strip():
            blocks.append(msg.content)
        for call in msg.tool_calls or []:
            blocks.append(
                TOOL_CALL_TEMPLATE.format(
                    name=call.name or UNKNOWN_TOOL_NAME,
                    call_id=call.id or "",
                    arguments=json.dumps(call.arguments or {}, ensure_ascii=False),
                )
            )
        return "\n".join(blocks)

    @staticmethod
    def _tool_result_text(msg: ChatMessage, call_names: dict[str, str]) -> str:
        """工具结果 → 「工具 <name> 的结果（call_id=…）：<内容>」普通文本。

        内容为空时也保留一个明确的占位（发生过一次调用、输出为空），
        绝不靠删掉这条消息让请求「看起来合法」。
        """
        call_id = msg.tool_call_id or ""
        name = call_names.get(call_id) or UNKNOWN_TOOL_NAME
        content = msg.content if msg.content not in (None, "") else "（没有输出）"
        return TOOL_RESULT_TEMPLATE.format(name=name, call_id=call_id, content=content)

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
