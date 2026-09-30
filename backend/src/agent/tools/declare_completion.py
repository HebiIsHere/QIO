"""declare_completion：把「完成 / 可用」这类结论交给后端核对。

见 docs/superpowers/specs/2026-09-30-final-answer-fact-check-design.md。

模型保留说明文字的自由，但「这个任务测试通过了 / 已注册 / 现在能用」是**可以被
后端证实或否证**的结论：它们必须来自工作区状态、注册表与持久层，不能由模型自己
宣布。这个工具就是那条通道 —— 全部对得上才算数，对不上就逐条说清缺什么。

它只读：不改任务状态、不触发审批、不注册任何东西。
"""

from __future__ import annotations

from typing import Any

from agent.prompts import TOOL_DECLARE_COMPLETION_DESC
from agent.tools.base import Tool, ToolResult

CLAIM_TEST_PASSED = "test_passed"
CLAIM_REGISTERED = "registered"
CLAIM_USABLE = "usable"
# 允许声明的结论词表：刻意很小，只收后端能证实的三种。
CLAIMS = (CLAIM_TEST_PASSED, CLAIM_REGISTERED, CLAIM_USABLE)


class DeclareCompletionTool(Tool):
    name = "declare_completion"
    description = TOOL_DECLARE_COMPLETION_DESC
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "开发任务（工作区）id"},
            "version": {
                "type": "string",
                "description": "要声明的版本 = 工作区内容摘要（dev_list_tasks 或工具结果里给出的那一串）",
            },
            "claims": {
                "type": "array",
                "items": {"type": "string", "enum": list(CLAIMS)},
                "description": (
                    "要核对的结论：test_passed（这一版有测试通过证据）、"
                    "registered（这一版提交成功且已注册）、"
                    "usable（现在可以使用，要求前面两条都成立）"
                ),
            },
        },
        "required": ["task_id", "version", "claims"],
    }
    # 只读核对：可与其它并发安全工具并行
    is_concurrency_safe = True

    def __init__(self, workspaces, registry, tool_store=None) -> None:
        self.workspaces = workspaces
        self.registry = registry
        self.tool_store = tool_store

    async def run(self, **kwargs: Any) -> ToolResult:
        task_id = str(kwargs.get("task_id") or "").strip()
        version = str(kwargs.get("version") or "").strip()
        claims, error = _normalize_claims(kwargs.get("claims"))
        if error:
            return self._reject(task_id, version, [], [error], [])
        status = self.workspaces.status(task_id) if task_id else {}
        if not status:
            return self._reject(
                task_id, version, claims, [f"找不到开发任务：{task_id or '（没有传 task_id）'}"], []
            )

        current_version = status.get("content_digest") or ""
        if not version or version != current_version:
            return self._reject(
                task_id,
                version,
                claims,
                [
                    "声明的版本不是当前版本："
                    f"现在这一版是 {str(current_version)[:12] or '（未知）'}，"
                    f"你声明的是 {version[:12] or '（没有传 version）'}。"
                    "版本变了就要重跑测试，再用新的摘要声明。"
                ],
                [],
            )

        definition = self.workspaces.read_definition(task_id)
        tool_name = getattr(definition, "name", None)
        requires_tests = definition is None or definition.tool_type != "subagent"
        facts = _Facts(
            version=current_version,
            test_passed=bool(status.get("test_evidence_current"))
            and status.get("last_test_passed") is True,
            test_state=status.get("evidence_state") or "none",
            test_summary=status.get("last_test_summary"),
            submitted=bool(status.get("submitted")),
            submitted_digest=status.get("submitted_digest"),
            tool_name=tool_name,
            registered=bool(tool_name) and self.registry.get(tool_name) is not None
            and (self.tool_store is None or self.tool_store.load(tool_name) is not None),
            requires_tests=requires_tests,
        )

        missing: list[str] = []
        for claim in claims:
            reason = facts.check(claim)
            if reason:
                missing.append(f"{_claim_label(claim)}：{reason}")
        if missing:
            return self._reject(task_id, version, claims, missing, facts.basis_lines())

        basis = "；".join(facts.basis_lines())
        return ToolResult(
            ok=True,
            content=(
                f"已核对：{'、'.join(_claim_label(c) for c in claims)} 与后端记录一致。\n"
                f"依据：{basis}"
            ),
            facts={
                "declaration": {
                    "accepted": True,
                    "task_id": task_id,
                    "version": version,
                    "claims": list(claims),
                    "basis": basis,
                    "missing": [],
                }
            },
        )

    def _reject(
        self,
        task_id: str,
        version: str,
        claims: list[str],
        missing: list[str],
        basis_lines: list[str],
    ) -> ToolResult:
        lines = ["声明没有通过核对（以后端记录为准）："]
        lines.extend(f"- {item}" for item in missing)
        if basis_lines:
            lines.append("现在的后端记录：" + "；".join(basis_lines))
        lines.append("先补齐缺的那一步，再用当前版本摘要重新声明；没有证据就不要说「已完成 / 可使用」。")
        return ToolResult(
            ok=False,
            error="\n".join(lines),
            facts={
                "declaration": {
                    "accepted": False,
                    "task_id": task_id,
                    "version": version,
                    "claims": list(claims),
                    "basis": None,
                    "missing": list(missing),
                }
            },
        )


def _normalize_claims(raw: Any) -> tuple[list[str], str | None]:
    """校验声明词表；返回 (claims, 错误说明)。"""
    if raw is None:
        return [], "claims 必填：只能声明 " + "、".join(CLAIMS)
    if not isinstance(raw, (list, tuple)):
        return [], "claims 必须是数组"
    claims: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if not name:
            continue
        if name not in CLAIMS:
            return [], f"不认识的结论「{name}」：只能声明 " + "、".join(CLAIMS)
        if name not in claims:
            claims.append(name)
    if not claims:
        return [], "claims 不能为空：只能声明 " + "、".join(CLAIMS)
    return claims, None


def _claim_label(claim: str) -> str:
    return {
        CLAIM_TEST_PASSED: "测试通过",
        CLAIM_REGISTERED: "已注册",
        CLAIM_USABLE: "可以使用",
    }.get(claim, claim)


class _Facts:
    """核对用的后端事实（只装数据与判定，便于单测）。"""

    def __init__(
        self,
        *,
        version: str,
        test_passed: bool,
        test_state: str,
        test_summary: str | None,
        submitted: bool,
        submitted_digest: str | None,
        tool_name: str | None,
        registered: bool,
        requires_tests: bool,
    ) -> None:
        self.version = version
        self.test_passed = test_passed
        self.test_state = test_state
        self.test_summary = test_summary
        self.submitted = submitted
        self.submitted_digest = submitted_digest
        self.tool_name = tool_name
        self.registered = registered
        self.requires_tests = requires_tests

    def check(self, claim: str) -> str | None:
        """对得上返回 None；对不上返回「缺什么」的人话说明。"""
        if claim == CLAIM_TEST_PASSED:
            return None if self.test_passed else self._test_reason()
        if claim == CLAIM_REGISTERED:
            return None if self._registered() else self._registered_reason()
        if claim == CLAIM_USABLE:
            if not self._registered():
                return self._registered_reason()
            if self.requires_tests and not self.test_passed:
                return self._test_reason()
            return None
        return f"不认识的结论「{claim}」"

    def basis_lines(self) -> list[str]:
        lines = [f"版本 {self.version[:12]}"]
        if self.requires_tests:
            if self.test_passed:
                lines.append(f"测试 {self.test_summary or '通过'}")
            else:
                lines.append(f"测试证据 {self.test_state}（未通过）")
        else:
            lines.append("该工具类型不需要确定性测试")
        lines.append(
            f"已提交（注册为 {self.tool_name}）" if self._registered() else "未提交 / 未注册"
        )
        return lines

    # -- internals --------------------------------------------------------

    def _registered(self) -> bool:
        return (
            self.submitted
            and bool(self.submitted_digest)
            and self.submitted_digest == self.version
            and self.registered
        )

    def _test_reason(self) -> str:
        if self.test_state == "stale":
            return "测试证据已失效（通过之后内容又改过），需要重跑"
        if self.test_state == "current":
            return f"这一版测试没有通过：{self.test_summary or '没有给出摘要'}"
        return "没有这一版的测试证据"

    def _registered_reason(self) -> str:
        if not self.submitted:
            return "还没有提交（提交要走审批并注册）"
        if self.submitted_digest and self.submitted_digest != self.version:
            return "提交过的是旧版本，当前版本还没有提交"
        if not self.tool_name:
            return "工作区里的工具定义无效，读不到工具名"
        return f"注册表里没有「{self.tool_name}」（可能已被撤销）"
