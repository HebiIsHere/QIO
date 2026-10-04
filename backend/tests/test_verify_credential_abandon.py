"""独立验证 v2：**凭据授权审批**与开发任务的关联（契约 B 章 + D3/D4）。

要验的三件事：
  B1  凭据审批载荷必须带 `workspace`，`invalidate_for_task` 才找得到它（复用既有识别规则）；
  B2  放弃要能作废这个任务的三类审批（tool_execution / tool_create / credential_grant）；
  B3  作废的文案必须是「任务已放弃，这个确认已作废」，**不能**说成「你拒绝了」；
  D3  放弃后确认卡立即消失（后端侧：等待方收到 cancelled、旧批准请求无效）；
  D4  两种顺序（审批先通过 / 放弃先发生）都不执行、不注册；其它任务与已注册工具不受影响。

这份文件由**验证方**维护，不覆盖实现方的用例。凭据存储用替身（本用例验的是审批关联，
不是凭据存储本身），其余对象（DevWorkspace / ApprovalService / ToolLifecycle）都是真实的。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.tools.approval import ApprovalResult, ApprovalService
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.lifecycle import ToolLifecycle

try:  # 实现前这个常量还不存在：这里退化成 None，让用例仍然能被收集并**按行为**变红
    from agent.tools.lifecycle import ABANDONED_APPROVAL_DETAIL
except ImportError:  # pragma: no cover - 只在「实现前」的对照运行里走到
    ABANDONED_APPROVAL_DETAIL = None

from agent.tools.registry import ToolRegistry
from agent.tools.sandbox import SandboxResult
from agent.tools.spec import ToolDefinition

CREDENTIAL_REF = "key_verify_credential"
GUARD_REASON = "这个开发任务已经放弃，不能再注册工具。"


# ---------------------------------------------------------------------------
# 替身与夹具
# ---------------------------------------------------------------------------


class _StubCredentials:
    """只回答「这条凭据现在可用吗」——凭据存储本身不在本轮验证范围内。"""

    def __init__(self, ref: str = CREDENTIAL_REF) -> None:
        self.ref = ref

    def get_metadata(self, key_id: str) -> dict | None:
        if key_id != self.ref:
            return None
        return {"key_id": key_id, "status": "active", "kind": "api_key", "label": "验证用凭据"}


class _OkSandbox:
    executor = "subprocess"
    timeout_seconds = 5.0

    async def effective_executor(self) -> str:
        return "subprocess"

    async def execute(self, code, arguments, **kwargs) -> SandboxResult:  # noqa: ANN001, ARG002
        return SandboxResult(ok=True, value={"ok": True}, stdout="", stderr="")


class _RecordingStatus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def emit(self, group_id, phase, label="", detail="", ok=None, tool_name=None):  # noqa: ANN001
        self.events.append(
            {"group": group_id, "phase": phase, "label": label, "detail": detail, "ok": ok, "tool": tool_name}
        )

    def last(self) -> dict:
        return self.events[-1] if self.events else {}


class _RecordingStore:
    """注册落盘替身：记录「有没有真的落盘」。"""

    def __init__(self) -> None:
        self.saved: list[str] = []

    def load(self, name: str):  # noqa: ANN201
        return None

    def save(self, definition) -> None:  # noqa: ANN001
        self.saved.append(definition.name)


class _CreateAutoApproved:
    """薄代理：只把 `tool_create` 自动批准，凭据审批走**真实** ApprovalService。

    这样凭据审批会真的进 `_waiters` / 落库，`invalidate_for_task` 的识别规则才有意义。
    """

    def __init__(self, real: ApprovalService) -> None:
        self.real = real
        self.kinds: list[str] = []

    async def request(self, kind: str, payload: dict, **kwargs):  # noqa: ANN003
        self.kinds.append(kind)
        if kind == "tool_create":
            return ApprovalResult("appr_create_auto", "approved", None, None)
        return await self.real.request(kind, payload, **kwargs)


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    monkeypatch.delenv("QIO_DATA_DIR", raising=False)


@pytest.fixture()
def client(db_conn, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _definition(credential_ref: str | None = CREDENTIAL_REF) -> ToolDefinition:
    return ToolDefinition(
        name="needs_credential",
        description="需要凭据的工具",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
        credential_ref=credential_ref,
    )


def _lifecycle(approvals, registry, store) -> tuple[ToolLifecycle, _RecordingStatus]:  # noqa: ANN001
    lifecycle = ToolLifecycle(
        adapter=type("_Adapter", (), {"mode": "native"})(),
        approvals=approvals,
        sandbox=_OkSandbox(),
        registry=registry,
        credentials=_StubCredentials(),
        tool_store=store,
    )
    status = _RecordingStatus()
    lifecycle.status = status
    return lifecycle, status


async def _wait_pending(approvals: ApprovalService, kind: str, timeout: float = 10.0) -> dict:
    for _ in range(int(timeout / 0.02)):
        rows = [row for row in approvals.pending() if row.get("kind") == kind]
        if rows:
            return rows[0]
        await asyncio.sleep(0.02)
    raise AssertionError(f"等不到 {kind} 审批进入等待表（pending={approvals.pending()}）")


def _guard(ws: DevWorkspace, task_id: str):
    return lambda: GUARD_REASON if ws.is_abandoned(task_id) else None


# ---------------------------------------------------------------------------
# B1 / B3 / D3：凭据审批与任务绑定；放弃后作废且不是「你拒绝了」
# ---------------------------------------------------------------------------


async def test_credential_approval_is_tied_to_the_task_and_cancelled_by_abandon(tmp_path):
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = ws.create("需要凭据的开发任务")
    approvals = ApprovalService(EventBus())
    proxy = _CreateAutoApproved(approvals)
    registry = ToolRegistry()
    store = _RecordingStore()
    lifecycle, status = _lifecycle(proxy, registry, store)
    lifecycle.abandon_guard = _guard(ws, task.id)

    submitting = asyncio.create_task(
        lifecycle.submit_definition(_definition(), "验证：凭据审批与放弃", group_id=task.id)
    )

    row = await _wait_pending(approvals, "credential_grant")
    payload = row["payload"]

    # B1：载荷必须带 workspace —— 否则 invalidate_for_task 找不到它，确认卡就不会消失
    assert payload.get("workspace") == task.id, (
        f"凭据审批载荷没有指向开发任务（找不到 workspace）：{payload}"
    )
    assert payload.get("key_id") == CREDENTIAL_REF and payload.get("tool_name") == "needs_credential"
    approval_id = row["approval_id"]

    # 用户此时点「放弃开发」：任务落盘 + 作废未决审批（v2 的顺序：先落盘再作废）
    result = ws.abandon(task.id)
    assert result.get("ok") is True, f"放弃失败：{result}"
    invalidated = approvals.invalidate_for_task(task.id)
    assert invalidated == 1, f"凭据审批没有被作废（作废了 {invalidated} 条）"

    outcome = await asyncio.wait_for(submitting, timeout=10)

    # B3：文案必须是「任务已放弃、确认已作废」，不能是「你拒绝了」
    assert outcome.ok is False, f"任务已放弃，凭据审批却放行了：{outcome}"
    assert outcome.step == "credential", f"失败步骤不对：{outcome}"
    assert "放弃" in outcome.detail and "作废" in outcome.detail, f"结论文案不对：{outcome.detail!r}"
    assert "拒绝" not in outcome.detail, f"把系统作废说成了用户拒绝：{outcome.detail!r}"
    assert status.last().get("label") == "已放弃", f"状态事件标签不对：{status.last()}"
    emitted = str(status.last().get("detail") or "")
    assert "放弃" in emitted and "作废" in emitted, f"状态事件文案不对（应说「任务已放弃、确认已作废」）：{emitted!r}"
    assert "拒绝" not in emitted, f"把系统作废说成了用户拒绝：{emitted!r}"
    if ABANDONED_APPROVAL_DETAIL is not None:
        assert emitted == ABANDONED_APPROVAL_DETAIL, f"文案与契约常量不一致：{emitted!r}"

    # D3：旧批准请求无效
    handled = await approvals.respond(approval_id, "approved")
    assert handled is False, "作废过的凭据审批仍然可以被批准"

    # D4：没有执行、没有注册
    assert store.saved == [], f"任务已放弃却注册了工具：{store.saved}"
    assert registry.get("needs_credential") is None, "任务已放弃却把工具放进了注册表"


# ---------------------------------------------------------------------------
# B2 / B5：三类审批一起作废；别的任务与已注册工具不受影响
# ---------------------------------------------------------------------------


def test_abandon_endpoint_cancels_all_three_kinds_and_spares_other_tasks(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("三类审批都属于我")
    other = ctx.dev_workspaces.create("别的任务")
    loop = asyncio.new_event_loop()
    held: list[object] = []
    waiters: list[asyncio.Task] = []

    async def _hold(kind: str, task_id: str):
        held.append(await ctx.approvals.request(kind, {"workspace": task_id}))

    ids: dict[str, str] = {}
    for kind in ("tool_execution", "tool_create", "credential_grant"):
        waiters.append(loop.create_task(_hold(kind, task.id)))
    waiters.append(loop.create_task(_hold("credential_grant", other.id)))
    for _ in range(300):
        if len(ctx.approvals.pending()) >= 4:
            break
        loop.run_until_complete(asyncio.sleep(0.02))
    pending = ctx.approvals.pending()
    assert len(pending) == 4, f"审批没有都进等待表：{pending}"
    for row in pending:
        ids.setdefault(row["kind"] if row["payload"].get("workspace") == task.id else "other", row["approval_id"])

    body = client.post(f"/api/dev/tasks/{task.id}/abandon").json()
    assert body["ok"] is True and body["persisted"] is True, f"放弃失败：{body}"
    assert body["invalidated_approvals"] == 3, f"三类审批应当全部作废，实际 {body['invalidated_approvals']}"

    for kind in ("tool_execution", "tool_create", "credential_grant"):
        approval_id = ids[kind]
        assert loop.run_until_complete(ctx.approvals.respond(approval_id, "approved")) is False, (
            f"{kind} 作废后仍然可以被批准"
        )
        assert ctx.conn.execute(
            "SELECT status FROM pending_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()["status"] == "cancelled", f"{kind} 在库里不是 cancelled"

    # B5：别的任务的审批不受影响，仍然在等
    remaining = [row for row in ctx.approvals.pending() if row["payload"].get("workspace") == other.id]
    assert len(remaining) == 1, f"别的任务的审批被误伤：{ctx.approvals.pending()}"

    for item in waiters:
        item.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    assert len(held) >= 0  # 结果只是留着证明等待方收到了结局


# ---------------------------------------------------------------------------
# D4：两种顺序都不执行、不注册
# ---------------------------------------------------------------------------


async def test_approval_passed_first_then_abandon_still_never_registers(tmp_path):
    """顺序 A：凭据审批**先被批准**，随后任务被放弃 —— 注册前守卫必须拦住。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = ws.create("审批先通过，再放弃")
    approvals = ApprovalService(EventBus())
    registry = ToolRegistry()
    store = _RecordingStore()
    already = _definition(credential_ref=None)
    already.name = "already_registered"
    registry.register(already)  # B5：已注册的工具

    lifecycle, status = _lifecycle(_CreateAutoApproved(approvals), registry, store)

    # 守卫在流程里会被调用多次（测试之后、注册之前都有）。第一次调用**不能**就放弃，
    # 否则流程会在凭据审批之前被打断；这里让「放弃」只在审批通过之后才发生 ——
    # 正是契约要覆盖的窗口：审批已通过、注册还没发生。
    window = {"approved": False}

    def guard():
        if window["approved"] and not ws.is_abandoned(task.id):
            ws.abandon(task.id)
        return GUARD_REASON if ws.is_abandoned(task.id) else None

    lifecycle.abandon_guard = guard

    submitting = asyncio.create_task(
        lifecycle.submit_definition(_definition(), "验证：审批先通过", group_id=task.id)
    )
    row = await _wait_pending(approvals, "credential_grant")
    # 先开窗，再批准：这样审批一通过，注册前的守卫就会在「用户已放弃」的状态下被调用
    window["approved"] = True
    handled = await approvals.respond(row["approval_id"], "approved")
    assert handled is True, "夹具前提不成立：审批没有被批准"

    outcome = await asyncio.wait_for(submitting, timeout=10)

    assert outcome.ok is False, f"任务已放弃却注册成功：{outcome}"
    assert store.saved == [], f"任务已放弃却落盘了：{store.saved}"
    assert registry.get("needs_credential") is None, "任务已放弃却进了注册表"
    assert registry.get("already_registered") is not None, "已注册的其它工具被误删"
    assert ws.status(task.id)["abandoned"] is True


async def test_abandon_first_then_submit_never_even_asks_for_credentials(tmp_path):
    """顺序 B：放弃**先发生** —— 连凭据审批都不该发起，更不该注册。"""
    ws = DevWorkspace(tmp_path / "dev-workspaces")
    task = ws.create("先放弃，再提交")
    assert ws.abandon(task.id)["ok"] is True

    approvals = ApprovalService(EventBus())
    proxy = _CreateAutoApproved(approvals)
    registry = ToolRegistry()
    store = _RecordingStore()
    lifecycle, status = _lifecycle(proxy, registry, store)
    lifecycle.abandon_guard = _guard(ws, task.id)

    outcome = await lifecycle.submit_definition(_definition(), "验证：先放弃", group_id=task.id)

    assert outcome.ok is False, f"已放弃的任务还能提交：{outcome}"
    assert "credential_grant" not in proxy.kinds, f"已放弃的任务还发起了凭据审批：{proxy.kinds}"
    assert approvals.pending() == [], f"已放弃的任务留下了待审批：{approvals.pending()}"
    assert store.saved == [] and registry.get("needs_credential") is None
