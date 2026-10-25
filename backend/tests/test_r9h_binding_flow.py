# -*- coding: utf-8 -*-
"""R9-H A：要求绑定、真实用户原话与主流程接线（契约 §3、§6）。

全部用例通过**真实 _execute_turn()**：
* 真实 InstructionRegistry / RequirementLedger / apply_resolution / 版本绑定；
* 模型调用用 FakeResolver（可控返回/异常/延迟），编排器用可观察替身（契约允许）；
* 不使用"预先登记正确要求再手动 set requirement_context"作为修复证据。

覆盖：
1) new/amend 后主流程拿到 AppliedResolution.requirement 的有效版本绑定；
2) withdraw 后不绑定已撤回要求、旧授权不可复用；
3) 真实用户原话逐字保存（模型 corrections 改写/追加也不得替代）；
4) message_id/turn_id/topic_id 来源正确、不串用；
5) 解析成功但 binding 建立失败：明确记录、status=unavailable、不回退登记另一条；
6) 解析失败：按新要求登记并标 unavailable，不扩大旧授权；
7) 取消轮：不触发后续实际操作、终态仍正确；
8) R9-H §6：legacy/unresolved 历史要求经模型重新理解并持久化；失败标 unresolved；
   旧文字原样交给模型（不关键词解析）、不影响 new/amend/withdraw 主路径；
9) resolution.py 的 removed_limits 与 extract_limits 结构契约。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnContext
from agent.prompts import (
    LIMIT_EXTRACTION_CHAIN_SCHEMA_VERSION,
    LIMIT_EXTRACTION_SCHEMA_VERSION,
    REQUIREMENT_RESOLUTION_SCHEMA_VERSION,
)
from agent.security.context import (
    current_requirement,
    requirement_limits,
    requirement_resolution_status,
)
from agent.security.requirements import (
    AuthorizationRecord,
    RequirementBinding,
)
from agent.security.resolution import (
    RequirementRelation,
    RequirementResolutionUnavailable,
    ResolutionOutcome,
    build_limit_extraction_payload,
    parse_extracted_limits,
    parse_resolution_outcome,
    render_limit_extraction_messages,
    validate_resolution,
)

_NOW = "2026-10-23T00:00:00+00:00"


class FakeResolver:
    """可编程要求解析器：记录调用、返回固定结果或抛错。"""

    model = "fake-r9h-model"
    prompt_version = REQUIREMENT_RESOLUTION_SCHEMA_VERSION
    limit_extraction_prompt_version = LIMIT_EXTRACTION_SCHEMA_VERSION

    def __init__(self, *, outcome=None, error=None, extract_limits=(), extract_error=None) -> None:
        self.outcome = outcome
        self.error = error
        self.extract_result = tuple(extract_limits)
        self.extract_error = extract_error
        self.resolve_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    async def resolve(self, **kwargs):
        self.resolve_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return self.outcome

    async def extract_limits(self, text, *, requirement_id=None, version=None):
        self.extract_calls.append(
            {"text": text, "requirement_id": requirement_id, "version": version}
        )
        if self.extract_error is not None:
            raise self.extract_error
        return self.extract_result


class _RequirementView:
    """给 RequirementLedger.by_id 结果补上 C 分支的 structured_limits_state 字段。"""

    def __init__(self, requirement, *, state: str) -> None:
        self._requirement = requirement
        self.structured_limits_state = state
        self.structured_limits = ()

    def __getattr__(self, name):
        return getattr(self._requirement, name)


class StructuredStateLedgerShim:
    """C 分支 RequirementLedger 接口的最小前向兼容替身（A 分支无该迁移/字段）。

    只模拟 R9-H §6 的持久化接口：structured_limits_state 与 set_structured_limits。
    其余方法（apply_resolution/binding_for/record_user_message/candidates/...）全部
    委托真实 RequirementLedger；测试仍通过真实 _execute_turn 与真实台账写库。
    """

    def __init__(self, real) -> None:
        self._real = real
        self._states: dict[str, str] = {}
        self.set_calls: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def mark_state(self, requirement_id, state: str) -> None:
        self._states[str(requirement_id)] = str(state)

    def state_of(self, requirement_id):
        return self._states.get(str(requirement_id))

    def by_id(self, requirement_id):
        requirement = self._real.by_id(requirement_id)
        if requirement is None:
            return None
        state = self._states.get(str(requirement_id))
        return requirement if state is None else _RequirementView(requirement, state=state)

    def apply_resolution(self, *args, **kwargs):
        applied = self._real.apply_resolution(*args, **kwargs)
        requirement = getattr(applied, "requirement", None)
        if requirement is not None:
            # 模拟 C 对历史父要求的处理：amend 子要求先按 legacy 处理，待模型重新理解。
            self.mark_state(requirement.requirement_id, "legacy")
        return applied

    def set_structured_limits(self, requirement_id, limits, *, source, state):
        self.set_calls.append(
            {
                "requirement_id": str(requirement_id),
                "limits": tuple(dict(item) for item in (limits or ())),
                "source": str(source),
                "state": str(state),
            }
        )
        self.mark_state(requirement_id, state)


@pytest.fixture()
def ctx(db_conn, tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"), db_conn)
    return app.state.ctx


def seed_requirement(ctx, text: str, *, message_id: str = "seed_msg", topic_id: str = "seed_topic"):
    return ctx.requirements.record_user_message(
        text=text, message_id=message_id, turn_id="seed_turn", topic_id=topic_id
    )


def seed_allow_record(ctx, requirement, *, fingerprint: str = "fp-old") -> AuthorizationRecord:
    record = AuthorizationRecord(
        record_id="authrec_" + uuid.uuid4().hex[:12],
        requirement_id=requirement.requirement_id,
        operation_kind="write",
        fingerprint=fingerprint,
        decision="allow",
        reason="旧的模型审查允许",
        allowed_operations=("write",),
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
        model="fake-r9h-model",
        prompt_version=REQUIREMENT_RESOLUTION_SCHEMA_VERSION,
    )


def requirements_rows(ctx):
    return ctx.conn.execute(
        "SELECT * FROM user_requirements ORDER BY created_at, rowid"
    ).fetchall()


def resolution_audit_rows(ctx):
    return ctx.conn.execute(
        "SELECT * FROM requirement_resolutions ORDER BY created_at, rowid"
    ).fetchall()


async def run_turn(
    ctx,
    *,
    message: str,
    resolver=None,
    user_message_id=None,
    cancelled: bool = False,
    orchestrator=None,
):
    """真实 _execute_turn：真实要求台账/绑定；编排器可观察替身（契约允许）。"""
    topic_id = ctx.current_topic()
    turn_id = "turn_flow_" + uuid.uuid4().hex[:10]
    if resolver is not None:
        ctx.requirement_resolver = resolver
    observed = {
        "executed": False,
        "binding": None,
        "status": None,
        "limits": (),
        "cancelled": None,
    }

    async def default_execute(turn_ctx):
        observed["executed"] = True
        observed["binding"] = current_requirement.get()
        observed["status"] = requirement_resolution_status.get()
        observed["limits"] = tuple(requirement_limits.get() or ())
        observed["cancelled"] = bool(getattr(turn_ctx, "cancelled", False))
        if orchestrator is not None:
            await orchestrator(turn_ctx)

    ctx.turn_orchestrator.execute = default_execute
    turn_ctx = TurnContext(
        turn_id=turn_id,
        message=message,
        initial_topic=topic_id,
        current_topic=topic_id,
        user_message_id=user_message_id,
    )
    turn_ctx.cancelled = cancelled
    await ctx._execute_turn(turn_ctx)
    return turn_ctx, observed


# ---------------------------------------------------------------------------
# 1) new：真实要求 + 有效版本绑定 + 原话逐字
# ---------------------------------------------------------------------------


async def test_new_resolution_binds_real_requirement_and_keeps_user_words(ctx):
    message = "帮我把 D:/work/季度报告.md 整理成一份纪要。"
    resolver = FakeResolver(
        outcome=make_outcome(
            RequirementRelation.NEW,
            corrections=("模型改写的摘要：整理季度报告",),
        )
    )
    turn_ctx, observed = await run_turn(
        ctx, message=message, resolver=resolver, user_message_id="msg_real_1"
    )

    rows = requirements_rows(ctx)
    assert len(rows) == 1
    requirement = ctx.requirements.by_id(str(rows[0]["id"]))
    assert requirement is not None and requirement.status == "active"
    assert requirement.text == message
    provenance = json.loads(rows[0]["provenance"])
    assert provenance["message_id"] == "msg_real_1"
    assert provenance["turn_id"] == turn_ctx.turn_id
    assert provenance["topic_id"] == turn_ctx.initial_topic

    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert binding.requirement_id == requirement.requirement_id
    assert binding.version == requirement.version == 1
    assert observed["status"] == "ok"
    assert ctx.requirements.binding_for(requirement.requirement_id) == binding


async def test_message_id_is_not_cross_used_when_unavailable(ctx):
    resolver = FakeResolver(outcome=make_outcome(RequirementRelation.NEW))
    turn_ctx, observed = await run_turn(ctx, message="整理报告。", resolver=resolver)
    row = requirements_rows(ctx)[0]
    provenance = json.loads(row["provenance"])
    assert provenance["message_id"] is None
    assert provenance["turn_id"] == turn_ctx.turn_id
    assert provenance["topic_id"] == turn_ctx.initial_topic
    assert provenance["message_id"] != turn_ctx.turn_id


# ---------------------------------------------------------------------------
# 2) amend：新版本绑定、旧版本不可用
# ---------------------------------------------------------------------------


async def test_amend_binds_new_version_and_invalidates_old_binding(ctx):
    parent = seed_requirement(ctx, "把 D:/work/周报.md 整理一下。")
    message = "补充：只改标题，正文不要动。"
    resolver = FakeResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=parent,
            target_version=parent.version,
            corrections=("只改标题",),
        )
    )
    turn_ctx, observed = await run_turn(
        ctx, message=message, resolver=resolver, user_message_id="msg_real_2"
    )

    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert binding.requirement_id != parent.requirement_id
    child = ctx.requirements.by_id(binding.requirement_id)
    assert child is not None
    assert child.version == 2 == parent.version + 1
    assert child.parent_id == parent.requirement_id
    assert child.text == message
    assert observed["status"] == "ok"
    assert ctx.requirements.binding_for(parent.requirement_id) is None
    assert ctx.requirements.validate_binding(
        RequirementBinding(parent.requirement_id, parent.version)
    ) is False


# ---------------------------------------------------------------------------
# 3) withdraw：不绑定已撤回要求，旧授权不可复用
# ---------------------------------------------------------------------------


async def test_withdraw_does_not_bind_withdrawn_requirement_or_reuse_old_grant(ctx):
    target = seed_requirement(ctx, "整理 D:/work/报告.md 并发送到 data.example.com。")
    seed_allow_record(ctx, target, fingerprint="fp-withdraw-old")
    resolver = FakeResolver(
        outcome=make_outcome(
            RequirementRelation.WITHDRAW,
            target=target,
            target_version=target.version,
        )
    )
    turn_ctx, observed = await run_turn(
        ctx, message="算了，刚才那个任务取消吧。", resolver=resolver
    )

    assert observed["binding"] is None
    assert observed["status"] == "ok"
    withdrawn = ctx.requirements.by_id(target.requirement_id)
    assert withdrawn is not None and withdrawn.status == "withdrawn"
    assert ctx.requirements.binding_for(target.requirement_id) is None
    assert ctx.authorizations.reuse(
        requirement_id=target.requirement_id,
        operation_kind="write",
        fingerprint="fp-withdraw-old",
    ) is None


# ---------------------------------------------------------------------------
# 4) 解析成功但 binding 建立失败：明确记录、不回退登记
# ---------------------------------------------------------------------------


async def test_binding_version_mismatch_is_explicit_failure_without_fallback_registration(
    ctx, monkeypatch
):
    resolver = FakeResolver(outcome=make_outcome(RequirementRelation.NEW))
    real_binding_for = ctx.requirements.binding_for

    def wrong_version(requirement_id):
        binding = real_binding_for(requirement_id)
        if binding is None:
            return None
        return RequirementBinding(binding.requirement_id, binding.version + 1)

    monkeypatch.setattr(ctx.requirements, "binding_for", wrong_version)
    turn_ctx, observed = await run_turn(
        ctx, message="帮我整理一下报告。", resolver=resolver
    )

    assert observed["binding"] is None
    assert observed["status"] == "unavailable"
    assert len(requirements_rows(ctx)) == 1
    assert any("binding_unavailable" in item for item in getattr(turn_ctx, "notices", []))
    audits = resolution_audit_rows(ctx)
    assert audits and audits[-1]["outcome"] == "unavailable"
    assert "binding_unavailable" in (audits[-1]["error"] or "")


async def test_missing_binding_is_explicit_failure_not_silent_success(ctx, monkeypatch):
    resolver = FakeResolver(outcome=make_outcome(RequirementRelation.NEW))
    monkeypatch.setattr(ctx.requirements, "binding_for", lambda requirement_id: None)
    turn_ctx, observed = await run_turn(ctx, message="整理一下报告。", resolver=resolver)

    assert observed["binding"] is None
    assert observed["status"] == "unavailable"
    assert len(requirements_rows(ctx)) == 1
    assert any("binding_unavailable" in item for item in getattr(turn_ctx, "notices", []))


async def test_apply_resolution_failure_does_not_register_fallback_requirement(ctx):
    """目标版本过期：apply_resolution 抛错 → 明确失败，不回退登记、不静默新建。"""
    parent = seed_requirement(ctx, "旧要求：整理 A 报告。")
    resolver = FakeResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=parent,
            target_version=parent.version + 1,  # 过期版本
            corrections=("只改标题",),
        )
    )
    turn_ctx, observed = await run_turn(
        ctx, message="只改标题。", resolver=resolver
    )

    assert observed["binding"] is None
    assert observed["status"] == "unavailable"
    rows = requirements_rows(ctx)
    assert len(rows) == 1  # 只有原来的 parent，没有回退登记
    assert any("apply_resolution_failed" in item for item in getattr(turn_ctx, "notices", []))
    audits = resolution_audit_rows(ctx)
    assert audits and audits[-1]["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# 5) 解析失败：按新要求登记、标 unavailable、不扩大旧授权
# ---------------------------------------------------------------------------


async def test_resolution_failure_registers_new_requirement_without_expanding_old_grant(ctx):
    old = seed_requirement(ctx, "旧要求：整理 A 报告。")
    seed_allow_record(ctx, old, fingerprint="fp-old-grant")
    resolver = FakeResolver(error=RequirementResolutionUnavailable("模型不可用"))
    message = "把 B 报告的标题也改一下。"
    turn_ctx, observed = await run_turn(
        ctx, message=message, resolver=resolver, user_message_id="msg_real_3"
    )

    rows = requirements_rows(ctx)
    assert len(rows) == 2
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert binding.requirement_id != old.requirement_id
    new_requirement = ctx.requirements.by_id(binding.requirement_id)
    assert new_requirement is not None and new_requirement.text == message
    assert observed["status"] == "unavailable"
    provenance = json.loads(
        ctx.conn.execute(
            "SELECT provenance FROM user_requirements WHERE id = ?", (binding.requirement_id,)
        ).fetchone()["provenance"]
    )
    assert provenance["message_id"] == "msg_real_3"
    assert ctx.requirements.binding_for(old.requirement_id) is not None
    assert ctx.authorizations.reuse(
        requirement_id=binding.requirement_id,
        operation_kind="write",
        fingerprint="fp-old-grant",
    ) is None


# ---------------------------------------------------------------------------
# 6) 取消：不触发后续实际操作、终态仍正确
# ---------------------------------------------------------------------------


async def test_cancelled_during_resolution_keeps_terminal_state(ctx):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowResolver(FakeResolver):
        async def resolve(self, **kwargs):
            started.set()
            await release.wait()
            return make_outcome(RequirementRelation.NEW)

    resolver = SlowResolver()
    ctx.requirement_resolver = resolver
    topic_id = ctx.current_topic()
    turn_id = "turn_flow_cancel"
    executed = {"count": 0}

    async def execute(turn_ctx):
        executed["count"] += 1
        assert turn_ctx.cancelled is True
        return None

    ctx.turn_orchestrator.execute = execute
    turn_ctx = TurnContext(
        turn_id=turn_id, message="整理报告。", initial_topic=topic_id, current_topic=topic_id
    )
    task = asyncio.create_task(ctx._execute_turn(turn_ctx))
    await started.wait()
    turn_ctx.cancelled = True
    release.set()
    await task

    assert executed["count"] == 1
    binding = ctx.bindings.binding_for(turn_id)
    assert binding is not None and binding.status == "cancelled"


# ---------------------------------------------------------------------------
# 7) R9-H §6：历史结构化限制重新理解
# ---------------------------------------------------------------------------


async def test_legacy_requirement_is_reunderstood_and_persisted(ctx):
    shim = StructuredStateLedgerShim(ctx.requirements)
    ctx.requirements = shim
    extracted = (
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": "D:/work/周报.md"}],
            "reason": "只改标题",
        },
    )
    resolver = FakeResolver(
        outcome=make_outcome(RequirementRelation.NEW),
        extract_limits=extracted,
    )
    message = "把 D:/work/周报.md 的标题改成「本周进展」。"
    turn_ctx, observed = await run_turn(ctx, message=message, resolver=resolver)

    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert len(resolver.extract_calls) == 1
    call = resolver.extract_calls[0]
    assert call["text"] == message
    assert call["requirement_id"] == binding.requirement_id
    assert call["version"] == binding.version
    assert shim.set_calls, "结构化限制必须持久化"
    saved = shim.set_calls[-1]
    assert saved["state"] == "structured"
    assert saved["source"] == LIMIT_EXTRACTION_SCHEMA_VERSION
    assert saved["limits"] == extracted
    assert shim.state_of(binding.requirement_id) == "structured"
    assert observed["status"] == "ok"


async def test_legacy_extraction_failure_marks_unresolved_without_blocking_binding(ctx):
    shim = StructuredStateLedgerShim(ctx.requirements)
    ctx.requirements = shim
    resolver = FakeResolver(
        outcome=make_outcome(RequirementRelation.NEW),
        extract_error=RequirementResolutionUnavailable("模型超时"),
    )
    turn_ctx, observed = await run_turn(ctx, message="整理报告。", resolver=resolver)

    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    assert observed["status"] == "ok"
    assert shim.set_calls, "失败也必须持久化 unresolved 状态"
    saved = shim.set_calls[-1]
    assert saved["state"] == "unresolved"
    assert saved["source"].startswith("extract_limits_failed:")
    assert shim.state_of(binding.requirement_id) == "unresolved"
    assert any("历史用户限制" in item for item in getattr(turn_ctx, "notices", []))


async def test_legacy_text_is_passed_verbatim_not_keyword_parsed(ctx):
    shim = StructuredStateLedgerShim(ctx.requirements)
    ctx.requirements = shim
    old_text = "不要修改正文；如果只是标题就改「本周进展」，别动其他内容。"
    resolver = FakeResolver(
        outcome=make_outcome(RequirementRelation.NEW),
        extract_limits=(),
    )
    await run_turn(ctx, message=old_text, resolver=resolver)

    assert len(resolver.extract_calls) == 1
    assert resolver.extract_calls[0]["text"] == old_text
    assert "限制：" not in resolver.extract_calls[0]["text"]


async def test_structured_requirement_does_not_trigger_extra_extraction(ctx):
    shim = StructuredStateLedgerShim(ctx.requirements)
    ctx.requirements = shim
    resolver = FakeResolver(outcome=make_outcome(RequirementRelation.NEW), extract_limits=())
    turn_ctx, observed = await run_turn(ctx, message="整理报告。", resolver=resolver)
    binding = observed["binding"]
    assert isinstance(binding, RequirementBinding)
    shim.mark_state(binding.requirement_id, "structured")
    resolver2 = FakeResolver(
        outcome=make_outcome(
            RequirementRelation.AMEND,
            target=ctx.requirements.by_id(binding.requirement_id),
            target_version=binding.version,
            corrections=("再补一句",),
        ),
        extract_limits=(),
    )
    # 本轮模拟 C 分支：amend 子要求已是 structured，不再触发重新理解。
    shim.apply_resolution = lambda *a, **kw: shim._real.apply_resolution(*a, **kw)
    await run_turn(ctx, message="再补一句。", resolver=resolver2)
    assert resolver2.extract_calls == []


# ---------------------------------------------------------------------------
# 8) resolution.py：removed_limits 与 extract_limits 结构契约
# ---------------------------------------------------------------------------


def test_resolution_outcome_supports_removed_limits_only_for_amend():
    candidates = [{"requirement_id": "req_a", "version": 3, "status": "active"}]
    raw = json.dumps(
        {
            "relation": "amend",
            "target_requirement_id": "req_a",
            "target_version": 3,
            "corrections": ["正文也可以改了"],
            "limits": [],
            "removed_limits": [
                {"operations": ["write"], "objects": [], "reason": "解除只改标题的限制"}
            ],
            "reason": "用户明确解除标题限制",
        },
        ensure_ascii=False,
    )
    outcome = parse_resolution_outcome(raw)
    assert outcome.removed_limits[0]["reason"] == "解除只改标题的限制"
    validated = validate_resolution(outcome, candidates=candidates)
    assert validated.removed_limits == outcome.removed_limits
    assert validated.as_dict()["removed_limits"][0]["operations"] == ["write"]

    for relation in ("new", "withdraw"):
        data = {
            "relation": relation,
            "reason": "x",
            "removed_limits": [
                {"operations": ["write"], "objects": [], "reason": "解除限制"}
            ],
        }
        if relation == "withdraw":
            data.update(target_requirement_id="req_a", target_version=3)
        parsed = parse_resolution_outcome(json.dumps(data, ensure_ascii=False))
        with pytest.raises(RequirementResolutionUnavailable):
            validate_resolution(parsed, candidates=candidates)


def test_removed_limits_do_not_require_candidate_objects():
    """解除限制只是放宽：旧对象不必出现在当前候选里，只做结构校验。"""
    candidates = [{"requirement_id": "req_a", "version": 3, "status": "active"}]
    raw = json.dumps(
        {
            "relation": "amend",
            "target_requirement_id": "req_a",
            "target_version": 3,
            "corrections": ["之前限制的目录也放开"],
            "limits": [],
            "removed_limits": [
                {
                    "operations": ["write"],
                    "objects": [{"kind": "directory", "value": "D:/old-downloads"}],
                    "reason": "解除旧目录限制",
                }
            ],
            "reason": "用户明确解除",
        },
        ensure_ascii=False,
    )
    validated = validate_resolution(parse_resolution_outcome(raw), candidates=candidates)
    assert validated.removed_limits[0]["objects"][0]["value"] == "D:/old-downloads"


async def test_extract_limits_uses_single_tools_free_model_call():
    from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion
    from agent.security.resolution import ModelRequirementResolver

    class FakeAdapter(BaseAdapter):
        mode = AdapterMode.NATIVE
        model = "fake-r9h-extract"
        endpoint = "fake://r9h"

        def __init__(self, text):
            self.text = text
            self.requests = []

        async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
            self.requests.append({"messages": messages, "tools": tools})
            return Completion(message=ChatMessage(role="assistant", content=self.text))

    raw = json.dumps(
        {"limits": [{"operations": ["write"], "objects": [], "reason": "只改标题"}]},
        ensure_ascii=False,
    )
    adapter = FakeAdapter(raw)
    resolver = ModelRequirementResolver(adapter_factory=lambda: adapter, timeout_seconds=2.0)
    # 历史原话里的对象不在当前后端对象清单也应被接受（只收紧、不产生授权）。
    resolver.allowed_objects_provider = lambda: [
        {"kind": "path", "value": "D:/current-only.md"}
    ]
    limits = await resolver.extract_limits("只改标题，正文不要动。", requirement_id="req_old", version=2)
    assert limits == ({"operations": ["write"], "objects": [], "reason": "只改标题"},)
    assert len(adapter.requests) == 1
    assert adapter.requests[0]["tools"] == []
    user_text = adapter.requests[0]["messages"][1].content
    payload = json.loads(user_text[user_text.index("{", user_text.rindex("extract_input:")) :])
    assert payload["text"] == "只改标题，正文不要动。"
    assert payload["requirement_id"] == "req_old"
    assert payload["version"] == 2
    assert payload["prompt_version"] == LIMIT_EXTRACTION_SCHEMA_VERSION


def test_extract_limits_payload_and_render_use_user_words_only():
    payload = build_limit_extraction_payload(
        text="不要改正文；只改标题。", requirement_id="req_1", version=1
    )
    assert payload["text"] == "不要改正文；只改标题。"
    messages = render_limit_extraction_messages(
        text="不要改正文；只改标题。", requirement_id="req_1", version=1
    )
    assert messages[0].role == "system"
    assert "限制" in messages[0].content
    assert "不要改正文；只改标题。" in messages[1].content


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"limits": "not-a-list"}),
        json.dumps({"limits": [{"operations": ["keyword_allow"], "objects": []}]}),
        json.dumps({"limits": [{"operations": [], "objects": []}]}),
    ],
)
def test_parse_extracted_limits_rejects_illegal_structure(raw: str):
    with pytest.raises(RequirementResolutionUnavailable):
        parse_extracted_limits(raw)


async def test_real_resolver_extract_limits_is_wired_through_execute_turn(ctx):
    """真实 ModelRequirementResolver + FakeAdapter：app 的历史限制接线不依赖替身。"""
    from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion
    from agent.security.resolution import ModelRequirementResolver

    class SequenceAdapter(BaseAdapter):
        mode = AdapterMode.NATIVE
        model = "fake-r9h-sequence"
        endpoint = "fake://r9h"

        def __init__(self, responses):
            self.responses = list(responses)
            self.requests = []

        async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
            self.requests.append({"messages": messages, "tools": tools})
            return Completion(
                message=ChatMessage(role="assistant", content=self.responses.pop(0))
            )

    resolution_json = json.dumps(
        {
            "relation": "new",
            "target_requirement_id": None,
            "target_version": None,
            "corrections": [],
            "limits": [],
            "removed_limits": [],
            "reason": "新任务",
        },
        ensure_ascii=False,
    )
    limits_json = json.dumps(
        {
            "limits": [
                {
                    "operations": ["write"],
                    "objects": [{"kind": "path", "value": "D:/work/周报.md"}],
                    "reason": "只改标题",
                }
            ]
        },
        ensure_ascii=False,
    )
    adapter = SequenceAdapter([resolution_json, limits_json])
    shim = StructuredStateLedgerShim(ctx.requirements)
    ctx.requirements = shim
    ctx.requirement_resolver = ModelRequirementResolver(
        adapter_factory=lambda: adapter, timeout_seconds=5.0
    )
    message = "只改 D:/work/周报.md 的标题。"
    turn_ctx, observed = await run_turn(ctx, message=message)

    assert isinstance(observed["binding"], RequirementBinding)
    assert len(adapter.requests) == 2
    assert all(item["tools"] == [] for item in adapter.requests)
    assert shim.set_calls and shim.set_calls[-1]["state"] == "structured"
    # R9-H2：历史恢复改走完整链上下文，source 记录链提示词/摘要/目标版本。
    source = shim.set_calls[-1]["source"]
    assert source.startswith(LIMIT_EXTRACTION_CHAIN_SCHEMA_VERSION), source
    assert ";chain=" in source and ";version=" in source, source
    assert shim.set_calls[-1]["limits"][0]["reason"] == "只改标题"
    extraction_text = adapter.requests[1]["messages"][1].content
    assert message in extraction_text
