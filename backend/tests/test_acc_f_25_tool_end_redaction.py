"""F 独立验证（阶段二 · F25）：TOOL_END 事件出口未脱敏（契约 §C3）。

Lead 裁定（2026-10-09）：编号 F25（相邻路径同范围新缺陷），维护者 acc-b2（core/loop.py 出口）。

最小反例：登记一个**随机合成敏感值** → 一个必然失败的工具把该值放进 error →
跑一轮（假 provider 脚本化一次工具调用）→ TOOL_END.error 原样携带敏感值。

观察（实际结果）：
* loop 的**日志**已脱敏（WARNING ... ***redacted***）；
* ASSISTANT 流式正文、TURN_END.final_content、TURN_END.annotation 都已脱敏（F07 / 注记路径）；
* 唯独 **TOOL_END 事件载荷**仍是原文 —— SSE 会把原文送到前端；同一条 error 还会继续被
  tool_state / _tool_facts / _record_tool_call / turn_facts.record_tool 保存。

期望：TOOL_END 的 error（以及 content_preview）在**事件出口处**先过
agent/trace/redact.py::redact_text 再发布，落库的同一条 error 同样不得带原文。

运行：cd backend; uv run --frozen pytest -q tests/test_acc_f_25_tool_end_redaction.py
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.core.turn_facts import ANNOTATION_HEADER
from agent.tools.base import Tool, ToolResult
from agent.trace.redact import clear_registered_secrets, register_secret

TOOL_NAME = "acc_f_25_leaky_tool"
BODY = "====正文====\n这一轮的正常回答。"
_SECRET: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _register_synthetic_secret():
    value = "accf25-" + uuid.uuid4().hex
    _SECRET["value"] = value
    assert register_secret(value) is True
    try:
        yield
    finally:
        clear_registered_secrets()
        _SECRET.clear()


class _FailingTool(Tool):
    name = TOOL_NAME
    description = "失败时会泄漏合成敏感值的工具（F25 反例）"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    is_concurrency_safe = False

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="工具失败，诊断值：" + _SECRET["value"], category="verify")


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _history(app) -> list:
    return list(app.state.ctx.bus._history)


def _of(app, kind: str) -> list:
    return [e for e in _history(app) if e.type.value == kind]


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _of(app, "TURN_END"):
            if str(event.data.get("turn_id")) == turn_id:
                return event.data
        await asyncio.sleep(0.05)
    return None


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    application.state.ctx.registry.register(_FailingTool())
    return application


async def test_tool_end_error_must_be_redacted(app):
    secret = _SECRET["value"]
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[ScriptedToolCall(id="c_accf25", name=TOOL_NAME, arguments={})]),
            StreamScript(),
            StreamScript(text=BODY),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        resp = await client.post("/api/turns", json={"message": "调用会失败的工具"})
        assert resp.status_code == 200, resp.text[:200]
        turn_id = str(resp.json()["turn_id"])
        end = await _wait_turn_end(app, turn_id)

    assert end is not None, "TURN_END 没有到达"
    # 对照组：注记路径确实已脱敏（证明登记生效、装置有效）
    note = str(end.get("annotation") or end.get("final_annotation") or "")
    assert ANNOTATION_HEADER in note, ("注记必须以独立字段完整交付", sorted(end.keys()))
    assert secret not in note, "注记泄漏了登记敏感值（对照失败：装置本身有问题）"

    tool_ends = _of(app, "TOOL_END")
    assert tool_ends, "必须有 TOOL_END 事件（装置失效）"
    leaked_indices = [
        i for i, e in enumerate(tool_ends) if secret in str(e.data.get("error") or "")
    ]
    assert not leaked_indices, (
        "TOOL_END.error 未脱敏：SSE 事件出口把登记敏感值原样发布（契约 §C3）",
        {"events": len(tool_ends), "leaked_indices": leaked_indices},
    )
    # 事件整体序列化也不得带原文（防止 error 之外的字段泄漏）
    leaking_events = [e.type.value for e in _history(app) if secret in e.model_dump_json()]
    assert not leaking_events, ("事件出口泄漏了登记敏感值", leaking_events)
    print("[诊断] F25 TOOL_END 出口：TOOL_END=%d 泄漏=否 注记脱敏=%s" % (len(tool_ends), secret not in note))
