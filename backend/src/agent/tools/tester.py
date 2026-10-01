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

    async def run(
        self, definition: ToolDefinition, interpreter: str | None = None
    ) -> TestReport:
        report = TestReport(tool_name=definition.name)
        for test in definition.tests:
            outcome = await self._run_case(
                definition, test.name, test.input, test.expect, interpreter
            )
            if not outcome.passed:
                # 缺依赖是最常见的失败之一：把「缺的是哪一个、声明过没有」写在
                # 最前面，模型才知道是改代码还是补声明，而不是反复重试。
                hint = definition.dependency_hint(outcome.detail)
                if hint:
                    outcome.detail = f"{hint}\n{outcome.detail}"
            report.outcomes.append(outcome)
        return report

    async def _run_case(
        self,
        definition: ToolDefinition,
        name: str,
        inputs: dict,
        expected: dict,
        interpreter: str | None = None,
    ) -> TestOutcome:
        # 多文件项目：整份项目（入口 + 其它文件）一起进沙箱，测试跑的就是要注册的东西。
        result = await self.sandbox.execute(
            definition.code,
            inputs,
            files=definition.files,
            entry=definition.entry,
            interpreter=interpreter,
        )
        if not result.ok:
            # 把底层 stderr 一起给出来：只有「exit code 1」时，模型无从修改代码。
            detail = f"sandbox failure: {result.error or '未知错误'}"
            diagnostic = result.diagnostic()
            if diagnostic:
                detail += f"\n{diagnostic}"
            return TestOutcome(name, False, detail, result)
        if result.value != expected:
            diagnostic = result.diagnostic()
            detail = f"assertion mismatch: expected {expected!r}, got {result.value!r}"
            if diagnostic:
                detail += f"\n{diagnostic}"
            return TestOutcome(
                name,
                False,
                detail,
                result,
            )
        return TestOutcome(name, True, "assertion passed", result)
