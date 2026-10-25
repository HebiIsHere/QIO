"""R9-H2 真实主流程集成：链恢复自动接线 / 解析失败不全局阻塞 / 内部数据身份。

真实 create_app/AppContext/_execute_turn/RequirementLedger/AuthorizationEngine；
模型调用用 fake resolver/reviewer，不使用真实 Key。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.security import requirement_limits
from agent.security.context import requirement_context
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.resolution import (
    RequirementRelation,
    RequirementResolutionUnavailable,
    ResolutionOutcome,
)
from agent.security.review import ReviewDecision, ReviewOutcome


class FakeResolver:
    model = "fake-r9h2-real-flow"
    prompt_version = "fake-r9h2-resolution-v1"
    limit_extraction_prompt_version = "fake-r9h2-limit-v1"
    limit_extraction_chain_prompt_version = "r9h2-limit-extraction-chain-v1"

    def __init__(self, *, outcome=None, error=None, limits=()):
        self.outcome = outcome
        self.error = error
        self.limits = tuple(dict(item) for item in limits)
        self.resolve_calls = []
        self.context_calls = []

    async def resolve(self, **kwargs):
        self.resolve_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.outcome

    async def extract_limits_from_context(self, context, *, requirement_id, version):
        self.context_calls.append(
            {"context": context, "requirement_id": requirement_id, "version": version}
        )
        return self.limits


class PathReviewer:
    """按模型语义替身：B 文件 DENY，其他 ALLOW（只用于测试，不参与生产判定）。"""

    def __init__(self, forbidden: Path):
        self.forbidden = str(forbidden)
        self.calls = []

    async def review(self, request):
        self.calls.append(request)
        operation = request.operation
        denied = any(
            str(getattr(obj, "value", "")) == self.forbidden for obj in operation.objects
        )
        return ReviewOutcome(
            decision=ReviewDecision.DENY if denied else ReviewDecision.ALLOW,
            reason="测试模型：命中用户明确禁止的对象" if denied else "测试模型：不在禁止范围",
            requirement_id=request.requirement.requirement_id,
            allowed_operations=() if denied else (operation.kind,),
            objects=operation.objects,
            limits=() if denied else ("仅本次",),
            purpose=operation.summary or "测试",
        )


@pytest.fixture()
def ctx(db_conn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), db_conn)
    return app.state.ctx


def _write(path: Path) -> Effect:
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(path)),),
        purpose="写文件",
    )


def _read(path: Path) -> Effect:
    return Effect(
        kind=EffectKind.READ,
        targets=(Target("path", str(path)),),
        purpose="读文件",
    )


async def _run_turn(ctx, *, message: str, resolver) -> dict:
    topic_id = ctx.current_topic()
    ctx.requirement_resolver = resolver
    observed = {"binding": None, "status": None, "limits": ()}

    async def execute(turn_ctx):
        from agent.security.context import current_requirement, requirement_resolution_status

        observed["binding"] = current_requirement.get()
        observed["status"] = requirement_resolution_status.get()

    ctx.turn_orchestrator.execute = execute
    turn_ctx = TurnContext(
        turn_id="turn_r9h2_" + uuid.uuid4().hex[:10],
        message=message,
        initial_topic=topic_id,
        current_topic=topic_id,
    )
    await ctx._execute_turn(turn_ctx)
    return observed


def _seed_legacy(ctx, text: str):
    return ctx.requirements.record_user_message(
        text=text,
        message_id="seed_msg",
        turn_id="seed_turn",
        topic_id="seed_topic",
        structured_state=requirement_limits.STATE_LEGACY,
    )


async def test_legacy_chain_auto_recovery_keeps_restriction_in_real_turn(ctx, tmp_path):
    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    root = _seed_legacy(ctx, "修复项目，但不要修改 %s。" % b_path)
    resolver = FakeResolver(
        outcome=ResolutionOutcome(
            relation=RequirementRelation.AMEND,
            target_requirement_id=root.requirement_id,
            target_version=int(root.version),
            corrections=("把标题改成 X",),
            limits=(),
            removed_limits=(),
            reason="用户只补充标题",
            model="fake-r9h2-real-flow",
            prompt_version="fake-r9h2-resolution-v1",
        ),
        limits=(
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": str(b_path)}],
                "reason": "原要求明确不要修改 B",
            },
        ),
    )
    observed = await _run_turn(ctx, message="先把标题改成 X。", resolver=resolver)
    binding = observed["binding"]
    assert binding is not None
    child = ctx.requirements.by_id(binding.requirement_id)
    assert child is not None and int(child.version) == 2
    assert child.structured_limits_state == requirement_limits.STATE_STRUCTURED
    assert child.structured_limits == resolver.limits
    # 自动恢复确实拿到完整链（不是只喂补充句）。
    assert resolver.context_calls, "真实主流程应自动调用链恢复"
    chain = resolver.context_calls[0]["context"].chain
    assert [node.text for node in chain] == [root.text, "先把标题改成 X。"]
    assert [node.version for node in chain] == [1, 2]
    source = str(child.structured_limits_source or "")
    assert "r9h2-limit-extraction-chain-v1" in source
    assert ";chain=" in source and ";version=2" in source

    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    with requirement_context(binding):
        denied = await ctx.authorization.authorize(_write(b_path))
        allowed = await ctx.authorization.authorize(_write(a_path))
    assert denied.verdict == Verdict.DENY, denied.reason
    assert allowed.verdict == Verdict.ALLOW, allowed.reason


async def test_parse_failure_unresolved_uses_specific_confirmation_not_independent_review(
    ctx, tmp_path
):
    """H3 §3.1：unrestricted + unresolved 不再走普通独立授权审查。

    保留"解析失败持久化 unresolved/source、无关读取允许"等仍然有效的事实；
    旧 H2 的 targeted review（B DENY/A ALLOW/reviewer>=2）已按冻结契约删除。
    """
    b_path = tmp_path / "proj" / "B.py"
    a_path = tmp_path / "proj" / "A.py"
    resolver = FakeResolver(error=RequirementResolutionUnavailable("解析器不可用"))
    observed = await _run_turn(
        ctx, message="处理项目，但不要修改 %s。" % b_path, resolver=resolver
    )
    binding = observed["binding"]
    assert binding is not None
    requirement = ctx.requirements.by_id(binding.requirement_id)
    assert requirement is not None
    assert requirement.structured_limits_state == requirement_limits.STATE_UNRESOLVED
    assert requirement.structured_limits_source.startswith("resolution_unavailable:")
    assert observed["status"] == "unavailable"

    reviewer = PathReviewer(b_path)
    ctx.authorization.reviewer = reviewer
    ctx.settings_store.set("computer.permission_mode", "unrestricted")
    with requirement_context(binding):
        denied_unresolved = await ctx.authorization.authorize(_write(b_path))
        allowed_unresolved = await ctx.authorization.authorize(_write(a_path))
        read = await ctx.authorization.authorize(_read(b_path))
    # H3：unresolved 相关操作只给具体确认，不调用普通独立授权审查；读仍允许。
    assert denied_unresolved.verdict == Verdict.APPROVE, denied_unresolved.reason
    assert allowed_unresolved.verdict == Verdict.APPROVE, allowed_unresolved.reason
    assert read.verdict == Verdict.ALLOW
    assert reviewer.calls == [], "unrestricted 不得调用普通独立授权审查"


async def test_hardlink_and_moved_internal_file_still_protected(ctx, tmp_path):
    data_dir = Path(ctx.settings.data_dir)
    protected = data_dir / "identity-secret.db"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("internal", encoding="utf-8")
    refreshed = ctx.refresh_internal_data_identities()
    assert refreshed.get("ok") is True, refreshed

    outside = tmp_path / "outside-link.db"
    try:
        os.link(protected, outside)
    except OSError as exc:  # 平台/文件系统不支持硬链接：明确记录能力限制，不伪造验证
        pytest.skip("平台不支持硬链接，无法验证对象身份（能力限制）：%s" % exc)
    ctx.refresh_internal_data_identities()

    ctx.settings_store.set("computer.permission_mode", "read_only")
    with requirement_context(None):
        linked = await ctx.authorization.authorize(_read(outside))
    assert linked.verdict == Verdict.DENY, linked.reason
    assert "内部运行数据" in linked.reason

    # 同一对象改名/移动出注册目录：仍按真实身份保护，不因路径变化降级。
    moved = tmp_path / "moved.db"
    os.replace(protected, moved)
    ctx.refresh_internal_data_identities()
    with requirement_context(None):
        moved_read = await ctx.authorization.authorize(_read(moved))
        moved_write = await ctx.authorization.authorize(_write(moved))
    assert moved_read.verdict == Verdict.DENY, moved_read.reason
    assert moved_write.verdict == Verdict.APPROVE, moved_write.reason
    assert "三次确认" in moved_write.reason


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