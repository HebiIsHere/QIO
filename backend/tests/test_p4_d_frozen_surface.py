# -*- coding: utf-8 -*-
"""D 独立契约测试：阶段四冻结面（提示词 / 工具说明 / 参数文案 / 开发指南）。

契约：docs/qio-refactor-contract.md §14.2、§14.7；冻结提交 92d927f2。
只验证冻结面本身（以后所有角色只能引用 prompts.py），不依赖 A/B/C 实现。

基线说明：冻结文案已存在，本文件应在**任何阶段**都通过；若失败说明冻结面被改动。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

CLAUSE = "契约 §14.2"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src" / "agent"


def _prompts():
    try:
        return importlib.import_module("agent.prompts")
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{CLAUSE}：agent.prompts 不可导入：{exc!r}", pytrace=False)


def test_phase4_frozen_prompt_constants_exist():
    """§14.2：六个阶段四固定文案必须存在且非空。"""
    prompts = _prompts()
    expected = (
        "SUBTASK_CONTROL_DESCRIPTION", "SUBAGENT_SYSTEM", "SUBTASK_RETURN_POLICY",
        "SUBTASK_COMPLETION_SYSTEM", "AUTOMATION_SUGGESTION_SYSTEM",
        "ONBOARDING_FOLLOWUP_SYSTEM",
    )
    for name in expected:
        value = prompts.PROMPTS.get(name)
        assert isinstance(value, str) and value.strip(), (
            f"{CLAUSE}：PROMPTS 缺少冻结文案 {name}")
        assert len(value.strip()) >= 40, (
            f"{CLAUSE}：{name} 内容过短（不能是占位文案）")


def test_subtask_control_description_binds_required_semantics():
    """§14.2：_qio_subtask 控制说明必须覆盖 credential_ref / 上下文 / 预算 / 不可伪造。"""
    prompts = _prompts()
    text = prompts.PROMPTS["SUBTASK_CONTROL_DESCRIPTION"]
    for token in ("credential_ref", "model", "context", "evidence_ids", "budget"):
        assert token in text, f"{CLAUSE}：控制说明缺少 {token}"
    assert "不允许伪造" in text or "不能伪造" in text, (
        f"{CLAUSE}：控制说明必须明确模型不能伪造原话题/任务归属/授权")


def test_subagent_system_promises_no_full_conversation_and_memory_honesty():
    """§14.3：子任务执行者不得假设拥有完整主对话；记忆归属不得改写。"""
    prompts = _prompts()
    text = prompts.PROMPTS["SUBAGENT_SYSTEM"]
    assert "完整对话" in text or "完整主对话" in text, (
        f"{CLAUSE}：SUBAGENT_SYSTEM 必须声明不假设拥有完整对话")
    assert "记忆属于 QIO" in text or "保留原有行动者" in text, (
        f"{CLAUSE}：SUBAGENT_SYSTEM 必须保留记忆的行动者/来源/确定程度")
    assert "真实结果" in text and ("部分完成" in text or "受阻" in text), (
        f"{CLAUSE}：SUBAGENT_SYSTEM 必须要求按真实结果区分终态")


def test_subtask_return_and_completion_policies_keep_original_topic_and_ui():
    """§14.3：结果回原话题续作；不强行切换界面；尊重取消/暂停/目标修改。"""
    prompts = _prompts()
    ret = prompts.PROMPTS["SUBTASK_RETURN_POLICY"]
    done = prompts.PROMPTS["SUBTASK_COMPLETION_SYSTEM"]
    assert "原话题" in ret and ("不强行切换" in ret or "不强切" in ret), (
        f"{CLAUSE}：SUBTASK_RETURN_POLICY 必须说明原话题续作且不强切界面")
    assert "取消" in ret, f"{CLAUSE}：SUBTASK_RETURN_POLICY 必须尊重取消"
    assert "原目标" in done and "界面" in done, (
        f"{CLAUSE}：SUBTASK_COMPLETION_SYSTEM 必须检查原目标并保持用户界面")


def test_automation_suggestion_prompt_is_not_development_authorization():
    """§14.5：建议不是开发授权；只在稳定可重复且有收益时提出。"""
    prompts = _prompts()
    text = prompts.PROMPTS["AUTOMATION_SUGGESTION_SYSTEM"]
    assert "不是开发授权" in text, f"{CLAUSE}：必须写明建议不是开发授权"
    assert "空 suggestions" in text or "空" in text, (
        f"{CLAUSE}：必须允许返回空建议")
    assert "完整请求" in text, (
        f"{CLAUSE}：必须基于完整请求判断，不得只看开头文字")


def test_onboarding_followup_prompt_limits_two_questions_and_empty_allowed():
    """§14.5：首次引导最多两问、允许无问题、失败不阻塞。"""
    prompts = _prompts()
    text = prompts.PROMPTS["ONBOARDING_FOLLOWUP_SYSTEM"]
    assert "最多两个" in text, f"{CLAUSE}：追问必须最多两个"
    assert "空 questions" in text or "没有必要追问" in text, (
        f"{CLAUSE}：必须允许返回空 questions")


def test_phase4_argument_descriptions_are_single_source():
    """§14.2：_qio_subtask / credential_ref 参数文案只从 prompts 引用。"""
    prompts = _prompts()
    args = prompts.ARGUMENT_DESCRIPTIONS
    assert args.get("_qio_subtask") == prompts.PROMPTS["SUBTASK_CONTROL_DESCRIPTION"], (
        f"{CLAUSE}：ARGUMENT_DESCRIPTIONS[_qio_subtask] 必须引用同一份冻结文案")
    assert isinstance(args.get("credential_ref"), str) and args["credential_ref"].strip(), (
        f"{CLAUSE}：缺少 credential_ref 参数说明")


def test_phase4_tool_descriptions_cover_eleven_tools():
    """§14.2：11 条新工具说明必须存在且信息完整（不靠旧描述兜底）。"""
    prompts = _prompts()
    expected = {
        "await_task", "read_task_result", "list_available_credentials", "create_tool",
        "dev_list_tasks", "dev_list_files", "dev_read_file", "dev_write_file",
        "dev_run_tests", "dev_submit_tool", "declare_completion",
    }
    missing = sorted(name for name in expected if not str(prompts.TOOL_DESCRIPTIONS.get(name) or "").strip())
    assert not missing, f"{CLAUSE}：缺少工具说明：{missing}"
    assert "不返回密钥" in prompts.TOOL_DESCRIPTIONS["list_available_credentials"], (
        f"{CLAUSE}：凭据目录工具说明必须写明不返回密钥")
    assert "不表示已经完成" in prompts.TOOL_DESCRIPTIONS["await_task"], (
        f"{CLAUSE}：await_task 的 notify 必须说明不等于完成")
    assert "已放弃任务不能提交" in prompts.TOOL_DESCRIPTIONS["dev_submit_tool"], (
        f"{CLAUSE}：dev_submit_tool 必须说明已放弃任务不能提交")
    assert "完整 version" in prompts.TOOL_DESCRIPTIONS["declare_completion"], (
        f"{CLAUSE}：declare_completion 必须要求使用实际返回的完整 version")
    assert "四千字符" in prompts.TOOL_DESCRIPTIONS["read_task_result"], (
        f"{CLAUSE}：read_task_result 必须写明单次上限")


def test_dev_guide_sections_have_seven_sections_and_honest_isolation():
    """§14.2/§14.4：开发指南七节齐全；受限子进程不得称为安全沙箱。"""
    prompts = _prompts()
    sections = prompts.DEV_GUIDE_SECTIONS
    expected = {"flow", "requirements", "contract", "test", "submit", "claims", "recover"}
    missing = expected - set(sections)
    assert not missing, f"{CLAUSE}：DEV_GUIDE_SECTIONS 缺少：{sorted(missing)}"
    for key in expected:
        assert str(sections[key] or "").strip(), f"{CLAUSE}：开发指南 {key} 为空"
    text = sections["contract"]
    assert "受限子进程不等于安全沙箱" in text, (
        f"{CLAUSE}：契约节必须写明受限子进程不等于安全沙箱")
    assert "凭证" in sections["test"] or "凭据" in sections["test"], (
        f"{CLAUSE}：测试节必须说明使用模拟凭据、不得称真实链路验证")


def test_frozen_phase4_text_is_not_copied_elsewhere():
    """§14.2/§14.5：固定文案唯一来源；业务模块不得复制大段固定指令。"""
    prompts_text = (SRC / "prompts.py").read_text(encoding="utf-8")
    markers = (
        "建议本身不是开发授权",
        "最多两个问题，每个问题一句话",
        "结果只给预览时，按需要使用 read_task_result",
        "受限子进程不等于安全沙箱",
    )
    for marker in markers:
        assert marker in prompts_text, f"{CLAUSE}：prompts.py 缺少冻结原句「{marker}」"
        hits = []
        for path in SRC.rglob("*.py"):
            if path.name == "prompts.py":
                continue
            if marker in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path.relative_to(SRC)))
        assert not hits, (
            f"{CLAUSE}：固定文案出现第二份来源：{hits}（只能引用 prompts.py）")


def test_qio_answer_marker_is_retained():
    """§14.7：[[QIO:ANSWER]] 回答声明保留（不得因清理旧路径被顺手删除）。"""
    prompts = _prompts()
    texts = [str(value) for value in vars(prompts).values() if isinstance(value, str)]
    merged = "\n".join(texts)
    assert "[[QIO:ANSWER]]" in merged, f"{CLAUSE}：prompts 中缺少 [[QIO:ANSWER]] 回答声明"
