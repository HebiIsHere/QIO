"""R9-F 引擎接入：受限模式模型授权审查 / 有界授权记录复用 / 后端来源（契约 §2、§3.5）。

用真实 RequirementLedger / AuthorizationLedger（迁移 55）+ 可编程 reviewer 替身，
验证引擎的接线与失败语义；模型调用本身由 W1 的 test_r9f_review 覆盖。
"""

from __future__ import annotations

import pytest

from agent.security.context import current_requirement
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.review import (
    ReviewDecision,
    ReviewOutcome,
    ReviewUnavailable,
    operation_fingerprint,
)
from agent.security.requirements import AuthorizationLedger, RequirementLedger
from agent.storage.settings import SettingsStore


class StubReviewer:
    """可编程审查器：记录请求、按队列返回结果、可统计调用次数。"""

    def __init__(self, *, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    async def review(self, request):
        self.calls.append(request)
        if isinstance(self.outcomes, Exception):
            raise self.outcomes
        if not self.outcomes:
            raise ReviewUnavailable("没有配置审查结果")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome(request)
        return outcome


def allow_outcome(request, *, reason="用户明确要求修改这个文件"):
    op = request.operation
    return ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason=reason,
        requirement_id=request.requirement.requirement_id,
        allowed_operations=(op.kind,),
        objects=op.objects,
        limits=("仅限本次操作",),
        purpose=op.summary or "用户要求",
    )


@pytest.fixture(autouse=True)
def _isolate_requirement_context():
    token = current_requirement.set(None)
    yield
    current_requirement.reset(token)


def make_engine(tmp_path, conn, *, mode="restricted", reviewer=None, origin=None):
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    requirements = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    engine = AuthorizationEngine(
        conn=conn,
        settings=store,
        workspace_root=lambda: tmp_path,
        requirements=requirements,
        authorizations=authorizations,
        reviewer=reviewer,
        origin_provider=(lambda path: origin) if origin is not None else None,
    )
    requirement = requirements.record_user_message(
        text="把报告改成新的结论并保存",
        message_id="msg_1",
        turn_id="turn_1",
        topic_id="topic_1",
    )
    # R9-G：操作必须显式绑定具体要求（不再回退"最近一条 active"）。测试在
    # 当前测试上下文设置该绑定，模拟 turn 运行时的 requirement_context。
    binding = requirements.binding_for(requirement.requirement_id)
    current_requirement.set(binding)
    return engine, requirements, authorizations, requirement


def write_effect(path):
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path), identity="vol:file"),),
        purpose="修改报告内容",
        detail={"argv": [], "cwd": str(path.parent)},
    )


def execute_effect(path, *, program=None, argv=(), cwd=None):
    program = program or str(path)
    return Effect(
        kind=EffectKind.EXECUTE,
        targets=(Target("program", program),),
        purpose="运行项目测试",
        detail={"program": program, "argv": list(argv), "cwd": str(cwd or path.parent)},
    )


async def test_restricted_write_allowed_by_review_and_reused(db_conn, tmp_path):
    reviewer = StubReviewer()
    reviewer.outcomes = [allow_outcome]
    engine, requirements, authorizations, _ = make_engine(
        tmp_path, db_conn, reviewer=reviewer
    )
    effect = write_effect(tmp_path / "报告.md")

    first = await engine.authorize(effect)
    assert first.verdict == Verdict.ALLOW, first.reason
    assert "模型审查确认" in first.reason
    assert reviewer.calls and reviewer.calls[0].requirement.text == "把报告改成新的结论并保存"
    assert len(authorizations.records_for_requirement(
        reviewer.calls[0].requirement.requirement_id
    )) == 1

    # 同一条要求 + 同一 fingerprint：复用记录，不再调用模型
    second = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert second.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1, "有效范围内的重复操作不应反复调用模型"


async def test_review_confirm_maps_to_ordinary_approval(db_conn, tmp_path):
    def confirm(request):
        return ReviewOutcome(
            decision=ReviewDecision.CONFIRM,
            reason="用户只说整理，没有明确允许覆盖这个文件",
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(request.operation.kind,),
            objects=request.operation.objects,
            limits=(),
            purpose="需要用户确认",
        )

    reviewer = StubReviewer()
    reviewer.outcomes = [confirm]
    engine, *_ = make_engine(tmp_path, db_conn, reviewer=reviewer)
    decision = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert decision.verdict == Verdict.APPROVE
    assert "需要你确认" in decision.reason


async def test_review_deny_is_not_converted_to_approval(db_conn, tmp_path):
    def deny(request):
        return ReviewOutcome(
            decision=ReviewDecision.DENY,
            reason="用户明确说不要修改这个文件",
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(),
            objects=(),
            limits=(),
            purpose="与用户要求冲突",
        )

    reviewer = StubReviewer()
    reviewer.outcomes = [deny]
    engine, *_ = make_engine(tmp_path, db_conn, reviewer=reviewer)
    decision = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert decision.verdict == Verdict.DENY
    assert "冲突" in decision.reason
    assert "不会转成普通审批" in decision.reason


async def test_review_unavailable_does_not_default_allow(db_conn, tmp_path):
    reviewer = StubReviewer()
    reviewer.outcomes = [ReviewUnavailable("模型超时")]
    engine, *_ = make_engine(tmp_path, db_conn, reviewer=reviewer)
    decision = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert decision.verdict == Verdict.APPROVE
    assert decision.needs_review is True
    assert "无法确认授权范围" in decision.reason


async def test_invalid_review_outcome_does_not_allow(db_conn, tmp_path):
    def bad(request):
        # 结构/对象不合法：validate_outcome 必须拒绝
        return ReviewOutcome(
            decision=ReviewDecision.ALLOW,
            reason="",
            requirement_id="req_not_exists",
            allowed_operations=("write",),
            objects=request.operation.objects,
            limits=(),
            purpose="",
        )

    reviewer = StubReviewer()
    reviewer.outcomes = [bad]
    engine, *_ = make_engine(tmp_path, db_conn, reviewer=reviewer)
    decision = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert decision.verdict == Verdict.APPROVE
    assert "无效" in decision.reason or "无法确认" in decision.reason


async def test_new_requirement_does_not_reuse_old_record(db_conn, tmp_path):
    reviewer = StubReviewer()
    reviewer.outcomes = [allow_outcome, allow_outcome]
    engine, requirements, authorizations, first_requirement = make_engine(
        tmp_path, db_conn, reviewer=reviewer
    )
    assert (await engine.authorize(write_effect(tmp_path / "报告.md"))).verdict == Verdict.ALLOW
    requirements.supersede(first_requirement.requirement_id)
    second_requirement = requirements.record_user_message(
        text="把另一份报告也改一下", message_id="msg_2", turn_id="turn_2"
    )
    # 新的用户要求 → turn 运行时会把绑定切到第二条（不沿用旧记录）。
    current_requirement.set(requirements.binding_for(second_requirement.requirement_id))
    second = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert second.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 2, "新要求必须重新判断，不得继承旧记录"


async def test_read_only_and_unrestricted_do_not_call_reviewer(db_conn, tmp_path):
    for mode, expected in (("read_only", Verdict.DENY), ("unrestricted", Verdict.ALLOW)):
        reviewer = StubReviewer()
        reviewer.outcomes = [allow_outcome]
        engine, *_ = make_engine(tmp_path, db_conn, mode=mode, reviewer=reviewer)
        decision = await engine.authorize(write_effect(tmp_path / "报告.md"))
        assert decision.verdict == expected
        assert reviewer.calls == [], f"{mode} 不应调用普通授权审查"


async def test_backend_origin_qio_generated_allows_without_review(db_conn, tmp_path):
    reviewer = StubReviewer()
    engine, *_ = make_engine(tmp_path, db_conn, reviewer=reviewer, origin="qio_generated")
    decision = await engine.authorize(write_effect(tmp_path / "报告.md"))
    assert decision.verdict == Verdict.ALLOW
    assert "QIO 生成" in decision.reason
    assert reviewer.calls == []


async def test_execute_review_records_program_fingerprint_and_reuses(db_conn, tmp_path):
    reviewer = StubReviewer()
    reviewer.outcomes = [allow_outcome]
    engine, requirements, authorizations, requirement = make_engine(
        tmp_path, db_conn, reviewer=reviewer
    )
    project = tmp_path / "proj"
    project.mkdir()

    first = await engine.authorize(
        execute_effect(project / "a.py", program=str(project / "a.py"), argv=("--check",))
    )
    assert first.verdict == Verdict.ALLOW
    request = reviewer.calls[0]
    assert request.operation.program == str(project / "a.py")
    assert request.operation.argv == ("--check",)
    record = authorizations.records_for_requirement(requirement.requirement_id)[0]
    # 指纹必须绑定真实程序/参数（问题 D：目录授权不得放行任意程序）
    expected = operation_fingerprint(request.operation, mode="restricted")
    assert record.fingerprint == expected

    # 同程序同参数：复用
    second = await engine.authorize(
        execute_effect(project / "a.py", program=str(project / "a.py"), argv=("--check",))
    )
    assert second.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1

    # 换程序：必须重新审查（不能让目录命中放行任意程序）
    reviewer.outcomes = [allow_outcome]
    third = await engine.authorize(
        execute_effect(project / "b.py", program=str(project / "b.py"), argv=())
    )
    assert third.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 2
