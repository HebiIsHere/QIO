"""第二阶段冻结契约：后台知识维护的类型模型、状态枚举与交接数据结构。

契约：docs/qio-refactor-contract.md §12。

本模块是跨智能体的唯一类型来源：

* A（调度）：使用 MaintenanceSnapshot / MaintenanceRunOutcome 与调度状态枚举；
* B（维护动作）：产出/校验 MaintenanceOutput、ResolutionOutput，执行真实领域操作；
* C（独立界面 / API）：按 PendingItemView 等读取模型展示，不另造状态字符串。

模型配置一律 extra=forbid；展示给模型的 JSON Schema 由 json_schema_for 从这些类型生成
（与本地校验同源）。固定提示词在 agent.prompts（MAINTENANCE_SYSTEM /
MAINTENANCE_ANSWER_SYSTEM）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.tools.contracts import json_schema_for

#: 单次维护最多处理 / 生成的候选数量（配置可覆盖，但 schema 与本地校验共用这组上限）。
MAX_CANDIDATES_PER_RUN = 20
#: 单条知识正文进入维护上下文的上限（数据层裁剪，不截断序列化后的 JSON）。
MAX_KNOWLEDGE_CHARS = 2000
#: 单次最多涉及的知识 ID 数量（合并来源上限）。
MAX_KNOWLEDGE_IDS = 8
#: 单次最多引用的证据数量。
MAX_EVIDENCE_IDS = 20
#: 单个独立问题的长度上限。
MAX_QUESTION_CHARS = 500
#: 单次维护处理的变化条数上限（调度快照的默认值；配置可改）。
MAX_BATCH_CHANGES = 50

#: 维护任务统一的 limits（写进提示词的数据，也用于本地校验）。
MAINTENANCE_LIMITS: dict[str, int] = {
    "max_candidates": MAX_CANDIDATES_PER_RUN,
    "max_batch_changes": MAX_BATCH_CHANGES,
    "max_knowledge_chars": MAX_KNOWLEDGE_CHARS,
    "max_knowledge_ids": MAX_KNOWLEDGE_IDS,
    "max_evidence_ids": MAX_EVIDENCE_IDS,
    "max_question_chars": MAX_QUESTION_CHARS,
}


class MaintenanceAction(str, Enum):
    """后台维护候选动作。"""

    MERGE = "merge"
    NORMALIZE = "normalize"
    ASK = "ask"


class MaintenanceCandidate(BaseModel):
    """一条后台维护候选（模型输出的单项；结构合法性在本地强制执行）。"""

    model_config = ConfigDict(extra="forbid")

    action: MaintenanceAction
    knowledge_ids: list[str] = Field(default_factory=list, max_length=MAX_KNOWLEDGE_IDS)
    new_content: str | None = Field(default=None, max_length=MAX_KNOWLEDGE_CHARS)
    question: str | None = Field(default=None, max_length=MAX_QUESTION_CHARS)
    reason: str = Field(default="", max_length=1000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)

    @model_validator(mode="after")
    def _check_action_shape(self) -> "MaintenanceCandidate":
        ids = [item.strip() for item in self.knowledge_ids if item.strip()]
        if len(ids) != len(set(ids)):
            raise ValueError("knowledge_ids 不得重复")
        content = (self.new_content or "").strip()
        question = (self.question or "").strip()
        if self.action is MaintenanceAction.MERGE:
            if len(ids) < 2:
                raise ValueError("merge 至少需要两个不同 knowledge_id")
            if not content:
                raise ValueError("merge 必须提供非空 new_content")
            if question:
                raise ValueError("merge 不得提供 question")
        elif self.action is MaintenanceAction.NORMALIZE:
            if len(ids) != 1:
                raise ValueError("normalize 恰好需要一个 knowledge_id")
            if not content:
                raise ValueError("normalize 必须提供非空 new_content")
            if question:
                raise ValueError("normalize 不得提供 question")
        else:  # ask
            if not question:
                raise ValueError("ask 必须提供具体 question")
            if content:
                raise ValueError("ask 不得提供 new_content")
        return self


class MaintenanceOutput(BaseModel):
    """后台维护任务的完整结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[MaintenanceCandidate] = Field(
        default_factory=list, max_length=MAX_CANDIDATES_PER_RUN
    )


class ResolutionKind(str, Enum):
    RESOLVED = "resolved"
    NEEDS_CLARIFICATION = "needs_clarification"
    NO_CHANGE = "no_change"
    DEFERRED = "deferred"


class ResolutionAction(str, Enum):
    REVISE = "revise"
    REVOKE = "revoke"


class ResolutionChange(BaseModel):
    """独立回答里的一条真实修改（结构合法性本地强制执行）。"""

    model_config = ConfigDict(extra="forbid")

    action: ResolutionAction
    knowledge_id: str = Field(min_length=1)
    new_content: str | None = Field(default=None, max_length=MAX_KNOWLEDGE_CHARS)

    @model_validator(mode="after")
    def _check_action_shape(self) -> "ResolutionChange":
        content = (self.new_content or "").strip()
        if self.action is ResolutionAction.REVISE and not content:
            raise ValueError("revise 必须提供非空 new_content")
        if self.action is ResolutionAction.REVOKE and content:
            raise ValueError("revoke 不得提供 new_content")
        return self


class ResolutionOutput(BaseModel):
    """独立回答任务的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    resolution: ResolutionKind
    changes: list[ResolutionChange] = Field(default_factory=list, max_length=MAX_KNOWLEDGE_IDS)
    question: str | None = Field(default=None, max_length=MAX_QUESTION_CHARS)

    @model_validator(mode="after")
    def _check_resolution_shape(self) -> "ResolutionOutput":
        question = (self.question or "").strip()
        if self.resolution is ResolutionKind.NEEDS_CLARIFICATION:
            if not question:
                raise ValueError("needs_clarification 必须提供具体问题")
            if self.changes:
                raise ValueError("needs_clarification 不得同时应用修改")
        elif self.resolution is ResolutionKind.DEFERRED:
            if self.changes:
                raise ValueError("deferred 不得应用修改")
            if question:
                raise ValueError("deferred 不得提供新问题")
        elif self.resolution is ResolutionKind.NO_CHANGE:
            if self.changes:
                raise ValueError("no_change 不得应用修改")
            if question:
                raise ValueError("no_change 不得提供新问题")
        else:  # resolved
            if question:
                raise ValueError("resolved 不得提供新问题")
            if not self.changes:
                raise ValueError("resolved 必须至少包含一条 revise 或 revoke")
        return self


class PendingKind(str, Enum):
    """「记忆待确认」入口里的两类事项；处理程序必须区分。"""

    KNOWLEDGE_PROPOSAL = "knowledge_proposal"  # 主对话新增知识待审批（复用知识面板流程）
    MAINTENANCE_QUESTION = "maintenance_question"  # 后台维护提出的独立问题


class PendingStatus(str, Enum):
    AWAITING_ANSWER = "awaiting_answer"
    AWAITING_APPROVAL = "awaiting_approval"
    PROCESSING = "processing"
    PROCESSED = "processed"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    FAILED = "failed"


class PendingAction(str, Enum):
    ANSWER = "answer"
    APPROVE = "approve"
    APPROVE_MODIFIED = "approve_modified"
    REJECT = "reject"
    DEFER = "defer"


class BatchStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_PARTIAL = "rollback_partial"


class MaintenanceTrigger(str, Enum):
    CHANGE_COUNT = "change_count"
    WAITING_TURNS = "waiting_turns"


class MaintenanceCandidateStatus(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"  # 候选本身不合法 / 不满足领域约束，原知识保持不动
    ASKED = "asked"
    FAILED = "failed"


#: 后台写入的来源键（provenance.source）；统一由 API 映射成人话标签。
SOURCE_BACKGROUND_EXTRACTION = "background_extraction"
SOURCE_MAINTENANCE_MERGE = "maintenance_merge"
SOURCE_MAINTENANCE_NORMALIZE = "maintenance_normalize"
SOURCE_MAINTENANCE_RESTORE = "maintenance_restore"
SOURCE_MAIN_CHAT = "main_chat"
SOURCE_ONBOARDING = "onboarding"

#: 这些都算「后台整理」：后台写入、API 读取、界面筛选共用同一组键。
MAINTENANCE_SOURCE_KEYS: tuple[str, ...] = (
    SOURCE_BACKGROUND_EXTRACTION,
    SOURCE_MAINTENANCE_MERGE,
    SOURCE_MAINTENANCE_NORMALIZE,
    SOURCE_MAINTENANCE_RESTORE,
)

#: 后台写入不参与「知识变化计数」（避免维护自身形成循环触发）。
MAINTENANCE_ORIGINS: frozenset[str] = frozenset(
    {SOURCE_MAINTENANCE_MERGE, SOURCE_MAINTENANCE_NORMALIZE, SOURCE_MAINTENANCE_RESTORE}
)


def _field_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ()))
        errors.append(f"{location}: {item.get('msg', 'invalid')}")
    return errors


def _load_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """只接受单个 JSON 对象；服务器输出不应带 Markdown 围栏或解释正文。"""
    try:
        payload = json.loads((text or "").strip())
    except (TypeError, ValueError) as exc:
        return None, f"不是合法 JSON：{exc}"
    if not isinstance(payload, dict):
        return None, "输出必须是单个 JSON 对象"
    return payload, None


def validate_maintenance_output(text: str) -> tuple[MaintenanceOutput | None, list[str]]:
    """解析 + 本地校验维护输出；失败返回可读字段错误（供一次有依据的修复）。"""
    payload, error = _load_json_object(text)
    if payload is None:
        return None, [error or "输出不可解析"]
    try:
        return MaintenanceOutput.model_validate(payload), []
    except ValidationError as exc:
        return None, _field_errors(exc)


def validate_resolution_output(text: str) -> tuple[ResolutionOutput | None, list[str]]:
    """解析 + 本地校验独立回答输出。"""
    payload, error = _load_json_object(text)
    if payload is None:
        return None, [error or "输出不可解析"]
    try:
        return ResolutionOutput.model_validate(payload), []
    except ValidationError as exc:
        return None, _field_errors(exc)


def maintenance_output_schema() -> dict[str, Any]:
    """展示给模型的维护输出 JSON Schema（与本地校验同一类型）。"""
    return json_schema_for(MaintenanceOutput)


def resolution_output_schema() -> dict[str, Any]:
    """展示给模型的独立回答 JSON Schema（与本地校验同一类型）。"""
    return json_schema_for(ResolutionOutput)


# ---------------------------------------------------------------------------
# A / B 交接数据结构（冻结）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaintenanceSnapshot:
    """调度器交给维护执行器的处理快照（A 产出，B 消费）。

    * 只包含本轮允许处理的变化与知识；期间新增变化留给后续批次；
    * limits 来自 MAINTENANCE_LIMITS（配置可覆盖），B 用它裁剪输入与候选数；
    * 不包含主对话上下文 / 回答标记（维护任务独立运行）。
    """

    batch_seq: int
    trigger: MaintenanceTrigger
    change_seq_max: int
    pending_change_count: int
    waiting_turns: int
    change_ids: tuple[int, ...] = ()
    knowledge_ids: tuple[str, ...] = ()
    limits: Mapping[str, int] = field(default_factory=lambda: dict(MAINTENANCE_LIMITS))


@dataclass(frozen=True)
class MaintenanceRunOutcome:
    """B 的一轮维护结果（A 据此推进处理位置、记录失败与重试退避）。"""

    ok: bool
    status: str
    batch_id: str | None = None
    processed_change_seq: int | None = None
    applied: int = 0
    asked: int = 0
    rejected: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MaintenanceAnswerOutcome:
    """B 处理一条独立问题回答的结果（C 的 API 返回给界面）。"""

    ok: bool
    status: str
    item_id: str
    resolution: str | None = None
    applied_changes: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# C 的读取模型（API 返回形状；字段名冻结）
# ---------------------------------------------------------------------------


class PendingKnowledgeRef(BaseModel):
    """待确认事项涉及的一条知识版本（前后对比 / 来源展示用）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    content: str
    category: str
    scope: list[str] = Field(default_factory=list)
    epistemic_status: str | None = None
    source: str | None = None
    state: str | None = None


class PendingItemView(BaseModel):
    """「记忆待确认」列表项（两类事项共用；handler 决定处理程序）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: PendingKind
    status: PendingStatus
    handler: str
    title: str
    question: str | None = None
    knowledge: list[PendingKnowledgeRef] = Field(default_factory=list)
    before: list[dict[str, Any]] = Field(default_factory=list)
    after: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    batch_id: str | None = None
    error: str | None = None
