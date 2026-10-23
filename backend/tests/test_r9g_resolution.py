# -*- coding: utf-8 -*-
"""R9-G W1：用户要求解析（ModelRequirementResolver / validate_resolution / apply_resolution）。

契约 docs/r9g-contracts.md §1、§3：
* 每个用户 turn 开始时用主模型判断 new / amend / withdraw，并抽取结构化限制；
* 后端核对 target 存在、active、版本一致后才落实；失败 → RequirementResolutionUnavailable，
  按新要求继续并写失败审计，不做关键词兜底；
* 落实结果写 requirement_resolutions 审计；修正生成 version=parent+1 的子要求并让父要求失效。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion
from agent.prompts import REQUIREMENT_RESOLUTION_SCHEMA_VERSION
from agent.security.requirements import (
    RequirementLedger,
    RequirementVersionMismatch,
    RESOLUTION_OUTCOME_APPLIED,
    RESOLUTION_OUTCOME_REJECTED,
    RESOLUTION_OUTCOME_UNAVAILABLE,
    STATUS_SUPERSEDED,
    STATUS_WITHDRAWN,
)
from agent.security.resolution import (
    ModelRequirementResolver,
    RequirementRelation,
    RequirementResolutionUnavailable,
    ResolutionOutcome,
    parse_resolution_outcome,
    render_resolution_messages,
    validate_resolution,
)

_NOW_TEXT = "2026-10-23T00:00:00+00:00"


class FakeResolutionAdapter(BaseAdapter):
    mode = AdapterMode.NATIVE
    model = "fake-r9g-resolver"
    endpoint = "fake://r9g-resolution"

    def __init__(self, responses, *, delay: float = 0.0, error: BaseException | None = None) -> None:
        self.responses = list(responses)
        self.delay = delay
        self.error = error
        self.requests: list[dict] = []

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        self.requests.append({"messages": messages, "tools": tools})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        text = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return Completion(message=ChatMessage(role="assistant", content=text))


def make_resolver(adapter, *, timeout: float = 5.0, allowed_objects_provider=None) -> ModelRequirementResolver:
    return ModelRequirementResolver(
        adapter_factory=lambda: adapter,
        timeout_seconds=timeout,
        allowed_objects_provider=allowed_objects_provider,
    )


def resolution_json(
    relation: str = "new",
    *,
    target_requirement_id=None,
    target_version=None,
    corrections=(),
    limits=(),
    reason="依据用户当前消息的语义判断",
) -> str:
    return json.dumps(
        {
            "relation": relation,
            "target_requirement_id": target_requirement_id,
            "target_version": target_version,
            "corrections": list(corrections),
            "limits": list(limits),
            "reason": reason,
        },
        ensure_ascii=False,
    )


def candidates_of(ledger: RequirementLedger, *, limit: int = 12) -> list[dict]:
    return ledger.candidates(limit=limit)


async def resolve_with(adapter, message: str, candidates: list[dict], **kwargs) -> ResolutionOutcome:
    return await make_resolver(adapter, **kwargs).resolve(
        message=message,
        message_id="msg_1",
        turn_id="turn_1",
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# 解析输入与调用边界
# ---------------------------------------------------------------------------


async def test_resolution_is_single_tools_free_call_with_bounded_candidates():
    candidates = [
        {
            "requirement_id": "req_a",
            "version": 2,
            "text_excerpt": "整理 D:/work/报告.md",
            "created_at": _NOW_TEXT,
            "updated_at": _NOW_TEXT,
            "status": "active",
        }
    ]
    adapter = FakeResolutionAdapter([resolution_json(corrections=["只改标题"], limits=[])])
    outcome = await resolve_with(adapter, "只改标题就好", candidates)
    assert outcome.relation == RequirementRelation.NEW
    assert outcome.model == "fake-r9g-resolver"
    assert outcome.prompt_version == REQUIREMENT_RESOLUTION_SCHEMA_VERSION
    assert len(adapter.requests) == 1
    assert adapter.requests[0]["tools"] == []
    user_text = adapter.requests[0]["messages"][1].content
    payload = json.loads(user_text[user_text.index("{", user_text.rindex("resolution_input:")) :])
    assert payload["prompt_version"] == REQUIREMENT_RESOLUTION_SCHEMA_VERSION
    assert payload["message"]["text"] == "只改标题就好"
    assert payload["candidates"][0]["requirement_id"] == "req_a"
    assert payload["candidates"][0]["version"] == 2
    assert payload["related_context"] == []


def test_render_messages_uses_prompt_single_source():
    messages = render_resolution_messages(
        message="继续刚才那个任务",
        message_id="m1",
        turn_id="t1",
        candidates=[{"requirement_id": "req_a", "version": 1, "text_excerpt": "整理报告"}],
    )
    assert messages[0].role == "system" and "语义" in messages[0].content
    assert REQUIREMENT_RESOLUTION_SCHEMA_VERSION in messages[1].content
    assert "resolution_input" in messages[1].content


# ---------------------------------------------------------------------------
# new：新建要求 + 审计
# ---------------------------------------------------------------------------


async def test_new_resolution_creates_version_one_requirement_and_audit(db_conn):
    ledger = RequirementLedger(db_conn)
    adapter = FakeResolutionAdapter([resolution_json()])
    outcome = await resolve_with(adapter, "帮我把 D:/work/报告.md 整理一下", [])
    applied = ledger.apply_resolution(
        outcome, message_id="msg_1", turn_id="turn_1", topic_id="topic_1", message="帮我把 D:/work/报告.md 整理一下"
    )
    assert applied.relation == "new"
    assert applied.requirement is not None
    assert applied.requirement.version == 1
    assert applied.requirement.text == "帮我把 D:/work/报告.md 整理一下"  # 用户原话不改写
    assert ledger.binding_for(applied.requirement.requirement_id).version == 1

    rows = ledger.resolutions_for(target_requirement_id=None)
    assert rows and rows[0]["relation"] == "new"
    assert rows[0]["outcome"] == RESOLUTION_OUTCOME_APPLIED
    assert rows[0]["model"] == "fake-r9g-resolver"
    assert rows[0]["prompt_version"] == REQUIREMENT_RESOLUTION_SCHEMA_VERSION
    assert rows[0]["message_id"] == "msg_1"
    assert rows[0]["turn_id"] == "turn_1"


async def test_new_resolution_persists_structured_limits_as_readable_text(db_conn):
    ledger = RequirementLedger(db_conn)
    limits = [
        {
            "operations": ["write"],
            "objects": [{"kind": "path", "value": "D:/work/报告.md"}],
            "reason": "只改标题",
        }
    ]
    adapter = FakeResolutionAdapter([resolution_json(limits=limits)])
    outcome = await resolve_with(adapter, "只改标题", [])
    applied = ledger.apply_resolution(outcome, message_id="m1", turn_id="t1", message="只改标题")
    assert applied.limits == tuple(dict(item) for item in limits)
    stored = ledger.by_id(applied.requirement.requirement_id)
    assert any("限制：" in item and "只改标题" in item for item in stored.limits)


# ---------------------------------------------------------------------------
# amend：目标/版本核对 + version=parent+1 + 父要求失效
# ---------------------------------------------------------------------------


async def test_amend_requires_candidate_target_and_matching_version(db_conn):
    ledger = RequirementLedger(db_conn)
    parent = ledger.record_user_message(text="整理 D:/work/报告.md", message_id="m1", turn_id="t1", topic_id="top")
    candidates = candidates_of(ledger)

    adapter = FakeResolutionAdapter(
        [resolution_json("amend", target_requirement_id=parent.requirement_id, target_version=parent.version,
                         corrections=["只改标题"])]
    )
    outcome = await resolve_with(adapter, "补充：只改标题", candidates)
    assert outcome.relation == RequirementRelation.AMEND
    assert outcome.target_requirement_id == parent.requirement_id
    assert outcome.target_version == 1

    applied = ledger.apply_resolution(
        outcome,
        message_id="m2",
        turn_id="t2",
        message="补充：只改标题",
        latency_ms=12,
    )
    assert applied.requirement is not None
    assert applied.requirement.version == 2 == parent.version + 1
    assert applied.requirement.parent_id == parent.requirement_id
    assert applied.requirement.text == "补充：只改标题"
    assert ledger.by_id(parent.requirement_id).status == STATUS_SUPERSEDED
    assert ledger.binding_for(parent.requirement_id) is None

    rows = ledger.resolutions_for(target_requirement_id=parent.requirement_id)
    assert rows and rows[0]["outcome"] == RESOLUTION_OUTCOME_APPLIED
    assert rows[0]["relation"] == "amend"
    assert rows[0]["latency_ms"] == 12


async def test_amend_with_stale_version_is_rejected_and_audited_without_new_requirement(db_conn):
    ledger = RequirementLedger(db_conn)
    parent = ledger.record_user_message(text="整理报告", message_id="m1", turn_id="t1", topic_id="top")
    stale_candidates = candidates_of(ledger)  # 模型看到的是 version=1

    # 并发场景：另一个 turn 已经修正了同一要求（version=2、父要求 superseded）
    ledger.add_correction(parent.requirement_id, "只改标题", message_id="m2")
    before = ledger.candidates(limit=50)

    adapter = FakeResolutionAdapter(
        [resolution_json("amend", target_requirement_id=parent.requirement_id, target_version=1,
                         corrections=["改成蓝色"])]
    )
    outcome = await resolve_with(adapter, "改成蓝色", stale_candidates)
    assert outcome.target_version == 1
    with pytest.raises((RequirementVersionMismatch, ValueError)):
        ledger.apply_resolution(outcome, message_id="m3", turn_id="t3", message="改成蓝色")

    rows = ledger.resolutions_for(target_requirement_id=parent.requirement_id)
    assert any(row["outcome"] == RESOLUTION_OUTCOME_REJECTED for row in rows)
    # 失败不静默新建：active 要求数量不变
    assert len(ledger.candidates(limit=50)) == len(before)


async def test_withdraw_resolution_keeps_history_and_audit(db_conn):
    ledger = RequirementLedger(db_conn)
    target = ledger.record_user_message(text="整理报告并发送", message_id="m1", turn_id="t1", topic_id="top")
    candidates = candidates_of(ledger)
    adapter = FakeResolutionAdapter(
        [resolution_json("withdraw", target_requirement_id=target.requirement_id, target_version=1)]
    )
    outcome = await resolve_with(adapter, "不用了，取消刚才那个任务", candidates)
    applied = ledger.apply_resolution(
        outcome, message_id="m2", turn_id="t2", message="不用了，取消刚才那个任务"
    )
    assert applied.relation == "withdraw"
    stored = ledger.by_id(target.requirement_id)
    assert stored is not None and stored.status == STATUS_WITHDRAWN
    assert ledger.binding_for(target.requirement_id) is None
    rows = ledger.resolutions_for(target_requirement_id=target.requirement_id)
    assert rows and rows[0]["outcome"] == RESOLUTION_OUTCOME_APPLIED
    assert rows[0]["relation"] == "withdraw"


# ---------------------------------------------------------------------------
# 校验失败路径（不关键词兜底、不静默新建）
# ---------------------------------------------------------------------------


def test_validate_rejects_target_outside_candidates():
    outcome = parse_resolution_outcome(
        resolution_json("amend", target_requirement_id="req_other", target_version=1, corrections=["x"])
    )
    with pytest.raises(RequirementResolutionUnavailable) as info:
        validate_resolution(outcome, candidates=[{"requirement_id": "req_a", "version": 1}])
    assert "不在有限候选内" in str(info.value)


def test_validate_rejects_version_mismatch_and_new_with_target():
    candidates = [{"requirement_id": "req_a", "version": 3, "status": "active"}]
    stale = parse_resolution_outcome(
        resolution_json("amend", target_requirement_id="req_a", target_version=1, corrections=["x"])
    )
    with pytest.raises(RequirementResolutionUnavailable) as info:
        validate_resolution(stale, candidates=candidates)
    assert "版本" in str(info.value)
    bad_new = parse_resolution_outcome(
        resolution_json("new", target_requirement_id="req_a", target_version=3)
    )
    with pytest.raises(RequirementResolutionUnavailable):
        validate_resolution(bad_new, candidates=candidates)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({}),
        json.dumps({"relation": "replace"}),
        json.dumps({"relation": "new", "reason": ""}),
        json.dumps(
            {
                "relation": "new",
                "reason": "x",
                "limits": [{"operations": ["keyword_allow"], "objects": [], "reason": "x"}],
            }
        ),
        json.dumps(
            {
                "relation": "new",
                "reason": "x",
                "limits": "not-a-list",
            }
        ),
    ],
)
def test_parse_or_validate_rejects_illegal_resolution(raw: str):
    with pytest.raises(RequirementResolutionUnavailable):
        outcome = parse_resolution_outcome(raw)
        validate_resolution(outcome, candidates=[{"requirement_id": "req_a", "version": 1}])


def test_validate_limits_objects_must_be_backend_resolvable_when_provided():
    raw = resolution_json(
        limits=[
            {
                "operations": ["write"],
                "objects": [{"kind": "path", "value": "D:/work/unknown.md"}],
                "reason": "只改这个文件",
            }
        ]
    )
    outcome = parse_resolution_outcome(raw)
    with pytest.raises(RequirementResolutionUnavailable) as info:
        validate_resolution(
            outcome,
            candidates=[{"requirement_id": "req_a", "version": 1}],
            allowed_objects=[{"kind": "path", "value": "D:/work/known.md"}],
        )
    assert "可解析对象之外" in str(info.value)
    # 对象清单里有 → 通过
    validated = validate_resolution(
        outcome,
        candidates=[{"requirement_id": "req_a", "version": 1}],
        allowed_objects=[{"kind": "path", "value": "D:/work/unknown.md"}],
    )
    assert validated.limits[0]["objects"][0]["value"] == "D:/work/unknown.md"


def test_validate_limits_object_kind_may_differ_when_same_backend_object():
    """同一个后端对象被模型写成 path/directory 之一，不应因类别措辞不同而不可用。"""
    outcome = parse_resolution_outcome(
        resolution_json(
            limits=[
                {
                    "operations": ["execute"],
                    "objects": [{"kind": "path", "value": "D:/proj"}],
                    "reason": "只跑这个目录里的测试",
                }
            ]
        )
    )
    validated = validate_resolution(
        outcome,
        candidates=[{"requirement_id": "req_a", "version": 1}],
        allowed_objects=[{"kind": "directory", "value": "D:/proj"}],
    )
    assert validated.limits[0]["objects"][0]["value"] == "D:/proj"


def test_validate_rejects_invented_limit_object_kind():
    outcome = parse_resolution_outcome(
        resolution_json(
            limits=[
                {
                    "operations": ["write"],
                    "objects": [{"kind": "document_section", "value": "第 3 段"}],
                    "reason": "只改第 3 段",
                }
            ]
        )
    )
    with pytest.raises(RequirementResolutionUnavailable):
        validate_resolution(outcome, candidates=[{"requirement_id": "req_a", "version": 1}])


def test_apply_rejects_unknown_or_inactive_target_without_creating_requirement(db_conn):
    ledger = RequirementLedger(db_conn)
    withdrawn = ledger.record_user_message(text="旧任务", message_id="m1", turn_id="t1", topic_id="top")
    ledger.withdraw(withdrawn.requirement_id)
    before = ledger.candidates(limit=50)
    outcome = ResolutionOutcome(
        relation=RequirementRelation.WITHDRAW,
        target_requirement_id=withdrawn.requirement_id,
        corrections=(),
        limits=(),
        reason="取消旧任务",
        model="fake",
        prompt_version="v1",
        target_version=1,
    )
    with pytest.raises(ValueError):
        ledger.apply_resolution(outcome, message_id="m2", turn_id="t2")
    assert len(ledger.candidates(limit=50)) == len(before)
    rows = ledger.resolutions_for(target_requirement_id=withdrawn.requirement_id)
    assert any(row["outcome"] == RESOLUTION_OUTCOME_REJECTED for row in rows)


async def test_resolver_failure_paths_never_fall_back_to_keywords():
    candidates = [{"requirement_id": "req_a", "version": 1, "text_excerpt": "整理报告"}]
    no_adapter = ModelRequirementResolver(adapter_factory=lambda: None, timeout_seconds=1.0)
    with pytest.raises(RequirementResolutionUnavailable) as info:
        await no_adapter.resolve(message="取消吧", message_id="m", turn_id="t", candidates=candidates)
    assert "主模型" in str(info.value)

    with pytest.raises(RequirementResolutionUnavailable):
        await resolve_with(FakeResolutionAdapter(["not json"]), "取消吧", candidates)

    with pytest.raises(RequirementResolutionUnavailable) as timeout_info:
        await resolve_with(FakeResolutionAdapter([resolution_json()], delay=0.2), "继续", candidates, timeout=0.01)
    assert "超时" in str(timeout_info.value)

    with pytest.raises(RequirementResolutionUnavailable):
        await resolve_with(FakeResolutionAdapter([resolution_json()], error=RuntimeError("network down")), "继续", candidates)


def test_record_resolution_failure_writes_unavailable_audit(db_conn):
    ledger = RequirementLedger(db_conn)
    resolution_id = ledger.record_resolution_failure(
        error="模型超时",
        message_id="m9",
        turn_id="t9",
        model="fake",
        prompt_version="v1",
        latency_ms=20000,
    )
    assert resolution_id.startswith("reqres_")
    rows = ledger.resolutions_for(limit=10)
    assert rows[0]["outcome"] == RESOLUTION_OUTCOME_UNAVAILABLE
    assert rows[0]["message_id"] == "m9"
    assert "超时" in (rows[0]["error"] or "")
