"""R9-H3 B：移除完全无限制模式的独立授权审查（真实引擎/台账/审批，fake 模型）。

核心证据（不能只断言标记进入输入）：
* review.py 的数据类不再有 targeted 字段；只有 restricted 可通过 validate；普通模型审查器
  在 unrestricted 请求上于模型调用之前拒绝（fake 模型调用 0）。
* 真实引擎：unrestricted + unresolved（有无已知限制）/structured/批量/重复 authorize 与
  recheck 均不调用普通模型审查；已知限制仍拒绝；restricted 仍调用；read_only 不新增；
  内部数据三次确认、永久删除规则不变。
* 迁移 63 撤销的旧 targeted 授权记录不能再复用，行/审计保留。

注意：本分支基线 engine.py 仍保留 A 负责删除的 targeted 分支（契约 §5：A 改 engine）。
因此 unrestricted+unresolved 场景在 B 分支上 engine 仍可能「调用」reviewer，但 review.py
会先于模型调用拒绝；测试断言的是模型调用 0 且结论不为 ALLOW。A 合入后 reviewer.review
本身也不会被调用。该依赖已在 docs/r9h3-reports/B.md 中说明。
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import Settings
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.gateway import AuthContext
from agent.security.requirements import (
    AuthorizationLedger,
    AuthorizationRecord,
    RequirementLedger,
)
from agent.security.review import (
    ModelAuthorizationReviewer,
    ReviewDecision,
    ReviewObject,
    ReviewOperation,
    ReviewOutcome,
    ReviewRequest,
    ReviewUnavailable,
    build_review_payload,
    validate_outcome,
)
from agent.storage.settings import SettingsStore

_NOW = "2026-10-23T00:00:00+00:00"


# --------------------------------------------------------------------------
# 真实装配：引擎 + 真实台账/授权记录
# --------------------------------------------------------------------------


def make_engine(tmp_path, conn, *, mode, reviewer, internal_paths=()):
    store = SettingsStore(conn)
    store.set("computer.permission_mode", mode)
    requirements = RequirementLedger(conn)
    authorizations = AuthorizationLedger(conn)
    engine = AuthorizationEngine(
        conn=conn,
        settings=store,
        workspace_root=lambda: tmp_path,
        internal_data_paths=lambda: list(internal_paths),
        requirements=requirements,
        authorizations=authorizations,
        reviewer=reviewer,
    )
    return SimpleNamespace(
        engine=engine,
        requirements=requirements,
        authorizations=authorizations,
        store=store,
        tmp_path=tmp_path,
    )


def new_requirement(
    ledger: RequirementLedger,
    text: str,
    *,
    limits=(),
    state: str | None = None,
):
    requirement = ledger.record_user_message(
        text=text, message_id="m_" + uuid.uuid4().hex[:8], turn_id="t_r9h3"
    )
    if state is not None:
        ledger.set_structured_limits(
            requirement.requirement_id,
            tuple(dict(item) for item in (limits or ())),
            source="r9h3_test",
            state=state,
        )
    refreshed = ledger.by_id(requirement.requirement_id)
    assert refreshed is not None
    return refreshed


def limit_for(path: Path, reason: str = "不要修改这个文件") -> dict:
    return {
        "operations": ["write"],
        "objects": [{"kind": "path", "value": str(path.resolve())}],
        "reason": reason,
    }


def write_effect(path) -> Effect:
    path = Path(path)
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path), identity="vol:test-file"),),
        purpose="修改报告内容",
        detail={"argv": [], "cwd": str(path.parent)},
    )


def read_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path), identity="vol:test-file"),),
        purpose="读取材料",
        detail={"argv": [], "cwd": str(Path(path).parent)},
    )


def permanent_delete_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.DELETE,
        targets=(Target("path", str(path), identity="vol:test-file"),),
        purpose="永久删除测试对象",
        detail={"permanent": True},
    )


def ctx_for(binding) -> AuthContext:
    return AuthContext(requirement=binding)


# --------------------------------------------------------------------------
# Fake 审查器：记录调用；普通模型审查器用真实类 + fake model_call
# --------------------------------------------------------------------------


def allow_outcome(request: ReviewRequest) -> ReviewOutcome:
    operation = request.operation
    return ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="测试用允许",
        requirement_id=request.requirement.requirement_id,
        allowed_operations=(operation.kind,),
        objects=operation.objects,
        limits=("仅本次",),
        purpose=operation.summary or "测试",
    )


class RecordingReviewer:
    def __init__(self) -> None:
        self.calls: list = []

    async def review(self, request):
        self.calls.append(request)
        return allow_outcome(request)


class CountingModelReviewer(ModelAuthorizationReviewer):
    """真实 ModelAuthorizationReviewer + fake model_call；记录模型调用次数。"""

    def __init__(self) -> None:
        self.model_calls: list = []
        self.review_calls = 0

        async def model_call(messages):
            self.model_calls.append(messages)
            return json.dumps({"decision": "allow"}, ensure_ascii=False)

        super().__init__(adapter_factory=None, model_call=model_call)

    async def review(self, request):
        self.review_calls += 1
        return await super().review(request)


def make_request(
    ledger: RequirementLedger,
    *,
    mode: str,
    path: Path,
    requirement=None,
    **legacy,
) -> ReviewRequest:
    requirement = requirement or ledger.record_user_message(
        text="修改报告。", message_id="m_req", turn_id="t_req"
    )
    operation = ReviewOperation(
        kind="write",
        objects=(ReviewObject(kind="path", value=str(path)),),
        summary="修改报告内容",
    )
    return ReviewRequest(
        requirement=requirement,
        corrections=(),
        limits=(),
        mode=mode,
        operation=operation,
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
        requirement_resolution_status="resolved",
        **legacy,
    )


# --------------------------------------------------------------------------
# 1) review.py：字段删除、只有 restricted、模型调用前拒绝
# --------------------------------------------------------------------------


def test_review_request_no_longer_has_targeted_field(tmp_path, db_conn):
    names = {item.name for item in dataclasses.fields(ReviewRequest)}
    assert "targeted_limits_review" not in names
    ledger = RequirementLedger(db_conn)
    request = make_request(
        ledger, mode="unrestricted", path=tmp_path / "a.md", targeted_limits_review=True
    )
    assert not hasattr(request, "targeted_limits_review"), "旧 engine 传参只做兼容忽略，不保留字段"
    payload = build_review_payload(request)
    assert "targeted_limits_review" not in payload
    assert "targeted" not in json.dumps(payload, ensure_ascii=False)


def test_validate_outcome_rejects_unrestricted_request(tmp_path, db_conn):
    ledger = RequirementLedger(db_conn)
    request = make_request(ledger, mode="unrestricted", path=tmp_path / "a.md")
    with pytest.raises(ReviewUnavailable):
        validate_outcome(allow_outcome(request), request)

    restricted = make_request(ledger, mode="restricted", path=tmp_path / "a.md")
    validated = validate_outcome(allow_outcome(restricted), restricted)
    assert validated.decision == ReviewDecision.ALLOW


@pytest.mark.asyncio
async def test_model_reviewer_rejects_unrestricted_before_model_call(tmp_path, db_conn):
    ledger = RequirementLedger(db_conn)
    request = make_request(ledger, mode="unrestricted", path=tmp_path / "a.md")
    reviewer = CountingModelReviewer()
    with pytest.raises(ReviewUnavailable):
        await reviewer.review(request)
    assert reviewer.model_calls == [], "unrestricted 必须在模型调用之前拒绝"


# --------------------------------------------------------------------------
# 2) 真实引擎：unrestricted 不执行普通独立授权审查
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_unrestricted_unresolved_does_not_call_model_reviewer(
    tmp_path, db_conn, known
):
    reviewer = CountingModelReviewer()
    path_b = tmp_path / "B报告.md"
    known_limits = (limit_for(path_b),) if known else ()
    security = make_engine(tmp_path, db_conn, mode="unrestricted", reviewer=reviewer)
    requirement = new_requirement(
        security.requirements,
        "修复项目，但不要修改 B。",
        limits=known_limits,
        state="unresolved",
    )
    binding = requirement.as_binding()

    decision = await security.engine.authorize(
        write_effect(path_b), ctx=ctx_for(binding)
    )

    assert reviewer.model_calls == [], "完全无限制模式不得执行普通模型授权审查"
    assert decision.verdict != Verdict.ALLOW, "未恢复的限制不得静默放行"
    # A 合入后 engine 不再调用 reporter；B 分支由 review.py 在模型前拒绝。
    assert not hasattr(decision, "targeted_limits_review")


@pytest.mark.asyncio
async def test_unrestricted_structured_requirement_no_review_and_allowed(tmp_path, db_conn):
    reviewer = CountingModelReviewer()
    security = make_engine(tmp_path, db_conn, mode="unrestricted", reviewer=reviewer)
    requirement = new_requirement(
        security.requirements, "整理 A 报告。", state="structured"
    )
    path_a = tmp_path / "A报告.md"

    decision = await security.engine.authorize(
        write_effect(path_a), ctx=ctx_for(requirement.as_binding())
    )

    assert decision.verdict == Verdict.ALLOW
    assert reviewer.review_calls == 0
    assert reviewer.model_calls == []


@pytest.mark.asyncio
async def test_unrestricted_known_limits_still_deny(tmp_path, db_conn):
    reviewer = CountingModelReviewer()
    security = make_engine(tmp_path, db_conn, mode="unrestricted", reviewer=reviewer)
    path_a = tmp_path / "A报告.md"
    path_b = tmp_path / "B报告.md"
    requirement = new_requirement(
        security.requirements,
        "只改 A，不要修改 B。",
        limits=(limit_for(path_b),),
        state="structured",
    )
    binding = requirement.as_binding()

    denied = await security.engine.authorize(write_effect(path_b), ctx=ctx_for(binding))
    allowed = await security.engine.authorize(write_effect(path_a), ctx=ctx_for(binding))

    assert denied.verdict == Verdict.DENY, denied.reason
    assert allowed.verdict == Verdict.ALLOW, allowed.reason
    assert reviewer.model_calls == []


@pytest.mark.asyncio
async def test_unresolved_with_known_limits_not_silently_allowed(tmp_path, db_conn):
    """unresolved 的已知限制优先级由 A（engine 顺序）修复；B 分支断言不静默放行。

    A 合入后这里应是 DENY（见 test_r9h3_limits_priority）；当前基线 unresolved guard
    先返回 APPROVE，因此 B 只断言 != ALLOW 且无模型审查，风险在报告中如实记录。
    """
    reviewer = CountingModelReviewer()
    security = make_engine(tmp_path, db_conn, mode="unrestricted", reviewer=reviewer)
    path_b = tmp_path / "B报告.md"
    requirement = new_requirement(
        security.requirements,
        "不要修改 B。",
        limits=(limit_for(path_b),),
        state="unresolved",
    )

    decision = await security.engine.authorize(
        write_effect(path_b), ctx=ctx_for(requirement.as_binding())
    )

    assert decision.verdict != Verdict.ALLOW
    assert reviewer.model_calls == []


@pytest.mark.asyncio
async def test_unrestricted_batch_and_repeated_recheck_no_model_review(tmp_path, db_conn):
    reviewer = CountingModelReviewer()
    security = make_engine(tmp_path, db_conn, mode="unrestricted", reviewer=reviewer)
    path_a = tmp_path / "A.md"
    path_b = tmp_path / "B.md"

    unresolved = new_requirement(
        security.requirements, "修复项目。", state="unresolved"
    )
    batch = await security.engine.authorize_many(
        [write_effect(path_a), write_effect(path_b)],
        ctx=ctx_for(unresolved.as_binding()),
    )
    assert batch.verdict == Verdict.APPROVE
    assert reviewer.model_calls == []

    for _ in range(3):
        decision = await security.engine.authorize(
            write_effect(path_a), ctx=ctx_for(unresolved.as_binding())
        )
        assert decision.verdict != Verdict.ALLOW
        recheck = security.engine.recheck_approved(
            write_effect(path_a), context=ctx_for(unresolved.as_binding())
        )
        assert recheck.verdict != Verdict.ALLOW
    assert reviewer.model_calls == [], "重复 authorize/recheck 不得反复调用普通审查"

    structured = new_requirement(
        security.requirements, "整理报告。", state="structured"
    )
    review_calls_before = reviewer.review_calls
    batch_structured = await security.engine.authorize_many(
        [write_effect(path_a), write_effect(path_b)],
        ctx=ctx_for(structured.as_binding()),
    )
    assert batch_structured.verdict == Verdict.ALLOW
    assert reviewer.review_calls == review_calls_before, "structured 批量不应新增审查调用"
    assert reviewer.model_calls == []


@pytest.mark.asyncio
async def test_recovery_success_continues_normally(tmp_path, db_conn):
    reviewer = CountingModelReviewer()
    security = make_engine(tmp_path, db_conn, mode="unrestricted", reviewer=reviewer)
    path_a = tmp_path / "A.md"
    path_b = tmp_path / "B.md"
    requirement = new_requirement(
        security.requirements, "修复项目。", state="unresolved"
    )
    # 真实台账上模拟历史恢复成功：保存完整结构化限制（不是测试预构造最终 DENY）
    security.requirements.set_structured_limits(
        requirement.requirement_id,
        (limit_for(path_b),),
        source="recovered",
        state="structured",
    )
    binding = requirement.as_binding()
    bound = security.requirements.by_id(requirement.requirement_id)
    assert bound is not None and bound.structured_limits_state == "structured"

    denied = await security.engine.authorize(write_effect(path_b), ctx=ctx_for(binding))
    allowed = await security.engine.authorize(write_effect(path_a), ctx=ctx_for(binding))
    assert denied.verdict == Verdict.DENY
    assert allowed.verdict == Verdict.ALLOW
    assert reviewer.model_calls == []


# --------------------------------------------------------------------------
# 3) restricted 仍调用、read_only 不新增、内部数据/永久删除规则不变
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restricted_still_reviews(tmp_path, db_conn):
    reviewer = RecordingReviewer()
    security = make_engine(tmp_path, db_conn, mode="restricted", reviewer=reviewer)
    requirement = new_requirement(
        security.requirements, "整理报告。", state="structured"
    )
    decision = await security.engine.authorize(
        write_effect(tmp_path / "报告.md"), ctx=ctx_for(requirement.as_binding())
    )
    assert len(reviewer.calls) == 1, "restricted 仍应走既有模型审查"
    assert decision.verdict == Verdict.ALLOW


@pytest.mark.asyncio
async def test_read_only_does_not_add_review(tmp_path, db_conn):
    reviewer = RecordingReviewer()
    security = make_engine(tmp_path, db_conn, mode="read_only", reviewer=reviewer)
    requirement = new_requirement(
        security.requirements, "整理报告。", state="structured"
    )
    decision = await security.engine.authorize(
        write_effect(tmp_path / "报告.md"), ctx=ctx_for(requirement.as_binding())
    )
    assert decision.verdict == Verdict.DENY
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_internal_data_and_permanent_delete_rules_unchanged(tmp_path, db_conn):
    reviewer = CountingModelReviewer()
    protected = tmp_path / "runtime" / "qio.db"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("db", encoding="utf-8")
    security = make_engine(
        tmp_path, db_conn, mode="unrestricted", reviewer=reviewer, internal_paths=[protected]
    )
    requirement = new_requirement(
        security.requirements, "维护运行数据。", state="structured"
    )
    binding = requirement.as_binding()

    internal_decision = await security.engine.authorize(
        write_effect(protected), ctx=ctx_for(binding)
    )
    assert internal_decision.verdict == Verdict.APPROVE
    assert "三次确认" in internal_decision.reason
    assert reviewer.model_calls == []

    permanent = await security.engine.authorize(
        permanent_delete_effect(tmp_path / "out.txt"), ctx=ctx_for(binding)
    )
    assert permanent.verdict == Verdict.ALLOW
    assert "永久删除" in permanent.reason
    assert reviewer.model_calls == []


# --------------------------------------------------------------------------
# 4) 迁移 63 撤销的旧 targeted 授权记录：不能复用，行/审计保留
# --------------------------------------------------------------------------


def test_revoked_targeted_records_cannot_be_reused_and_are_preserved(tmp_path, db_conn):
    ledger = AuthorizationLedger(db_conn)
    requirements = RequirementLedger(db_conn)
    requirement = requirements.record_user_message(
        text="旧要求。", message_id="m_targeted", turn_id="t_targeted"
    )
    revoked = AuthorizationRecord(
        record_id="authrec_targeted_" + uuid.uuid4().hex[:8],
        requirement_id=requirement.requirement_id,
        operation_kind="write",
        fingerprint="fp-targeted-revoked",
        decision="allow",
        reason="R9-H2 targeted 例外产生的旧允许",
        allowed_operations=("write",),
        objects=(),
        limits=("仅本次",),
        source="model_review",
        mode="unrestricted",
        created_at=_NOW,
        expires_at=None,
        revoked_at=_NOW,
        review_id="rev_targeted_1",
    )
    ledger.record(revoked)

    assert (
        ledger.reuse(
            requirement_id=requirement.requirement_id,
            operation_kind="write",
            fingerprint="fp-targeted-revoked",
        )
        is None
    ), "迁移撤销后的旧 targeted 记录不得复用"

    rows = ledger.records_for_requirement(requirement.requirement_id, include_revoked=True)
    assert any(item.record_id == revoked.record_id for item in rows), "行/审计必须保留"
    kept = next(item for item in rows if item.record_id == revoked.record_id)
    assert kept.revoked_at is not None
    assert kept.reason == revoked.reason
    assert kept.review_id == "rev_targeted_1"

    # 未撤销的旧记录仍可被查询到（迁移只撤销可明确识别的 targeted 记录，
    # 不删除全部历史授权），但本用例不把它当作 B 分支可改写的事实。
    active = AuthorizationRecord(
        record_id="authrec_other_" + uuid.uuid4().hex[:8],
        requirement_id=requirement.requirement_id,
        operation_kind="write",
        fingerprint="fp-other",
        decision="allow",
        reason="其他来源允许",
        allowed_operations=("write",),
        objects=(),
        limits=(),
        source="user_approval",
        mode="restricted",
        created_at=_NOW,
        expires_at=None,
        revoked_at=None,
        review_id=None,
    )
    ledger.record(active)
    rows_after = ledger.records_for_requirement(requirement.requirement_id, include_revoked=True)
    assert len(rows_after) == 2
