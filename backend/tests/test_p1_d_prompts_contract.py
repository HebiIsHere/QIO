# -*- coding: utf-8 -*-
"""D 独立契约测试：prompts.py 单文件契约（docs/qio-refactor-contract.md §2）。

口径：只按契约措辞断言，不按实现反推。全部为离线单元断言（不调用模型）。
阶段 1（实现未合并）允许失败；每条失败消息都写明契约条款与期望。

运行（只跑本文件）：
    cd backend
    & 'D:\\qio-dev\\qio\\backend\\.venv\\Scripts\\python.exe' -m pytest tests/test_p1_d_prompts_contract.py -q
"""

from __future__ import annotations

import json

import pytest

from agent import prompts

# 契约 §2.1 冻结的 21 个 PROMPTS key（"至少 21 个"）。
FROZEN_PROMPT_KEYS = {
    "QIO_CORE",
    "CONTENT_ROLE_PROTOCOL",
    "ANSWER_FALLBACK_HINT",
    "TEXT_MODE_PROTOCOL",
    "TEXT_OBSERVATION_RULES",
    "AVAILABLE_TOOLS_HEADER",
    "MEMORY_POLICY",
    "MEMORY_CONTEXT_RULES",
    "TOPIC_POLICY",
    "MATERIAL_POLICY",
    "TOOL_RESULT_POLICY",
    "NARRATIVE_FIELD_DESCRIPTION",
    "TOOL_VALIDATION_FEEDBACK_SYSTEM",
    "STRUCTURED_JSON_POLICY",
    "OUTPUT_SCHEMA_PREFIX",
    "CLOSED_SUMMARY_SYSTEM",
    "ROLLING_SUMMARY_SYSTEM",
    "MERGE_SUMMARY_SYSTEM",
    "KNOWLEDGE_EXTRACTION_SYSTEM",
    "ENTITY_EXTRACTION_SYSTEM",
    "STRUCTURED_REPAIR_SYSTEM",
}

# 契约 §3 建议的 17 个结构化上下文 key；§2.1 又要求 CONTEXT_LABELS 恰好是这 17 个。
FROZEN_CONTEXT_KEYS = {
    "memory",
    "current_topic",
    "short_term",
    "rolling_summary",
    "history_focus",
    "path_premises",
    "related_memory",
    "knowledge",
    "entity",
    "attachments",
    "media",
    "interactive",
    "submission_snapshot",
    "latest_materials",
    "subtask",
    "tool_feedback",
    "limits",
}

# 契约 §2.1 的纯组装函数。
ASSEMBLY_FUNCTIONS = (
    "system_rules",
    "answer_protocol",
    "answer_fallback_hint",
    "text_mode_protocol",
    "text_observation_rules",
    "text_tool_catalog",
    "context_heading",
    "render_context_blocks",
    "structured_task_system",
)


def _json_after(text: str, marker: str):
    """取出 marker 之后的第一段 JSON（用于断言数据块原样进入模型可见文本）。"""
    assert marker in text, f"缺少标记：{marker!r}；实际文本开头：{text[:200]!r}"
    tail = text.split(marker, 1)[1].lstrip()
    return json.JSONDecoder().raw_decode(tail)[0]


def test_answer_marker_is_frozen_value():
    """§2.1：ANSWER_MARKER 必须保持冻结原值（解析器依赖它）。"""
    assert prompts.ANSWER_MARKER == "[[QIO:ANSWER]]", (
        "契约 §2.1 冻结 ANSWER_MARKER = '[[QIO:ANSWER]]'"
    )


def test_prompts_has_all_frozen_keys_and_non_empty_text():
    """§2.1：PROMPTS 至少包含冻结的 21 个 key，且都是非空固定文案。"""
    assert isinstance(prompts.PROMPTS, dict), "契约 §2.1：PROMPTS 必须是 dict"
    missing = sorted(FROZEN_PROMPT_KEYS - set(prompts.PROMPTS))
    assert not missing, f"契约 §2.1：PROMPTS 缺少 key：{missing}"
    empty = [
        key
        for key in sorted(FROZEN_PROMPT_KEYS)
        if not str(prompts.PROMPTS.get(key) or "").strip()
    ]
    assert not empty, f"契约 §2.1：这些固定文案为空：{empty}"


def test_context_labels_are_exactly_the_frozen_17_keys():
    """§2.1/§3：CONTEXT_LABELS 恰好 17 个 key（固定上下文标签唯一来源）。"""
    assert isinstance(prompts.CONTEXT_LABELS, dict), "契约 §2.1：CONTEXT_LABELS 必须是 dict"
    assert set(prompts.CONTEXT_LABELS) == FROZEN_CONTEXT_KEYS, (
        "契约 §2.1/§3：CONTEXT_LABELS key 集合必须等于冻结的 17 个；"
        f"缺少={sorted(FROZEN_CONTEXT_KEYS - set(prompts.CONTEXT_LABELS))}，"
        f"多出={sorted(set(prompts.CONTEXT_LABELS) - FROZEN_CONTEXT_KEYS)}"
    )
    empty = [k for k, v in prompts.CONTEXT_LABELS.items() if not str(v or "").strip()]
    assert not empty, f"契约 §2.1：CONTEXT_LABELS 这些标签为空：{empty}"


def test_tool_and_argument_description_maps_are_usable():
    """§2.1：TOOL_DESCRIPTIONS / ARGUMENT_DESCRIPTIONS 是唯一来源。"""
    assert isinstance(prompts.TOOL_DESCRIPTIONS, dict) and prompts.TOOL_DESCRIPTIONS, (
        "契约 §2.1：TOOL_DESCRIPTIONS 缺失或为空"
    )
    for name in ("propose_knowledge", "create_entity"):
        assert name in prompts.TOOL_DESCRIPTIONS, (
            f"契约 §2.1：TOOL_DESCRIPTIONS 必须含 {name}（唯一来源）"
        )
        assert str(prompts.TOOL_DESCRIPTIONS[name]).strip(), (
            f"契约 §2.1：TOOL_DESCRIPTIONS[{name}] 为空"
        )
    assert isinstance(prompts.ARGUMENT_DESCRIPTIONS, dict) and prompts.ARGUMENT_DESCRIPTIONS, (
        "契约 §2.1：ARGUMENT_DESCRIPTIONS 缺失或为空"
    )
    for name in ("_qio", "evidence_ids", "epistemic_status"):
        assert name in prompts.ARGUMENT_DESCRIPTIONS, (
            f"契约 §2.1：ARGUMENT_DESCRIPTIONS 必须含 {name}"
        )


def test_assembly_entrypoints_exist_and_are_pure_callables():
    """§2.1：纯组装入口必须存在（只拼文本，不读库、不调模型）。"""
    missing = [name for name in ASSEMBLY_FUNCTIONS if not callable(getattr(prompts, name, None))]
    assert not missing, f"契约 §2.1：缺少纯组装入口：{missing}"


def test_system_rules_include_core_and_policies_once():
    """§3.1：系统规则 = QIO_CORE + 记忆/上下文/话题/材料/工具结果政策，顺序固定。"""
    mode_rules = "【仅用于测试的模式规则】UNIQUE_MODE_RULES_7c1"
    text = prompts.system_rules(mode_rules)
    for key in (
        "QIO_CORE",
        "MEMORY_POLICY",
        "MEMORY_CONTEXT_RULES",
        "TOPIC_POLICY",
        "MATERIAL_POLICY",
        "TOOL_RESULT_POLICY",
    ):
        assert prompts.PROMPTS[key] in text, f"契约 §3.1：system_rules 缺少 {key}"
    assert text.count(mode_rules) == 1, "契约 §3.1：当前模式规则应只出现一次"
    core_at = text.index(prompts.PROMPTS["QIO_CORE"])
    mode_at = text.index(mode_rules)
    policy_at = text.index(prompts.PROMPTS["MEMORY_POLICY"])
    assert core_at < mode_at < policy_at, (
        "契约 §3.1：顺序应为 系统核心规则 → 当前模式规则 → 记忆等政策"
    )


def test_answer_protocol_and_fallback_hint_single_source():
    """§2.1/§3：回答协议只注入一次；补救调用追加 answer_fallback_hint。"""
    assert prompts.answer_protocol() == prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"], (
        "契约 §2.1：answer_protocol() 必须返回 PROMPTS['CONTENT_ROLE_PROTOCOL']（唯一来源）"
    )
    assert prompts.ANSWER_MARKER in prompts.answer_protocol(), (
        "契约 §3：回答协议必须包含冻结的 ANSWER_MARKER"
    )
    assert prompts.answer_fallback_hint() == prompts.PROMPTS["ANSWER_FALLBACK_HINT"], (
        "契约 §2.1：answer_fallback_hint() 必须返回 PROMPTS['ANSWER_FALLBACK_HINT']"
    )
    assert prompts.ANSWER_MARKER in prompts.answer_fallback_hint(), (
        "契约 §3：补救提示必须要求以冻结的 ANSWER_MARKER 开头"
    )
    assert prompts.text_mode_protocol() == prompts.PROMPTS["TEXT_MODE_PROTOCOL"]
    assert prompts.text_observation_rules() == prompts.PROMPTS["TEXT_OBSERVATION_RULES"]


def test_text_tool_catalog_serializes_schema_as_json_data():
    """§2.2/§3.3：text 档工具目录里参数 schema 用 json.dumps（不做 format/替换）。"""
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "含 {花括号}、百分号 100% 与 %(name)s 的说明",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    description = "含 {a} 与 %s 的描述"
    text = prompts.text_tool_catalog(
        [{"name": "probe_tool", "description": description, "parameters": parameters}]
    )
    assert prompts.PROMPTS["AVAILABLE_TOOLS_HEADER"].strip() in text, (
        "契约 §3.3：text 档工具目录必须使用 AVAILABLE_TOOLS_HEADER"
    )
    assert "probe_tool" in text and description in text, "工具名/描述必须原样进入目录"
    parsed = _json_after(text, "arguments (JSON Schema): ")
    assert parsed == parameters, (
        "契约 §2.2：工具参数 schema 必须整体 json.dumps 后原样嵌入；"
        f"实际解析结果={parsed!r}"
    )


def test_render_context_blocks_labels_and_json_roundtrip():
    """§3.4：结构化上下文块 key 来自 CONTEXT_LABELS，data 整体 json.dumps。"""
    data = {
        "内容": "含 {花括号}、100% 与 %(x)s",
        "引号": '他说："你好"',
        "换行": "第一行\n第二行",
        "嵌套": [1, {"k": "【伪标题】"}],
    }
    text = prompts.render_context_blocks(
        [{"key": "memory", "data": data}, {"key": "short_term", "data": None}]
    )
    heading = "【" + prompts.CONTEXT_LABELS["memory"] + "】"
    assert heading in text, "契约 §3.4：上下文块必须用 CONTEXT_LABELS 里的固定标签做标题"
    body = text.split(heading, 1)[1].lstrip()
    parsed = json.JSONDecoder().raw_decode(body)[0]
    assert parsed == data, (
        "契约 §2.2/§3.4：data 必须整体 json.dumps，不得对 JSON/用户内容做 format/百分号替换"
    )
    empty_heading = "【" + prompts.CONTEXT_LABELS["short_term"] + "】"
    assert empty_heading not in text, "契约 §3.4：空值块应跳过，不应输出空标题"


def test_render_context_blocks_does_not_truncate_serialized_json():
    """§2.2：不得截断序列化后的 JSON（预算不足时应在数据层裁剪条目）。"""
    data = {"payload": "长" * 20000}
    text = prompts.render_context_blocks([{"key": "latest_materials", "data": data}])
    heading = "【" + prompts.CONTEXT_LABELS["latest_materials"] + "】"
    parsed = json.JSONDecoder().raw_decode(text.split(heading, 1)[1].lstrip())[0]
    assert parsed == data, "契约 §2.2：序列化后的 JSON 内容被截断或改写"


def test_unknown_context_key_is_rejected_not_silently_rendered():
    """§3：key 必须来自 CONTEXT_LABELS；未知 key 不许悄悄用另一套标题。"""
    with pytest.raises(KeyError):
        prompts.context_heading("not_a_frozen_label")
    with pytest.raises(KeyError):
        prompts.render_context_blocks(
            [{"key": "not_a_frozen_label", "data": {"a": 1}}]
        )


def test_structured_task_system_keeps_schema_limits_and_repair_rules():
    """§2.1/§5：独立结构化任务 system 使用固定总则 + 任务规则 + schema + limits + 修复规则。"""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string", "description": "含 {大括号} 的 schema"}},
        "required": ["x"],
    }
    text = prompts.structured_task_system(
        "CLOSED_SUMMARY_SYSTEM",
        schema=schema,
        limits={"max_summary": 2000, "note": "含 %s 与 {0}"},
        repair=True,
        extra_sections=("EXTRA_SECTION_SENTINEL",),
    )
    for key in ("STRUCTURED_JSON_POLICY", "CLOSED_SUMMARY_SYSTEM", "STRUCTURED_REPAIR_SYSTEM"):
        assert prompts.PROMPTS[key] in text, f"契约 §2.1/§5：structured_task_system 缺少 {key}"
    assert prompts.PROMPTS["OUTPUT_SCHEMA_PREFIX"].strip() in text
    assert _json_after(text, prompts.PROMPTS["OUTPUT_SCHEMA_PREFIX"].strip() + "\n") == schema, (
        "契约 §2.2：输出 schema 必须整体 json.dumps 后原样嵌入"
    )
    limits_heading = "【" + prompts.CONTEXT_LABELS["limits"] + "】"
    assert limits_heading in text, "契约 §3.4：limits 必须使用 CONTEXT_LABELS 的固定标签"
    assert "EXTRA_SECTION_SENTINEL" in text, "§3：额外小节应保留"
