# -*- coding: utf-8 -*-
"""D 独立契约测试：阶段二冻结类型 / 提示词 / 校验入口（契约 §12.2）。

口径：不检查实现内部函数，只检查冻结面（提示词键与硬规则文字、pydantic 形状、
校验 / schema 入口、A/B/C 交接数据结构、C 的读取模型字段）。

运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p2_d_frozen_models_prompts.py -q

基线说明：本文件在 bcce3a2a（冻结提交）上即可全部通过；它防止整合期间
冻结面被改写（D 阶段 2 静态审阅的自动化部分）。
"""

from __future__ import annotations

import json

import pytest

CLAUSE = "契约 §12.2"


# ---------------------------------------------------------------------------
# 提示词：固定文案只在 prompts.py，且包含冻结稿的硬规则（不做全文快照）
# ---------------------------------------------------------------------------


def test_maintenance_prompts_exist_and_are_non_empty():
    """§12.2：PROMPTS 必须含 MAINTENANCE_SYSTEM / MAINTENANCE_ANSWER_SYSTEM。"""
    from agent import prompts

    for key in ("MAINTENANCE_SYSTEM", "MAINTENANCE_ANSWER_SYSTEM"):
        assert key in prompts.PROMPTS, f"{CLAUSE}：PROMPTS 缺少冻结键 {key}"
        text = prompts.PROMPTS[key]
        assert isinstance(text, str) and text.strip(), f"{CLAUSE}：{key} 为空"
        assert text == text.strip(), f"{CLAUSE}：{key} 首尾不应有多余空白（冻结稿是 strip 后的）"


def test_maintenance_system_contains_hard_rules():
    """§12.4：维护动作硬规则（merge 至少两个 / normalize 一个 / ask 不改知识）。

    只断言冻结稿的关键词与硬规则句子，不复制全文（避免提示词全文快照测试）。
    """
    from agent.prompts import PROMPTS

    text = PROMPTS["MAINTENANCE_SYSTEM"]
    for phrase in (
        "允许 merge、normalize 和 ask",
        "merge 用于含义相同的重复知识",
        "normalize 仅用于不改变原意的格式整理",
        "涉及实质认识变化、有效性变化或无法解释的冲突时使用 ask",
        "我不会仅因内容较旧、长期未出现或存在否定词就撤销知识",
        "merge 和 normalize 提供非空 new_content，不提供 question",
        "已经等待回答的同一事项不重复提出",
        "没有需要处理的内容时返回空 candidates",
    ):
        assert phrase in text, f"{CLAUSE}：MAINTENANCE_SYSTEM 缺少冻结硬规则「{phrase}」"


def test_maintenance_answer_system_contains_hard_rules():
    """§12.4：独立回答硬规则（不改主对话 / 不自行批准 / 版本由程序检查）。"""
    from agent.prompts import PROMPTS

    text = PROMPTS["MAINTENANCE_ANSWER_SYSTEM"]
    for phrase in (
        "只根据当前事项、涉及的知识版本、必要来源和用户本次回答处理",
        "用户回答明确解决问题时，提出对应 revise 或 revoke",
        "使用 needs_clarification，并提出一个具体的后续问题，不同时应用修改",
        "使用 deferred，保留事项，不把它记成已经解决",
        "我不能自行输出“已批准”或“已验证”来替代程序权限和版本检查",
        "本次交流属于独立记忆事项，不写入主对话",
    ):
        assert phrase in text, f"{CLAUSE}：MAINTENANCE_ANSWER_SYSTEM 缺少冻结硬规则「{phrase}」"


# ---------------------------------------------------------------------------
# 输出模型：extra=forbid / 动作形状在本地强制执行
# ---------------------------------------------------------------------------


def test_maintenance_models_forbid_extra_fields():
    """§12.2/§12.4：模型输出类型 extra=forbid（不得靠多写字段绕过本地规则）。"""
    from pydantic import ValidationError

    from agent.maintenance.models import (
        MaintenanceCandidate,
        MaintenanceOutput,
        ResolutionChange,
        ResolutionOutput,
    )

    with pytest.raises(ValidationError):
        MaintenanceOutput.model_validate({"candidates": [], "approved": True})
    with pytest.raises(ValidationError):
        MaintenanceCandidate.model_validate(
            {"action": "ask", "question": "?", "approved_by_model": True}
        )
    with pytest.raises(ValidationError):
        ResolutionOutput.model_validate(
            {"resolution": "no_change", "changes": [], "verified": True}
        )
    with pytest.raises(ValidationError):
        ResolutionChange.model_validate(
            {"action": "revoke", "knowledge_id": "kn_x", "already_approved": True}
        )


@pytest.mark.parametrize(
    "candidate",
    [
        {"action": "merge", "knowledge_ids": ["kn_a"], "new_content": "x"},
        {"action": "merge", "knowledge_ids": ["kn_a", "kn_b"], "new_content": "  "},
        {"action": "merge", "knowledge_ids": ["kn_a", "kn_b"], "new_content": "x", "question": "?"},
        {"action": "merge", "knowledge_ids": ["kn_a", "kn_a"], "new_content": "x"},
        {"action": "normalize", "knowledge_ids": ["kn_a", "kn_b"], "new_content": "x"},
        {"action": "normalize", "knowledge_ids": ["kn_a"], "new_content": ""},
        {"action": "ask", "knowledge_ids": ["kn_a"]},
        {"action": "ask", "knowledge_ids": ["kn_a"], "question": "?", "new_content": "x"},
        {"action": "expire", "knowledge_ids": ["kn_a"]},
        {"action": "revoke", "knowledge_ids": ["kn_a"]},
    ],
)
def test_maintenance_candidate_rejects_illegal_shapes(candidate):
    """§12.4：merge >=2 个不同 ID / normalize 恰好 1 个 / ask 有 question 且不改知识。

    旧动作 expire / revoke 不是维护候选的合法动作（不得作为新维护动作的回退）。
    """
    from pydantic import ValidationError

    from agent.maintenance.models import MaintenanceCandidate

    with pytest.raises(ValidationError):
        MaintenanceCandidate.model_validate(candidate)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "revise", "knowledge_id": "kn_a", "new_content": ""},
        {"action": "revise", "knowledge_id": "kn_a"},
        {"action": "revoke", "knowledge_id": "kn_a", "new_content": "不应有内容"},
        {"action": "expire", "knowledge_id": "kn_a"},
        {"action": "delete", "knowledge_id": "kn_a"},
    ],
)
def test_resolution_change_rejects_illegal_shapes(change):
    """§12.4：revise 要求非空 new_content；revoke 要求 new_content 为空。"""
    from pydantic import ValidationError

    from agent.maintenance.models import ResolutionChange

    with pytest.raises(ValidationError):
        ResolutionChange.model_validate(change)


@pytest.mark.parametrize(
    "payload",
    [
        {"resolution": "needs_clarification"},
        {"resolution": "needs_clarification", "question": "?", "changes": [
            {"action": "revise", "knowledge_id": "kn_a", "new_content": "x"}]},
        {"resolution": "deferred", "changes": [
            {"action": "revoke", "knowledge_id": "kn_a"}]},
        {"resolution": "no_change", "question": "?"},
        {"resolution": "resolved", "changes": []},
        {"resolution": "resolved", "question": "?", "changes": [
            {"action": "revise", "knowledge_id": "kn_a", "new_content": "x"}]},
    ],
)
def test_resolution_output_rejects_illegal_shapes(payload):
    """§12.4：needs_clarification 必须提问且不同时改；deferred/no_change 不改；
    resolved 必须至少一条 revise/revoke。"""
    from pydantic import ValidationError

    from agent.maintenance.models import ResolutionOutput

    with pytest.raises(ValidationError):
        ResolutionOutput.model_validate(payload)


def test_limits_and_source_keys_are_frozen():
    """§12.2：上下限与来源键冻结；维护自身来源不计入变化计数。"""
    from agent.maintenance import models as m

    assert m.MAX_CANDIDATES_PER_RUN == 20
    assert m.MAX_BATCH_CHANGES == 50
    assert m.MAX_KNOWLEDGE_CHARS == 2000
    assert m.MAX_KNOWLEDGE_IDS == 8
    assert m.MAX_EVIDENCE_IDS == 20
    assert m.MAX_QUESTION_CHARS == 500
    assert dict(m.MAINTENANCE_LIMITS) == {
        "max_candidates": 20,
        "max_batch_changes": 50,
        "max_knowledge_chars": 2000,
        "max_knowledge_ids": 8,
        "max_evidence_ids": 20,
        "max_question_chars": 500,
    }
    assert set(m.MAINTENANCE_SOURCE_KEYS) == {
        "background_extraction",
        "maintenance_merge",
        "maintenance_normalize",
        "maintenance_restore",
    }
    assert set(m.MAINTENANCE_ORIGINS) == {
        "maintenance_merge",
        "maintenance_normalize",
        "maintenance_restore",
    }, "§12.3：只有 maintenance_* 不计入变化计数（避免维护自身循环触发）"


def test_validation_entries_share_the_same_models():
    """§12.2：本地校验与展示给模型的 schema 同源。"""
    from agent.tools.contracts import json_schema_for

    from agent.maintenance import models as m

    good = json.dumps(
        {"candidates": [
            {"action": "merge", "knowledge_ids": ["kn_a", "kn_b"], "new_content": "合并"}]},
        ensure_ascii=False,
    )
    output, errors = m.validate_maintenance_output(good)
    assert errors == [] and output is not None
    assert m.maintenance_output_schema() == json_schema_for(m.MaintenanceOutput)

    resolution, errors = m.validate_resolution_output(
        json.dumps({"resolution": "no_change", "changes": []})
    )
    assert errors == [] and resolution is not None
    assert m.resolution_output_schema() == json_schema_for(m.ResolutionOutput)

    # 带 Markdown 围栏 / 非对象 / 非法 JSON 必须有可读错误（供一次有依据的修复）
    fence = "\u0060\u0060\u0060json\n{}\n\u0060\u0060\u0060"
    for text in (fence, "[]", "{", ""):
        parsed, errs = m.validate_maintenance_output(text)
        assert parsed is None and errs, f"非法输出未被拒绝：{text!r}"


def test_handoff_dataclasses_are_frozen():
    """§12.2：A→B / B→A / B→C 交接结构字段冻结且不可变。"""
    import dataclasses

    from agent.maintenance import models as m

    snapshot = m.MaintenanceSnapshot(
        batch_seq=1,
        trigger=m.MaintenanceTrigger.CHANGE_COUNT,
        change_seq_max=10,
        pending_change_count=2,
        waiting_turns=0,
        change_ids=(1, 2),
        knowledge_ids=("kn_a", "kn_b"),
    )
    assert dataclasses.is_dataclass(snapshot) and snapshot.__dataclass_params__.frozen
    assert set(snapshot.limits) == set(m.MAINTENANCE_LIMITS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.batch_seq = 2  # type: ignore[misc]

    outcome = m.MaintenanceRunOutcome(ok=True, status="completed")
    assert dataclasses.is_dataclass(outcome) and outcome.__dataclass_params__.frozen
    answer = m.MaintenanceAnswerOutcome(ok=True, status="processed", item_id="it_1")
    assert dataclasses.is_dataclass(answer) and answer.__dataclass_params__.frozen


def test_pending_read_models_field_names_are_frozen():
    """§12.5：C 的读取模型字段名冻结（API / 界面按它读）。"""
    from pydantic import ValidationError

    from agent.maintenance.models import PendingItemView, PendingKnowledgeRef

    assert set(PendingKnowledgeRef.model_fields) == {
        "id", "version", "content", "category", "scope", "epistemic_status",
        "source", "state",
    }
    assert set(PendingItemView.model_fields) == {
        "id", "kind", "status", "handler", "title", "question", "knowledge",
        "before", "after", "evidence", "actions", "created_at", "updated_at",
        "batch_id", "error",
    }
    item = PendingItemView(
        id="it_1",
        kind="maintenance_question",
        status="awaiting_answer",
        handler="maintenance",
        title="问题",
    )
    assert item.kind.value == "maintenance_question"
    assert item.handler == "maintenance"
    with pytest.raises(ValidationError):
        PendingItemView(
            id="it_1", kind="maintenance_question", status="awaiting_answer",
            handler="maintenance", title="问题", invented="x",
        )
