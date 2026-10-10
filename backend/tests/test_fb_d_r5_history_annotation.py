"""fb-D 检查（R5 后端字段 · 冻结契约 K2.3）：历史 API 的 raw.annotation 是明确字段。

K2.3 冻结：实时 TURN_END 的注记走 annotation（兼容别名 final_annotation）；
历史里新记录以 raw.annotation（字符串）作为**明确字段**返回，正文保持原样；
legacy 旧记录才可能把注记内联在正文末尾（由消费端按固定表头拆分）。

本文件确认后端这一半已经成立（不满足时做最小修正）：
1. 产生系统核对注释的一轮，assistant 消息的 content 是**纯正文**（不含表头）；
2. 该消息的 raw（JSON）里有明确的 annotation 字段且完整（含表头与结论）；
3. 没有注释的消息不带 annotation 字段（不伪造）。

模型调用一律用假 provider；不联网、不读任何真实密钥。
运行：cd backend; uv run --frozen pytest -q -p no:warnings tests/test_fb_d_r5_history_annotation.py
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.server import create_app
from agent.core.turn_facts import ANNOTATION_HEADER
from agent.credentials.store import MemoryKeyring
from agent.tools.base import Tool, ToolResult

BODY = "====正文====\n这是这一轮唯一的正式回答。"
TOOL_NAME = "fb_d_failing_tool"


class _FailingTool(Tool):
    name = TOOL_NAME
    description = "永远失败的工具（R5 验证用）"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    is_concurrency_safe = False

    async def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(ok=False, error="这一步失败了，但可以重试", category="verify")


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _history(app) -> list:
    return list(app.state.ctx.bus._history)


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _history(app):
            if event.type.value == "TURN_END" and str(event.data.get("turn_id")) == turn_id:
                return event.data
        await asyncio.sleep(0.05)
    return None


def _raw_of(message: dict) -> dict:
    raw = message.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    application.state.ctx.registry.register(_FailingTool())
    return application


async def test_history_raw_has_explicit_annotation_field(app):
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[ScriptedToolCall(id="c_fb_d_r5", name=TOOL_NAME, arguments={})]),
            StreamScript(),
            StreamScript(text=BODY),
        ]
    )
    app.state.ctx.build_adapter = AsyncMock(return_value=adapter)

    async with _client(app) as client:
        resp = await client.post("/api/turns", json={"message": "调用一个会失败的工具"})
        assert resp.status_code == 200, resp.text[:200]
        turn_id = str(resp.json()["turn_id"])
        end = await _wait_turn_end(app, turn_id)
        assert end is not None, "TURN_END 没有到达"
        context = (await client.get("/api/session/context")).json()

    assert ANNOTATION_HEADER in str(end.get("annotation") or ""), (
        "装置失效：这一轮应当产生系统核对注释",
        sorted(end.keys()),
    )

    assistant = [
        message
        for message in context["messages"]
        if str(message.get("turn_id")) == turn_id and message.get("role") == "assistant"
    ]
    assert assistant, "历史里必须有这一轮的 assistant 消息"
    message = assistant[0]
    body = str(message.get("content") or "")
    assert body.startswith(BODY), ("历史正文必须原样保留", body)
    assert ANNOTATION_HEADER not in body, ("新记录不得把注记内联进正文", body[-160:])

    raw = _raw_of(message)
    note = raw.get("annotation")
    assert isinstance(note, str) and ANNOTATION_HEADER in note, (
        "历史 raw 必须以明确字段 annotation 返回系统核对注释（K2.3）",
        {"raw_keys": sorted(raw.keys())},
    )
    assert note.strip() == str(end.get("annotation") or "").strip(), (
        "历史注释与实时 TURN_END 必须一致",
        {"raw_len": len(note), "event_len": len(str(end.get("annotation") or ""))},
    )

    # 没有注释的消息不得伪造 annotation 字段
    others = [
        message
        for message in context["messages"]
        if str(message.get("turn_id")) != turn_id
    ]
    for other in others:
        assert "annotation" not in _raw_of(other), ("无注释的消息不得伪造字段", other.get("id"))
    print(
        "[诊断] R5 历史字段：assistant 正文纯净=%s；raw.annotation 完整=%s；消息数=%d"
        % (ANNOTATION_HEADER not in body, ANNOTATION_HEADER in note, len(context["messages"]))
    )
