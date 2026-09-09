"""Tool definition contract for agent-created tools.

The MAIN model proposes a tool as JSON; local validation enforces the
schema before any testing or approval happens. The model never writes
directly to the registry — everything passes the lifecycle.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


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
    tool_type: Literal["function", "subagent"] = "function"
    sync: bool = True
    credential_ref: str | None = None
    model: str | None = None  # subagent tools: which model to run
    tests: list[TestCase] = Field(default_factory=list, max_length=20)
    subagent_budget: SubagentBudget | None = None

    @model_validator(mode="after")
    def _require_code_for_functions(self) -> "ToolDefinition":
        if self.tool_type == "function" and not self.code.strip():
            raise ValueError("function tools require code")
        if self.tool_type == "subagent":
            if self.subagent_budget is None:
                self.subagent_budget = SubagentBudget()
        return self


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
