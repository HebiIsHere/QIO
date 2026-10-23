# -*- coding: utf-8 -*-
"""R9-H4 B：统一授权引擎、执行前核对、操作事实与子智能体范围。

真实 RequirementLedger / AuthorizationLedger / AuthorizationEngine / RecheckService，
模型审查用 FakeReviewer（可控、计数、可在审查期间修改真实状态），不联网。

覆盖契约 §2/§5/§6/§7/§9：
* _limit_conflict 核对 base operation + detail.limit_impacts + 程序对象 + 目录影响 +
  read/delete/internal_operation；相对路径按 workspace_root / cwd 规范化；限制优先 DENY；
* 绑定要求撤回/替换/版本不一致/不存在不能当作无要求进入 unrestricted 允许；
* 模型审查返回后重新读要求/版本/状态与操作事实，失效结果不采用、不建立授权；
* RecheckResult.decision 正确读取（verdict 属性）、保留调用方对象身份/范围/内容依据；
* 子智能体 scope 不足三种模式都 DENY，普通审查/always-ALLOW 不能覆盖；
* 批量逐项、错误 decision 读取不误报普通失败。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.security.context import AgentScope, requirement_context
from agent.security.effects import AuthDecision, Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.gateway import AuthContext
from agent.security.instructions import Grant, Scope
from agent.security.recheck import OperationBinding, RecheckResult, RecheckService
from agent.security.requirements import (
    AuthorizationLedger,
    RequirementBinding,
    RequirementLedger,
)
from agent.security.resolution import RequirementRelation, ResolutionOutcome
from agent.security.review import ReviewDecision, ReviewOutcome, ReviewRequest
from agent.storage.settings import SettingsStore

_NOW = "2026-10-23T00:00:00+00:00"


# --------------------------------------------------------------------------
# 真实装配与 fake 审查器
# --------------------------------------------------------------------------


class FakeReviewer:
    """计数型审查替身；on_review 可在真实审查等待期间修改台账/effect。"""

    def __init__(self, decision: ReviewDecision = ReviewDecision.ALLOW) -> None:
        self.decision = decision
        self.calls: list[ReviewRequest] = []
        self.on_review = None

    async def review(self, request: ReviewRequest) -> ReviewOutcome:
        self.calls.append(request)
        if callable(self.on_review):
            result = self.on_review(request)
            if hasattr(result, "__await__"):
                await result
        return ReviewOutcome(
            decision=self.decision,
            reason="fake reviewer " + self.decision.value,
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(request.operation.kind,),
            objects=tuple(request.operation.objects),
            limits=("fake reviewer 允许本次测试操作",),
            purpose="fake reviewer",
        )


def make_security(
    conn,
    tmp_path: Path,
    *,
    mode: str = "unrestricted",
    reviewer=None,
    protected=(),
):
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    ledger = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    engine = AuthorizationEngine(
        conn=conn,
        approvals=None,
        settings=store,
        instance_id="inst_r9h4_b",
        workspace_root=lambda: workspace,
        internal_data_paths=lambda: [Path(item) for item in protected],
        requirements=ledger,
        authorizations=authorizations,
        reviewer=reviewer,
    )
    return SimpleNamespace(
        engine=engine,
        store=store,
        requirements=ledger,
        authorizations=authorizations,
        workspace=workspace,
    )


def make_requirement(
    security,
    *,
    state: str = "structured",
    limits=(),
    text: str = "修复项目，但不要修改 B。",
):
    return security.requirements.record_user_message(
        text=text,
        message_id="msg_" + uuid.uuid4().hex[:8],
        turn_id="turn_" + uuid.uuid4().hex[:8],
        structured_state=state,
        structured_limits=tuple(dict(item) for item in (limits or ())),
        structured_source="r9h4_test",
    )


def binding_of(requirement) -> RequirementBinding:
    return RequirementBinding(
        requirement_id=requirement.requirement_id, version=int(requirement.version)
    )


def ctx_for(binding, *, scope=None, **kwargs) -> AuthContext:
    return AuthContext(requirement=binding, agent_scope=scope, **kwargs)


def write_effect(path, **detail) -> Effect:
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path)),),
        purpose="写入测试文件",
        detail=dict(detail),
    )


def read_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path)),),
        purpose="读取测试文件",
    )


def delete_effect(path, **detail) -> Effect:
    payload = {"permanent": True}
    payload.update(detail)
    return Effect(
        kind=EffectKind.DELETE,
        targets=(Target("path", str(path), identity="vol:test"),),
        purpose="删除测试文件",
        detail=payload,
    )


def execute_effect(program=None, *, impacts=(), **detail) -> Effect:
    payload = dict(detail)
    if impacts:
        payload["limit_impacts"] = tuple(impacts)
    targets = (Target("program", str(program)),) if program else ()
    return Effect(
        kind=EffectKind.EXECUTE,
        targets=targets,
        purpose="执行测试程序",
        detail=payload,
    )


def internal_effect(path, *, operation=None, **detail) -> Effect:
    payload = dict(detail)
    if operation:
        payload["internal_operation"] = operation
    return Effect(
        kind=EffectKind.INTERNAL_DATA,
        targets=(Target("path", str(path)),),
        purpose="维护内部数据",
        detail=payload,
    )


def limit(
    *,
    operations=("write",),
    objects=(),
    reason: str = "用户明确限制了这个操作",
) -> dict:
    return {
        "operations": list(operations),
        "objects": [dict(item) for item in objects],
        "reason": reason,
    }


def path_object(value) -> dict:
    return {"kind": "path", "value": str(value)}


# --------------------------------------------------------------------------
# 1) 实际影响 / 程序 / 目录 / read / internal_operation / 相对路径
# --------------------------------------------------------------------------


def test_limit_conflict_checks_limit_impacts_before_review(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(
        security,
        limits=(
            limit(
                operations=("write",),
                objects=(path_object(b_path),),
                reason="用户明确要求不要修改 B",
            ),
        ),
    )
    binding = binding_of(requirement)
    effect = write_effect(
        a_path,
        limit_impacts=(
            {"operation": "write", "objects": [path_object(a_path)]},
            {"operation": "write", "objects": [path_object(b_path)]},
        ),
    )

    import asyncio

    decision = asyncio.run(security.engine.authorize(effect, ctx=ctx_for(binding)))
    assert decision.verdict == Verdict.DENY
    assert "不要修改 B" in decision.reason
    assert reviewer.calls == [], "实际影响命中已知限制必须先 DENY，不能进入模型审查"


def test_limit_conflict_program_delete_and_directory_impacts(db_conn, tmp_path):
    workspace = tmp_path / "workspace"
    program = workspace / "bin" / "danger.exe"
    other_program = workspace / "bin" / "safe.exe"
    victim = workspace / "victim.txt"
    directory = workspace / "dir"
    child = directory / "secret.txt"
    import asyncio

    # 程序对象限制
    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(
        security,
        limits=(
            limit(
                operations=("execute",),
                objects=({"kind": "program", "value": str(program)},),
                reason="不要运行 danger",
            ),
        ),
    )
    binding = binding_of(requirement)
    denied = asyncio.run(
        security.engine.authorize(execute_effect(program), ctx=ctx_for(binding))
    )
    allowed = asyncio.run(
        security.engine.authorize(execute_effect(other_program), ctx=ctx_for(binding))
    )
    assert denied.verdict == Verdict.DENY and "不要运行 danger" in denied.reason
    assert allowed.verdict == Verdict.ALLOW

    # 命令删除对象的实际影响：base 是 execute，impact 是 delete victim
    security2 = make_security(db_conn, tmp_path, mode="unrestricted")
    command_requirement = make_requirement(
        security2,
        limits=(
            limit(
                operations=("delete",),
                objects=(path_object(victim),),
                reason="不要删除 victim",
            ),
        ),
    )
    delete_impact = {
        "operation": "delete",
        "objects": [path_object(victim)],
    }
    command_denied = asyncio.run(
        security2.engine.authorize(
            execute_effect("rm", impacts=(delete_impact,)),
            ctx=ctx_for(binding_of(command_requirement)),
        )
    )
    assert command_denied.verdict == Verdict.DENY
    assert "不要删除 victim" in command_denied.reason

    # 目录影响：限制目录本身命中其下写；限制子文件命中目录递归删除的子对象影响
    security3 = make_security(db_conn, tmp_path, mode="unrestricted")
    dir_requirement = make_requirement(
        security3,
        limits=(
            limit(
                operations=("write",),
                objects=({"kind": "directory", "value": str(directory)},),
                reason="不要修改 dir 目录",
            ),
        ),
    )
    child_denied = asyncio.run(
        security3.engine.authorize(
            write_effect(child), ctx=ctx_for(binding_of(dir_requirement))
        )
    )
    assert child_denied.verdict == Verdict.DENY and "不要修改 dir" in child_denied.reason

    security4 = make_security(db_conn, tmp_path, mode="unrestricted")
    child_requirement = make_requirement(
        security4,
        limits=(
            limit(
                operations=("delete",),
                objects=(path_object(child),),
                reason="不要删除 secret",
            ),
        ),
    )
    dir_delete = delete_effect(
        directory,
        limit_impacts=(
            {"operation": "delete", "objects": [path_object(child)]},
        ),
    )
    dir_denied = asyncio.run(
        security4.engine.authorize(
            dir_delete, ctx=ctx_for(binding_of(child_requirement))
        )
    )
    assert dir_denied.verdict == Verdict.DENY
    assert "不要删除 secret" in dir_denied.reason


def test_limit_conflict_read_and_internal_operation(db_conn, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    internal_path = tmp_path / "internal" / "state.db"
    import asyncio

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    # A 分支当前基线尚未在 requirement_limits 中支持 read 操作（契约 §2 由 A 补）。
    # 这里用真实引擎 + 真实台账要求 id/version 的视图承载已保存的 read 限制，
    # 验证 B 负责的引擎匹配；A 合入后 Lead 可直接用真实台账复验端到端。
    requirement = make_requirement(security, state="structured")
    read_view = SimpleNamespace(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
        status="active",
        structured_limits_state="structured",
        structured_limits=[[
            {
                "operations": ["read"],
                "objects": [path_object(b_path)],
                "reason": "不要读取 B",
            }
        ]][0],
    )
    denied_read = security.engine._limit_conflict(read_effect(b_path), read_view)
    allowed_read = security.engine._limit_conflict(read_effect(a_path), read_view)
    assert denied_read and "不要读取 B" in denied_read
    assert allowed_read is None

    # INTERNAL_DATA 不得固定映射成 write：同一个对象，read 限制不应命中 write
    read_limit = {
        "operations": ["read"],
        "objects": [path_object(internal_path)],
        "reason": "不要读取内部对象",
    }
    internal_view = SimpleNamespace(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
        status="active",
        structured_limits_state="structured",
        structured_limits=(read_limit,),
    )
    write_conflict = security.engine._limit_conflict(
        internal_effect(internal_path, operation="write"), internal_view
    )
    assert write_conflict is None, "internal_operation=write 不能被固定映射成 read 限制命中"
    read_conflict = security.engine._limit_conflict(
        internal_effect(internal_path, operation="read"), internal_view
    )
    assert read_conflict

    # 未提供 internal_operation：动作不确定，对象命中时保守拒绝（不能当无限制）
    write_limit = {
        "operations": ["write"],
        "objects": [path_object(internal_path)],
        "reason": "不要修改内部对象",
    }
    write_view = SimpleNamespace(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
        status="active",
        structured_limits_state="structured",
        structured_limits=(write_limit,),
    )
    unknown_internal = security.engine._limit_conflict(
        internal_effect(internal_path), write_view
    )
    assert unknown_internal


def test_relative_limit_paths_normalized_against_workspace_and_cwd(db_conn, tmp_path):
    import asyncio

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    workspace = security.workspace
    requirement = make_requirement(
        security,
        limits=(
            limit(
                operations=("write",),
                objects=(path_object("B.md"),),
                reason="不要修改 B.md",
            ),
        ),
    )
    binding = binding_of(requirement)

    absolute_b = workspace / "B.md"
    absolute_a = workspace / "A.md"
    denied = asyncio.run(
        security.engine.authorize(write_effect(absolute_b), ctx=ctx_for(binding))
    )
    allowed = asyncio.run(
        security.engine.authorize(write_effect(absolute_a), ctx=ctx_for(binding))
    )
    assert denied.verdict == Verdict.DENY
    assert allowed.verdict == Verdict.ALLOW

    # effect 相对路径 + detail cwd：以 cwd 为基准，与 workspace_root 下的 B.md 不同
    sub = workspace / "sub"
    effect_cwd_denied = asyncio.run(
        security.engine.authorize(
            write_effect(
                "B.md", cwd=str(workspace), execution={"cwd": str(workspace)}
            ),
            ctx=ctx_for(binding),
        )
    )
    assert effect_cwd_denied.verdict == Verdict.DENY
    effect_cwd_other = asyncio.run(
        security.engine.authorize(
            write_effect(
                "B.md", cwd=str(sub), execution={"cwd": str(sub)}
            ),
            ctx=ctx_for(binding),
        )
    )
    assert effect_cwd_other.verdict == Verdict.ALLOW, "不得把不同 cwd 下的同名文件误当同一对象"


def test_known_limit_denies_before_internal_permanent_and_mode(db_conn, tmp_path):
    import asyncio

    protected = tmp_path / "internal" / "state.db"
    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer()
    security = make_security(
        db_conn, tmp_path, mode="unrestricted", reviewer=reviewer, protected=[protected]
    )
    requirement = make_requirement(
        security,
        state="unresolved",
        limits=(
            limit(
                operations=("write", "delete"),
                objects=(path_object(protected),),
                reason="不要动内部状态",
            ),
        ),
    )
    binding = binding_of(requirement)

    internal = asyncio.run(
        security.engine.authorize(internal_effect(protected), ctx=ctx_for(binding))
    )
    assert internal.verdict == Verdict.DENY
    permanent = asyncio.run(
        security.engine.authorize(
            delete_effect(protected), ctx=ctx_for(binding)
        )
    )
    assert permanent.verdict == Verdict.DENY
    assert reviewer.calls == []


# --------------------------------------------------------------------------
# 2) 绑定要求撤回 / 版本不一致 / 不存在
# --------------------------------------------------------------------------


def _withdraw(requirements: RequirementLedger, requirement) -> None:
    requirements.apply_resolution(
        ResolutionOutcome(
            relation=RequirementRelation.WITHDRAW,
            target_requirement_id=requirement.requirement_id,
            target_version=int(requirement.version),
            corrections=(),
            limits=(),
            removed_limits=(),
            reason="用户撤回该要求",
            model="fake-r9h4",
            prompt_version="fake-r9h4",
        ),
        message_id="msg_withdraw_" + uuid.uuid4().hex[:6],
        turn_id="turn_withdraw",
        message="算了，撤回刚才的要求",
    )


def test_withdrawn_requirement_binding_denied_in_unrestricted(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    _withdraw(security.requirements, requirement)

    denied = asyncio.run(
        security.engine.authorize(
            write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(binding)
        )
    )
    assert denied.verdict == Verdict.DENY
    assert "撤回" in denied.reason or "版本" in denied.reason
    read_allowed = asyncio.run(
        security.engine.authorize(read_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(binding))
    )
    assert read_allowed.verdict == Verdict.ALLOW, "普通受控读取保持可用"
    assert reviewer.calls == []


def test_version_mismatch_and_missing_requirement_denied(db_conn, tmp_path):
    import asyncio

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(security, state="structured")
    resolution = ResolutionOutcome(
        relation=RequirementRelation.AMEND,
        target_requirement_id=requirement.requirement_id,
        target_version=int(requirement.version),
        corrections=("补充：只改 A",),
        limits=(),
        removed_limits=(),
        reason="补充要求",
        model="fake-r9h4",
        prompt_version="fake-r9h4",
    )
    applied = security.requirements.apply_resolution(
        resolution,
        message_id="msg_amend",
        turn_id="turn_amend",
        message="补充：只改 A",
    )
    child = applied.requirement
    assert child is not None and int(child.version) == int(requirement.version) + 1

    old_binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    stale = asyncio.run(
        security.engine.authorize(
            write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(old_binding)
        )
    )
    assert stale.verdict == Verdict.DENY
    assert "版本" in stale.reason or "替换" in stale.reason or "撤回" in stale.reason

    missing_binding = RequirementBinding(
        requirement_id="req_does_not_exist", version=1
    )
    missing = asyncio.run(
        security.engine.authorize(
            write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(missing_binding)
        )
    )
    assert missing.verdict == Verdict.DENY
    assert "不存在" in missing.reason


# --------------------------------------------------------------------------
# 3) _apply_review 模型返回后要求/内容变化：不采用、不建立授权
# --------------------------------------------------------------------------


def _authorization_count(security, requirement) -> int:
    return len(
        security.authorizations.records_for_requirement(
            requirement.requirement_id, include_revoked=True
        )
    )


def test_review_requirement_withdrawn_while_waiting_not_adopted(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    effect = write_effect(tmp_path / "proj" / "A.py")

    def on_review(_request):
        _withdraw(security.requirements, requirement)

    reviewer.on_review = on_review
    decision = asyncio.run(security.engine.authorize(effect, ctx=ctx_for(binding)))

    assert len(reviewer.calls) == 1, "审查确实发生"
    assert decision.verdict == Verdict.APPROVE, "审查等待期间要求撤回，旧 ALLOW 不得采用"
    assert "撤回" in decision.reason or "版本" in decision.reason or "旧审查" in decision.reason
    assert _authorization_count(security, requirement) == 0, "失效后不得建立授权记录"


def test_review_content_changed_while_waiting_not_adopted(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    effect = write_effect(
        tmp_path / "proj" / "A.py", content_sha256="old-content"
    )

    def on_review(_request):
        effect.detail["content_sha256"] = "new-content"

    reviewer.on_review = on_review
    decision = asyncio.run(security.engine.authorize(effect, ctx=ctx_for(binding)))

    assert decision.verdict == Verdict.APPROVE
    assert "内容" in decision.reason or "变化" in decision.reason
    assert _authorization_count(security, requirement) == 0


def test_review_limits_updated_while_waiting_not_adopted(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    b_path = tmp_path / "proj" / "B.py"

    def on_review(_request):
        security.requirements.set_structured_limits(
            requirement.requirement_id,
            (limit(objects=(path_object(b_path),)),),
            source="late_update",
            state="structured",
        )
        reviewer.decision = ReviewDecision.ALLOW

    reviewer.on_review = on_review
    decision = asyncio.run(
        security.engine.authorize(write_effect(b_path), ctx=ctx_for(binding))
    )

    assert decision.verdict == Verdict.APPROVE
    assert "限制" in decision.reason or "变化" in decision.reason
    assert _authorization_count(security, requirement) == 0


def test_normal_review_still_allows_and_records(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    decision = asyncio.run(
        security.engine.authorize(
            write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(binding_of(requirement))
        )
    )
    assert decision.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1
    assert _authorization_count(security, requirement) == 1


# --------------------------------------------------------------------------
# 4) RecheckService：正确读取 decision、保留调用方上下文、模式变化
# --------------------------------------------------------------------------


class FakeDecisionEngine:
    def __init__(self, decision: AuthDecision) -> None:
        self.decision = decision
        self.ctx = None
        self.effect = None

    def mode(self):
        return SimpleNamespace(value="unrestricted")

    async def authorize(self, effect, *, ctx=None):
        self.effect = effect
        self.ctx = ctx
        return self.decision


def test_recheck_result_verdict_reads_decision_correctly(db_conn, tmp_path):
    import asyncio

    # deny：真实引擎 + 已知限制
    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    b_path = tmp_path / "proj" / "B.py"
    requirement = make_requirement(
        security, limits=(limit(objects=(path_object(b_path),)),)
    )
    service = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        authorizations=security.authorizations,
    )
    binding = OperationBinding(
        requirement=binding_of(requirement),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    denied = asyncio.run(service.recheck(binding, write_effect(b_path)))
    assert denied.ok is False and denied.code == "denied"
    assert denied.verdict == "deny"
    assert getattr(denied.decision, "verdict", None) == Verdict.DENY

    # approve：unrestricted + unresolved → 需要新确认；调用方读取 verdict 才能进入确认流程
    requirement2 = make_requirement(security, state="unresolved")
    binding2 = OperationBinding(
        requirement=binding_of(requirement2),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    awaiting = asyncio.run(
        service.recheck(binding2, write_effect(tmp_path / "proj" / "A.py"))
    )
    assert awaiting.ok is False and awaiting.code == "needs_confirmation"
    assert awaiting.verdict == "approve", (
        "调用方读取 RecheckResult.verdict 时必须得到 approve，而不是空字段导致普通失败"
    )
    assert getattr(awaiting.decision, "verdict", None) == Verdict.APPROVE

    # allow：structured 无限制
    requirement3 = make_requirement(security, state="structured")
    binding3 = OperationBinding(
        requirement=binding_of(requirement3),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    allowed = asyncio.run(
        service.recheck(binding3, write_effect(tmp_path / "proj" / "A.py"))
    )
    assert allowed.ok is True
    assert allowed.verdict == "allow"


def test_recheck_preserves_caller_context_scope_and_object(db_conn, tmp_path):
    import asyncio

    requirement = RequirementLedger(db_conn).record_user_message(
        text="整理项目。", message_id="m_ctx", turn_id="t_ctx"
    )
    engine = FakeDecisionEngine(
        AuthDecision(
            verdict=Verdict.ALLOW,
            reason="fake allow",
            effects=(),
        )
    )
    requirements = RequirementLedger(db_conn)
    service = RecheckService(engine=engine, requirements=requirements)
    effect = write_effect(tmp_path / "proj" / "A.py")
    scope = AgentScope(depth=1, purpose="子任务", grants=())
    context = AuthContext(
        agent_scope=scope,
        origin="subagent",
        turn_id="turn_ctx",
        session_id="session_ctx",
        extra={"custom_evidence": {"k": "v"}, "untrusted_material": ({"kind": "file"},)},
    )
    binding = OperationBinding(
        requirement=RequirementBinding(
            requirement_id=requirement.requirement_id,
            version=int(requirement.version),
        ),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    result = asyncio.run(service.recheck(binding, effect, context=context))

    assert result.ok is True, result.reason
    assert engine.effect is effect, "必须沿用调用方同一个 effect 对象（对象身份依据不丢）"
    assert engine.ctx is not None
    assert engine.ctx.agent_scope is scope, "重核对不得丢子智能体范围"
    assert engine.ctx.turn_id == "turn_ctx" and engine.ctx.session_id == "session_ctx"
    assert engine.ctx.extra.get("custom_evidence") == {"k": "v"}
    assert engine.ctx.extra.get("untrusted_material") == ({"kind": "file"},)


def test_recheck_mode_change_needs_confirmation(db_conn, tmp_path):
    import asyncio

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(security, state="structured")
    service = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        authorizations=security.authorizations,
    )
    binding = OperationBinding(
        requirement=binding_of(requirement),
        mode="restricted",  # 审批/审查时的模式
        operation_kind="write",
        fingerprint="",
    )
    result = asyncio.run(
        service.recheck(binding, write_effect(tmp_path / "proj" / "A.py"))
    )
    assert result.ok is False
    assert result.code == "needs_confirmation", result.code
    assert result.verdict == "approve"
    assert "模式" in result.reason


def test_recheck_content_and_operation_change_are_not_ordinary_failure(db_conn, tmp_path):
    import asyncio

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(security, state="structured")
    target = tmp_path / "proj" / "A.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    service = RecheckService(
        engine=security.engine,
        requirements=security.requirements,
        authorizations=security.authorizations,
    )
    binding = OperationBinding(
        requirement=binding_of(requirement),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
        content_digests=((str(target), "not-the-current-digest"),),
    )
    changed = asyncio.run(service.recheck(binding, write_effect(target)))
    assert changed.ok is False
    assert changed.code == "content_changed"
    assert changed.verdict == "", "未执行时没有 allow 结论，不能误报普通失败为 allow"


# --------------------------------------------------------------------------
# 5) 子智能体范围：三种模式都 DENY，普通审查/always-ALLOW 不能覆盖
# --------------------------------------------------------------------------


def _scope_for(project: Path, *, operations=("write", "create", "delete")) -> AgentScope:
    grant = Grant(
        grant_id="grant_scope_" + uuid.uuid4().hex[:6],
        instruction_id="instr_parent",
        operations=frozenset(operations),
        objects=[Scope("path_prefix", str(project))],
        purpose="用户在父指令中授权项目范围",
        source="user_explicit",
        created_at=_NOW,
    )
    return AgentScope(
        parent_instruction_id="instr_parent",
        grants=(grant,),
        depth=1,
        purpose="子任务",
    )


def test_subagent_scope_insufficient_denied_in_all_modes(db_conn, tmp_path):
    import asyncio

    project = tmp_path / "project"
    project.mkdir()
    inside = project / "a.py"
    outside = tmp_path / "outside.py"

    for mode in ("restricted", "unrestricted"):
        reviewer = FakeReviewer()
        security = make_security(db_conn, tmp_path, mode=mode, reviewer=reviewer)
        requirement = make_requirement(security, state="structured")
        scope = _scope_for(project)
        binding = binding_of(requirement)

        inside_decision = asyncio.run(
            security.engine.authorize(
                write_effect(inside), ctx=ctx_for(binding, scope=scope)
            )
        )
        calls_before_outside = len(reviewer.calls)
        outside_decision = asyncio.run(
            security.engine.authorize(
                write_effect(outside), ctx=ctx_for(binding, scope=scope)
            )
        )
        assert inside_decision.verdict == Verdict.ALLOW, mode
        assert outside_decision.verdict == Verdict.DENY, (
            mode + "：范围不足必须 DENY，不能被普通审查/always-ALLOW 覆盖"
        )
        assert "主智能体" in outside_decision.reason
        assert len(reviewer.calls) == calls_before_outside, (
            "范围不足不能先进入模型审查"
        )

        # 执行前核对同样保留范围边界
        rechecked = security.engine.recheck_approved(
            write_effect(outside), context=ctx_for(binding, scope=scope)
        )
        assert rechecked.verdict == Verdict.DENY


def test_subagent_scope_program_and_batch(db_conn, tmp_path):
    import asyncio

    project = tmp_path / "project"
    project.mkdir()
    inside = project / "a.py"
    outside = tmp_path / "outside.py"
    git = tmp_path / "bin" / "git"
    other = tmp_path / "bin" / "other"

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    scope = _scope_for(project, operations=("write", "execute"))
    # 追加程序范围 grant
    program_grant = Grant(
        grant_id="grant_program",
        instruction_id="instr_parent",
        operations=frozenset({"execute"}),
        objects=[Scope("program", str(git))],
        purpose="用户批准运行 git",
        source="user_explicit",
        created_at=_NOW,
    )
    scope = AgentScope(
        parent_instruction_id="instr_parent",
        grants=tuple(scope.grants) + (program_grant,),
        depth=1,
        purpose="子任务",
    )

    allowed_program = asyncio.run(
        security.engine.authorize(
            execute_effect(git), ctx=ctx_for(binding, scope=scope)
        )
    )
    denied_program = asyncio.run(
        security.engine.authorize(
            execute_effect(other), ctx=ctx_for(binding, scope=scope)
        )
    )
    assert allowed_program.verdict == Verdict.ALLOW
    assert denied_program.verdict == Verdict.DENY

    decisions = asyncio.run(
        security.engine.authorize_batch(
            [write_effect(inside), write_effect(outside)],
            ctx=ctx_for(binding, scope=scope),
        )
    )
    assert decisions[0].verdict != Verdict.DENY, "范围外不得因局部越界全批判废"
    assert decisions[1].verdict == Verdict.DENY


# --------------------------------------------------------------------------
# 补充：批量逐项、restricted 撤回绑定、审查期间对象身份变化
# --------------------------------------------------------------------------


def test_batch_known_limit_only_affects_matching_item(db_conn, tmp_path):
    import asyncio

    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    requirement = make_requirement(
        security,
        limits=(
            limit(
                operations=("write",),
                objects=(path_object(b_path),),
                reason="不要修改 B",
            ),
        ),
    )
    binding = binding_of(requirement)
    effects = [write_effect(a_path), write_effect(b_path)]
    decisions = asyncio.run(
        security.engine.authorize_batch(effects, ctx=ctx_for(binding))
    )
    assert len(decisions) == 2
    assert decisions[0].verdict != Verdict.DENY, "范围外 A 不得因局部限制变成 DENY"
    assert decisions[1].verdict == Verdict.DENY
    many = asyncio.run(security.engine.authorize_many(effects, ctx=ctx_for(binding)))
    assert many.verdict == Verdict.DENY
    assert "不要修改 B" in many.reason
    assert reviewer.calls == []


def test_withdrawn_binding_restricted_denied_without_review(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    _withdraw(security.requirements, requirement)

    decision = asyncio.run(
        security.engine.authorize(
            write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(binding)
        )
    )
    assert decision.verdict == Verdict.DENY
    assert reviewer.calls == [], "绑定要求失效时不能再用普通审查替代要求有效性核对"


def test_review_object_identity_changed_while_waiting_not_adopted(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)
    target_a = tmp_path / "proj" / "A.py"
    target_b = tmp_path / "proj" / "B.py"
    effect = write_effect(target_a)

    def on_review(_request):
        object.__setattr__(
            effect,
            "targets",
            (Target("path", str(target_b), identity="vol:changed"),),
        )

    reviewer.on_review = on_review
    decision = asyncio.run(security.engine.authorize(effect, ctx=ctx_for(binding)))
    assert decision.verdict == Verdict.APPROVE
    assert "对象" in decision.reason or "变化" in decision.reason
    assert _authorization_count(security, requirement) == 0


def test_review_requirement_version_changed_while_waiting_not_adopted(db_conn, tmp_path):
    import asyncio

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security, state="structured")
    binding = binding_of(requirement)

    def on_review(_request):
        security.requirements.apply_resolution(
            ResolutionOutcome(
                relation=RequirementRelation.AMEND,
                target_requirement_id=requirement.requirement_id,
                target_version=int(requirement.version),
                corrections=("补充：只改 A",),
                limits=(),
                removed_limits=(),
                reason="补充要求",
                model="fake-r9h4",
                prompt_version="fake-r9h4",
            ),
            message_id="msg_wait_amend",
            turn_id="turn_wait_amend",
            message="补充：只改 A",
        )

    reviewer.on_review = on_review
    decision = asyncio.run(
        security.engine.authorize(
            write_effect(tmp_path / "proj" / "A.py"), ctx=ctx_for(binding)
        )
    )
    assert decision.verdict == Verdict.APPROVE
    assert "版本" in decision.reason or "撤回" in decision.reason or "旧审查" in decision.reason
    assert _authorization_count(security, requirement) == 0


# --------------------------------------------------------------------------
# 6) E 身份生命周期接口（lookup/tokens/known_paths）与 limit_impacts_complete
# --------------------------------------------------------------------------


def test_engine_identity_lookup_public_interfaces_and_wiring(db_conn, tmp_path, monkeypatch):
    lookups: list[tuple] = []
    tokens = frozenset({"vol:1#win:creation:1", "vol:2#posix:birth:2"})
    known = frozenset({str(tmp_path / "old.db")})

    def match_identity(identity, birth_key=None):
        lookups.append((identity, birth_key))
        return {
            "state": "same",
            "identity_key": str((identity or {}).get("identity_key") or ""),
            "birth_key": birth_key,
        }

    engine = AuthorizationEngine(
        conn=db_conn,
        settings=None,
        workspace_root=lambda: tmp_path,
        internal_data_paths=lambda: [],
        requirements=RequirementLedger(db_conn),
        authorizations=AuthorizationLedger(db_conn),
        internal_data_identity_lookup=match_identity,
        internal_data_identity_tokens=lambda: tokens,
        internal_data_identity_keys=lambda: frozenset({"vol:1"}),
        internal_data_known_paths=lambda: known,
    )

    assert engine.internal_data_identity_tokens() == tokens
    assert engine.internal_data_identity_known_paths() == known
    result = engine.internal_data_identity_lookup("vol:1", "win:creation:1")
    assert result["state"] == "same"
    assert lookups and lookups[-1][1] == "win:creation:1"
    assert str(lookups[-1][0].get("identity_key")) == "vol:1"

    # E 合入后的 internal_data 接受 identity_lookup：引擎必须传入它。
    import agent.security.internal_data as internal_data_module

    captured: dict = {}
    original = internal_data_module.effect_targets_internal_data

    def fake_effect_targets(effect, protected, **kwargs):
        captured.update(kwargs)
        legacy = {
            key: value for key, value in kwargs.items() if key != "identity_lookup"
        }
        return original(effect, protected, **legacy)

    monkeypatch.setattr(
        internal_data_module, "effect_targets_internal_data", fake_effect_targets
    )
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(tmp_path / "linked.db"), identity="vol:1"),),
        purpose="测试身份接线",
    )
    # 路径不在 protected / keys 不含 target 身份时由 lookup 参与（这里只验证接线参数）。
    engine._effect_is_internal(effect, [])
    assert "identity_lookup" in captured
    assert callable(captured["identity_lookup"])


def test_engine_identity_lookup_legacy_internal_data_keeps_keys(db_conn, tmp_path):
    """E 未合入（旧 internal_data 不接受 identity_lookup）时不得丢掉身份键登记。"""
    engine = AuthorizationEngine(
        conn=db_conn,
        settings=None,
        workspace_root=lambda: tmp_path,
        internal_data_paths=lambda: [],
        requirements=RequirementLedger(db_conn),
        authorizations=AuthorizationLedger(db_conn),
        internal_data_identity_lookup=lambda identity, birth=None: {"state": "same"},
        internal_data_identity_keys=lambda: frozenset({"vol:legacy"}),
        internal_data_known_paths=lambda: (),
    )
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(
            Target("path", str(tmp_path / "outside.db"), identity="vol:legacy"),
        ),
        purpose="旧签名兼容",
    )
    assert engine._effect_is_internal(effect, []) is True


def test_limit_impacts_incomplete_conservatively_denied(db_conn, tmp_path):
    import asyncio

    b_path = tmp_path / "proj" / "B.py"
    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="unrestricted", reviewer=reviewer)
    requirement = make_requirement(
        security,
        limits=(
            limit(
                operations=("delete",),
                objects=(path_object(b_path),),
                reason="不要删除 B",
            ),
        ),
    )
    effect = delete_effect(
        tmp_path / "proj" / "dir",
        limit_impacts=(
            {"operation": "delete", "objects": [path_object(tmp_path / "proj" / "other")]},
        ),
        limit_impacts_complete=False,
    )
    decision = asyncio.run(
        security.engine.authorize(effect, ctx=ctx_for(binding_of(requirement)))
    )
    assert decision.verdict == Verdict.DENY
    assert "无法完整" in decision.reason or "限制" in decision.reason
    assert reviewer.calls == []

    # 没有限制时不能因为“影响不完整”就把普通操作变成 DENY。
    plain_requirement = make_requirement(security, state="structured")
    plain = asyncio.run(
        security.engine.authorize(
            delete_effect(
                tmp_path / "proj" / "plain",
                limit_impacts_complete=False,
            ),
            ctx=ctx_for(binding_of(plain_requirement)),
        )
    )
    assert plain.verdict == Verdict.ALLOW


def test_limit_program_matches_resolved_program(db_conn, tmp_path):
    import asyncio

    security = make_security(db_conn, tmp_path, mode="unrestricted")
    python = str(tmp_path / "Python" / "python.exe")
    requirement = make_requirement(
        security,
        limits=(
            limit(
                operations=("execute",),
                objects=({"kind": "program", "value": "python"},),
                reason="不要运行 python",
            ),
        ),
    )
    binding = binding_of(requirement)
    denied = asyncio.run(
        security.engine.authorize(
            execute_effect(
                None,
                resolved_program=python,
                execution={"program": "python", "resolved_program": python},
            ),
            ctx=ctx_for(binding),
        )
    )
    assert denied.verdict == Verdict.DENY
    assert "不要运行 python" in denied.reason

    allowed = asyncio.run(
        security.engine.authorize(
            execute_effect(
                None,
                resolved_program=str(tmp_path / "node.exe"),
            ),
            ctx=ctx_for(binding),
        )
    )
    assert allowed.verdict == Verdict.ALLOW

    # 限制值是 resolve 后绝对路径，effect 只有 detail.resolved_program 也要命中。
    security2 = make_security(db_conn, tmp_path, mode="unrestricted")
    absolute_requirement = make_requirement(
        security2,
        limits=(
            limit(
                operations=("execute",),
                objects=({"kind": "program", "value": python},),
                reason="不要运行这个 python",
            ),
        ),
    )
    absolute_denied = asyncio.run(
        security2.engine.authorize(
            execute_effect(None, resolved_program=python),
            ctx=ctx_for(binding_of(absolute_requirement)),
        )
    )
    assert absolute_denied.verdict == Verdict.DENY
    assert "不要运行这个 python" in absolute_denied.reason
