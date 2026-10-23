"""R9-H4 C：文件/附件实际影响、目录范围、批量逐项（契约 §2/§3/§6/§7）。

真实工具 + 真实文件操作 + 真实审批执行器（approved_operations 现有路径）；
授权网关用记录型 fake（只记录 effect / 返回预设 verdict，不复制权限判定）。
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from agent.security.effects import AuthDecision, Verdict
from agent.security.operations import ExecutionContext, PendingOperation
from agent.services.attachments import AttachmentService
from agent.services.computer import ComputerSandbox
from agent.tools import approved_operations, fs_tools, security_gate
from agent.tools.attachment_tools import ReadAttachmentTool
from agent.tools.fs_tools import (
    FsDeleteTool,
    FsListTool,
    FsReadTool,
    FsWriteTool,
    FsPatchTool,
)


def _sandbox(root: Path, mode: str = "restricted") -> ComputerSandbox:
    return ComputerSandbox(
        resolve_root=lambda: str(root), permission_mode=lambda: mode
    )


class RecordingGateway:
    """记录 effect / 返回预设 verdict；submit 记录待审批操作（不做权限判定）。"""

    def __init__(
        self,
        mode: str = "restricted",
        verdicts: dict | None = None,
        *,
        internal_paths: list[Path] | None = None,
    ) -> None:
        self._mode = mode
        self.verdicts = dict(verdicts or {})
        self.effects: list = []
        self.submitted: list = []
        self.internal_paths = list(internal_paths or [])

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    def internal_data_paths(self):
        return list(self.internal_paths)

    async def authorize(self, effect, *, ctx=None):
        self.effects.append(effect)
        kind = str(getattr(getattr(effect, "kind", None), "value", effect.kind))
        return AuthDecision(
            verdict=self.verdicts.get(kind, Verdict.ALLOW), reason=f"fake:{kind}"
        )

    async def submit(self, operation, *, ctx=None):
        self.submitted.append(operation)
        return types.SimpleNamespace(
            approval_id="appr_r9h4_c",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=operation.confirmations_required,
        )


def _ctx(operation: PendingOperation, *, mode: str = "restricted") -> ExecutionContext:
    return ExecutionContext(
        approval_id="appr_r9h4_c",
        operation=operation,
        mode_at_request=mode,
        mode_at_approval=mode,
    )


def _detail(effect) -> dict:
    return dict(getattr(effect, "detail", {}) or {})


def _impact_map(effect) -> dict[str, list[tuple[str, str]]]:
    return {
        str(item.get("operation") or ""): [
            (str(obj.get("kind") or ""), str(obj.get("value") or ""))
            for obj in item.get("objects") or []
        ]
        for item in _detail(effect).get(fs_tools.LIMIT_IMPACTS_KEY) or []
    }


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# 1) 写/读实际影响（真实工具）
# ---------------------------------------------------------------------------


async def test_fs_write_and_patch_report_real_impacts(workspace: Path):
    target = workspace / "a.txt"
    target.write_text("旧内容", encoding="utf-8")
    gateway = RecordingGateway(verdicts={"write": Verdict.APPROVE})
    tool = FsWriteTool(computer=_sandbox(workspace), security=gateway)
    result = await tool.run(path=str(target), content="新内容")
    assert result.ok is True and result.code == "pending_approval"
    effect = gateway.effects[0]
    assert _impact_map(effect) == {"write": [("path", str(target))]}
    assert _detail(effect)[fs_tools.LIMIT_IMPACTS_COMPLETE_KEY] is True
    operation = gateway.submitted[0]
    assert operation.fingerprint["limit_impacts"] == fs_tools.write_limit_impacts(target)
    assert operation.fingerprint["limit_impacts_complete"] is True
    assert operation.fingerprint["identity_key"]

    # 新建文件 = create；补丁 = write
    new_path = workspace / "b.txt"
    gateway2 = RecordingGateway(verdicts={"write": Verdict.APPROVE})
    tool2 = FsWriteTool(computer=_sandbox(workspace), security=gateway2)
    await tool2.run(path=str(new_path), content="创建内容")
    assert _impact_map(gateway2.effects[0]) == {"create": [("path", str(new_path))]}

    gateway3 = RecordingGateway(verdicts={"write": Verdict.APPROVE})
    tool3 = FsPatchTool(computer=_sandbox(workspace), security=gateway3)
    target.write_text("旧内容", encoding="utf-8")
    await tool3.run(path=str(target), old="旧内容", new="补丁内容")
    assert _impact_map(gateway3.effects[0]) == {"write": [("path", str(target))]}


async def test_fs_read_and_directory_queries_are_not_expanded(workspace: Path):
    directory = workspace / "资料"
    directory.mkdir()
    (directory / "child.txt").write_text("x", encoding="utf-8")
    gateway = RecordingGateway()
    listed = await FsListTool(
        computer=_sandbox(workspace), security=gateway
    ).run(path=str(directory))
    assert listed.ok is True
    impacts = _impact_map(gateway.effects[-1])
    assert impacts == {"read": [("path", str(directory))]}
    # 普通目录查询不得把每个子对象都当成限制对象（不扩大限制）。
    values = [value for _, value in impacts["read"]]
    assert str(directory / "child.txt") not in values

    read_gateway = RecordingGateway()
    read = await FsReadTool(
        computer=_sandbox(workspace), security=read_gateway
    ).run(path=str(directory / "child.txt"))
    assert read.ok is True
    assert _impact_map(read_gateway.effects[-1]) == {
        "read": [("path", str(directory / "child.txt"))]
    }


# ---------------------------------------------------------------------------
# 2) 删除目录：有界枚举子对象（含受保护/内部数据），不能只查目录本身
# ---------------------------------------------------------------------------


async def test_fs_delete_directory_impacts_include_children_and_protected(
    workspace: Path,
):
    directory = workspace / "待删目录"
    (directory / "sub").mkdir(parents=True)
    (directory / "a.txt").write_text("A", encoding="utf-8")
    (directory / "sub" / "b.txt").write_text("B", encoding="utf-8")
    protected_child = directory / "secret.db"
    protected_child.write_text("internal", encoding="utf-8")
    gateway = RecordingGateway(
        # 目录自身不注册、但枚举到受保护子对象：该删除按内部数据维护审批。
        verdicts={"delete": Verdict.APPROVE, "internal_data": Verdict.APPROVE},
        internal_paths=[protected_child],
    )
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    result = await tool.run(paths=[str(directory)])
    assert result.ok is True and result.code == "pending_approval"

    effect = gateway.effects[0]
    detail = _detail(effect)
    assert detail[fs_tools.LIMIT_IMPACTS_COMPLETE_KEY] is True
    deleted = _impact_map(effect)["delete"]
    deleted_values = {value for _, value in deleted}
    for expected in (directory, directory / "a.txt", directory / "sub", directory / "sub" / "b.txt", protected_child):
        assert str(expected) in deleted_values, expected
    # 受保护子对象明确列出（不能只查目录本身）。
    assert str(protected_child) in detail[fs_tools.INTERNAL_DATA_ENTRIES_KEY]
    assert detail[fs_tools.DIRECTORY_IMPACT_DIGEST_KEY]
    assert detail[fs_tools.DIRECTORY_IMPACT_KEY]["complete"] is True
    entries = {entry["path"]: entry for entry in detail[fs_tools.DIRECTORY_IMPACT_KEY]["entries"]}
    assert entries[str(protected_child)]["protected"] is True

    operation = gateway.submitted[0]
    item_fp = operation.fingerprint["paths"][0]
    assert item_fp["is_directory"] is True
    assert item_fp["directory_impact_digest"] == detail[fs_tools.DIRECTORY_IMPACT_DIGEST_KEY]
    assert item_fp["limit_impacts_complete"] is True
    assert str(protected_child) in item_fp["internal_data_entries"]

    # R9-H4 §7：等待期间新增子对象 → 审批时的目录影响依据不再成立。
    (directory / "new.txt").write_text("新增", encoding="utf-8")
    verdict = fs_tools.verify_directory_impact(
        item_fp["directory_impact"],
        directory,
        is_protected=lambda path: path == protected_child,
    )
    assert verdict["ok"] is False
    assert verdict["added"], verdict
    assert str(directory / "new.txt") in verdict["added"]


async def test_directory_impact_truncation_is_explicit(workspace: Path, monkeypatch):
    monkeypatch.setattr(fs_tools, "DIRECTORY_IMPACT_MAX_ENTRIES", 3)
    directory = workspace / "大目录"
    directory.mkdir()
    for index in range(6):
        (directory / f"f{index}.txt").write_text(str(index), encoding="utf-8")
    gateway = RecordingGateway(verdicts={"delete": Verdict.APPROVE})
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    await tool.run(paths=[str(directory)])
    effect = gateway.effects[0]
    detail = _detail(effect)
    assert detail[fs_tools.LIMIT_IMPACTS_COMPLETE_KEY] is False
    assert "上限" in str(detail[fs_tools.LIMIT_IMPACTS_REASON_KEY])
    objects = _impact_map(effect)["delete"]
    assert any(kind == "path_prefix" for kind, _ in objects), "不完整时必须有范围兜底对象"
    operation = gateway.submitted[0]
    assert operation.fingerprint["paths"][0]["limit_impacts_complete"] is False


def test_verify_directory_impact_detects_add_remove_replace_and_unchanged(
    workspace: Path,
):
    directory = workspace / "目录"
    directory.mkdir()
    first = directory / "a.txt"
    second = directory / "b.txt"
    first.write_text("A", encoding="utf-8")
    second.write_text("B", encoding="utf-8")
    expected = fs_tools.directory_impact(directory, operation="delete")
    assert fs_tools.verify_directory_impact(expected, directory)["ok"] is True

    (directory / "c.txt").write_text("C", encoding="utf-8")
    added = fs_tools.verify_directory_impact(expected, directory)
    assert added["ok"] is False and str(directory / "c.txt") in added["added"]
    (directory / "c.txt").unlink()

    second.unlink()
    removed = fs_tools.verify_directory_impact(expected, directory)
    assert removed["ok"] is False and str(second) in removed["removed"]
    second.write_text("B", encoding="utf-8")

    security_gate.atomic_write_text(first, "A-新内容")
    replaced = fs_tools.verify_directory_impact(expected, directory)
    assert replaced["ok"] is False and str(first) in replaced["replaced"]

    # 审批时影响不完整（达到上限）→ 不能假装没变化。
    partial = fs_tools.directory_impact(directory, max_entries=1)
    assert partial["complete"] is False
    verdict = fs_tools.verify_directory_impact(partial, directory)
    assert verdict["ok"] is False
    assert verdict["code"] == "directory_impact_incomplete"


# ---------------------------------------------------------------------------
# 3) 批量逐项：只 B 失效，A 继续；汇总不漏项；平台去重
# ---------------------------------------------------------------------------


def _fake_recycle(monkeypatch):
    from agent.services import recycle_bin

    calls: list[str] = []

    def _move(path):
        target = Path(path)
        calls.append(str(target))
        if target.exists():
            target.unlink()
        return {"ok": True, "mechanism": "fake_recycle", "deleted_permanently": False}

    monkeypatch.setattr(recycle_bin, "send_to_recycle_bin", _move, raising=False)
    return calls


async def test_batch_delete_only_changed_item_invalidated_other_completes(
    workspace: Path, monkeypatch
):
    first = workspace / "A.txt"
    second = workspace / "B.txt"
    first.write_text("A", encoding="utf-8")
    second.write_text("B", encoding="utf-8")
    gateway = RecordingGateway(verdicts={"delete": Verdict.APPROVE})
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    result = await tool.run(paths=[str(first), str(second)])
    assert result.ok is True and result.code == "pending_approval"
    assert len(gateway.submitted) == 1, "一批一个审批操作（不逐项弹窗）"
    operation = gateway.submitted[0]

    # 等待期间 B 被原子替换（身份/内容变化）；A 未变。
    security_gate.atomic_write_text(second, "B-被替换")
    calls = _fake_recycle(monkeypatch)
    executed = await approved_operations.execute_fs_delete(
        operation.payload, _ctx(operation)
    )
    assert calls == [str(first)], "只有 A 真正进回收站；B 不能先被整批判废/误删"
    assert not first.exists()
    assert second.exists(), "B 已变化：必须保留"
    statuses = {item["path"]: item for item in executed.detail["items"]}
    assert statuses[str(first)]["status"] == "completed"
    assert statuses[str(second)]["status"] == "invalidated"
    assert len(statuses) == 2, "汇总必须包含全部项，不漏项"
    assert executed.ok is False
    assert "部分文件" in (executed.summary or "")


async def test_batch_delete_dedupes_by_platform_semantics(workspace: Path):
    # 纯 helper：Windows 归一化去重；POSIX 大小写敏感。
    assert fs_tools.path_dedupe_key("C:/Work/A.txt", windows=True) == fs_tools.path_dedupe_key(
        "c:/work/a.txt", windows=True
    )
    assert fs_tools.path_dedupe_key("/tmp/A.txt", windows=False) != fs_tools.path_dedupe_key(
        "/tmp/a.txt", windows=False
    )
    target = workspace / "重复.txt"
    target.write_text("x", encoding="utf-8")
    gateway = RecordingGateway(verdicts={"delete": Verdict.APPROVE})
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    await tool.run(paths=[str(target), str(target), str(target)])
    operation = gateway.submitted[0]
    assert len(operation.payload["paths"]) == 1, "同一路径去重（按平台语义）"


async def test_batch_summary_contains_all_items(workspace: Path, monkeypatch):
    _fake_recycle(monkeypatch)
    files = []
    for index in range(3):
        path = workspace / f"file{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        files.append(path)
    gateway = RecordingGateway(verdicts={"delete": Verdict.ALLOW})
    tool = FsDeleteTool(computer=_sandbox(workspace), security=gateway)
    result = await tool.run(paths=[str(path) for path in files])
    assert result.ok is True
    data = result.data or {}
    statuses = data.get("items") or []
    results = data.get("results") or []
    assert len(statuses) == 3 and len(results) == 3
    assert {item["path"] for item in statuses} == {str(path) for path in files}


# ---------------------------------------------------------------------------
# 4) 附件：读取 / 写回 / 回收影响
# ---------------------------------------------------------------------------


@pytest.fixture()
def attachment_service(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


async def test_attachment_read_and_write_back_report_impacts(
    attachment_service: AttachmentService, tmp_path: Path
):
    source = tmp_path / "源.txt"
    source.write_text("原内容", encoding="utf-8")
    attachment = attachment_service.register_source(
        path=str(source), source_kind="desktop_path"
    )
    allow = RecordingGateway()
    read_tool = ReadAttachmentTool(
        attachment_service, active_turn_id=lambda: None, security=allow
    )
    read = await read_tool.run(attachment_id=attachment.id)
    assert read.ok is True
    assert _impact_map(allow.effects[0]) == {"read": [("path", str(source))]}

    write_gateway = RecordingGateway(verdicts={"write": Verdict.APPROVE})
    pending = await attachment_service.write_back_source(
        attachment.id, "新内容".encode("utf-8"), gateway=write_gateway
    )
    assert pending["pending_approval"] is True
    effect = write_gateway.effects[0]
    assert _impact_map(effect) == {"write": [("path", str(source))]}
    assert _detail(effect)[fs_tools.LIMIT_IMPACTS_COMPLETE_KEY] is True
    operation = write_gateway.submitted[0]
    assert operation.fingerprint["limit_impacts"] == fs_tools.write_limit_impacts(source)


async def test_attachment_recycle_directory_impacts(
    attachment_service: AttachmentService, tmp_path: Path
):
    directory = tmp_path / "材料目录"
    directory.mkdir()
    (directory / "one.txt").write_text("1", encoding="utf-8")
    protected = directory / "internal.db"
    protected.write_text("internal", encoding="utf-8")
    gateway = RecordingGateway(
        verdicts={"delete": Verdict.APPROVE}, internal_paths=[protected]
    )
    result = await attachment_service.recycle_material_files(
        [{"attachment_id": "att", "path": str(directory)}], gateway=gateway
    )
    assert result["pending_approval"] is True
    effect = gateway.effects[0]
    detail = _detail(effect)
    assert detail[fs_tools.LIMIT_IMPACTS_COMPLETE_KEY] is True
    deleted = {value for _, value in _impact_map(effect)["delete"]}
    assert str(directory / "one.txt") in deleted
    assert str(protected) in deleted
    assert str(protected) in detail[fs_tools.INTERNAL_DATA_ENTRIES_KEY]
    operation = gateway.submitted[0]
    item_fp = operation.fingerprint["items"][0]
    assert item_fp["directory_impact_digest"] == detail[fs_tools.DIRECTORY_IMPACT_DIGEST_KEY]
    assert item_fp["precondition_digest"]
