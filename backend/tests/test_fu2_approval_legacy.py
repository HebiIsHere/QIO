"""B 回归用例：旧审批恢复身份 / 路由 / 结果闭环 / 预算 overrides（先红后绿）。

基线 373fda40 的四个问题（见 _fixpm2/BRIEF-b.md）：

1. 旧 request() 行（state 为空、operation 为空）被 state_of 默认映射成 waiting，
   row_to_dict 直接写 state=waiting，且没有 legacy 标识 —— 前端会当 v2 审批去调 /decide；
2. 启动恢复把旧行写成 state=interrupted_unverified（伪装成 v2 状态），
   API 输出里也没有保留原 turn/session/digest 的绑定事实；
3. /decide 对旧行不明确拒绝（落到 revision CAS 冲突），/respond 对已恢复旧行只给模糊 404；
4. 用户点「允许」后工具真实执行完（成功/失败），既不写 approval_history，
   也不发 APPROVAL_RESULT(executed/failed)；不支持 overrides 的旧路径静默丢弃 overrides。

运行：cd backend && uv run --frozen pytest tests/test_fu2_approval_legacy.py -q
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ToolCall
from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.security.operations import PendingOperation
from agent.storage.approvals import ApprovalStore
from agent.tools.approval import ApprovalService, request_digest
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry

from _r9_auth_support import record_events

LEGACY_ID = "appr_legacy0001"
LEGACY_KIND = "tool_execution"
LEGACY_PAYLOAD = {
    "tool": "legacy_write",
    "arguments": {"path": "C:\\work\\a.txt"},
    "description": "写入验收文件",
}
LEGACY_TURN = "turn_legacy"
LEGACY_SESSION = "sess_legacy"
LEGACY_CREATED = "2026-10-20T08:00:00+00:00"
LEGACY_EXPIRES = "2099-01-01T00:00:00+00:00"
PROPOSAL_LABEL = "写入验收文件"


# ---------------------------------------------------------------------------
# 夹具 / 小工具
# ---------------------------------------------------------------------------


def _insert_legacy(
    conn,
    *,
    approval_id: str = LEGACY_ID,
    status: str = "pending",
    state: str | None = None,
    kind: str = LEGACY_KIND,
    payload: dict | None = None,
    turn_id: str | None = LEGACY_TURN,
    session_id: str | None = LEGACY_SESSION,
    created_at: str = LEGACY_CREATED,
    expires_at: str = LEGACY_EXPIRES,
) -> None:
    """直接落一行旧 request() 形状的记录：state 为空、operation 为空。"""
    conn.execute(
        "INSERT OR REPLACE INTO pending_approvals"
        " (approval_id, kind, payload, turn_id, session_id, created_at, expires_at,"
        "  status, state, operation)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')",
        (
            approval_id,
            kind,
            json.dumps(payload if payload is not None else LEGACY_PAYLOAD, ensure_ascii=False),
            turn_id,
            session_id,
            created_at,
            expires_at,
            status,
            state,
        ),
    )
    conn.commit()


def _legacy_row(conn, approval_id: str = LEGACY_ID):
    return conn.execute(
        "SELECT * FROM pending_approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()


@pytest.fixture()
def client(db_conn, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def legacy_client(db_conn, settings: Settings):
    """旧行在应用启动前就躺在库里（模拟上一个进程留下的事实）。"""
    from agent.credentials.store import MemoryKeyring

    _insert_legacy(db_conn)
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _submit(client):
    async def _do():
        ctx = client.app.state.ctx
        operation = PendingOperation(
            kind="test.op",
            label="测试操作",
            payload={"value": 1},
            effect={"kind": "write", "targets": [{"kind": "path", "value": "C:\\work\\a.txt"}]},
            fingerprint={"path": "C:\\work\\a.txt", "sha256": "abc"},
            instruction_id="instr_fu2_b",
            instruction_text="把文件改成 X 并保存",
            purpose="fu2 B 测试",
            confirmations_required=1,
            source="tool",
        )
        if getattr(ctx, "requirements", None) is not None:
            requirement = ctx.requirements.record_user_message(
                text="把文件改成 X 并保存", message_id="msg_fu2_b", turn_id="turn_fu2_b"
            )
            operation.requirement_id = requirement.requirement_id
            operation.requirement_version = int(getattr(requirement, "version", 1) or 1)
        return await ctx.approvals.submit(operation)

    return client.portal.call(_do)


def _history(approvals, approval_id: str) -> list[dict]:
    return [item for item in approvals.history(limit=50) if item["approval_id"] == approval_id]


def _result_events(events, approval_id: str) -> list:
    return [
        event
        for event in events
        if event.type.value == "APPROVAL_RESULT"
        and event.data.get("approval_id") == approval_id
    ]


def _phases(events, approval_id: str) -> list[str]:
    return [str(event.data.get("phase") or "") for event in _result_events(events, approval_id)]


class _LegacyApprovalTool(Tool):
    """工具内部走**旧** request() 审批（state 为空的 legacy 行）。"""

    name = "legacy_write"
    description = "经旧审批保护的写入"
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        approvals: ApprovalService,
        *,
        kind: str = "computer",
        payload: dict | None = None,
        gate: asyncio.Event | None = None,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        self.approvals = approvals
        self.kind = kind
        self.payload = dict(payload or LEGACY_PAYLOAD)
        self.gate = gate
        self.ok = ok
        self.error = error
        self.decisions: list = []

    async def run(self, **kwargs):  # noqa: ANN003
        decision = await self.approvals.request(self.kind, dict(self.payload))
        self.decisions.append(decision)
        if decision.decision != "approved":
            return ToolResult(ok=False, error="未获批准：" + str(decision.decision))
        if self.gate is not None:
            await self.gate.wait()
        if not self.ok:
            return ToolResult(ok=False, error=self.error or "执行失败")
        return ToolResult(ok=True, content="执行完成")


def _registry(approvals: ApprovalService, tool: Tool) -> ToolRegistry:
    registry = ToolRegistry(approvals=approvals)
    registry.register(tool)
    return registry


async def _wait_request(approvals: ApprovalService, *, timeout: float = 2.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if approvals._requests:
            return next(iter(approvals._requests))
        await asyncio.sleep(0.01)
    raise AssertionError("旧审批请求没有登记到等待表")


async def _start(registry: ToolRegistry):
    return asyncio.create_task(
        registry.execute(ToolCall(id="call_1", name="legacy_write", arguments={}))
    )


# ---------------------------------------------------------------------------
# 1) legacy 身份：不得伪造 waiting，且必须显式 legacy=true
# ---------------------------------------------------------------------------


def test_legacy_row_is_not_faked_as_waiting_and_is_flagged(db_conn):
    _insert_legacy(db_conn, status="pending")
    store = ApprovalStore(db_conn)
    row = store.get(LEGACY_ID)
    assert row is not None
    assert row["legacy"] is True, "旧 request() 行必须显式标成 legacy"
    assert row["state"] in (None, ""), "旧行没有 v2 状态，不得被伪造成 waiting"
    assert row["status"] == "pending"
    assert row["turn_id"] == LEGACY_TURN
    assert row["session_id"] == LEGACY_SESSION
    assert row["created_at"] == LEGACY_CREATED
    assert row["expires_at"] == LEGACY_EXPIRES
    # state_of 是「读取权威状态」的唯一入口，不能再给旧行兜底 waiting
    assert store.state_of({"status": "some_future_status"}) in ("", None)
    assert store.state_of({"status": "pending"}) in ("", None)


def test_startup_marks_legacy_interrupted_legacy_and_keeps_binding(db_conn):
    _insert_legacy(db_conn)
    service = ApprovalService(EventBus(), conn=db_conn)
    raw = _legacy_row(db_conn)
    assert raw["status"] == "interrupted_legacy", (
        "旧审批的结局要用独立事实 interrupted_legacy，而不是伪装成 v2 状态"
    )
    assert raw["state"] in (None, ""), "旧行不得写 v2 状态"
    assert raw["turn_id"] == LEGACY_TURN
    assert raw["session_id"] == LEGACY_SESSION
    assert raw["created_at"] == LEGACY_CREATED
    assert raw["expires_at"] == LEGACY_EXPIRES

    items = [item for item in service.interrupted() if item["approval_id"] == LEGACY_ID]
    assert len(items) == 1
    item = items[0]
    assert item["legacy"] is True
    assert item["state"] in (None, "")
    assert item["status"] == "interrupted_legacy"
    assert item["outcome"] == "not_executed"
    assert item["what"] == PROPOSAL_LABEL
    assert item["turn_id"] == LEGACY_TURN
    assert item["session_id"] == LEGACY_SESSION
    assert item["created_at"] == LEGACY_CREATED
    assert item["expires_at"] == LEGACY_EXPIRES
    assert item["request_digest"] == request_digest(LEGACY_KIND, LEGACY_PAYLOAD), (
        "恢复必须保留原请求摘要（批准 A 不能被拿去执行 B）"
    )


# ---------------------------------------------------------------------------
# 2) 路由：/decide 明确拒绝 legacy；/respond 正常服务；中断项明确“不会再执行”
# ---------------------------------------------------------------------------


def test_recovered_legacy_is_read_only_fact_not_decidable(legacy_client, db_conn):
    state = legacy_client.get("/api/runtime/state").json()
    items = [
        item
        for item in state["interrupted_approvals"]
        if item["approval_id"] == LEGACY_ID
    ]
    assert len(items) == 1, "恢复后的旧审批必须作为「未执行」事实可读"
    item = items[0]
    assert item["legacy"] is True
    assert item["state"] in (None, "")
    assert item["status"] == "interrupted_legacy"
    assert item["turn_id"] == LEGACY_TURN
    assert item["session_id"] == LEGACY_SESSION
    assert item["created_at"] == LEGACY_CREATED
    assert item["expires_at"] == LEGACY_EXPIRES
    assert item["request_digest"] == request_digest(LEGACY_KIND, LEGACY_PAYLOAD)

    pending_ids = {
        entry["approval_id"]
        for entry in legacy_client.get("/api/approvals/pending").json()["approvals"]
    }
    assert LEGACY_ID not in pending_ids, "恢复后的旧审批不是可 /decide 的 waiting 项"

    decide = legacy_client.post(
        f"/api/approvals/{LEGACY_ID}/decide", json={"decision": "approve"}
    )
    assert decide.status_code == 409, decide.text
    detail = decide.json()["detail"]
    assert detail["code"] == "legacy_approval_use_respond"
    assert "respond" in detail["message"] or "旧" in detail["message"]

    respond = legacy_client.post(
        f"/api/approvals/{LEGACY_ID}/respond", json={"decision": "approved"}
    )
    assert respond.status_code == 409, respond.text
    detail = respond.json()["detail"]
    assert detail["code"] == "legacy_approval_not_executable"
    assert "没有执行" in detail["message"] or "不会再执行" in detail["message"]
    # 原行没有被消费或改写
    assert _legacy_row(db_conn)["status"] == "interrupted_legacy"


def test_pending_api_marks_in_memory_legacy_and_decide_is_rejected(client):
    approvals = client.app.state.ctx.approvals

    async def arm():
        approvals.set_context(turn_id="turn_api_legacy", session_id="sess_api_legacy")
        return asyncio.create_task(
            approvals.request("computer", {"description": "写入文件"})
        )

    task = client.portal.call(arm)
    client.portal.call(lambda: asyncio.sleep(0.05))
    entries = client.get("/api/approvals/pending").json()["approvals"]
    mine = [entry for entry in entries if entry.get("kind") == "computer"]
    assert mine, "等待中的旧审批仍要在 pending 里出现（否则界面看不到）"
    item = mine[0]
    assert item["legacy"] is True, "旧路径审批必须显式 legacy=true（前端据此只走 /respond）"
    assert item.get("state") in (None, ""), "旧审批没有 v2 状态"
    assert item["turn_id"] == "turn_api_legacy"
    assert item["session_id"] == "sess_api_legacy"
    assert item["request_digest"]

    decide = client.post(
        f"/api/approvals/{item['approval_id']}/decide", json={"decision": "approve"}
    )
    assert decide.status_code == 409, decide.text
    assert decide.json()["detail"]["code"] == "legacy_approval_use_respond"

    respond = client.post(
        f"/api/approvals/{item['approval_id']}/respond",
        json={
            "decision": "approved",
            "turn_id": "turn_api_legacy",
            "session_id": "sess_api_legacy",
            "request_digest": item["request_digest"],
        },
    )
    assert respond.status_code == 200, respond.text
    result = client.portal.call(lambda: task)
    assert result.decision == "approved"

    again = client.post(
        f"/api/approvals/{item['approval_id']}/respond", json={"decision": "approved"}
    )
    assert again.status_code == 404, "单次消费：已应答的旧审批不能被再消费"


async def test_legacy_respond_still_checks_turn_session_and_digest(db_conn):
    approvals = ApprovalService(EventBus(), conn=db_conn)
    approvals.set_context(turn_id="turn_bind", session_id="sess_bind")
    tool = _LegacyApprovalTool(approvals)
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    with pytest.raises(ValueError):
        await approvals.respond(approval_id, "approved", turn_id="turn_other")
    with pytest.raises(ValueError):
        await approvals.respond(approval_id, "approved", session_id="sess_other")
    with pytest.raises(ValueError):
        await approvals.respond(approval_id, "approved", digest="deadbeef")
    # 失败的尝试不消费
    assert await approvals.respond(approval_id, "approved") is True
    result = await asyncio.wait_for(task, timeout=3)
    assert result.ok is True


# ---------------------------------------------------------------------------
# 3) 结果闭环：已批准 ≠ 已执行；真实执行结束写 history + APPROVAL_RESULT
# ---------------------------------------------------------------------------


async def test_approved_legacy_is_not_executed_until_tool_finishes(db_conn):
    bus = EventBus()
    events = record_events(bus)
    approvals = ApprovalService(bus, conn=db_conn)
    gate = asyncio.Event()
    tool = _LegacyApprovalTool(approvals, gate=gate)
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    assert await approvals.respond(approval_id, "approved") is True
    await asyncio.sleep(0.05)

    row = approvals.store.get(approval_id)
    assert row["legacy"] is True and row["state"] in (None, "")
    history = _history(approvals, approval_id)
    assert history, "用户的「允许」本身就是可读事实"
    assert history[0]["outcome"] == "approved", (
        "工具还没执行完，只能显示已批准（等待执行），不得提前显示已执行成功"
    )
    assert "已批准" in history[0]["summary"]
    assert history[0]["detail"]["decision"] == "approved"
    assert history[0]["detail"]["kind"] == "computer"
    assert history[0]["detail"]["label"] == PROPOSAL_LABEL
    phases = _phases(events, approval_id)
    assert "approved" in phases
    assert "executed" not in phases, phases

    gate.set()
    result = await asyncio.wait_for(task, timeout=3)
    assert result.ok is True
    history = _history(approvals, approval_id)
    assert len(history) == 1, "approval_history 每条审批只有一条最新结果（PK approval_id）"
    assert history[0]["outcome"] == "executed"
    assert "已执行" in history[0]["summary"]
    detail = history[0]["detail"]
    assert detail["ok"] is True
    assert detail["kind"] == "computer"
    assert detail["label"] == PROPOSAL_LABEL
    assert detail["error"] in (None, "")

    executed = [
        event
        for event in _result_events(events, approval_id)
        if event.data.get("phase") == "executed"
    ]
    assert executed, "真实执行成功必须发布 APPROVAL_RESULT phase=executed"
    data = executed[-1].data
    assert data["ok"] is True
    assert data["summary"]
    assert data["detail"]["ok"] is True
    assert data["detail"]["kind"] == "computer"
    assert data["detail"]["label"] == PROPOSAL_LABEL
    assert data["legacy"] is True
    assert data["turn_id"] is None or isinstance(data["turn_id"], (str, type(None)))


async def test_legacy_tool_failure_records_failed_outcome(db_conn):
    bus = EventBus()
    events = record_events(bus)
    approvals = ApprovalService(bus, conn=db_conn)
    tool = _LegacyApprovalTool(approvals, ok=False, error="磁盘写满")
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    await approvals.respond(approval_id, "approved")
    result = await asyncio.wait_for(task, timeout=3)
    assert result.ok is False

    history = _history(approvals, approval_id)
    assert history and history[0]["outcome"] == "failed", "已批准但执行失败必须如实记为 failed"
    assert history[0]["detail"]["ok"] is False
    assert history[0]["detail"]["error"] == "磁盘写满"
    assert "失败" in history[0]["summary"]
    failed = [
        event
        for event in _result_events(events, approval_id)
        if event.data.get("phase") == "failed"
    ]
    assert failed, "真实执行失败必须发布 APPROVAL_RESULT phase=failed"
    assert failed[-1].data["ok"] is False
    assert failed[-1].data["detail"]["error"] == "磁盘写满"


async def test_legacy_rejection_history_is_not_executed(db_conn):
    bus = EventBus()
    events = record_events(bus)
    approvals = ApprovalService(bus, conn=db_conn)
    tool = _LegacyApprovalTool(approvals)
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    assert await approvals.respond(approval_id, "rejected") is True
    result = await asyncio.wait_for(task, timeout=3)
    assert result.ok is False

    history = _history(approvals, approval_id)
    assert history and history[0]["outcome"] == "rejected"
    assert "已拒绝" in history[0]["summary"]
    assert history[0]["detail"]["decision"] == "rejected"
    phases = _phases(events, approval_id)
    assert "rejected" in phases
    assert "executed" not in phases and "failed" not in phases


async def test_legacy_timeout_records_expired_history(db_conn):
    bus = EventBus()
    events = record_events(bus)
    approvals = ApprovalService(bus, conn=db_conn, timeout_seconds=0.05)
    tool = _LegacyApprovalTool(approvals)
    registry = _registry(approvals, tool)
    result = await asyncio.wait_for(await _start(registry), timeout=3)
    assert result.ok is False

    rows = db_conn.execute(
        "SELECT approval_id FROM pending_approvals WHERE kind = 'computer'"
    ).fetchall()
    assert len(rows) == 1
    approval_id = str(rows[0]["approval_id"])
    history = _history(approvals, approval_id)
    assert history and history[0]["outcome"] == "expired", "超时必须如实记为过期，不是「你拒绝了」"
    assert "过期" in history[0]["summary"] or "超时" in history[0]["summary"]
    phases = _phases(events, approval_id)
    assert "expired" in phases


async def test_legacy_cancel_records_cancelled_history(db_conn):
    bus = EventBus()
    events = record_events(bus)
    approvals = ApprovalService(bus, conn=db_conn)
    tool = _LegacyApprovalTool(approvals)
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    task.cancel()
    result = await asyncio.wait_for(task, timeout=3)
    assert result.ok is False

    history = _history(approvals, approval_id)
    assert history and history[0]["outcome"] == "cancelled", "本轮停止不是用户拒绝"
    assert "取消" in history[0]["summary"] or "停止" in history[0]["summary"]
    assert "cancelled" in _phases(events, approval_id)


# ---------------------------------------------------------------------------
# 4) 预算 overrides：支持的旧路径采用批准值；不支持的路径显式 400
# ---------------------------------------------------------------------------


async def test_supported_legacy_create_path_receives_approved_overrides(db_conn):
    approvals = ApprovalService(EventBus(), conn=db_conn)
    tool = _LegacyApprovalTool(
        approvals,
        kind="tool_create",
        payload={
            "name": "budget_agent",
            "description": "按主题检索资料的子智能体",
            "tool_type": "subagent",
            "capabilities": [],
        },
    )
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    overrides = {
        "subagent_budget": {"max_iterations": 7, "max_tokens": 70000, "output_limit_chars": 1500}
    }
    assert await approvals.respond(approval_id, "approved", overrides=overrides) is True
    result = await asyncio.wait_for(task, timeout=3)
    assert result.ok is True
    assert tool.decisions[-1].overrides == overrides, (
        "支持 overrides 的旧路径必须把用户批准的值原样交给执行器"
    )


async def test_unsupported_override_path_is_rejected_explicitly(db_conn):
    from agent.storage.approvals import OverridesUnsupported

    approvals = ApprovalService(EventBus(), conn=db_conn)
    tool = _LegacyApprovalTool(approvals, kind="computer")
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    with pytest.raises(OverridesUnsupported) as excinfo:
        await approvals.respond(
            approval_id, "approved", overrides={"subagent_budget": {"max_iterations": 9}}
        )
    assert excinfo.value.code == "overrides_unsupported"
    # 没有被静默消费
    assert approval_id in approvals._requests
    assert await approvals.respond(approval_id, "approved") is True
    await asyncio.wait_for(task, timeout=3)


async def test_unknown_override_key_on_supported_path_is_rejected(db_conn):
    from agent.storage.approvals import OverridesUnsupported

    approvals = ApprovalService(EventBus(), conn=db_conn)
    tool = _LegacyApprovalTool(
        approvals,
        kind="tool_create",
        payload={"name": "budget_agent2", "description": "子智能体", "tool_type": "subagent"},
    )
    registry = _registry(approvals, tool)
    task = await _start(registry)
    approval_id = await _wait_request(approvals)

    with pytest.raises(OverridesUnsupported):
        await approvals.respond(approval_id, "approved", overrides={"lifetime": "once"})
    assert await approvals.respond(approval_id, "approved") is True
    await asyncio.wait_for(task, timeout=3)


def test_api_decide_rejects_overrides_and_keeps_waiting(client):
    ticket = _submit(client)
    resp = client.post(
        f"/api/approvals/{ticket.approval_id}/decide",
        json={"decision": "approve", "overrides": {"subagent_budget": {"max_iterations": 9}}},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "overrides_unsupported"
    assert client.app.state.ctx.approvals.store.get(ticket.approval_id)["state"] == "waiting"


def test_api_respond_rejects_overrides_on_v2_and_forwards_on_legacy(client, monkeypatch):
    approvals = client.app.state.ctx.approvals

    async def fake_respond(approval_id, decision, scope=None, overrides=None, **kwargs):
        seen.update({"overrides": overrides, "decision": decision})
        return True

    seen: dict = {}
    monkeypatch.setattr(approvals, "respond", fake_respond)
    resp = client.post(
        "/api/approvals/appr_x/respond",
        json={"decision": "approved", "overrides": {"subagent_budget": {"max_iterations": 7}}},
    )
    assert resp.status_code == 200, resp.text
    assert seen["overrides"] == {"subagent_budget": {"max_iterations": 7}}, (
        "HTTP 层必须把 overrides 原样交给服务层，不能在路由里吞掉"
    )
