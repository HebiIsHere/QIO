# -*- coding: utf-8 -*-
"""D 独立契约测试：知识 / 实体契约（docs/qio-refactor-contract.md §6、§7）。

覆盖：新工具登记与 pydantic schema 同源、参数严格校验（extra/enum）、主对话新增
不能绕过审批、待审批不进注入面、明确纠正当场生效、后台提炼输出新字段与固定提示词。

口径：契约措辞 → 可验证断言；模型调用全部 fake provider。阶段 1（C/A 的实现未合并）
允许失败，失败消息写明契约条款。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_knowledge_contract.py -q
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from agent import prompts
from agent.adapters.base import ToolCall
from agent.adapters.fake import FakeStreamAdapter, StreamScript
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.knowledge.extraction import (
    KnowledgeExtraction,
    extract_knowledge_candidates_outcome,
    validate_knowledge,
)
from agent.knowledge.evidence import EvidenceCatalog
from agent.knowledge.lifecycle import KnowledgeService
from agent.memory.contract import FragmentSummary
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

ANSWER = "[[QIO:ANSWER]]"
CATEGORY_VALUES = {"user_profile", "agent_self", "goal", "general_fact", "tool_experience"}
EPISTEMIC_VALUES = {"user_explicit", "shared_understanding", "qio_observed", "qio_inference"}
ATTACH_VALUES = {"user", "topic", "entity"}


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "knowledge.db")
    apply_migrations(conn)
    application = AppContext(Settings(data_dir=tmp_path / "data"), conn, EventBus())
    application.credentials._kr = MemoryKeyring()
    return application


def _tool(ctx: AppContext, name: str):
    return ctx.registry._tools.get(name)  # noqa: SLF001 - 需要拿未注入叙事字段的原始 schema


def _spec(ctx: AppContext, name: str):
    return next((spec for spec in ctx.registry.specs() if spec.name == name), None)


def _prompts_string_values() -> set[str]:
    """prompts.py 暴露的全部固定文本（模块字符串常量 + 四个冻结映射的值）。

    说明：源码里相邻字符串字面量会被 Python 拼接，运行时正文未必能按原文在源码中
    连续匹配；因此判据用「运行时值来自 prompts.py 的固定常量」而不是源码 substring。
    """
    values: set[str] = set()
    for name in dir(prompts):
        value = getattr(prompts, name)
        if isinstance(value, str):
            values.add(value)
    for mapping in (
        prompts.PROMPTS,
        prompts.TOOL_DESCRIPTIONS,
        prompts.ARGUMENT_DESCRIPTIONS,
        prompts.CONTEXT_LABELS,
    ):
        values.update(value for value in mapping.values() if isinstance(value, str))
    return values


def _module_string_literals(path: str | None) -> set[str]:
    """模块源码里出现的字符串字面量（AST 会折叠相邻字面量，便于查正文副本）。"""
    if not path:
        return set()
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _tool_module_source_path(ctx: AppContext, name: str) -> str | None:
    tool = ctx.registry._tools.get(name)  # noqa: SLF001 - 只读诊断
    module = inspect.getmodule(tool) if tool is not None else None
    return getattr(module, "__file__", None)


def _walk_enums(node) -> list[list]:
    found = []
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            found.append(enum)
        for value in node.values():
            found.extend(_walk_enums(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_enums(value))
    return found


def _has_additional_properties_false(node) -> bool:
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            return True
        return any(_has_additional_properties_false(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_additional_properties_false(value) for value in node)
    return False


def _knowledge_states(conn, content: str) -> list[str]:
    rows = conn.execute(
        "SELECT state FROM knowledge WHERE content = ?", (content,)
    ).fetchall()
    return [str(row["state"]) for row in rows]


def _create_active_knowledge(ctx: AppContext, content: str):
    service = KnowledgeService(ctx.conn)
    item = service.create(category="general_fact", content=content)
    service.submit(item.id)
    service.verify(item.id, verified_by="system")
    return service.activate(item.id)


def _fake_summary() -> FragmentSummary:
    return FragmentSummary(
        title="摘要标题", summary="摘要正文", entities=[], keywords=[]
    )


# ---------------------------------------------------------------------------
# 新工具登记 / 冻结描述 / pydantic schema（§6.1、§6.3、§6.4、§7）
# ---------------------------------------------------------------------------


def test_propose_knowledge_and_create_entity_are_registered(ctx: AppContext):
    """§6：主对话必须真的有 propose_knowledge / create_entity 两个新工具。"""
    names = {spec.name for spec in ctx.registry.specs()}
    missing = {"propose_knowledge", "create_entity"} - names
    assert not missing, (
        "契约 §6.1/§6.4：注册表缺少新工具：" f"{sorted(missing)}（当前工具数={len(names)}）"
    )


def test_all_registered_tool_descriptions_come_from_frozen_source(ctx: AppContext):
    """§2.1/§2.3：全部工具描述来自 prompts.py（Lead 2026-10-21 裁决口径）。

    裁决：
    * 冻结词典 TOOL_DESCRIPTIONS 里的工具：description 必须与 TOOL_DESCRIPTIONS[name] 逐字一致；
    * 其余工具（await_task / read_task_result / create_tool / dev_* / declare_completion 等）：
      正文可以是 prompts.py 里的独立常量（如既有 TOOL_*_DESC），但不得留在工具模块里。
      判据：运行时 description 逐字等于 prompts.py 暴露的某个固定文本；
      且该正文不得再以字符串字面量出现在工具模块源码里（不是模块自写副本）。
    """
    specs = {spec.name: spec for spec in ctx.registry.specs()}
    mismatch = sorted(
        name
        for name, spec in specs.items()
        if name in prompts.TOOL_DESCRIPTIONS
        and prompts.TOOL_DESCRIPTIONS[name] != spec.description
    )
    assert not mismatch, (
        "契约 §2.1/§2.3（冻结词典部分）：这些工具的 description 与 TOOL_DESCRIPTIONS[name] "
        f"不是逐字一致：{mismatch}"
    )

    prompt_values = _prompts_string_values()
    not_from_prompts = sorted(
        name
        for name, spec in specs.items()
        if name not in prompts.TOOL_DESCRIPTIONS
        and (spec.description or "").strip()
        and spec.description not in prompt_values
    )
    assert not not_from_prompts, (
        "契约 §2.1/§2.3（其余工具部分）：这些已注册工具的 description 不等于 prompts.py "
        "暴露的任何固定文本（正文不是来自 prompts.py 的单一常量）："
        f"{not_from_prompts}"
    )

    duplicated_in_tool_module = sorted(
        name
        for name, spec in specs.items()
        if name not in prompts.TOOL_DESCRIPTIONS
        and (spec.description or "").strip()
        and spec.description
        in _module_string_literals(_tool_module_source_path(ctx, name))
    )
    assert not duplicated_in_tool_module, (
        "契约 §2.1/§2.3（其余工具部分）：这些工具的描述正文同时以字面量留在工具模块源码里"
        f"（另一份副本，不是单一来源）：{duplicated_in_tool_module}"
    )


def test_propose_knowledge_parameters_match_frozen_contract(ctx: AppContext):
    """§6.1/§7：必填字段、五类 category、scope、四种 epistemic_status、evidence_ids。"""
    tool = _tool(ctx, "propose_knowledge")
    assert tool is not None, "契约 §6.1：propose_knowledge 未注册"
    params = dict(tool.parameters or {})
    required = set(params.get("required") or [])
    assert {"content", "category", "scope", "epistemic_status", "evidence_ids"} <= required, (
        "契约 §6.1：propose_knowledge 必填字段 = content/category/scope/"
        f"epistemic_status/evidence_ids；实际 required={sorted(required)}"
    )
    enums = _walk_enums(params)
    assert any(set(enum) == CATEGORY_VALUES for enum in enums), (
        f"契约 §6.1：category 五类枚举没有进入下发的 schema；实际枚举={enums}"
    )
    assert any(set(enum) == EPISTEMIC_VALUES for enum in enums), (
        f"契约 §6.1：epistemic_status 四种枚举没有进入下发的 schema；实际枚举={enums}"
    )
    assert any(set(enum) == {"user", "topic", "entity"} for enum in enums), (
        f"契约 §6.1：scope.type 枚举没有进入下发的 schema；实际枚举={enums}"
    )
    assert _has_additional_properties_false(params), (
        "契约 §7：propose_knowledge 的参数模型必须 extra='forbid'（additionalProperties: false）"
    )


def test_create_entity_parameters_match_frozen_contract(ctx: AppContext):
    """§6.4/§7：必填 name/identity/creation_reason/evidence_ids，可选字段齐全。"""
    tool = _tool(ctx, "create_entity")
    assert tool is not None, "契约 §6.4：create_entity 未注册"
    params = dict(tool.parameters or {})
    required = set(params.get("required") or [])
    assert {"name", "identity", "creation_reason", "evidence_ids"} <= required, (
        "契约 §6.4：create_entity 必填字段 = name/identity/creation_reason/evidence_ids；"
        f"实际 required={sorted(required)}"
    )
    properties = set((params.get("properties") or {}))
    optional = {"kind", "aliases", "summary", "attributes", "relations"}
    assert optional <= properties, (
        f"契约 §6.4：create_entity 可选字段缺失：{sorted(optional - properties)}"
    )
    assert _has_additional_properties_false(params), (
        "契约 §7：create_entity 的参数模型必须 extra='forbid'"
    )


def test_correction_tools_expose_stable_locators(ctx: AppContext):
    """§6.3：correct_knowledge 加 knowledge_id/expected_version；correct_entity 加
    entity_id/expected_revision，同时保留旧参数兼容。"""
    knowledge_tool = _tool(ctx, "correct_knowledge")
    assert knowledge_tool is not None, "契约 §6.3：correct_knowledge 未注册"
    kprops = set((dict(knowledge_tool.parameters or {}).get("properties") or {}))
    assert {"knowledge_id", "expected_version"} <= kprops, (
        "契约 §6.3：correct_knowledge 缺少稳定定位参数 knowledge_id/expected_version；"
        f"实际参数={sorted(kprops)}"
    )
    assert {"content", "new_content", "delete", "candidate_index"} <= kprops, (
        "契约 §6.3：correct_knowledge 的旧参数必须保留兼容"
    )
    entity_tool = _tool(ctx, "correct_entity")
    assert entity_tool is not None, "契约 §6.3：correct_entity 未注册"
    eprops = set((dict(entity_tool.parameters or {}).get("properties") or {}))
    assert {"entity_id", "expected_revision"} <= eprops, (
        "契约 §6.3：correct_entity 缺少稳定定位参数 entity_id/expected_revision；"
        f"实际参数={sorted(eprops)}"
    )
    assert {"entity", "attribute_key", "attribute_value", "delete_attribute"} <= eprops, (
        "契约 §6.3：correct_entity 的旧参数必须保留兼容"
    )


# ---------------------------------------------------------------------------
# 参数严格校验：额外字段 / 非法枚举 → 结构化失败（§7）
# ---------------------------------------------------------------------------


async def test_propose_knowledge_rejects_unknown_argument_structurally(ctx: AppContext):
    """§7：本地校验失败返回结构化失败结果（ToolEnvelope + 字段错误），不靠中文句子。"""
    tool = _tool(ctx, "propose_knowledge")
    assert tool is not None, "契约 §6.1：propose_knowledge 未注册（无法验证严格校验）"
    result = await ctx.registry.execute(
        ToolCall(
            id="call_p1d_1",
            name="propose_knowledge",
            arguments={
                "content": "参数校验测试内容-SENTINEL-7d3",
                "category": "general_fact",
                "scope": {"type": "user", "node_ids": []},
                "epistemic_status": "user_explicit",
                "evidence_ids": [],
                "unexpected_field": "should_be_rejected",
            },
        )
    )
    assert result.ok is False, "契约 §7：额外字段 unexpected_field 必须被拒绝"
    assert result.protocol_version == 1, (
        "契约 §7：本地校验失败必须返回结构化 ToolEnvelope（protocol_version=1）；"
        f"实际 protocol_version={result.protocol_version}, error={str(result.error)[:160]!r}"
    )
    detail = f"{result.error or ''}{json.dumps(result.data or {}, ensure_ascii=False)}"
    assert "unexpected_field" in detail, (
        "契约 §7：字段错误必须可读（应指出 unexpected_field）；"
        f"实际 error/data={detail[:200]!r}"
    )
    assert _knowledge_states(ctx.conn, "参数校验测试内容-SENTINEL-7d3") == [], (
        "契约 §7：参数校验失败不得落库"
    )


async def test_propose_knowledge_rejects_fabricated_evidence(ctx: AppContext):
    """§6.1：来源必须实际存在；编造的 evidence_ids 必须被拒绝且不落库。"""
    tool = _tool(ctx, "propose_knowledge")
    assert tool is not None, "契约 §6.1：propose_knowledge 未注册（无法验证证据校验）"
    content = "编造证据测试-SENTINEL-9a2"
    result = await ctx.registry.execute(
        ToolCall(
            id="call_p1d_2",
            name="propose_knowledge",
            arguments={
                "content": content,
                "category": "general_fact",
                "scope": {"type": "user", "node_ids": []},
                "epistemic_status": "qio_observed",
                "evidence_ids": ["does_not_exist_9a2"],
            },
        )
    )
    assert result.ok is False, (
        "契约 §6.1：evidence_ids 指向不存在的来源时不得成功（来源必须实际存在）"
    )
    assert result.protocol_version == 1, (
        "契约 §7：证据校验失败必须返回结构化 ToolEnvelope"
    )
    states = _knowledge_states(ctx.conn, content)
    assert "active" not in states and "verified" not in states, (
        f"契约 §6.1：证据不存在的知识不得生效；实际 state={states}"
    )


# ---------------------------------------------------------------------------
# 主对话新增知识全部待审批（§6.1）
# ---------------------------------------------------------------------------


async def test_general_fact_cannot_enter_active_knowledge_directly(ctx: AppContext):
    """§6.1：主对话新增知识全部须用户审批，不能通过选择 general_fact 绕过。"""
    tool = _tool(ctx, "propose_knowledge")
    assert tool is not None, "契约 §6.1：propose_knowledge 未注册"
    content = "低影响分类也不能绕过审批-SENTINEL-4c8"
    result = await ctx.registry.execute(
        ToolCall(
            id="call_p1d_3",
            name="propose_knowledge",
            arguments={
                "content": content,
                "category": "general_fact",
                "scope": {"type": "user", "node_ids": []},
                "epistemic_status": "user_explicit",
                "evidence_ids": [],
            },
        )
    )
    states = _knowledge_states(ctx.conn, content)
    assert "active" not in states and "verified" not in states, (
        "契约 §6.1：general_fact 提议被直接当成有效知识（绕过用户审批）；"
        f"实际 state={states}, tool_result.ok={result.ok}"
    )
    # 如果工具接受了这次提议，它必须停在待审批状态（draft/pending_review），不能消失得无声无息。
    if states:
        assert set(states) <= {"draft", "pending_review"}, (
            f"契约 §6.1：主对话新增知识的合法落点只有待审批；实际 state={states}"
        )


async def test_pending_knowledge_is_not_injected_into_model_input(ctx: AppContext):
    """§6.1：待审批不作为有效知识使用（不得进入注入面）。"""
    content = "待审批知识-不得注入-SENTINEL-e11"
    service = KnowledgeService(ctx.conn)
    item = service.create(category="general_fact", content=content)
    service.submit(item.id)
    assert service.get(item.id).state.value == "pending_review"

    topic = ctx.topics.nodes.create_topic("待审批知识注入检查").id
    ctx.memory.append_message(topic_id=topic, role="user", content="历史消息：待审批内容不应注入")
    adapter = FakeStreamAdapter([StreamScript(text=ANSWER + "\n收到")])

    async def _build(*args, **kwargs):
        return adapter

    ctx.build_adapter = _build
    import asyncio

    await asyncio.wait_for(ctx.run_turn("查询一下-SENTINEL-e11", topic_id=topic), timeout=60)
    assert adapter.requests, "装置失效：没有模型请求"
    for request in adapter.requests:
        joined = "\n".join(str(m.content or "") for m in request["messages"])
        assert content not in joined, (
            "契约 §6.1：pending_review 知识进入了注入面（待审批不得作为有效知识使用）"
        )


# ---------------------------------------------------------------------------
# 明确纠正当场生效（§6.3）
# ---------------------------------------------------------------------------


async def test_correct_knowledge_with_stable_id_takes_effect_immediately(ctx: AppContext):
    """§6.3：明确纠正当场生效，同一轮工具返回真实结果，不需要再次确认。"""
    content = "用户旧偏好-SENTINEL-b25"
    item = _create_active_knowledge(ctx, content)
    tool = _tool(ctx, "correct_knowledge")
    assert tool is not None, "契约 §6.3：correct_knowledge 未注册"
    result = await ctx.registry.execute(
        ToolCall(
            id="call_p1d_4",
            name="correct_knowledge",
            arguments={
                "content": content,
                "knowledge_id": item.id,
                "expected_version": item.version,
                "delete": True,
            },
        )
    )
    assert result.ok is True, (
        "契约 §6.3：用稳定 knowledge_id 定位的明确纠正必须当场成功；"
        f"实际 error={str(result.error)[:200]!r}"
    )
    current = KnowledgeService(ctx.conn).get(item.id)
    assert current is not None and current.state.value not in ("active", "verified"), (
        f"契约 §6.3：明确纠正没有当场生效；实际 state={getattr(current, 'state', None)}"
    )


async def test_correct_knowledge_version_conflict_is_reported_not_ignored(ctx: AppContext):
    """§6.3：expected_version 用于定位；版本不符时应如实冲突而不是盲目改写。"""
    content = "并发纠正测试-SENTINEL-d90"
    item = _create_active_knowledge(ctx, content)
    tool = _tool(ctx, "correct_knowledge")
    assert tool is not None, "契约 §6.3：correct_knowledge 未注册"
    result = await ctx.registry.execute(
        ToolCall(
            id="call_p1d_5",
            name="correct_knowledge",
            arguments={
                "content": content,
                "knowledge_id": item.id,
                "expected_version": int(item.version) + 99,
                "delete": True,
            },
        )
    )
    assert result.ok is False, (
        "契约 §6.3：expected_version 不符时必须报冲突（不得静默按内容定位后删除/改写）"
    )
    current = KnowledgeService(ctx.conn).get(item.id)
    assert current is not None and current.state.value == "active", (
        "契约 §6.3：版本冲突时不得改动条目；"
        f"实际 state={getattr(current, 'state', None)}"
    )


# ---------------------------------------------------------------------------
# 后台提炼输出契约（§6.2、§6.4）与固定提示词
# ---------------------------------------------------------------------------


async def test_knowledge_extraction_prompt_uses_frozen_text():
    """§2.1/§6.2：知识提炼使用 PROMPTS['KNOWLEDGE_EXTRACTION_SYSTEM']。"""
    adapter = FakeStreamAdapter([StreamScript(text='{"candidates": []}')])
    outcome = await extract_knowledge_candidates_outcome(adapter, _fake_summary())
    assert outcome.value is not None, f"知识提炼校验失败：{outcome.error}"
    prompt = "\n".join(str(m.content or "") for m in adapter.requests[0]["messages"])
    assert prompts.PROMPTS["KNOWLEDGE_EXTRACTION_SYSTEM"] in prompt, (
        "契约 §2.1/§6.2：知识提炼必须使用 PROMPTS['KNOWLEDGE_EXTRACTION_SYSTEM']"
    )


def test_knowledge_extraction_preserves_epistemic_status_and_evidence_ids():
    """§6.2：后台知识输出新增 epistemic_status 与 evidence_ids（必须真实存在由程序校验）。"""
    payload = {
        "candidates": [
            {
                "content": "用户每周三开预算会",
                "category": "general_fact",
                "attach": None,
                "entity": None,
                "epistemic_status": "qio_observed",
                "evidence_ids": ["frag_123"],
            }
        ]
    }
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    assert outcome.value is not None, (
        "契约 §6.2：知识提炼输出必须接受 epistemic_status/evidence_ids 字段；"
        f"校验失败原因={outcome.error!r}"
    )
    assert isinstance(outcome.value, KnowledgeExtraction)
    candidate = outcome.value.candidates[0]
    assert getattr(candidate, "epistemic_status", None) == "qio_observed", (
        "契约 §6.2：epistemic_status 在解析/修正层被丢弃"
    )
    assert getattr(candidate, "evidence_ids", None) == ["frag_123"], (
        "契约 §6.2：evidence_ids 在解析/修正层被丢弃"
    )


def test_knowledge_extraction_rejects_invalid_epistemic_value():
    """§6.2/§7：epistemic_status 是枚举；非法值不得被当成合法候选。"""
    payload = {
        "candidates": [
            {
                "content": "非法确定程度",
                "category": "general_fact",
                "attach": None,
                "entity": None,
                "epistemic_status": "made_up_status",
                "evidence_ids": [],
            }
        ]
    }
    outcome = validate_knowledge(json.dumps(payload, ensure_ascii=False))
    values = list(outcome.value.candidates) if outcome.value is not None else []
    assert not any(
        getattr(item, "epistemic_status", None) == "made_up_status" for item in values
    ), "契约 §6.2/§7：非法 epistemic_status 被当成合法候选保留"


async def test_entity_extraction_uses_frozen_prompt_and_new_schema():
    """§6.4：后台实体提炼使用 ENTITY_EXTRACTION_SYSTEM，输出带 identity/decision/evidence_ids。"""
    from agent.entities.extract import extract_entity_cards_outcome

    model_output = {
        "entities": [
            {
                "entity_id": None,
                "name": "我家的鹅",
                "identity": "用户家中饲养的一只鹅",
                "aliases": [],
                "kind": "宠物",
                "summary": "用户提到过的鹅",
                "decision": "create",
                "reason": "身份明确且持续提及",
                "evidence_ids": ["frag_777"],
                "attributes": [
                    {"key": "名字", "value": "大白", "evidence_ids": ["frag_777"]}
                ],
                "relations": [
                    {
                        "target": "用户",
                        "target_entity_id": None,
                        "type": "饲养",
                        "evidence_ids": ["frag_777"],
                    }
                ],
            }
        ]
    }
    adapter = FakeStreamAdapter(
        [StreamScript(text=json.dumps(model_output, ensure_ascii=False))]
    )
    # 证据目录由程序拥有；测试构造一条真实存在的 fragment id（模型只能引用目录里的 id）。
    catalog = EvidenceCatalog.from_entries(
        [{"id": "frag_777", "kind": "fragment", "label": "测试片段"}]
    )
    outcome = await extract_entity_cards_outcome(
        adapter,
        [{"id": "m_1", "role": "user", "content": "我家的鹅叫大白"}],
        evidence_catalog=catalog,
    )
    assert outcome.value, f"契约 §6.4：实体提炼输出被丢弃；失败原因={outcome.error!r}"
    prompt = "\n".join(str(m.content or "") for m in adapter.requests[0]["messages"])
    assert prompts.PROMPTS["ENTITY_EXTRACTION_SYSTEM"] in prompt, (
        "契约 §2.1/§6.4：后台实体提炼必须使用 PROMPTS['ENTITY_EXTRACTION_SYSTEM']"
    )

    def _as_dict(candidate):
        if hasattr(candidate, "model_dump"):
            return candidate.model_dump()
        if isinstance(candidate, dict):
            return dict(candidate)
        return dict(vars(candidate))

    data = _as_dict(outcome.value[0])
    for field in ("identity", "decision", "evidence_ids"):
        assert field in data, (
            "契约 §6.4：实体提炼输出缺少新字段 " + field + "（解析/修正层丢弃了它）"
        )
    assert data["decision"] in {"create", "update", "observe", "identity_uncertain"}, (
        f"契约 §6.4：decision 取值不在冻结枚举内：{data.get('decision')!r}"
    )
    assert data["decision"] != "create" or data.get("entity_id") in (None, ""), (
        "契约 §6.4：decision=create 必须对应空 entity_id"
    )
