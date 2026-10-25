"""R9-H13 Lead 引擎侧验收（基线 e68fb9b7 上先红；修复后必须绿）。

覆盖契约 §3.E 第 1/2/4/7 条（引擎侧）与 §3.I 的引擎语义：
1. 提交审批时由**后端按真实影响对象**确定最低确认次数，不信任调用方填写的 1；
2. 受限模式下模型审查 ALLOW 不能完成内部维护（内部运行数据仍须人工三次确认）；
3. 执行前重核对按后端最低次数判断，历史/错误的一次确认记录不能当三次；
4. 未变化内部对象完成三次确认后仍正常放行（不增加第四次确认）；普通对象维持 1 次。

真实/模拟分界：真实 SQLite 台账 + 真实引擎/审批服务；模型审查用可编程 reviewer 替身
（模拟，不调用真实模型）；内部运行数据用真实临时目录注册。
"""

from __future__ import annotations

import pytest

from agent.security.context import current_requirement
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.requirements import AuthorizationLedger
from agent.security.review import ReviewDecision, ReviewOutcome
from agent.tools.security_gate import effect_to_dict

from _r9_auth_support import bind_requirement, make_security, with_instruction


class StubReviewer:
    """可编程审查器：记录请求、按队列返回结果（与 R9-F 测试同一替身语义）。"""

    def __init__(self, *, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    async def review(self, request):
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(request)
        return outcome


def allow_outcome(request):
    op = request.operation
    return ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="模型认为这属于用户要求",
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


def _write_effect(path) -> Effect:  # noqa: ANN001
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path)),),
        purpose="写入测试对象",
        detail={"content_sha256": "c" * 64},
    )


def _internal_setup(tmp_path):
    internal_dir = tmp_path / "qio-internal"
    internal_dir.mkdir()
    target = internal_dir / "app.db"
    target.write_text("db", encoding="utf-8")
    return internal_dir, target


def _operation(effect: Effect, *, confirmations: int = 1):
    from agent.security.operations import PendingOperation

    return PendingOperation(
        kind="fs.write",
        label="写入测试对象",
        payload={"path": effect.targets[0].value, "content": "x"},
        effect=effect_to_dict(effect),
        confirmations_required=int(confirmations),
        source="tool",
    )


async def test_submit_derives_internal_confirmations_from_real_target(tmp_path, db_conn) -> None:
    internal_dir, target = _internal_setup(tmp_path)
    security = make_security(
        tmp_path, db_conn, mode="restricted", protected=[internal_dir]
    )
    effect = _write_effect(target)
    operation = _operation(effect, confirmations=1)
    bind_requirement(security, operation)
    ticket = await security.engine.submit(operation)
    assert int(ticket.confirmations_required) == 3, (
        "内部运行数据的确认次数必须由后端按真实目标推导，不能沿用调用方填写的 1"
    )
    row = security.approvals.store.get(ticket.approval_id)
    assert int(row["requires_confirmations"]) == 3
    # 普通对象仍是 1 次（相邻正常用例：不因规则整体收紧）。
    ordinary = tmp_path / "report.txt"
    ordinary.write_text("report", encoding="utf-8")
    plain_operation = _operation(_write_effect(ordinary), confirmations=1)
    bind_requirement(security, plain_operation)
    plain_ticket = await security.engine.submit(plain_operation)
    assert int(plain_ticket.confirmations_required) == 1


async def test_internal_approval_needs_three_confirmation_stages(tmp_path, db_conn) -> None:
    internal_dir, target = _internal_setup(tmp_path)
    security = make_security(
        tmp_path, db_conn, mode="restricted", protected=[internal_dir]
    )
    operation = _operation(_write_effect(target), confirmations=1)
    bind_requirement(security, operation)
    ticket = await security.engine.submit(operation)
    first = await security.approvals.confirm(ticket.approval_id, 1)
    assert str(first.get("state") or "") == "waiting", (
        "三次确认未完成前不得进入 approved（历史/错误的一次确认不能当三次）"
    )
    assert int(first.get("confirmed_stage") or 0) == 1
    second = await security.approvals.confirm(ticket.approval_id, 2)
    assert str(second.get("state") or "") == "waiting"
    assert int(second.get("confirmed_stage") or 0) == 2


async def test_model_review_allow_cannot_complete_internal_maintenance(
    tmp_path, db_conn
) -> None:
    internal_dir, target = _internal_setup(tmp_path)
    security = make_security(
        tmp_path, db_conn, mode="restricted", protected=[internal_dir]
    )
    security.engine.authorizations = AuthorizationLedger(db_conn)
    reviewer = StubReviewer(outcomes=[allow_outcome])
    security.engine.reviewer = reviewer
    async with with_instruction(security, "把内部数据整理一下"):
        decision = await security.engine.authorize(_write_effect(target))
    assert decision.verdict == Verdict.APPROVE, (
        "内部运行数据不能被普通模型审查 ALLOW 直接完成，必须转人工三次确认"
    )
    assert reviewer.calls == [], "内部维护不应调用普通独立授权审查"
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM authorization_records WHERE decision = 'allow'"
    ).fetchone()
    assert int(rows["n"]) == 0, "内部维护不能凭模型审查形成 allow 授权记录"


async def test_model_review_allow_still_works_for_ordinary_write(tmp_path, db_conn) -> None:
    security = make_security(tmp_path, db_conn, mode="restricted")
    security.engine.authorizations = AuthorizationLedger(db_conn)
    reviewer = StubReviewer(outcomes=[allow_outcome])
    security.engine.reviewer = reviewer
    ordinary = tmp_path / "report.txt"
    ordinary.write_text("report", encoding="utf-8")
    async with with_instruction(security, "把报告改成新的结论并保存"):
        decision = await security.engine.authorize(_write_effect(ordinary))
    assert decision.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1


def test_recheck_uses_backend_minimum_not_caller_value(tmp_path, db_conn) -> None:
    internal_dir, target = _internal_setup(tmp_path)
    security = make_security(
        tmp_path, db_conn, mode="restricted", protected=[internal_dir]
    )
    # 调用方声称只需要 1 次、已完成 2 次；后端必须按真实内部对象要求 3 次。
    decision = security.engine.recheck_approved(
        _write_effect(target), confirmations_required=1, confirmed_stage=2
    )
    assert decision.verdict == Verdict.APPROVE, (
        "内部维护只完成 2 次确认不能执行；最低次数来自真实影响对象"
    )
    completed = security.engine.recheck_approved(
        _write_effect(target), confirmations_required=1, confirmed_stage=3
    )
    assert completed.verdict == Verdict.ALLOW, "三次确认完成后正常放行，不增加第四次"


def test_recheck_keeps_single_confirmation_for_ordinary_object(tmp_path, db_conn) -> None:
    security = make_security(tmp_path, db_conn, mode="restricted")
    ordinary = tmp_path / "report.txt"
    ordinary.write_text("report", encoding="utf-8")
    decision = security.engine.recheck_approved(
        _write_effect(ordinary), confirmations_required=1, confirmed_stage=1
    )
    assert decision.verdict == Verdict.ALLOW
    # 普通单次审批：批准本身就是唯一确认，confirmed_stage 仍为 0 也必须放行
    # （R9-H13 回归：不能把“需要 1 次”误读成“必须已有 1 次阶段确认”）。
    single = security.engine.recheck_approved(
        _write_effect(ordinary), confirmations_required=1, confirmed_stage=0
    )
    assert single.verdict == Verdict.ALLOW
