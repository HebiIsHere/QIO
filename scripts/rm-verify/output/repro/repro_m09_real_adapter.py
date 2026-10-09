# -*- coding: utf-8 -*-
"""复现 2（M09）：用**真实 NativeAdapter + 生产同款绑定**统计真实 provider 请求次数。

F 组原用例把 NativeAdapter.complete() 整个替换掉了，而集成后的预算闸门就装在
真实 complete() 的请求前（adapters/native.py 的 ensure_adapter_request_allowed），
所以原用例测在错误的边界上。这里按生产接线（credential_usage_sink 绑定）重测。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

from agent.adapters.native import NativeAdapter  # noqa: E402
from agent.api.bus import EventBus  # noqa: E402
from agent.core.loop import AgentLoop  # noqa: E402
from agent.credentials.store import CredentialStore, MemoryKeyring  # noqa: E402
from agent.credentials.usage import credential_usage_sink  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.tools.registry import ToolRegistry  # noqa: E402


class _Usage:
    def model_dump(self) -> dict:
        return {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


class _RawClient:
    """openai SDK 形状的假客户端：只数**真实请求**次数。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def chat(self):  # noqa: ANN201
        return self

    @property
    def completions(self):  # noqa: ANN201
        return self

    async def create(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        if self.calls == 1:
            tool = type(
                "TC",
                (),
                {"id": "c1", "function": type("Fn", (), {"name": "echo", "arguments": "{}"})()},
            )()
            message = type("Msg", (), {"content": None, "tool_calls": [tool]})()
            finish = "tool_calls"
        else:
            message = type("Msg", (), {"content": "第二次回答", "tool_calls": None})()
            finish = "stop"
        choice = type("Choice", (), {"message": message, "finish_reason": finish})()
        return type("Raw", (), {"choices": [choice], "usage": _Usage()})()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="rmf-repro-m09-"))
    conn = connect(tmp / "b.db")
    apply_migrations(conn)
    store = CredentialStore(conn, keyring_backend=MemoryKeyring())
    store.create(
        "k1",
        "placeholder-not-a-key",
        tags=["main-loop"],
        verify_state="verified",
        budget=120,
    )

    client = _RawClient()
    adapter = NativeAdapter(client, "m")
    adapter.key_id = "k1"
    loop = AgentLoop(
        adapter,
        ToolRegistry(),
        EventBus(),
        usage_sink=credential_usage_sink(store, adapter),
    )
    result = asyncio.run(loop.run("你好"))

    print("真实 provider 请求次数:", client.calls)
    print("最终回答:", repr(result.final_content))
    print("warnings:", result.warnings)
    print("budget_used:", store.get_metadata("k1")["budget_used"])


if __name__ == "__main__":
    main()
