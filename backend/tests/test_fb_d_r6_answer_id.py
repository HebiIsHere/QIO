"""fb-D 独立验收（R6 · 冻结契约 K2.1）：TURN_END 的最小回答身份字段 answer_id。

K2.1 冻结：回答身份 = 流式 delta_id（一次模型调用的累计正文段）；生产端在 TURN_END
增加最小字段 **answer_id**，指向本次最终校准的目标回答的 delta_id；没有可校准回答时
缺省 / 为 null（旧生产端没有该字段）。

本文件（真实 FastAPI + TurnManager + 假 provider）钉住：
1. 正常声明回答 → answer_id == 承载 final_content 的那条 ASSISTANT 的 delta_id，
   且该身份在 ASSISTANT 事件里 interim=False（不是过程区文字）；
2. 工具轮 + 兜底/未声明回答 → answer_id 指向**最终回答段**，不是工具轮的过程说明；
3. 整轮没有任何回答段（模型空输出 → 系统补的停止说明）→ answer_id 为 null；
4. core/turn.py::_emit_turn_end 透传该字段，重复调用幂等（只有一条 TURN_END）。

模型调用一律用假 provider；不联网、不读任何真实密钥。
运行：cd backend; uv run --frozen pytest -q -p no:warnings tests/test_fb_d_r6_answer_id.py
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.api.server import create_app
from agent.core.turn import TurnManager
from agent.credentials.store import MemoryKeyring
from agent.tools.base import Tool, ToolResult

DECL = "[[QIO:ANSWER]]"
BODY = "====正文====\n这是这一轮唯一的正式回答。"
TOOL_NAME = "fb_d_answer_id_tool"


class _OkTool(Tool):
    name = TOOL_NAME
    description = "成功返回固定文本的工具（R6 验证用）"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    is_concurrency_safe = True

    async def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(ok=True, content="工具结果", category="verify")


def _client(app):
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://qio.test", timeout=120.0
    )


def _events(app, kind: str, turn_id: str | None = None) -> list[dict]:
    out = []
    for event in list(app.state.ctx.bus._history):
        if event.type.value != kind:
            continue
        if turn_id is not None and str(event.data.get("turn_id")) != turn_id:
            continue
        out.append(event.data)
    return out


async def _wait_turn_end(app, turn_id: str, timeout: float = 60.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ends = _events(app, "TURN_END", turn_id)
        if ends:
            return ends[0]
        await asyncio.sleep(0.05)
    return None


@pytest.fixture()
def app(db_conn, settings):
    application = create_app(settings, db_conn)
    application.state.ctx.credentials._kr = MemoryKeyring()
    application.state.ctx.registry.register(_OkTool())
    return application


async def _run_one(app, scripts: list[StreamScript], message: str = "回答我") -> tuple[str, dict]:
    app.state.ctx.build_adapter = AsyncMock(return_value=FakeStreamAdapter(scripts))
    async with _client(app) as client:
        resp = await client.post("/api/turns", json={"message": message})
        assert resp.status_code == 200, resp.text[:200]
        turn_id = str(resp.json()["turn_id"])
        end = await _wait_turn_end(app, turn_id)
    assert end is not None, "TURN_END 没有到达"
    return turn_id, end


def _answer_events(app, turn_id: str) -> list[dict]:
    return [data for data in _events(app, "ASSISTANT", turn_id) if data.get("interim") is False]


async def test_declared_answer_identity_matches_final_answer_event(app):
    turn_id, end = await _run_one(app, [StreamScript(text_chunks=[DECL + "\n", "正式回答。"])])

    answer_id = end.get("answer_id")
    assert isinstance(answer_id, str) and answer_id.startswith("dl_"), (
        "TURN_END 必须带最小回答身份 answer_id（= 最终回答段的 delta_id）",
        answer_id,
    )
    assert end["final_content"] == "正式回答。", end["final_content"]

    # 身份在事件里稳定：承载最终正文的那条 ASSISTANT（interim=False）就是它
    answers = _answer_events(app, turn_id)
    assert answers, "必须有正式回答事件"
    carriers = [data for data in answers if data.get("delta_id") == answer_id]
    assert carriers, ("answer_id 必须指向真实存在的正式回答身份", [d.get("delta_id") for d in answers])
    assert carriers[-1]["content"] == end["final_content"], carriers[-1]
    assert all(data.get("interim") is False for data in carriers), carriers
    # 过程区（interim=True）不得被当成回答身份
    interim_ids = {
        data.get("delta_id")
        for data in _events(app, "ASSISTANT", turn_id)
        if data.get("interim") is True
    }
    assert answer_id not in interim_ids, ("answer_id 不得指向过程区说明", interim_ids)
    print("[诊断] R6 声明回答：answer_id=%s 事件数=%d" % (answer_id, len(carriers)))


async def test_tool_turn_identity_points_to_final_answer_segment(app):
    scripts = [
        StreamScript(tool_calls=[ScriptedToolCall(id="c_fb_d", name=TOOL_NAME, arguments={})]),
        StreamScript(),
        StreamScript(text=BODY),
    ]
    turn_id, end = await _run_one(app, scripts, message="调用一次工具再回答")

    answer_id = end.get("answer_id")
    assert isinstance(answer_id, str) and answer_id.startswith("dl_"), answer_id
    assert end["final_content"] == BODY, end["final_content"]

    answers = _answer_events(app, turn_id)
    carriers = [data for data in answers if data.get("delta_id") == answer_id]
    assert carriers and carriers[-1]["content"] == BODY, carriers
    # 工具轮的过程说明用的是另一个 delta_id（不能被误当成回答身份）
    interim = [data for data in _events(app, "ASSISTANT", turn_id) if data.get("interim") is True]
    assert all(data.get("delta_id") != answer_id for data in interim if data.get("content")), interim
    print("[诊断] R6 工具轮：answer_id=%s interim 身份=%s"
          % (answer_id, [d.get("delta_id") for d in interim]))


async def test_no_answer_segment_reports_null(app):
    turn_id, end = await _run_one(app, [StreamScript(text="")], message="什么都不说的模型")

    assert end.get("answer_id") is None, (
        "整轮没有可校准回答段时必须为 null（系统补的停止说明不是回答身份）",
        end.get("answer_id"),
    )
    assert str(end.get("final_content") or "").strip(), ("仍然必须有可读的结束说明", end)
    assert _answer_events(app, turn_id) == [], "空输出轮不得凭空造出正式回答事件"
    print("[诊断] R6 无回答段：answer_id=%r final_content 非空=%s"
          % (end.get("answer_id"), bool(str(end.get("final_content") or "").strip())))


async def test_turn_end_passes_answer_id_and_is_idempotent():
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    async def runner(ctx) -> None:  # noqa: ANN001
        ctx.final_content = "正式回答。"
        ctx.result = {
            "ok": True,
            "turn": {"final_answer_id": "dl_abcd1234_2", "stop_reason_code": "none"},
        }

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("回答我")
    await manager.wait(ctx.turn_id, timeout=5)
    await manager.shutdown()

    ends = [data for name, data in events if name == "TURN_END"]
    assert len(ends) == 1, ends
    assert ends[0]["answer_id"] == "dl_abcd1234_2", ends[0]

    # 重复收尾幂等：身份不变、不重复发事件
    await manager._emit_turn_end(ctx)
    await manager._emit_turn_end(ctx)
    ends_after = [data for name, data in events if name == "TURN_END"]
    assert len(ends_after) == 1, ends_after
    assert ends_after[0]["answer_id"] == "dl_abcd1234_2", ends_after[0]
    print("[诊断] R6 幂等：TURN_END 数=%d answer_id=%s" % (len(ends_after), ends_after[0]["answer_id"]))
