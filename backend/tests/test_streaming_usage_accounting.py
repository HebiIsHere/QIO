"""流式调用的记账（集成补丁）：真流式同样进凭据账本、同样受预算门禁。

为什么单独钉这一条（合并后发现的真实缺口）：

* main 的记账口径是「adapter 自己记每一次实际请求」（`accounts_requests = True`），
  loop 的上层 usage sink 据此**跳过**补记；
* 而流式实现（`native.stream` / `anthropic.stream`）集成时只带来 `supports_stream`，
  没有记账 —— 于是回答阶段的主路径（真流式）既不进账本、也不受预算上限约束，
  而界面/trace 里的用量数字仍然显示，表面上没有异常；
* 仓库里原有断言全部落在 `complete()` 路径，抓不到这个缺口。

本用例把这条边界钉住：一次用户可见的流式调用必须记一次账（用量来自流结束时的
真实 usage），并且进入请求之前必须先核对预算。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_streaming_usage_accounting.py -q
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from agent.adapters.base import ChatMessage
from agent.adapters.native import NativeAdapter
from agent.credentials.policy import BudgetExhausted
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import accounting_snapshot, bind_request_accounting

FAKE_SECRET = "sk-test-not-a-real-key"


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


class _FakeOpenAIClient:
    """openai SDK 形状：chat.completions.create(stream=True) → 异步分片。"""

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = list(chunks)
        self.requests: list[dict[str, Any]] = []

    @property
    def chat(self) -> "_FakeOpenAIClient":
        return self

    @property
    def completions(self) -> "_FakeOpenAIClient":
        return self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)

        async def _iter():
            for chunk in self.chunks:
                yield chunk

        return _iter()


def _bound(client: Any, db_conn: Any, *, budget: int | None = None) -> tuple[NativeAdapter, CredentialStore]:
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create("k1", FAKE_SECRET, tags=["stream"], verify_state="verified", budget=budget)
    adapter = NativeAdapter(client=client, model="fake-model")
    adapter.key_id = "k1"
    bind_request_accounting(adapter, store, "k1")
    return adapter, store


async def _drain(adapter: NativeAdapter) -> list[Any]:
    out: list[Any] = []
    async for delta in adapter.stream([ChatMessage(role="user", content="hi")], []):
        out.append(delta)
    return out


def test_stream_records_real_usage_once(db_conn):
    """流结束时的真实用量必须入账 —— 且一次用户可见调用只记一次。"""
    client = _FakeOpenAIClient([_text_chunk("你好"), _finish_chunk(), _usage_chunk(11, 7)])
    adapter, store = _bound(client, db_conn)

    deltas = asyncio.run(_drain(adapter))
    assert deltas and deltas[-1].completion is not None
    assert deltas[-1].completion.usage is not None

    snapshot = accounting_snapshot(adapter)
    assert snapshot is not None
    assert snapshot["requests"] == 1, snapshot
    meta = store.get_metadata("k1") or {}
    assert int(meta["usage_input"]) == 11, meta
    assert int(meta["usage_output"]) == 7, meta
    assert len(client.requests) == 1


def test_stream_checks_budget_before_sending_anything(db_conn, monkeypatch):
    """预算门禁：流式同样必须在**发出请求之前**核对累计用量。"""
    import agent.credentials.usage as usage_module

    seen: list[Any] = []

    def _deny(adapter: Any) -> None:
        seen.append(adapter)
        raise BudgetExhausted("budget exhausted (test)")

    monkeypatch.setattr(usage_module, "ensure_adapter_request_allowed", _deny)
    client = _FakeOpenAIClient([_text_chunk("不该发生")])
    adapter, _store = _bound(client, db_conn)

    with pytest.raises(BudgetExhausted):
        asyncio.run(_drain(adapter))
    assert seen, "进入流式之前必须先核对预算"
    assert client.requests == [], "预算耗尽时不得发出任何请求"


def test_text_adapter_is_not_expected_to_stream(db_conn):
    """诚实边界：text 档显式声明不支持流式（由 loop 一次性交付），不参与本补丁。"""
    from agent.adapters.text import TextAdapter

    assert TextAdapter(client=object(), model="m").supports_stream is False
