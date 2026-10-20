"""propose_knowledge 工具（Phase 1 C）：主对话新增知识一律等待用户审批。

冻结契约（docs/qio-refactor-contract.md §6.1 / §7）：

* 参数模型是真的执行约束的 pydantic 模型（extra=forbid / enum / 长度），
  下发给模型的 schema 用 json_schema_for 从同一个模型生成，本地校验用
  validate_model —— 展示与校验同源。
* 必填 content / category / scope / epistemic_status / evidence_ids；
  evidence_ids 的每一个 id 都必须在本轮**程序证据目录**里存在。
* scope 由程序解析成真实归属节点（user 全局 / 真实话题 / 真实实体），
  不允许模型编造 id。
* 主对话新增一律 create -> submit（pending_review），绝不自动 verify / activate；
  工具结果用结构化字段说明 status=awaiting_approval，文案只说「已提交审批」。
* 幂等：同一内容 + 同一范围已有待审批条目 → 返回同一条目；已 active 的同内容
  不重复建。用户明确拒绝过的同 category+content → 结构化冲突，不静默重存。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.knowledge.evidence import (
    DEFAULT_EPISTEMIC_STATUS,
    EPISTEMIC_STATUSES,
    MAX_EVIDENCE_IDS,
    coerce_catalog,
    validate_epistemic_status,
)
from agent.knowledge.lifecycle import KnowledgeService, KnowledgeState
from agent.knowledge.rejection import rejection_record
from agent.knowledge.scope import MAX_SCOPE_NODES, SCOPE_TYPES, resolve_scope_request
from agent.memory.limits import MAX_CANDIDATE_CONTENT
from agent.prompts import ARGUMENT_DESCRIPTIONS, TOOL_DESCRIPTIONS
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for, validate_model

MAX_EVIDENCE_ID_LENGTH = 80

_CATEGORY_VALUES = (
    "user_profile",
    "agent_self",
    "goal",
    "general_fact",
    "tool_experience",
)


class KnowledgeScopeArgs(BaseModel):
    """知识归属范围：程序会把它解析成真实图节点。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["user", "topic", "entity"]
    node_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_SCOPE_NODES,
        description=ARGUMENT_DESCRIPTIONS["scope"],
    )


class ProposeKnowledgeArgs(BaseModel):
    """propose_knowledge 的完整参数（展示 schema 与本地校验同源）。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(
        min_length=1,
        max_length=MAX_CANDIDATE_CONTENT,
        description=ARGUMENT_DESCRIPTIONS["content"],
    )
    category: Literal[
        "user_profile", "agent_self", "goal", "general_fact", "tool_experience"
    ]
    scope: KnowledgeScopeArgs
    epistemic_status: Literal[
        "user_explicit",
        "shared_understanding",
        "qio_observed",
        "qio_inference",
    ] = Field(description=ARGUMENT_DESCRIPTIONS["epistemic_status"])
    evidence_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_EVIDENCE_IDS,
        description=ARGUMENT_DESCRIPTIONS["evidence_ids"],
    )


def _failure(
    code: str,
    message: str,
    *,
    recoverable: bool = True,
    data: dict[str, Any] | None = None,
    status: str = "failed",
) -> ToolResult:
    return ToolResult(
        ok=False,
        content=message,
        error=message,
        recoverable=recoverable,
        protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        status=status,
        code=code,
        data=dict(data or {}),
    )


class ProposeKnowledgeTool(Tool):
    name = "propose_knowledge"
    description = TOOL_DESCRIPTIONS["propose_knowledge"]
    parameters = json_schema_for(ProposeKnowledgeArgs)

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        evidence_provider: Callable[[], Any] | None = None,
        turn_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.conn = conn
        self.evidence_provider = evidence_provider
        self.turn_id_provider = turn_id_provider

    # -- helpers ----------------------------------------------------------

    def _catalog(self):
        if self.evidence_provider is None:
            return coerce_catalog(None)
        return coerce_catalog(self.evidence_provider())

    def _turn_id(self) -> str | None:
        if self.turn_id_provider is None:
            return None
        try:
            return self.turn_id_provider()
        except Exception:  # noqa: BLE001 - 归属信息是旁路，不阻断工具
            return None

    def _duplicate(
        self, ks: KnowledgeService, *, category: str, content: str, nodes: list[str]
    ):
        """同内容 + 同范围的已有条目（待审批 / 已验证 / 生效 / 草稿）。"""
        target = content.strip()
        wanted = set(nodes)
        for item in ks.list_items(category=category):
            if item.content.strip() != target:
                continue
            if set(item.node_ids or []) != wanted:
                continue
            if item.state in (
                KnowledgeState.DRAFT,
                KnowledgeState.PENDING_REVIEW,
                KnowledgeState.VERIFIED,
                KnowledgeState.ACTIVE,
            ):
                return item
        return None

    # -- run --------------------------------------------------------------

    async def run(self, **kwargs: Any) -> ToolResult:
        args, errors = validate_model(ProposeKnowledgeArgs, kwargs)
        if args is None:
            return _failure(
                "invalid_arguments",
                "参数不合法，请修正后重试：" + "；".join(errors[:5]),
                data={"field_errors": errors},
            )
        content = args.content.strip()
        if not content:
            return _failure("invalid_arguments", "content 不能为空")
        catalog = self._catalog()
        evidence_ids, missing = catalog.validate_evidence_ids(args.evidence_ids)
        if missing:
            return _failure(
                "evidence_not_found",
                "evidence_ids 必须来自本轮实际证据目录，未找到："
                + ", ".join(missing[:5]),
                data={"missing_evidence_ids": missing, "available_ids": catalog.ids()[:20]},
            )
        epistemic_reason = validate_epistemic_status(
            catalog, args.epistemic_status, evidence_ids
        )
        if epistemic_reason is not None:
            return _failure(
                "evidence_unsupported",
                epistemic_reason,
                data={"epistemic_status": args.epistemic_status},
            )
        nodes, entity_ids, scope_error = resolve_scope_request(
            self.conn,
            scope_type=args.scope.type,
            node_ids=args.scope.node_ids,
        )
        if scope_error is not None:
            return _failure(
                "invalid_scope",
                scope_error,
                data={"scope_type": args.scope.type, "node_ids": args.scope.node_ids},
            )
        # 用户明确拒绝过同一内容：不静默重存（返回结构化冲突 + 拒绝事实）。
        tombstone = rejection_record(
            self.conn, category=args.category, content=content
        )
        if tombstone is not None:
            return _failure(
                "rejected_content",
                "这条内容用户明确拒绝过，未再次保存：「" + content[:60] + "」",
                recoverable=False,
                status="conflict",
                data={"rejected": tombstone},
            )
        ks = KnowledgeService(self.conn)
        existing = self._duplicate(
            ks, category=args.category, content=content, nodes=nodes
        )
        if existing is not None:
            if existing.state is KnowledgeState.ACTIVE:
                return ToolResult(
                    ok=True,
                    content="这条知识已经是有效知识，无需重复提交。",
                    protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
                    status="completed",
                    code="already_active",
                    data={
                        "knowledge_id": existing.id,
                        "state": existing.state.value,
                        "created": False,
                    },
                )
            if existing.state in (
                KnowledgeState.PENDING_REVIEW,
                KnowledgeState.VERIFIED,
            ):
                return ToolResult(
                    ok=True,
                    content="这条知识已提交审批（待用户批准后才会作为有效知识使用）。",
                    protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
                    status="awaiting_approval",
                    code="already_awaiting_approval",
                    data={
                        "knowledge_id": existing.id,
                        "state": existing.state.value,
                        "created": False,
                    },
                )
            # draft：同一内容尚未被拒绝过 → 复用这一条重新提交（不重复建行）
            item = ks.submit(existing.id)
            return self._awaiting(item, created=False)

        provenance = {
            "source": "main_chat",
            "epistemic_status": args.epistemic_status,
            "evidence_ids": evidence_ids,
            "scope_type": args.scope.type,
            "proposed_by": "propose_knowledge",
        }
        turn_id = self._turn_id()
        if turn_id:
            provenance["turn_id"] = turn_id
        try:
            item = ks.create(
                category=args.category,
                content=content,
                node_ids=list(nodes),
                entity_ids=list(entity_ids),
                provenance=provenance,
            )
            item = ks.submit(item.id)
        except ValueError as exc:
            return _failure("create_failed", f"提交审批失败：{exc}", recoverable=False)
        return self._awaiting(item, created=True)

    def _awaiting(self, item, *, created: bool) -> ToolResult:
        return ToolResult(
            ok=True,
            content=(
                "已提交审批：「"
                + item.content[:60]
                + "」（等待用户批准后才会作为有效知识使用）"
            ),
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            status="awaiting_approval",
            code="awaiting_user_approval" if created else "resubmitted_for_approval",
            data={
                "knowledge_id": item.id,
                "state": item.state.value,
                "category": item.category,
                "epistemic_status": item.provenance.get("epistemic_status"),
                "evidence_ids": item.provenance.get("evidence_ids") or [],
                "scope_nodes": list(item.node_ids or []),
                "created": created,
            },
        )
