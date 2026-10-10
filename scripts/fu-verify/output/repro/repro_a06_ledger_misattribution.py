# -*- coding: utf-8 -*-
"""V 组反例证据（A06）：适配器的记账归属被「后建的全局默认库」接管。

现场（基线 `da0436b`）：两个独立 AppContext（A 额度 0 / B 额度 100，同名 key），
全局默认库是后建的 B；`_create_adapter` 没有显式绑定，于是 A 的适配器
  1. 预算核对落在 B 上（A 的 0 余额拦不住它）；
  2. 用量写进 B 的账本（A 永远是 0）。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from agent.adapters.base import AdapterMode  # noqa: E402
from agent.adapters.probe import ProbeResult  # noqa: E402
from agent.api.bus import EventBus  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.services.app import AppContext  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402

import agent.services.app as app_module  # noqa: E402

KEY = "key-shared"


async def fake_probe(client, model, endpoint=None, cache=None):  # noqa: ANN001, ANN202
    return ProbeResult(AdapterMode.NATIVE, "ok", time.time())


def make_ctx(tmp: Path, name: str, budget: float):
    conn = connect(tmp / f"{name}.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp / name), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    ctx.credentials.create(
        KEY, f"sk-{name}-secret", ["main-loop"], "https://api.example.com/v1", "gpt-x",
        budget=budget, verify_state="verified",
    )
    return ctx, conn


def used(conn) -> float:
    return float(conn.execute(
        "SELECT budget_used FROM credentials WHERE id = ?", (KEY,)
    ).fetchone()["budget_used"])


async def main() -> None:
    app_module.probe_adapter = fake_probe  # 受控替身：不联网、不付费

    tmp = Path(tempfile.mkdtemp(prefix="fuv-repro-a06-"))
    ctx_a, conn_a = make_ctx(tmp, "a", 0.0)
    ctx_b, conn_b = make_ctx(tmp, "b", 100.0)

    from agent.credentials.usage import (
        account_adapter_request,
        default_accounting_store,
        ensure_adapter_request_allowed,
        request_accounting,
    )

    print("[构造] A 账本 key 额度 =", ctx_a.credentials.budget_left(KEY),
          " B 账本同名 key 额度 =", ctx_b.credentials.budget_left(KEY))
    print("[构造] 全局默认库是 B 吗 =", default_accounting_store() is ctx_b.credentials)

    adapter = await ctx_a.build_adapter_for_credential(KEY)
    print("[构造] A 造出的 adapter.key_id =", getattr(adapter, "key_id", None))

    binding = request_accounting(adapter)
    print("[基线观察] 这条 adapter 实际归属的账本是 =",
          "A" if binding is not None and binding.store is ctx_a.credentials
          else ("B" if binding is not None and binding.store is ctx_b.credentials else None))

    try:
        left = ensure_adapter_request_allowed(adapter)
        print("[基线观察] A=0 的首次直接调用核对结果 = 放行，剩余额度", left)
    except Exception as exc:  # noqa: BLE001
        print("[基线观察] A=0 的首次直接调用核对结果 = 被挡下：", type(exc).__name__, exc)

    account_adapter_request(adapter, SimpleNamespace(input_tokens=7, output_tokens=3))
    print("[基线观察] 记完一次用量之后：A.budget_used =", used(conn_a),
          " B.budget_used =", used(conn_b))
    print("[结论] A 的预算 0 拦不住 A 的调用，用量记进了 B 的账本。")

    await ctx_a.aclose()
    await ctx_b.aclose()
    conn_a.close()
    conn_b.close()


if __name__ == "__main__":
    asyncio.run(main())
