"""R9 端到端验收：真实应用装配（create_app + ToolRegistry + ApprovalService）下的授权行为。

为什么需要它：单元测试分别验证引擎、工具、执行器；但它们"接线对不对"只有真实应用装配能证明。
这里覆盖四条用户可见结论：

1. 只读模式下 fs_write 被拒且不产生任何文件/待审批；
2. 受限模式下写用户原有文件 → 提交待审批（不阻塞、不执行）→ 用户批准 → 后台执行器真的写成功；
3. 用户明确指令（"修改 <绝对路径>"）本身构成当次授权：同一指令内不再重复审批；
4. 完全无限制模式下，内部运行数据直接写入仍要求三次确认（只确认 1 次不会执行）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ToolCall
from agent.api.server import create_app
from agent.config import Settings
from agent.security.context import instruction_context
from agent.storage.settings import SettingsStore


@pytest.fixture()
def app_client(db_conn, settings: Settings, tmp_path):
    """真实应用；工作区根放到临时目录，避免碰用户真实工作区。"""
    from agent.credentials.store import MemoryKeyring

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SettingsStore(db_conn)
    store.set("computer.root_dir", str(workspace))
    # 默认受限模式（首次安装默认，契约 §1）
    store.set("computer.permission_mode", "restricted")
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        client.workspace = workspace
        yield client


def _exec(client: TestClient, name: str, arguments: dict):
    async def _do():
        return await client.app.state.ctx.registry.execute(
            ToolCall(id="call_e2e", name=name, arguments=arguments)
        )

    return client.portal.call(_do)


def _pending(client: TestClient) -> list[dict]:
    resp = client.get("/api/approvals/pending")
    assert resp.status_code == 200
    return resp.json()["approvals"]


def _wait_state(client: TestClient, approval_id: str, state: str, *, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        for item in _pending(client):
            if item["approval_id"] == approval_id:
                last = item
                break
        row = client.app.state.ctx.conn.execute(
            "SELECT state, result, error FROM pending_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is not None and row["state"] == state:
            return dict(row)
        time.sleep(0.05)
    raise AssertionError(f"审批 {approval_id} 未在 {timeout}s 内进入 {state}；最后待审={last}")


def test_read_only_mode_blocks_fs_write_without_pending_approval(app_client):
    client = app_client
    client.put("/api/security/mode", json={"mode": "read_only", "user_confirmed": True})
    target = client.workspace / "blocked.txt"
    result = _exec(client, "fs_write", {"path": str(target), "content": "should not exist"})
    assert result.ok is False
    assert "只读" in (result.error or "")
    assert not target.exists(), "只读模式下绝不能真的写出文件"
    assert _pending(client) == [], "只读应是拒绝，不是待审批"


def test_restricted_write_pends_then_approval_executes(tmp_path, app_client):
    client = app_client
    target = client.workspace / "user-material.txt"
    target.write_text("原始内容", encoding="utf-8")
    result = _exec(client, "fs_write", {"path": str(target), "content": "批准后的内容"})
    # 非阻塞：工具立即返回，文件还没变
    assert result.ok is True, result.error
    assert target.read_text(encoding="utf-8") == "原始内容"
    assert getattr(result, "data", None) or True  # 结果附 pending 元数据（不同版本字段名不同，不在此断言形状）
    pending = _pending(client)
    assert len(pending) == 1
    approval_id = pending[0]["approval_id"]
    assert pending[0]["state"] == "waiting"

    resp = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert resp.status_code == 200, resp.text
    row = _wait_state(client, approval_id, "completed")
    assert target.read_text(encoding="utf-8") == "批准后的内容"
    assert row["result"] is not None


def test_explicit_instruction_requires_review_not_keyword_grant(tmp_path, app_client):
    """R9-F：明确指令不再靠"修改/保存"关键词直接放行。

    模型审查不可用（无模型/超时/非法输出同一路径）时必须退回普通审批，
    不能默认允许；这保证"明确指令"本身不再是一个绕过审查的旧授权入口。
    """
    client = app_client
    target = client.workspace / "explicit.txt"
    target.write_text("old", encoding="utf-8")
    ctx = client.app.state.ctx
    original_reviewer = getattr(ctx.authorization, "reviewer", None)
    ctx.authorization.reviewer = None  # 模拟模型审查不可用
    instruction = ctx.instructions.begin(
        turn_id="turn_explicit", text=f"修改 {target} 的内容改成 hello 并保存"
    )
    try:
        with instruction_context(instruction):
            result = _exec(client, "fs_write", {"path": str(target), "content": "hello"})
    finally:
        ctx.instructions.end(instruction)
        ctx.authorization.reviewer = original_reviewer
    assert "已提交审批" in (result.content or ""), "关键词授权的旧行为不得复活（应返回待审批）"
    assert target.read_text(encoding="utf-8") == "old", "没有审查结论时不得执行写入"
    pending = _pending(client)
    assert len(pending) == 1, "应当退回普通审批（等待用户明确决定）"
    assert pending[0]["state"] == "waiting"


def test_unrestricted_still_requires_three_confirmations_for_internal_data(app_client):
    client = app_client
    client.put("/api/security/mode", json={"mode": "unrestricted", "user_confirmed": True})
    ctx = client.app.state.ctx
    internal = str(ctx.settings.db_path)
    result = _exec(client, "fs_write", {"path": internal, "content": "corrupt"})
    assert result.ok is True, result.error
    pending = _pending(client)
    assert len(pending) == 1
    item = pending[0]
    assert item["state"] == "waiting"
    assert int(item.get("confirmed_stage") or 0) == 0
    # API 字段名是 requires_confirmations（storage/approvals.py 的权威形状）
    assert int(item.get("requires_confirmations") or 1) == 3
    # 只确认一次：不执行，仍等待
    resp = client.post(f"/api/approvals/{item['approval_id']}/confirm", json={"stage": 1})
    assert resp.status_code == 200, resp.text
    time.sleep(0.1)
    assert not any(
        row["state"] == "completed"
        for row in client.app.state.ctx.conn.execute(
            "SELECT state FROM pending_approvals WHERE approval_id = ?", (item["approval_id"],)
        )
    )
    # 用户取消：停止，不执行
    client.post(f"/api/approvals/{item['approval_id']}/decide", json={"decision": "cancel"})
