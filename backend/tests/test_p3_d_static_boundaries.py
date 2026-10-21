# -*- coding: utf-8 -*-
"""D 独立静态审阅（契约 §13.2 / §13.3 / §13.4 / §13.6）：边界与接线纪律。

覆盖（不依赖运行时，只看源码事实）：
* 新增固定模型文案只进 prompts.py；新模块引用冻结常量而不是自带提示词；
* tool_models.py / runtime.py 冻结面结构未被改写（字段 / 函数集合）；
* Lead 接线：services/app.py 注册 12 个板面工具；turn_orchestrator 注入 INTERACTIVE_POLICY、
  设置 / 复位 runtime 上下文、轮末交付问题；普通模式不暴露 board_* 工具；
* A 退休旧路径：提交路径不再调用独立理解 / JSON 修复 / 空正文二次生成；
  interactive/prompts.py 的「禁止接入通用主循环」旧约束已移除；
* 新流程不恢复旧三分类 JSON 协议；
* 前端：来源 / 状态按真实字段、长卡片 Markdown、等待与续作入口存在。

基线说明：本阶段实现未合入时这些检查会失败（不是 skip），失败信息写明契约条款。
运行（只跑本文件）：
    cd backend
    & 'D:/qio-dev/qio/backend/.venv/Scripts/python.exe' -m pytest tests/test_p3_d_static_boundaries.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CLAUSE = "契约 §13.2/§13.3/§13.4/§13.6"
ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = ROOT / "backend" / "src" / "agent"
FRONTEND_SRC = ROOT / "frontend" / "src"

BOARD_TOOLS = (
    "board_inspect", "board_read_cards", "board_propose_intents",
    "board_update_intent", "board_wait_for_input", "board_create_card",
    "board_update_card", "board_move_card", "board_group_cards",
    "board_order_group", "board_add_link", "board_finish_intent",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _require(path: Path, what: str) -> Path:
    if not path.exists():
        pytest.fail(f"{CLAUSE}：缺少 {what}（{path.relative_to(ROOT)}）", pytrace=False)
    return path


def _python_files() -> list[Path]:
    return sorted(BACKEND_SRC.rglob("*.py"))


# ---------------------------------------------------------------------------
# 提示词单文件 / 冻结面
# ---------------------------------------------------------------------------


def test_new_prompt_text_lives_only_in_prompts_py():
    """§13.2：新增固定模型文案只在 prompts.py（新模块引用常量而不是复制正文）。"""
    phrases = ("当前运行于互动模式，使用同一个 QIO 的理解",)
    offenders = []
    for path in _python_files():
        if path.name == "prompts.py" and path.parent.name == "agent":
            continue
        text = _read(path)
        for phrase in phrases:
            if phrase in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"{CLAUSE}：INTERACTIVE_POLICY 正文出现在 prompts.py 以外：{offenders}"


def test_board_tools_reference_frozen_descriptions_and_runtime():
    """§13.4：12 个工具实现引用冻结说明 / 参数模型 / 可信运行上下文。"""
    path = _require(BACKEND_SRC / "interactive" / "board_tools.py", "B 的 board_tools.py")
    text = _read(path)
    assert "TOOL_DESCRIPTIONS" in text, f"{CLAUSE}：工具说明必须引用 prompts.TOOL_DESCRIPTIONS"
    assert "TOOL_ARG_MODELS" in text, f"{CLAUSE}：工具参数模型必须来自 tool_models.TOOL_ARG_MODELS"
    assert "require_board_turn" in text, (
        f"{CLAUSE}：工具入口必须调用 runtime.require_board_turn（不接受模型自报板面）")
    assert "build_board_tools" in text, f"{CLAUSE}：必须导出 build_board_tools(app) 注册工厂"


def test_intent_service_exists_and_owns_authorization():
    """§13.4：授权 / 账本 / 依赖校验由 B 的 intent_service 统一实现。"""
    path = _require(BACKEND_SRC / "interactive" / "intent_service.py", "B 的 intent_service.py")
    text = _read(path)
    for token in ("scope", "authorization", "operation_key", "board_operations"):
        assert token in text, f"{CLAUSE}：intent_service 缺少 {token} 相关实现"


def test_tool_models_and_runtime_frozen_surface():
    """§13.2：tool_models / runtime 的冻结公开结构仍在（未被改写）。"""
    from agent.interactive import tool_models, runtime

    assert tuple(tool_models.TOOL_ARG_MODELS) == BOARD_TOOLS
    assert tool_models.READ_ONLY_TOOLS == ("board_inspect", "board_read_cards")
    for name in ("BoardTurnContext", "BoardToolUnavailable", "require_board_turn",
                 "set_board_turn", "reset_board_turn", "record_delivered_question",
                 "take_delivered_questions", "clear_delivered_questions"):
        assert hasattr(runtime, name), f"{CLAUSE}：runtime 缺少冻结符号 {name}"
    fields = set(tool_models.IntentSpec.model_fields)
    # P73 允许扩展：operations 是可视化意图的数据来源（只追加字段，不改写既有字段）。
    assert fields == {"client_ref", "title", "goal", "expected", "basis", "scope",
                      "depends_on", "conflicts_with", "operations"}, (
        f"{CLAUSE}：IntentSpec 字段被改写：{sorted(fields)}")


# ---------------------------------------------------------------------------
# Lead 接线
# ---------------------------------------------------------------------------


def test_app_registers_board_tools_and_retires_understanding_scheduling():
    """§13.6：app.py 注册 build_board_tools；不再调度独立理解任务。"""
    app_text = _read(_require(BACKEND_SRC / "services" / "app.py", "services/app.py"))
    assert "build_board_tools" in app_text, (
        f"{CLAUSE}：services/app.py 未注册 build_board_tools（Lead 接线缺失）")
    for retired in ("run_understanding", "schedule_understanding",
                    "request_understanding"):
        assert retired not in app_text, (
            f"{CLAUSE}：services/app.py 仍在调用旧理解链路 {retired}（契约要求退��）")


def test_orchestrator_injects_interactive_policy_and_runtime():
    """§13.6：turn_orchestrator 按模式注入 INTERACTIVE_POLICY、设置 / 复位 runtime。"""
    text = _read(_require(BACKEND_SRC / "services" / "turn_orchestrator.py",
                          "services/turn_orchestrator.py"))
    assert "INTERACTIVE_POLICY" in text, (
        f"{CLAUSE}：turn_orchestrator 未注入 INTERACTIVE_POLICY 模式规则")
    for token in ("set_board_turn", "reset_board_turn", "take_delivered_questions"):
        assert token in text, (
            f"{CLAUSE}：turn_orchestrator 缺少 {token} 接线（可信上下文 / 问题交付）")
    assert "board_" in text and ("interactive" in text or "mode" in text), (
        f"{CLAUSE}：turn_orchestrator 缺少按模式选择板面工具的接线")
    # 普通模式不得暴露板面工具：需要存在显式的过滤 / 可见性判断
    assert re.search(r"(visible|exclude|filter|hidden).*board|board.*(visible|exclude|filter|hidden)",
                     text, re.IGNORECASE | re.DOTALL), (
        f"{CLAUSE}：未找到普通模式隐藏 board_* 工具的显式接线")


def test_loop_supports_allowing_no_reply():
    """§13.6：主循环支持「允许不回复」的轮次选项（已保存意图 / 等待轮不强制补救）。"""
    text = _read(_require(BACKEND_SRC / "core" / "loop.py", "core/loop.py"))
    assert re.search(r"allow_no_answer|allow_no_reply|allow_no_final|board_resume", text), (
        f"{CLAUSE}：core/loop.py 未提供「允许不回复」的轮次选项")


# ---------------------------------------------------------------------------
# A 退休旧路径 / 旧三分类协议
# ---------------------------------------------------------------------------


def test_submission_no_longer_invokes_understanding_chain():
    """§13.3：提交路径不再调用独立理解 / JSON 修复 / 空正文二次生成。

    旧函数 / 旧记录只读兼容可以保留（契约允许），但新提交流程不得调用它们：
    这里只禁「真实调用链」，并核对提交路径确实走 turn_acceptor（主循环受理）。
    """
    text = _read(_require(BACKEND_SRC / "interactive" / "submission.py",
                          "interactive/submission.py"))
    for retired in ("run_understanding(", "build_understanding_messages(",
                    "build_repair_messages(", "_generate_text("):
        assert retired not in text, (
            f"{CLAUSE}：submission.py 仍引用旧链路 {retired}（新流程必须退休）")
    # _schedule_understanding 只允许保留「定义 + 旧只读重试包装」两处，不得出现在提交路径
    assert text.count("_schedule_understanding(") <= 2, (
        f"{CLAUSE}：_schedule_understanding 出现了调用点（新提交不得调度独立理解）")
    assert "turn_acceptor" in text, (
        f"{CLAUSE}：提交路径必须走主循环受理（turn_acceptor），而不是独立理解")


def test_interactive_prompts_old_constraint_removed():
    """§13.3：interactive/prompts.py 的「禁止接入通用主循环」旧约束必须移除。"""
    path = _require(BACKEND_SRC / "interactive" / "prompts.py", "interactive/prompts.py")
    text = _read(path)
    assert "绝不允许把上下文再拼回通用聊天循环" not in text, (
        f"{CLAUSE}：仍保留「禁止把上下文拼回通用主循环」的旧约束原句")
    assert "不再保留" in text, (
        f"{CLAUSE}：interactive/prompts.py 未明确声明旧约束已移除")


def test_new_flow_does_not_reuse_old_three_category_protocol():
    """§13.2/§13.3：新流程不恢复旧三分类 JSON 协议（intent / clarification / no_action）。"""
    for name in ("board_tools.py", "intent_service.py", "submission.py"):
        path = BACKEND_SRC / "interactive" / name
        if not path.exists():
            pytest.fail(f"{CLAUSE}：缺少 {path.relative_to(ROOT)}", pytrace=False)
        text = _read(path)
        for token in ("INTERACTIVE_OUTPUT_SCHEMA", "UNDERSTANDING_KINDS",
                      "\"kind\": \"clarification\"", "\"no_action\""):
            assert token not in text, (
                f"{CLAUSE}：{name} 又用上了旧三分类协议（{token}）")


# ---------------------------------------------------------------------------
# 前端：长卡片 Markdown / 等待与续作入口
# ---------------------------------------------------------------------------


def test_frontend_long_card_markdown_is_used_without_fixed_truncation():
    """§13.5：结果卡用 Markdown 渲染，且不存在写死的四千字截断。"""
    components = FRONTEND_SRC / "components" / "interactive"
    texts = {p.name: _read(p) for p in components.glob("*.vue")}
    joined = "\n".join(texts.values())
    assert "MarkdownContent" in joined, (
        f"{CLAUSE}：互动结果卡未使用 MarkdownContent 渲染 Markdown")
    assert not re.search(r"slice\(\s*0\s*,\s*4000\s*\)|substring\(\s*0\s*,\s*4000\s*\)",
                         joined), (
        f"{CLAUSE}：存在写死的 4000 字截断（长正文必须完整保存 / 展示）")


def test_frontend_has_waiting_and_resume_entries():
    """§13.5：前端有等待 / 续作 / 局部失败与恢复的真实入口。"""
    texts = "\n".join(_read(p) for p in FRONTEND_SRC.rglob("*.vue"))
    for token in ("等待", "续作", "部分完成"):
        assert token in texts, f"{CLAUSE}：前端缺少「{token}」相关真实 UI 文案 / 入口"
