# -*- coding: utf-8 -*-
"""阶段四 C：旧路径清理与「模型调用 / 固定文案」静态清点。

守护三件事：
1. 旧 ToolCreator（向主模型要工具提案→草案）已从生产导出删除，不恢复审批前执行旧路径；
2. 固定文案只有 agent/prompts.py 单一来源，旧自动化/追问固定提示不得在业务模块散落；
3. 模型调用入口清点：范围可核对，自动化建议/追问只走公共结构化任务，不注入聊天回答标记。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agent"


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _code_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return names, attributes


def _imports_module(tree: ast.AST, module: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == module for alias in node.names):
                return True
    return False


def test_tool_creator_is_removed_from_production():
    assert not (SRC / "tools" / "creator.py").exists()
    for path in _python_files():
        tree = ast.parse(_read(path))
        names, attributes = _code_names(tree)
        assert "ToolCreator" not in names, f"仍有旧 ToolCreator 名字：{path}"
        assert "ToolCreator" not in attributes, f"仍有旧 ToolCreator 属性：{path}"
        assert not _imports_module(tree, "agent.tools.creator"), f"仍有旧 creator 导入：{path}"


def test_old_fixed_prompts_are_only_referenced_in_prompts_module():
    """旧固定提示（工具自动化草案 / 旧追问）不得在业务模块留副本或调用。"""
    offenders: dict[str, list[str]] = {}
    for name in ("TOOL_AUTOMATION_PROMPT", "FOLLOWUP_SUGGEST_PROMPT"):
        hits: list[str] = []
        for path in _python_files():
            if path.name == "prompts.py":
                continue
            if name in _read(path):
                hits.append(path.relative_to(SRC).as_posix())
        offenders[name] = hits
    assert offenders == {"TOOL_AUTOMATION_PROMPT": [], "FOLLOWUP_SUGGEST_PROMPT": []}


def test_answer_marker_only_lives_in_prompts():
    hits = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if "[[QIO:ANSWER]]" in _read(path) and path.name != "prompts.py"
    ]
    assert hits == []


def test_automation_and_followups_use_structured_calls_without_chat_loop():
    for rel in ("services/automation.py", "services/followups.py"):
        tree = ast.parse(_read(SRC / rel))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert "AgentLoop" not in names, f"{rel} 不得再走聊天主循环"
        assert "run" not in attributes, f"{rel} 不得 loop.run（会注入回答协议）"
        assert "structure_task_marker" not in names


#: 当前允许出现模型调用入口的文件（名字 → 调用方式说明，供审阅/清点）。
MODEL_CALL_SITES = {
    "adapters/anthropic.py": "适配器内部完成请求",
    "core/loop.py": "主循环统一调用 adapter",
    "entities/extract.py": "实体提炼结构化任务",
    "interactive/execution.py": "已批准结果卡片缺少正文时的一次生成",
    "interactive/understanding.py": "旧理解链（仅旧失败记录重试；新提交不调用）",
    "knowledge/extraction.py": "知识提炼结构化任务",
    "maintenance/questions.py": "维护独立回答结构化任务",
    "maintenance/runner.py": "维护结构化任务 + 一次有依据修复",
    "memory/summary.py": "记忆摘要结构化任务",
    # R9-G：受限模式模型授权审查 + 每轮用户要求解析。两者都使用 prompts.py
    # 单一来源的提示词、结构化 JSON 输出，并由后端校验；不执行工具/文件/设置。
    "security/review.py": "受限模式模型授权审查（R9-G，prompts.py 单一来源）",
    "security/resolution.py": "用户要求解析（R9-G，prompts.py 单一来源，结构化 JSON）",
    "services/app.py": "独立回答 / 通知续作的主循环入口",
    "services/automation.py": "自动化建议结构化任务（公共入口）",
    "services/followups.py": "首次引导追问结构化任务",
    "services/maintenance.py": "后台维护（旧 dreaming 函数保留，已不在调度器路径）",
    "services/turn_orchestrator.py": "主对话轮次",
    "tools/subagent_tool.py": "子任务执行",
}


def test_model_call_sites_are_inventoried():
    found: set[str] = set()
    for path in _python_files():
        text = _read(path)
        if (
            "adapter.complete(" in text
            or "run_structured_task(" in text
            or "loop.run(" in text
        ):
            found.add(path.relative_to(SRC).as_posix())
    assert found == set(MODEL_CALL_SITES), (
        "模型调用入口变了：新增请想清楚是否该走 prompts 单一来源 + 结构化入口，"
        "并在证据里登记，再更新本清单。"
        f" 新增：{sorted(found - set(MODEL_CALL_SITES))}；"
        f" 消失：{sorted(set(MODEL_CALL_SITES) - found)}"
    )


def test_new_submission_flow_does_not_call_retired_understanding_chain():
    tree = ast.parse(_read(SRC / "interactive" / "submission.py"))
    names, attributes = _code_names(tree)
    # 注释里可以说明历史；**代码**不得调用旧理解链。
    assert "run_understanding" not in names
    assert "run_understanding" not in attributes


def test_result_card_generation_has_single_generation_site():
    text = _read(SRC / "interactive" / "execution.py")
    # 只有「已批准、但结果卡片缺少正文」时的一次生成；不允许第二次重生成。
    assert text.count("content=_GENERATE_SYSTEM") == 1, "结果卡正文只有一处生成入口（不做二次生成）"
    assert "INTERACTIVE_RESULT_CARD_SYSTEM" in text
