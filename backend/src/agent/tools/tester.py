"""Cross-testing: deterministic assertions over sandboxed executions.

Primary signal is exact-match assertions; an optional LLM evaluator can be
attached later (v1.5) as a secondary check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.tools.sandbox import SandboxExecutor, SandboxResult
from agent.tools.spec import ToolDefinition


@dataclass
class TestOutcome:
    name: str
    passed: bool
    detail: str
    result: SandboxResult | None = None


@dataclass
class TestReport:
    tool_name: str
    outcomes: list[TestOutcome] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.outcomes) > 0 and all(o.passed for o in self.outcomes)

    @property
    def summary(self) -> str:
        total = len(self.outcomes)
        ok = sum(1 for o in self.outcomes if o.passed)
        return f"{ok}/{total} tests passed"


class ToolTester:
    def __init__(self, sandbox: SandboxExecutor) -> None:
        self.sandbox = sandbox

    async def run(self, definition: ToolDefinition) -> TestReport:
        report = TestReport(tool_name=definition.name)
        for test in definition.tests:
            outcome = await self._run_case(definition.code, test.name, test.input, test.expect)
            report.outcomes.append(outcome)
        return report

    async def _run_case(
        self, code: str, name: str, inputs: dict, expected: dict
    ) -> TestOutcome:
        result = await self.sandbox.execute(code, inputs)
        if not result.ok:
            return TestOutcome(name, False, f"sandbox failure: {result.error}", result)
        if result.value != expected:
            return TestOutcome(
                name,
                False,
                f"assertion mismatch: expected {expected!r}, got {result.value!r}",
                result,
            )
        return TestOutcome(name, True, "assertion passed", result)