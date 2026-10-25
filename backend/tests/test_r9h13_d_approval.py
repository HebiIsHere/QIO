# -*- coding: utf-8 -*-
"""R9-H13 W-D：附件写回一致性 / 批量恢复 / 内部维护确认（契约 §3.C2、§3.E、§8、§12 适配层）。

真实流程边界（契约 §5）：
* 真实 ``AuthorizationEngine`` + 真实 SQLite 审批台账（``pending_approvals``）+
  真实 ``ApprovalService.decide``/``confirm`` + 真实后台执行器
  ``ApprovalService._execute_approved`` + 真实 ``AttachmentService`` + 临时文件；
  生产装配同款 ``RecheckService`` 挂在引擎上（``app.py`` 接线）。
* 模型一律 fake（``FakeReviewer``，不联网、不使用真实 Key）。
* **真实回收站替身**：``agent.services.recycle_bin.send_to_recycle_bin`` 用临时文件替身
  （``test_trash``），只验证"进回收站"这一动作与执行器判定，不是操作系统真实回收站验收。
* **历史错误审批行模拟**：通过 ``ApprovalStore.create`` + 受控 SQL 把
  ``confirmations_required``/``state`` 改成修复前的历史形状，用于验证后端重算，
  不是真实历史数据库迁移验收。
* 身份/权限判定全部来自真实引擎；测试不按名称/reason 文本推断权限，只断言
  结构化状态、真实文件事实（写入次数、最终路径与内容）与审批台账阶段。

覆盖：
* C2：读禁止时写回流程 0 次读取、0 次写入；写批准不解除读取禁止；
  写回操作事实携带必要读取影响；拒绝时内容读取次数为 0。
* E1：审批等待期间附件引用改到禁止目录的同一对象硬链接 → 最终真实目标参与核对，
  不出现"检查旧路径、实际写新路径"。
* E2：正常写回/回收不被误判为内容变化（身份 / 读取前内容 / 拟写入内容分别表达）；
  直接允许与审批恢复同规则；真实变化仍使旧方案失效。
* E3：批量逐项核对，单项失效只影响该项；共同条件（要求撤回）失败停止整批。
* §8：内部维护三次确认由后端按真实对象核对（提交/批准/恢复/执行），历史一次确认
  不能当三次；模型 ALLOW 不能完成内部维护；批量里内部对象不是首项也命中；
  三次确认不能覆盖用户禁止。
* §12（审批/执行适配层）：不能仅因模式字符串变化整体失效；read_only 仍停止。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from _r9_auth_support import make_security

from agent.security.context import current_requirement
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.operations import PendingOperation, operation_digest
from agent.security.recheck import RecheckService
from agent.security.requirements import AuthorizationLedger
from agent.services.attachments import (
    AttachmentPermissionDenied,
    AttachmentService,
)
from agent.services.computer import ComputerSandbox
from agent.storage.settings import SettingsStore
from agent.tools import security_gate
from agent.tools.approved_operations import register_approved_operations
from agent.tools.cmd_tools import RunProgramTool


# ---------------------------------------------------------------------------
# Fake 模型审查器（只记录请求/按队列返回，不联网）
# ---------------------------------------------------------------------------


class FakeReviewer:
    """受限模式普通独立授权审查的替身；默认 ALLOW，可排队覆盖。"""

    def __init__(self, *, outcomes=None) -> None:
        self.calls: list = []
        self.outcomes = list(outcomes or [])

    async def review(self, request):
        self.calls.append(request)
        if not self.outcomes:
            return _allow_outcome(request)
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(request)
        return outcome


def _allow_outcome(request):
    from agent.security.review import ReviewDecision, ReviewOutcome

    operation = request.operation
    return ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="用户明确要求处理这个对象",
        requirement_id=request.requirement.requirement_id,
        allowed_operations=(operation.kind,),
        objects=operation.objects,
        limits=("仅限本次操作",),
        purpose=operation.summary or "用户要求",
    )


# ---------------------------------------------------------------------------
# 生产装配：真实引擎 + 真实审批 + 生产同款 RecheckService + 真实附件服务
# ---------------------------------------------------------------------------


def _wire_env(
    tmp_path,
    db_conn,
    *,
    mode="restricted",
    protected=(),
    reviewer=None,
    computer=None,
):
    security = make_security(tmp_path, db_conn, mode=mode, protected=protected)
    security.engine.authorizations = AuthorizationLedger(db_conn)
    security.engine.reviewer = reviewer
    # app.py 同款接线：执行前重核对服务挂在引擎上。
    security.recheck = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        approvals=security.approvals,
        authorizations=security.engine.authorizations,
    )
    security.engine.recheck = security.recheck
    service = AttachmentService(
        db_conn, tmp_path / "data", workspace_dir=tmp_path / "workspace"
    )
    _register_operations(security, service, computer=computer)
    # 决定后不自动后台执行：让测试能在"批准之后、执行之前"插入真实状态变化。
    security.approvals._schedule_execute = lambda approval_id: False
    security.service = service
    security.mode = lambda: security.engine.mode().value
    return security


def _register_operations(security, service, *, computer=None):
    register_approved_operations(
        security.operations,
        computer=computer,
        security=security.engine,
        attachments_getter=lambda: service,
    )


@asynccontextmanager
async def _bound_requirement(security, *, text="按你的要求处理这些文件", limits=None):
    """登记一条真实用户要求并进入绑定上下文（生产由 turn 运行时建立）。"""
    suffix = uuid4().hex[:8]
    requirement = security.requirements.record_user_message(
        text=text, message_id="msg_" + suffix, turn_id="turn_" + suffix
    )
    if limits is not None:
        security.requirements.set_structured_limits(
            requirement.requirement_id, list(limits), source="test_fixture"
        )
    binding = security.requirements.binding_for(requirement.requirement_id)
    token = current_requirement.set(binding)
    try:
        yield requirement, binding
    finally:
        current_requirement.reset(token)


def _add_limit(security, requirement, limit) -> None:
    existing = list(
        getattr(
            security.requirements.by_id(requirement.requirement_id),
            "structured_limits",
            (),
        )
        or ()
    )
    security.requirements.set_structured_limits(
        requirement.requirement_id, existing + [limit], source="test_fixture"
    )


@contextmanager
def _count_content_reads(monkeypatch):
    """统计真实的正文/摘要读取：服务准备路径 + 审批执行器路径（不是源码字符串检查）。"""
    import agent.services.attachments as attachment_module
    import agent.services.file_identity as identity_module

    service_read = attachment_module.content_sha256
    executor_read = identity_module.content_sha256
    counts = {"service": 0, "executor": 0, "paths": []}

    def service_counter(path):  # noqa: ANN001
        counts["service"] += 1
        counts["paths"].append(str(path))
        return service_read(Path(path))

    def executor_counter(path):  # noqa: ANN001
        counts["executor"] += 1
        counts["paths"].append(str(path))
        return executor_read(Path(path))

    monkeypatch.setattr(attachment_module, "content_sha256", service_counter)
    monkeypatch.setattr(identity_module, "content_sha256", executor_counter)
    yield counts


@contextmanager
def _fake_trash(monkeypatch):
    """真实回收站替身（见模块 docstring 边界说明）。"""
    import agent.services.recycle_bin as recycle_module

    calls: list[str] = []

    def mover(path: str) -> dict:
        calls.append(str(path))
        os.unlink(path)
        return {
            "ok": True,
            "path": str(path),
            "mechanism": "test_trash",
            "reason": "已移入测试回收站替身",
            "deleted_permanently": False,
            "trashed_path": str(path) + ".trash",
        }

    monkeypatch.setattr(recycle_module, "send_to_recycle_bin", mover)
    yield calls


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _write_source(service, path: Path, content: str | bytes = "旧内容"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return service.register_source(path=str(path), source_kind="desktop_path")


def _row(security, approval_id: str) -> dict:
    row = security.approvals.store.get(approval_id)
    assert row is not None, "审批台账里应当有这一行"
    return row


def _approval_id(result) -> str:  # noqa: ANN001
    assert result.get("pending_approval") is True, result
    approval = result.get("approval") or {}
    identifier = str(approval.get("approval_id") or "")
    assert identifier, result
    return identifier


def _pending_rows(security) -> list[dict]:
    return security.approvals.store.list_pending()


async def _approve(security, approval_id: str) -> dict:
    return await security.approvals.decide(approval_id, "approve")


async def _execute(security, approval_id: str) -> dict:
    await security.approvals._execute_approved(approval_id)
    return _row(security, approval_id)


def _require_confirmations(row: dict) -> int:
    return int(row.get("requires_confirmations") or 1)


def _was_invalidated(row: dict) -> bool:
    """执行器/审批行是否明确"失效"（state=invalidated 或结果里标记了失效项）。"""
    if row.get("state") == "invalidated":
        return True
    detail = dict(row.get("result") or {})
    if detail.get("invalidated"):
        return True
    for item in detail.get("results") or detail.get("items") or ():
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") == "invalidated" or item.get("invalidated"):
            return True
    return False


# ---------------------------------------------------------------------------
# C2：附件读取必须先获准，写批准不解除读取禁止
# ---------------------------------------------------------------------------


async def test_write_back_with_read_ban_never_reads_and_never_writes(
    tmp_path, db_conn, monkeypatch
):
    """C2：存在明确读取禁止时，写回准备/授权阶段读取次数必须为 0，且不写文件。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    source = tmp_path / "workspace" / "报告.txt"
    att = _write_source(security.service, source, "原始内容")
    with _count_content_reads(monkeypatch) as reads:
        with pytest.raises(AttachmentPermissionDenied):
            async with _bound_requirement(
                security,
                limits=[
                    {
                        "operation": "read",
                        "objects": [{"kind": "path", "value": str(source)}],
                    }
                ],
            ) as (requirement, _binding):
                await security.service.write_back_source(
                    att.id, b"\xe4\xb8\x8d\xe8\xaf\xa5\xe5\x86\x99\xe5\x85\xa5", gateway=security.engine
                )
    assert reads["service"] == 0, "读取被禁止时，写回流程不得读取源文件正文/摘要"
    assert reads["executor"] == 0
    assert source.read_text(encoding="utf-8") == "原始内容"
    assert _pending_rows(security) == [], "读取被禁止时不得建立可执行的写回审批"


async def test_write_back_without_read_ban_is_approved_then_written_once(
    tmp_path, db_conn, monkeypatch
):
    """C2 相邻正常放行：没有读取禁止时，批准后真实写回一次（读取发生在获准之后）。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    source = tmp_path / "workspace" / "报告.txt"
    att = _write_source(security.service, source, "原始内容")
    with _count_content_reads(monkeypatch) as reads:
        async with _bound_requirement(security) as (_requirement, _binding):
            pending = await security.service.write_back_source(
                att.id, "新内容".encode("utf-8"), gateway=security.engine
            )
            approval_id = _approval_id(pending)
            reads_before_approval = dict(reads)
            assert reads_before_approval["service"] >= 1, "获准读取后应读取一次覆盖前内容"
            row = _row(security, approval_id)
            assert _require_confirmations(row) == 1
            await _approve(security, approval_id)
            finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert source.read_text(encoding="utf-8") == "新内容"
    # 重复执行同一审批：单次领取，不重复写。
    await _execute(security, approval_id)
    assert source.read_text(encoding="utf-8") == "新内容"
    assert [item["approval_id"] for item in _pending_rows(security)] == []


async def test_write_approval_does_not_lift_read_ban_added_after_approval(
    tmp_path, db_conn, monkeypatch
):
    """C2：写批准 ≠ 读批准。批准后再出现读取禁止，执行器不得读取也不得写入。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    source = tmp_path / "workspace" / "秘密.txt"
    att = _write_source(security.service, source, "秘密原始")
    async with _bound_requirement(security) as (requirement, _binding):
        pending = await security.service.write_back_source(
            att.id, "秘密新值".encode("utf-8"), gateway=security.engine
        )
        approval_id = _approval_id(pending)
        await _approve(security, approval_id)
        # 批准之后才出现"禁止读取这个文件"的明确用户限制。
        _add_limit(
            security,
            requirement,
            {"operation": "read", "objects": [{"kind": "path", "value": str(source)}]},
        )
        with _count_content_reads(monkeypatch) as reads:
            finished = await _execute(security, approval_id)
    assert reads["service"] == 0 and reads["executor"] == 0, (
        "执行器在读取前必须核对最新读取权限；写批准不能解除读取禁止"
    )
    assert _was_invalidated(finished), finished
    assert source.read_text(encoding="utf-8") == "秘密原始", "读取被禁止时不得写回"


# ---------------------------------------------------------------------------
# E1：最终真实目标必须参与限制/绑定核对（不检查旧路径、写新路径）
# ---------------------------------------------------------------------------


async def test_write_back_rejects_relocated_forbidden_hardlink_target(
    tmp_path, db_conn
):
    """E1：等待审批期间引用改到禁止目录中的同一对象硬链接 → 不写任何路径。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    allowed = tmp_path / "workspace" / "allowed"
    banned = tmp_path / "workspace" / "banned"
    banned.mkdir(parents=True, exist_ok=True)
    source = allowed / "文档.txt"
    att = _write_source(security.service, source, "旧内容")
    async with _bound_requirement(
        security,
        limits=[
            {
                "operation": "write",
                "objects": [{"kind": "path_prefix", "value": str(banned)}],
            }
        ],
    ) as (_requirement, _binding):
        pending = await security.service.write_back_source(
            att.id, "新内容".encode("utf-8"), gateway=security.engine
        )
        approval_id = _approval_id(pending)
        # 同一对象（同一文件身份）在禁止目录里建硬链接，并把附件引用改过去。
        hardlink = banned / "文档-链接.txt"
        os.link(source, hardlink)
        assert _hardlink_same_object(source, hardlink)
        relocated = security.service.relocate_reference(att.id, str(hardlink))
        assert Path(relocated.source_path) == hardlink
        await _approve(security, approval_id)
        finished = await _execute(security, approval_id)
    assert _was_invalidated(finished), finished
    assert source.read_text(encoding="utf-8") == "旧内容"
    assert hardlink.read_text(encoding="utf-8") == "旧内容", (
        "不得检查审批时的旧路径、实际把内容写进禁止目录里的新路径"
    )


async def test_write_back_with_unchanged_reference_completes_despite_other_limits(
    tmp_path, db_conn
):
    """E1 相邻正常放行：目标未变时按真实目标核对限制（禁止目录与本次无关）。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    source = tmp_path / "workspace" / "allowed" / "文档.txt"
    banned = tmp_path / "workspace" / "banned"
    banned.mkdir(parents=True, exist_ok=True)
    att = _write_source(security.service, source, "旧内容")
    async with _bound_requirement(
        security,
        limits=[
            {
                "operation": "write",
                "objects": [{"kind": "path_prefix", "value": str(banned)}],
            }
        ],
    ) as (_requirement, _binding):
        pending = await security.service.write_back_source(
            att.id, "新内容".encode("utf-8"), gateway=security.engine
        )
        approval_id = _approval_id(pending)
        await _approve(security, approval_id)
        finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert source.read_text(encoding="utf-8") == "新内容"
    assert list(banned.iterdir()) == [], "禁止目录不应被写入任何对象"


def _hardlink_same_object(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# E2：身份 / 读取前内容 / 拟写入内容分别表达；正常写回/回收不得误判为变化
# ---------------------------------------------------------------------------


async def test_unrestricted_write_back_of_unchanged_file_completes(
    tmp_path, db_conn, monkeypatch
):
    """E2：完全无限制模式、文件无外部变化 → 正常写回完成（不是"内容变化"）。"""
    security = _wire_env(tmp_path, db_conn, mode="unrestricted")
    source = tmp_path / "workspace" / "报告.txt"
    att = _write_source(security.service, source, "旧内容")
    with _count_content_reads(monkeypatch) as reads:
        async with _bound_requirement(security) as (_requirement, _binding):
            outcome = await security.service.write_back_source(
                att.id, "新内容".encode("utf-8"), gateway=security.engine
            )
    assert outcome.get("ok") is True, outcome
    assert source.read_text(encoding="utf-8") == "新内容"
    assert reads["service"] >= 1, "允许直接写回时仍应读取一次覆盖前内容用于绑定"
    assert _pending_rows(security) == [], "完全无限制模式的普通写回不增加常规审批"


async def test_write_back_invalidates_when_content_really_changed_during_wait(
    tmp_path, db_conn
):
    """E2：等待期间内容真实变化 → 旧方案失效，不覆盖新内容。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    source = tmp_path / "workspace" / "报告.txt"
    att = _write_source(security.service, source, "旧内容")
    async with _bound_requirement(security) as (_requirement, _binding):
        pending = await security.service.write_back_source(
            att.id, "审批时的新内容".encode("utf-8"), gateway=security.engine
        )
        approval_id = _approval_id(pending)
        source.write_text("等待期间被外部改掉的内容", encoding="utf-8")
        await _approve(security, approval_id)
        finished = await _execute(security, approval_id)
    assert _was_invalidated(finished), finished
    assert source.read_text(encoding="utf-8") == "等待期间被外部改掉的内容"


async def test_unrestricted_recycle_of_unchanged_material_completes(
    tmp_path, db_conn, monkeypatch
):
    """E2：回收流程不得把身份字段当内容摘要（文件未变化时正常进回收站）。"""
    security = _wire_env(tmp_path, db_conn, mode="unrestricted")
    material = tmp_path / "workspace" / "材料.txt"
    att = _write_source(security.service, material, "材料内容")
    with _fake_trash(monkeypatch) as trashed:
        async with _bound_requirement(security) as (_requirement, _binding):
            result = await security.service.recycle_material_files(
                [{"attachment_id": att.id, "path": str(material), "reason": "整理"}],
                gateway=security.engine,
            )
    assert result.get("pending_approval") is False, result
    assert [item["path"] for item in result["results"]] == [str(material)]
    assert result["results"][0]["ok"] is True, result
    assert trashed == [str(material)], "文件未变化时应正常移入回收站"
    assert not material.exists()


async def test_recycle_invalidates_when_content_changed_during_wait(
    tmp_path, db_conn, monkeypatch
):
    """E2：等待期间内容真实变化 → 回收项失效、文件保留。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    material = tmp_path / "workspace" / "材料.txt"
    att = _write_source(security.service, material, "材料内容")
    with _fake_trash(monkeypatch) as trashed:
        async with _bound_requirement(security) as (_requirement, _binding):
            pending = await security.service.recycle_material_files(
                [{"attachment_id": att.id, "path": str(material), "reason": "整理"}],
                gateway=security.engine,
            )
            approval_id = _approval_id(pending)
            material.write_text("等待期间的新内容", encoding="utf-8")
            await _approve(security, approval_id)
            finished = await _execute(security, approval_id)
    assert _was_invalidated(finished), finished
    assert trashed == [], "内容变化后旧回收方案不得执行"
    assert material.read_text(encoding="utf-8") == "等待期间的新内容"


# ---------------------------------------------------------------------------
# E3：批量逐项；单项失效只影响对应项
# ---------------------------------------------------------------------------


async def test_batch_write_back_single_item_ban_only_blocks_that_item(
    tmp_path, db_conn
):
    """E3：等待期间只新增对 B 的写禁止 → A 仍执行，B 失效保留。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    first = tmp_path / "workspace" / "a.txt"
    second = tmp_path / "workspace" / "b.txt"
    att_a = _write_source(security.service, first, "A-旧")
    att_b = _write_source(security.service, second, "B-旧")
    async with _bound_requirement(security) as (requirement, _binding):
        pending = await security.service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"A-\xe6\x96\xb0"},
                {"attachment_id": att_b.id, "data": b"B-\xe6\x96\xb0"},
            ],
            gateway=security.engine,
        )
        approval_id = _approval_id(pending)
        _add_limit(
            security,
            requirement,
            {"operation": "write", "objects": [{"kind": "path", "value": str(second)}]},
        )
        await _approve(security, approval_id)
        finished = await _execute(security, approval_id)
    detail = dict(finished.get("result") or {})
    items = {item["attachment_id"]: item for item in detail.get("items") or []}
    assert first.read_text(encoding="utf-8") == "A-新", "独立有效项必须继续执行"
    assert second.read_text(encoding="utf-8") == "B-旧", "单项限制只使对应项失效"
    assert items.get(att_a.id, {}).get("status") == "completed", items
    assert items.get(att_b.id, {}).get("status") == "invalidated", items
    assert finished["state"] in ("completed", "failed"), finished


async def test_batch_write_back_common_requirement_withdrawal_stops_whole_batch(
    tmp_path, db_conn
):
    """E3 共同条件：绑定要求被撤回 → 整批停止（不能逐项执行）。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    first = tmp_path / "workspace" / "a.txt"
    second = tmp_path / "workspace" / "b.txt"
    att_a = _write_source(security.service, first, "A-旧")
    att_b = _write_source(security.service, second, "B-旧")
    async with _bound_requirement(security) as (requirement, _binding):
        pending = await security.service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"A-\xe6\x96\xb0"},
                {"attachment_id": att_b.id, "data": b"B-\xe6\x96\xb0"},
            ],
            gateway=security.engine,
        )
        approval_id = _approval_id(pending)
        await _approve(security, approval_id)
        security.requirements.withdraw(requirement.requirement_id)
        finished = await _execute(security, approval_id)
    assert _was_invalidated(finished), finished
    assert first.read_text(encoding="utf-8") == "A-旧"
    assert second.read_text(encoding="utf-8") == "B-旧"


# ---------------------------------------------------------------------------
# §8：内部维护三次确认（审批提交 / 批准 / 恢复 / 执行前）
# ---------------------------------------------------------------------------


def _internal_target(tmp_path) -> tuple[Path, Path]:
    internal_dir = tmp_path / "qio-internal"
    internal_dir.mkdir(exist_ok=True)
    log = internal_dir / "app.log"
    log.write_text("旧日志", encoding="utf-8")
    return internal_dir, log


async def test_internal_log_write_back_requires_three_actual_confirmations(
    tmp_path, db_conn, monkeypatch
):
    """§8：附件写回内部日志必须三次确认；确认前不读受保护正文，执行只写一次。"""
    internal_dir, log = _internal_target(tmp_path)
    security = _wire_env(tmp_path, db_conn, mode="restricted", protected=[internal_dir])
    att = _write_source(security.service, log, "旧日志")
    with _count_content_reads(monkeypatch) as reads:
        async with _bound_requirement(security) as (_requirement, _binding):
            pending = await security.service.write_back_source(
                att.id, "新日志".encode("utf-8"), gateway=security.engine
            )
            approval_id = _approval_id(pending)
            row = _row(security, approval_id)
            assert _require_confirmations(row) == 3, (
                "内部运行数据的最低确认次数必须由后端按真实对象推导"
            )
            with pytest.raises(Exception) as first_try:
                await _approve(security, approval_id)
            assert "确认" in str(first_try.value), first_try.value
            assert log.read_text(encoding="utf-8") == "旧日志", "三次确认前不得修改内部数据"
            reads_before_confirm = dict(reads)
            first = await security.approvals.confirm(approval_id, 1)
            assert first["state"] == "waiting"
            assert int(first["confirmed_stage"]) == 1
            assert log.read_text(encoding="utf-8") == "旧日志"
            second = await security.approvals.confirm(approval_id, 2)
            assert second["state"] == "waiting"
            assert int(second["confirmed_stage"]) == 2
            third = await security.approvals.confirm(approval_id, 3)
            assert third["state"] == "approved"
            assert int(third["confirmed_stage"]) == 3
            finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert log.read_text(encoding="utf-8") == "新日志"
    assert reads_before_confirm["service"] == 0 and reads_before_confirm["executor"] == 0, (
        "内部维护在确认完成前不得读取受保护正文/摘要"
    )
    assert reads["service"] == 0 and reads["executor"] == 0, (
        "内部维护写回只需要元数据核对，不得读取正文"
    )
    # 不增加第四次确认：三次确认后直接可执行。
    assert int(finished["confirmed_stage"]) == 3


async def test_historical_single_confirmation_row_cannot_act_as_three(tmp_path, db_conn):
    """§8.4：历史/错误的一次确认记录不能当三次；批准/执行均按后端真实最低次数核对。"""
    internal_dir, log = _internal_target(tmp_path)
    security = _wire_env(tmp_path, db_conn, mode="restricted", protected=[internal_dir])
    new_content = "修复后的日志"
    digests = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    effect = security_gate.write_effect(
        "fs.write",
        log,
        purpose="内部日志维护",
        content_sha256=digests,
        exists=True,
        detail={"action": "write"},
        internal=True,
    )
    operation = PendingOperation(
        kind="fs.write",
        label="内部日志维护",
        payload={"path": str(log), "content": new_content},
        effect=security_gate.effect_to_dict(effect),
        fingerprint={"path": str(log), "content_sha256": digests},
        confirmations_required=1,  # 历史错误行：调用方/旧存储写成了 1
        source="tool",
        mode_at_request="restricted",
    )
    async with _bound_requirement(security) as (requirement, _binding):
        operation.requirement_id = requirement.requirement_id
        operation.requirement_version = int(requirement.version)
        operation.fingerprint["operation_digest"] = operation_digest(operation)
        row = security.approvals.store.create(operation)
        approval_id = row["approval_id"]
        # 1) 批准阶段：后端按真实对象重算 → 0 次确认不能批准
        with pytest.raises(Exception) as refused:
            await _approve(security, approval_id)
        assert "确认" in str(refused.value), refused.value
        assert log.read_text(encoding="utf-8") == "旧日志"
        # 2) 恢复/执行阶段：把历史行伪造成"已批准" → 执行器仍必须拒绝
        db_conn.execute(
            "UPDATE pending_approvals SET state='approved', status='approved'"
            " WHERE approval_id=?",
            (approval_id,),
        )
        db_conn.commit()
        finished = await _execute(security, approval_id)
        assert _was_invalidated(finished), finished
        assert log.read_text(encoding="utf-8") == "旧日志", "一次确认不能当三次执行"
        # 3) 按后端要求补足三次确认后正常执行（不增加第四次）
        db_conn.execute(
            "UPDATE pending_approvals SET state='waiting', status='pending'"
            " WHERE approval_id=?",
            (approval_id,),
        )
        db_conn.commit()
        await security.approvals.confirm(approval_id, 1)
        await security.approvals.confirm(approval_id, 2)
        third = await security.approvals.confirm(approval_id, 3)
        assert third["state"] == "approved"
        finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert log.read_text(encoding="utf-8") == new_content


async def test_batch_write_back_internal_second_item_needs_three_confirmations(
    tmp_path, db_conn
):
    """§8.5：批量里内部对象不是首项时，同样要求真实的连续三次确认。"""
    internal_dir, log = _internal_target(tmp_path)
    security = _wire_env(tmp_path, db_conn, mode="restricted", protected=[internal_dir])
    ordinary = tmp_path / "workspace" / "普通.txt"
    att_a = _write_source(security.service, ordinary, "普通旧")
    att_b = _write_source(security.service, log, "旧日志")
    async with _bound_requirement(security) as (_requirement, _binding):
        pending = await security.service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"\xe6\x99\xae\xe9\x80\x9a\xe6\x96\xb0"},
                {"attachment_id": att_b.id, "data": b"\xe6\x96\xb0\xe6\x97\xa5\xe5\xbf\x97"},
            ],
            gateway=security.engine,
        )
        approval_id = _approval_id(pending)
        row = _row(security, approval_id)
        assert _require_confirmations(row) == 3, "批量里的内部对象（非首项）也必须命中"
        with pytest.raises(Exception):
            await _approve(security, approval_id)
        assert ordinary.read_text(encoding="utf-8") == "普通旧"
        assert log.read_text(encoding="utf-8") == "旧日志"
        await security.approvals.confirm(approval_id, 1)
        await security.approvals.confirm(approval_id, 2)
        await security.approvals.confirm(approval_id, 3)
        finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert ordinary.read_text(encoding="utf-8") == "普通新"
    assert log.read_text(encoding="utf-8") == "新日志"


async def test_model_allow_cannot_complete_internal_but_ordinary_write_back_allowed(
    tmp_path, db_conn
):
    """§8.2：模型 ALLOW 不能完成内部维护；普通受控写回仍按既有模型授权规则放行。"""
    internal_dir, log = _internal_target(tmp_path)
    reviewer = FakeReviewer()
    security = _wire_env(
        tmp_path, db_conn, mode="restricted", protected=[internal_dir], reviewer=reviewer
    )
    att_log = _write_source(security.service, log, "旧日志")
    ordinary = tmp_path / "workspace" / "普通.txt"
    att_plain = _write_source(security.service, ordinary, "普通旧")
    async with _bound_requirement(security) as (_requirement, _binding):
        internal_result = await security.service.write_back_source(
            att_log.id, "新日志".encode("utf-8"), gateway=security.engine
        )
        approval_id = _approval_id(internal_result)
        row = _row(security, approval_id)
        assert _require_confirmations(row) == 3
        assert log.read_text(encoding="utf-8") == "旧日志", (
            "模型 ALLOW 不得直接完成内部维护，必须等三段人工确认"
        )
        plain_result = await security.service.write_back_source(
            att_plain.id, "普通新".encode("utf-8"), gateway=security.engine
        )
    assert plain_result.get("pending_approval") is not True, plain_result
    assert ordinary.read_text(encoding="utf-8") == "普通新"
    assert reviewer.calls, "普通写回的模型授权审查仍然生效（不因内部维护收紧而失效）"


async def test_three_confirmations_cannot_override_user_prohibition(tmp_path, db_conn):
    """§8.6：三次确认不能覆盖用户明确禁止。"""
    internal_dir, log = _internal_target(tmp_path)
    security = _wire_env(tmp_path, db_conn, mode="restricted", protected=[internal_dir])
    att = _write_source(security.service, log, "旧日志")
    async with _bound_requirement(security) as (requirement, _binding):
        pending = await security.service.write_back_source(
            att.id, "新日志".encode("utf-8"), gateway=security.engine
        )
        approval_id = _approval_id(pending)
        await security.approvals.confirm(approval_id, 1)
        await security.approvals.confirm(approval_id, 2)
        # 第三次确认前出现明确写禁止：确认完成也不能执行。
        _add_limit(
            security,
            requirement,
            {"operation": "write", "objects": [{"kind": "path", "value": str(log)}]},
        )
        third = await security.approvals.confirm(approval_id, 3)
        assert third["state"] == "approved"
        finished = await _execute(security, approval_id)
    assert _was_invalidated(finished), finished
    assert log.read_text(encoding="utf-8") == "旧日志", "内部维护确认不能覆盖用户禁止"


# ---------------------------------------------------------------------------
# §12（适配层）：模式变化按当前规则判断，不因模式字符串不同整体失效
# ---------------------------------------------------------------------------


async def test_approved_batch_mode_relaxed_to_unrestricted_still_executes_once(
    tmp_path, db_conn
):
    """§12：restricted 批准的批量写回，放宽到 unrestricted 后仍按当前规则继续。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    first = tmp_path / "workspace" / "a.txt"
    second = tmp_path / "workspace" / "b.txt"
    att_a = _write_source(security.service, first, "A-旧")
    att_b = _write_source(security.service, second, "B-旧")
    async with _bound_requirement(security) as (_requirement, _binding):
        pending = await security.service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"A-\xe6\x96\xb0"},
                {"attachment_id": att_b.id, "data": b"B-\xe6\x96\xb0"},
            ],
            gateway=security.engine,
        )
        approval_id = _approval_id(pending)
        await _approve(security, approval_id)  # 批准时仍是 restricted
        security.store.set("computer.permission_mode", "unrestricted")
        finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert first.read_text(encoding="utf-8") == "A-新"
    assert second.read_text(encoding="utf-8") == "B-新"
    # 只执行一次：再次触发执行不重复写。
    await security.approvals._execute_approved(approval_id)
    assert first.read_text(encoding="utf-8") == "A-新"


async def test_approved_batch_mode_tightened_to_read_only_stops(tmp_path, db_conn):
    """§12：收紧到只读后，当前规则不允许的执行必须停止（且不写任何文件）。"""
    security = _wire_env(tmp_path, db_conn, mode="restricted")
    first = tmp_path / "workspace" / "a.txt"
    second = tmp_path / "workspace" / "b.txt"
    att_a = _write_source(security.service, first, "A-旧")
    att_b = _write_source(security.service, second, "B-旧")
    async with _bound_requirement(security) as (_requirement, _binding):
        pending = await security.service.write_back_many(
            [
                {"attachment_id": att_a.id, "data": b"A-\xe6\x96\xb0"},
                {"attachment_id": att_b.id, "data": b"B-\xe6\x96\xb0"},
            ],
            gateway=security.engine,
        )
        approval_id = _approval_id(pending)
        await _approve(security, approval_id)
        security.store.set("computer.permission_mode", "read_only")
        finished = await _execute(security, approval_id)
    assert _was_invalidated(finished), finished
    assert first.read_text(encoding="utf-8") == "A-旧"
    assert second.read_text(encoding="utf-8") == "B-旧"


async def test_approved_program_mode_change_uses_current_rules(tmp_path, db_conn):
    """§12（approved_operations 适配层）：程序审批不能仅因模式字符串变化失效。

    真实流程：完全无限制模式 + 历史未恢复限制 → 执行程序需要具体确认（APPROVE，
    审批单记录 mode_at_request=unrestricted）；批准后切回 restricted：
    * 仍有有效人工批准，按当前规则允许 → 程序真实启动一次；
    * 执行前切到 read_only：当前规则不允许 → 停止（不得启动）。
    """
    marker = tmp_path / "workspace" / "ran.txt"
    code = "open(r'%s', 'w', encoding='utf-8').write('ran')" % str(marker)

    security = _wire_env(tmp_path, db_conn, mode="unrestricted")
    computer = ComputerSandbox(
        resolve_root=lambda: str(security.workspace),
        permission_mode=lambda: security.engine.mode().value,
    )
    _register_operations(security, security.service, computer=computer)
    tool = RunProgramTool(
        computer=computer, security=security.engine, approvals=security.approvals
    )
    async with _bound_requirement(security) as (requirement, _binding):
        # 历史限制未可靠恢复：unrestricted 下对相关操作只给具体确认，不自动放行。
        security.requirements.set_structured_limits(
            requirement.requirement_id, None, state="unresolved", source="test_fixture"
        )
        submitted = await tool.run(
            program=sys.executable,
            args=["-c", code],
            cwd=str(security.workspace),
            timeout=30,
        )
        assert submitted.code == "pending_approval", (submitted.error, submitted.content)
        approval_id = _pending_rows(security)[-1]["approval_id"]
        await _approve(security, approval_id)  # 批准时仍是 unrestricted
        security.store.set("computer.permission_mode", "restricted")
        finished = await _execute(security, approval_id)
    assert finished["state"] == "completed", finished
    assert marker.read_text(encoding="utf-8") == "ran", "程序必须真实启动一次"

    # 收紧到只读：必须停止，程序不得启动。
    marker2 = tmp_path / "ro" / "workspace" / "ran.txt"
    code2 = "open(r'%s', 'w', encoding='utf-8').write('ran')" % str(marker2)
    security2 = _wire_env(tmp_path / "ro", db_conn, mode="unrestricted")
    computer2 = ComputerSandbox(
        resolve_root=lambda: str(security2.workspace),
        permission_mode=lambda: security2.engine.mode().value,
    )
    _register_operations(security2, security2.service, computer=computer2)
    tool2 = RunProgramTool(
        computer=computer2, security=security2.engine, approvals=security2.approvals
    )
    async with _bound_requirement(security2) as (requirement2, _binding2):
        security2.requirements.set_structured_limits(
            requirement2.requirement_id, None, state="unresolved", source="test_fixture"
        )
        submitted = await tool2.run(
            program=sys.executable,
            args=["-c", code2],
            cwd=str(security2.workspace),
            timeout=30,
        )
        assert submitted.code == "pending_approval", (submitted.error, submitted.content)
        approval_id = _pending_rows(security2)[-1]["approval_id"]
        await _approve(security2, approval_id)
        security2.store.set("computer.permission_mode", "read_only")
        finished = await _execute(security2, approval_id)
    assert _was_invalidated(finished), finished
    assert not marker2.exists(), "只读模式下不得启动程序"
