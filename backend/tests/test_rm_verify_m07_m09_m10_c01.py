# -*- coding: utf-8 -*-
"""F 组独立验证：M07（取消清理）、M09（预算耗尽停止新调用）、M10（用量记账）、C01（文本协议）。

模型一律 fake：假 provider / 假 SDK 客户端，本地脚本化响应，不联网、不读取任何真实 Key。

* M07 —— 外层 Task.cancel 时，停在请求里的内层模型调用必须被取消并等待清理，
  取消继续传播，且不留悬挂任务。
* M09 —— 凭据余额被第一次调用耗尽之后，原计划的第二次模型调用不得发生。
* M10 —— 内部解析重试的**每一次真实响应**都要记账（含被丢弃的那次），总账一致。
* C01 —— text 档第二次请求里不得出现 role=tool（无 tool_call_id）的消息，
  工具调用与工具结果必须以普通文本出现（含工具名与参数），最终回答完成。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.adapters.native import NativeAdapter
from agent.adapters.text import TextAdapter
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import credential_usage_sink
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# 通用假件
# ---------------------------------------------------------------------------


class _CaseTool(Tool):
    name = "rmf_echo"
    description = "回显给定文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(ok=True, content=f"回显结果:{kwargs.get('text')}")


def _raw_response(*, content=None, tool_calls=None, usage=None):
    message = type("Msg", (), {"content": content, "tool_calls": tool_calls})()
    choice = type(
        "Choice",
        (),
        {"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"},
    )()
    raw_usage = None
    if usage is not None:
        raw_usage = type("Usage", (), {"model_dump": lambda self, _u=usage: dict(_u)})()
    return type("Raw", (), {"choices": [choice], "usage": raw_usage})()


def _raw_tool_call(call_id: str, name: str, arguments: str):
    function = type("Fn", (), {"name": name, "arguments": arguments})()
    return type("TC", (), {"id": call_id, "function": function})()


class _ScriptedRawClient:
    """openai SDK 形状的最小替身：按脚本返回原始响应。"""

    def __init__(self, responses) -> None:  # noqa: ANN001
        self._responses = list(responses)
        self.calls = 0

    @property
    def chat(self):  # noqa: ANN201
        return self

    @property
    def completions(self):  # noqa: ANN201
        return self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# M07：外层取消必须掐掉在途请求
# ---------------------------------------------------------------------------


class _GatedAdapter:
    """停在请求里、只能被取消打断的假 provider。"""

    mode = "native"
    model = "fake-gate"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        self.cancelled = False
        self.finished = False

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN003
        self.entered.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True
        return Completion(message=ChatMessage(role="assistant", content="done"))


async def test_m07_outer_cancel_cancels_the_inflight_request_and_leaves_nothing():
    adapter = _GatedAdapter()
    loop = AgentLoop(adapter, ToolRegistry(), EventBus())
    task = asyncio.create_task(loop.run("你好"))
    await asyncio.wait_for(adapter.entered.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    for pending in leftovers:  # 清理，避免污染事件循环收尾
        pending.cancel()

    assert adapter.cancelled is True, "外层任务被取消时，内层模型请求也必须被取消（不是干等它返回）"
    assert leftovers == [], f"取消之后不得留下悬挂的请求/等待任务：{leftovers}"


# ---------------------------------------------------------------------------
# M09：余额耗尽 → 不再发起新调用
# ---------------------------------------------------------------------------


class _BudgetSdkClient:
    """假 SDK 客户端（openai 形状）：只数**真实 provider 请求**次数。

    第一次响应请求一个工具（使循环本会发起第二次请求），用量恰好等于全部余额；
    第二次响应是普通文本。`usage` 必须提供 `model_dump()` —— 记账读的就是这个口子，
    少了它这次请求会被如实标成 incomplete（没有用量数据），预算也就不会被耗尽。
    """

    def __init__(self, *, input_tokens: int = 100, output_tokens: int = 20) -> None:
        self.calls = 0
        self._input = input_tokens
        self._output = output_tokens

    @property
    def chat(self):  # noqa: ANN201
        return self

    @property
    def completions(self):  # noqa: ANN201
        return self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        usage = type(
            "Usage",
            (),
            {
                "model_dump": lambda self, _u=None: {
                    "prompt_tokens": self._in,
                    "completion_tokens": self._out,
                    "total_tokens": self._in + self._out,
                }
            },
        )()
        usage._in, usage._out = self._input, self._output
        if self.calls == 1:
            function = type("Fn", (), {"name": "rmf_echo", "arguments": '{"text": "hi"}'})()
            tool = type("TC", (), {"id": "c1", "function": function})()
            message = type("Msg", (), {"content": None, "tool_calls": [tool]})()
            finish = "tool_calls"
        else:
            message = type("Msg", (), {"content": "第二次回答", "tool_calls": None})()
            finish = "stop"
        choice = type("Choice", (), {"message": message, "finish_reason": finish})()
        return type("Raw", (), {"choices": [choice], "usage": usage})()


def test_m09_exhausted_credential_budget_stops_the_next_model_call(db_conn):
    """真实 adapter 的真实请求路径：第一次请求用尽余额后，第二次请求不得发出。

    这里必须用真实 `NativeAdapter`：预算闸门装在**每次实际请求之前**
    （`adapters/native.py` 的请求前核对），整层覆盖 `complete()` 的假 adapter
    正好会绕开它 —— 那是测错边界，而不是产物行为。
    """
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    # 余额恰好等于一次调用的用量（进 100 + 出 20）
    store.create(
        "k1",
        "rmf-placeholder-secret",
        tags=["main-loop"],
        verify_state="verified",
        budget=120,
    )

    client = _BudgetSdkClient()
    adapter = NativeAdapter(client, "fake-budget")
    # fake client 不返回异步流：如实声明不支持流式（本用例只验预算闸门）。
    adapter.supports_stream = False
    adapter.key_id = "k1"
    loop = AgentLoop(
        adapter,
        _registry_with_case_tool(),
        EventBus(),
        usage_sink=credential_usage_sink(store, adapter),
    )
    result = asyncio.run(loop.run("你好"))

    assert client.calls == 1, (
        "余额在第一次调用之后已经耗尽，不得再发出第二次真实 provider 请求；"
        f"实际发出 {client.calls} 次"
    )
    meta = store.get_metadata("k1")
    assert meta["budget_used"] == 120, f"真实用量必须如实入账且不得重复记：{meta}"
    reason = result.final_content or ""
    assert any(word in reason for word in ("耗尽", "上限", "预算")), (
        f"耗尽后必须给用户一句简短的真实原因，而不是静默停止：{reason!r}"
    )


# ---------------------------------------------------------------------------
# M10：内部解析重试的每次响应都要记账
# ---------------------------------------------------------------------------


def test_m10_parse_retry_responses_are_all_accounted(db_conn):
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create("k1", "rmf-placeholder-secret", tags=["main-loop"], verify_state="verified", budget=10**6)

    client = _ScriptedRawClient(
        [
            # 第一次响应：工具参数不是合法 JSON → 适配器内部重试
            _raw_response(
                tool_calls=[_raw_tool_call("c1", "echo", "{不是合法 JSON")],
                usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            ),
            # 重试之后的最终响应
            _raw_response(
                content="完成",
                usage={"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
            ),
        ]
    )
    adapter = NativeAdapter(client, "m")
    # fake client 不返回异步流：如实声明不支持流式（本用例只验记账次数）。
    adapter.supports_stream = False
    adapter.key_id = "k1"
    loop = AgentLoop(
        adapter,
        ToolRegistry(),
        EventBus(),
        usage_sink=credential_usage_sink(store, adapter),
    )
    result = asyncio.run(loop.run("hi"))

    assert result.final_content == "完成"
    assert client.calls == 2, "这一轮必须真的发生了两次真实请求（解析重试）"
    meta = store.get_metadata("k1")
    assert meta["budget_used"] == 165, (
        "解析重试的每一次真实响应都必须记账（110 + 55 = 165），"
        f"既不能漏记被重试丢弃的那次，也不能重复记：{meta}"
    )
    assert meta["usage_input"] == 150 and meta["usage_output"] == 15, meta


# ---------------------------------------------------------------------------
# C01：text 档消息协议
# ---------------------------------------------------------------------------


class _TextRecordingClient:
    """text 档假 SDK 客户端：记录每次请求发出的 messages。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.seen: list[list[dict]] = []
        self.calls = 0

    @property
    def chat(self):  # noqa: ANN201
        return self

    @property
    def completions(self):  # noqa: ANN201
        return self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        self.seen.append(list(kwargs.get("messages") or []))
        content = self._contents.pop(0)
        message = type("Msg", (), {"content": content})()
        choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
        return type("Raw", (), {"choices": [choice], "usage": None})()


def _registry_with_case_tool() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_CaseTool())
    return registry


def test_c01_text_mode_second_request_has_no_tool_role_and_carries_the_result():
    client = _TextRecordingClient(
        [
            json.dumps(
                {"tool_calls": [{"name": "rmf_echo", "arguments": {"text": "你好"}}]},
                ensure_ascii=False,
            ),
            "最终回答",
        ]
    )
    adapter = TextAdapter(client, "fake-text")
    loop = AgentLoop(adapter, _registry_with_case_tool(), EventBus())
    result = asyncio.run(loop.run("请回显"))

    assert result.final_content == "最终回答"
    assert client.calls == 2, "文本档必须先调用一次工具，再发第二次请求"

    second = client.seen[1]
    roles = [str(m.get("role")) for m in second]
    assert "tool" not in roles, f"带 role=tool 但没有 tool_call_id 的消息不得下发：{second}"
    assert all(not m.get("tool_call_id") for m in second), second

    joined = "\n".join(str(m.get("content") or "") for m in second)
    assert "rmf_echo" in joined, f"assistant 的工具调用必须转成普通文本（含工具名）：{joined}"
    assert "你好" in joined, f"工具参数必须保留在文本里：{joined}"
    assert "回显结果:你好" in joined, f"工具结果必须以普通文本出现：{joined}"


def test_c01_text_mode_multi_tool_and_failed_result_keep_a_stable_order():
    client = _TextRecordingClient(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {"name": "rmf_echo", "arguments": {"text": "A"}},
                        {"name": "rmf_ghost", "arguments": {}},
                    ]
                },
                ensure_ascii=False,
            ),
            "收尾",
        ]
    )
    adapter = TextAdapter(client, "fake-text")
    loop = AgentLoop(adapter, _registry_with_case_tool(), EventBus())
    result = asyncio.run(loop.run("一次调两个"))

    # 失败的工具会被后端事实核对补一句说明，模型正文仍在最前面
    assert str(result.final_content).startswith("收尾")
    second = client.seen[1]
    roles = [str(m.get("role")) for m in second]
    assert "tool" not in roles, second
    joined = "\n".join(str(m.get("content") or "") for m in second)
    assert "rmf_echo" in joined and "rmf_ghost" in joined, joined
    assert joined.index("rmf_echo") < joined.index("rmf_ghost"), f"工具顺序必须稳定：{joined}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
