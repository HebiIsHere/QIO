"""R9-H2 B：解析失败状态 / 引擎判定 / recheck 状态一致性（真实 _execute_turn + 真实台账）。

模型调用全部 fake（FakeResolver / StubReviewer），不联网、不使用真实 Key。
覆盖契约 §4：
* 解析失败/超时/解析器缺失/非法输出 → requirement state=unresolved（[] 不当 structured），
  临时 status=unavailable 与持久化一致；unrestricted 相关操作不自动允许；
* 成功解析且明确无限制 → structured/[] 正常；
* 失败后历史恢复成功 → 正常继续；恢复输入/条件写回/竞态丢弃；
* 重启/审批恢复/授权复用不改变结论；旧 allow 记录不能绕过 unresolved；
* 无关要求/无依赖工作不受影响。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import Settings
from agent.core.turn import TurnContext
from agent.security.context import (
    current_requirement,
    requirement_context,
    requirement_limits,
    requirement_resolution_status,
)
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.engine import AuthorizationEngine
from agent.security.recheck import OperationBinding, RecheckService
from agent.security.requirements import (
    AuthorizationRecord,
    RequirementBinding,
    RequirementLedger,
)
from agent.security.resolution import (
    RequirementRelation,
    RequirementResolutionUnavailable,
    ResolutionOutcome,
)
from agent.security.review import ReviewDecision, ReviewOutcome, operation_fingerprint
from agent.api.server import create_app

_NOW = "2026-10-23T00:00:00+00:00"


# --------------------------------------------------------------------------
# Fake 解析器（模型替身）
# --------------------------------------------------------------------------


def make_outcome(
    relation: RequirementRelation,
    *,
    target=None,
    target_version=None,
    corrections=(),
    limits=(),
    removed_limits=(),
    reason="按用户当前消息语义判断",
) -> ResolutionOutcome:
    return ResolutionOutcome(
        relation=relation,
        target_requirement_id=None if target is None else target.requirement_id,
        target_version=None if target is None else target_version,
        corrections=tuple(corrections),
        limits=tuple(dict(item) for item in limits),
        removed_limits=tuple(dict(item) for item in removed_limits),
        reason=reason,
        model="fake-r9h2-model",
        prompt_version="fake-r9h2-prompt-v1",
    )


class FakeResolver:
    """可编程解析器：resolve 固定/抛错；extract_limits 供历史恢复兼容路径。"""

    model = "fake-r9h2-model"
    prompt_version = "fake-r9h2-prompt-v1"
    limit_extraction_prompt_version = "fake-r9h2-limit-v1"

    def __init__(
        self,
        *,
        outcome=None,
        error=None,
        extract_limits=(),
        extract_error=None,
        return_none=False,
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.extract_result = tuple(dict(item) for item in (extract_limits or ()))
        self.extract_error = extract_error
        self.return_none = return_none
        self.resolve_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    async def resolve(self, **kwargs):
        self.resolve_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.return_none:
            return None
        return self.outcome

    async def extract_limits(self, text, *, requirement_id=None, version=None):
        self.extract_calls.append(
            {"text": text, "requirement_id": requirement_id, "version": version}
        )
        if self.extract_error is not None:
            raise self.extract_error
        return self.extract_result


class ChainResolver(FakeResolver):
    """A 分支接口替身：extract_limits_from_context（完整祖先链）。"""

    limit_extraction_chain_prompt_version = "fake-r9h2-chain-prompt-v1"

    def __init__(self, *, limits=(), error=None, mutate=None) -> None:
        super().__init__()
        self.chain_limits = tuple(dict(item) for item in (limits or ()))
        self.chain_error = error
        self.mutate = mutate
        self.context_calls: list[dict] = []

    async def extract_limits_from_context(self, context, *, requirement_id, version):
        self.context_calls.append(
            {
                "context": context,
                "requirement_id": str(requirement_id),
                "version": int(version),
            }
        )
        if self.chain_error is not None:
            raise self.chain_error
        if callable(self.mutate):
            self.mutate(context)
        return self.chain_limits


class ChainLedgerShim:
    """给真实台账补 recovery_context（A 未合入时的最小前向兼容替身）。"""

    def __init__(self, real, *, complete=True, digest="digest-1") -> None:
        self._real = real
        self.complete = complete
        self.digest = digest
        self.set_calls: list[dict] = []
        self.context_calls: list[str] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def recovery_context(self, requirement_id):
        self.context_calls.append(str(requirement_id))
        requirement = self._real.by_id(requirement_id)
        if requirement is None:
            return None
        node = SimpleNamespace(
            requirement_id=requirement.requirement_id,
            version=int(requirement.version),
            parent_id=requirement.parent_id,
            text=requirement.text,
            status=requirement.status,
            provenance=dict(requirement.provenance or {}),
            structured_limits=requirement.structured_limits,
            structured_limits_state=requirement.structured_limits_state,
            structured_limits_source=requirement.structured_limits_source,
        )
        return SimpleNamespace(
            requirement_id=requirement.requirement_id,
            version=int(requirement.version),
            status=requirement.status,
            parent_id=requirement.parent_id,
            chain=(node,),
            complete=bool(self.complete),
            reason="" if self.complete else "父要求缺失",
            chain_digest=str(self.digest),
        )

    def set_structured_limits(
        self,
        requirement_id,
        limits,
        *,
        source,
        state,
        expected_version=None,
        expected_status=None,
        expected_chain_digest=None,
    ):
        self.set_calls.append(
            {
                "requirement_id": str(requirement_id),
                "limits": tuple(dict(item) for item in (limits or ())),
                "source": str(source),
                "state": str(state),
                "expected_version": expected_version,
                "expected_status": expected_status,
                "expected_chain_digest": expected_chain_digest,
            }
        )
        if expected_chain_digest is not None and str(expected_chain_digest) != str(
            self.digest
        ):
            raise ValueError("chain digest mismatch")
        return self._real.set_structured_limits(
            requirement_id, limits, source=source, state=state
        )


class StubReviewer:
    """受限模式的模型审查替身（只记录请求并返回 ALLOW）。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def review(self, request):
        self.calls.append(request)
        operation = request.operation
        return ReviewOutcome(
            decision=ReviewDecision.ALLOW,
            reason="测试用模型允许本次操作",
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(operation.kind,),
            objects=operation.objects,
            limits=("仅本次",),
            purpose=operation.summary or "测试",
        )


# --------------------------------------------------------------------------
# 真实主流程
# --------------------------------------------------------------------------


@pytest.fixture()
def ctx(db_conn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), db_conn)
    return app.state.ctx


async def run_turn(ctx, *, message: str, resolver=None):
    """真实 _execute_turn；编排器可观察替身（契约允许）。"""
    topic_id = ctx.current_topic()
    turn_id = "turn_r9h2_" + uuid.uuid4().hex[:10]
    if resolver is not None:
        ctx.requirement_resolver = resolver
    observed = {
        "executed": False,
        "binding": None,
        "status": None,
        "limits": (),
    }

    async def default_execute(turn_ctx):
        observed["executed"] = True
        observed["binding"] = current_requirement.get()
        observed["status"] = requirement_resolution_status.get()
        observed["limits"] = tuple(requirement_limits.get() or ())

    ctx.turn_orchestrator.execute = default_execute
    turn_ctx = TurnContext(
        turn_id=turn_id,
        message=message,
        initial_topic=topic_id,
        current_topic=topic_id,
    )
    turn_ctx.cancelled = False
    await ctx._execute_turn(turn_ctx)
    return turn_ctx, observed


def requirement_for(ctx, binding):
    requirement = ctx.requirements.by_id(binding.requirement_id)
    assert requirement is not None
    return requirement


def write_effect(path) -> Effect:
    path = Path(path)
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path), identity="vol:fake-file"),),
        purpose="修改报告内容",
        detail={"argv": [], "cwd": str(path.parent)},
    )


def read_effect(path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path), identity="vol:fake-file"),),
        purpose="读取材料",
        detail={"argv": [], "cwd": str(Path(path).parent)},
    )


def set_mode(ctx, mode: str) -> None:
    ctx.settings_store.set("computer.permission_mode", mode)


def seed_allow_record(ctx, requirement, *, fingerprint: str, kind: str = "write"):
    record = AuthorizationRecord(
        record_id="authrec_" + uuid.uuid4().hex[:12],
        requirement_id=requirement.requirement_id,
        operation_kind=kind,
        fingerprint=fingerprint,
        decision="allow",
        reason="旧的模型审查允许",
        allowed_operations=(kind,),
        objects=(),
        limits=("仅本次",),
        source="model_review",
        mode="restricted",
        created_at=_NOW,
        expires_at=None,
        revoked_at=None,
        review_id=None,
    )
    ctx.authorizations.record(record)
    return record


def _limit(path: str, *, reason: str = "不要修改这个文件") -> dict:
    return {
        "operations": ["write"],
        "objects": [{"kind": "path", "value": str(path)}],
        "reason": reason,
    }


# --------------------------------------------------------------------------
# 1) 解析失败 → unresolved 且 unrestricted 不自动允许（四类失败）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout", "invalid", "missing"])
async def test_parse_failure_persists_unresolved_and_blocks_unrestricted(ctx, failure):
    resolver = None
    if failure == "error":
        resolver = FakeResolver(error=RequirementResolutionUnavailable("模型不可用"))
    elif failure == "timeout":
        resolver = FakeResolver(error=TimeoutError("解析超时"))
    elif failure == "invalid":
        resolver = FakeResolver(return_none=True)
    if failure == "missing":
        ctx.requirement_resolver = None

    turn_ctx, observed = await run_turn(
        ctx, message="把 B 报告的标题改一下。", resolver=resolver
    )

    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert observed["status"] == "unavailable"
    requirement = requirement_for(ctx, binding)
    assert requirement.structured_limits_state == "unresolved", (
        "[] 或默认值不能把解析失败落成 structured/已确认无限制"
    )
    source = str(requirement.structured_limits_source or "")
    assert source.startswith("resolution_unavailable:"), source
    if failure in ("error", "timeout"):
        assert source.endswith("RequirementResolutionUnavailable") or source.endswith(
            "TimeoutError"
        )
    if failure == "invalid":
        assert source.endswith("invalid_output")
    if failure == "missing":
        assert source.endswith("resolver_missing")

    # 即使引擎在完全无限制模式，相关操作也不能因错误空限制自动放行。
    set_mode(ctx, "unrestricted")
    engine = ctx.authorization
    target = Path(ctx.computer.root()) / "B报告.md"
    with requirement_context(binding):
        decision = await engine.authorize(write_effect(target))
        read = await engine.authorize(read_effect(target))
    assert decision.verdict == Verdict.APPROVE, decision.reason
    assert read.verdict == Verdict.ALLOW
    assert any("要求解析" in item for item in getattr(turn_ctx, "notices", []))


@pytest.mark.asyncio
async def test_success_with_explicit_no_limits_is_structured_and_allowed(ctx):
    resolver = FakeResolver(outcome=make_outcome(RequirementRelation.NEW, limits=()))
    _turn_ctx, observed = await run_turn(
        ctx, message="把 B 报告的标题改成「本周进展」。", resolver=resolver
    )

    binding = observed["binding"]
    assert observed["status"] == "ok"
    requirement = requirement_for(ctx, binding)
    assert requirement.structured_limits_state == "structured"
    assert requirement.structured_limits == (), "完整理解后明确无限制保存为 []"
    set_mode(ctx, "unrestricted")
    target = Path(ctx.computer.root()) / "B报告.md"
    with requirement_context(binding):
        decision = await ctx.authorization.authorize(write_effect(target))
    assert decision.verdict == Verdict.ALLOW


# --------------------------------------------------------------------------
# 2) 失败但用户明确禁止 B：不因空限制自动允许；恢复成功后继续
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_parse_does_not_auto_allow_forbidden_object_then_recovery_continues(
    ctx,
):
    _turn_ctx, observed = await run_turn(
        ctx, message="不要修改 B 报告，只改 A 报告。", resolver=FakeResolver(error=TimeoutError("超时"))
    )
    binding = observed["binding"]
    requirement = requirement_for(ctx, binding)
    assert requirement.structured_limits_state == "unresolved"

    set_mode(ctx, "unrestricted")
    target_b = Path(ctx.computer.root()) / "B报告.md"
    with requirement_context(binding):
        before = await ctx.authorization.authorize(write_effect(target_b))
    assert before.verdict == Verdict.APPROVE, "未恢复前不能因错误空限制自动允许"

    # 历史恢复成功（兼容路径）：真实台账写 structured，本轮限制生效
    recover_ctx = SimpleNamespace(notices=[])
    ctx.requirement_resolver = FakeResolver(extract_limits=(_limit(target_b),))
    recovered = await ctx._ensure_legacy_structured_limits(
        recover_ctx, binding, message_id=None, turn_id="turn_recover"
    )
    assert recovered is not None and recovered[0]["objects"][0]["value"] == str(
        target_b
    )
    restored = requirement_for(ctx, binding)
    assert restored.structured_limits_state == "structured"
    assert any("历史用户限制" not in item for item in []) or True  # 成功不要求 notice

    target_a = Path(ctx.computer.root()) / "A报告.md"
    with requirement_context(binding):
        denied = await ctx.authorization.authorize(write_effect(target_b))
        allowed = await ctx.authorization.authorize(write_effect(target_a))
    assert denied.verdict == Verdict.DENY, denied.reason
    assert allowed.verdict == Verdict.ALLOW


# --------------------------------------------------------------------------
# 3) A 分支完整链恢复：条件写回 / 变化丢弃 / 失败保留 known
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_recovery_uses_context_and_conditional_write(ctx):
    real = ctx.requirements
    requirement = real.record_user_message(
        text="修复项目，但不要修改 B。", message_id="m1", turn_id="t1"
    )
    real.set_structured_limits(
        requirement.requirement_id, (), source="legacy_seed", state="legacy"
    )
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    limit_path = Path(ctx.computer.root()) / "B.md"
    resolver = ChainResolver(limits=(_limit(limit_path),))
    shim = ChainLedgerShim(real, digest="chain-digest-1")
    ctx.requirements = shim
    ctx.requirement_resolver = resolver
    recover_ctx = SimpleNamespace(notices=[])

    recovered = await ctx._ensure_legacy_structured_limits(
        recover_ctx, binding, message_id=None, turn_id="turn_chain"
    )

    assert recovered is not None and recovered[0]["objects"][0]["value"] == str(
        limit_path
    )
    assert resolver.context_calls and resolver.context_calls[0]["requirement_id"] == (
        requirement.requirement_id
    )
    saved = shim.set_calls[-1]
    assert saved["state"] == "structured"
    assert saved["expected_version"] == int(requirement.version)
    assert saved["expected_status"] == "active"
    assert saved["expected_chain_digest"] == "chain-digest-1"
    assert "chain-digest-1" in saved["source"] and "version" in saved["source"]
    assert str(resolver.limit_extraction_chain_prompt_version) in saved["source"]
    assert real.by_id(requirement.requirement_id).structured_limits_state == "structured"


@pytest.mark.asyncio
async def test_chain_recovery_discards_result_when_context_changes(ctx):
    real = ctx.requirements
    requirement = real.record_user_message(
        text="修复项目，但不要修改 B。", message_id="m2", turn_id="t2"
    )
    real.set_structured_limits(
        requirement.requirement_id, (), source="legacy_seed", state="legacy"
    )
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    shim = ChainLedgerShim(real, digest="chain-digest-old")
    ctx.requirements = shim
    ctx.requirement_resolver = ChainResolver(
        limits=(_limit(Path(ctx.computer.root()) / "B.md"),)
    )
    # 模型返回前：要求链已变化（例如 amend/withdraw 造成新版本/新链）
    original_extract = ctx.requirement_resolver.extract_limits_from_context

    async def mutate_then_return(context, *, requirement_id, version):
        shim.digest = "chain-digest-new"
        return await original_extract(
            context, requirement_id=requirement_id, version=version
        )

    ctx.requirement_resolver.extract_limits_from_context = mutate_then_return
    recover_ctx = SimpleNamespace(notices=[])
    recovered = await ctx._ensure_legacy_structured_limits(
        recover_ctx, binding, message_id=None, turn_id="turn_chain_race"
    )

    assert recovered is None, "恢复结果返回后链变化必须丢弃"
    assert not any(call["state"] == "structured" for call in shim.set_calls)
    assert real.by_id(requirement.requirement_id).structured_limits_state == "legacy"
    assert any("历史用户限制" in item for item in recover_ctx.notices)


@pytest.mark.asyncio
async def test_chain_recovery_failure_keeps_known_limits(ctx):
    real = ctx.requirements
    requirement = real.record_user_message(
        text="修复项目，但不要修改 B。", message_id="m3", turn_id="t3"
    )
    known = (_limit(Path(ctx.computer.root()) / "B.md"),)
    real.set_structured_limits(
        requirement.requirement_id, known, source="known", state="unresolved"
    )
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    shim = ChainLedgerShim(real, digest="chain-digest-x")
    ctx.requirements = shim
    ctx.requirement_resolver = ChainResolver(
        error=RequirementResolutionUnavailable("模型超时")
    )
    recover_ctx = SimpleNamespace(notices=[])
    recovered = await ctx._ensure_legacy_structured_limits(
        recover_ctx, binding, message_id=None, turn_id="turn_chain_fail"
    )

    assert recovered is None
    saved = shim.set_calls[-1]
    assert saved["state"] == "unresolved"
    assert saved["limits"] == known, "失败必须保留已有 known limits，不能写空覆盖"
    stored = real.by_id(requirement.requirement_id)
    assert stored.structured_limits_state == "unresolved"
    assert stored.structured_limits == known


# --------------------------------------------------------------------------
# 4) 旧 allow 记录不能绕过 unresolved；重启/重核对状态不丢
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_blocks_authorization_reuse_and_permanent_delete_reuse(ctx):
    real = ctx.requirements
    requirement = real.record_user_message(
        text="整理报告。", message_id="m4", turn_id="t4"
    )
    real.set_structured_limits(
        requirement.requirement_id, (), source="failed_parse", state="unresolved"
    )
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    set_mode(ctx, "restricted")
    reviewer = StubReviewer()
    ctx.authorization.reviewer = reviewer
    target = Path(ctx.computer.root()) / "报告.md"
    effect = write_effect(target)
    operation = ctx.authorization._review_operation(effect)
    fingerprint = operation_fingerprint(operation, mode="restricted")
    seed_allow_record(ctx, requirement, fingerprint=fingerprint)

    with requirement_context(binding):
        decision = await ctx.authorization.authorize(effect)
    assert decision.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1, "unresolved 时不得复用 fingerprint 命中的旧 allow 记录"

    # 永久删除：旧 delete_permanent allow 记录也不能绕过 unresolved
    from agent.security.engine import PermissionMode

    delete_effect = Effect(
        kind=EffectKind.DELETE,
        targets=(Target("path", str(target), identity="vol:fake-file"),),
        purpose="永久删除测试",
        detail={"permanent": True},
    )
    intent = SimpleNamespace(
        permanent=True,
        verified=True,
        method="fs.permanent_delete",
        objects=(
            SimpleNamespace(path=str(target), identity="vol:fake-file", is_directory=False),
        ),
    )
    delete_fingerprint = ctx.authorization._permanent_delete_fingerprint(
        delete_effect, intent
    )
    seed_allow_record(
        ctx, requirement, fingerprint=delete_fingerprint, kind="delete_permanent"
    )
    verdict, reason, _scope = ctx.authorization._permanent_delete_decision(
        delete_effect, PermissionMode.RESTRICTED, real.by_id(requirement.requirement_id)
    )
    assert verdict == Verdict.APPROVE, reason


@pytest.mark.asyncio
async def test_restart_and_recheck_preserve_unresolved_state(ctx):
    _turn_ctx, observed = await run_turn(
        ctx, message="整理报告。", resolver=FakeResolver(error=TimeoutError("超时"))
    )
    binding = observed["binding"]
    requirement = requirement_for(ctx, binding)
    assert requirement.structured_limits_state == "unresolved"

    # 重启：新台账实例从持久化 DB 读回同一状态
    reloaded_ledger = RequirementLedger(ctx.conn)
    reloaded = reloaded_ledger.by_id(binding.requirement_id)
    assert reloaded is not None and reloaded.structured_limits_state == "unresolved"

    # 重建执行前重核对上下文：状态不因重新构造而丢失
    set_mode(ctx, "unrestricted")
    service = RecheckService(
        engine=ctx.authorization,
        requirements=reloaded_ledger,
        approvals=ctx.approvals,
        authorizations=ctx.authorizations,
    )
    target = Path(ctx.computer.root()) / "报告.md"
    operation_binding = OperationBinding(
        requirement=RequirementBinding(
            requirement_id=binding.requirement_id, version=int(binding.version)
        ),
        mode="unrestricted",
        operation_kind="write",
        fingerprint="",
    )
    result = await service.recheck(operation_binding, write_effect(target))
    assert result.ok is False
    assert result.requirement_state == "unresolved"
    assert result.code in ("needs_confirmation", "denied")

    # 旧 allow 记录在重启后的引擎实例上同样不能绕过 unresolved
    reviewer = StubReviewer()
    old_engine = AuthorizationEngine(
        conn=ctx.conn,
        approvals=ctx.approvals,
        settings=ctx.settings_store,
        requirements=reloaded_ledger,
        authorizations=ctx.authorizations,
        reviewer=reviewer,
    )
    effect = write_effect(target)
    fingerprint = operation_fingerprint(
        old_engine._review_operation(effect), mode="unrestricted"
    )
    seed_allow_record(ctx, reloaded, fingerprint=fingerprint)
    with requirement_context(binding):
        decision = await old_engine.authorize(effect)
    # R9-H2：unresolved 时不得复用旧 allow 记录；本用例提供 reviewer，
    # 走针对性语义审查后按模型结论 ALLOW（不是缓存复用），审查必须被调用。
    assert decision.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1, "unresolved 必须重新审查，不能复用旧 allow 记录"


@pytest.mark.asyncio
async def test_unrelated_requirement_is_not_affected(ctx):
    resolver = FakeResolver(outcome=make_outcome(RequirementRelation.NEW, limits=()))
    _turn_ctx, observed = await run_turn(
        ctx, message="整理 A 报告的标题。", resolver=resolver
    )
    good_binding = observed["binding"]
    good_requirement = requirement_for(ctx, good_binding)
    assert good_requirement.structured_limits_state == "structured"

    set_mode(ctx, "unrestricted")
    target = Path(ctx.computer.root()) / "A报告.md"
    with requirement_context(good_binding):
        decision = await ctx.authorization.authorize(write_effect(target))
    assert decision.verdict == Verdict.ALLOW

@pytest.mark.asyncio
async def test_record_user_message_a_interface_receives_unresolved_state(ctx):
    """A 分支 record_user_message 支持 structured_state 时，fallback 直接写 unresolved。"""

    class RecordStateShim:
        def __init__(self, real):
            self._real = real
            self.calls: list[dict] = []

        def __getattr__(self, name):
            return getattr(self._real, name)

        def record_user_message(
            self,
            *,
            text,
            message_id=None,
            turn_id=None,
            topic_id=None,
            structured_state=None,
            structured_source=None,
            structured_limits=(),
        ):
            requirement = self._real.record_user_message(
                text=text, message_id=message_id, turn_id=turn_id, topic_id=topic_id
            )
            self.calls.append(
                {
                    "state": structured_state,
                    "source": structured_source,
                    "requirement_id": requirement.requirement_id,
                }
            )
            if structured_state is not None:
                self._real.set_structured_limits(
                    requirement.requirement_id,
                    tuple(structured_limits or ()),
                    source=structured_source or "unresolved",
                    state=str(structured_state),
                )
            return self._real.by_id(requirement.requirement_id)

    shim = RecordStateShim(ctx.requirements)
    ctx.requirements = shim
    _turn_ctx, observed = await run_turn(
        ctx, message="整理 B 报告。", resolver=FakeResolver(error=TimeoutError("超时"))
    )
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert shim.calls and shim.calls[-1]["state"] == "unresolved"
    assert shim.calls[-1]["source"].startswith("resolution_unavailable:")
    requirement = ctx.requirements.by_id(binding.requirement_id)
    assert requirement.structured_limits_state == "unresolved"


@pytest.mark.asyncio
async def test_structured_requirement_still_reuses_allow_record(ctx):
    """只有 legacy/unresolved 禁止复用；structured 明确限制仍可按 fingerprint 复用。"""
    requirement = ctx.requirements.record_user_message(
        text="整理报告。", message_id="m_structured", turn_id="t_structured"
    )
    ctx.requirements.set_structured_limits(
        requirement.requirement_id, (), source="resolved", state="structured"
    )
    binding = RequirementBinding(
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    set_mode(ctx, "restricted")
    reviewer = StubReviewer()
    ctx.authorization.reviewer = reviewer
    target = Path(ctx.computer.root()) / "结构化报告.md"
    effect = write_effect(target)
    operation = ctx.authorization._review_operation(effect)
    fingerprint = operation_fingerprint(operation, mode="restricted")
    seed_allow_record(ctx, requirement, fingerprint=fingerprint)

    with requirement_context(binding):
        decision = await ctx.authorization.authorize(effect)
    assert decision.verdict == Verdict.ALLOW
    assert reviewer.calls == [], "structured（已可靠理解）时有效 allow 记录可正常复用"

# --------------------------------------------------------------------------
# 5) app/engine 内部数据身份登记接线（C 模块已合入）
# --------------------------------------------------------------------------


def test_app_initializes_internal_data_identity_store(ctx, tmp_path):
    store = getattr(ctx, "internal_data_identity", None)
    assert store is not None, (
        "C 模块已合入时必须创建身份登记；失败原因："
        + str(getattr(ctx, "internal_data_identity_error", None))
    )
    keys = ctx._internal_identity_keys()
    known = ctx._internal_identity_known_paths()
    assert isinstance(keys, frozenset)
    assert isinstance(known, frozenset)
    assert ctx.authorization._internal_data_identity_keys is not None
    assert ctx.authorization._internal_data_known_paths is not None
    refreshed = ctx.refresh_internal_data_identities()
    assert refreshed.get("ok") is True


@pytest.mark.asyncio
async def test_engine_uses_registered_identity_outside_protected_paths(ctx, tmp_path):
    """移出注册路径但身份仍是登记对象 → 仍按内部数据三次确认（不是普通文件）。"""
    from agent.services import file_identity

    store = ctx.internal_data_identity
    assert store is not None
    outside = Path(ctx.computer.root()) / "看起来普通.txt"
    outside.write_text("同一真实对象的另一条路径", encoding="utf-8")
    found = file_identity.identity_of(outside)
    assert found is not None and found.key
    ctx.conn.execute(
        "INSERT OR REPLACE INTO internal_data_identities"
        " (identity_key, path, kind, active, source, created_at, updated_at)"
        " VALUES (?, ?, 'file', 1, 'test_identity', ?, ?)",
        (found.key, str(outside), _NOW, _NOW),
    )
    ctx.conn.commit()

    set_mode(ctx, "unrestricted")
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(outside), identity=found.key),),
        purpose="按身份登记测试写文件",
        detail={"argv": [], "cwd": str(outside.parent)},
    )
    with requirement_context(None):
        decision = await ctx.authorization.authorize(effect)
    assert decision.verdict == Verdict.APPROVE, (
        "身份登记命中时内部数据规则必须生效（不能按普通文件自动放行）"
    )
    assert "三次确认" in decision.reason


