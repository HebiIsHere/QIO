"""F 独立验证（阶段一 · F11）：工具失败 + 系统注释下的「正式回答正文唯一且注释完整」。

契约：docs/plans/2026-10-09-process-attachment-audit-consolidation.md §C1 ——
「TURN_END 可携带 final_content；前端 applyFinalAnswer 以 turn 身份 + 最终校准为准，
禁止再用「全文是否相等」判断同一次回答」；§C3 注释也必须完整交付。

场景：一轮里工具失败 → 后端事实台账在最终答复末尾追加系统注释（core/turn_facts.py 的
ANNOTATION_HEADER）→ 流式正文 body 与 TURN_END.final_content = body + 注释。
本文件在**后端出口**钉住事实，并把它**真实捕获**成前端可复放的夹具
（docs/acc/acc-f-f11-events.json，已归一化 volatile 字段），供前端真实 store 用例消费。

链路：假 provider（FakeStreamAdapter）→ adapter → AgentLoop → TurnManager → 事件总线。

断言（后端出口）：
1) TURN_END.status = completed，final_content = body + 完整系统注释；
2) 注释只出现在最终校准里，ASSISTANT 流式正文里没有（否则就是重复来源）；
3) 正式回答正文在 ASSISTANT 事件里恰好出现一次。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_acc_f_11_annotation_final_answer.py -q
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.core.turn_facts import ANNOTATION_HEADER
from agent.tools.base import Tool, ToolResult

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "docs" / "acc" / "acc-f-f11-events.json"
BODY = "====正文====\n这是这一轮唯一的正式回答。"
TOOL_NAME = "acc_f_failing_tool"

# 夹具里不保留的易变字段（时间 / 版本 / token 等），保证重复运行夹具稳定。
VOLATILE_KEYS = {
    "revision", "instance_id", "created_at", "ended_at", "started_at",
    "duration_ms", "queue_ms", "tokens", "input_tokens", "output_tokens",
    "total_tokens", "iterations", "tool_calls", "usage", "record_id", "presentation",
}
FIXTURE_TYPES = {
    "TURN_START", "ASSISTANT", "TURN_END", "WARNING", "ERROR", "STAGE", "NARRATIVE",
}


class _FailingTool(Tool):
    name = TOOL_NAME
    description = "永远失败的工具（F11 验证用）"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    is_concurrency_safe = False

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="这一步失败了，但可以重试", category="verify")


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _events(app, kind: str | None = None) -> list:
    out = []
    for event in list(app.state.ctx.bus._history):
        if kind is None or event.type.value == kind:
            out.append(event)
    return out


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _events(app, "TURN_END"):
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


def _norm_data(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k not in VOLATILE_KEYS}
    if "turn_id" in out:
        out["turn_id"] = "turn_accf11"
    delta = out.get("delta_id")
    if isinstance(delta, str) and "_" in delta:
        out["delta_id"] = "dl_accf11_" + delta.rsplit("_", 1)[-1]
    return out


def _write_fixture(app) -> int:
    rows = []
    for index, event in enumerate(_events(app)):
        if event.type.value not in FIXTURE_TYPES:
            continue
        rows.append(
            {
                "type": event.type.value,
                "id": "evt_fixture_%02d" % index,
                "ts": "2026-10-09T00:00:00+00:00",
                "data": _norm_data(dict(event.data)),
            }
        )
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return len(rows)


async def test_tool_failure_annotation_is_complete_and_body_is_unique(app):
    adapter = FakeStreamAdapter(
        [
            StreamScript(tool_calls=[ScriptedToolCall(id="c_accf11", name=TOOL_NAME, arguments={})]),
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
    assert end.get("status") == "completed", ("工具失败可恢复，整轮仍应完成", end.get("status"))
    final = str(end.get("final_content") or "")
    assert final.startswith(BODY), ("模型正文必须原样保留在 final_content 开头", final)
    assert ANNOTATION_HEADER in final, ("系统注释必须完整出现在 final_content", final)
    assert "不能当作「已完成 / 可使用」" in final, ("注释的结论句必须完整", final)

    # 注释只来自最终校准；ASSISTANT 流式正文里不得出现（否则重复来源）
    assistant = _events(app, "ASSISTANT")
    assert assistant, "必须有 ASSISTANT 事件"
    contents = [str(e.data.get("content") or "") for e in assistant]
    assert all(ANNOTATION_HEADER not in text for text in contents), (
        "注释不得混进流式正文（会让正式回答出现两份）", contents
    )

    # 正式回答正文恰好出现一次
    occurrences = sum(text.count(BODY) for text in contents)
    assert occurrences == 1, (
        "正式回答正文在 ASSISTANT 事件里必须恰好出现一次",
        {"occurrences": occurrences, "events": len(contents)},
    )

    written = _write_fixture(app)
    assert FIXTURE_PATH.exists(), "前端复放夹具没有写出"
    print(
        "[诊断] F11 后端出口：status=%s 注释完整=%s 正文出现次数=%d 夹具事件=%d"
        % (end.get("status"), ANNOTATION_HEADER in final, occurrences, written)
    )
