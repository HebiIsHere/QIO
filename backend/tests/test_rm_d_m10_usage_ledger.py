"""M10：用量总账 —— 每次实际请求记一次（含重试的每次响应），不漏记不重记。

验收（契约 C8）：

* 主循环、摘要、知识、压缩（滚动摘要）、实体提炼用**固定用量响应**核对总账；
* 一次内部解析重试（含最终失败）里每一次响应都记，失败但已知用量照样记；
* 无用量失败明确标 ``incomplete``，不造数（账本一分不动）；
* 归因跟随实际调用配置（哪把凭据发出请求就记到哪把，不串账）；
* ``_account_usage`` 迁移到统一入口后不得双记（真实 adapter 自记账 → 上层跳过）。

模型调用一律假 provider（脚本化客户端，不联网、不需要真实 Key）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest

from agent.adapters.base import ChatMessage, ToolCall
from agent.adapters.native import NativeAdapter
from agent.api.bus import EventBus
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import (
    accounting_snapshot,
    adapter_self_accounts,
    bind_request_accounting,
    credential_usage_sink,
    record_request_usage,
    request_accounting,
)
from agent.core.loop import AgentLoop
from agent.memory.summary import (
    extract_knowledge_candidates_outcome,
    summarize_fragment_outcome,
    summarize_rolling_outcome,
)
from agent.entities.extract import extract_entity_cards_outcome
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

FAKE_SECRET = "sk-fake-placeholder"


# -- 假 provider ----------------------------------------------------------


@dataclass
class FakeMessage:
    content: str | None
    tool_calls: list[Any] | None = None


@dataclass
class FakeChoice:
    message: FakeMessage
    finish_reason: str = "stop"


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    usage: Any = None


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self._payload = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def model_dump(self) -> dict[str, int]:
        return dict(self._payload)


def _tc(tool_id: str, name: str, arguments: str) -> Any:
    return type(
        "TC",
        (),
        {"id": tool_id, "function": type("F", (), {"name": name, "arguments": arguments})()},
    )()


def tool_response(name: str, arguments: str, *, inp: int, out: int) -> FakeCompletion:
    return FakeCompletion(
        [FakeChoice(FakeMessage(None, [_tc(f"call_{name}", name, arguments)]))],
        usage=FakeUsage(inp, out),
    )


def text_response(content: str, *, inp: int, out: int) -> FakeCompletion:
    return FakeCompletion([FakeChoice(FakeMessage(content, None))], usage=FakeUsage(inp, out))


def no_usage_response(content: str) -> FakeCompletion:
    return FakeCompletion([FakeChoice(FakeMessage(content, None))], usage=None)


class ScriptedClient:
    def __init__(self, script: list[FakeCompletion]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    @property
    def chat(self) -> "ScriptedClient":
        return self

    @property
    def completions(self) -> "ScriptedClient":
        return self

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        if self.script:
            return self.script.pop(0)
        return text_response("（脚本已用尽）", inp=1, out=1)


class FailingClient:
    """请求在传输层就失败：没有响应，也就没有用量。"""

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


class ParseFailClient(ScriptedClient):
    """前 N 次返回「坏了参数」的工具调用，之后正常返回。"""

    def __init__(self, failures: int, *, inp: int = 10, out: int = 5) -> None:
        super().__init__([])
        self.failures = failures
        self.inp, self.out = inp, out

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        if self.failures > 0:
            self.failures -= 1
            return FakeCompletion(
                [FakeChoice(FakeMessage(None, [_tc("call_bad", "echo", "{bad json")]))],
                usage=FakeUsage(self.inp, self.out),
            )
        return tool_response("echo", '{"text": "ok"}', inp=self.inp, out=self.out)


# -- 公共装置 -------------------------------------------------------------


def _store(db_conn: sqlite3.Connection, budget: int | None, key_id: str = "k1") -> CredentialStore:
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(key_id, FAKE_SECRET, tags=["main-loop"], verify_state="verified", budget=budget)
    return store


def _bound_adapter(client: Any, store: CredentialStore, key_id: str = "k1") -> NativeAdapter:
    adapter = NativeAdapter(client=client, model="fake-model")
    adapter.key_id = key_id
    bind_request_accounting(adapter, store, key_id)
    return adapter


def _meta(store: CredentialStore, key_id: str = "k1") -> tuple[int, int, int]:
    meta = store.get_metadata(key_id) or {}
    return (
        int(meta["usage_input"]),
        int(meta["usage_output"]),
        int(meta["budget_used"]),
    )


# -- 总账：主循环 + 摘要 + 知识 + 压缩 + 实体 ------------------------------


SUMMARY_JSON = (
    '{"title": "标题", "summary": "摘要正文", "entities": ["小明"], "keywords": ["饮食"]}'
)
KNOWLEDGE_JSON = (
    '{"candidates": [{"content": "用户偏好清淡饮食", "category": "user_profile",'
    ' "attach": "user", "entity": null}]}'
)
ENTITY_JSON = (
    '{"entities": [{"name": "小明", "kind": "person", "summary": "朋友",'
    ' "aliases": [], "attributes": [], "relations": []}]}'
)
ROLLING_JSON = (
    '{"title": "滚动标题", "summary": "滚动摘要", "entities": [], "keywords": []}'
)


async def test_ledger_matches_main_loop_summary_knowledge_compression_entities(db_conn):
    """固定用量响应下，五个来源的总账必须与逐次相加完全一致。"""
    store = _store(db_conn, budget=1000)
    client = ScriptedClient(
        [
            tool_response("echo", '{"text": "hi"}', inp=60, out=40),  # 主循环第 1 次
            text_response("最终回答", inp=10, out=5),                 # 主循环第 2 次
            text_response(SUMMARY_JSON, inp=20, out=7),               # 摘要
            text_response(KNOWLEDGE_JSON, inp=30, out=9),             # 知识
            text_response(ROLLING_JSON, inp=40, out=11),              # 压缩（滚动摘要）
            text_response(ENTITY_JSON, inp=50, out=13),               # 实体
        ]
    )
    adapter = _bound_adapter(client, store)

    registry = ToolRegistry()
    registry.register(EchoTool())
    loop = AgentLoop(
        adapter,
        registry,
        EventBus(),
        usage_sink=credential_usage_sink(store, adapter),
        max_iterations=4,
    )
    result = await loop.run("你好")
    assert result.final_content == "最终回答"

    messages = [{"role": "user", "content": "聊了点饮食"}, {"role": "assistant", "content": "好"}]
    summary = await summarize_fragment_outcome(adapter, messages)
    assert summary.value is not None, summary.error
    knowledge = await extract_knowledge_candidates_outcome(adapter, summary.value)
    assert knowledge.value is not None, knowledge.error
    rolling = await summarize_rolling_outcome(adapter, "旧摘要", messages)
    assert rolling.value is not None, rolling.error
    entities = await extract_entity_cards_outcome(adapter, messages)
    assert entities.value is not None, entities.error

    # 主循环 2 次 + 摘要 1 + 知识 1 + 压缩 1 + 实体 1 = 6 次，每次进/出都记
    assert len(client.calls) == 6
    assert _meta(store) == (60 + 10 + 20 + 30 + 40 + 50, 40 + 5 + 7 + 9 + 11 + 13, 295)
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 6,
        "recorded": 6,
        "incomplete": 0,
    }
    assert store.budget_left("k1") == 1000 - 295


async def test_usage_event_carries_accounting_counts(db_conn):
    """展示统计要能看到「请求几次 / 入账几次 / 不完整几次」。"""
    import asyncio
    import json

    store = _store(db_conn, budget=1000)
    client = ScriptedClient([text_response("回答", inp=100, out=20)])
    adapter = _bound_adapter(client, store)
    bus = EventBus()
    seen: list[dict] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    loop = AgentLoop(adapter, ToolRegistry(), bus, usage_sink=credential_usage_sink(store, adapter))
    await loop.run("hi")
    await asyncio.sleep(0.05)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    usage_events = [e for e in seen if e["type"] == "USAGE"]
    assert usage_events
    data = usage_events[-1]["data"]
    assert (data["input_tokens"], data["output_tokens"], data["total_tokens"]) == (100, 20, 120)
    assert data["accounting"] == {
        "key_id": "k1",
        "requests": 1,
        "recorded": 1,
        "incomplete": 0,
    }


# -- 内部解析重试 ---------------------------------------------------------


async def test_each_parse_retry_response_is_recorded_once(db_conn):
    """一次成功的重试链：每一次响应各记一次（1 + 1 = 2 次）。"""
    store = _store(db_conn, budget=1000)
    client = ParseFailClient(failures=1, inp=10, out=5)
    adapter = _bound_adapter(client, store)

    completion = await adapter.complete([ChatMessage(role="user", content="hi")], [])
    assert completion.tool_calls is not None
    assert len(client.calls) == 2
    assert _meta(store) == (20, 10, 30)
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 2,
        "recorded": 2,
        "incomplete": 0,
    }


async def test_retry_chain_that_fails_still_records_known_usage(db_conn):
    """重试最终失败：此前每次响应带用量，也必须如实记录（3 次 = 45）。"""
    from agent.adapters.base import ToolCallParseError

    store = _store(db_conn, budget=1000)
    client = ParseFailClient(failures=99, inp=10, out=5)
    adapter = _bound_adapter(client, store)

    with pytest.raises(ToolCallParseError):
        await adapter.complete([ChatMessage(role="user", content="hi")], [])

    assert len(client.calls) == 3, "1 次首发 + 2 次重试，每次都真的发出去了"
    assert _meta(store) == (30, 15, 45)
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 3,
        "recorded": 3,
        "incomplete": 0,
    }


async def test_failed_request_without_usage_is_incomplete_and_fabricates_nothing(db_conn):
    """无用量失败：标 incomplete，账本一分不动（不造数）。"""
    store = _store(db_conn, budget=1000)
    client = FailingClient()
    adapter = _bound_adapter(client, store)

    with pytest.raises(Exception):
        await adapter.complete([ChatMessage(role="user", content="hi")], [])

    assert client.calls == 1
    assert _meta(store) == (0, 0, 0), "没有用量就绝不能写一个猜出来的数字"
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 1,
        "recorded": 0,
        "incomplete": 1,
    }


async def test_success_without_usage_is_marked_incomplete(db_conn):
    """成功但供应商没报用量：同样标 incomplete（不可完整入账），不造数。"""
    store = _store(db_conn, budget=1000)
    client = ScriptedClient([no_usage_response("回答")])
    adapter = _bound_adapter(client, store)

    await adapter.complete([ChatMessage(role="user", content="hi")], [])

    assert _meta(store) == (0, 0, 0)
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 1,
        "recorded": 0,
        "incomplete": 1,
    }


# -- 统一入口本身 ---------------------------------------------------------


def test_record_request_usage_marks_incomplete_without_writing_numbers(db_conn):
    store = _store(db_conn, budget=1000)
    receipt = record_request_usage(store, "k1", 0, 0, incomplete=True, reason="供应商没给用量")

    assert receipt.recorded is False
    assert receipt.incomplete is True
    assert _meta(store) == (0, 0, 0)

    # 已知用量 + 不完整标记：用量照记，标记照留（「失败但有已知用量也要记」）
    receipt2 = record_request_usage(
        store, "k1", 12, 8, incomplete=True, reason="这次请求本身失败了"
    )
    assert (receipt2.recorded, receipt2.incomplete) == (True, True)
    assert _meta(store) == (12, 8, 20)


def test_record_request_usage_uses_optional_incomplete_hook(db_conn):
    """账本若有 note_incomplete_usage 扩展点就通知它；没有也不报错（DDL 不在本轮）。"""

    class FakeStore:
        def __init__(self) -> None:
            self.written: list[tuple] = []
            self.notes: list[tuple] = []

        def record_usage(self, key_id, *, input_tokens=0, output_tokens=0) -> None:
            self.written.append((key_id, input_tokens, output_tokens))

        def note_incomplete_usage(self, key_id, reason) -> None:
            self.notes.append((key_id, reason))

        def budget_left(self, key_id):
            return None

    store = FakeStore()
    record_request_usage(store, "k1", 0, 0, incomplete=True, reason="没有用量")
    assert store.written == []
    assert store.notes == [("k1", "没有用量")]


def test_record_request_usage_never_raises_on_a_broken_store():
    class BrokenStore:
        def record_usage(self, key_id, **kwargs) -> None:
            raise sqlite3.ProgrammingError("database is closed")

    receipt = record_request_usage(BrokenStore(), "k1", 5, 5)
    assert receipt.recorded is False and receipt.incomplete is False
    assert receipt.reason == "记账写入失败"


# -- 归因跟随实际调用配置 -------------------------------------------------


async def test_attribution_follows_the_credential_that_actually_called(db_conn):
    """两把钥匙各发一次：各自记账，不串账。"""
    store = _store(db_conn, budget=1000, key_id="k1")
    store.create("k2", FAKE_SECRET, tags=["main-loop"], verify_state="verified", budget=1000)

    client_a = ScriptedClient([text_response("A", inp=11, out=1)])
    client_b = ScriptedClient([text_response("B", inp=22, out=2)])
    adapter_a = _bound_adapter(client_a, store, "k1")
    adapter_b = _bound_adapter(client_b, store, "k2")

    await adapter_a.complete([ChatMessage(role="user", content="a")], [])
    await adapter_b.complete([ChatMessage(role="user", content="b")], [])

    assert _meta(store, "k1") == (11, 1, 12)
    assert _meta(store, "k2") == (22, 2, 24)
    assert request_accounting(adapter_a).key_id == "k1"
    assert request_accounting(adapter_b).key_id == "k2"


# -- 不重记（统一入口 vs 上层 sink） ---------------------------------------


async def test_loop_does_not_double_count_when_the_adapter_already_accounts(db_conn):
    """生产接线（credential_usage_sink + 真实 adapter）下只记一次。"""
    store = _store(db_conn, budget=1000)
    client = ScriptedClient([text_response("回答", inp=100, out=20)])
    adapter = _bound_adapter(client, store)
    sink = credential_usage_sink(store, adapter)

    assert adapter_self_accounts(adapter) is True
    assert sink.covers_requests is True

    loop = AgentLoop(adapter, ToolRegistry(), EventBus(), usage_sink=sink)
    await loop.run("hi")

    assert _meta(store) == (100, 20, 120), "一次模型调用只能记一次（不得双记）"
    assert accounting_snapshot(adapter)["requests"] == 1


async def test_custom_sink_still_receives_usage_for_duck_typed_adapters(db_conn):
    """鸭子类型替身 adapter（不自记账）：自定义 sink 照旧按时收到用量。"""
    seen: list[tuple[int, int]] = []

    class FakeAdapter:
        mode = "native"
        model = "fake-usage"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import Completion, ModelUsage

            return Completion(
                message=ChatMessage(role="assistant", content="收到"),
                usage=ModelUsage(input_tokens=120, output_tokens=30),
            )

    loop = AgentLoop(
        FakeAdapter(),
        ToolRegistry(),
        EventBus(),
        usage_sink=lambda inp, out: seen.append((inp, out)),
    )
    await loop.run("你好")

    assert seen == [(120, 30)]
