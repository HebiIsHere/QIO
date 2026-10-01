"""Cross-testing: deterministic assertions over sandboxed executions.

Primary signal is exact-match assertions; an optional LLM evaluator can be
attached later (v1.5) as a secondary check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.tools.mock_services import MockFixture, MockServiceError, fixture_for_definition
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
    # 这次测试用了声明的模拟服务：结论里必须带上「不是真实服务验证」。
    simulated: bool = False

    @property
    def passed(self) -> bool:
        return len(self.outcomes) > 0 and all(o.passed for o in self.outcomes)

    @property
    def summary(self) -> str:
        total = len(self.outcomes)
        ok = sum(1 for o in self.outcomes if o.passed)
        text = f"{ok}/{total} tests passed"
        if self.simulated:
            text += "（模拟服务：没有访问真实服务，不等于真实服务已验证）"
        return text


def _mock_note(fixture: MockFixture | None) -> str:
    """用了模拟服务就要写在结论里：没有声明就什么都不加。"""
    if fixture is None:
        return ""
    return "\n" + fixture.note_for_result()


class ToolTester:
    def __init__(self, sandbox: SandboxExecutor) -> None:
        self.sandbox = sandbox

    async def run(
        self,
        definition: ToolDefinition,
        interpreter: str | None = None,
        container_image: str | None = None,
    ) -> TestReport:
        """跑一遍定义里的确定性用例。

        interpreter 是宿主专用环境的 Python；container_image 是按锁定清单准备好的依赖
        镜像。两者由调用方按执行器二选一（见 tools/tool_envs.py 的
        resolve_execution_environment）；两个都不给 = 随包环境。"""
        report = TestReport(tool_name=definition.name)
        try:
            fixture = fixture_for_definition(definition)
        except MockServiceError as exc:
            # 声明写错了不能静默忽略（那会变成「用什么都没测的东西换一个绿灯」）：
            # 每个用例都失败，并把原因原样写出来。
            reason = f"模拟服务声明有问题：{exc}"
            for name in [test.name for test in definition.tests] or ["mocks"]:
                report.outcomes.append(TestOutcome(name, False, reason, None))
            return report
        try:
            for test in definition.tests:
                outcome = await self._run_case(
                    definition,
                    test.name,
                    test.input,
                    test.expect,
                    interpreter,
                    fixture,
                    container_image,
                )
                if not outcome.passed:
                    # 缺依赖是最常见的失败之一：把「缺的是哪一个、声明过没有」写在
                    # 最前面，模型才知道是改代码还是补声明，而不是反复重试。
                    hint = definition.dependency_hint(outcome.detail)
                    if hint:
                        outcome.detail = f"{hint}\n{outcome.detail}"
                report.outcomes.append(outcome)
            report.simulated = fixture is not None
        finally:
            # 无论成败（含异常）都把这份夹具收掉。
            if fixture is not None:
                fixture.cleanup()
        return report

    async def _run_case(
        self,
        definition: ToolDefinition,
        name: str,
        inputs: dict,
        expected: dict,
        interpreter: str | None = None,
        fixture: MockFixture | None = None,
        container_image: str | None = None,
    ) -> TestOutcome:
        # 多文件项目：整份项目（入口 + 其它文件）一起进沙箱，测试跑的就是要注册的东西。
        # 声明了模拟服务的项目：把夹具的环境变量一起带进去（默认断网 + 假凭据）。
        # 容器依赖镜像只在真给了的时候才传：sandbox 的替身（测试里）不必认识这个参数。
        execution: dict = {"interpreter": interpreter}
        if container_image:
            execution["container_image"] = container_image
        result = await self.sandbox.execute(
            definition.code,
            inputs,
            extra_env=fixture.extra_env() if fixture is not None else None,
            files=definition.files,
            entry=definition.entry,
            **execution,
        )
        note = _mock_note(fixture)
        if not result.ok:
            # 把底层 stderr 一起给出来：只有「exit code 1」时，模型无从修改代码。
            detail = f"sandbox failure: {result.error or '未知错误'}"
            diagnostic = result.diagnostic()
            if diagnostic:
                detail += f"\n{diagnostic}"
            return TestOutcome(name, False, detail + note, result)
        if result.value != expected:
            diagnostic = result.diagnostic()
            detail = f"assertion mismatch: expected {expected!r}, got {result.value!r}"
            if diagnostic:
                detail += f"\n{diagnostic}"
            return TestOutcome(
                name,
                False,
                detail + note,
                result,
            )
        return TestOutcome(name, True, "assertion passed" + note, result)
