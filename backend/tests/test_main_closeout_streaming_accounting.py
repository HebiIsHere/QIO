"""合并收尾验收（2026-10-11）：流式降级不许漏记**已经真实发出的请求**。

原缺陷（main `7ff5e2e`，`adapters/native.py`）

    `stream()` 的失败记账按**异常类别**判断：

        if isinstance(exc, (NotImplementedError, UnsupportedCapability)):
            return          # 当成「没有请求发出」

    可 `UnsupportedCapability` 有两个来源。`_stream_once()` 可能在**已经调用客户端、
    读到响应之后**才抛它 ——

        * 客户端不返回异步流（`client did not return an async stream`）；
        * 流里一个增量都没有、且看不到 Content-Type（服务忽略 stream=true）。

    最小反例：假客户端记录了一次 `create()` 调用、返回空异步流，真实适配器抛出
    `UnsupportedCapability`，却**没有调用任何记账入口** —— 这次真实请求在账本上
    凭空消失（界面统计也少一次）。反过来，还没发出请求的能力检查失败必须零记账，
    否则会凭空多出一次「失败的请求」。

本文件钉住（全部用假客户端 / 本地假 provider，不联网）：

    1. 能力检查阶段未发生请求 → 零请求、零记账（UnsupportedCapability 与
       NotImplementedError 两种都验）；
    2. 请求之后才发现不能用流式（空异步流 / 非异步响应）→ 该次请求登记一次
       「不完整用量」，不估算 token；
    3. 流式失败后整段降级 → 请求次数与登记次数一致（2 次请求 = 2 条登记）；
    4. 正常流式 → 真实用量只登记一次；
    5. 服务直接返回整段 JSON → 消费已有响应并登记，不额外重复请求；
    6. stream_options 被拒后重试 → 每次实际尝试分别登记，且**重试前重新核对预算**；
    7. 预算耗尽 → 不发出新的请求（含 `complete()` 内部解析重试）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import types
from typing import Any

import pytest

from agent.adapters.base import ChatMessage
from agent.adapters.errors import UnsupportedCapability
from agent.adapters.native import NativeAdapter
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.credentials.policy import BudgetExhausted
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import accounting_snapshot, bind_request_accounting
from agent.tools.registry import ToolRegistry

FAKE_SECRET = "sk-test-not-a-real-key"
KEY = "k1"


def _ns(**kw: Any) -> Any:
    return types.SimpleNamespace(**kw)


def _text_chunk(text: str) -> Any:
    return _ns(
        choices=[_ns(delta=_ns(content=text, tool_calls=None), index=0, finish_reason=None)],
        usage=None,
    )


def _finish_chunk(reason: str = "stop") -> Any:
    return _ns(
        choices=[_ns(delta=_ns(content=None, tool_calls=None), index=0, finish_reason=reason)],
        usage=None,
    )


def _usage_chunk(inp: int, out: int) -> Any:
    return _ns(
        choices=[],
        usage=_ns(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out),
    )


def _raw_completion(
    content: str | None,
    *,
    inp: int = 0,
    out: int = 0,
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
) -> Any:
    usage = (
        {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}
        if (inp or out)
        else None
    )
    return _ns(
        choices=[_ns(message=_ns(content=content, tool_calls=tool_calls), finish_reason=finish_reason)],
        usage=usage,
    )


async def _empty_async_stream() -> Any:
    """同 SDK 的「零 chunk 流」：形状对、内容空（服务忽略了 stream=true）。"""
    if False:  # pragma: no cover - 只是让它成为异步生成器
        yield None


def _iter_stream(chunks: list[Any]) -> Any:
    async def _gen() -> Any:
        for chunk in chunks:
            yield chunk

    return _gen()


# ---------------------------------------------------------------------------
# 假客户端
# ---------------------------------------------------------------------------


class _RecordingClient:
    """openai SDK 形状的假客户端；`mode` 决定 create() 怎么回。"""

    def __init__(self, mode: str = "stream", chunks: list[Any] | None = None) -> None:
        self.mode = mode
        self.chunks = list(chunks or [_text_chunk("ok"), _finish_chunk()])
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> "_RecordingClient":
        return self

    @property
    def completions(self) -> "_RecordingClient":
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.mode == "empty_stream":
            return _empty_async_stream()
        if self.mode == "non_async":
            return {"not": "a stream"}
        if self.mode == "reject_stream_options" and "stream_options" in kwargs:
            raise TypeError("unexpected keyword argument 'stream_options'")
        return _iter_stream(self.chunks)


class _DegradingClient:
    """stream=true → 空流（真实请求已发出）；整段 → 带真实用量的完成。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> "_DegradingClient":
        return self

    @property
    def completions(self) -> "_DegradingClient":
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return _empty_async_stream()
        return _raw_completion("整段回答", inp=11, out=7)


class _NonSseClient:
    """只看 raw 路径：忽略 stream=true、直接回整段 JSON 的服务。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> "_NonSseClient":
        return self

    @property
    def completions(self) -> "_NonSseClient":
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        body = json.dumps(self.payload).encode("utf-8")

        async def _aread() -> bytes:
            return body

        return _ns(
            http_response=_ns(
                status_code=200,
                headers={"content-type": "application/json"},
                aread=_aread,
            )
        )

    @property
    def with_raw_response(self) -> "_NonSseClient":
        return self


class _ParseFailClient:
    """每次都回「工具参数不是合法 JSON」的整段响应（内部解析重试用）。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> "_ParseFailClient":
        return self

    @property
    def completions(self) -> "_ParseFailClient":
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return _raw_completion(
            None,
            tool_calls=[_ns(id="call_bad", function=_ns(name="echo", arguments="{bad json"))],
            inp=10,
            out=5,
        )


# ---------------------------------------------------------------------------
# 公共装置
# ---------------------------------------------------------------------------


def _bound(
    client: Any,
    db_conn: sqlite3.Connection,
    *,
    budget: int | None = None,
    cls: type[NativeAdapter] = NativeAdapter,
) -> tuple[NativeAdapter, CredentialStore]:
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create(KEY, FAKE_SECRET, tags=["main-loop"], verify_state="verified", budget=budget)
    adapter = cls(client=client, model="fake-model")
    adapter.key_id = KEY
    bind_request_accounting(adapter, store, KEY)
    return adapter, store


async def _drain(adapter: NativeAdapter) -> list[Any]:
    out: list[Any] = []
    async for delta in adapter.stream([ChatMessage(role="user", content="hi")], []):
        out.append(delta)
    return out


def _meta(store: CredentialStore) -> Any:
    return store.get_metadata(KEY)


# ---------------------------------------------------------------------------
# 1. 能力检查阶段未发生请求 → 零请求、零记账
# ---------------------------------------------------------------------------


class _PrecheckUnsupported(NativeAdapter):
    """模拟「还没发请求就发现这条路径用不了流式」。"""

    async def _stream_once(self, kwargs: dict[str, Any], attempt: Any) -> Any:  # type: ignore[override]
        assert attempt.requested is False
        raise UnsupportedCapability("capability precheck: no async stream support")
        yield  # pragma: no cover - 让本方法保持异步生成器形状


class _PrecheckNotImplemented(NativeAdapter):
    """BaseAdapter 默认实现的形状：没实现 stream，同样一个请求都没发。"""

    async def _stream_once(self, kwargs: dict[str, Any], attempt: Any) -> Any:  # type: ignore[override]
        assert attempt.requested is False
        raise NotImplementedError("not implemented")
        yield  # pragma: no cover


def test_capability_precheck_without_request_accounts_nothing(db_conn):
    client = _RecordingClient()
    adapter, store = _bound(client, db_conn, cls=_PrecheckUnsupported)

    with pytest.raises(UnsupportedCapability):
        asyncio.run(_drain(adapter))

    assert client.requests == [], "能力检查阶段不得发出任何请求"
    assert accounting_snapshot(adapter) == {
        "key_id": KEY,
        "requests": 0,
        "recorded": 0,
        "incomplete": 0,
    }
    assert int(_meta(store)["usage_input"]) == 0


def test_not_implemented_stream_without_request_accounts_nothing(db_conn):
    client = _RecordingClient()
    adapter, store = _bound(client, db_conn, cls=_PrecheckNotImplemented)

    with pytest.raises(NotImplementedError):
        asyncio.run(_drain(adapter))

    assert client.requests == []
    assert accounting_snapshot(adapter)["requests"] == 0
    assert int(_meta(store)["usage_input"]) == 0


# ---------------------------------------------------------------------------
# 2. 请求之后才发现不能用流式 → 该次请求必须登记（最小反例）
# ---------------------------------------------------------------------------


def test_empty_async_stream_after_request_is_accounted(db_conn):
    """最小反例：create() 调了一次、返回空异步流 → 不能漏记。"""
    client = _RecordingClient(mode="empty_stream")
    adapter, store = _bound(client, db_conn)

    with pytest.raises(UnsupportedCapability):
        asyncio.run(_drain(adapter))

    assert len(client.requests) == 1, "场景前提：这次尝试真的调用了客户端"
    snapshot = accounting_snapshot(adapter)
    assert snapshot["requests"] == 1, snapshot
    assert snapshot["incomplete"] == 1 and snapshot["recorded"] == 0, snapshot
    assert int(_meta(store)["usage_input"]) == 0, "没有可靠用量就不许估算 token"
    assert int(_meta(store)["usage_output"]) == 0


def test_non_async_response_after_request_is_accounted(db_conn):
    """客户端不返回异步流（`client did not return an async stream`）同样不能漏记。"""
    client = _RecordingClient(mode="non_async")
    adapter, store = _bound(client, db_conn)

    with pytest.raises(UnsupportedCapability):
        asyncio.run(_drain(adapter))

    assert len(client.requests) == 1
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["incomplete"], snapshot["recorded"]) == (1, 1, 0)
    assert int(_meta(store)["usage_input"]) == 0


def test_stream_that_ends_without_a_completion_is_accounted(db_conn):
    """防御分支：拿到了流却一条 done 都没有 —— 这次请求同样不许漏记。"""

    class _SilentEof(NativeAdapter):
        async def _stream_once(self, kwargs: dict[str, Any], attempt: Any) -> Any:  # type: ignore[override]
            attempt.requested = True
            if False:  # pragma: no cover
                yield None

    client = _RecordingClient()
    adapter, store = _bound(client, db_conn, cls=_SilentEof)

    assert asyncio.run(_drain(adapter)) == []
    # 客户端没被调用，但「请求事实」为真 → 记一次不完整（这正是防御分支的意义）
    assert accounting_snapshot(adapter)["incomplete"] == 1
    assert int(_meta(store)["usage_input"]) == 0


# ---------------------------------------------------------------------------
# 3. 流式失败后整段降级：请求次数 == 登记次数
# ---------------------------------------------------------------------------


def test_stream_degrading_to_full_completion_registers_both_requests(db_conn):
    client = _DegradingClient()
    adapter, store = _bound(client, db_conn)
    loop = AgentLoop(adapter, ToolRegistry(), EventBus())

    result = asyncio.run(loop.run("你好"))

    assert "整段回答" in (result.final_content or "")
    assert len(client.requests) == 2, "流式一次 + 整段降级一次 = 两次真实请求"
    assert client.requests[0].get("stream") is True
    assert not client.requests[1].get("stream")
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["recorded"], snapshot["incomplete"]) == (2, 1, 1), snapshot
    assert int(_meta(store)["usage_input"]) == 11 and int(_meta(store)["usage_output"]) == 7


# ---------------------------------------------------------------------------
# 4. 正常流式：真实用量只登记一次
# ---------------------------------------------------------------------------


def test_normal_stream_records_real_usage_once(db_conn):
    client = _RecordingClient(chunks=[_text_chunk("你好"), _finish_chunk(), _usage_chunk(11, 7)])
    adapter, store = _bound(client, db_conn)

    deltas = asyncio.run(_drain(adapter))

    assert deltas[-1].completion is not None and deltas[-1].completion.usage is not None
    assert len(client.requests) == 1
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["recorded"], snapshot["incomplete"]) == (1, 1, 0), snapshot
    assert int(_meta(store)["usage_input"]) == 11 and int(_meta(store)["usage_output"]) == 7


def test_streaming_path_does_not_double_count_with_loop_sink(db_conn):
    """记账只有一个负责入口：请求层记过，上层的 usage sink 必须跳过。

    Native / Anthropic 都声明 `accounts_requests`（每次实际请求自己记账），
    生产接线的 `credential_usage_sink` 据此 `covers_requests=True`。
    """
    from agent.adapters.anthropic import AnthropicAdapter
    from agent.credentials.usage import adapter_self_accounts, credential_usage_sink

    assert NativeAdapter.accounts_requests is True
    assert AnthropicAdapter.accounts_requests is True

    client = _RecordingClient(chunks=[_text_chunk("你好"), _finish_chunk(), _usage_chunk(11, 7)])
    adapter, store = _bound(client, db_conn)
    sink = credential_usage_sink(store, adapter)
    assert adapter_self_accounts(adapter) is True and sink.covers_requests is True

    loop = AgentLoop(adapter, ToolRegistry(), EventBus(), usage_sink=sink)
    result = asyncio.run(loop.run("你好"))

    assert "你好" in (result.final_content or "")
    meta = _meta(store)
    assert (int(meta["usage_input"]), int(meta["usage_output"])) == (11, 7), meta
    assert accounting_snapshot(adapter)["requests"] == 1


# ---------------------------------------------------------------------------
# 5. 服务直接回整段 JSON：消费已有响应并登记，不额外重复请求
# ---------------------------------------------------------------------------


def test_non_sse_json_body_is_consumed_and_accounted_without_extra_request(db_conn):
    client = _NonSseClient(
        {
            "choices": [{"message": {"content": "整段回答"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        }
    )
    adapter, store = _bound(client, db_conn)

    deltas = asyncio.run(_drain(adapter))

    assert len(client.requests) == 1, "已经有整段响应，不许再补发一次请求"
    assert deltas[-1].completion is not None
    assert deltas[-1].completion.message.content == "整段回答"
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["recorded"], snapshot["incomplete"]) == (1, 1, 0), snapshot
    assert int(_meta(store)["usage_input"]) == 9 and int(_meta(store)["usage_output"]) == 4


# ---------------------------------------------------------------------------
# 6. stream_options 被拒后重试：每次尝试分别登记 + 重试前核对预算
# ---------------------------------------------------------------------------


def test_stream_options_rejection_registers_each_actual_attempt(db_conn):
    client = _RecordingClient(
        mode="reject_stream_options",
        chunks=[_text_chunk("ok"), _finish_chunk(), _usage_chunk(11, 7)],
    )
    adapter, store = _bound(client, db_conn)

    deltas = asyncio.run(_drain(adapter))

    assert [d.text for d in deltas if d.text] == ["ok"]
    assert len(client.requests) == 2
    assert "stream_options" in client.requests[0] and "stream_options" not in client.requests[1]
    # 第一次被拒（真实请求，无用量）+ 第二次成功（真实用量）：两次都登记
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["recorded"], snapshot["incomplete"]) == (2, 1, 1), snapshot
    assert int(_meta(store)["usage_input"]) == 11 and int(_meta(store)["usage_output"]) == 7


def test_stream_options_retry_checks_budget_before_second_request(db_conn, monkeypatch):
    import agent.credentials.usage as usage_module

    checks: list[Any] = []

    def _gate(adapter: Any) -> None:
        checks.append(adapter)
        if len(checks) >= 2:  # 第二次核对（重试之前）时判定额度耗尽
            raise BudgetExhausted("budget exhausted (test)")

    monkeypatch.setattr(usage_module, "ensure_adapter_request_allowed", _gate)
    client = _RecordingClient(mode="reject_stream_options", chunks=[_text_chunk("不该发生")])
    adapter, _store = _bound(client, db_conn)

    with pytest.raises(BudgetExhausted):
        asyncio.run(_drain(adapter))

    assert len(checks) >= 2, "重试之前必须重新核对预算，不能复用第一次的结论"
    assert len(client.requests) == 1, "预算耗尽时不得发出新的请求"
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["incomplete"]) == (1, 1), snapshot


def test_complete_parse_retry_checks_budget_before_each_request(db_conn, monkeypatch):
    """`complete()` 的内部解析重试同样每次请求前核对预算（不得绕过）。"""
    import agent.credentials.usage as usage_module

    checks: list[Any] = []

    def _gate(adapter: Any) -> None:
        checks.append(adapter)
        if len(checks) >= 2:
            raise BudgetExhausted("budget exhausted (test)")

    monkeypatch.setattr(usage_module, "ensure_adapter_request_allowed", _gate)
    client = _ParseFailClient()
    adapter, _store = _bound(client, db_conn, budget=10**6)

    with pytest.raises(BudgetExhausted):
        asyncio.run(adapter.complete([ChatMessage(role="user", content="hi")], []))

    assert len(client.requests) == 1, "第二次解析重试不得再发请求"
    snapshot = accounting_snapshot(adapter)
    assert (snapshot["requests"], snapshot["recorded"]) == (1, 1), snapshot
