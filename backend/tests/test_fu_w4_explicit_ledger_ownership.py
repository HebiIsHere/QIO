"""W4 / A06：适配器必须显式绑定所属账本（同进程多个 AppContext 的归属）。

限定场景（不夸大）：**同进程里存在多个 AppContext** 时，适配器第一次被**直接调用**
（后台提炼 / 引导追问 / 内部重试 —— 不经过主循环 sink）而尚未显式绑定记账器，
``request_accounting`` 会落到「最后登记的那个」进程默认库上：预算被别人的账本挡、
用量记到别人头上。独立进程不共享这个全局变量，所以这不是「所有多开都失效」。

本文件锁住的规则（契约 §5）：

1. ``bind_request_accounting(adapter, store)`` 是**显式**绑定；``key_id`` 缺省取
   ``adapter.key_id``；显式绑定永远优先，**后来创建的上下文不得改写已有归属**；
2. 全局默认库只允许作为「完全没有显式绑定」时的兼容兜底；
3. 预算核对（``ensure_adapter_request_allowed``）与用量写入
   （``account_adapter_request``）必须落在**同一个**账本上（只修一边不合格）。

验收：两份独立账本 A=0 / B=100，先建 A 再建 B（B 覆盖全局默认库）；A 的首次直接
调用被自己的预算挡下且 B 额度不被扣；交换额度后只写入实际归属账本一次；缓存复用
保持归属；B 关闭后 A 仍能正确查预算与记账。

模型调用一律**严格本地替身**（脚本化假客户端）：不联网、不需要真实 Key；
占位密钥串只用于本地替身账本。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent.adapters.base import ChatMessage, ModelUsage
from agent.adapters.native import NativeAdapter
from agent.credentials.policy import BudgetExhausted, CredentialPolicy
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import (
    account_adapter_request,
    accounting_snapshot,
    adapter_self_accounts,
    bind_request_accounting,
    clear_default_accounting_store,
    default_accounting_store,
    ensure_adapter_request_allowed,
    request_accounting,
    set_default_accounting_store,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

FAKE_SECRET = "sk-w4-fake-placeholder"  # 本地替身占位串，不是任何真实 Key
KEY = "k1"


# -- 严格本地替身：脚本化假 provider ---------------------------------------


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


def _tool_call(tool_id: str, name: str, arguments: str) -> Any:
    return type(
        "TC",
        (),
        {"id": tool_id, "function": type("F", (), {"name": name, "arguments": arguments})()},
    )()


def text_response(content: str, *, inp: int, out: int) -> FakeCompletion:
    return FakeCompletion([FakeChoice(FakeMessage(content, None))], usage=FakeUsage(inp, out))


class ScriptedClient:
    """按脚本逐次返回的假客户端：每次调用都被记下来（严格本地替身）。"""

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


# -- 两份独立账本 + 假 AppContext ------------------------------------------


@dataclass
class Ledger:
    """一份独立账本（自己的数据库 + 自己的凭据库），名字用于断言提示。"""

    name: str
    store: CredentialStore
    conn: Any

    def used(self, key_id: str = KEY) -> tuple[int, int, int]:
        meta = self.store.get_metadata(key_id) or {}
        return (
            int(meta["usage_input"]),
            int(meta["usage_output"]),
            int(meta["budget_used"]),
        )

    def left(self, key_id: str = KEY) -> float | None:
        return self.store.budget_left(key_id)

    def close(self) -> None:
        self.conn.close()


def _make_ledger(tmp_path: Path, name: str, budget: float | None) -> Ledger:
    conn = connect(tmp_path / f"{name}.db")
    apply_migrations(conn)
    store = CredentialStore(conn, keyring_backend=MemoryKeyring())
    store.create(
        KEY,
        FAKE_SECRET,
        tags=["main-loop"],
        verify_state="verified",
        budget=budget,
    )
    return Ledger(name, store, conn)


class FakeAppContext:
    """按 ``services/app.py`` 的形状模拟一个 AppContext（只为接线形状服务）。

    * ``__init__`` 建 ``CredentialPolicy`` —— 也就是登记进程内默认库，**后建覆盖先建**；
    * ``_create_adapter`` 建完 adapter 立刻显式绑定**本上下文**的账本
      （= Lead 要在 app.py 挂的那一行 ``bind_request_accounting(adapter, self.credentials)``）；
    * adapter 缓存命中直接返回，不在命中路径上改写归属。
    """

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.policy = CredentialPolicy(ledger.store)
        self._adapter_cache: dict[str, NativeAdapter] = {}

    def _create_adapter(self, key_id: str, client: Any) -> NativeAdapter:
        adapter = NativeAdapter(client=client, model="fake-model")
        adapter.key_id = key_id
        # ↓ 与 services/app.py::_create_adapter 里 Lead 要挂的那一行等价
        bind_request_accounting(adapter, self.ledger.store)
        return adapter

    def adapter_for(self, key_id: str, client: Any) -> NativeAdapter:
        cached = self._adapter_cache.get(key_id)
        if cached is not None:
            return cached
        adapter = self._create_adapter(key_id, client)
        self._adapter_cache[key_id] = adapter
        return adapter


@pytest.fixture(autouse=True)
def _isolated_default_store():
    """默认库是进程级全局：用例前后都清干净，互不污染。"""
    clear_default_accounting_store()
    yield
    clear_default_accounting_store()


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="你好")]


# -- A06 主场景 ------------------------------------------------------------


async def test_first_direct_call_of_a_is_blocked_by_its_own_budget(tmp_path):
    """A=0 / B=100；先建 A 再建 B（B 覆盖默认库）→ A 的首次直接调用被 A 自己挡下。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=0)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)

    client = ScriptedClient([text_response("这次请求不该发出去", inp=60, out=40)])
    adapter = ctx_a.adapter_for(KEY, client)

    # 前提（反例的土壤）：全局默认库现在是 B，A 的 adapter 只能靠显式绑定自证归属。
    assert default_accounting_store() is ledger_b.store
    assert ctx_b is not None
    binding = request_accounting(adapter)
    assert binding is not None
    assert binding.store is ledger_a.store
    assert binding.is_explicit is True

    with pytest.raises(BudgetExhausted):
        await adapter.complete(_messages(), [])

    assert client.calls == [], "预算耗尽不得发出新请求"
    assert ledger_b.used() == (0, 0, 0), "不得动另一份账本"
    assert ledger_b.left() == 100
    assert ledger_a.used() == (0, 0, 0)


def test_later_context_registration_cannot_rewrite_an_explicit_binding(tmp_path):
    """显式绑定是权威：别的上下文后来登记默认库（或再绑一次）都改不动它。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    adapter = ctx_a.adapter_for(KEY, ScriptedClient([]))
    first = request_accounting(adapter)
    assert first is not None

    ctx_b = FakeAppContext(ledger_b)
    assert ctx_b is not None
    set_default_accounting_store(ledger_b.store)  # 明确的「后登记」

    again = request_accounting(adapter)
    assert again is first
    assert again.store is ledger_a.store
    # 再显式绑一次也不行：缓存复用的 adapter 保持原归属
    assert bind_request_accounting(adapter, ledger_b.store) is first
    assert request_accounting(adapter).key_id == KEY


def test_default_store_is_only_a_fallback_for_never_bound_adapters(tmp_path):
    """默认库只是「完全没有显式绑定」时的兼容兜底，且不得被后登记改写。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)

    raw = NativeAdapter(client=ScriptedClient([]), model="fake-model")
    raw.key_id = KEY  # 从没显式绑定过（历史调用路径的形状）
    assert request_accounting(raw, allow_default=False) is None

    fallback = request_accounting(raw)
    assert fallback is not None
    assert fallback.store is ledger_a.store
    assert fallback.is_explicit is False

    ctx_b = FakeAppContext(ledger_b)  # 后登记覆盖默认库
    assert ctx_b is not None
    assert default_accounting_store() is ledger_b.store
    assert request_accounting(raw).store is ledger_a.store, "已有归属不得因后登记改变"

    # 显式绑定一出现就成为权威；升级时保留已发生的计数
    fallback.requests += 1
    upgraded = bind_request_accounting(raw, ledger_b.store)
    assert upgraded is not None
    assert upgraded.store is ledger_b.store
    assert upgraded.is_explicit is True
    assert upgraded.requests == 1
    set_default_accounting_store(ledger_a.store)
    assert request_accounting(raw).store is ledger_b.store


def test_cached_adapter_keeps_its_owner_when_a_second_context_appears(tmp_path):
    """adapter 缓存复用保持归属：命中路径不重建、不改写绑定。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    first = ctx_a.adapter_for(KEY, ScriptedClient([]))
    reused = ctx_a.adapter_for(KEY, ScriptedClient([]))
    assert reused is first, "缓存命中必须复用同一个 adapter"
    binding = request_accounting(first)

    ctx_b = FakeAppContext(ledger_b)
    ctx_b.adapter_for(KEY, ScriptedClient([]))

    assert request_accounting(reused) is binding
    assert binding.store is ledger_a.store


def test_budget_check_and_usage_write_share_one_ledger(tmp_path):
    """同一个账本：核对用哪一份，写入就必须用哪一份（只修一边不合格）。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)  # 默认库 = A

    raw = NativeAdapter(client=ScriptedClient([]), model="fake-model")
    raw.key_id = KEY
    assert ensure_adapter_request_allowed(raw) == 100, "核对发生在 A 的账本上"

    ctx_b = FakeAppContext(ledger_b)  # 核对之后，默认库换成 B
    assert ctx_b is not None

    receipt = account_adapter_request(raw, ModelUsage(input_tokens=2, output_tokens=3))
    assert receipt is not None and receipt.recorded is True
    assert ledger_a.used() == (2, 3, 5), "写入必须落在核对过的同一份账本上"
    assert ledger_b.used() == (0, 0, 0)


async def test_swapped_budgets_write_once_only_to_the_actual_owner(tmp_path):
    """交换额度（A=100 / B=0）后：只写入实际归属账本一次，另一方一分不动。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ledger_b = _make_ledger(tmp_path, "b", budget=0)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)  # 后建：默认库是 B，而且 B 的额度是 0

    client_a = ScriptedClient([text_response("A 的回答", inp=2, out=3)])
    adapter_a = ctx_a.adapter_for(KEY, client_a)
    completion = await adapter_a.complete(_messages(), [])
    assert completion.message.content == "A 的回答"
    assert len(client_a.calls) == 1, "每次实际调用记一次"
    assert ledger_a.used() == (2, 3, 5)
    assert ledger_a.left() == 95
    assert ledger_b.used() == (0, 0, 0), "不得写到另一份账本，也不得改它的额度"
    assert ledger_b.left() == 0
    assert accounting_snapshot(adapter_a) == {
        "key_id": KEY,
        "requests": 1,
        "recorded": 1,
        "incomplete": 0,
    }

    # 反向：B 自己的额度是 0，被自己的预算挡下，不动 A 的额度
    client_b = ScriptedClient([text_response("不该发出去", inp=2, out=3)])
    adapter_b = ctx_b.adapter_for(KEY, client_b)
    with pytest.raises(BudgetExhausted):
        await adapter_b.complete(_messages(), [])
    assert client_b.calls == []
    assert ledger_a.used() == (2, 3, 5)
    assert ledger_b.used() == (0, 0, 0)


async def test_a_still_checks_budget_and_records_after_b_is_gone(tmp_path):
    """B 关闭（登记撤销）后：A 照旧用**自己的**账本查预算与记账。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=5)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    client = ScriptedClient([text_response("第一次", inp=2, out=3)])
    adapter = ctx_a.adapter_for(KEY, client)
    ctx_b = FakeAppContext(ledger_b)
    assert ctx_b is not None
    assert default_accounting_store() is ledger_b.store

    # A 关闭不得抹掉 B 的登记；B 关闭只撤销自己那一条
    assert clear_default_accounting_store(ledger_a.store) is False
    assert clear_default_accounting_store(ledger_b.store) is True
    assert default_accounting_store() is None

    await adapter.complete(_messages(), [])
    assert ledger_a.used() == (2, 3, 5)
    assert ledger_a.left() == 0
    assert ledger_b.used() == (0, 0, 0)

    with pytest.raises(BudgetExhausted):
        await adapter.complete(_messages(), [])
    assert len(client.calls) == 1, "A 的额度（而不是 B 剩下的 100）说了算"
    assert ledger_b.used() == (0, 0, 0)
    assert ledger_b.left() == 100


def test_policy_bind_accounting_helper_uses_that_contexts_ledger(tmp_path):
    """``CredentialPolicy.bind_accounting`` 是同一件事的一行接线形态。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ledger_b = _make_ledger(tmp_path, "b", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    ctx_b = FakeAppContext(ledger_b)
    assert ctx_b is not None

    adapter = NativeAdapter(client=ScriptedClient([]), model="fake-model")
    adapter.key_id = KEY
    binding = ctx_a.policy.bind_accounting(adapter)
    assert binding is not None
    assert binding.store is ledger_a.store
    assert request_accounting(adapter).store is ledger_a.store


def test_unpinnable_adapter_is_not_silently_attributed(tmp_path):
    """钉不住绑定的替身：不做兜底（宁少记，不记错），也不冒充「自己记账」。"""
    ledger_a = _make_ledger(tmp_path, "a", budget=100)
    ctx_a = FakeAppContext(ledger_a)
    assert ctx_a is not None

    class Slotted:
        """没有 __dict__ 的替身：绑不上 _request_accounting。"""

        __slots__ = ("key_id", "accounts_requests")

        def __init__(self) -> None:
            self.key_id = KEY
            self.accounts_requests = True

    adapter = Slotted()
    assert request_accounting(adapter) is None
    assert request_accounting(adapter, allow_default=False) is None
    assert accounting_snapshot(adapter) is None
    assert adapter_self_accounts(adapter) is False
    assert ledger_a.used() == (0, 0, 0)
