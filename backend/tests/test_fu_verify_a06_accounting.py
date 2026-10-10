"""V 组独立验证 · A06：适配器必须显式绑定账本。

原缺陷（基线 `da0436b`，见 `_contracts` §5）

    `credential_usage` 的兜底是**一个进程内全局弱引用**：
    `CredentialPolicy.__init__` 每次构造都 `set_default_accounting_store(store)`，
    于是「后建的上下文覆盖先建的」。而 `AppContext._create_adapter()` 在基线里
    只给 adapter 打上 `key_id`，**没有**显式绑定它属于哪个账本。

    结果：只要进程里存在第二份凭据库（第二个上下文 / 第二份数据目录 / 导入的
    另一个库），第一份库里那条 adapter 的预算核对与用量写入都会落到**别人的账本**上：

      * A 账本 key 余额 0、B 账本同名 key 余额 100 → A 的适配器首次直接调用
        不会被自己的预算挡下（安全检查形同不存在）；
      * 把两份额度对调 → A 的适配器反而被 B 的 0 余额挡住；
      * 用量记到 B 上（A 的账本永远显示 0）。

    这条路径覆盖「不经过主循环 sink」的直接调用：后台提炼、引导追问、内部重试。

验证手段

    * 两份**独立** `AppContext`（各自的数据目录与 sqlite 文件），同名 key，
      额度 A=0 / B=100（以及交换的对照）；
    * 只通过真实的 `AppContext.build_adapter_for_credential()` 造适配器
      （能力探测用受控替身，**不联网、不付费**）；
    * 断言只看可观察量：`request_accounting(adapter).store`、预算核对是否抛
      `BudgetExhausted`、两个 sqlite 里 `credentials.budget_used` 的实际数字。

    不 import 任何尚不存在的新名字，因此基线可正常收集；失败是断言失败。
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent.adapters.base import AdapterMode
from agent.adapters.probe import ProbeResult
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.policy import BudgetExhausted
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

SHARED_KEY = "key-shared"
ENDPOINT = "https://api.example.com/v1"
MODEL = "gpt-x"


@pytest.fixture(autouse=True)
def fake_capability_probe(monkeypatch):
    """能力探测换受控替身：不联网、不付费（AGENTS.md 硬约束）。"""

    async def fake_probe(client, model, endpoint=None, cache=None):  # noqa: ANN001, ANN202
        return ProbeResult(AdapterMode.NATIVE, "ok", time.time())

    monkeypatch.setattr("agent.services.app.probe_adapter", fake_probe)
    return fake_probe


def _make_ctx(tmp_path, name: str, budget: float) -> tuple[AppContext, sqlite3.Connection]:
    db = connect(tmp_path / f"{name}.db")
    apply_migrations(db)
    ctx = AppContext(Settings(data_dir=tmp_path / name), db, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    ctx.credentials.create(
        SHARED_KEY,
        f"sk-{name}-secret",
        ["main-loop"],
        ENDPOINT,
        MODEL,
        budget=budget,
        verify_state="verified",
    )
    return ctx, db


def _budget_used(conn: sqlite3.Connection, key_id: str = SHARED_KEY) -> float:
    row = conn.execute(
        "SELECT budget_used FROM credentials WHERE id = ?", (key_id,)
    ).fetchone()
    assert row is not None, "场景构造：这条凭据必须存在"
    return float(row["budget_used"])


async def _adapter_of(ctx: AppContext):  # noqa: ANN202
    adapter = await ctx.build_adapter_for_credential(SHARED_KEY)
    assert adapter is not None, "场景构造：受控替身探测下必须能造出适配器"
    return adapter


# --------------------------------------------------------------------------
# 归属：显式绑定的账本优先于「后建的全局默认」
# --------------------------------------------------------------------------


async def test_a06_owning_ledger_wins_over_later_global_default(tmp_path):
    """先建 A（额度 0）再建 B（额度 100）：A 的适配器必须绑到 A。"""
    ctx_a, conn_a = _make_ctx(tmp_path, "a", 0.0)
    ctx_b, conn_b = _make_ctx(tmp_path, "b", 100.0)
    try:
        from agent.credentials.usage import default_accounting_store, request_accounting

        # 场景前提：全局默认**已经**被后建的 B 覆盖（这正是缺陷的触发条件）
        assert default_accounting_store() is ctx_b.credentials

        adapter = await _adapter_of(ctx_a)
        binding = request_accounting(adapter)

        assert binding is not None, "真实 adapter 必须能归因到某个账本"
        assert binding.store is ctx_a.credentials, (
            "A 的适配器必须显式绑定 A 自己的账本，不能被后建的全局默认库接管"
        )
        assert binding.key_id == SHARED_KEY

        await ctx_a.aclose()
        await ctx_b.aclose()
    finally:
        conn_a.close()
        conn_b.close()


async def test_a06_first_direct_call_is_blocked_by_own_budget(tmp_path):
    """A=0 / B=100：A 的适配器首次直接调用必须被**自己的**预算挡下。"""
    ctx_a, conn_a = _make_ctx(tmp_path, "a", 0.0)
    ctx_b, conn_b = _make_ctx(tmp_path, "b", 100.0)
    try:
        from agent.credentials.usage import ensure_adapter_request_allowed

        adapter = await _adapter_of(ctx_a)

        with pytest.raises(BudgetExhausted):
            ensure_adapter_request_allowed(adapter)

        await ctx_a.aclose()
        await ctx_b.aclose()
    finally:
        conn_a.close()
        conn_b.close()


async def test_a06_swapped_budgets_do_not_block_own_call(tmp_path):
    """额度对调（A=100 / B=0）：归属正确时 A 的调用**不得**被 B 的 0 余额挡住。"""
    ctx_a, conn_a = _make_ctx(tmp_path, "a", 100.0)
    ctx_b, conn_b = _make_ctx(tmp_path, "b", 0.0)
    try:
        from agent.credentials.usage import ensure_adapter_request_allowed

        adapter = await _adapter_of(ctx_a)

        # 归属正确 → 核对的是 A 的 100（够用）；归属错到 B → 会被 B 的 0 挡住
        remaining = ensure_adapter_request_allowed(adapter)
        assert remaining is not None and remaining > 0

        await ctx_a.aclose()
        await ctx_b.aclose()
    finally:
        conn_a.close()
        conn_b.close()


async def test_a06_usage_is_written_to_owning_ledger_exactly_once(tmp_path):
    """用量核对与写入必须用**同一个账本**：只进 A，且只进一次。"""
    ctx_a, conn_a = _make_ctx(tmp_path, "a", 1000.0)
    ctx_b, conn_b = _make_ctx(tmp_path, "b", 1000.0)
    try:
        from agent.credentials.usage import account_adapter_request

        adapter = await _adapter_of(ctx_a)
        usage = SimpleNamespace(input_tokens=7, output_tokens=3)

        receipt = account_adapter_request(adapter, usage)
        assert receipt is not None and receipt.recorded is True

        assert _budget_used(conn_a) == 10.0, "实际调用必须记在它自己的账本上"
        assert _budget_used(conn_b) == 0.0, "不得记到别的账本上"
        assert _budget_used(conn_a) == 10.0, "一次调用只能记一次（不得双记）"

        await ctx_a.aclose()
        await ctx_b.aclose()
    finally:
        conn_a.close()
        conn_b.close()


# --------------------------------------------------------------------------
# 不回归（对照）：单上下文正常路径、失败无用量标 incomplete
# --------------------------------------------------------------------------


async def test_a06_single_context_path_still_accounts(tmp_path):
    """单上下文（没有第二个库覆盖）：既有记账行为不得倒退。"""
    ctx_a, conn_a = _make_ctx(tmp_path, "solo", 1000.0)
    try:
        from agent.credentials.usage import account_adapter_request, ensure_adapter_request_allowed

        adapter = await _adapter_of(ctx_a)
        assert ensure_adapter_request_allowed(adapter) is not None

        receipt = account_adapter_request(
            adapter, SimpleNamespace(input_tokens=11, output_tokens=0)
        )
        assert receipt is not None and receipt.recorded is True
        assert _budget_used(conn_a) == 11.0

        await ctx_a.aclose()
    finally:
        conn_a.close()


async def test_a06_failed_call_without_usage_marks_incomplete_and_does_not_invent_numbers(
    tmp_path,
):
    """失败且没有用量：标 incomplete、**不造数**（对照，基线也应成立）。"""
    ctx_a, conn_a = _make_ctx(tmp_path, "fail", 1000.0)
    try:
        from agent.credentials.usage import account_adapter_failure

        adapter = await _adapter_of(ctx_a)
        receipt = account_adapter_failure(adapter, RuntimeError("transport boom"))

        assert receipt is not None
        assert receipt.recorded is False
        assert receipt.incomplete is True
        assert _budget_used(conn_a) == 0.0, "没有用量就不许写任何 token 数"

        await ctx_a.aclose()
    finally:
        conn_a.close()
