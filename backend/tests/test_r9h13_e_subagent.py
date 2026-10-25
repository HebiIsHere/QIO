# -*- coding: utf-8 -*-
"""R9-H13 E：子智能体权限必须同时防止扩大和无故缩小（契约 §3.H，F1–F6）。

真实验收边界（契约 §5）：
* 真实 `AuthorizationEngine` / `RequirementLedger` / `AuthorizationLedger` /
  `InstructionRegistry` / `ApprovalService` / `OperationRegistry` / `SettingsStore` /
  `SubagentTool` / `TaskManager` / 临时文件；
* 模型一律 fake：`FakeReviewer`（真实审查输入、可控 ALLOW）与脚本化 adapter；
* 子任务执行循环：F6 的"请求→授予→重试→撤销"用真实 `AgentLoop` + fake adapter
  调真实 `request_agent_scope` 工具；其余 F6 用例用脚本化子任务体（等价于真实子
  任务工具调用统一授权入口）执行**真实** effect，模型不参与判定；
* 不以 helper 存在/原因文字/源码字符串代替行为断言：断言逐动作逐对象 verdict、
  撤销后读取、internal data、父级授权形成的子 scope、请求状态机字段。

基线（r9h12/main e68fb9b7 + Lead 49ba218a）复现：
* F1 只有 write grant 的子范围仍可读（_scope_limited READ 分支只比对象）；
* F2 父 grant 撤销后子范围仍可读（同上，未走 grant.allows）；
* F3 unrestricted 下子范围可读内部日志（内部数据分支在 scope 检查之前）；
* F4 同时授予"运行删除程序 + 删除指定文件"仍被拒（复合 effect 按 execute 核对
  全部路径）；
* F5 只授予路径 execute 却能通过删除影响（同一拍平根因）；
* F6 模型审查 ALLOW 只写 AuthorizationLedger、不形成子 scope，正常子任务被拒，
  且创造力不足时只返回一句错误文字（无结构化请求/授予/重试/撤销状态）。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.security.context import (
    agent_scope_context,
    instruction_context,
    requirement_context,
)
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.gateway import AuthContext
from agent.security.instructions import Grant, InstructionRegistry, Scope
from agent.security.operations import OperationRegistry
from agent.security.requirements import AuthorizationLedger, RequirementBinding, RequirementLedger
from agent.security.review import ReviewDecision, ReviewOutcome, ReviewRequest
from agent.storage.settings import SettingsStore
from agent.tools import security_gate
from agent.tools.approval import ApprovalService
from agent.tools.base import ToolResult
from agent.tools.spec import ToolDefinition
from agent.tools.subagent_tool import SubagentTool
from agent.tools.task_manager import TaskManager

_NOW = "2026-10-25T00:00:00+00:00"


# --------------------------------------------------------------------------
# 真实装配（SQLite 台账 + 引擎 + 审批）与 fake 模型
# --------------------------------------------------------------------------


class FakeReviewer:
    """计数型审查替身：真实 ReviewRequest 进、可控 ALLOW 出（不联网）。"""

    def __init__(self, decision: ReviewDecision = ReviewDecision.ALLOW) -> None:
        self.decision = decision
        self.calls: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> ReviewOutcome:
        self.calls.append(request)
        return ReviewOutcome(
            decision=self.decision,
            reason="fake reviewer: " + self.decision.value,
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(request.operation.kind,),
            objects=tuple(request.operation.objects),
            limits=("fake reviewer 允许本次测试操作",),
            purpose="fake reviewer",
        )


class FakeBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event) -> None:
        self.published.append(event)


class FakeCreds:
    def get_secret(self, ref):
        return "sk-sub"

    def get_default_secret(self):
        return None

    def get_default_meta(self):
        return None

    def get_metadata(self, ref):
        return {"id": ref, "default_model": "m1"}

    def list_tagged(self, tag):
        return [{"id": "key_sub", "tags": [tag], "budget": None, "budget_used": 0}]


def make_security(conn, tmp_path: Path, *, mode="restricted", reviewer=None, protected=()):
    """真实引擎 + 审批 + 指令/要求/授权台账（不注入任何权限结论）。"""
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    requirements = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    bus = EventBus()
    approvals = ApprovalService(bus, conn=conn)
    operations = OperationRegistry()
    engine = AuthorizationEngine(
        conn=conn,
        approvals=approvals,
        settings=store,
        instance_id="inst_r9h13_e",
        workspace_root=lambda: workspace,
        internal_data_paths=lambda: [Path(item) for item in protected],
        requirements=requirements,
        authorizations=authorizations,
        reviewer=reviewer,
    )
    approvals.set_engine(engine)
    approvals.set_operations(operations)
    return SimpleNamespace(
        engine=engine,
        approvals=approvals,
        operations=operations,
        instructions=InstructionRegistry(),
        requirements=requirements,
        authorizations=authorizations,
        store=store,
        workspace=workspace,
        bus=bus,
    )


def make_requirement(security) -> SimpleNamespace:
    requirement = security.requirements.record_user_message(
        text="修复项目里的报告，只改这个项目内的文件。",
        message_id="msg_" + uuid.uuid4().hex[:8],
        turn_id="turn_" + uuid.uuid4().hex[:8],
    )
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(getattr(requirement, "version", 1) or 1),
    )
    return SimpleNamespace(requirement=requirement, binding=binding)


def path_effect(kind: EffectKind, path, **detail) -> Effect:
    payload = dict(detail)
    return Effect(
        kind=kind,
        targets=(Target("path", str(path)),),
        purpose="R9-H13 E 测试 effect",
        detail=payload,
    )


def make_grant(operations, objects, *, grant_id=None, revoked=False, source="user_approved",
               purpose="测试授权", requirement_id=None, requirement_version=None,
               source_record_id=None):
    grant = Grant(
        grant_id=grant_id or "grant_" + uuid.uuid4().hex[:8],
        instruction_id="instr_test",
        operations=frozenset(operations),
        objects=list(objects),
        purpose=purpose,
        source=source,
        created_at=_NOW,
        revoked=revoked,
    )
    # 新字段由 instructions.Grant 以默认值提供；测试只在存在时设置（基线兼容）。
    for name, value in (
        ("requirement_id", requirement_id),
        ("requirement_version", requirement_version),
        ("source_record_id", source_record_id),
    ):
        if hasattr(grant, name) and value is not None:
            setattr(grant, name, value)
    return grant


def make_scope(*grants) -> object:
    from agent.security.context import AgentScope

    return AgentScope(
        parent_instruction_id="instr_test",
        grants=tuple(grants),
        depth=1,
        purpose="子任务",
    )


def execute_delete_effect(program: Path, target: Path, *, cwd: Path | None = None) -> Effect:
    """按 RunProgramTool 的真实形状构造复合 EXECUTE effect（execute + delete 影响）。"""
    targets = [Target("program", str(program))]
    if cwd is not None:
        targets.append(Target("path", str(cwd)))
    targets.append(Target("path", str(target)))
    return Effect(
        kind=EffectKind.EXECUTE,
        targets=tuple(targets),
        purpose="运行删除程序删除指定文件",
        tool="run_program",
        detail={
            "program": str(program),
            "requested_program": str(program),
            "argv": [str(program), "delete", str(target)],
            "cwd": str(cwd) if cwd is not None else None,
            "shell": False,
            "limit_impacts": [
                {
                    "operation": "execute",
                    "objects": [{"kind": "program", "value": str(program)}],
                },
                {
                    "operation": "delete",
                    "objects": [{"kind": "path", "value": str(target)}],
                },
            ],
            "limit_impacts_complete": True,
        },
    )


# --------------------------------------------------------------------------
# 子智能体工具装配（真实 SubagentTool + TaskManager）
# --------------------------------------------------------------------------


class ScriptedChildAdapter:
    """子任务模型替身：按脚本产出 tool call / 最终答复。"""

    mode = "native"
    model = "sub-model"

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list = []

    async def complete(self, messages, tools, **kwargs):
        self.calls.append(messages)
        step = self.script.pop(0) if self.script else None
        if step is None:
            return Completion(message=ChatMessage(role="assistant", content="子任务完成"))
        name, arguments = step
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(id=f"c{len(self.calls)}", name=name, arguments=arguments)
                ],
            )
        )


class QuietChildAdapter:
    mode = "native"
    model = "sub-model"

    async def complete(self, messages, tools, **kwargs):  # pragma: no cover - 被替换
        return Completion(message=ChatMessage(role="assistant", content="子任务完成"))


def make_subagent_tool(security, *, child_adapter=None, sync=True) -> SubagentTool:
    definition = ToolDefinition(
        name="research_x",
        description="整理项目里的报告文件",
        tool_type="subagent",
        credential_ref="key_sub",
        model="m1",
        sync=sync,
        subagent_budget={"max_iterations": 6, "max_tokens": 100000},
    )
    adapter = child_adapter or QuietChildAdapter()

    async def adapter_factory(ref, model):
        return adapter

    return SubagentTool(
        definition,
        credentials=FakeCreds(),
        task_manager=TaskManager(FakeBus(), max_concurrent=2),
        adapter_factory=adapter_factory,
        bus=FakeBus(),
        security=security.engine,
    )


def patch_child_body(
    monkeypatch, security, captured: dict, *, writes=(), reads=(), deletes=()
):
    """把子任务体替换为「真实 effect → 统一授权入口」的脚本（模型 fake）。"""

    async def fake_run_subagent(
        self, task_id, adapter, business, control, budget, choice, tid, tracer
    ):
        scope = security_gate.current_agent_scope()
        captured["scope"] = scope
        for key, effects in (("write", writes), ("read", reads), ("delete", deletes)):
            decisions = []
            for effect in effects:
                decision = await security.engine.authorize(effect)
                decisions.append(
                    {
                        "verdict": decision.verdict.value,
                        "reason": decision.reason,
                        "object_decisions": [
                            dict(item) for item in (decision.object_decisions or ())
                        ],
                    }
                )
            if effects:
                captured[key] = decisions
        return ToolResult(ok=True, content="子任务完成", status="completed", code="ok")

    monkeypatch.setattr(SubagentTool, "_run_subagent", fake_run_subagent)


def _normalized(path) -> str:
    import os

    return os.path.normcase(os.path.normpath(str(path)))


def _grant_objects(grants) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for grant in grants or ():
        for item in getattr(grant, "objects", ()) or ():
            out.add((str(getattr(item, "kind", "")), _normalized(getattr(item, "value", ""))))
    return out


def _grant_operations(grants, source_prefix: str = "") -> set[str]:
    operations: set[str] = set()
    for grant in grants or ():
        if source_prefix and not str(getattr(grant, "source", "")).startswith(source_prefix):
            continue
        operations.update(str(op) for op in getattr(grant, "operations", ()) or ())
    return operations


# --------------------------------------------------------------------------
# F1：只有写权限仍能读取 → 读取也检查操作权限
# --------------------------------------------------------------------------


async def test_f1_write_only_grant_does_not_authorize_read(db_conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "report.txt"
    target.write_text("正文", encoding="utf-8")
    read_dir = tmp_path / "readable"
    read_dir.mkdir()
    readable = read_dir / "notes.txt"
    readable.write_text("可读", encoding="utf-8")

    security = make_security(db_conn, tmp_path, mode="restricted")
    write_only = make_grant({"write"}, [Scope("path_prefix", str(project))])
    read_grant = make_grant({"read", "write"}, [Scope("path_prefix", str(read_dir))])
    scope = make_scope(write_only, read_grant)

    with agent_scope_context(scope):
        inside = await security.engine.authorize(path_effect(EffectKind.READ, target))
        adjacent = await security.engine.authorize(path_effect(EffectKind.READ, readable))

    # F1：只有 write 的授权不能读（基线：ALLOW = 扩大）
    assert inside.verdict == Verdict.DENY, (
        "只有写权限的子范围不得读取：" + inside.reason
    )
    # 相邻正常放行：显式 read 授权的对象必须可读（不能整体缩小）
    assert adjacent.verdict == Verdict.ALLOW, adjacent.reason


# --------------------------------------------------------------------------
# F2：授权撤销后仍能读取 → 读取检查撤销状态
# --------------------------------------------------------------------------


async def test_f2_revoked_parent_grant_stops_child_read(db_conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "report.txt"
    target.write_text("正文", encoding="utf-8")

    security = make_security(db_conn, tmp_path, mode="restricted")
    instruction = security.instructions.begin(turn_id="turn_f2", text="修改项目报告")
    grant = security.instructions.add_grant(
        instruction,
        operations={"read", "write"},
        objects=[Scope("path_prefix", str(project))],
        purpose="用户批准读取/修改项目",
    )
    assert grant is not None
    with instruction_context(instruction):
        scope = security.engine.child_scope(purpose="子任务")
    assert any(item.grant_id == grant.grant_id for item in scope.grants)

    with instruction_context(instruction), agent_scope_context(scope):
        before = await security.engine.authorize(path_effect(EffectKind.READ, target))
    assert before.verdict == Verdict.ALLOW, before.reason

    # 用户要求撤回/轮次结束：父 grant 全部撤销；读取也必须立即停止。
    security.instructions.end(instruction)
    with instruction_context(instruction), agent_scope_context(scope):
        after = await security.engine.authorize(path_effect(EffectKind.READ, target))
    assert after.verdict == Verdict.DENY, (
        "父授权撤销后子范围不得继续读取：" + after.reason
    )


# --------------------------------------------------------------------------
# F3：内部数据 / unrestricted 不能绕过子范围
# --------------------------------------------------------------------------


async def test_f3_child_scope_bounds_internal_data_read_in_unrestricted(db_conn, tmp_path):
    internal_log = tmp_path / "qio-internal.log"
    internal_log.write_text("内部运行日志", encoding="utf-8")
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    ordinary = tmp_path / "project" / "report.txt"
    ordinary.parent.mkdir()
    ordinary.write_text("普通正文", encoding="utf-8")

    security = make_security(
        db_conn, tmp_path, mode="unrestricted", protected=[internal_log]
    )
    # 子范围来自一次"空文件授权"：只有一个普通对象的读取授权。
    scope = make_scope(make_grant({"read"}, [Scope("path", str(empty_file))]))

    # 主智能体自己（没有 agent_scope）在 unrestricted 下仍可读内部日志：规则不变。
    main_agent = await security.engine.authorize(
        path_effect(EffectKind.READ, internal_log)
    )
    assert main_agent.verdict == Verdict.ALLOW, main_agent.reason

    with agent_scope_context(scope):
        internal = await security.engine.authorize(
            path_effect(EffectKind.READ, internal_log)
        )
        adjacent = await security.engine.authorize(path_effect(EffectKind.READ, ordinary))

    # F3：内部数据 + unrestricted 都不能绕过子范围（基线：ALLOW = 绕过）
    assert internal.verdict == Verdict.DENY, (
        "子范围不得读取内部运行日志（即使 unrestricted）：" + internal.reason
    )
    # 相邻：子范围自己的普通对象不受影响
    assert adjacent.verdict == Verdict.DENY, "空文件授权不覆盖普通报告（范围逐对象核对）"


# --------------------------------------------------------------------------
# F4：已授予"运行删除程序 + 删除指定文件"却仍被拒 → 逐动作逐对象核对
# --------------------------------------------------------------------------


async def test_f4_compound_delete_allowed_when_each_action_and_object_granted(
    db_conn, tmp_path
):
    program = tmp_path / "bin" / "delete-tool"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    target = tmp_path / "project" / "old.txt"
    target.parent.mkdir()
    target.write_text("旧文件", encoding="utf-8")
    other = tmp_path / "project" / "keep.txt"
    other.write_text("保留", encoding="utf-8")

    reviewer = FakeReviewer()
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security)
    scope = make_scope(
        make_grant({"execute"}, [Scope("program", str(program))]),
        make_grant({"delete"}, [Scope("path", str(target))]),
    )
    ctx = AuthContext(
        requirement=requirement.binding, agent_scope=scope, origin="subagent"
    )

    compound = execute_delete_effect(program, target, cwd=target.parent)
    decision = await security.engine.authorize(compound, ctx=ctx)

    # F4：两个真实动作各自有对应对象的授权 → 必须放行，不得无故缩小。
    assert decision.verdict == Verdict.ALLOW, (
        "已授予运行删除程序 + 删除该文件的复合操作不得被拒：" + decision.reason
    )
    assert reviewer.calls == [], "已有核实授权覆盖的操作不得重复审查/审批"

    # 相邻正常放行：单独运行程序、单独删除对象也分别放行。
    run_only = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(program)),),
        purpose="单独运行程序",
        tool="run_program",
        detail={
            "program": str(program),
            "shell": False,
            "limit_impacts": [
                {
                    "operation": "execute",
                    "objects": [{"kind": "program", "value": str(program)}],
                }
            ],
            "limit_impacts_complete": True,
        },
    )
    run_decision = await security.engine.authorize(run_only, ctx=ctx)
    delete_decision = await security.engine.authorize(
        path_effect(EffectKind.DELETE, target), ctx=ctx
    )
    assert run_decision.verdict == Verdict.ALLOW, run_decision.reason
    assert delete_decision.verdict == Verdict.ALLOW, delete_decision.reason

    # 逐对象：同一次复合操作换成未授权对象必须拒绝（不是整类放行）。
    denied_scope = make_scope(
        make_grant({"execute"}, [Scope("program", str(program))]),
        make_grant({"delete"}, [Scope("path", str(target))]),
    )
    denied_ctx = AuthContext(
        requirement=requirement.binding, agent_scope=denied_scope, origin="subagent"
    )
    other_decision = await security.engine.authorize(
        execute_delete_effect(program, other, cwd=target.parent), ctx=denied_ctx
    )
    assert other_decision.verdict == Verdict.DENY, other_decision.reason


# --------------------------------------------------------------------------
# F5：只授予对应路径的执行权限，反而能够删除文件 → 复合操作逐动作核对
# --------------------------------------------------------------------------


async def test_f5_execute_only_does_not_authorize_delete_impact(db_conn, tmp_path):
    program = tmp_path / "bin" / "delete-tool"
    program.parent.mkdir()
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    target = tmp_path / "project" / "old.txt"
    target.parent.mkdir()
    target.write_text("旧文件", encoding="utf-8")

    security = make_security(db_conn, tmp_path, mode="restricted")
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f5", text="整理项目")
    # 只授予"执行权限"（程序入口 + 该路径），没有 delete 授权。
    assert security.instructions.add_grant(
        instruction,
        operations={"execute"},
        objects=[Scope("program", str(program))],
        purpose="用户批准运行该程序",
    )
    assert security.instructions.add_grant(
        instruction,
        operations={"execute"},
        objects=[Scope("path", str(target))],
        purpose="用户批准在该路径执行",
    )
    with instruction_context(instruction):
        scope = security.engine.child_scope(purpose="子任务")
    ctx = AuthContext(
        requirement=requirement.binding, agent_scope=scope, origin="subagent"
    )
    with instruction_context(instruction):
        decision = await security.engine.authorize(
            execute_delete_effect(program, target), ctx=ctx
        )
    # F5：execute 授权不能覆盖 delete 影响（基线：ALLOW = 扩大）
    assert decision.verdict == Verdict.DENY, (
        "只有执行授权不得删除文件：" + decision.reason
    )

    # 相邻正常放行：没有删除影响的 execute 影响仍按 execute 授权放行。
    run_only = Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", str(program)),),
        purpose="单独运行程序",
        tool="run_program",
        detail={
            "program": str(program),
            "shell": False,
            "limit_impacts": [
                {
                    "operation": "execute",
                    "objects": [{"kind": "program", "value": str(program)}],
                }
            ],
            "limit_impacts_complete": True,
        },
    )
    with instruction_context(instruction):
        run_decision = await security.engine.authorize(run_only, ctx=ctx)
    assert run_decision.verdict == Verdict.ALLOW, run_decision.reason


# --------------------------------------------------------------------------
# F6：主智能体经模型审查取得的有效授权形成可委派子范围
# --------------------------------------------------------------------------


async def test_f6_verified_review_authorization_forms_delegable_child_scope(
    db_conn, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    authorized = project / "report.txt"
    authorized.write_text("原文", encoding="utf-8")
    other = project / "other.txt"
    other.write_text("别的文件", encoding="utf-8")

    reviewer = FakeReviewer(ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6", text="修改项目报告")
    captured: dict = {}
    patch_child_body(
        monkeypatch,
        security,
        captured,
        writes=[path_effect(EffectKind.WRITE, authorized)],
        reads=[path_effect(EffectKind.READ, authorized)],
    )
    tool = make_subagent_tool(security)

    with instruction_context(instruction), requirement_context(requirement.binding):
        # 主智能体先经模型审查取得对 authorized 的写授权（真实 ALLOW 记录）。
        parent_decision = await security.engine.authorize(
            path_effect(EffectKind.WRITE, authorized)
        )
        assert parent_decision.verdict == Verdict.ALLOW, parent_decision.reason
        review_calls_after_parent = len(reviewer.calls)
        assert review_calls_after_parent == 1

        result = await tool.run(query="整理项目报告")

    assert result.ok is True, result.error
    scope = captured["scope"]
    assert scope is not None, "子任务必须处在 AgentScope 中"

    # 父级核实授权必须形成明确的子 scope：操作/对象/要求版本/用途边界都在。
    verified_grants = [
        grant
        for grant in scope.grants
        if str(getattr(grant, "source", "")).startswith("verified")
    ]
    assert verified_grants, "模型审查 ALLOW 必须形成可委派 grant"
    objects = _grant_objects(verified_grants)
    assert ("path", _normalized(authorized)) in objects
    assert ("path", _normalized(other)) not in objects
    assert {"write", "read"} <= _grant_operations(verified_grants)
    records = security.authorizations.records_for_requirement(
        requirement.requirement.requirement_id
    )
    assert len(records) == 1
    for grant in verified_grants:
        assert getattr(grant, "requirement_id", None) == requirement.requirement.requirement_id
        assert int(getattr(grant, "requirement_version", 0) or 0) == int(
            requirement.binding.version
        )
        assert getattr(grant, "purpose", ""), "用途边界不得丢失"
        # 内容/条件边界：核实记录的指纹与限制必须随 grant 保留（不能只有路径）。
        assert getattr(grant, "source_record_id", None) == records[0].record_id
        assert getattr(grant, "fingerprint", None) == records[0].fingerprint
        assert tuple(getattr(grant, "limits", ())) == tuple(records[0].limits)
        assert getattr(grant, "is_active", lambda: True)(), "有效核实授权必须可用"

    # 逐动作逐对象：已核实对象可读可写；其他对象、其他动作仍拒绝。
    assert captured["write"][0]["verdict"] == "allow", captured["write"][0]["reason"]
    assert captured["read"][0]["verdict"] == "allow", captured["read"][0]["reason"]
    assert len(reviewer.calls) == review_calls_after_parent, (
        "父级已有核实授权，子任务覆盖的操作不得重复审查/要求用户批准"
    )
    # 子任务结果必须带结构化委派事实（不是只有一句文字）。
    delegation = result.data["agent_scope"]["delegation"]
    assert delegation["state"] == "granted"
    assert delegation["requirement_id"] == requirement.requirement.requirement_id
    assert delegation["requirement_version"] == int(requirement.binding.version)
    assert {"write", "read"} <= set(delegation["operations"])
    assert any(
        item["value"].endswith("report.txt") for item in delegation["objects"]
    )
    assert delegation["revocable"] is True
    assert delegation["grants"], "结构化委派事实必须列出每个 grant 的操作/对象/来源"


async def test_f6_boundary_other_object_and_action_denied(db_conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    authorized = project / "report.txt"
    authorized.write_text("原文", encoding="utf-8")
    other = project / "other.txt"
    other.write_text("别的文件", encoding="utf-8")

    reviewer = FakeReviewer(ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6b", text="修改项目报告")
    captured: dict = {}
    patch_child_body(
        monkeypatch,
        security,
        captured,
        writes=[path_effect(EffectKind.WRITE, other)],
        reads=[path_effect(EffectKind.READ, other)],
        # 动作边界：已核实对象上的 delete 不在 allowed_operations（write）里 → 拒绝。
        deletes=[path_effect(EffectKind.DELETE, authorized)],
    )
    tool = make_subagent_tool(security)

    with instruction_context(instruction), requirement_context(requirement.binding):
        await security.engine.authorize(path_effect(EffectKind.WRITE, authorized))
        result = await tool.run(query="整理项目报告")

    assert result.ok is True, result.error
    # 对象边界：别的文件不在授权对象里 → 读写都拒绝。
    assert captured["write"][0]["verdict"] == "deny", captured["write"][0]["reason"]
    assert captured["read"][0]["verdict"] == "deny", captured["read"][0]["reason"]
    assert captured["delete"][0]["verdict"] == "deny", captured["delete"][0]["reason"]


async def test_f6_revoked_or_stale_authorization_not_delegated(
    db_conn, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    authorized = project / "report.txt"
    authorized.write_text("原文", encoding="utf-8")

    reviewer = FakeReviewer(ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6c", text="修改项目报告")
    captured: dict = {}
    patch_child_body(
        monkeypatch, security, captured, writes=[path_effect(EffectKind.WRITE, authorized)]
    )
    tool = make_subagent_tool(security)

    with instruction_context(instruction), requirement_context(requirement.binding):
        await security.engine.authorize(path_effect(EffectKind.WRITE, authorized))
        records = security.authorizations.records_for_requirement(
            requirement.requirement.requirement_id
        )
        assert len(records) == 1
        result_granted = await tool.run(query="整理项目报告")
        assert result_granted.data["agent_scope"]["delegation"]["state"] == "granted"
        assert captured["write"][0]["verdict"] == "allow"

        # 用户/后端撤销这条授权记录：新的子任务不得再继承。
        security.authorizations.revoke(records[0].record_id)
        result_revoked = await tool.run(query="整理项目报告再试一次")

    assert result_revoked.ok is True, result_revoked.error
    delegation = result_revoked.data["agent_scope"]["delegation"]
    assert delegation["state"] == "revoked", delegation
    assert captured["write"][-1]["verdict"] == "deny", captured["write"][-1]["reason"]
    revoked_requests = result_revoked.data["scope_requests"]
    assert revoked_requests and revoked_requests[0]["state"] == "revoked", revoked_requests

    # 要求版本/状态变化（修正=旧授权立即失效）后也不得被当成可委派范围。
    with instruction_context(instruction), requirement_context(requirement.binding):
        corrected = security.requirements.add_correction(
            requirement.requirement.requirement_id, "把标题改成 R9"
        )
        stale_binding = RequirementBinding(
            requirement_id=corrected.requirement_id,
            version=int(corrected.version),
        )
        with requirement_context(stale_binding):
            result_stale = await tool.run(query="整理项目报告第三次")
    assert result_stale.ok is True, result_stale.error
    stale_delegation = result_stale.data["agent_scope"]["delegation"]
    assert stale_delegation["state"] in {"requested", "superseded"}, stale_delegation
    assert stale_delegation["requirement_id"] == corrected.requirement_id
    assert stale_delegation["grants"] == [], "旧版本的授权不得被当成新版本的可委派范围"
    stale_requests = result_stale.data["scope_requests"]
    assert stale_requests and stale_requests[0]["state"] == "requested", stale_requests
    assert captured["write"][-1]["verdict"] == "deny", captured["write"][-1]["reason"]


async def test_f6_empty_scope_research_subtask_still_runs(db_conn, tmp_path):
    """相邻正常用例：没有文件权限范围的研究型子任务不得被无故拒绝创建。"""
    security = make_security(
        db_conn, tmp_path, mode="restricted", reviewer=FakeReviewer()
    )
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6e", text="查一下项目约定")
    tool = make_subagent_tool(security, child_adapter=QuietChildAdapter())

    with instruction_context(instruction), requirement_context(requirement.binding):
        result = await tool.run(query="研究项目约定")

    assert result.ok is True, result.error
    assert "子任务完成" in (result.content or "")
    delegation = result.data["agent_scope"]["delegation"]
    assert delegation["state"] == "requested", delegation
    assert delegation["requirement_id"] == requirement.requirement.requirement_id
    assert delegation["escalation"] == "main_agent", delegation
    assert delegation["grants"] == []
    requests = result.data["scope_requests"]
    assert requests and requests[0]["state"] == "requested", requests
    assert requests[0]["escalation"] == "main_agent", requests[0]


async def test_f6_authorization_stays_bound_to_its_requirement(
    db_conn, tmp_path, monkeypatch
):
    """不把一次许可扩大成长期通配授权：别的/后续要求不得继承旧授权。"""
    project = tmp_path / "project"
    project.mkdir()
    authorized = project / "report.txt"
    authorized.write_text("原文", encoding="utf-8")

    reviewer = FakeReviewer(ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement_a = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6f", text="修改项目报告")
    captured: dict = {}
    patch_child_body(
        monkeypatch, security, captured, writes=[path_effect(EffectKind.WRITE, authorized)]
    )
    tool = make_subagent_tool(security)

    with instruction_context(instruction), requirement_context(requirement_a.binding):
        await security.engine.authorize(path_effect(EffectKind.WRITE, authorized))
        result_a = await tool.run(query="整理项目报告")
    assert result_a.data["agent_scope"]["delegation"]["state"] == "granted"
    assert captured["write"][0]["verdict"] == "allow", captured["write"][0]["reason"]

    # 换一条用户要求（不同 requirement_id + 版本）：旧授权不得被继承。
    requirement_b = security.requirements.record_user_message(
        text="另一件事：只看一下报告标题。",
        message_id="msg_other_requirement",
        turn_id="turn_other_requirement",
    )
    binding_b = RequirementBinding(
        requirement_id=requirement_b.requirement_id,
        version=int(requirement_b.version),
    )
    captured_b: dict = {}
    patch_child_body(
        monkeypatch, security, captured_b, writes=[path_effect(EffectKind.WRITE, authorized)]
    )
    with instruction_context(instruction), requirement_context(binding_b):
        result_b = await tool.run(query="整理项目报告")

    delegation_b = result_b.data["agent_scope"]["delegation"]
    assert delegation_b["requirement_id"] == requirement_b.requirement_id
    assert delegation_b["grants"] == [], "旧要求的授权不得变成新要求的通配授权"
    assert delegation_b["state"] == "requested", delegation_b
    assert captured_b["write"][0]["verdict"] == "deny", captured_b["write"][0]["reason"]


async def test_f6_read_only_escalates_to_user_not_silent_grant(db_conn, tmp_path):
    """主智能体自己也没有权限时，结构化申请升级给用户（read_only → user）。"""
    project = tmp_path / "project"
    project.mkdir()
    allowed = project / "report.txt"
    allowed.write_text("原文", encoding="utf-8")
    needed = project / "appendix.txt"
    needed.write_text("附录", encoding="utf-8")

    security = make_security(db_conn, tmp_path, mode="read_only")
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6g", text="只读查看项目")
    assert security.instructions.add_grant(
        instruction,
        operations={"read"},
        objects=[Scope("path", str(allowed))],
        purpose="用户批准读取这份报告",
    )
    child_adapter = ScriptedChildAdapter(
        [
            (
                "request_agent_scope",
                {
                    "operations": ["write"],
                    "objects": [{"kind": "path", "value": str(needed)}],
                    "reason": "只读模式下无法写附录",
                },
            )
        ]
    )
    tool = make_subagent_tool(security, child_adapter=child_adapter)

    with instruction_context(instruction), requirement_context(requirement.binding):
        result = await tool.run(query="整理项目报告")

    requests = result.data["scope_requests"]
    assert requests, "主智能体也没有权限时必须形成结构化申请"
    assert requests[0]["state"] == "requested", requests[0]
    assert requests[0]["escalation"] == "user", (
        "只读模式下主智能体没有写权限，申请必须升级给用户而不是静默放宽"
    )
    delegation = result.data["agent_scope"]["delegation"]
    assert "write" not in delegation["operations"], "结构化申请不得被静默当成已授予"
    assert "read" in delegation["operations"]


# --------------------------------------------------------------------------
# F6：子权限不足先向主智能体申请；请求/授予/重试/撤销使用结构化状态
# --------------------------------------------------------------------------


async def _child_file_effects(security, monkeypatch, captured, path, *, write=True, read=True):
    patch_child_body(
        monkeypatch,
        security,
        captured,
        writes=[path_effect(EffectKind.WRITE, path)] if write else [],
        reads=[path_effect(EffectKind.READ, path)] if read else [],
    )


async def test_f6_child_request_grant_retry_revoke_structured(db_conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    allowed = project / "report.txt"
    allowed.write_text("原文", encoding="utf-8")
    needed = project / "appendix.txt"
    needed.write_text("附录", encoding="utf-8")

    reviewer = FakeReviewer(ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f6d", text="整理项目报告")
    assert security.instructions.add_grant(
        instruction,
        operations={"read", "write"},
        objects=[Scope("path", str(allowed))],
        purpose="用户批准整理这份报告",
    )
    # 子模型：先向主智能体提交结构化范围申请，再给出结论（真实 AgentLoop +
    # 真实 request_agent_scope 工具；模型是脚本化 fake）。
    child_adapter = ScriptedChildAdapter(
        [
            (
                "request_agent_scope",
                {
                    "operations": ["write", "read"],
                    "objects": [{"kind": "path", "value": str(needed)}],
                    "reason": "子任务需要写入附录文件才能完成",
                },
            )
        ]
    )
    tool = make_subagent_tool(security, child_adapter=child_adapter)

    with instruction_context(instruction), requirement_context(requirement.binding):
        result_requested = await tool.run(query="整理项目报告")
        # 请求是结构化状态：不是一句错误文字。
        requests = result_requested.data["scope_requests"]
        assert requests, "子权限不足时必须形成结构化范围申请"
        request = requests[0]
        assert request["state"] == "requested", request
        assert request["requirement_id"] == requirement.requirement.requirement_id
        assert request["requirement_version"] == int(requirement.binding.version)
        assert request["escalation"] == "main_agent", request
        assert {"write", "read"} <= set(request["operations"])
        assert [item["value"] for item in request["objects"]] == [str(needed)]
        assert request["request_id"]
        assert request["attempts"] >= 1

        # 主智能体自己有权限时先由主智能体取得核实授权（不再直接要求用户）。
        parent = await security.engine.authorize(path_effect(EffectKind.WRITE, needed))
        assert parent.verdict == Verdict.ALLOW, parent.reason

        # 重试：新子 scope 覆盖申请内容 → 申请状态 granted，子任务真实动作放行。
        captured: dict = {}
        await _child_file_effects(security, monkeypatch, captured, needed)
        result_retried = await tool.run(query="整理项目报告")
        retried = result_retried.data["scope_requests"][0]
        assert retried["state"] == "granted", retried
        assert retried["attempts"] >= request["attempts"]
        assert captured["write"][0]["verdict"] == "allow", captured["write"][0]["reason"]
        assert result_retried.data["agent_scope"]["delegation"]["state"] == "granted"

        # 授权撤销后：申请状态 revoked，子任务读取/写入重新被拒绝。
        for record in security.authorizations.records_for_requirement(
            requirement.requirement.requirement_id
        ):
            security.authorizations.revoke(record.record_id)
        revoked_captured: dict = {}
        await _child_file_effects(security, monkeypatch, revoked_captured, needed)
        result_revoked = await tool.run(query="整理项目报告")
        revoked = result_revoked.data["scope_requests"][0]
        assert revoked["state"] == "revoked", revoked
        assert revoked_captured["write"][0]["verdict"] == "deny", (
            revoked_captured["write"][0]["reason"]
        )
        assert result_revoked.data["agent_scope"]["delegation"]["state"] == "revoked"


# --------------------------------------------------------------------------
# F2（追加）：授权记录被撤销后，已经形成的子范围读取也必须立即停止
# --------------------------------------------------------------------------


async def test_f2_authorization_record_revocation_stops_child_read(
    db_conn, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    authorized = project / "report.txt"
    authorized.write_text("原文", encoding="utf-8")

    reviewer = FakeReviewer(ReviewDecision.ALLOW)
    security = make_security(db_conn, tmp_path, mode="restricted", reviewer=reviewer)
    requirement = make_requirement(security)
    instruction = security.instructions.begin(turn_id="turn_f2b", text="修改项目报告")
    captured: dict = {}
    patch_child_body(
        monkeypatch, security, captured, reads=[path_effect(EffectKind.READ, authorized)]
    )
    tool = make_subagent_tool(security)

    with instruction_context(instruction), requirement_context(requirement.binding):
        await security.engine.authorize(path_effect(EffectKind.WRITE, authorized))
        await tool.run(query="整理项目报告")
        scope = captured["scope"]
        assert captured["read"][0]["verdict"] == "allow", captured["read"][0]["reason"]

        record = security.authorizations.records_for_requirement(
            requirement.requirement.requirement_id
        )[0]
        security.authorizations.revoke(record.record_id)
        with agent_scope_context(scope):
            after = await security.engine.authorize(
                path_effect(EffectKind.READ, authorized)
            )

    assert after.verdict == Verdict.DENY, (
        "授权记录撤销后，已形成子范围的读取必须立即停止：" + after.reason
    )
