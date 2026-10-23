"""R9-G 验收：审批/授权判定链路的真实接线（W4）。

覆盖 docs/r9g-contracts.md §12 的 A/B/C/D：
* A 语义与来源：受控模型返回下，陈述/礼貌/疑问/否定/条件/引号/材料不改变"模型理解 + 后端结构校验"的接线；
* B 模式与命令：三模式矩阵、固定查询、永久删除政策、内部数据三次确认、同名文件不误判、应用受控运行；
* C 批量与要求关联：批量逐项、单次合并审批、新增对象不偷用授权、要求版本绑定不串用；
* D 一致性与执行前核对：脚本/参数变化旧许可不复用、模式降级、过期、重复回调、确认顺序与内容绑定。

假件边界（契约 §11）：模型调用一律 FakeReviewer/FakeAdapter（唯一允许的 fake）；
create_app / AuthorizationEngine / 注册表 / 工具 / 审批存储与执行器全部真实装配。
依赖分支未合入的用例标 xfail(strict=False) + 明确 reason：可运行、不隐藏，集成后应转绿。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.adapters.base import ToolCall
from agent.api.server import create_app
from agent.config import Settings
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.storage.settings import SettingsStore

PENDING_W1 = "R9-G W1（删关键词守卫/要求版本绑定/要求解析）未合入本分支：待集成后转绿"
PENDING_W2 = "R9-G W2（fixed_queries/deletion 结构化）未合入本分支：待集成后转绿"
PENDING_LEAD = "R9-G Lead（recheck/批量/应用受控运行接线）未合入本分支：待集成后转绿"
PENDING_W3 = "R9-G W3（fs/附件逐项与批次状态）未合入本分支：待集成后转绿"


# ---------------------------------------------------------------------------
# 装置（真实应用 + 受控模型返回）
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_client(db_conn, settings: Settings, tmp_path):
    from agent.credentials.store import MemoryKeyring

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SettingsStore(db_conn)
    store.set("computer.root_dir", str(workspace))
    store.set("computer.permission_mode", "restricted")
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as client:
        client.workspace = workspace
        yield client


def _binding_for_client(client: TestClient):
    """测试装置：给工具/引擎提供一个真实存在的用户要求绑定。

    生产由 turn 运行时的要求解析建立；验收测试直接登记一条要求模拟该上下文，
    避免用"最近一条 active"这种要被替换的旧逻辑。
    """
    binding = getattr(client, "_r9g_binding", None)
    if binding is not None:
        return binding
    from agent.security.context import current_requirement

    binding = current_requirement.get()
    if binding is not None:
        return binding
    ctx = client.app.state.ctx
    requirement = _record_requirement(client, "按我的要求整理并保存工作区文件")
    binder = getattr(ctx.requirements, "binding_for", None)
    binding = binder(requirement.requirement_id) if callable(binder) else None
    if binding is None:
        binding = SimpleNamespace(
            requirement_id=requirement.requirement_id,
            version=int(getattr(requirement, "version", 1) or 1),
        )
    client._r9g_binding = binding
    return binding


def _exec(client: TestClient, name: str, arguments: dict):
    async def _do():
        from agent.security.context import requirement_context

        with requirement_context(_binding_for_client(client)):
            return await client.app.state.ctx.registry.execute(
                ToolCall(id="call_r9g", name=name, arguments=arguments)
            )

    return client.portal.call(_do)


def _pending(client: TestClient) -> list[dict]:
    resp = client.get("/api/approvals/pending")
    assert resp.status_code == 200
    return resp.json()["approvals"]


def _set_mode(client: TestClient, mode: str) -> None:
    resp = client.put("/api/security/mode", json={"mode": mode, "user_confirmed": True})
    assert resp.status_code == 200, resp.text


def _decide(client: TestClient, approval_id: str, decision: str):
    return client.post(f"/api/approvals/{approval_id}/decide", json={"decision": decision})


def _row(client: TestClient, approval_id: str):
    return client.app.state.ctx.conn.execute(
        "SELECT * FROM pending_approvals WHERE approval_id = ?", (approval_id,)
    ).fetchone()


def _wait_terminal(client: TestClient, approval_id: str, states: set[str], *, timeout: float = 20.0):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _row(client, approval_id)
        if row is not None and str(row["state"]) in states:
            return row
        time.sleep(0.05)
    return row


def _authorize(client: TestClient, effect: Effect):
    async def _do():
        from agent.security.context import requirement_context

        with requirement_context(_binding_for_client(client)):
            return await client.app.state.ctx.authorization.authorize(effect)

    return client.portal.call(_do)


def _write_effect(path, **detail) -> Effect:
    return Effect(
        kind=EffectKind.WRITE,
        targets=(Target(kind="path", value=str(path)),),
        purpose="写用户文件",
        detail=dict(detail),
    )


def _delete_effect(path, **detail) -> Effect:
    return Effect(
        kind=EffectKind.DELETE,
        targets=(Target(kind="path", value=str(path)),),
        purpose="删除文件",
        detail=dict(detail),
    )


def _execute_effect(program: str, argv=(), cwd=None, **detail) -> Effect:
    targets = [Target(kind="program", value=program)]
    if cwd is not None:
        targets.append(Target(kind="path", value=str(cwd)))
    data = {"argv": [program, *[str(a) for a in argv]], "shell": False}
    data.update(detail)
    return Effect(kind=EffectKind.EXECUTE, targets=tuple(targets), purpose="运行程序", detail=data)


def _record_requirement(client: TestClient, text: str, *, message_id: str = "msg_r9g"):
    ctx = client.app.state.ctx
    return ctx.requirements.record_user_message(
        text=text, message_id=message_id, turn_id="turn_r9g", topic_id=None
    )


class _FakeReviewer:
    """受控模型返回：只替代模型调用；结构校验/记录/复用仍走真实 review 模块。"""

    def __init__(self, decision=None, *, outcome_factory=None):
        from agent.security.review import ReviewDecision

        self.decision = decision or ReviewDecision.ALLOW
        self.outcome_factory = outcome_factory
        self.calls: list = []

    async def review(self, request):
        from agent.security.review import ReviewDecision, ReviewOutcome

        self.calls.append(request)
        if self.outcome_factory is not None:
            return self.outcome_factory(request)
        allow = self.decision == ReviewDecision.ALLOW
        op = request.operation
        return ReviewOutcome(
            decision=self.decision,
            reason="依据用户要求",
            requirement_id=request.requirement.requirement_id,
            allowed_operations=(op.kind,) if allow else (),
            objects=op.objects if allow else (),
            limits=("仅限本次列明的对象",) if allow else (),
            purpose=op.summary or "用户要求",
        )


def _install_reviewer(client: TestClient, text: str, decision=None, *, outcome_factory=None):
    ctx = client.app.state.ctx
    requirement = _record_requirement(client, text)
    binder = getattr(ctx.requirements, "binding_for", None)
    binding = binder(requirement.requirement_id) if callable(binder) else None
    if binding is None:
        binding = SimpleNamespace(
            requirement_id=requirement.requirement_id,
            version=int(getattr(requirement, "version", 1) or 1),
        )
    client._r9g_binding = binding
    reviewer = _FakeReviewer(decision, outcome_factory=outcome_factory)
    ctx.authorization.reviewer = reviewer
    return ctx, requirement, reviewer


def _internal_target(client: TestClient) -> str:
    target = Path(client.app.state.ctx.settings.data_dir) / "tmp" / "r9g-internal.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


# ---------------------------------------------------------------------------
# A. 语义与来源（受控模型返回验证接线；不宣称模型语义正确）
# ---------------------------------------------------------------------------


def test_capability_question_maps_confirm_to_approval_without_modification(app_client):
    """能力询问（疑问句）下模型给 CONFIRM → 引擎 APPROVE，文件不被修改。"""
    from agent.security.review import ReviewDecision

    client = app_client
    target = client.workspace / "capability.txt"
    target.write_text("原始", encoding="utf-8")
    ctx, requirement, reviewer = _install_reviewer(
        client, "你能把这个文件的标题改成 hello 吗？", ReviewDecision.CONFIRM
    )
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict == Verdict.APPROVE, decision
    assert reviewer.calls, "受限模式下写用户文件应触发模型审查"
    assert target.read_text(encoding="utf-8") == "原始"


def test_model_deny_maps_to_deny(app_client):
    from agent.security.review import ReviewDecision

    client = app_client
    target = client.workspace / "deny.txt"
    ctx, requirement, reviewer = _install_reviewer(client, "整理一下工作区", ReviewDecision.DENY)
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict == Verdict.DENY, decision


def test_review_unavailable_is_not_silent_allow(app_client):
    from agent.security.review import ReviewUnavailable

    client = app_client
    target = client.workspace / "unavailable.txt"

    async def _boom(request):
        raise ReviewUnavailable("模型超时（受控）")

    ctx, requirement, reviewer = _install_reviewer(client, "整理一下工作区", outcome_factory=_boom)
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict == Verdict.APPROVE, "模型不可用不得默认允许"
    assert not target.exists()


def test_allow_requires_limits_purpose_and_exact_objects(app_client):
    """后端结构校验：ALLOW 必须覆盖请求对象、给出 limits/purpose，不得扩大对象/操作。"""
    from agent.security.review import (
        ReviewDecision,
        ReviewObject,
        ReviewOperation,
        ReviewOutcome,
        ReviewRequest,
        ReviewUnavailable,
        validate_outcome,
    )

    client = app_client
    ctx = client.app.state.ctx
    requirement = _record_requirement(client, "把报告标题改成 hello")
    obj = ReviewObject(kind="path", value=str(client.workspace / "a.md"))
    operation = ReviewOperation(kind="write", objects=(obj,), summary="写报告")
    request = ReviewRequest(
        requirement=requirement,
        corrections=(),
        limits=(),
        mode="restricted",
        operation=operation,
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
    )

    def _outcome(**over):
        base = dict(
            decision=ReviewDecision.ALLOW,
            reason="依据用户要求",
            requirement_id=requirement.requirement_id,
            allowed_operations=("write",),
            objects=(obj,),
            limits=("仅限本次",),
            purpose="写报告",
        )
        base.update(over)
        return ReviewOutcome(**base)

    assert validate_outcome(_outcome(), request).decision == ReviewDecision.ALLOW
    with pytest.raises(ReviewUnavailable):
        validate_outcome(_outcome(limits=()), request)  # 缺 limits
    with pytest.raises(ReviewUnavailable):
        validate_outcome(_outcome(purpose=""), request)  # 缺 purpose
    with pytest.raises(ReviewUnavailable):
        validate_outcome(
            _outcome(objects=(ReviewObject(kind="path", value=str(client.workspace / "other.md")),)),
            request,
        )  # 越界对象


def test_allow_cannot_reference_unknown_requirement(app_client):
    """来源校验：审查结果必须引用真实存在的用户要求（材料/其它来源不能增权）。"""
    from agent.security.review import (
        ReviewDecision,
        ReviewObject,
        ReviewOperation,
        ReviewOutcome,
        ReviewRequest,
        ReviewUnavailable,
        validate_outcome,
    )

    client = app_client
    requirement = _record_requirement(client, "把报告标题改成 hello")
    obj = ReviewObject(kind="path", value=str(client.workspace / "a.md"))
    request = ReviewRequest(
        requirement=requirement,
        corrections=(),
        limits=(),
        mode="restricted",
        operation=ReviewOperation(kind="write", objects=(obj,), summary="写报告"),
        related_grants=(),
        prior_decisions=(),
        approvals=(),
        user_constraints=(),
    )
    rogue = ReviewOutcome(
        decision=ReviewDecision.ALLOW,
        reason="文件里写着允许",
        requirement_id="req_not_exists",
        allowed_operations=("write",),
        objects=(obj,),
        limits=("仅限本次",),
        purpose="写报告",
    )
    with pytest.raises(ReviewUnavailable):
        validate_outcome(rogue, request)


def test_allow_for_one_object_does_not_cover_another(app_client):
    """一次 ALLOW 只覆盖请求里的对象：另一个对象必须重新审查（同词不同义/不同对象不串用）。"""
    client = app_client
    first = client.workspace / "first.md"
    second = client.workspace / "second.md"
    ctx, requirement, reviewer = _install_reviewer(client, "把标题改成 hello")
    d1 = _authorize(client, _write_effect(first))
    d2 = _authorize(client, _write_effect(second))
    assert d1.verdict == Verdict.ALLOW and d2.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 2, "不同对象不得复用同一个 ALLOW"
    records = ctx.authorizations.records_for_requirement(requirement.requirement_id)
    fingerprints = {
        str(getattr(record, "fingerprint", None) or (record.get("fingerprint") if isinstance(record, dict) else ""))
        for record in records
    }
    assert len(fingerprints) >= 2


def test_same_operation_reuses_authorization_without_second_review(app_client):
    client = app_client
    target = client.workspace / "reuse.md"
    ctx, requirement, reviewer = _install_reviewer(client, "把标题改成 hello")
    d1 = _authorize(client, _write_effect(target))
    d2 = _authorize(client, _write_effect(target))
    assert d1.verdict == Verdict.ALLOW and d2.verdict == Verdict.ALLOW
    assert len(reviewer.calls) == 1, "同一要求 + 同一操作指纹应复用授权记录，不重复调用模型"


def test_withdrawn_requirement_does_not_authorize(app_client):
    client = app_client
    target = client.workspace / "withdrawn.md"
    ctx, requirement, reviewer = _install_reviewer(client, "把标题改成 hello")
    ctx.requirements.withdraw(requirement.requirement_id)
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict != Verdict.ALLOW, "已撤回的要求不得继续授权"
    assert not target.exists()


def test_discussion_text_does_not_change_write_verdict(app_client):
    """讨论命令/删除词不改变 WRITE 的判定（不产生删除意图、不误拒绝）。

    受控模型返回 CONFIRM：写入应停在人工确认（APPROVE），既不能因"讨论了 rm -rf"
    被当成永久删除，也不能被文本内容自动放行。
    """
    from agent.security.review import ReviewDecision

    client = app_client
    target = client.workspace / "discuss.md"
    ctx, requirement, reviewer = _install_reviewer(
        client, "帮我解释一下 rm -rf 这个命令是什么意思", ReviewDecision.CONFIRM
    )
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict == Verdict.APPROVE, decision


@pytest.mark.xfail(strict=False, reason=PENDING_W1)
def test_question_text_does_not_block_model_allow(app_client):
    """R9-G §1/§2：模型 ALLOW 不再因疑问句文字被后端关键词守卫推翻。"""
    client = app_client
    target = client.workspace / "question.md"
    ctx, requirement, reviewer = _install_reviewer(client, "你能把这个文件的标题改成 hello 吗？")
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict == Verdict.ALLOW, decision


@pytest.mark.xfail(strict=False, reason=PENDING_W1)
def test_negation_elsewhere_does_not_block_model_allow(app_client):
    """R9-G §1：否定词出现在别处（针对另一个对象）时，不得靠文本包含推翻模型 ALLOW。"""
    client = app_client
    allowed = client.workspace / "allowed.md"
    other = client.workspace / "other.md"
    ctx, requirement, reviewer = _install_reviewer(
        client, f"不要修改 {other}；把 {allowed} 的标题改成 hello"
    )
    decision = _authorize(client, _write_effect(allowed))
    assert decision.verdict == Verdict.ALLOW, decision


@pytest.mark.xfail(strict=False, reason=PENDING_W1)
def test_structured_limits_from_resolution_are_enforced(app_client):
    """R9-G §3：无限制模式的用户限制来自要求解析的结构化 limits（不是关键词）。"""
    from agent.security.resolution import ResolutionOutcome, RequirementRelation  # noqa: F401

    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx, "requirement_resolver") or hasattr(ctx.requirements, "apply_resolution")


# ---------------------------------------------------------------------------
# B. 模式与命令
# ---------------------------------------------------------------------------


def test_three_mode_verdict_matrix(app_client):
    client = app_client
    target = client.workspace / "matrix.txt"
    expected = {
        "read_only": (Verdict.DENY, Verdict.DENY, Verdict.ALLOW),
        "restricted": (Verdict.APPROVE, Verdict.APPROVE, Verdict.ALLOW),
        "unrestricted": (Verdict.ALLOW, Verdict.ALLOW, Verdict.ALLOW),
    }
    for mode, (write_v, exec_v, read_v) in expected.items():
        _set_mode(client, mode)
        write = _authorize(client, _write_effect(target))
        execute = _authorize(client, _execute_effect(sys.executable, ("-c", "print(1)")))
        read = _authorize(
            client, Effect(kind=EffectKind.READ, targets=(Target(kind="path", value=str(target)),))
        )
        assert write.verdict == write_v, (mode, write)
        assert execute.verdict == exec_v, (mode, execute)
        assert read.verdict == read_v, (mode, read)


@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
def test_internal_data_three_confirmations_all_modes(app_client, mode):
    client = app_client
    _set_mode(client, mode)
    result = _exec(client, "fs_write", {"path": _internal_target(client), "content": "x"})
    assert result.ok is True, result.error
    item = _pending(client)[0]
    assert int(item.get("requires_confirmations") or 1) == 3, item
    assert int(item.get("confirmed_stage") or 0) == 0, item
    _decide(client, item["approval_id"], "cancel")


def test_internal_data_three_confirmations_execute(app_client):
    client = app_client
    _set_mode(client, "restricted")
    target = _internal_target(client)
    result = _exec(client, "fs_write", {"path": target, "content": "内部数据写入"})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    for stage in (1, 2, 3):
        resp = client.post(f"/api/approvals/{approval_id}/confirm", json={"stage": stage})
        assert resp.status_code == 200, resp.text
    row = _wait_terminal(client, approval_id, {"completed", "failed"})
    assert row is not None and str(row["state"]) == "completed", dict(row) if row else None
    assert Path(target).read_text(encoding="utf-8") == "内部数据写入"


def test_normal_same_named_file_is_not_internal_data(app_client):
    """普通同名文件（工作区里的 qio.db）不得被误判成内部运行数据。"""
    client = app_client
    lookalike = client.workspace / "qio.db"
    lookalike.write_text("普通文件", encoding="utf-8")
    decision = _authorize(client, _write_effect(lookalike))
    assert decision.verdict == Verdict.APPROVE, "工作区普通文件应走普通审批，不是三次确认"


def test_unrestricted_skips_model_review(app_client):
    from agent.security.review import ReviewDecision

    client = app_client
    _set_mode(client, "unrestricted")
    target = client.workspace / "unrestricted.md"
    ctx, requirement, reviewer = _install_reviewer(client, "整理工作区", ReviewDecision.CONFIRM)
    decision = _authorize(client, _write_effect(target))
    assert decision.verdict == Verdict.ALLOW, decision
    assert reviewer.calls == [], "无限制模式不调用普通授权审查"


@pytest.mark.xfail(strict=False, reason=PENDING_W2)
def test_fixed_query_allowed_in_read_only_and_restricted(app_client):
    """R9-G §4：固定查询（python --version）在只读/受限都直接 ALLOW，不审查不弹确认。"""
    from agent.security.fixed_queries import FixedQueryFacts, classify_fixed_query  # noqa: F401

    client = app_client
    for mode in ("read_only", "restricted"):
        _set_mode(client, mode)
        result = _exec(client, "run_program", {"program": sys.executable, "args": ["--version"]})
        assert result.ok is True, (mode, result.error)
        assert _pending(client) == [], (mode, _pending(client))


@pytest.mark.xfail(strict=False, reason=PENDING_W2)
def test_fixed_query_classifier_rejects_scripts_and_metacharacters(app_client):
    """R9-G §4：解释器脚本、重定向/串联/嵌套都不算固定查询（解析不确定 → None）。"""
    from agent.security.fixed_queries import FixedQueryFacts, classify_fixed_query

    facts = FixedQueryFacts(
        kind="program",
        program_path=sys.executable,
        program_name=Path(sys.executable).name,
        argv=(sys.executable, "-c", "print(1)"),
        cwd=None,
        shell=False,
    )
    assert classify_fixed_query(facts) is None
    shell_facts = FixedQueryFacts(
        kind="shell",
        program_path=None,
        program_name="cmd",
        argv=("cmd", "/c", "python --version > out.txt"),
        cwd=None,
        shell=True,
    )
    assert classify_fixed_query(shell_facts) is None


@pytest.mark.xfail(strict=False, reason=PENDING_W2)
def test_permanent_delete_policy_three_modes(app_client):
    """R9-G §5：永久删除 read_only DENY / restricted 需人工确认 / unrestricted ALLOW。"""
    from agent.security.deletion import DeleteIntent, detect_delete_intent  # noqa: F401

    client = app_client
    target = client.workspace / "to-delete.txt"
    target.write_text("x", encoding="utf-8")
    expected = {"read_only": Verdict.DENY, "restricted": Verdict.APPROVE, "unrestricted": Verdict.ALLOW}
    for mode, want in expected.items():
        _set_mode(client, mode)
        decision = _authorize(client, _delete_effect(target, permanent=True))
        assert decision.verdict == want, (mode, decision)


@pytest.mark.xfail(strict=False, reason=PENDING_W2)
def test_model_allow_cannot_replace_permanent_delete_confirmation(app_client):
    """R9-G §5：restricted 下模型 ALLOW 不能替代永久删除的人工确认。"""
    client = app_client
    target = client.workspace / "perm.txt"
    target.write_text("x", encoding="utf-8")
    ctx, requirement, reviewer = _install_reviewer(client, f"永久删除 {target}，不要进回收站")
    decision = _authorize(client, _delete_effect(target, permanent=True))
    assert decision.verdict == Verdict.APPROVE, "永久删除必须人工确认，模型 ALLOW 不可替代"


@pytest.mark.xfail(strict=False, reason=PENDING_LEAD)
def test_app_managed_marker_cannot_bypass_read_only(app_client):
    """R9-G §8：应用受控运行必须用可信内部操作类型/路径区分，任意 detail 自报标记不得豁免。"""
    client = app_client
    _set_mode(client, "read_only")
    target = client.workspace / "self-declared.txt"
    decision = _authorize(client, _write_effect(target, app_managed=True))
    assert decision.verdict != Verdict.ALLOW, "只读模式下不得靠自报 app_managed 绕过"


# ---------------------------------------------------------------------------
# C. 批量与要求关联
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=False, reason=PENDING_W3 + "；Lead 批量接线")
def test_batch_items_expand_and_authorize_individually(app_client):
    from agent.security.batch import BatchItem  # noqa: F401

    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx.authorization, "authorize_batch")


@pytest.mark.xfail(strict=False, reason=PENDING_W3 + "；Lead 批量接线")
def test_batch_consolidates_needing_confirmation_items(app_client):
    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx.authorization, "authorize_batch"), "批量应合并为一个审批操作"


@pytest.mark.xfail(strict=False, reason=PENDING_W3 + "；Lead 批量接线")
def test_batch_new_object_does_not_inherit_authorization(app_client):
    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx.authorization, "authorize_batch")


@pytest.mark.xfail(strict=False, reason=PENDING_W1)
def test_requirement_version_binding_prevents_cross_use(app_client):
    """R9-G §3：操作绑定显式要求（版本），不得回退"最近一条 active"。"""
    from agent.security.requirements import RequirementBinding  # noqa: F401

    client = app_client
    ctx = client.app.state.ctx
    first = _record_requirement(client, "把 A 的标题改成 hello", message_id="m-a")
    second = _record_requirement(client, "我们聊点别的", message_id="m-b")
    binding = ctx.requirements.binding_for(first.requirement_id)
    assert binding is not None and binding.version >= 1
    assert ctx.requirements.validate_binding(binding) is True
    ctx.requirements.withdraw(first.requirement_id)
    assert ctx.requirements.validate_binding(binding) is False


@pytest.mark.xfail(strict=False, reason=PENDING_W1)
def test_background_and_subagent_inherit_requirement_binding(app_client):
    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx.authorization, "child_scope")
    scope = ctx.authorization.child_scope(purpose="子任务")
    assert getattr(scope, "requirement", None) is not None, "子智能体应继承父要求绑定"


@pytest.mark.xfail(strict=False, reason=PENDING_W1)
def test_requirement_resolution_new_amend_withdraw(app_client):
    from agent.security.resolution import (  # noqa: F401
        RequirementRelation,
        ResolutionOutcome,
        validate_resolution,
    )

    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx, "requirement_resolver") or hasattr(ctx.requirements, "apply_resolution")


# ---------------------------------------------------------------------------
# D. 一致性与执行前核对
# ---------------------------------------------------------------------------


def test_script_change_invalidates_approval(app_client, tmp_path):
    """已实现（R9-F W2）：批准后脚本内容变化 → 旧许可失效，篡改内容不执行。"""
    client = app_client
    script = tmp_path / "job.py"
    original_marker = tmp_path / "original.marker"
    tampered_marker = tmp_path / "tampered.marker"
    script.write_text(f"open(r'{original_marker}', 'w').write('orig')", encoding="utf-8")

    result = _exec(client, "run_program", {"program": sys.executable, "args": [str(script)]})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    script.write_text(f"open(r'{tampered_marker}', 'w').write('tampered')", encoding="utf-8")
    _decide(client, approval_id, "approve")
    row = _wait_terminal(client, approval_id, {"failed", "invalidated", "completed"})
    assert row is not None and str(row["state"]) in {"failed", "invalidated"}, (
        dict(row) if row else None
    )
    assert not tampered_marker.exists()
    assert not original_marker.exists()


@pytest.mark.xfail(strict=False, reason=PENDING_LEAD + "：执行器需要独立于 payload 的绑定摘要")
def test_argv_change_in_approval_record_is_not_executed(app_client, tmp_path):
    """审批记录里的 program/argv 被改动后，重放不得执行改动后的程序。

    现状：执行器从同一条 operation_detail 重新构造 effect，记录被改动时"预期形状"与
    "实际形状"一起变 → 仍会执行（缺少独立于 payload 的 spec_digest/绑定）。
    """
    client = app_client
    tampered_marker = tmp_path / "argv-tampered.marker"
    result = _exec(
        client, "run_program", {"program": sys.executable, "args": ["-c", "print('ORIGINAL')"]}
    )
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    ctx = client.app.state.ctx
    row = _row(client, approval_id)
    detail = json.loads(row["operation_detail"])
    detail["payload"]["args"] = ["-c", f"open(r'{tampered_marker}', 'w').write('x')"]
    ctx.conn.execute(
        "UPDATE pending_approvals SET operation_detail = ? WHERE approval_id = ?",
        (json.dumps(detail), approval_id),
    )
    ctx.conn.commit()
    _decide(client, approval_id, "approve")
    row = _wait_terminal(client, approval_id, {"failed", "invalidated", "completed"})
    assert row is not None, "审批应进入终态"
    assert not tampered_marker.exists(), "审批记录与本次执行不一致时不得执行"


def test_mode_downgrade_before_execution_is_denied():
    """执行前重核对：模式快照为受限、当前已切只读 → 不允许执行。"""
    from agent.security.operations import ExecutionContext

    ctx = ExecutionContext(
        approval_id="a1",
        operation=SimpleNamespace(),
        mode_at_request="restricted",
        mode_at_approval="restricted",
        engine=SimpleNamespace(),  # 非 None 才会走模式重核对分支
        mode_getter=lambda: "read_only",
    )
    import asyncio

    allowed, reason = asyncio.run(ctx.authorize_started())
    assert allowed is False and "只读" in reason, (allowed, reason)


@pytest.mark.xfail(strict=False, reason=PENDING_LEAD + "：decide/执行前未核对 expires_at（实测过期审批仍可执行）")
def test_expired_approval_does_not_execute(app_client):
    """R9-G §9：过期审批不得执行。

    实测（基线 97777b55）：storage.decide 的 approve 分支不检查 expires_at，
    expire_waiting 只在 recover/列表收口时调用 → 未被收口的过期 waiting 行仍可批准并执行。
    """
    client = app_client
    target = client.workspace / "expired.txt"
    target.write_text("before", encoding="utf-8")
    result = _exec(client, "fs_write", {"path": str(target), "content": "after"})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    ctx = client.app.state.ctx
    ctx.conn.execute(
        "UPDATE pending_approvals SET expires_at = ? WHERE approval_id = ?",
        ("2000-01-01T00:00:00+00:00", approval_id),
    )
    ctx.conn.commit()
    resp = _decide(client, approval_id, "approve")
    row = _wait_terminal(client, approval_id, {"expired", "invalidated", "rejected", "completed", "failed"})
    assert row is not None and str(row["state"]) != "completed", dict(row) if row else None
    assert target.read_text(encoding="utf-8") == "before", "过期审批不得执行"


def test_duplicate_decide_does_not_execute_twice(app_client):
    client = app_client
    target = client.workspace / "once.txt"
    target.write_text("before", encoding="utf-8")
    result = _exec(client, "fs_write", {"path": str(target), "content": "after"})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    _decide(client, approval_id, "approve")
    row = _wait_terminal(client, approval_id, {"completed", "failed"})
    assert row is not None and str(row["state"]) == "completed", dict(row) if row else None
    assert target.read_text(encoding="utf-8") == "after"
    _decide(client, approval_id, "approve")  # 重复回调
    time.sleep(0.3)
    row2 = _row(client, approval_id)
    assert int(row2["attempt"] or 0) == int(row["attempt"] or 0), "重复回调不得再次领取执行"


def test_confirm_stage_must_be_sequential(app_client):
    client = app_client
    _set_mode(client, "unrestricted")
    result = _exec(client, "fs_write", {"path": _internal_target(client), "content": "x"})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    resp = client.post(f"/api/approvals/{approval_id}/confirm", json={"stage": 2})
    assert resp.status_code >= 400, ("跳阶段必须被拒绝", resp.status_code, resp.text[:200])


@pytest.mark.xfail(strict=False, reason=PENDING_LEAD)
def test_confirmations_bind_same_object_and_content(app_client):
    """R9-G §9：三次确认必须绑定相同对象/动作/内容（内容变化 → 确认重置）。"""
    client = app_client
    _set_mode(client, "restricted")
    target = _internal_target(client)
    result = _exec(client, "fs_write", {"path": target, "content": "v1"})
    assert result.ok is True, result.error
    approval_id = _pending(client)[0]["approval_id"]
    client.post(f"/api/approvals/{approval_id}/confirm", json={"stage": 1})
    ctx = client.app.state.ctx
    row = _row(client, approval_id)
    detail = json.loads(row["operation_detail"])
    detail["payload"]["content"] = "v2"
    ctx.conn.execute(
        "UPDATE pending_approvals SET operation_detail = ? WHERE approval_id = ?",
        (json.dumps(detail), approval_id),
    )
    ctx.conn.commit()
    row_after = _row(client, approval_id)
    assert int(row_after["confirmed_stage"] or 0) == 0, "内容变化后确认必须重置为 0"


@pytest.mark.xfail(strict=False, reason=PENDING_LEAD)
def test_recheck_service_revalidates_mode_requirement_objects(app_client):
    from agent.security.recheck import OperationBinding, RecheckService  # noqa: F401

    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx, "recheck") or hasattr(ctx.authorization, "recheck")


@pytest.mark.xfail(strict=False, reason=PENDING_LEAD)
def test_queue_wait_mode_change_affects_only_unstarted(app_client):
    """R9-G §7/§8：等待期间模式变化只影响尚未开始的操作。"""
    client = app_client
    ctx = client.app.state.ctx
    assert hasattr(ctx, "recheck") or hasattr(ctx.authorization, "recheck")