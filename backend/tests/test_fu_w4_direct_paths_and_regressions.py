"""W4 / A06：不经主循环 sink 的直接调用路径（后台提炼 / 引导追问 / 内部重试）归属。

契约 §5 要求显式绑定覆盖「主循环 sink 之外」的直接调用，并且不回归既有记账语义：

* 预算耗尽不发新请求；
* 每次**实际**调用记一次（含内部解析重试的每一次响应）；
* 失败但已知用量照记；
* 没有用量只标 ``incomplete``、不造数；
* 单上下文正常路径不变。

模型调用一律**严格本地替身**（脚本化假客户端）：不联网、不需要真实 Key。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.adapters.native import NativeAdapter
from agent.credentials.policy import BudgetExhausted
from agent.credentials.usage import accounting_snapshot, bind_request_accounting
from agent.memory.summary import summarize_fragment_outcome
from agent.services.followups import suggest_follow_ups

from test_fu_w4_explicit_ledger_ownership import (  # noqa: E402 - 复用同一套本地替身
    KEY,
    FakeAppContext,
    FakeChoice,
    FakeCompletion,
    FakeMessage,
    FakeUsage,
    ScriptedClient,
    _make_ledger,
    _messages,
    _tool_call,
)


def tool_call_response(name: str, arguments: str, *, inp: int, out: int) -> FakeCompletion:
    return FakeCompletion(
        [FakeChoice(FakeMessage(None, [_tool_call(f"call_{name}", name, arguments)]))],
        usage=FakeUsage(inp, out),
    )


def text_response(content: str, *, inp: int, out: int) -> FakeCompletion:
    return FakeCompletion([FakeChoice(FakeMessage(content, None))], usage=FakeUsage(inp, out))


class FailingClient:
    """传输层就失败：没有响应，也就没有用量。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def chat(self) -> "FailingClient":
        return self

    @property
    def completions(self) -> "FailingClient":
        return self

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls += 1
        raise RuntimeError("transport exploded")


SUMMARY_JSON = '{"title": "标题", "summary": "摘要正文", "entities": [], "keywords": []}'


async def test_internal_retry_records_every_response_on_the_owner(tmp_path):
    """一次内部解析重试：两次响应都记到**实际归属**账本，另一方一分不动。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=1000)
    ledger_b = _make_ledger(tmp_path, "b", budget=1000)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)  # 默认库是 B：兜底会记错，显式绑定才对

    client = ScriptedClient(
        [
            tool_call_response("echo", "{坏掉的 JSON", inp=10, out=5),  # 解析失败 → 重试
            text_response("最终回答", inp=20, out=7),
        ]
    )
    adapter = ctx_a.adapter_for(KEY, client)
    assert ctx_b is not None

    completion = await adapter.complete(_messages(), [])
    assert completion.message.content == "最终回答"
    assert len(client.calls) == 2, "两次都是真实请求"
    assert ledger_a.used() == (30, 12, 42)
    assert ledger_b.used() == (0, 0, 0)
    assert accounting_snapshot(adapter) == {
        "key_id": KEY,
        "requests": 2,
        "recorded": 2,
        "incomplete": 0,
    }


async def test_background_refinement_and_onboarding_followups_use_the_owner(tmp_path):
    """后台提炼（摘要）与引导追问都是直接调用 adapter：归属必须正确。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=1000)
    ledger_b = _make_ledger(tmp_path, "b", budget=1000)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)

    client = ScriptedClient(
        [
            text_response(SUMMARY_JSON, inp=20, out=7),                          # 后台提炼
            text_response('{"questions": ["你希望我怎么称呼你？"]}', inp=4, out=2),  # 引导追问
        ]
    )
    adapter = ctx_a.adapter_for(KEY, client)
    assert ctx_b is not None

    summary = await summarize_fragment_outcome(
        adapter, [{"role": "user", "content": "聊了点饮食"}]
    )
    assert summary.value is not None, summary.error
    questions, error = await suggest_follow_ups(adapter, "我是做后端的，想长期记饮食")
    assert error is None and questions

    assert ledger_a.used() == (24, 9, 33)
    assert ledger_b.used() == (0, 0, 0)
    assert accounting_snapshot(adapter)["recorded"] == 2


async def test_known_usage_is_recorded_even_when_the_request_fails(tmp_path):
    """失败但已知用量照记（含最终失败的那一次）。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=1000)
    ledger_b = _make_ledger(tmp_path, "b", budget=1000)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)

    client = ScriptedClient([tool_call_response("echo", "{坏掉的 JSON", inp=10, out=5)])
    adapter = NativeAdapter(client=client, model="fake-model", parse_retries=0)
    adapter.key_id = KEY
    bind_request_accounting(adapter, ledger_a.store)
    assert ctx_b is not None

    with pytest.raises(Exception):
        await adapter.complete(_messages(), [])

    assert ledger_a.used() == (10, 5, 15), "失败但用量已知，照记"
    assert ledger_b.used() == (0, 0, 0)
    assert accounting_snapshot(adapter) == {
        "key_id": KEY,
        "requests": 1,
        "recorded": 1,
        "incomplete": 0,
    }


async def test_missing_usage_is_marked_incomplete_without_inventing_numbers(tmp_path):
    """没有用量的失败：只标 incomplete，不造数（两份账本都不动）。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=1000)
    ledger_b = _make_ledger(tmp_path, "b", budget=1000)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)

    client = FailingClient()
    adapter = ctx_a.adapter_for(KEY, client)
    assert ctx_b is not None

    with pytest.raises(Exception):
        await adapter.complete(_messages(), [])

    assert client.calls == 1
    assert ledger_a.used() == (0, 0, 0)
    assert ledger_b.used() == (0, 0, 0)
    assert accounting_snapshot(adapter) == {
        "key_id": KEY,
        "requests": 1,
        "recorded": 0,
        "incomplete": 1,
    }


async def test_single_context_normal_path_is_unchanged(tmp_path):
    """单上下文正常路径：记一次、余额按实际用量扣。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    client = ScriptedClient([text_response("回答", inp=2, out=3)])
    adapter = ctx_a.adapter_for(KEY, client)

    completion = await adapter.complete(_messages(), [])
    assert completion.message.content == "回答"
    assert ledger_a.used() == (2, 3, 5)
    assert ledger_a.left() == 95
    assert accounting_snapshot(adapter) == {
        "key_id": KEY,
        "requests": 1,
        "recorded": 1,
        "incomplete": 0,
    }


async def test_exhausted_budget_stops_after_the_last_affordable_call(tmp_path):
    """预算耗尽后不再发新请求（既有语义不回归）。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=5)
    ctx_a = FakeAppContext(ledger_a)
    client = ScriptedClient(
        [
            text_response("第一次", inp=2, out=3),
            text_response("第二次不该发", inp=2, out=3),
        ]
    )
    adapter = ctx_a.adapter_for(KEY, client)

    await adapter.complete(_messages(), [])
    with pytest.raises(BudgetExhausted):
        await adapter.complete(_messages(), [])

    assert len(client.calls) == 1
    assert ledger_a.used() == (2, 3, 5)
    assert accounting_snapshot(adapter)["requests"] == 1
