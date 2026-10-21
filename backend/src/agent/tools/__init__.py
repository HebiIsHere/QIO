"""Tool mechanism: registry, builtin tools, lifecycle (M10)."""

from agent.tools.approval import ApprovalRequest, ApprovalResult, ApprovalService
from agent.tools.base import Tool, ToolResult
from agent.tools.builtin import EchoTool, NowTool
from agent.tools.lifecycle import ToolLifecycle, ToolOutcome
from agent.tools.memory_search import MemorySearchTool
from agent.tools.registry import ToolRegistry
from agent.tools.runtime_tools import CodeTool, SubagentStubTool
from agent.tools.sandbox import SandboxExecutor, SandboxResult
from agent.tools.spec import TestCase, ToolDefinition, ToolProposal, validate_tool_proposal
from agent.tools.tester import TestOutcome, TestReport, ToolTester

# 阶段四（角色 C）：旧 ToolCreator（向主模型要工具提案 → 生成代码草案）已从生产导出移除。
# 工具创建的唯一入口是 dev_workspaces + submit_definition（先授权、再测试、再注册）；
# 自动化建议走 agent.services.automation 的结构化建议 + tool_candidate 审批，不再恢复旧路径。

__all__ = [
    "Tool", "ToolResult", "ToolRegistry", "EchoTool", "NowTool",
    "ToolLifecycle", "ToolOutcome",
    "CodeTool", "SubagentStubTool", "SandboxExecutor", "SandboxResult",
    "ToolDefinition", "ToolProposal", "TestCase", "validate_tool_proposal",
    "ToolTester", "TestOutcome", "TestReport",
    "ApprovalService", "ApprovalRequest", "ApprovalResult",
    "MemorySearchTool",
]
