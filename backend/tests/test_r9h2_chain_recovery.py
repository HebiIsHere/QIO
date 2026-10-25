# -*- coding: utf-8 -*-
"""R9-H2 A：历史要求修正链的结构化限制恢复（契约 §3）。

测试原则（契约 §3.4）：
* 关键场景走真实 _execute_turn()/_prepare_requirement_binding：真实 InstructionRegistry、
  真实 RequirementLedger、真实 apply_resolution 与版本链；
* 模型调用用 FakeResolver / model_call 替身，不联网、不用真实 Key；
* 恢复消费逻辑用 tests 内最小消费者（B 的 app 接线尚未在本分支，E2E 由 B 验证），
  但上下文组装、条件写入、链恢复全部调用 A 的真实实现；不使用预先提供的完整限制
  绕过 requirements.recovery_context；
* 竞态用 asyncio.Event 闸门，不用随机 sleep。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.prompts import LIMIT_EXTRACTION_CHAIN_SCHEMA_VERSION
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.requirements import (
    RequirementBinding,
    RequirementRecoveryContext,
    RequirementVersionMismatch,
)
from agent.security.resolution import (
    ModelRequirementResolver,
    RequirementRelation,
    RequirementResolutionUnavailable,
    ResolutionOutcome,
    build_limit_extraction_context_payload,
    parse_extracted_context_limits,
    render_limit_extraction_context_messages,
)

_NOW = "2026-10-23T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 替身与夹具
# ---------------------------------------------------------------------------


class FakeChainResolver:
    """可控要求解析 + 链恢复替身；旧单句 extract_limits 故意不可用。

    旧 app 分支（R9-H）在 legacy/unresolved 时会调用 extract_limits(text)；
    这里让它抛 Unavailable，模拟"单句恢复不适用于历史链"的事实，同时让
    测试中的恢复显式走 extract_limits_from_context（A 的新接口）。
    """

    model = "fake-r9h2-model"
    prompt_version = "fake-r9h2-resolution-v1"
    limit_extraction_prompt_version = "fake-r9h2-limit-extraction-v1"
    limit_extraction_chain_prompt_version = LIMIT_EXTRACTION_CHAIN_SCHEMA_VERSION

    def __init__(
        self,
        *,
        outcome=None,
        limits=(),
        error=None,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.outcome = outcome
        self.limits = tuple(dict(item) for item in limits)
        self.error = error
        self.started = started
        self.release = release
        self.resolve_calls: list[dict] = []
        self.context_calls: list[dict] = []

    async def resolve(self, **kwargs):
        self.resolve_calls.append(dict(kwargs))
        return self.outcome

    async def extract_limits(self, text, *, requirement_id=None, version=None):
        raise RequirementResolutionUnavailable("旧单句恢复接口不用于历史链")

    async def extract_limits_from_context(self, context, *, requirement_id, version):
        self.context_calls.append(
            {"context": context, "requirement_id": requirement_id, "version": version}
        )
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.limits


class StaleRecovery(RuntimeError):
    """恢复结果在模型等待期间已过期（B 应按契约丢弃，不写新版本）。"""


@pytest.fixture()
def ctx(db_conn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), db_conn)
    return app.state.ctx


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
        prompt_version="fake-r9h2-resolution-v1",
    )


def seed_legacy(ctx, text: str, *, message_id: str = "seed_msg"):
    return ctx.requirements.record_user_message(
        text=text,
        message_id=message_id,
        turn_id="seed_turn",
        topic_id="seed_topic",
        structured_state=requirement_limits.STATE_LEGACY,
    )


def requirement_rows(ctx):
    return ctx.conn.execute(
        "SELECT * FROM user_requirements ORDER BY created_at, rowid"
    ).fetchall()


async def run_turn(ctx, *, message: str, resolver: FakeChainResolver):
    """真实 _execute_turn：真实台账/apply_resolution；编排器用可观察替身。"""
    topic_id = ctx.current_topic()
    turn_id = "turn_chain_" + uuid.uuid4().hex[:10]
    ctx.requirement_resolver = resolver
    observed = {"binding": None, "status": None, "executed": False}

    async def execute(turn_ctx):
        from agent.security.context import current_requirement

        observed["executed"] = True
        observed["binding"] = current_requirement.get()
        from agent.security.context import requirement_resolution_status

        observed["status"] = requirement_resolution_status.get()

    ctx.turn_orchestrator.execute = execute
    # A 文件验证 A 的链上下文/条件写回接口；B 的生产 app 会自动链恢复，其真实主流程
    # 由 Lead 的 test_r9h2_real_flow 覆盖。这里临时关闭自动恢复，保留显式 recover() 测试。
    async def _skip_auto_recovery(*args, **kwargs):
        return None

    ctx._ensure_legacy_structured_limits = _skip_auto_recovery  # type: ignore[method-assign]
    turn_ctx = TurnContext(
        turn_id=turn_id,
        message=message,
        initial_topic=topic_id,
        current_topic=topic_id,
    )
    await ctx._execute_turn(turn_ctx)
    return turn_ctx, observed


async def recover(
    ctx,
    resolver: FakeChainResolver,
    requirement_id: str,
    *,
    state: str = requirement_limits.STATE_STRUCTURED,
):
    """最小消费者：按契约 §3.3 调用 A 的真实接口（不是生产 app 代码）。"""
    requirement = ctx.requirements.by_id(requirement_id)
    assert requirement is not None
    context = ctx.requirements.recovery_context(requirement_id)
    if not context.complete:
        raise AssertionError("恢复上下文不完整：" + context.reason)
    limits = await resolver.extract_limits_from_context(
        context,
        requirement_id=requirement.requirement_id,
        version=int(requirement.version),
    )
    fresh = ctx.requirements.by_id(requirement_id)
    fresh_context = ctx.requirements.recovery_context(requirement_id)
    if (
        fresh is None
        or fresh_version_mismatch(fresh.version, requirement.version)
        or fresh.status != "active"
        or fresh_context.chain_digest != context.chain_digest
    ):
        raise StaleRecovery("模型返回时要求链已变化，丢弃旧恢复结果")
    source = (
        resolver.limit_extraction_chain_prompt_version
        + ";chain="
        + context.chain_digest
        + ";version="
        + str(requirement.version)
    )
    saved = ctx.requirements.set_structured_limits(
        requirement_id,
        limits,
        source=source,
        state=state,
        expected_version=int(requirement.version),
        expected_status="active",
        expected_chain_digest=context.chain_digest,
    )
    return limits, saved, context


def fresh_version_mismatch(a: int, b: int) -> bool:
    return int(a) != int(b)


async def authorize(ctx, binding: RequirementBinding, path, *, kind=EffectKind.WRITE):
    """在 unrestricted 下用真实引擎核对结构化限制（限制只拒绝，不产生许可）。"""
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    effect = Effect(
        kind=kind,
        targets=(Target("path", str(path)),),
        purpose="R9-H2 链恢复限制验证",
    )
    with requirement_context(binding):
        return await ctx.authorization.authorize(effect)


# ---------------------------------------------------------------------------
# 1) legacy 原要求 + 只补充标题：恢复输入必须是完整链，B 仍被禁止
# ---------------------------------------------------------------------------


async def test_legacy_root_plus_title_only_keeps_b_restriction(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    root = seed_legacy(ctx, "修复项目，但不要修改 %s。" % b_path)
    assert root.structured_limits_state == requirement_limits.STATE_LEGACY

    resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=root,
            target_version=int(root.version),
            corrections=("把标题改成 X",),
        )
    )
    message = "先把标题改成 X。"
    turn_ctx, observed = await run_turn(ctx, message=message, resolver=resolver)
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    child = ctx.requirements.by_id(binding.requirement_id)
    assert child is not None
    assert int(child.version) == 2
    # 旧 app 分支会把 legacy 子要求标 unresolved；核心断言：未解决、不得当无限制。
    assert child.structured_limits_state in (
        requirement_limits.STATE_LEGACY,
        requirement_limits.STATE_UNRESOLVED,
    )

    context = ctx.requirements.recovery_context(child.requirement_id)
    assert isinstance(context, RequirementRecoveryContext)
    assert context.complete is True, context.reason
    assert [node.requirement_id for node in context.chain] == [
        root.requirement_id,
        child.requirement_id,
    ]
    assert [node.text for node in context.chain] == [root.text, message]
    assert [node.version for node in context.chain] == [1, 2]
    assert context.chain[1].parent_id == root.requirement_id
    assert len(context.chain_digest) == 64

    resolver.limits = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": str(b_path)}],
            "reason": "用户要求不要修改 B",
        },
    )
    limits, saved, used_context = await recover(ctx, resolver, child.requirement_id)
    assert saved.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert saved.structured_limits == limits
    assert used_context.chain_digest == context.chain_digest
    # 模型确实拿到了完整链（不是只喂补充句）
    assert len(resolver.context_calls) == 1
    passed = resolver.context_calls[0]["context"]
    assert [node.text for node in passed.chain] == [root.text, message]

    denied = await authorize(ctx, binding, b_path)
    assert denied.verdict == Verdict.DENY
    allowed = await authorize(ctx, binding, a_path)
    assert allowed.verdict == Verdict.ALLOW


# ---------------------------------------------------------------------------
# 2) 多次补充后原限制仍有效
# ---------------------------------------------------------------------------


async def test_multiple_amendments_keep_original_restriction(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    root = seed_legacy(ctx, "修复项目，但不要修改 %s。" % b_path)

    resolver1 = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=root,
            target_version=int(root.version),
            corrections=("把标题改成 X",),
        )
    )
    _, observed1 = await run_turn(
        ctx, message="先把标题改成 X。", resolver=resolver1
    )
    binding1 = observed1["binding"]
    assert isinstance(binding1, RequirementBinding)
    first_child = ctx.requirements.by_id(binding1.requirement_id)
    assert first_child is not None and int(first_child.version) == 2

    resolver2 = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=first_child,
            target_version=int(first_child.version),
            corrections=("再加一句结论",),
        )
    )
    _, observed2 = await run_turn(
        ctx, message="再加一句结论。", resolver=resolver2
    )
    binding2 = observed2["binding"]
    assert isinstance(binding2, RequirementBinding)
    latest = ctx.requirements.by_id(binding2.requirement_id)
    assert latest is not None and int(latest.version) == 3
    assert latest.structured_limits_state in (
        requirement_limits.STATE_LEGACY,
        requirement_limits.STATE_UNRESOLVED,
    )

    context = ctx.requirements.recovery_context(latest.requirement_id)
    assert context.complete is True, context.reason
    assert [node.text for node in context.chain] == [
        root.text,
        "先把标题改成 X。",
        "再加一句结论。",
    ]
    assert [node.version for node in context.chain] == [1, 2, 3]

    resolver2.limits = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": str(b_path)}],
            "reason": "原要求明确不要修改 B",
        },
    )
    limits, saved, _ = await recover(ctx, resolver2, latest.requirement_id)
    assert saved.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert saved.structured_limits == limits

    denied = await authorize(ctx, binding2, b_path)
    assert denied.verdict == Verdict.DENY


# ---------------------------------------------------------------------------
# 3) 明确解除一个限制：其他限制保留；解除全部：允许
# ---------------------------------------------------------------------------


async def test_explicit_remove_one_limit_keeps_other(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    data_dir = tmp_path / "data"
    root = seed_legacy(
        ctx,
        "不要修改 %s；也不要删除 %s 目录。" % (b_path, data_dir),
    )
    resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=root,
            target_version=int(root.version),
            corrections=("B 的限制不用了，其他照旧",),
        )
    )
    _, observed = await run_turn(
        ctx, message="B 的限制不用了，其他照旧。", resolver=resolver
    )
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    # 模型语义：只保留"不要删除 data 目录"
    resolver.limits = (
        {
            "operations": ["delete"],
            "objects": [{"kind": "directory", "value": str(data_dir)}],
            "reason": "用户只解除了 B 的限制",
        },
    )
    limits, saved, _ = await recover(ctx, resolver, binding.requirement_id)
    assert saved.structured_limits == limits
    assert len(saved.structured_limits or ()) == 1

    allowed = await authorize(ctx, binding, b_path)
    assert allowed.verdict == Verdict.ALLOW
    denied = await authorize(ctx, binding, data_dir / "x.txt", kind=EffectKind.DELETE)
    assert denied.verdict == Verdict.DENY


async def test_explicit_remove_all_limits_marks_structured_empty(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    root = seed_legacy(ctx, "不要修改 %s。" % b_path)
    resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=root,
            target_version=int(root.version),
            corrections=("之前的限制全部取消",),
        )
    )
    _, observed = await run_turn(
        ctx, message="之前的限制全部取消。", resolver=resolver
    )
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    resolver.limits = ()
    limits, saved, _ = await recover(ctx, resolver, binding.requirement_id)
    assert limits == ()
    assert saved.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert saved.structured_limits == ()

    allowed = await authorize(ctx, binding, b_path)
    assert allowed.verdict == Verdict.ALLOW


# ---------------------------------------------------------------------------
# 4) 不相关要求不得串入链
# ---------------------------------------------------------------------------


async def test_unrelated_requirement_not_in_chain(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    root = seed_legacy(ctx, "修复项目，但不要修改 %s。" % b_path)
    unrelated = seed_legacy(ctx, "整理下载目录里的旧截图。")
    resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=root,
            target_version=int(root.version),
            corrections=("把标题改成 X",),
        )
    )
    _, observed = await run_turn(
        ctx, message="把标题改成 X。", resolver=resolver
    )
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)

    context = ctx.requirements.recovery_context(binding.requirement_id)
    assert context.complete is True
    assert [node.requirement_id for node in context.chain] == [
        root.requirement_id,
        binding.requirement_id,
    ]
    assert unrelated.requirement_id not in [
        node.requirement_id for node in context.chain
    ]
    payload = build_limit_extraction_context_payload(
        context, requirement_id=binding.requirement_id, version=binding.version
    )
    texts = [node["text"] for node in payload["chain"]]
    assert texts == [root.text, "把标题改成 X。"]
    assert unrelated.text not in texts


# ---------------------------------------------------------------------------
# 5) 缺父/版本不连续/循环：complete=False，不标 structured
# ---------------------------------------------------------------------------


async def test_missing_parent_marks_incomplete_and_skips_model(ctx, monkeypatch):
    root = seed_legacy(ctx, "修复项目，但不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    # 真实台账里父行缺失（模拟历史迁移/损坏）：直接改 parent_id 指向不存在的要求
    ctx.conn.execute(
        "UPDATE user_requirements SET parent_id = ? WHERE id = ?",
        ("req_missing_parent", child.requirement_id),
    )
    ctx.conn.commit()
    context = ctx.requirements.recovery_context(child.requirement_id)
    assert context.complete is False
    assert "父要求缺失" in context.reason
    assert [node.requirement_id for node in context.chain] == [child.requirement_id]

    resolver = FakeChainResolver()
    with pytest.raises(AssertionError):
        await recover(ctx, resolver, child.requirement_id)
    assert resolver.context_calls == []  # 不完整上下文不调用模型
    row = ctx.requirements.by_id(child.requirement_id)
    assert row is not None
    assert row.structured_limits_state != requirement_limits.STATE_STRUCTURED

    # 不完整上下文直接调用解析器也必须拒绝（不产生"无限制"结果）
    with pytest.raises(RequirementResolutionUnavailable):
        build_limit_extraction_context_payload(
            context, requirement_id=child.requirement_id, version=child.version
        )


async def test_version_discontinuity_and_cycle_are_incomplete(ctx):
    root = seed_legacy(ctx, "修复项目，但不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    assert ctx.requirements.recovery_context(child.requirement_id).complete is True

    ctx.conn.execute(
        "UPDATE user_requirements SET version = ? WHERE id = ?",
        (9, child.requirement_id),
    )
    ctx.conn.commit()
    broken = ctx.requirements.recovery_context(child.requirement_id)
    assert broken.complete is False
    assert "版本不连续" in broken.reason

    ctx.conn.execute(
        "UPDATE user_requirements SET version = ? WHERE id = ?",
        (2, child.requirement_id),
    )
    ctx.conn.execute(
        "UPDATE user_requirements SET parent_id = ? WHERE id = ?",
        (child.requirement_id, root.requirement_id),
    )
    ctx.conn.commit()
    cycle = ctx.requirements.recovery_context(child.requirement_id)
    assert cycle.complete is False
    assert "循环" in cycle.reason


async def test_non_active_current_is_incomplete(ctx):
    root = seed_legacy(ctx, "修复项目，但不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    ctx.requirements.withdraw(child.requirement_id)
    context = ctx.requirements.recovery_context(child.requirement_id)
    assert context.complete is False
    assert "不是 active" in context.reason
    assert context.status == "withdrawn"


# ---------------------------------------------------------------------------
# 6) 条件写入：版本/链变化时拒绝，且保留已有 known limits/source
# ---------------------------------------------------------------------------


async def test_conditional_write_rejects_stale_version_and_preserves_known_limits(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    root = seed_legacy(ctx, "不要修改 %s。" % b_path)
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    known = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": str(b_path)}],
            "reason": "旧恢复结果",
        },
    )
    child = ctx.requirements.set_structured_limits(
        child.requirement_id,
        known,
        state=requirement_limits.STATE_UNRESOLVED,
        source="prior_recovery",
    )
    context_before = ctx.requirements.recovery_context(child.requirement_id)
    assert context_before.complete is True

    # 真实主流程：再加一次修正 → 新版本
    resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=child,
            target_version=int(child.version),
            corrections=("再改一次标题",),
        )
    )
    _, observed = await run_turn(ctx, message="再改一次标题。", resolver=resolver)
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert int(binding.version) == 3

    # 用旧版本/旧链摘要写回 → 必须拒绝
    with pytest.raises(RequirementVersionMismatch):
        ctx.requirements.set_structured_limits(
            child.requirement_id,
            known,
            state=requirement_limits.STATE_STRUCTURED,
            source="stale_recovery",
            expected_version=int(child.version),
            expected_status="active",
            expected_chain_digest=context_before.chain_digest,
        )
    # 旧行（已被 supersede）与 new 行都不得被旧结果覆盖
    old_row = ctx.requirements.by_id(child.requirement_id)
    assert old_row is not None
    assert old_row.status == "superseded"
    assert old_row.structured_limits == known
    assert old_row.structured_limits_source == "prior_recovery"
    new_row = ctx.requirements.by_id(binding.requirement_id)
    assert new_row is not None
    assert new_row.structured_limits_state in (
        requirement_limits.STATE_LEGACY,
        requirement_limits.STATE_UNRESOLVED,
    )


async def test_conditional_write_recomputes_chain_digest_inside_transaction(ctx):
    root = seed_legacy(ctx, "不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    context = ctx.requirements.recovery_context(child.requirement_id)
    assert context.complete is True

    # 只改祖先文本（版本/状态都不变）→ chain_digest 变化，条件写入必须拒绝
    ctx.conn.execute(
        "UPDATE user_requirements SET text = ? WHERE id = ?",
        ("不要修改 B 和 C。", root.requirement_id),
    )
    ctx.conn.commit()
    with pytest.raises(RequirementVersionMismatch):
        ctx.requirements.set_structured_limits(
            child.requirement_id,
            (),
            state=requirement_limits.STATE_STRUCTURED,
            source="stale_chain",
            expected_version=int(child.version),
            expected_status="active",
            expected_chain_digest=context.chain_digest,
        )
    row = ctx.requirements.by_id(child.requirement_id)
    assert row is not None
    assert row.structured_limits_state in (
        requirement_limits.STATE_LEGACY,
        requirement_limits.STATE_UNRESOLVED,
    )


# ---------------------------------------------------------------------------
# 7) 模型等待期间 amend/withdraw：旧结果不得写新版本
# ---------------------------------------------------------------------------


async def test_amend_during_recovery_discards_stale_result(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    root = seed_legacy(ctx, "不要修改 %s。" % b_path)
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    started = asyncio.Event()
    release = asyncio.Event()
    resolver = FakeChainResolver(
        limits=(
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(b_path)}],
                "reason": "旧链恢复",
            },
        ),
        started=started,
        release=release,
    )
    task = asyncio.create_task(recover(ctx, resolver, child.requirement_id))
    await started.wait()

    # 模型等待期间，用户又补充一句（真实主流程 → 新版本）
    amend_resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=child,
            target_version=int(child.version),
            corrections=("再改一次",),
        )
    )
    _, observed = await run_turn(ctx, message="再改一次。", resolver=amend_resolver)
    new_binding = observed["binding"]
    assert isinstance(new_binding, RequirementBinding)
    assert int(new_binding.version) == 3
    release.set()
    with pytest.raises(StaleRecovery):
        await task

    # 新版本不得被旧结果写成 structured；旧结果也不得写到旧行
    new_row = ctx.requirements.by_id(new_binding.requirement_id)
    assert new_row is not None
    assert new_row.structured_limits_state in (
        requirement_limits.STATE_LEGACY,
        requirement_limits.STATE_UNRESOLVED,
    )
    old_row = ctx.requirements.by_id(child.requirement_id)
    assert old_row is not None
    assert old_row.structured_limits_state in (
        requirement_limits.STATE_LEGACY,
        requirement_limits.STATE_UNRESOLVED,
    )


async def test_withdraw_during_recovery_discards_stale_result(ctx):
    root = seed_legacy(ctx, "不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    started = asyncio.Event()
    release = asyncio.Event()
    resolver = FakeChainResolver(
        limits=(
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": "B"}],
                "reason": "旧链恢复",
            },
        ),
        started=started,
        release=release,
    )
    task = asyncio.create_task(recover(ctx, resolver, child.requirement_id))
    await started.wait()

    withdraw_resolver = FakeChainResolver(
        outcome=make_outcome(
            RequirementRelation.WITHDRAW,
            target=child,
            target_version=int(child.version),
        )
    )
    _, observed = await run_turn(
        ctx, message="这个任务取消。", resolver=withdraw_resolver
    )
    assert observed["binding"] is None  # withdraw 后不绑定
    release.set()
    with pytest.raises(StaleRecovery):
        await task

    withdrawn = ctx.requirements.by_id(child.requirement_id)
    assert withdrawn is not None
    assert withdrawn.status == "withdrawn"
    assert withdrawn.structured_limits_state != requirement_limits.STATE_STRUCTURED


# ---------------------------------------------------------------------------
# 8) 恢复失败保留已知限制与来源（不写空数组）
# ---------------------------------------------------------------------------


async def test_recovery_failure_keeps_known_limits_and_source(ctx):
    root = seed_legacy(ctx, "不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    known = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": "B"}],
            "reason": "之前已知的限制",
        },
    )
    ctx.requirements.set_structured_limits(
        child.requirement_id,
        known,
        state=requirement_limits.STATE_UNRESOLVED,
        source="prior_recovery",
    )
    resolver = FakeChainResolver(error=RequirementResolutionUnavailable("模型超时"))
    with pytest.raises(RequirementResolutionUnavailable):
        await recover(ctx, resolver, child.requirement_id)

    # 契约 §3.3-6：失败时保留已有 known limits，不得用空数组覆盖
    row = ctx.requirements.by_id(child.requirement_id)
    assert row is not None
    ctx.requirements.set_structured_limits(
        child.requirement_id,
        row.structured_limits,
        state=requirement_limits.STATE_UNRESOLVED,
        source="recovery_failed:RequirementResolutionUnavailable",
    )
    failed = ctx.requirements.by_id(child.requirement_id)
    assert failed is not None
    assert failed.structured_limits == known
    assert failed.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert failed.structured_limits_source == (
        "recovery_failed:RequirementResolutionUnavailable"
    )


# ---------------------------------------------------------------------------
# 9) 上下文摘要稳定，链变化后 digest 变化
# ---------------------------------------------------------------------------


async def test_chain_digest_stable_and_changes_after_amend(ctx):
    root = seed_legacy(ctx, "不要修改 B。")
    first = ctx.requirements.recovery_context(root.requirement_id)
    again = ctx.requirements.recovery_context(root.requirement_id)
    assert first.chain_digest == again.chain_digest
    assert len(first.chain_digest) == 64

    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    second = ctx.requirements.recovery_context(child.requirement_id)
    assert second.chain_digest != first.chain_digest
    assert [node.requirement_id for node in second.chain] == [
        root.requirement_id,
        child.requirement_id,
    ]


# ---------------------------------------------------------------------------
# 10) record_user_message 状态参数（默认兼容；生产 fallback 显式 unresolved）
# ---------------------------------------------------------------------------


async def test_record_user_message_structured_parameters(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    default = ctx.requirements.record_user_message(text="默认新要求。")
    assert default.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert default.structured_limits == ()
    assert default.structured_limits_source == "user_message"

    unresolved = ctx.requirements.record_user_message(
        text="解析失败的消息。",
        structured_state=requirement_limits.STATE_UNRESOLVED,
        structured_source="resolution_unavailable:timeout",
    )
    assert unresolved.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert unresolved.structured_limits == ()
    assert unresolved.structured_limits_source == "resolution_unavailable:timeout"

    legacy = ctx.requirements.record_user_message(
        text="历史要求。",
        structured_state=requirement_limits.STATE_LEGACY,
        structured_source=None,
    )
    assert legacy.structured_limits_state == requirement_limits.STATE_LEGACY
    assert legacy.structured_limits is None
    assert legacy.structured_limits_source is None

    with pytest.raises(ValueError):
        ctx.requirements.record_user_message(
            text="非法状态。", structured_state="not-a-state"
        )


# ---------------------------------------------------------------------------
# 11) 解析器：链输入渲染/结构校验/失败路径
# ---------------------------------------------------------------------------


async def test_chain_payload_contains_full_ordered_chain(ctx):
    root = seed_legacy(ctx, "原要求：不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "补充：把标题改成 X。")
    context = ctx.requirements.recovery_context(child.requirement_id)
    payload = build_limit_extraction_context_payload(
        context, requirement_id=child.requirement_id, version=int(child.version)
    )
    assert payload["target_requirement_id"] == child.requirement_id
    assert payload["target_version"] == int(child.version)
    assert payload["chain_digest"] == context.chain_digest
    assert payload["complete"] is True
    assert [item["text"] for item in payload["chain"]] == [root.text, child.text]
    assert [item["version"] for item in payload["chain"]] == [1, 2]
    assert [item["requirement_id"] for item in payload["chain"]] == [
        root.requirement_id,
        child.requirement_id,
    ]
    assert payload["chain"][0]["structured_limits_state"] == (
        requirement_limits.STATE_LEGACY
    )
    messages = render_limit_extraction_context_messages(
        context, requirement_id=child.requirement_id, version=int(child.version)
    )
    assert messages[0].role == "system"
    assert "完整" in messages[0].content
    user_content = messages[1].content
    assert root.text in user_content and child.text in user_content

    # 目标/版本不一致或链不完整 → 直接拒绝，不调用模型
    with pytest.raises(RequirementResolutionUnavailable):
        build_limit_extraction_context_payload(
            context, requirement_id="req_other", version=int(child.version)
        )
    assert parse_extracted_context_limits(json.dumps({"limits": []})) == ()
    with pytest.raises(RequirementResolutionUnavailable):
        parse_extracted_context_limits("not json")
    # R9-H4 §2：read 与 program 对象是规范限制操作/对象，必须被解析接受
    accepted = parse_extracted_context_limits(
        json.dumps(
            {
                "limits": [
                    {"operations": ["read"], "objects": [], "reason": "不要读取"},
                    {
                        "operations": ["execute"],
                        "objects": [{"kind": "program", "value": "python"}],
                        "reason": "不要运行 python",
                    },
                ]
            },
            ensure_ascii=False,
        )
    )
    assert accepted[0]["operations"] == ["read"]
    assert accepted[1]["objects"][0]["kind"] == "program_name"
    with pytest.raises(RequirementResolutionUnavailable):
        # 非规范操作名仍然拒绝
        parse_extracted_context_limits(
            json.dumps(
                {
                    "limits": [
                        {"operations": ["keyword_allow"], "objects": [], "reason": "x"}
                    ]
                }
            )
        )


async def test_extract_limits_from_context_model_call_and_failures(ctx):
    root = seed_legacy(ctx, "不要修改 B。")
    child = ctx.requirements.add_correction(root.requirement_id, "把标题改成 X。")
    context = ctx.requirements.recovery_context(child.requirement_id)
    calls = {"count": 0}

    def model_call(messages):
        calls["count"] += 1
        return json.dumps(
            {
                "limits": [
                    {
                        "operations": ["write"],
                        "objects": [{"kind": "path", "value": "B"}],
                        "reason": "原要求不要修改 B",
                    }
                ]
            },
            ensure_ascii=False,
        )

    resolver = ModelRequirementResolver(
        adapter_factory=None, model_call=model_call, timeout_seconds=2.0
    )
    limits = await resolver.extract_limits_from_context(
        context, requirement_id=child.requirement_id, version=int(child.version)
    )
    assert limits == (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": "B"}],
            "reason": "原要求不要修改 B",
        },
    )
    assert calls["count"] == 1

    bad_resolver = ModelRequirementResolver(
        adapter_factory=None, model_call=lambda messages: "not-json", timeout_seconds=2.0
    )
    with pytest.raises(RequirementResolutionUnavailable):
        await bad_resolver.extract_limits_from_context(
            context, requirement_id=child.requirement_id, version=int(child.version)
        )

    # 不完整上下文：在调用模型之前拒绝
    ctx.conn.execute(
        "UPDATE user_requirements SET parent_id = ? WHERE id = ?",
        ("req_missing", child.requirement_id),
    )
    ctx.conn.commit()
    broken = ctx.requirements.recovery_context(child.requirement_id)
    assert broken.complete is False
    before = calls["count"]
    with pytest.raises(RequirementResolutionUnavailable):
        await resolver.extract_limits_from_context(
            broken, requirement_id=child.requirement_id, version=int(child.version)
        )
    assert calls["count"] == before


@pytest.fixture(autouse=True)
def _r9h13_legacy_structural_limits(monkeypatch):
    """R9-H13 B2 修订：本文件旧用例只比较结构性限制对象（kind/value/operations）。

    H13 新增的“保存时对象事实”字段由专门用例覆盖
    （test_r9h13_a_requirements.py / test_r9h13_b_object_state.py）；这里保留原输入与
    原断言，仅在本文件的保存边界关闭事实采集，不改变任何判定逻辑。
    """
    from agent.security import requirement_limits as _requirement_limits

    monkeypatch.setattr(
        _requirement_limits,
        "_collect_saved_object_facts",
        lambda item: dict(item),
    )