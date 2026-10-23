"""R9-H B：审查对象组装 / 内容绑定 / 授权复用的真实接线证据。

原则（契约 §4/§9）：
* 测试通过**真实工具/执行器**产生的 effect.detail，再用**真实 AuthorizationEngine
  ._review_operation** 组装 ReviewOperation；不手填 content_digest 当作接线证据；
* 相同文件身份不等于相同内容：content A 允许后 content B 不得复用旧记录；
* 同路径脚本内容/内联代码/argv/cwd 变化、前置状态变化、删除方式都要进入指纹；
* 审查请求（模型看到的 payload）也包含这些绑定事实；
* 审批恢复执行器使用审批单绑定的内容与前置状态，不重新生成。

模型调用全部是 FakeReviewer（不联网、不使用真实 Key）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.api.bus import EventBus
from agent.security.context import current_requirement
from agent.security.effects import AuthDecision, Effect, EffectKind, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.operations import ExecutionContext, PendingOperation
from agent.security.requirements import AuthorizationLedger, RequirementLedger
from agent.security.review import (
    ReviewDecision,
    ReviewOutcome,
    build_review_payload,
    operation_fingerprint,
)
from agent.services.computer import ComputerSandbox
from agent.storage.settings import SettingsStore
from agent.tools import security_gate
from agent.tools.approval import ApprovalService
from agent.tools.approved_operations import execute_fs_delete, execute_fs_write
from agent.tools.cmd_tools import RunProgramTool
from agent.tools.fs_tools import FsDeleteTool, FsPatchTool, FsWriteTool

PY = sys.executable


# --------------------------------------------------------------------------
# Fake 模型审查器（只记录请求/按队列返回，不联网）
# --------------------------------------------------------------------------


class FakeReviewer:
    """默认返回 ALLOW（allowed_operations/objects 来自请求）；可排队覆盖。"""

    def __init__(self, *, outcomes=None) -> None:
        self.calls: list = []
        self.outcomes = list(outcomes or [])

    async def review(self, request):
        self.calls.append(request)
        if not self.outcomes:
            return allow_outcome(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome(request)
        return outcome


def allow_outcome(request):
    operation = request.operation
    return ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="用户明确要求修改这个文件并保存",
        requirement_id=request.requirement.requirement_id,
        allowed_operations=(operation.kind,),
        objects=operation.objects,
        limits=("仅限本次操作",),
        purpose=operation.summary or "用户要求",
    )


# --------------------------------------------------------------------------
# 真实装配：引擎 + 台账 + 审批（SQLite）；工具注入同一引擎
# --------------------------------------------------------------------------


def make_engine(tmp_path, conn, *, mode="restricted", reviewer=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    requirements = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    approvals = ApprovalService(EventBus(), conn=conn)
    engine = AuthorizationEngine(
        conn=conn,
        approvals=approvals,
        settings=store,
        instance_id="inst_test",
        workspace_root=lambda: workspace,
        requirements=requirements,
        authorizations=authorizations,
        reviewer=reviewer,
    )
    approvals.set_engine(engine)
    requirement = requirements.record_user_message(
        text="把项目报告改成新的内容并保存",
        message_id="msg_1",
        turn_id="turn_1",
        topic_id="topic_1",
    )
    binding = requirements.binding_for(requirement.requirement_id)
    computer = ComputerSandbox(
        resolve_root=lambda: str(workspace),
        permission_mode=lambda: "restricted",
    )
    return SimpleNamespace(
        engine=engine,
        requirements=requirements,
        authorizations=authorizations,
        approvals=approvals,
        reviewer=reviewer,
        requirement=requirement,
        binding=binding,
        workspace=workspace,
        computer=computer,
    )


@pytest.fixture()
def reviewer():
    return FakeReviewer()


@pytest.fixture()
def security(tmp_path, db_conn, reviewer):
    return make_engine(tmp_path, db_conn, reviewer=reviewer)


@pytest.fixture()
def bound(security):
    token = current_requirement.set(security.binding)
    try:
        yield security
    finally:
        current_requirement.reset(token)


# --------------------------------------------------------------------------
# 等待审批的捕获网关（复用真实工具 effect，再交给真实引擎组装）
# --------------------------------------------------------------------------


class CaptureGateway:
    """捕获真实工具 effect；可选 review_engine 让工具指纹使用真实引擎组装。"""

    def __init__(
        self, *, verdict="approve", mode="restricted", review_engine=None, requirement=None
    ) -> None:
        self.verdict = verdict
        self._mode = mode
        self.review_engine = review_engine
        #: 与真实 engine.submit 一样，把当前要求绑定写进 PendingOperation。
        self.requirement = requirement
        self.effects: list = []
        self.submitted: list = []

    def _review_operation(self, effect):
        if self.review_engine is None:
            raise RuntimeError("CaptureGateway 没有配置 review_engine")
        return self.review_engine._review_operation(effect)

    def mode(self):
        return self._mode

    def instruction(self):
        return None

    async def authorize(self, effect, ctx=None):
        self.effects.append(effect)
        return AuthDecision(verdict=Verdict(self.verdict), reason="capture:" + self.verdict)

    async def submit(self, operation, ctx=None):
        if self.requirement is not None and not getattr(operation, "requirement_id", None):
            operation.requirement_id = str(
                getattr(self.requirement, "requirement_id", "") or ""
            ) or None
            version = getattr(self.requirement, "version", None)
            operation.requirement_version = int(version) if version is not None else None
        self.submitted.append(operation)
        return SimpleNamespace(
            approval_id="appr_r9h",
            state="waiting",
            expires_at="2099-01-01T00:00:00+00:00",
            confirmations_required=int(operation.confirmations_required or 1),
        )


def review_of_effect(engine, effect) -> object:
    """真实引擎从真实 effect.detail 组装 ReviewOperation（唯一证据来源）。"""
    return engine._review_operation(effect)


def effect_from_pending(operation) -> Effect:
    restored = security_gate.effect_from_dict(dict(operation.effect or {}))
    assert restored is not None, "审批单 effect 快照无法还原"
    return restored


def context_for(operation, security) -> ExecutionContext:
    return ExecutionContext(
        approval_id="appr_r9h",
        operation=operation,
        mode_at_request=str(operation.mode_at_request or "restricted"),
        mode_at_approval="restricted",
        engine=security.engine,
        mode_getter=lambda: "restricted",
    )


# --------------------------------------------------------------------------
# 1) fs_write：真实工具 effect → 真实引擎组装；content A 允许后 B 不复用
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_write_content_digest_comes_from_real_effect_and_changes_reuse_key(
    bound, tmp_path
):
    tool = FsWriteTool(
        computer=bound.computer,
        security=bound.engine,
        approvals=bound.approvals,
    )

    first = await tool.run(path="报告.md", content="结论 A")
    assert first.ok is True, (first.error, first.content)
    assert len(bound.reviewer.calls) == 1
    request = bound.reviewer.calls[0]
    expected_a = hashlib.sha256("结论 A".encode("utf-8")).hexdigest()
    assert request.operation.content_digest == expected_a
    assert request.operation.precondition_digest, "新文件也必须有 absent 前置状态摘要"
    payload = build_review_payload(request)
    assert payload["operation"]["content_digest"] == expected_a
    assert payload["operation"]["precondition_digest"] == request.operation.precondition_digest

    second = await tool.run(path="报告.md", content="结论 B")
    assert second.ok is True, (second.error, second.content)
    assert len(bound.reviewer.calls) == 2, "内容 A 的授权不能复用给内容 B"
    expected_b = hashlib.sha256("结论 B".encode("utf-8")).hexdigest()
    assert bound.reviewer.calls[1].operation.content_digest == expected_b
    assert bound.reviewer.calls[1].operation.content_digest != expected_a
    assert (
        bound.reviewer.calls[1].operation.precondition_digest
        != bound.reviewer.calls[0].operation.precondition_digest
    ), "覆盖前内容变化必须改变前置状态摘要"

    records = bound.authorizations.records_for_requirement(
        bound.requirement.requirement_id
    )
    fingerprints = {getattr(item, "fingerprint", "") for item in records}
    assert len(fingerprints) == 2, "两条不同内容的授权记录必须使用不同指纹"


@pytest.mark.asyncio
async def test_fs_patch_binds_resulting_content_and_precondition(security, tmp_path):
    target = security.workspace / "报告.md"
    target.write_text("结论：旧", encoding="utf-8")
    token = current_requirement.set(security.binding)
    try:
        tool = FsPatchTool(
            computer=security.computer,
            security=security.engine,
            approvals=security.approvals,
        )
        result = await tool.run(path="报告.md", old="旧", new="新")
        assert result.ok is True, (result.error, result.content)
        request = security.reviewer.calls[-1]
        updated = "结论：新"
        assert request.operation.content_digest == hashlib.sha256(
            updated.encode("utf-8")
        ).hexdigest()
        assert request.operation.precondition_digest
        assert target.read_text(encoding="utf-8") == updated
    finally:
        current_requirement.reset(token)


@pytest.mark.asyncio
async def test_same_effect_same_content_precondition_can_reuse_even_with_digests(
    bound, tmp_path
):
    """相同内容、相同前置状态、相同对象：有效期内正常复用（不机械重复审查）。"""
    gateway = CaptureGateway(verdict="allow")
    tool = FsWriteTool(computer=bound.computer, security=gateway)
    result = await tool.run(path="复用.txt", content="同一内容")
    assert result.ok is True, (result.error, result.content)
    effect = gateway.effects[0]

    # effect 来自真实工具；引擎真实组装并复用同一 fingerprint 的授权记录。
    first = await bound.engine.authorize(effect)
    second = await bound.engine.authorize(effect)
    assert first.verdict == Verdict.ALLOW and second.verdict == Verdict.ALLOW
    assert len(bound.reviewer.calls) == 1, "相同内容/前置状态/对象应复用已确认记录"


# --------------------------------------------------------------------------
# 2) fs_delete：删除方式 + 删除前状态进入审查对象
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_delete_effect_carries_delete_mode_and_precondition(bound, tmp_path):
    victim = bound.workspace / "待清理.txt"
    victim.write_text("旧内容", encoding="utf-8")
    expected_content = hashlib.sha256("旧内容".encode("utf-8")).hexdigest()
    gateway = CaptureGateway(verdict="approve", review_engine=bound.engine, requirement=bound.binding)
    tool = FsDeleteTool(computer=bound.computer, security=gateway)
    result = await tool.run(paths=[str(victim)])
    assert result.code == "pending_approval"
    operation = gateway.submitted[-1]
    effect = effect_from_pending(operation)
    review_operation = review_of_effect(bound.engine, effect)

    assert review_operation.delete_mode == "recycle_bin"
    assert review_operation.kind == "delete"
    assert review_operation.precondition_digest
    fingerprint = dict(operation.fingerprint.get("operation_binding") or {})
    assert fingerprint.get("fingerprint") == operation_fingerprint(
        review_operation, mode="restricted"
    )
    item = (operation.fingerprint.get("items") or [])[0]
    assert item.get("content_sha256") == expected_content
    assert item.get("delete_mode") == "recycle_bin"
    assert victim.exists(), "待审批不删除"


@pytest.mark.asyncio
async def test_fs_delete_replay_invalidates_when_content_changed_after_approval(
    bound, tmp_path
):
    victim = bound.workspace / "审批后变化.txt"
    victim.write_text("原始内容", encoding="utf-8")
    gateway = CaptureGateway(verdict="approve", requirement=bound.binding)
    tool = FsDeleteTool(computer=bound.computer, security=gateway)
    result = await tool.run(paths=[str(victim)])
    assert result.code == "pending_approval"
    operation = gateway.submitted[-1]

    # 审批之后用户在外部修改了同一文件（OS 身份不变，内容变了）
    victim.write_text("用户刚改的内容", encoding="utf-8")
    token = current_requirement.set(bound.binding)
    try:
        executed = await execute_fs_delete(operation.payload, context_for(operation, bound))
    finally:
        current_requirement.reset(token)
    assert executed.ok is False
    results = executed.detail.get("results") or []
    assert results and (
        "内容" in str(results[0].get("reason") or "")
        or "invalidated" in str(results[0].get("code") or "")
    ), results
    assert victim.exists(), "内容变化的文件不得用旧审批删除"


# --------------------------------------------------------------------------
# 3) 命令：脚本文件内容 / 内联代码 / cwd 变化都进入复用键
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_script_content_change_prevents_reuse(bound, tmp_path):
    script = bound.workspace / "任务.py"
    script.write_text("print('A')", encoding="utf-8")
    tool = RunProgramTool(
        computer=bound.computer,
        security=bound.engine,
        approvals=bound.approvals,
    )
    first = await tool.run(program=PY, args=[str(script)], cwd=str(bound.workspace))
    assert first.ok is True, (first.error, first.content)
    assert len(bound.reviewer.calls) == 1
    first_operation = bound.reviewer.calls[0].operation
    assert first_operation.script_sha256, "脚本文件内容摘要必须进入审查对象"
    assert first_operation.spec_digest, "程序/argv/cwd/脚本摘要必须进入审查对象"

    script.write_text("print('B')", encoding="utf-8")
    second = await tool.run(program=PY, args=[str(script)], cwd=str(bound.workspace))
    assert second.ok is True, (second.error, second.content)
    assert len(bound.reviewer.calls) == 2, "同路径脚本内容变化不得复用旧授权"
    second_operation = bound.reviewer.calls[1].operation
    assert second_operation.script_sha256 != first_operation.script_sha256
    assert second_operation.spec_digest != first_operation.spec_digest


@pytest.mark.asyncio
async def test_inline_code_and_argv_change_prevents_reuse(bound, tmp_path):
    tool = RunProgramTool(
        computer=bound.computer,
        security=bound.engine,
        approvals=bound.approvals,
    )
    first = await tool.run(program=PY, args=["-c", "print(1)"], cwd=str(bound.workspace))
    assert first.ok is True, (first.error, first.content)
    second = await tool.run(program=PY, args=["-c", "print(2)"], cwd=str(bound.workspace))
    assert second.ok is True, (second.error, second.content)
    assert len(bound.reviewer.calls) == 2, "内联代码不同不得复用旧授权"
    assert (
        bound.reviewer.calls[0].operation.spec_digest
        != bound.reviewer.calls[1].operation.spec_digest
    )
    # 审查模型输入也真实包含 spec_digest（不是只加到数据类）
    payload = build_review_payload(bound.reviewer.calls[1])
    assert payload["operation"]["spec_digest"] == bound.reviewer.calls[1].operation.spec_digest


# --------------------------------------------------------------------------
# 4) 附件写回 / 回收：内容与前置状态进入真实引擎组装
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_write_back_binds_new_content_and_old_state(bound, tmp_path):
    from agent.services.attachments import AttachmentService

    service = AttachmentService(bound.engine.conn, tmp_path / "data")
    source = tmp_path / "用户源文件.txt"
    source.write_text("旧内容", encoding="utf-8")
    attachment = service.register_source(path=str(source), source_kind="desktop_path")

    result = await service.write_back_source(
        attachment.id, "新内容".encode("utf-8"), gateway=bound.engine
    )
    assert result["ok"] is True, result
    assert len(bound.reviewer.calls) == 1
    request = bound.reviewer.calls[0]
    new_sha = hashlib.sha256("新内容".encode("utf-8")).hexdigest()
    assert request.operation.content_digest == new_sha
    assert request.operation.precondition_digest
    payload = build_review_payload(request)
    assert payload["operation"]["content_digest"] == new_sha
    assert source.read_text(encoding="utf-8") == "新内容"

    # 再写不同内容：内容/前置状态变化 → 不复用旧授权
    result2 = await service.write_back_source(
        attachment.id, "第三版".encode("utf-8"), gateway=bound.engine
    )
    assert result2["ok"] is True, result2
    assert len(bound.reviewer.calls) == 2
    assert (
        bound.reviewer.calls[1].operation.content_digest
        != bound.reviewer.calls[0].operation.content_digest
    )


@pytest.mark.asyncio
async def test_attachment_recycle_effect_has_delete_mode_and_precondition(bound, tmp_path):
    from agent.services.attachments import AttachmentService

    service = AttachmentService(bound.engine.conn, tmp_path / "data")
    source = tmp_path / "待回收材料.txt"
    source.write_text("材料内容", encoding="utf-8")
    attachment = service.register_source(path=str(source), source_kind="desktop_path")
    expected_sha = hashlib.sha256("材料内容".encode("utf-8")).hexdigest()

    gateway = CaptureGateway(verdict="approve", review_engine=bound.engine, requirement=bound.binding)
    result = await service.recycle_material_files(
        [{"attachment_id": attachment.id, "path": str(source), "reason": "测试"}],
        gateway=gateway,
    )
    assert result.get("pending_approval") is True
    operation = gateway.submitted[-1]
    effect = effect_from_pending(operation)
    review_operation = review_of_effect(bound.engine, effect)
    assert review_operation.delete_mode == "recycle_bin"
    assert review_operation.precondition_digest
    fingerprint = dict(operation.fingerprint.get("operation_binding") or {})
    assert fingerprint.get("fingerprint") == operation_fingerprint(
        review_operation, mode="restricted"
    )
    items = operation.fingerprint.get("items") or []
    assert items and items[0].get("content_sha256") == expected_sha
    assert source.exists(), "待审批不回收"


# --------------------------------------------------------------------------
# 5) 审批恢复执行器：使用绑定的内容/前置状态，不重新生成
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_write_replay_uses_bound_content_and_rejects_content_mismatch(
    bound, tmp_path
):
    target = bound.workspace / "审批写入.txt"
    gateway = CaptureGateway(verdict="approve", requirement=bound.binding)
    tool = FsWriteTool(computer=bound.computer, security=gateway)
    result = await tool.run(path=str(target), content="审批时的内容")
    assert result.code == "pending_approval"
    operation = gateway.submitted[-1]

    token = current_requirement.set(bound.binding)
    try:
        # 正常批准：执行的就是审批单 payload 里的内容
        ok = await execute_fs_write(operation.payload, context_for(operation, bound))
        assert ok.ok is True, (ok.error, ok.summary)
        assert target.read_text(encoding="utf-8") == "审批时的内容"
    finally:
        current_requirement.reset(token)

    # 再来一次：审批单 payload 被改成另一份内容（模拟恢复时内容漂移）→ 不执行
    target2 = bound.workspace / "审批写入2.txt"
    gateway2 = CaptureGateway(verdict="approve", requirement=bound.binding)
    tool2 = FsWriteTool(computer=bound.computer, security=gateway2)
    pending2 = await tool2.run(path=str(target2), content="原始内容")
    assert pending2.code == "pending_approval"
    operation2 = gateway2.submitted[-1]
    tampered = dict(operation2.payload)
    tampered["content"] = "被篡改的内容"
    token = current_requirement.set(bound.binding)
    try:
        rejected = await execute_fs_write(tampered, context_for(operation2, bound))
    finally:
        current_requirement.reset(token)
    assert rejected.ok is False
    assert not target2.exists(), "内容不一致时不得写任何内容"
