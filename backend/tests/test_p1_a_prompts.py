"""Phase 1 A：提示词单文件化（唯一来源 / 薄引用 / 参数说明）。"""

from __future__ import annotations

from agent import prompts
from agent.interactive import prompts as interactive_prompts
from agent.tools import builtin, cmd_tools, fs_tools, memory_search, web_fetch, web_search


def test_frozen_public_symbols_exist_and_are_aliased():
    assert prompts.ANSWER_MARKER == "[[QIO:ANSWER]]"
    assert len(prompts.PROMPTS) >= 21
    # 旧符号只是指向同一来源的别名，不是另一份正文
    assert prompts.CONTENT_ROLE_PROTOCOL == prompts.PROMPTS["CONTENT_ROLE_PROTOCOL"]
    assert prompts.ANSWER_FALLBACK_HINT == prompts.PROMPTS["ANSWER_FALLBACK_HINT"]
    assert prompts.NARRATIVE_FIELD_DESCRIPTION == prompts.PROMPTS["NARRATIVE_FIELD_DESCRIPTION"]
    # 文本工具协议保留 tool_calls / name / arguments 键名（解析器不动）
    assert "tool_calls" in prompts.PROMPTS["TEXT_MODE_PROTOCOL"]
    assert '\"name\"' in prompts.PROMPTS["TEXT_MODE_PROTOCOL"]
    assert "arguments" in prompts.PROMPTS["TEXT_MODE_PROTOCOL"]


def test_old_duplicated_literals_are_gone():
    # 旧英文 system prompt / 工具条目模板已迁走，不再有第二份正文
    assert not hasattr(prompts, "SYSTEM_PROMPT_TEXT_MODE")
    assert not hasattr(prompts, "SYSTEM_PROMPT_TOOLS_HEADER")
    assert not hasattr(prompts, "TEXT_TOOL_ENTRY")
    # 工具调用 / 观测的旧模块级模板也已迁到 prompts
    from agent.adapters import text as text_adapter

    assert not hasattr(text_adapter, "TOOL_RESULT_TEMPLATE")
    assert not hasattr(text_adapter, "TOOL_CALL_TEMPLATE")
    assert not hasattr(text_adapter, "UNKNOWN_TOOL_NAME")


def test_tool_descriptions_come_from_prompts():
    assert builtin.EchoTool.description == prompts.TOOL_DESCRIPTIONS["echo"]
    assert builtin.NowTool.description == prompts.TOOL_DESCRIPTIONS["now"]
    assert fs_tools.FsReadTool.description == prompts.TOOL_DESCRIPTIONS["fs_read"]
    assert fs_tools.FsWriteTool.description == prompts.TOOL_DESCRIPTIONS["fs_write"]
    assert cmd_tools.RunProgramTool.description == prompts.TOOL_DESCRIPTIONS["run_program"]
    assert cmd_tools.RunCmdTool.description == prompts.TOOL_DESCRIPTIONS["run_shell"]
    assert web_search.WebSearchTool.description == prompts.TOOL_DESCRIPTIONS["web_search"]
    assert web_fetch.WebFetchTool.description == prompts.TOOL_DESCRIPTIONS["web_fetch"]
    assert (
        memory_search.MemorySearchTool.description
        == prompts.TOOL_DESCRIPTIONS["memory_search"]
    )


def test_tool_argument_descriptions_come_from_prompts():
    assert (
        builtin.EchoTool.parameters["properties"]["text"]["description"]
        == prompts.argument_description("echo", "text")
    )
    assert (
        web_fetch.WebFetchTool.parameters["properties"]["max_chars"]["description"]
        == prompts.argument_description("web_fetch", "max_chars")
    )
    assert (
        cmd_tools.RunProgramTool.parameters["properties"]["program"]["description"]
        == prompts.argument_description("run_program", "program")
    )


def test_interactive_prompts_are_thin_references():
    system = interactive_prompts.system_prompt()
    expected = prompts.interactive_system_prompt(
        interactive_prompts._allowed_ops_text(), prompts.INTERACTIVE_OUTPUT_SCHEMA
    )
    assert system == expected
    # 输出 schema 只出现一次（不是两份正文）
    assert system.count(prompts.INTERACTIVE_OUTPUT_SCHEMA) == 1
    assert "add_reply_card" in system

    messages = interactive_prompts.build_understanding_messages(
        {"requirement": {"text": "整理一下"}}
    )
    assert [m.role for m in messages] == ["system", "user"]
    assert prompts.INTERACTIVE_CONTEXT_START in (messages[1].content or "")
    assert prompts.INTERACTIVE_CONTEXT_END in (messages[1].content or "")


def test_interactive_repair_keeps_reasons_as_data_not_format():
    messages = interactive_prompts.build_repair_messages(
        {"requirement": {"text": "{name} $HOME"}}, "raw-output", ["缺少 intentKey", '字段 "x" 非法']
    )
    last = messages[-1].content or ""
    assert "缺少 intentKey" in last
    assert '字段 "x" 非法' in last
    assert prompts.ANSWER_MARKER not in last


def test_tool_proposal_prompt_comes_from_prompts():
    from agent.tools import spec

    assert spec.TOOL_PROPOSAL_PROMPT is prompts.TOOL_PROPOSAL_SYSTEM
