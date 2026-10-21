# -*- coding: utf-8 -*-
"""P4-B 工具开发闭环：完整版本证据 / 模拟 != 真实可用 / 放弃只读 / 停用恢复。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent.prompts import TOOL_DESCRIPTIONS
from agent.storage.tool_store import ToolStore
from agent.tools.base import ToolResult
from agent.tools.declare_completion import DeclareCompletionTool
from agent.tools.dev_tools import (
    DevListFilesTool,
    DevListTasksTool,
    DevReadFileTool,
    DevRunTestsTool,
    DevSubmitTool,
    DevWriteFileTool,
)
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.lifecycle import ToolLifecycle
from agent.tools.policy import isolation_label
from agent.tools.registry import ToolRegistry
from agent.tools.spec import ToolDefinition


class _AutoApprovals:
    """自动批准的审批替身（执行确认由它放行）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def request(self, kind, payload, **kwargs):  # noqa: ANN001
        from agent.tools.approval import ApprovalResult

        self.requests.append(payload)
        return ApprovalResult("appr_test", "approved")


class _DummyTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "dummy"
        self.parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):  # noqa: ANN003
        return ToolResult(ok=True, content="{}")


def _ws_root() -> Path:
    return Path(tempfile.mkdtemp()) / "ws"


def _function_definition(*, credential_ref: str | None = None) -> ToolDefinition:
    return ToolDefinition(
        name="add_numbers",
        description="求和",
        tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
        credential_ref=credential_ref,
    )


def _ready_workspace(tmp_path, definition: ToolDefinition, *, tested: bool = True):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("求和工具")
    ws.write_definition(task.id, definition)
    if tested:
        ws.record_test(task.id, True, "1/1 tests passed")
    ws.mark_submitted(task.id)
    return ws, task


# ---------------------------------------------------------------------------
# 1. 完整版本摘要进入模型可见 data（不截短、不要求模型计算）
# ---------------------------------------------------------------------------


async def test_dev_list_tasks_returns_full_version_evidence():
    ws = DevWorkspace(_ws_root())
    first = ws.create("任务一")
    ws.write_definition(first.id, _function_definition())
    ws.record_test(first.id, True, "1/1 tests passed")
    tool = DevListTasksTool(ws)
    result = await tool.run()
    assert result.ok is True
    tasks = result.data["tasks"]
    assert tasks
    task_data = tasks[0]
    assert task_data["version"] == ws.content_digest(first.id)
    assert len(task_data["version"]) == 64
    assert task_data["test"]["passed"] is True
    assert task_data["test"]["state"] == "current"
    assert task_data["registration"]["status"] == "none"
    assert task_data["real_verification"]["required"] is False


async def test_dev_run_tests_returns_full_version_and_separate_evidence():
    ws = DevWorkspace(_ws_root())
    task = ws.create("求和工具")
    ws.write_definition(task.id, _function_definition())
    tool = DevRunTestsTool(ws, approvals=_AutoApprovals())
    result = await tool.run(workspace=task.id)
    assert result.ok is True, result.error
    data = result.data
    digest = ws.content_digest(task.id)
    assert data["version"] == digest and len(data["version"]) == 64
    assert data["test"]["passed"] is True
    assert data["test"]["state"] == "current"
    assert data["registration"]["status"] == "none"
    assert data["real_verification"]["required"] is False
    # 执行边界如实来自唯一文案来源（受限子进程不是安全沙箱）。
    boundary = data["execution_boundary"]
    assert boundary["isolation"] in (
        isolation_label(""),
        isolation_label("docker"),
    )
    if boundary["executor"] != "docker":
        assert "受限子进程" in boundary["isolation"]
        assert "不是安全沙箱" in boundary["isolation"]
        assert boundary["unprotected"]


async def test_dev_submit_returns_full_version_and_registration_evidence():
    ws = DevWorkspace(_ws_root())
    task = ws.create("求和工具")
    ws.write_definition(task.id, _function_definition())
    digest_before = ws.content_digest(task.id)

    from agent.tools.lifecycle import ToolOutcome

    class _FakeLifecycle:
        async def submit_definition(
            self, definition, explanation, *, group_id=None, test_sink=None
        ):
            if test_sink is not None:
                test_sink(True, "1/1 tests passed")
            return ToolOutcome(True, "add_numbers", "registered", "ok")

    async def builder():
        return _FakeLifecycle()

    tool = DevSubmitTool(ws, lifecycle_builder=builder, approvals=_AutoApprovals())
    result = await tool.run(
        workspace=task.id,
        explanation="计算两个数字之和，便于以后重复使用",
    )
    assert result.ok is True, result.error
    data = result.data
    assert data["version"] == digest_before
    assert len(data["version"]) == 64
    assert data["registration"]["status"] == "registered"
    assert data["registration"]["tool_name"] == "add_numbers"
    assert data["registration"]["version"] == digest_before
    # 内容里也给出完整版本，而不是 12 位前缀。
    assert digest_before in result.content
    # 注册证据落盘：重启后仍可读。
    restored = DevWorkspace(ws.root_dir.parent / "ws")
    status = restored.status(task.id)
    assert status["registration"]["status"] == "registered"


async def test_declare_completion_returns_full_version_data(tmp_path):
    ws, task = _ready_workspace(tmp_path, _function_definition())
    registry = ToolRegistry()
    registry.register(_DummyTool("add_numbers"))
    tool = DeclareCompletionTool(ws, registry)
    version = ws.content_digest(task.id)
    ws.record_registration(task.id, tool_name="add_numbers", version=version)
    result = await tool.run(
        task_id=task.id, version=version, claims=["test_passed", "registered", "usable"]
    )
    assert result.ok is True, result.error
    data = result.data
    assert data["version"] == version and len(data["version"]) == 64
    assert data["accepted"] is True
    assert data["registration"]["status"] == "registered"
    assert data["test"]["passed"] is True
    assert version in result.content  # 依据里给完整摘要
    # 旧 facts 协议保留（不重命名机器 claims）。
    assert result.facts["declaration"]["accepted"] is True


# ---------------------------------------------------------------------------
# 2. 模拟测试 + 注册 != 真实可用（真实条件验证独立记录）
# ---------------------------------------------------------------------------


async def test_credential_tool_usable_requires_real_verification(tmp_path):
    ws, task = _ready_workspace(
        tmp_path, _function_definition(credential_ref="weather-key")
    )
    registry = ToolRegistry()
    registry.register(_DummyTool("add_numbers"))
    version = ws.content_digest(task.id)
    ws.record_registration(task.id, tool_name="add_numbers", version=version)
    tool = DeclareCompletionTool(ws, registry)
    rejected = await tool.run(
        task_id=task.id, version=version, claims=["registered", "usable"]
    )
    assert rejected.ok is False
    assert "真实条件" in rejected.error
    assert rejected.data["real_verification"]["required"] is True
    assert rejected.data["real_verification"]["status"] in ("not_attempted", "unavailable")
    assert rejected.data["real_verification"]["verified"] is False
    # 模拟测试通过 + 注册成功仍不能声明可用。
    assert rejected.data["test"]["passed"] is True
    assert rejected.data["registration"]["status"] == "registered"
    # 条件与授权充分时做真实验证 → 可用可以声明。
    ws.record_real_verification(
        task.id,
        status="verified",
        summary="real_lookup_ok",
        reason_code="",
        evidence_ids=["tool_result_1"],
    )
    accepted = await tool.run(
        task_id=task.id, version=version, claims=["registered", "usable"]
    )
    assert accepted.ok is True, accepted.error
    assert accepted.data["real_verification"]["verified"] is True


async def test_real_verification_goes_stale_after_content_change(tmp_path):
    ws, task = _ready_workspace(
        tmp_path, _function_definition(credential_ref="weather-key")
    )
    registry = ToolRegistry()
    registry.register(_DummyTool("add_numbers"))
    version = ws.content_digest(task.id)
    ws.record_registration(task.id, tool_name="add_numbers", version=version)
    ws.record_real_verification(task.id, status="verified", summary="ok")
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 2")
    new_version = ws.content_digest(task.id)
    tool = DeclareCompletionTool(ws, registry)
    snapshot = ws.evidence_snapshot(task.id)
    assert snapshot["real_verification"]["current"] is False
    assert snapshot["real_verification"]["verified"] is False
    stale = await tool.run(
        task_id=task.id, version=new_version, claims=["registered", "usable"]
    )
    assert stale.ok is False
    assert stale.data["real_verification"]["current"] is False


# ---------------------------------------------------------------------------
# 3. 已放弃任务：允许只读 / 禁止写入、测试、提交
# ---------------------------------------------------------------------------


async def test_abandoned_task_is_read_only_but_material_reusable():
    ws = DevWorkspace(_ws_root())
    task = ws.create("已放弃的任务")
    ws.write_file(task.id, "notes.md", "可复用的材料")
    abandoned = ws.abandon(task.id)
    assert abandoned["ok"] is True
    assert ws.is_abandoned(task.id) is True

    listed = await DevListFilesTool(ws).run(workspace=task.id)
    assert listed.ok is True
    assert "notes.md" in listed.content
    assert listed.data["read_only"] is True

    read = await DevReadFileTool(ws).run(workspace=task.id, name="notes.md")
    assert read.ok is True
    assert read.content == "可复用的材料"
    assert read.data["read_only"] is True

    for rejected in (
        await DevWriteFileTool(ws).run(workspace=task.id, name="x.py", content="x"),
        await DevRunTestsTool(ws, approvals=_AutoApprovals()).run(workspace=task.id),
        await DevSubmitTool(ws, lifecycle_builder=_never_lifecycle, approvals=_AutoApprovals()).run(
            workspace=task.id, explanation="x"
        ),
    ):
        assert rejected.ok is False
        assert rejected.category == "abandoned"
        assert rejected.data["abandoned"] is True
        assert rejected.data["write_allowed"] is False


async def _never_lifecycle():
    raise AssertionError("已放弃任务不应走到生命周期提交")


# ---------------------------------------------------------------------------
# 4. 工具暂时停用 / 恢复（保留工作）
# ---------------------------------------------------------------------------


def test_suspend_and_resume_keep_definition_and_work(db_conn: sqlite3.Connection):
    store = ToolStore(db_conn)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=object(), approvals=object(), registry=registry, tool_store=store
    )
    definition = _function_definition()
    store.save(definition)
    lifecycle._register(definition)
    assert registry.get("add_numbers") is not None

    suspended = lifecycle.suspend_tool("add_numbers", reason="major_defect")
    assert suspended["ok"] is True
    assert suspended["status"] == "suspended"
    assert suspended["work_preserved"] is True
    assert registry.get("add_numbers") is None
    # 停用不是撤销：定义还在持久层（suspended），可以恢复。
    assert store.load("add_numbers") is None
    assert store.load_any("add_numbers") is not None
    meta = {row["name"]: row["status"] for row in store.list_meta()}
    assert meta["add_numbers"] == "suspended"

    resumed = lifecycle.resume_tool("add_numbers")
    assert resumed["ok"] is True
    assert resumed["status"] == "active"
    assert registry.get("add_numbers") is not None
    assert store.load("add_numbers") is not None
    meta_after = {row["name"]: row["status"] for row in store.list_meta()}
    assert meta_after["add_numbers"] == "active"
    # 定义内容没有被停用/恢复改写。
    assert store.load("add_numbers").code == definition.code


def test_resume_reports_missing_definition(db_conn: sqlite3.Connection):
    store = ToolStore(db_conn)
    lifecycle = ToolLifecycle(
        adapter=object(), approvals=object(), registry=ToolRegistry(), tool_store=store
    )
    missing = lifecycle.resume_tool("ghost_tool")
    assert missing["ok"] is False
    assert missing["code"] == "definition_not_found"


# ---------------------------------------------------------------------------
# 5. 工具 descriptions 仍单一来源 / 文案诚实
# ---------------------------------------------------------------------------


def test_tool_descriptions_come_from_prompts_single_source():
    for name in (
        "dev_list_tasks",
        "dev_run_tests",
        "dev_submit_tool",
        "declare_completion",
        "create_tool",
    ):
        assert name in TOOL_DESCRIPTIONS
    # 受限子进程表述唯一来源（tools/policy.py）：绝不能叫安全沙箱。
    assert "不是安全沙箱" in isolation_label("")
    assert "Docker" in isolation_label("docker")
# ---------------------------------------------------------------------------
# 6. data 真正进入模型消息（不是只放进 facts）
# ---------------------------------------------------------------------------


async def test_full_version_reaches_model_message_not_only_facts():
    from agent.core.tool_feedback import message as feedback_message

    ws = DevWorkspace(_ws_root())
    task = ws.create("求和工具")
    ws.write_definition(task.id, _function_definition())
    tool = DevRunTestsTool(ws, approvals=_AutoApprovals())
    result = await tool.run(workspace=task.id)
    assert result.protocol_version == 1
    digest = ws.content_digest(task.id)
    model_text = feedback_message(result, tool_name="dev_run_tests")
    assert digest in model_text
    assert '"version"' in model_text
    assert "受限子进程" in model_text or "Docker" in model_text

    tasks_result = await DevListTasksTool(ws).run()
    assert tasks_result.protocol_version == 1
    assert digest in feedback_message(tasks_result, tool_name="dev_list_tasks")


async def test_declare_completion_message_contains_full_version(tmp_path):
    from agent.core.tool_feedback import message as feedback_message

    ws, task = _ready_workspace(tmp_path, _function_definition())
    registry = ToolRegistry()
    registry.register(_DummyTool("add_numbers"))
    version = ws.content_digest(task.id)
    ws.record_registration(task.id, tool_name="add_numbers", version=version)
    tool = DeclareCompletionTool(ws, registry)
    result = await tool.run(task_id=task.id, version=version, claims=["test_passed"])
    assert result.protocol_version == 1
    text = feedback_message(result, tool_name="declare_completion")
    assert version in text
    assert '"real_verification"' in text
    assert '"registration"' in text
