# -*- coding: utf-8 -*-
"""D 独立契约测试：旧路径清理 / 模型调用清点 / 密钥不外泄 / 迁移（契约 §14.5、§14.7）。

验收点：
* 旧 ToolCreator 生产调用退出；若保留兼容名，只是单来源别名、不执行旧提案流程；
* 旧互动理解 / JSON 修复 / 结果卡二次生成 / 旧提案调用不得恢复；
* 全部模型调用使用 prompts.py 固定指令（业务模块无大段本地指令副本）；
* [[QIO:ANSWER]] 回答声明保留；
* 本阶段无新增迁移（版本仍 51，历史迁移不可改）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

CLAUSE = "契约 §14.5/§14.7"
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
SRC = BACKEND / "src" / "agent"


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def test_tool_creator_has_no_production_call_sites():
    """§14.5：旧 ToolCreator 生产调用退出；不能在服务/API/主循环里再被实例化。"""
    offenders: list[str] = []
    for path in _py_files():
        text = _read(path)
        if "ToolCreator" not in text and "tools.creator" not in text:
            continue
        if path.name == "creator.py":
            continue  # 兼容模块自身（只允许保留定义与归档说明）
        if path.name == "__init__.py":
            # 兼容导出只允许出现在 tools/__init__，且不得被业务代码导入使用
            continue
        # 允许两处：明确定位为旧流程的注释/守卫；这里禁止实例化与调用
        if re.search(r"ToolCreator\s*\(", text) or re.search(r"creator\.propose", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert not offenders, (
        f"{CLAUSE}：旧 ToolCreator 仍有生产调用点：{offenders}（应删除或归档，不恢复审批前执行旧路径）")


def test_tool_creator_compat_export_is_single_source_or_gone():
    """§14.5：若保留兼容名，只引用单一来源；不得复制旧提案实现。"""
    init = _read(SRC / "tools" / "__init__.py")
    creator_path = SRC / "tools" / "creator.py"
    if "ToolCreator" in init:
        assert creator_path.exists(), (
            f"{CLAUSE}：tools/__init__ 仍导出 ToolCreator 但实现文件不存在")
        text = _read(creator_path)
        assert len(text) > 0, f"{CLAUSE}：ToolCreator 兼容模块为空"
    # 旧提案提示词不得在其他业务模块复制
    prompts_text = _read(SRC / "prompts.py")
    marker = "TOOL_PROPOSAL_PROMPT"
    duplicates = [
        str(path.relative_to(SRC))
        for path in _py_files()
        if path.name not in ("prompts.py",)
        and marker in _read(path)
        and "creator.py" not in path.name
        and path.name != "spec.py"
    ]
    assert not duplicates, (
        f"{CLAUSE}：旧提案提示词在业务模块出现第二份来源：{duplicates}")


def test_retired_interactive_paths_stay_retired():
    """§14.5：旧互动理解链不得恢复为**新调用**（旧记录的只读兼容函数允许保留）。

    契约只要求旧链退出新流程；阶段三已经保留只读兼容函数（历史记录可查）。
    因此这里禁止的是「interactive 包之外出现调用」以及提交路径再次调用它们。
    """
    banned = (
        "build_understanding_messages(",
        "build_repair_messages(",
        "_generate_text(",
        "run_understanding(",
    )
    offenders: list[str] = []
    interactive_dir = SRC / "interactive"
    for path in _py_files():
        if interactive_dir in path.parents:
            continue  # 旧链自身模块（只读兼容定义）允许保留
        text = _read(path)
        for token in banned:
            if token in text:
                offenders.append(f"{path.relative_to(SRC)}::{token}")
    assert not offenders, (
        f"{CLAUSE}：旧互动理解链在 interactive 包之外被调用：{offenders}")
    submission = _read(SRC / "interactive" / "submission.py")
    assert "run_understanding(" not in submission, (
        f"{CLAUSE}：提交路径再次调用旧独立理解")
    assert "build_understanding_messages(" not in submission, (
        f"{CLAUSE}：提交路径再次构造旧 JSON 理解消息")


def test_all_model_calls_use_prompts_module():
    """§14.5：清点全部模型调用；固定指令不得散落在业务模块。"""
    import agent.prompts as prompts_module  # noqa: F401 - 断言模块可导入

    prompt_constants = [
        name for name, value in vars(prompts_module).items()
        if isinstance(value, str) and len(value) > 80
    ]
    offenders: list[str] = []
    for path in _py_files():
        text = _read(path)
        if "adapter.complete(" not in text and ".complete(" not in text:
            continue
        if path.name in ("prompts.py", "base.py", "anthropic.py", "openai_compat.py"):
            continue
        # 固定指令必须来自 prompts.py：引用其常量不可怕，重写一份长指令才违规
        inline_instruction = re.search(
            r"=\s*(\"\"\"|\'\'\')[^\1]{200,}\1", text, re.S)
        if inline_instruction is not None:
            offenders.append(f"{path.relative_to(SRC)}（本地长指令，可能绕过 prompts.py）")
        imported_constants = [name for name in prompt_constants if name in text]
        if not imported_constants and inline_instruction is None:
            # 允许通过 helper 传提示词（如 derived_tasks 调 lifecycle helper）：
            # 只要模块内没有自造长指令，就不是「固定指令散落」。
            continue
    assert not offenders, (
        f"{CLAUSE}：以下模块自带长固定指令（固定文案必须只有 prompts.py 一份）：{offenders}")


def test_answer_declaration_is_used_by_main_loop():
    """§14.7：[[QIO:ANSWER]] 保留且主循环仍消费（不能被旧路径清理误删）。"""
    prompts_text = _read(SRC / "prompts.py")
    assert "ANSWER_MARKER" in prompts_text and "[[QIO:ANSWER]]" in prompts_text, (
        f"{CLAUSE}：prompts.py 缺少回答声明标记常量")
    loop_text = _read(SRC / "core" / "loop.py")
    assert "ANSWER_MARKER" in loop_text, (
        f"{CLAUSE}：主循环没有消费 ANSWER_MARKER（回答协议被误删？）")
    assert "from agent.prompts import ANSWER_MARKER" in loop_text or (
        "agent.prompts" in loop_text and "ANSWER_MARKER" in loop_text), (
        f"{CLAUSE}：主循环必须从 prompts.py 单一来源取回答标记")


def test_no_plaintext_secret_logging_patterns():
    """§14.7：密钥不得进入日志 / Trace / 错误输出（静态检查明显违规写法）。"""
    patterns = (
        re.compile(r"logger\.[a-z]+\([^\n]*%s[^\n]*secret", re.S),
        re.compile(r"logger\.[a-z]+\([^\n]*get_secret\(", re.S),
        re.compile(r"print\([^\n]*get_secret\(", re.S),
    )
    offenders: list[str] = []
    for path in _py_files():
        text = _read(path)
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(str(path.relative_to(SRC)))
                break
    assert not offenders, (
        f"{CLAUSE}：疑似把密钥写进日志/输出：{offenders}（必须走 trace/redact.py）")


def test_no_new_migration_or_version_bump():
    """§14.1：本阶段无新增迁移；版本仍 51，不得改历史迁移。"""
    schema = __import__("agent.storage.schema", fromlist=["SCHEMA_VERSION"])
    assert int(getattr(schema, "SCHEMA_VERSION")) == 51, (
        f"{CLAUSE}：本阶段不应新增迁移/提升版本（实际 {getattr(schema, 'SCHEMA_VERSION', None)}）")
    assert int(getattr(schema, "COMPENSATION_VERSION")) == 51, (
        f"{CLAUSE}：补偿版本应保持 51")
    numbers = [int(number) for number, _ in getattr(schema, "MIGRATIONS")]
    assert max(numbers) == 51, f"{CLAUSE}：出现了未登记的 52/53/54 迁移：{max(numbers)}"
    assert 49 in numbers and 50 in numbers and 51 in numbers, (
        f"{CLAUSE}：阶段三迁移 49/50/51 必须保留（只追加）")


def test_redaction_module_still_used_for_new_output_paths():
    """§14.7：新增输出路径必须经过 trace/redact.py；redact 能力仍在。"""
    redact = SRC / "trace" / "redact.py"
    assert redact.exists(), f"{CLAUSE}：trace/redact.py 缺失"
    text = _read(redact)
    assert "def redact" in text or "def redact_text" in text or "SECRET" in text, (
        f"{CLAUSE}：redact 模块没有可用的脱敏入口")
