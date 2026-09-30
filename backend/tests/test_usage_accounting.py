"""用量归因：一次模型调用的进/出要累计到「用的那把钥匙」上。

真实问题（2026-09-29 评审）：凭据可以设「用量上限」，但运行期从来不回写用量
（`record_usage` 只有测试调用），于是「已用量」永远停在 0，上限也就不会真的生效。

两条硬约束：

* 一次调用要分开记「进」与「出」—— 账单口径不同，混成一个数字看不出钱花在哪；
* 归因不了就不记（宁少记，不记错）：不允许把用量记到另一把钥匙头上。
"""

from __future__ import annotations

import sqlite3

from agent.adapters.base import ChatMessage, Completion, ModelUsage
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.credentials.usage import credential_usage_sink
from agent.tools.registry import ToolRegistry


class _UsageAdapter:
    """最小适配器：一次调用返回固定用量。"""

    mode = "native"
    model = "fake-usage"
    key_id = "k1"

    def __init__(self, usage: ModelUsage) -> None:
        self.usage = usage
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        return Completion(
            message=ChatMessage(role="assistant", content="收到"),
            usage=self.usage,
        )


def _store(db_conn: sqlite3.Connection) -> CredentialStore:
    return CredentialStore(db_conn, keyring_backend=MemoryKeyring())


async def test_loop_reports_each_model_call_to_the_usage_sink():
    seen: list[tuple[int, int]] = []
    adapter = _UsageAdapter(ModelUsage(input_tokens=120, output_tokens=30))
    loop = AgentLoop(
        adapter,
        ToolRegistry(),
        EventBus(),
        usage_sink=lambda inp, out: seen.append((inp, out)),
    )
    result = await loop.run("你好")
    assert result.cancelled is False
    assert seen == [(120, 30)], "每次模型调用都要报一次进/出"


def test_credential_usage_sink_writes_to_that_credential(db_conn: sqlite3.Connection):
    store = _store(db_conn)
    store.create("k1", "sk-x", tags=["main-loop"], verify_state="verified", budget=1000)
    adapter = _UsageAdapter(ModelUsage(input_tokens=120, output_tokens=30))

    sink = credential_usage_sink(store, adapter)
    assert sink is not None
    sink(120, 30)

    meta = store.get_metadata("k1")
    assert (meta["usage_input"], meta["usage_output"], meta["budget_used"]) == (120, 30, 150)
    assert store.budget_left("k1") == 850


def test_credential_usage_sink_is_none_when_the_adapter_has_no_credential(
    db_conn: sqlite3.Connection,
):
    """归因不了就不记：宁可少记，也不能记到别的钥匙头上。"""

    class _Anonymous:
        mode = "native"
        model = "fake"

    assert credential_usage_sink(_store(db_conn), _Anonymous()) is None
