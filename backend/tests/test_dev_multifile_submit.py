"""多文件项目从工作区一路走到注册：定义带着项目文件，缺依赖如实点名。

回归的缺口：定义里只有一段 `code`，工作区里写的 `pkg/util.py` 在测试与运行
时都不存在；重启后即使工作区还在，注册的工具也 import 不到自己的模块。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from pydantic import ValidationError

from agent.storage.tool_store import ToolStore
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.runtime_tools import CodeTool
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition
from agent.tools.tester import ToolTester

ENTRY_CODE = (
    "from pkg.util import double\n"
    "\n"
    "def run(**kwargs):\n"
    "    return {'value': double(kwargs['x'])}\n"
)


def _executor() -> SandboxExecutor:
    return SandboxExecutor("subprocess")


def _workspace_with_package(tmp_path) -> tuple[DevWorkspace, str]:
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("做一个翻倍工具")
    ws.write_file(task.id, "pkg/__init__.py", "")
    ws.write_file(task.id, "pkg/util.py", "def double(x):\n    return x * 2\n")
    ws.write_definition(
        task.id,
        ToolDefinition(
            name="doubler",
            description="把数字翻倍",
            code=ENTRY_CODE,
            parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
            tests=[{"name": "翻倍", "input": {"x": 21}, "expect": {"value": 42}}],
        ),
    )
    return ws, task.id


def test_submit_collects_workspace_files_into_the_definition(tmp_path):
    ws, task_id = _workspace_with_package(tmp_path)

    definition = ws.collect_definition(task_id)

    assert definition is not None
    assert definition.files["pkg/util.py"] == "def double(x):\n    return x * 2\n"
    assert definition.files["pkg/__init__.py"] == ""
    # 清单与需求不是项目模块，不塞进定义
    assert "tool.json" not in definition.files
    assert "request.md" not in definition.files
    assert "state.json" not in definition.files


def test_registered_tool_still_imports_its_modules_after_restart(
    tmp_path, db_conn: sqlite3.Connection
):
    """注册过的工具必须自包含：定义里带着模块，重启后照样跑。"""
    ws, task_id = _workspace_with_package(tmp_path)
    store = ToolStore(db_conn)
    store.save(ws.collect_definition(task_id))

    reloaded = ToolStore(db_conn).load("doubler")
    assert reloaded is not None
    result = asyncio.run(CodeTool(reloaded, _executor()).run(x=21))

    assert result.ok, result.error
    assert '"value": 42' in result.content


def test_tester_runs_the_whole_project(tmp_path):
    ws, task_id = _workspace_with_package(tmp_path)

    report = asyncio.run(ToolTester(_executor()).run(ws.collect_definition(task_id)))

    assert report.passed, report.outcomes[0].detail


def test_definition_rejects_escaping_file_paths():
    with pytest.raises(ValidationError):
        ToolDefinition(
            name="evil",
            description="d",
            code="def run(**kwargs):\n    return {}\n",
            files={"../evil.py": "print('nope')"},
        )


def test_definition_rejects_an_oversized_project():
    files = {f"mod_{i}.py": "x" * 20_000 for i in range(40)}

    with pytest.raises(ValidationError):
        ToolDefinition(
            name="huge",
            description="d",
            code="def run(**kwargs):\n    return {}\n",
            files=files,
        )


def test_entry_only_function_tool_is_allowed():
    """入口写在模块里时不需要再给 code —— 否则多文件项目只能「一半」用包。"""
    definition = ToolDefinition(
        name="pkg_tool",
        description="d",
        entry="pkg.main:run",
        files={
            "pkg/__init__.py": "",
            "pkg/main.py": "def run(**kwargs):\n    return {'ok': True}\n",
        },
    )

    assert definition.code == ""


def test_missing_dependency_names_the_declared_requirement():
    definition = ToolDefinition(
        name="needs_dep",
        description="d",
        code="def run(**kwargs):\n    return {}\n",
        requirements=["definitely_missing_pkg"],
    )

    hint = definition.dependency_hint(
        "ModuleNotFoundError: No module named 'definitely_missing_pkg'"
    )

    assert hint is not None
    assert "缺少依赖" in hint
    assert "definitely_missing_pkg" in hint


def test_test_report_explains_a_missing_declared_dependency():
    definition = ToolDefinition(
        name="needs_dep",
        description="d",
        code="import definitely_missing_pkg\n\ndef run(**kwargs):\n    return {}\n",
        requirements=["definitely_missing_pkg"],
        tests=[{"name": "t1", "input": {}, "expect": {}}],
    )

    report = asyncio.run(ToolTester(_executor()).run(definition))

    assert not report.passed
    detail = report.outcomes[0].detail
    assert "缺少依赖" in detail
    assert "definitely_missing_pkg" in detail
