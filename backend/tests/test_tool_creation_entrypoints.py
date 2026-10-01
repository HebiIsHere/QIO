"""守卫：工具创建的**唯一**入口是 submit_definition，执行生成代码**必须先过授权闸门**。

背景（2026-10-02 复现并修复的缺口）：`ToolLifecycle.create_from_request`
（模型提案 → 交叉测试 → 审批 → 注册）在**审批之前**就把 AI 生成的代码真的跑了一遍 ——
复现里用户点了拒绝，沙箱仍然执行了两次（两条用例各一次）。它在生产代码里没有任何
调用点（只有测试用），但一直留着，形成「它不安全、但现在没人调用」的状态。已删除。

「删掉就完了」不算闭环：这里用**源码守卫**把两件事钉住：

1. 生命周期里不得再出现向模型要提案的创建入口（`create_from_request` /
   `ToolCreator` / `propose(`）；
2. 生产代码里执行生成代码的路径只能有两处（`DevRunTestsTool.run` 与
   `ToolLifecycle.submit_definition`），并且调用方在跑测试之前必须先拿到
   `ensure_test_authorization` 的授权。

新增第三条执行路径时，这条守卫会失败 —— 那是**刻意的**：先想清楚它的授权闸门，
再改这里，而不是绕过它。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src" / "agent"

# 生产代码里允许出现「跑生成代码」的地方（每处都必须先过授权闸门）
EXECUTION_SITES = {"tools/dev_tools.py", "tools/lifecycle.py"}


def _sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8-sig"))
        for path in sorted(SRC.rglob("*.py"))
    ]


def test_lifecycle_has_no_model_proposal_entrypoint():
    """按 AST 看**代码**：说明里提到历史名字是可以的，重新写出来不行。"""
    # utf-8-sig：源码文件可能带 BOM，ast.parse 不认 BOM。
    tree = ast.parse((SRC / "tools" / "lifecycle.py").read_text(encoding="utf-8-sig"))
    functions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "create_from_request" not in functions
    assert "ToolCreator" not in names
    # 也禁止换个名字重新加回「向模型要提案」这条路
    assert "propose" not in attributes
    assert "creator" not in attributes


def test_only_the_two_gated_call_sites_run_generated_code():
    found: dict[str, int] = {}
    for rel, text in _sources():
        if rel == "tools/tester.py":
            continue  # 测试器的定义本身不执行代码
        if "tester.run(" in text:
            found[rel] = text.count("tester.run(")
    assert set(found) == EXECUTION_SITES, (
        "执行生成代码的路径变了："
        f"{sorted(found)}。新增路径必须自带授权闸门（tools/dev_auth.py），"
        "想清楚之后再更新这条守卫。"
    )


def test_the_test_runner_asks_for_authorization_before_running_code():
    from agent.tools.dev_tools import DevRunTestsTool, DevSubmitTool

    for tool in (DevRunTestsTool, DevSubmitTool):
        source = inspect.getsource(tool.run)
        assert "ensure_test_authorization" in source, f"{tool.__name__} 少了授权闸门"
        gate = source.index("ensure_test_authorization")
        after = [
            index
            for marker in ("self.tester.run", "submit_definition")
            if (index := source.find(marker)) != -1
        ]
        assert after, f"{tool.__name__} 里找不到执行点"
        assert gate < min(after), f"{tool.__name__} 的执行点出现在授权闸门之前"


def test_the_lifecycle_does_not_own_a_model_client():
    """生命周期只管「测试 → 审批 → 注册」；提案由工作区文件负责。"""
    from agent.tools.lifecycle import ToolLifecycle

    assert not hasattr(ToolLifecycle, "create_from_request")
    lifecycle = ToolLifecycle(
        adapter=None, approvals=None, registry=__import__(
            "agent.tools.registry", fromlist=["ToolRegistry"]
        ).ToolRegistry(),
    )
    assert not hasattr(lifecycle, "creator")
