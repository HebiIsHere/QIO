"""审批的 turn / 会话身份必须真的被绑定，而不是留一个永远为空的字段。

背景（Agent G 的真机验收发现）：`pending_approvals.session_id` 全程为 NULL ——
`ApprovalService.set_context` 支持 session_id，但生产路径的两个调用点（turn 运行时、
通知轮）都只传了 turn_id。于是 `respond()` 里那条会话比对**一次也没有生效过**：
一个看起来在校验会话、实际永远不会触发的检查。

这里用真实 turn 路径 + 真实落库证明它现在真的有值。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    from agent.credentials.store import MemoryKeyring

    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


@pytest.fixture()
def client(db_conn, settings):
    """真实 FastAPI 应用：验证生产装配链路，而不是各部件单独自证。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


class _EchoTool(Tool):
    name = "f_echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=f"echo:{kwargs.get('text', '')}")


class _AnswerOnly:
    mode = "native"
    model = "fake"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="好的"))


class _ToolThenAnswer:
    """永远先要一次工具调用：好让迭代预算耗尽，走到「继续/停止」审批。"""

    mode = "native"
    model = "fake"

    async def complete(self, messages, tools, **kwargs):
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="c1", name="f_echo", arguments={"text": "hi"})],
            ),
            usage={"prompt_tokens": 10, "completion_tokens": 4},
        )


class _RecordingApproval:
    """只记录 turn 运行时到底把什么上下文交给了审批。"""

    def __init__(self) -> None:
        self.contexts: list[dict] = []
        self.requests: list[tuple[str, dict]] = []
        self.decision = "rejected"

    def set_context(self, **kwargs) -> None:
        self.contexts.append(kwargs)

    def pending(self) -> list:
        return []

    def interrupted(self) -> list:
        return []

    async def request(self, kind, payload, **kwargs):
        self.requests.append((kind, payload))
        return SimpleNamespace(decision=self.decision)


async def test_turn_binds_approvals_to_a_real_session_identity(ctx, monkeypatch):
    """生产路径必须把会话身份传进审批上下文（以前只传 turn_id）。"""
    assert ctx.session_id, "AppContext 必须给出会话身份"

    topic = ctx.topics.nodes.create_topic("会话绑定").id
    recorder = _RecordingApproval()
    ctx.approvals = recorder
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_AnswerOnly()))
    await ctx.run_turn("你好", topic_id=topic)

    bound = [c for c in recorder.contexts if c.get("session_id")]
    assert bound, f"没有任何一次 set_context 带上 session_id：{recorder.contexts}"
    assert all(c["session_id"] == ctx.session_id for c in bound), recorder.contexts
    # turn 结束时上下文被清掉，但会话身份仍在（它是进程级的，不是这一轮的）
    assert recorder.contexts[-1]["turn_id"] is None
    assert recorder.contexts[-1]["session_id"] == ctx.session_id


def test_pending_approval_row_is_written_with_the_session(client):
    """落库证明：绑定字段真的进 pending_approvals，而不是只出现在事件里。"""
    app_ctx = client.app.state.ctx
    approvals = app_ctx.approvals
    session_id = app_ctx.session_id
    assert session_id

    async def arm():
        approvals.set_context(turn_id="turn_sess", session_id=session_id)
        task = asyncio.create_task(approvals.request("computer", {"action": "read"}))
        await asyncio.sleep(0.05)
        return task

    task = client.portal.call(arm)
    row = app_ctx.conn.execute(
        "SELECT turn_id, session_id FROM pending_approvals ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None, "等待中的审批必须落库"
    assert row["turn_id"] == "turn_sess"
    assert row["session_id"] == session_id, "会话身份以前永远是 NULL"

    approval_id = next(iter(approvals._requests))
    assert client.portal.call(lambda: approvals.respond(approval_id, "approved")) is True
    client.portal.call(lambda: asyncio.wait_for(task, timeout=2))


async def test_budget_continue_payload_carries_the_real_reason(ctx, monkeypatch):
    """预算耗尽的「继续/停止」必须说清是哪种预算。

    以前 payload 只有 used/max，界面只能说「已达迭代上限 X/Y」——
    token 预算耗尽时那是一句与事实不符的话，而用户正是拿它决定继续还是停止。
    """
    topic = ctx.topics.nodes.create_topic("预算暂停").id
    ctx.registry.register(_EchoTool())
    ctx.settings_store.set("loop.max_iterations", "1")
    recorder = _RecordingApproval()
    ctx.approvals = recorder
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_ToolThenAnswer()))
    await ctx.run_turn("请调用工具", topic_id=topic)

    kinds = [k for k, _ in recorder.requests]
    assert "continue" in kinds, recorder.requests
    _, payload = recorder.requests[kinds.index("continue")]
    assert payload.get("reason") == "budget", payload
    assert payload.get("budget_kind") in {"iterations", "tokens"}, payload
    assert payload.get("message"), "必须给出人话说明，界面不猜"


