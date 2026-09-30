"""Tool definition contract for agent-created tools.

The MAIN model proposes a tool as JSON; local validation enforces the
schema before any testing or approval happens. The model never writes
directly to the registry — everything passes the lifecycle.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
# 入口写法：`包.模块:函数`（多文件项目里入口写在模块里，而不是 code 字符串里）。
_ENTRY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)
# 依赖声明：名称 + 可选 extras + 可选一个版本约束（不做完整 PEP 508 解析，
# 只挡住明显不是包名的东西）。
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9_.\-]{1,64}"
    r"(\[[A-Za-z0-9_,\-]{1,64}\])?"
    r"([<>=!~]=?[A-Za-z0-9_.\-*+]{1,32})?$"
)
_MISSING_MODULE_RE = re.compile(r"No module named '([^']+)'")
MAX_REQUIREMENTS = 10


def _dist_name(requirement: str) -> str:
    """从一条依赖声明里取出分发包名（用于和 `No module named 'x'` 对上）。"""
    text = requirement.strip()
    for separator in ("[", "=", "<", ">", "!", "~", " "):
        text = text.split(separator)[0]
    return text


class TestCase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    input: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(default_factory=dict)


class SubagentBudget(BaseModel):
    """Execution budget for subagent tools (independent of the main loop)."""

    max_iterations: int = Field(default=5, ge=1, le=50)
    max_tokens: int = Field(default=100_000, ge=1_000)
    output_limit_chars: int = Field(default=2000, ge=100, le=50_000)


class ToolDefinition(BaseModel):
    name: str
    description: str = Field(min_length=1, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)
    code: str = Field(default="", max_length=20_000)
    # 多文件项目：入口可以是模块里的函数（`pkg.main:run`），这时 code 可以为空。
    entry: str | None = None
    # 项目里的其它文件（相对路径 → 内容）。路径与体积规则与工作区完全一致。
    files: dict[str, str] = Field(default_factory=dict)
    # 声明的第三方依赖。**本机不会自动安装**：缺了就如实报缺哪个。
    requirements: list[str] = Field(default_factory=list, max_length=MAX_REQUIREMENTS)
    tool_type: Literal["function", "subagent"] = "function"
    sync: bool = True
    credential_ref: str | None = None
    model: str | None = None  # subagent tools: which model to run
    # 批准时的能力指纹；恢复时若与当前推导不一致 → 需重新批准
    approved_policy_fingerprint: str | None = None
    tests: list[TestCase] = Field(default_factory=list, max_length=20)
    subagent_budget: SubagentBudget | None = None

    @model_validator(mode="after")
    def _require_code_for_functions(self) -> "ToolDefinition":
        if self.tool_type == "function" and not self.code.strip() and not self.entry:
            raise ValueError("function tools require code or entry")
        if self.tool_type == "subagent":
            if self.subagent_budget is None:
                self.subagent_budget = SubagentBudget()
        if self.entry and not _ENTRY_RE.match(self.entry):
            raise ValueError(
                f"invalid entry: {self.entry!r} (expected 'pkg.module:function')"
            )
        # 项目文件的路径与体积走与工作区同一份规则：一份实现，不会两边漂移。
        from agent.tools.project_files import check_project_size, safe_rel_path

        normalized: dict[str, str] = {}
        for name, content in self.files.items():
            normalized[safe_rel_path(name)] = str(content or "")
        check_project_size(normalized)
        self.files = normalized
        cleaned: list[str] = []
        for item in self.requirements:
            text = str(item).strip()
            if not _REQUIREMENT_RE.match(text):
                raise ValueError(f"invalid requirement: {item!r}")
            cleaned.append(text)
        self.requirements = cleaned
        return self

    @field_validator("entry", mode="before")
    @classmethod
    def _normalize_entry(cls, value: object) -> object:
        """空字符串按「没有入口」处理：`tool.json` 模板里它就是空的。"""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def dependency_hint(self, error: str | None) -> str | None:
        """缺依赖时给出「缺的是哪一个、声明过没有」。

        第一阶段没有「受管依赖环境」：依赖必须由用户装好。所以这里只把事实说清，
        不假装能自动补上。
        """
        match = _MISSING_MODULE_RE.search(error or "")
        if match is None:
            return None
        name = match.group(1).split(".")[0]
        if any(_dist_name(item) == name for item in self.requirements):
            return (
                f"缺少依赖：{name}（tool.json 的 requirements 里声明了它，"
                "但本机环境没有安装；不会自动安装，请让用户装好再试，"
                "或改成只用标准库实现）"
            )
        return (
            f"缺少依赖：{name}（没有在 tool.json 的 requirements 里声明；"
            "第三方依赖要先声明，而且本机需要已经装好）"
        )


class ToolProposal(BaseModel):
    explanation: str = Field(min_length=1, max_length=1000)
    tool: ToolDefinition


def validate_tool_proposal(text: str) -> tuple[ToolProposal | None, str | None]:
    """Parse model output; tolerates ```json fences."""
    import re as _re

    match = _re.search(r"```(?:json)?\s*(.*?)```", text, _re.DOTALL)
    candidate = match.group(1) if match else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "proposal is not a JSON object"
    try:
        proposal = ToolProposal(**data)
    except ValidationError as exc:
        return None, f"schema violation: {exc.errors()[:3]}"
    if not _NAME_RE.match(proposal.tool.name):
        return None, f"invalid tool name: {proposal.tool.name!r}"
    if not proposal.tool.tests:
        return None, "at least one deterministic test case is required"
    return proposal, None


TOOL_PROPOSAL_PROMPT = (
    "You are designing a new tool for an agent. The user wants a concrete capability. "
    "Return only a single JSON object, no prose or Markdown fences:\n"
    "{\n"
    '  "explanation": "<why this tool is needed and what it does, for the user review>",\n'
    '  "tool": {\n'
    '    "name": "<snake_case name>",\n'
    '    "description": "<one-line description for the agent>",\n'
    '    "parameters": {<JSON Schema object>},\n'
    '    "code": "<python function body: def run(**kwargs) -> dict, pure, deterministic, no network, no secrets>",\n'
    '    "tool_type": "function",\n'
    '    "sync": true,\n'
    '    "tests": [{"name": "<case name>", "input": {...}, "expect": {...}}]\n'
    "  }\n"
    "}\n"
    "Rules: code must be deterministic, side-effect free, and safe in a sandbox; tests must cover representative inputs."
)
