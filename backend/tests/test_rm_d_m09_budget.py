"""M09：调用预算 —— 耗尽之后不再发新请求，主循环 / 子任务 / 后台维护 / 内部重试同一规则。

验收（契约 C8）：

* 余额允许第一次调用、第一次结果把余额耗尽 → 原计划第二次调用**不发生**；
* 检查调用计数与停止原因（不是静默失败、不是换配置）；
* 覆盖主循环、子任务、后台维护、内部重试的接线；
* ``remaining_budget`` 必须区分「没有上限（None）」与「已耗尽（<= 0）」。

模型调用全部是假 provider（脚本化客户端，不联网、不需要真实 Key）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from agent.adapters.base import ChatMessage
from agent.adapters.native import NativeAdapter
from agent.api.bus import EventBus
from agent.credentials.policy import (
    BudgetExhausted,
    ensure_budget_available,
    remaining_budget,
)
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import (
    accounting_snapshot,
    bind_request_accounting,
    credential_usage_sink,
)
from agent.core.loop import AgentLoop
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

# 一段假的 Key 原文：只在本文件里当占位符用，绝不写进任何日志/断言输出。
FAKE_SECRET = "sk-fake-placeholder"


# -- 假 provider（OpenAI 兼容形状） ---------------------------------------


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


def tool_response(name: str, arguments: str, *, prompt: int, completion: int) -> FakeCompletion:
    return FakeCompletion(
        [FakeChoice(FakeMessage(None, [_tc(f"call_{name}", name, arguments)]))],
        usage=FakeUsage(prompt, completion),
    )


def bad_tool_response(*, prompt: int, completion: int) -> FakeCompletion:
    return FakeCompletion(
        [FakeChoice(FakeMessage(None, [_tc("call_bad", "echo", "{bad json")]))],
        usage=FakeUsage(prompt, completion),
    )


def text_response(content: str, *, prompt: int, completion: int) -> FakeCompletion:
    return FakeCompletion(
        [FakeChoice(FakeMessage(content, None))], usage=FakeUsage(prompt, completion)
    )


class ScriptedClient:
    """脚本化客户端：按顺序返回响应，用完返回一句普通文本。"""

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
        return text_response("（脚本已用尽）", prompt=1, completion=1)


# -- 公共装置 -------------------------------------------------------------


def _store(db_conn: sqlite3.Connection, budget: int | None, key_id: str = "k1") -> CredentialStore:
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(
        key_id,
        FAKE_SECRET,
        tags=["main-loop"],
        verify_state="verified",
        budget=budget,
    )
    return store


def _adapter(client: ScriptedClient, store: CredentialStore) -> NativeAdapter:
    """按生产接线建 adapter：key_id 由构造它的地方填（services/app.py 同一规则）。"""
    adapter = NativeAdapter(client=client, model="fake-model")
    adapter.key_id = "k1"
    # 这个 fake client 不会返回异步流：如实声明「不支持流式」，本用例只关注预算闸门。
    # 流式探测与降级路径由 tests/test_streaming_deltas.py 覆盖。
    adapter.supports_stream = False
    bind_request_accounting(adapter, store)
    return adapter


def _main_loop(adapter: NativeAdapter, store: CredentialStore) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentLoop(
        adapter,
        registry,
        EventBus(),
        # 生产接线：turn_orchestrator / subagent / maintenance 都是这一行
        usage_sink=credential_usage_sink(store, adapter),
        max_iterations=8,
    )


# -- 预算语义 -------------------------------------------------------------


def test_remaining_budget_distinguishes_no_limit_from_exhausted(db_conn):
    store = _store(db_conn, budget=None)
    assert remaining_budget(store, "k1") is None, "没有上限必须返回 None，不是 0"
    assert ensure_budget_available(store, "k1") is None

    store.create("k2", FAKE_SECRET, tags=["main-loop"], verify_state="verified", budget=100)
    store.record_usage("k2", input_tokens=60, output_tokens=40)
    assert remaining_budget(store, "k2") == 0
    with pytest.raises(BudgetExhausted):
        ensure_budget_available(store, "k2")

    # 账本里没这条凭据：不把「查不到」当成耗尽（宁可放行也不误拦）。
    assert remaining_budget(store, "no-such-key") is None


# -- 主循环 ---------------------------------------------------------------


async def test_main_loop_does_not_send_the_second_request_after_budget_is_spent(db_conn):
    """余额允许第一次调用，第一结果耗尽余额 → 原计划第二次调用不发生。"""
    store = _store(db_conn, budget=100)
    client = ScriptedClient(
        [
            tool_response("echo", '{"text": "hi"}', prompt=60, completion=40),
            text_response("这次请求不该被发出去", prompt=60, completion=40),
        ]
    )
    adapter = _adapter(client, store)
    loop = _main_loop(adapter, store)

    result = await loop.run("你好")

    assert len(client.calls) == 1, "耗尽之后不得再发第二次模型请求"
    assert result.phase.value == "stopped"
    assert result.cancelled is False
    assert any("用量" in w for w in result.warnings), result.warnings
    assert result.final_content and "上限已耗尽" in result.final_content, result.final_content

    meta = store.get_metadata("k1")
    assert (meta["usage_input"], meta["usage_output"], meta["budget_used"]) == (60, 40, 100)
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 1,
        "recorded": 1,
        "incomplete": 0,
    }


async def test_no_budget_never_blocks_new_requests(db_conn):
    """没有上限（None）不是耗尽：该发的请求照发。"""
    store = _store(db_conn, budget=None)
    client = ScriptedClient(
        [
            tool_response("echo", '{"text": "hi"}', prompt=60, completion=40),
            text_response("第二次也发了", prompt=60, completion=40),
        ]
    )
    adapter = _adapter(client, store)
    loop = _main_loop(adapter, store)

    result = await loop.run("你好")

    assert len(client.calls) == 2
    assert result.final_content == "第二次也发了"
    assert remaining_budget(store, "k1") is None


async def test_already_exhausted_budget_sends_nothing_at_all(db_conn):
    """进循环之前就已耗尽 → 一次请求都不发，并给出简短真实原因。"""
    store = _store(db_conn, budget=0)
    client = ScriptedClient([text_response("不该被发出去", prompt=1, completion=1)])
    adapter = _adapter(client, store)
    loop = _main_loop(adapter, store)

    result = await loop.run("你好")

    assert client.calls == []
    assert result.final_content and "上限已耗尽" in result.final_content
    assert store.get_metadata("k1")["budget_used"] == 0


# -- 内部重试 -------------------------------------------------------------


async def test_internal_parse_retry_does_not_send_a_second_request_after_exhaustion(db_conn):
    """内部解析重试也要走同一道预算闸门：耗尽后不再发重试请求。"""
    store = _store(db_conn, budget=100)
    client = ScriptedClient(
        [
            bad_tool_response(prompt=60, completion=40),
            text_response("重试请求不该被发出去", prompt=60, completion=40),
        ]
    )
    adapter = _adapter(client, store)

    with pytest.raises(BudgetExhausted):
        await adapter.complete([ChatMessage(role="user", content="hi")], [])

    assert len(client.calls) == 1, "耗尽之后不得再发内部重试请求"
    assert store.get_metadata("k1")["budget_used"] == 100


# -- 子任务接线 -----------------------------------------------------------


async def test_subagent_loop_uses_the_same_budget_gate(db_conn):
    """子任务：SubagentTool 的循环与主循环共用同一把凭据上限。"""
    from agent.tools.spec import SubagentBudget, ToolDefinition
    from agent.tools.subagent_tool import SubagentTool
    from agent.tools.task_manager import TaskManager

    store = _store(db_conn, budget=100)
    client = ScriptedClient(
        [
            tool_response("ghost", "{}", prompt=60, completion=40),
            text_response("不该被发出去", prompt=60, completion=40),
        ]
    )
    adapter = _adapter(client, store)

    async def adapter_factory(key_id, model):
        return adapter

    definition = ToolDefinition(
        name="research_x",
        description="研究一下",
        tool_type="subagent",
        model="fake-model",
        sync=True,
        subagent_budget=SubagentBudget(max_iterations=5, max_tokens=100_000),
    )
    task_manager = TaskManager(EventBus(), max_concurrent=2)
    tool = SubagentTool(
        definition,
        credentials=store,
        task_manager=task_manager,
        adapter_factory=adapter_factory,
        bus=EventBus(),
    )

    result = await tool.run(query="研究饮食")

    assert result.ok is True
    assert len(client.calls) == 1, "子任务耗尽上限后不得再发请求"
    assert "上限已耗尽" in (result.content or ""), result.content
    assert store.get_metadata("k1")["budget_used"] == 100

    await task_manager.shutdown()


# -- 后台维护接线 ---------------------------------------------------------


async def test_background_draft_generation_stops_after_exhaustion(db_conn):
    """后台维护（工具草稿）也是真金白银的调用：耗尽后不再发新请求。"""
    from agent.services.maintenance import _generate_draft

    store = _store(db_conn, budget=100)
    client = ScriptedClient(
        [
            text_response('{"name": "auto_tool_0"}', prompt=60, completion=40),
            text_response('{"name": "never"}', prompt=60, completion=40),
        ]
    )
    adapter = _adapter(client, store)

    async def build_adapter():
        return adapter

    ctx = SimpleNamespace(build_adapter=build_adapter, bus=EventBus(), credentials=store)

    first = await _generate_draft(ctx, ["用户想批量导出报表"])
    assert first["name"] == "auto_tool_0"

    with pytest.raises(RuntimeError):
        # 预算已耗尽：第二次草稿生成拿不到任何模型响应（而不是继续偷偷调用）
        await _generate_draft(ctx, ["再来一次"])

    assert len(client.calls) == 1
    assert store.get_metadata("k1")["budget_used"] == 100


async def test_cancelled_request_does_not_charge_the_ledger(db_conn):
    """取消与预算是两件事：被取消的请求没有响应，就不扣额度、也不误报耗尽。"""

    class GatedClient(ScriptedClient):
        """停在请求中：取消之前不会返回任何结果。"""

        def __init__(self) -> None:
            super().__init__([])
            self.started = False
            self._gate = asyncio.Event()

        async def create(self, **kwargs: Any) -> FakeCompletion:
            self.started = True
            self.calls.append(kwargs)
            await self._gate.wait()
            return text_response("迟到的回答", prompt=60, completion=40)

    store = _store(db_conn, budget=100)
    client = GatedClient()
    adapter = _adapter(client, store)
    loop = _main_loop(adapter, store)

    task = asyncio.create_task(loop.run("你好"))
    for _ in range(200):
        if client.started:
            break
        await asyncio.sleep(0.01)
    assert client.started, "请求没有开始跑，测试前提不成立"

    loop.cancel()
    result = await asyncio.wait_for(task, timeout=3)

    assert result.cancelled is True
    assert store.get_metadata("k1")["budget_used"] == 0, "没有结果就不该扣额度"
    assert accounting_snapshot(adapter) == {
        "key_id": "k1",
        "requests": 0,
        "recorded": 0,
        "incomplete": 0,
    }
