"""每一轮的「后端事实」台账，以及最终答复末尾的事实注记。

为什么需要这一层（见 docs/superpowers/specs/2026-09-30-final-answer-fact-check-design.md）：

* `core/tool_feedback.py` 只负责「这一次调用」如实回填给模型；
* 「本轮到底做了什么」（哪些工具最终失败、哪个开发任务在哪个版本上有没有测试证据）
  以前没有任何地方记得住，于是模型说「测试全部通过、已经可以使用」时，谁也拦不住。

这里收两种来源，都在内存里、每轮一份：

* 工具调用的最终结局（同一个工具后写覆盖先写：本轮最后一次结局才算数）；
* 开发类工具上报的任务事实（`ToolResult.facts["dev_task"]`）。

它不落盘、不做权限判断、不改模型写过的字；只做两件事：
给出**未解决失败**的人话列表，以及决定要不要给最终答复补一段注记。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 工具终态里的两个取值（与 core/tool_feedback.py 一致；那边有对应常量，
# tests/test_turn_facts.py 会守住「这里不写错字」）。
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 开发任务的测试证据状态（与 tools/dev_workspace.py 的取值一致）。
DEV_TEST_NONE = "none"
DEV_TEST_CURRENT = "current"
DEV_TEST_STALE = "stale"
_TEST_STATES = frozenset({DEV_TEST_NONE, DEV_TEST_CURRENT, DEV_TEST_STALE})

# 知识提交（propose_knowledge）的结局事实：由 ToolResult.facts["knowledge_proposal"]
# 给出（state + 稳定 code）。界面与最终答复校准都只认这些程序字段。
KNOWLEDGE_PROPOSAL_KEY = "knowledge_proposal"
KNOWLEDGE_FAILED = "failed"
KNOWLEDGE_REJECTED = "rejected"
KNOWLEDGE_AWAITING_APPROVAL = "awaiting_approval"
KNOWLEDGE_ACTIVE = "active"
_KNOWLEDGE_STATES = frozenset(
    {KNOWLEDGE_FAILED, KNOWLEDGE_REJECTED, KNOWLEDGE_AWAITING_APPROVAL, KNOWLEDGE_ACTIVE}
)

# 稳定错误码 → 可读说明（只认程序字段，不从中文错误文本猜）。
_KNOWLEDGE_FAILURE_LABELS = {
    "evidence_not_found": "引用依据未通过核对",
    "evidence_unsupported": "引用依据不足以支持这条结论",
    "invalid_scope": "归属范围无效",
    "invalid_arguments": "提交参数不合法",
    "rejected_content": "这条内容你之前明确拒绝过",
    "create_failed": "创建待审批条目失败",
}

# 注记的硬约束：最多列这么多条失败，其余归并成一句；总长有上限。
MAX_ANNOTATION_ITEMS = 3
DEFAULT_ANNOTATION_LIMIT = 600

ANNOTATION_HEADER = "—— 系统核对（后端事实，不是模型的说法）："
ANNOTATION_FOOTER = "这几项没有通过验证，不能当作「已完成 / 可使用」。"


def _one_line(text: Any) -> str:
    """把任意文本压成一行（注记里每条只能占一行）。"""
    return " ".join(str(text or "").split())


def _no_braces(text: str) -> str:
    """注记文案里不留 ASCII 花括号。

    后台维护轮会从最终答复里用 `re.search(r"\\{.*\\}")` 取 JSON（见
    services/maintenance.py）；那种轮次用的是空注册表、本来就不会有注记，
    但这条约束很便宜，留着比解释为什么不需要更省事。
    """
    return text.replace("{", "｛").replace("}", "｝")


def _redact_and_limit(text: str, limit: int) -> str:
    from agent.trace.redact import redact_text

    cleaned = _no_braces(redact_text(text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "\n…[核对说明已截断]"


@dataclass(frozen=True)
class DevTaskFact:
    """某个开发任务在某一刻的事实（由开发类工具上报）。"""

    task_id: str
    tool_name: str = ""
    phase: str | None = None
    version: str | None = None            # 工作区内容摘要 = 「哪一版」
    submitted: bool = False
    # 这个任务是「必须要有测试」的类型吗（subagent 型工具不需要确定性测试）
    requires_tests: bool = True
    test_state: str = DEV_TEST_NONE       # none / current / stale
    test_passed: bool | None = None
    test_summary: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "DevTaskFact | None":
        """从 `ToolResult.facts["dev_task"]` 还原；形状不对就返回 None（不记账）。"""
        if not isinstance(payload, dict):
            return None
        task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
        if not task_id:
            return None
        raw_test = payload.get("test")
        test = raw_test if isinstance(raw_test, dict) else {}
        state = str(test.get("state") or DEV_TEST_NONE)
        if state not in _TEST_STATES:
            state = DEV_TEST_NONE
        passed = test.get("passed")
        summary = test.get("summary")
        return cls(
            task_id=task_id,
            tool_name=str(payload.get("tool_name") or ""),
            phase=payload.get("phase") if isinstance(payload.get("phase"), str) else None,
            version=payload.get("version") if isinstance(payload.get("version"), str) else None,
            submitted=bool(payload.get("submitted")),
            requires_tests=bool(payload.get("requires_tests", True)),
            test_state=state,
            test_passed=passed if isinstance(passed, bool) else None,
            test_summary=str(summary) if isinstance(summary, str) else None,
        )


class TurnFacts:
    """一轮里发生过的、后端能证实的事实。"""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._dev_tasks: dict[str, DevTaskFact] = {}
        self._declaration: dict[str, Any] | None = None
        # 本轮知识提交的结局：**按可追踪身份分别保留**（见 record_knowledge_proposal）。
        # 同一身份（fingerprint / knowledge_id）的后续结局更新这一项；没有身份的
        # 多次提交各自保留，绝不用最后一次覆盖前一次。
        self._knowledge_proposals: list[dict[str, Any]] = []

    # -- 记账 -------------------------------------------------------------

    def record_tool(
        self,
        *,
        call_id: str = "",
        tool_name: str,
        ok: bool,
        status: str,
        category: str | None = None,
        error: str | None = None,
    ) -> None:
        """记一次工具调用的终态；同一工具后写覆盖先写。"""
        name = str(tool_name or "").strip()
        if not name:
            return
        self._tools[name] = {
            "call_id": str(call_id or ""),
            "ok": bool(ok),
            "status": str(status or ""),
            "category": category,
            "error": _one_line(error),
        }

    def record_dev_task(self, fact: DevTaskFact) -> None:
        """记一个开发任务的最新事实（同一任务后写覆盖先写）。"""
        if fact is None or not fact.task_id:
            return
        self._dev_tasks[fact.task_id] = fact

    def record_facts(self, facts: Any) -> None:
        """收一次工具结果里的 `facts`：开发任务事实、结论声明与知识提交结局。"""
        if not isinstance(facts, dict):
            return
        fact = DevTaskFact.from_payload(facts.get("dev_task"))
        if fact is not None:
            self.record_dev_task(fact)
        declaration = facts.get("declaration")
        if isinstance(declaration, dict):
            self.record_declaration(
                accepted=bool(declaration.get("accepted")),
                basis=declaration.get("basis"),
                claims=declaration.get("claims"),
            )
        proposal = facts.get(KNOWLEDGE_PROPOSAL_KEY)
        if isinstance(proposal, dict):
            self.record_knowledge_proposal(
                state=proposal.get("state"),
                code=proposal.get("code"),
                knowledge_id=proposal.get("knowledge_id"),
                fingerprint=proposal.get("fingerprint"),
            )

    def record_knowledge_proposal(
        self,
        *,
        state: Any,
        code: Any,
        knowledge_id: Any = None,
        fingerprint: Any = None,
    ) -> None:
        """记一次知识提交的结局（按可追踪身份分别保留）。

        只认程序字段：state 必须是我们认识的四种之一，code 必须非空；
        形状不对就忽略（宁可不记，也不编一个「已经提交」）。

        身份 = 工具给的 fingerprint（同一内容/类别/范围）或 knowledge_id：
        * 同一身份的后续结局**更新这一项**（失败后重试成功不会永久保留旧失败）；
        * 没有身份关联的多次提交**各自保留**（A 失败、B 等待审批必须都能看见，
          不能因为最后一次成功就把前一次失败抹掉）。
        """
        state_text = str(state or "").strip()
        code_text = str(code or "").strip()
        if state_text not in _KNOWLEDGE_STATES or not code_text:
            return
        record: dict[str, Any] = {"state": state_text, "code": code_text}
        fingerprint_text = str(fingerprint or "").strip()
        if fingerprint_text:
            record["fingerprint"] = fingerprint_text
        knowledge_id_text = str(knowledge_id or "").strip()
        if knowledge_id_text:
            record["knowledge_id"] = knowledge_id_text
        identity = fingerprint_text or knowledge_id_text
        if identity:
            for index, previous in enumerate(self._knowledge_proposals):
                previous_identity = str(
                    previous.get("fingerprint") or previous.get("knowledge_id") or ""
                ).strip()
                if previous_identity == identity:
                    self._knowledge_proposals[index] = record
                    return
        self._knowledge_proposals.append(record)

    def record_declaration(
        self,
        *,
        accepted: bool,
        basis: str | None = None,
        claims: Any = None,
    ) -> None:
        """记一次结论声明（`declare_completion`）的核对结果。"""
        self._declaration = {
            "accepted": bool(accepted),
            "basis": _one_line(basis),
            "claims": [str(c) for c in claims] if isinstance(claims, (list, tuple)) else [],
        }

    # -- 读 ---------------------------------------------------------------

    @property
    def knowledge_proposals(self) -> list[dict]:
        """本轮知识提交的结局清单（按身份去重后的顺序；没有结构化事实时为空）。"""
        return [dict(record) for record in self._knowledge_proposals]

    @property
    def knowledge_proposal(self) -> dict | None:
        """本轮最后一次知识提交结局（兼容读取）；没有结构化事实时为 None（不猜）。"""
        if not self._knowledge_proposals:
            return None
        return dict(self._knowledge_proposals[-1])

    @property
    def declaration_accepted(self) -> bool:
        return bool(self._declaration and self._declaration.get("accepted"))

    @property
    def declaration(self) -> dict | None:
        """被接受的声明（给落库与 TURN_END 用）；没有对上的声明时为 None。"""
        if not self.declaration_accepted:
            return None
        record = dict(self._declaration or {})
        record.setdefault("basis", "")
        record.setdefault("claims", [])
        return record

    def unresolved(self) -> list[str]:
        """本轮「没有通过验证」的事实，一条一行（已压成单行）。"""
        items: list[str] = []
        # 每一项未解决的知识提交单独列出：不同提交不能互相覆盖（同一身份的失败
        # 若已被后续成功更新，记录本身已经不是 failed，自然不会再出现在这里）。
        knowledge_failures = [
            record
            for record in self._knowledge_proposals
            if record.get("state") in (KNOWLEDGE_FAILED, KNOWLEDGE_REJECTED)
        ]
        for record in knowledge_failures:
            # 知识提交失败：说人话，不把技术错误原文当结论（原文在工具卡展开详情里）。
            label = _KNOWLEDGE_FAILURE_LABELS.get(str(record.get("code") or ""))
            items.append(f"知识提交未成功，{label}" if label else "知识提交未成功")
        for name, record in self._tools.items():
            if record.get("status") != STATUS_FAILED:
                continue
            # 同一失败已经有可读的结构化说明时，不再重复一遍技术原文。
            if name == "propose_knowledge" and knowledge_failures:
                continue
            reason = record.get("error") or "（没有给出失败原因）"
            items.append(f"{name} 执行失败：{reason}")
        for fact in self._dev_tasks.values():
            label = f"任务 {fact.task_id}"
            if fact.test_state == DEV_TEST_STALE:
                items.append(
                    f"{label} 的测试证据已失效：通过之后内容又改过，需重跑"
                    if fact.test_passed
                    else f"{label} 的测试失败且内容已变更，需重跑"
                )
            elif fact.test_state == DEV_TEST_CURRENT and fact.test_passed is False:
                items.append(
                    f"{label} 的测试失败：{fact.test_summary or '没有给出摘要'}"
                )
            elif fact.requires_tests and fact.test_state == DEV_TEST_NONE:
                items.append(f"{label} 还没有测试证据")
        return items

    def annotation(self, limit: int = DEFAULT_ANNOTATION_LIMIT) -> str | None:
        """要追加到最终答复末尾的事实注记；None = 什么都不加。"""
        # 已经按后端账本声明过结论的轮次不再重复念一遍。
        if self.declaration_accepted:
            return None
        items = self.unresolved()
        if not items:
            return None
        shown = items[:MAX_ANNOTATION_ITEMS]
        lines = [ANNOTATION_HEADER]
        lines.extend(f"· {item}" for item in shown)
        hidden = len(items) - len(shown)
        if hidden > 0:
            lines.append(f"· 另有 {hidden} 项未通过验证。")
        lines.append(ANNOTATION_FOOTER)
        return _redact_and_limit("\n".join(lines), limit)
