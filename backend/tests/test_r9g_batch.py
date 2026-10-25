"""R9-G W3：批量逐项核对 + 审批合并 + 批准后重核对（契约 §6、§7）。

真实工具/服务 + 真实临时文件/DB；fake 网关按项返回 verdict（接口级）。
覆盖：
* 混合批次逐项状态 allowed/denied/waiting/completed，一个对象的允许不扩大整批；
* 需要确认的项合并为**一个**审批操作（payload items 列表），不逐项弹窗；
* 已允许项直接执行；被拒项不动；失败项保留；
* 执行前重核对失败：该项 invalidated，无依赖的已授权项继续；
* 批准后的 fs.delete / attachment 批量执行器逐项重核对，失效项保留文件；
* 附件批量回收/批量写回：逐项状态 + 一次审批 + 只执行仍获准项。
"""

from __future__ import annotations

import os
import types

import pytest

from agent.security.effects import AuthDecision, Verdict
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.attachments import AttachmentService
from agent.services.computer import ComputerSandbox
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools import approved_operations
from agent.tools.fs_tools import FsDeleteTool


class BatchGateway:
    """按调用顺序返回 verdict 的 fake 网关；记录 effect / 提交的审批操作。"""

    def __init__(self, verdicts: list[str], mode: str = "restricted") -> None:
        self.verdicts = list(verdicts)
        self._mode = mode
        self.effects: list = []
        self.submitted: list = []
        self._calls = 0

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        index = min(self._calls, len(self.verdicts) - 1)
        self._calls += 1
        return AuthDecision(
            verdict={
                "allow": Verdict.ALLOW,
                "approve": Verdict.APPROVE,
                "deny": Verdict.DENY,
            }[self.verdicts[index]],
            reason=f"fake:{self.verdicts[index]}",
        )

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_batch_1",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=int(getattr(operation, "confirmations_required", 1) or 1),
        )


class FakeLedger:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid

    def validate_binding(self, binding) -> bool:  # noqa: ANN001
        return self.valid


class FakeEngine:
    """只用于执行器重核对测试：requirements.validate_binding + authorize。"""

    def __init__(self, *, binding_valid: bool = True, verdict: str = "allow") -> None:
        self.requirements = FakeLedger(binding_valid)
        self.verdict = verdict
        self.authorized: list = []

    async def authorize(self, effect, *, ctx=None):
        self.authorized.append(effect)
        return AuthDecision(verdict=Verdict(self.verdict), reason=f"fake:{self.verdict}")


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _sandbox(root):
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: "restricted"
    )


def _ctx(operation: PendingOperation, engine=None) -> ExecutionContext:
    return ExecutionContext(
        approval_id="appr_test",
        operation=operation,
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=engine,
    )


def _fake_recycle(path: str) -> dict:
    os.unlink(path)
    return {
        "ok": True,
        "path": path,
        "mechanism": "fake-recycle",
        "reason": "已移入回收站",
        "deleted_permanently": False,
    }


def _by_path(data_items: list, path) -> dict:
    wanted = str(path)
    for item in data_items:
        if str(item.get("path")) == wanted:
            return item
    raise AssertionError(f"批次结果里没有 {wanted}：{data_items}")


# ---------------------------------------------------------------------------
# fs_delete 混合批次
# ---------------------------------------------------------------------------


async def test_fs_delete_mixed_batch_keeps_per_item_status(workspace, monkeypatch):
    import asyncio

    import agent.services.recycle_bin as recycle_module

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", _fake_recycle)
    first = workspace / "已授权删除.txt"
    second = workspace / "需要确认.txt"
    third = workspace / "被拒绝.txt"
    for path in (first, second, third):
        path.write_text("内容", encoding="utf-8")

    # 逐项判定：第一个 allow、第二个 approve、第三个 deny；随后第一项执行前重核对 allow
    gateway = BatchGateway(["allow", "approve", "deny", "allow"])
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    result = await tool.run(paths=[str(first), str(second), str(third)])

    assert result.ok is True and result.code == "pending_approval", result
    items = result.data["items"]
    assert _by_path(items, first)["status"] == "completed"
    assert _by_path(items, second)["status"] == "waiting"
    assert _by_path(items, third)["status"] == "denied"
    assert not first.exists(), "已允许项必须直接执行"
    assert second.exists() and third.exists(), "待确认/被拒项不得先动"
    # 一次批量审批：只有一个操作，payload items 只含等待项
    assert len(gateway.submitted) == 1
    operation = gateway.submitted[0]
    assert operation.kind == "fs.delete"
    submitted_items = operation.payload["items"]
    assert [item["path"] for item in submitted_items] == [str(second)]
    assert submitted_items[0]["identity"], "审批单必须绑定对象身份"
    assert operation.payload["paths"][0]["identity"]
    # 逐项判定确实发生（三个路径各一次 authorize；重核对另计）
    effect_paths = [
        str(effect.targets[0].value) for effect in gateway.effects if effect.targets
    ]
    assert effect_paths[:3] == [str(first), str(second), str(third)], effect_paths


async def test_fs_delete_recheck_failure_invalidates_only_that_item(workspace, monkeypatch):
    import agent.services.recycle_bin as recycle_module

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", _fake_recycle)
    first = workspace / "重核对失效.txt"
    second = workspace / "继续执行.txt"
    for path in (first, second):
        path.write_text("内容", encoding="utf-8")

    # 判定：两项都 allow；执行前重核对：第一项 deny、第二项 allow
    gateway = BatchGateway(["allow", "allow", "deny", "allow"])
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    result = await tool.run(paths=[str(first), str(second)])

    assert result.ok is False, "有失效项时不得假装全部成功"
    items = result.data["items"]
    assert _by_path(items, first)["status"] == "invalidated"
    assert _by_path(items, second)["status"] == "completed"
    assert first.exists(), "重核对失败的项必须保留文件"
    assert not second.exists(), "无依赖的已授权项必须继续"
    assert gateway.submitted == [], "没有需要确认的项，不得弹审批"


# ---------------------------------------------------------------------------
# 批准后批量执行器：逐项重核对 + 逐项状态
# ---------------------------------------------------------------------------


async def test_approved_batch_delete_executes_items_and_reports_status(workspace, monkeypatch):
    import agent.services.recycle_bin as recycle_module

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", _fake_recycle)
    first = workspace / "批准一.txt"
    second = workspace / "批准二.txt"
    for path in (first, second):
        path.write_text("内容", encoding="utf-8")
    from agent.tools import security_gate

    payload = {
        "paths": [
            {
                "path": str(first),
                "identity": security_gate.file_identity(str(first)),
                "exists": True,
            },
            {
                "path": str(second),
                "identity": security_gate.file_identity(str(second)),
                "exists": True,
            },
        ],
        "items": [
            {"item_id": "item_1", "path": str(first)},
            {"item_id": "item_2", "path": str(second)},
        ],
        "deleted_to_recycle_bin": True,
    }
    operation = PendingOperation(kind="fs.delete", label="批量删除", payload=payload)
    result = await approved_operations.execute_fs_delete(payload, _ctx(operation))
    assert result.ok is True, result.error
    assert not first.exists() and not second.exists()
    items = {item["item_id"]: item for item in result.detail["items"]}
    assert items["item_1"]["status"] == "completed"
    assert items["item_2"]["status"] == "completed"


async def test_approved_batch_delete_invalid_requirement_keeps_files(workspace, monkeypatch):
    import agent.services.recycle_bin as recycle_module

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", _fake_recycle)
    target = workspace / "要求已撤回.txt"
    target.write_text("内容", encoding="utf-8")
    payload = {
        "paths": [{"path": str(target), "identity": None, "exists": True}],
        "requirement_binding": {"requirement_id": "req_withdrawn", "version": 1},
        "deleted_to_recycle_bin": True,
    }
    operation = PendingOperation(kind="fs.delete", label="删除", payload=payload)
    result = await approved_operations.execute_fs_delete(
        payload, _ctx(operation, engine=FakeEngine(binding_valid=False))
    )
    assert result.ok is False
    assert target.exists(), "要求已撤回/版本不符时不得执行"
    assert result.detail["items"][0]["status"] == "invalidated"


async def test_approved_fs_write_invalid_requirement_keeps_original(workspace):
    target = workspace / "写入前要求失效.txt"
    target.write_text("原内容", encoding="utf-8")
    payload = {
        "path": str(target),
        "content": "新内容",
        "requirement_binding": {"requirement_id": "req_old", "version": 1},
    }
    operation = PendingOperation(kind="fs.write", label="写入", payload=payload)
    result = await approved_operations.execute_fs_write(
        payload, _ctx(operation, engine=FakeEngine(binding_valid=False))
    )
    assert result.ok is False
    assert target.read_text(encoding="utf-8") == "原内容"


# ---------------------------------------------------------------------------
# 附件批量回收/写回
# ---------------------------------------------------------------------------


@pytest.fixture()
def attachment_env(tmp_path):
    conn = connect(tmp_path / "r9g_batch.db")
    apply_migrations(conn)
    data_dir = tmp_path / "data"
    service = AttachmentService(conn, data_dir, workspace_dir=data_dir / "workspace")
    yield {"service": service, "tmp": tmp_path}
    conn.close()


async def test_attachment_batch_recycle_per_item_statuses(attachment_env, monkeypatch):
    import agent.services.recycle_bin as recycle_module

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", _fake_recycle)
    service = attachment_env["service"]
    allowed = service.write_material_bytes(b"a", name="允许回收.bin", source_kind="browser_upload")
    waiting = service.write_material_bytes(b"b", name="需要确认.bin", source_kind="browser_upload")
    # 逐项判定 allow/approve；已允许项执行前重核对仍为 allow
    gateway = BatchGateway(["allow", "approve", "allow"])
    targets = [
        {"attachment_id": allowed.id, "path": allowed.source_path},
        {"attachment_id": waiting.id, "path": waiting.source_path},
    ]
    result = await service.recycle_material_files(targets, gateway=gateway)
    assert result["pending_approval"] is True
    items = {item["item_id"]: item for item in result["items"]}
    assert items["item_1"]["status"] == "completed"
    assert items["item_2"]["status"] == "waiting"
    assert not os.path.exists(allowed.source_path), "已允许项应已回收"
    assert os.path.exists(waiting.source_path), "待确认项不得先删"
    assert len(gateway.submitted) == 1
    submitted = gateway.submitted[0]
    assert submitted.kind == "attachment.recycle"
    assert [item["path"] for item in submitted.payload["targets"]] == [waiting.source_path]
    assert submitted.payload["requirement_binding"] is None


async def test_attachment_batch_write_back_one_approval_then_execute(attachment_env, tmp_path):
    service = attachment_env["service"]
    first_src = tmp_path / "写回一.txt"
    second_src = tmp_path / "写回二.txt"
    for path in (first_src, second_src):
        path.write_text("旧内容", encoding="utf-8")
    first = service.register_source(path=str(first_src), source_kind="desktop_path")
    second = service.register_source(path=str(second_src), source_kind="desktop_path")

    # 逐项判定 allow/approve；已允许项执行前重核对仍为 allow
    gateway = BatchGateway(["allow", "approve", "allow"])
    result = await service.write_back_many(
        [
            {"attachment_id": first.id, "data": "新一".encode("utf-8")},
            {"attachment_id": second.id, "data": "新二".encode("utf-8")},
        ],
        gateway=gateway,
    )
    assert result["pending_approval"] is True
    items = {item["item_id"]: item for item in result["items"]}
    assert items["item_1"]["status"] == "completed"
    assert items["item_2"]["status"] == "waiting"
    assert first_src.read_text(encoding="utf-8") == "新一"
    assert second_src.read_text(encoding="utf-8") == "旧内容"
    assert len(gateway.submitted) == 1
    operation = gateway.submitted[0]
    assert operation.kind == "attachment.batch_write_back"
    assert [item["attachment_id"] for item in operation.payload["items"]] == [second.id]

    # 批准后执行器只执行 payload 列明项，并逐项重核对
    exec_result = await approved_operations.execute_attachment_batch_write_back(
        operation.payload,
        _ctx(operation),
        attachments_getter=lambda: service,
    )
    assert exec_result.ok is True, exec_result.error
    assert second_src.read_text(encoding="utf-8") == "新二"
    assert exec_result.detail["items"][0]["status"] == "completed"


async def test_attachment_batch_write_back_invalid_requirement_keeps_file(attachment_env, tmp_path):
    service = attachment_env["service"]
    source = tmp_path / "要求失效写回.txt"
    source.write_text("旧内容", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")
    import base64
    import hashlib

    data = "不该写入".encode("utf-8")
    payload = {
        "items": [
            {
                "item_id": "item_1",
                "attachment_id": att.id,
                "path": str(source),
                "identity": att.file_id,
                "content_b64": base64.b64encode(data).decode("ascii"),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
        "requirement_binding": {"requirement_id": "req_gone", "version": 1},
    }
    operation = PendingOperation(
        kind="attachment.batch_write_back", label="批量写回", payload=payload
    )
    result = await approved_operations.execute_attachment_batch_write_back(
        payload,
        _ctx(operation, engine=FakeEngine(binding_valid=False)),
        attachments_getter=lambda: service,
    )
    assert result.ok is False
    assert source.read_text(encoding="utf-8") == "旧内容"
    assert result.detail["items"][0]["status"] == "invalidated"


def test_no_move_rename_or_copy_tool_in_file_tools():
    """边界（契约 §6 的移动/重命名/复制核对）：本模块没有这类入口，无需伪造。"""
    from agent.tools import fs_tools

    names = {
        getattr(value, "name", "")
        for value in vars(fs_tools).values()
        if isinstance(value, type)
    }
    assert "fs_move" not in names and "fs_rename" not in names and "fs_copy" not in names


async def test_batch_replay_does_not_repeat_completed_items(workspace, monkeypatch):
    """恢复/重试：已执行过的项不重复成功；未执行且仍获准的项可继续（幂等重放）。"""
    import agent.services.recycle_bin as recycle_module

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", _fake_recycle)
    first = workspace / "已执行.txt"
    second = workspace / "未执行.txt"
    for path in (first, second):
        path.write_text("内容", encoding="utf-8")
    from agent.tools import security_gate

    payload = {
        "paths": [
            {"path": str(first), "identity": security_gate.file_identity(str(first)), "exists": True},
            {"path": str(second), "identity": security_gate.file_identity(str(second)), "exists": True},
        ],
        "items": [
            {"item_id": "item_1", "path": str(first)},
            {"item_id": "item_2", "path": str(second)},
        ],
        "deleted_to_recycle_bin": True,
    }
    operation = PendingOperation(kind="fs.delete", label="批量删除", payload=payload)
    # 模拟执行器在 item_1 成功、item_2 之前中断：先手工删掉 item_1
    first.unlink()
    result = await approved_operations.execute_fs_delete(payload, _ctx(operation))
    items = {item["item_id"]: item for item in result.detail["items"]}
    assert items["item_1"]["status"] == "invalidated", "已删除项不得被重复执行成功"
    assert items["item_2"]["status"] == "completed", "未执行且仍获准项继续"
    assert not second.exists()

    # 完全重放：两项都已完成 → 都不得再次成功，文件也不会被"复活"
    again = await approved_operations.execute_fs_delete(payload, _ctx(operation))
    again_items = {item["item_id"]: item for item in again.detail["items"]}
    assert all(item["status"] == "invalidated" for item in again_items.values()), again_items
