"""开发规范分节注入：每一步只给这一步该知道的规则。

回归的缺口：规范是**一份长文**，创建任务时一次性灌给模型 —— 之后无论模型在
写代码、排错还是提交，都没有再对上一次口径；而一开始那份长文里大半内容
（例如提交口径）在当下还用不到。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.prompts import DEV_GUIDE_BY_STEP, DEV_GUIDE_SECTIONS, dev_guide
from agent.tools.dev_tools import CreateToolTool, DevRunTestsTool
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.sandbox import SandboxResult
from agent.tools.spec import ToolDefinition


class _PassingSandbox:
    """测试永远通过、且记录「到底执行了没有」的沙箱替身。"""

    executor = "subprocess"
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.executions = 0

    async def effective_executor(self) -> str:
        return "subprocess"

    async def execute(
        self,
        code,
        arguments,
        extra_env=None,
        policy=None,
        files=None,
        entry=None,
        interpreter=None,
    ) -> SandboxResult:
        self.executions += 1
        return SandboxResult(ok=True, value=dict(arguments), stdout="", stderr="")


class _ApprovingService:
    """用户点「允许」时的审批服务（测试里只需要它同意）。"""

    async def request(self, kind: str, payload: dict, **kwargs):
        from agent.tools.approval import ApprovalResult

        return ApprovalResult("appr_test", "approved")


def test_each_step_only_gets_its_own_sections():
    prepare = dev_guide("create")
    testing = dev_guide("test")
    submit = dev_guide("submit")

    # 准备阶段：需求规格与契约（含多文件与依赖）
    assert "需求规格" in prepare and "多文件项目" in prepare
    assert "提交口径" not in prepare
    # 测试阶段：执行授权与「测试通过不等于真实链路」
    assert "执行授权" in testing and "不等于真实链路" in testing
    assert "契约约束" not in testing
    # 提交阶段：提交口径与结论核对
    assert "提交口径" in submit and "declare_completion" in submit
    assert "需求规格" not in submit


def test_every_section_is_used_by_some_step():
    """不留孤儿分节：分节表与步骤表必须一一对得上。"""
    used = {key for keys in DEV_GUIDE_BY_STEP.values() for key in keys}
    assert set(DEV_GUIDE_SECTIONS) == used
    assert all(text.strip() for text in DEV_GUIDE_SECTIONS.values())


def test_no_step_dumps_the_whole_guide():
    whole = sum(len(text) for text in DEV_GUIDE_SECTIONS.values())
    for step in DEV_GUIDE_BY_STEP:
        assert len(dev_guide(step)) < whole * 0.6, step


def test_create_tool_returns_the_preparation_sections(tmp_path):
    tool = CreateToolTool(DevWorkspace(tmp_path / "ws"))

    result = asyncio.run(tool.run(request="做一个把数字翻倍的工具"))

    assert result.ok
    assert "需求规格" in result.content
    assert "多文件项目" in result.content
    assert "提交口径" not in result.content


def test_run_tests_returns_the_test_section(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("做一个把数字翻倍的工具")
    ws.write_definition(
        task.id,
        ToolDefinition(
            name="doubler",
            description="翻倍",
            code="def run(**kwargs):\n    return {'x': kwargs['x']}\n",
            tests=[{"name": "t", "input": {"x": 1}, "expect": {"x": 1}}],
        ),
    )
    tool = DevRunTestsTool(ws, sandbox=_PassingSandbox(), approvals=_ApprovingService())

    result = asyncio.run(tool.run(workspace=task.id))

    assert result.ok
    assert "执行授权" in result.content
    assert "不等于真实链路" in result.content
