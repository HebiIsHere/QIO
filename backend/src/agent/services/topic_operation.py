"""话题操作身份、阶段与事实（本轮 P1 修复的共享契约，唯一权威定义）。

本模块只放**结构化事实**，不做任何自然语言理解：
* 语义判断（「用户是否主动要求」「是否值得拆题」）由本轮主循环模型在
  `switch_topic` / `create_topic` 的 `intent` 参数里声明，程序只消费这个
  受控通道的值，不再用关键词 / 正则 / 相似度阈值反推用户意图；
* 身份、版本、阶段、事实由本模块定义，所有调用方（loop / orchestrator /
  topic_application / topic_tools / api）只读这里的枚举与数据类，不各自造一套。

三层事实分开记录，任何一层失败都不允许被另一层的成功覆盖：

1. `TopicOperation`：一次逻辑话题操作的**身份**（operation_id 稳定，参数带版本）；
   状态拆两层（F02）：`DecisionStatus.decided`（用户 / 程序决定）与
   `AttemptStatus.attempt`（原工具执行到哪一步），任何一层成功都不能覆盖另一层；
2. `TopicStages`：这一步真实走到了哪里（工具成功 / 导航落实 / 归属搬迁 /
   上下文就绪 / 回答完成）；
3. `TopicPhase`：本轮输出边界（只有 ANSWERABLE 允许发布正式回答）。

参数口径（F07）：`execution_params`（只外层 strip）决定 param_version / 批准身份；
`normalize_topic_name` / `dedup_key` 宽松归一化只作查重证据，禁止用于身份判断。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


# ── 1. 语义来源（唯一受控通道：主循环模型写在工具调用参数里）──────────────

class IntentSource(str, Enum):
    """这次话题操作的来源。默认必须是 SUGGESTION（安全侧）。

    * USER_REQUEST：用户本轮明确要求现在执行这次具体切换 / 新建 → 直接执行原工具；
    * SUGGESTION：QIO 主动建议 → 走普通话题确认。
    """

    USER_REQUEST = "user_request"
    SUGGESTION = "suggestion"


INTENT_PARAM = "intent"
INTENT_SOURCE_PARAM = "intent_source"

#: 工具参数里允许出现的取值 → IntentSource。其它任何值一律按 SUGGESTION 处理。
INTENT_VALUES: dict[str, IntentSource] = {
    "user_request": IntentSource.USER_REQUEST,
    "user_requested": IntentSource.USER_REQUEST,
    "suggestion": IntentSource.SUGGESTION,
}

TOPIC_TOOL_KINDS: dict[str, str] = {
    "switch_topic": "topic_switch",
    "create_topic": "topic_create",
}


def intent_from_arguments(arguments: dict[str, Any] | None) -> IntentSource:
    """从工具参数读取语义来源；缺省 / 未知值 → SUGGESTION（不确认就不执行）。"""
    arguments = arguments if isinstance(arguments, dict) else {}
    raw = arguments.get(INTENT_PARAM)
    if raw is None:
        raw = arguments.get(INTENT_SOURCE_PARAM)
    return INTENT_VALUES.get(str(raw or "").strip().lower(), IntentSource.SUGGESTION)


# ── 2. 输出阶段（正式回答的发布边界）────────────────────────────────────

class TopicPhase(str, Enum):
    DECISION_OPEN = "decision_open"
    """本轮是否需要话题操作尚未确定：不得发布正式正文（interim=false）。"""

    AWAITING_CONFIRMATION = "awaiting_confirmation"
    """申请已建立，等待用户决定：不得发布正式正文，只显示受控处理状态。"""

    EXECUTING = "executing"
    """用户已批准，原工具执行中：不得发布正式正文。"""

    CONTEXT_PREPARING = "context_preparing"
    """话题已变更成功，目标上下文装配中：不得发布正式正文。"""

    ANSWERABLE = "answerable"
    """可以发布正式回答；本轮话题归属已经固定（迟到的导航请求不再改变归属）。"""

    RECOVERABLE_FAILURE = "recoverable_failure"
    """话题已变更但回答尚未完成：不得发布正式回答，等待显式恢复。"""


def publishes_formal_answer(phase: TopicPhase | str | None) -> bool:
    """唯一判定：只有 ANSWERABLE 允许发布正式正文 / 正式消息。"""
    if phase is None:
        return True  # 没有话题申请总线的普通轮（子任务 / 老路径）
    if isinstance(phase, TopicPhase):
        return phase is TopicPhase.ANSWERABLE
    try:
        # 注意：str(TopicPhase.ANSWERABLE) 是枚举的 repr，不是 "answerable"。
        return TopicPhase(str(phase).strip().lower()) is TopicPhase.ANSWERABLE
    except ValueError:
        return False

    # 说明：拿不准的值一律按「不可发布」处理（安全侧），不抛异常。


class OperationStatus(str, Enum):
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    EXECUTING = "executing"
    EXECUTED = "executed"
    CONTEXT_READY = "context_ready"
    ANSWERED = "answered"
    REFUSED = "refused"
    STALE = "stale"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            OperationStatus.ANSWERED,
            OperationStatus.REFUSED,
            OperationStatus.STALE,
            OperationStatus.FAILED,
        }


class ConflictStatus(str, Enum):
    """create_topic 查重冲突的独立状态：冲突 ≠ 创建成功，也 ≠ 整轮结束。"""

    NONE = "none"
    CONFLICT_PENDING = "conflict_pending"
    CONFLICT_CONFIRMED = "conflict_confirmed"
    CONFLICT_DECLINED = "conflict_declined"


class DecisionStatus(str, Enum):
    """决定层（F02）：用户 / 程序对这次操作做过什么决定。

    与执行层分开记录 —— 「用户明确要求执行」不等于「执行成功」，
    「用户拒绝」也不等于「工具失败」。只有这里的状态才决定旧批准能不能复用。
    """

    NONE = "none"
    """还没有任何决定（新操作 / 参数版本刚推进）。"""

    PENDING = "pending"
    """等待用户决定（普通申请等待中 / 申请超时后没有新决定）。"""

    APPROVED = "approved"
    """已批准（用户点同意，或本轮用户明确要求直接执行）。"""

    REFUSED = "refused"
    """用户拒绝 / 取消（含具体查重冲突被拒绝）。"""

    STALE = "stale"
    """申请失效：等待期间真实位置已改变。失效 ≠ 拒绝，不写拒绝证据。"""


class AttemptStatus(str, Enum):
    """执行层（F02）：原工具这次到底执行到哪一步。

    同 operation_id + 同 param_version 的合法重试只允许在
    RETRYABLE_FAILURE / CONFLICT_PENDING 上再次进入原工具；
    SUCCEEDED 只重放既有结果；REFUSED / STALE / TERMINAL_FAILURE 不重试。
    """

    NONE = "none"
    """尚未开始执行。"""

    EXECUTING = "executing"
    """原工具执行中。"""

    RETRYABLE_FAILURE = "retryable_failure"
    """可重试失败（如具体冲突确认超时 / 审批通道技术错误）：同一版本可再次进入原工具。"""

    CONFLICT_PENDING = "conflict_pending"
    """查重冲突待用户确认（同版本重试会再次进入工具内的具体冲突确认）。"""

    SUCCEEDED = "succeeded"
    """原工具真实成功；成功后的同版本重放只返回既有目标与结果。"""

    TERMINAL_FAILURE = "terminal_failure"
    """不可重试失败（技术错误 / 失效 / 具体冲突被拒绝由决定层表达）。"""


# ── 3. 操作身份 ─────────────────────────────────────────────────────────

#: R16（契约 §5）：continue_from_fragment 的工具导航命令 kind。它不是
#: topic_switch/topic_create 那种需要用户审批的话题申请，但同样需要稳定的
#: operation_id + param_version 命令身份（同 operation 参数变化推进 param_version）。
HISTORY_CONTINUE_KIND = "history_continue"


def new_operation_id(turn_id: str, kind: str) -> str:
    """一次逻辑话题操作的稳定身份（与名称无关；名称只是带版本的参数）。"""
    suffix = uuid.uuid4().hex[:12]
    return f"topop-{str(turn_id or 'turn')[:24]}-{str(kind or 'topic')[:16]}-{suffix}"


def strip_outer(value: Any) -> str:
    """只去掉外层空白（含首尾全角空格）；内部字符一个都不动。

    这是**执行参数**的清洗口径：真正落库 / 真正导航用的名称必须保留内部空格、
    引号、冒号等实际字符——它们变化时创建出来的话题就是另一个话题。
    """
    return str(value or "").strip()


def execution_params(kind: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """执行参数的规范形（F07）：决定 param_version 与批准身份。

    只保留真正影响执行结果的字段（reason / intent 等展示字段不参与版本）：

    * topic_switch → {"topic_id": <strip 后>}；
    * topic_create → {"name": <仅外层 strip 后的真实名称>}；
    * history_continue → {"fragment_id": <strip 后>}（R16：工具 continue 的命令身份参数）。

    内部空格、全角空格、引号、冒号等**任何实际存储会变化的字符都算参数变化**，
    旧版本的批准 / 冲突决定不能迁移到新版本。
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    if kind == "topic_switch":
        return {"topic_id": strip_outer(arguments.get("topic_id"))}
    if kind == HISTORY_CONTINUE_KIND:
        return {"fragment_id": strip_outer(arguments.get("fragment_id"))}
    return {"name": strip_outer(arguments.get("name"))}


def normalize_topic_name(value: Any) -> str:
    """宽松归一化：只用于查重 / 相似 / 幂等**证据**（F07）。

    会去掉空格、全角空格、引号、冒号等字符 —— 因此**禁止**用它决定
    param_version 或批准匹配（那会让「Data Science」与「DataScience」共享
    同一个批准身份，等于无确认地创建了另一个真实名称）。
    执行身份的唯一口径是 :func:`execution_params`。
    """
    text = str(value or "").strip()
    for ch in " 　\t\r\n\"'“”「」『』《》：:":
        text = text.replace(ch, "")
    return text


def dedup_key(kind: str, arguments: dict[str, Any] | None) -> str:
    """查重归一化键（证据）：只在相似 / 幂等判断里用，不参与批准身份。"""
    arguments = arguments if isinstance(arguments, dict) else {}
    if kind == "topic_switch":
        return strip_outer(arguments.get("topic_id"))
    return normalize_topic_name(arguments.get("name"))


def param_digest(kind: str, arguments: dict[str, Any] | None) -> str:
    """执行参数摘要（F07）：参数实质变化 → 值变化 → param_version 推进。"""
    params = execution_params(kind, arguments)
    return "|".join(f"{key}={params[key]}" for key in sorted(params))


@dataclass
class TopicOperation:
    """一次逻辑话题操作的权威身份与状态。

    生命周期：`operation_id` 不变；用户改名 / 改目标 → `param_version + 1`，
    旧版本的批准不能迁移到新版本（合法重试必须是「同 operation + 同参数版本」）。
    """

    operation_id: str
    turn_id: str
    kind: str  # topic_switch | topic_create
    intent_source: IntentSource = IntentSource.SUGGESTION
    params: dict[str, Any] = field(default_factory=dict)
    param_version: int = 1
    call_ids: list[str] = field(default_factory=list)
    route: str = "application"  # direct | application
    #: 本轮原始用户消息 id（恢复入口只重做未完成步骤时用它重装原问题，不新建消息）。
    user_message_id: str | None = None
    from_topic: str | None = None
    from_fragment: str | None = None
    base_nav_version: int | None = None
    approval_id: str | None = None
    target_topic_id: str | None = None
    new_name: str | None = None
    #: 决定层（F02）：用户 / 程序做过什么决定。与执行层分开，互不覆盖。
    decided: DecisionStatus = DecisionStatus.NONE
    #: 执行层（F02）：原工具执行到哪一步。
    attempt: AttemptStatus = AttemptStatus.NONE
    #: 最近一次参数的查重归一化（宽松形）：只作证据，不参与身份（F07）。
    dedup_name: str = ""
    #: 最近一次失败 / 等待的结构化代码（审批超时 / 冲突超时等），只作事实说明。
    failure_code: str = ""
    conflict: ConflictStatus = ConflictStatus.NONE
    stages: "TopicStages" = field(default_factory=lambda: TopicStages())

    # -- 状态投影（兼容既有读取方：loop 的发布前提 / 恢复记录）----------
    @property
    def status(self) -> OperationStatus:
        """把两层状态投影成既有 OperationStatus（只读；唯一事实仍是两层字段）。

        * 决定层失效 / 拒绝优先（即使之前执行过，也不能被说成成功）；
        * 执行层成功后按分步事实区分 EXECUTED / CONTEXT_READY / ANSWERED；
        * 执行中 / 待确认 / 已批准未执行 → 既有等待类状态（loop 据此阻止发布）。
        """
        if self.decided is DecisionStatus.STALE:
            return OperationStatus.STALE
        if self.decided is DecisionStatus.REFUSED:
            return OperationStatus.REFUSED
        if self.attempt is AttemptStatus.SUCCEEDED:
            if self.stages.answer_complete:
                return OperationStatus.ANSWERED
            if self.stages.context_ready:
                return OperationStatus.CONTEXT_READY
            return OperationStatus.EXECUTED
        if (
            self.decided is DecisionStatus.PENDING
            and self.failure_code == "approval_timeout"
        ):
            # 审批超时：本轮已终结（不会再弹、也不再执行），但**不是用户拒绝**，
            # 决定层仍如实记为 pending（没有新决定）。
            return OperationStatus.FAILED
        if self.attempt is AttemptStatus.TERMINAL_FAILURE:
            return OperationStatus.FAILED
        if self.attempt is AttemptStatus.EXECUTING:
            return OperationStatus.EXECUTING
        if self.decided is DecisionStatus.APPROVED:
            # 已批准但尚未真实执行（含冲突待确认 / 可重试失败）：仍算未完成。
            return OperationStatus.APPROVED
        if self.decided is DecisionStatus.PENDING:
            return OperationStatus.AWAITING_CONFIRMATION
        return OperationStatus.PENDING

    # -- 状态迁移（只在这里写两层字段，避免两处真相）--------------------
    def decide(self, decision: DecisionStatus) -> None:
        self.decided = DecisionStatus(decision)
        if self.decided is not DecisionStatus.APPROVED:
            self.failure_code = ""

    def begin_attempt(self) -> None:
        """原工具即将执行（同一版本重试也从这里进入）。"""
        self.attempt = AttemptStatus.EXECUTING
        self.failure_code = ""

    def fail_attempt(
        self,
        attempt: AttemptStatus,
        *,
        conflict: ConflictStatus | None = None,
        code: str = "",
    ) -> None:
        self.attempt = AttemptStatus(attempt)
        self.failure_code = str(code or "")
        if conflict is not None:
            self.conflict = ConflictStatus(conflict)

    def succeed_attempt(self) -> None:
        self.attempt = AttemptStatus.SUCCEEDED
        self.failure_code = ""
        self.conflict = ConflictStatus.NONE

    # -- 重试判定（F02 唯一口径）----------------------------------------
    def can_retry(self) -> bool:
        """同 operation + 同 param_version 是否允许再次进入原工具。

        只有「已批准（用户明确要求 / 用户点过同意）」且上次执行是
        可重试失败 / 冲突待确认时才允许；成功只重放、拒绝 / 失效不重试。
        """
        if self.decided is not DecisionStatus.APPROVED:
            return False
        return self.attempt in (
            AttemptStatus.NONE,
            AttemptStatus.RETRYABLE_FAILURE,
            AttemptStatus.CONFLICT_PENDING,
        )

    def retry_block_reason(self) -> str:
        """不允许重试时的结构化原因（给桥生成诚实的终态结果，不按文本猜）。"""
        if self.decided is DecisionStatus.STALE:
            return "stale"
        if self.decided is DecisionStatus.REFUSED:
            if self.conflict is ConflictStatus.CONFLICT_DECLINED:
                return "conflict_declined"
            return "refused"
        if self.attempt is AttemptStatus.SUCCEEDED:
            return "succeeded"
        if self.attempt is AttemptStatus.TERMINAL_FAILURE:
            return self.failure_code or "terminal_failure"
        if self.attempt is AttemptStatus.EXECUTING:
            return "executing"
        return "unavailable"

    # -- 参数版本 ------------------------------------------------------
    def update_params(self, arguments: dict[str, Any] | None) -> bool:
        """用新参数更新同一操作；**执行参数**实质变化才推进 param_version。

        F07：内部空格 / 全角空格 / 引号 / 冒号的实际变化都算参数变化；
        查重归一化（dedup_name）只更新证据，不参与比较。
        """
        params = execution_params(self.kind, arguments)
        self.dedup_name = dedup_key(self.kind, arguments)
        if params == self.params:
            return False
        self.params = params
        self.param_version += 1
        # 新版本 = 新身份：旧批准 / 旧冲突决定一律失效（F02/F07）。
        self.decided = DecisionStatus.NONE
        self.attempt = AttemptStatus.NONE
        self.failure_code = ""
        self.conflict = ConflictStatus.NONE
        return True

    def remember_call(self, call_id: str | None) -> None:
        value = str(call_id or "")
        if value and value not in self.call_ids:
            self.call_ids.append(value)

    # -- 事实 ----------------------------------------------------------
    def mark(self, field_name: str, value: bool = True) -> None:
        if field_name not in TopicStages.__dataclass_fields__:  # type: ignore[attr-defined]
            raise KeyError(field_name)
        setattr(self.stages, field_name, bool(value))

    def as_facts(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "turn_id": self.turn_id,
            "kind": self.kind,
            "intent_source": self.intent_source.value,
            "param_version": self.param_version,
            "route": self.route,
            "user_message_id": self.user_message_id,
            "from_topic": self.from_topic,
            "from_fragment": self.from_fragment,
            "base_nav_version": self.base_nav_version,
            "approval_id": self.approval_id,
            "status": self.status.value,
            "decided": self.decided.value,
            "attempt": self.attempt.value,
            "failure_code": self.failure_code,
            "conflict": self.conflict.value,
            "dedup_name": self.dedup_name,
            "params": dict(self.params),
            "stages": self.stages.as_dict(),
        }


@dataclass
class TopicStages:
    """一步一事实：绝不用一个成功状态覆盖其它步骤。"""

    tool_success: bool = False
    navigation_applied: bool = False
    turn_ownership: bool = False
    context_ready: bool = False
    answer_complete: bool = False
    #: 失败发生在哪一步（空 = 没有失败），用于恢复入口的真实说明。
    failed_stage: str = ""
    #: 失败原因（脱敏后短文本）。
    failure: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_success": self.tool_success,
            "navigation_applied": self.navigation_applied,
            "turn_ownership": self.turn_ownership,
            "context_ready": self.context_ready,
            "answer_complete": self.answer_complete,
            "failed_stage": self.failed_stage,
            "failure": self.failure,
        }


# ── 4. 进程内登记表（身份 / 幂等；不替代审批持久化）────────────────────

class TopicOperationRegistry:
    """按 (turn_id, operation_id) 登记本轮操作；有界，只做幂等与事实查询。

    * 不持久化：申请本身的持久化 / 有效期 / 单次消费仍由 `pending_approvals` 负责；
    * 跨轮不复用：新轮次新建 operation_id，旧布尔值 / 旧授权不会被继承。
    """

    def __init__(self, max_turns: int = 32, max_operations: int = 8) -> None:
        self._operations: dict[str, TopicOperation] = {}
        self._turn_order: list[str] = []
        self._max_turns = max(1, int(max_turns))
        self._max_operations = max(1, int(max_operations))
        self._by_approval: dict[str, str] = {}

    def begin(
        self,
        *,
        turn_id: str,
        kind: str,
        route: str,
        intent_source: IntentSource,
        arguments: dict[str, Any] | None,
        from_topic: str | None,
        from_fragment: str | None,
        base_nav_version: int | None,
        user_message_id: str | None = None,
        operation_id: str | None = None,
    ) -> TopicOperation:
        """取回（同轮同操作）或新建一条操作身份。"""
        turn_id = str(turn_id or "")
        existing = self.for_turn(turn_id, kind=kind)
        if existing is not None:
            existing.update_params(arguments)
            existing.route = route
            existing.intent_source = intent_source
            if from_topic:
                existing.from_topic = from_topic
            if from_fragment is not None:
                existing.from_fragment = from_fragment
            if base_nav_version is not None:
                existing.base_nav_version = base_nav_version
            if user_message_id:
                existing.user_message_id = str(user_message_id)
            self.remember(existing, arguments)
            return existing
        operation = TopicOperation(
            operation_id=operation_id or new_operation_id(turn_id, kind),
            turn_id=turn_id,
            kind=kind,
            intent_source=intent_source,
            params=execution_params(kind, arguments),
            dedup_name=dedup_key(kind, arguments),
            route=route,
            from_topic=from_topic,
            from_fragment=from_fragment,
            base_nav_version=base_nav_version,
            user_message_id=str(user_message_id) if user_message_id else None,
        )
        self.remember(operation, arguments)
        self._turn_slot(turn_id)
        return operation

    def remember(self, operation: TopicOperation, arguments: dict[str, Any] | None = None) -> None:
        if arguments is not None:
            operation.update_params(arguments)
        self._operations[operation.operation_id] = operation
        if operation.approval_id:
            self._by_approval[operation.approval_id] = operation.operation_id

    def for_turn(self, turn_id: str, *, kind: str | None = None) -> TopicOperation | None:
        """本轮最近一次（同 kind 的）操作；新轮次不会看到旧轮次的操作。"""
        turn_id = str(turn_id or "")
        for operation in reversed(list(self._operations.values())):
            if operation.turn_id != turn_id:
                continue
            if kind is not None and operation.kind != kind:
                continue
            return operation
        return None

    def for_turn_all(self, turn_id: str) -> list[TopicOperation]:
        turn_id = str(turn_id or "")
        return [op for op in self._operations.values() if op.turn_id == turn_id]

    def by_operation_id(self, operation_id: str | None) -> TopicOperation | None:
        return self._operations.get(str(operation_id or ""))

    def by_approval_id(self, approval_id: str | None) -> TopicOperation | None:
        operation_id = self._by_approval.get(str(approval_id or ""))
        return self._operations.get(operation_id) if operation_id else None

    def bind_approval(self, operation: TopicOperation, approval_id: str | None) -> None:
        value = str(approval_id or "")
        if not value:
            return
        operation.approval_id = value
        self._by_approval[value] = operation.operation_id

    def _turn_slot(self, turn_id: str) -> None:
        self._turn_order = [item for item in self._turn_order if item != turn_id]
        self._turn_order.append(turn_id)
        while len(self._turn_order) > self._max_turns:
            oldest = self._turn_order.pop(0)
            stale = [
                op.operation_id
                for op in self._operations.values()
                if op.turn_id == oldest
            ]
            for operation_id in stale:
                operation = self._operations.pop(operation_id, None)
                if operation is not None and operation.approval_id:
                    self._by_approval.pop(operation.approval_id, None)

    def reset(self) -> None:
        self._operations.clear()
        self._turn_order.clear()
        self._by_approval.clear()


# ── 5. 工具侧的当前操作（ContextVar；工具在轮次派生的 task 里读）────────

_CURRENT_OPERATION: ContextVar[TopicOperation | None] = ContextVar(
    "qio_topic_operation", default=None
)


def bind_operation(operation: TopicOperation | None) -> Token:
    return _CURRENT_OPERATION.set(operation)


def unbind_operation(token: Token) -> None:
    try:
        _CURRENT_OPERATION.reset(token)
    except (ValueError, LookupError):  # 跨上下文 token 失效：当没有绑定处理
        pass


def current_operation() -> TopicOperation | None:
    return _CURRENT_OPERATION.get()


def operation_scope(operation: TopicOperation | None):
    """`with operation_scope(op):` 的同步上下文管理器（工具执行用）。"""
    import contextlib

    @contextlib.contextmanager
    def _scope():
        token = bind_operation(operation)
        try:
            yield operation
        finally:
            unbind_operation(token)

    return _scope()


# ── 5.5 导航命令身份（契约 §5/§14.8；工具路径复用 operation_id+param_version）──

def navigation_identity(operation: TopicOperation):
    """工具真实执行的导航命令身份：operation:<operation_id>:<param_version>。

    返回 A 的 `NavCommandIdentity`（frozen）。延迟导入：position_state 是 R16 新模块，
    旧代码路径（迁移未就绪/旧测试替身）不应因为 import 本模块而失败。
    """
    from agent.services.position_state import NavCommandIdentity

    return NavCommandIdentity.operation(
        str(operation.operation_id), int(operation.param_version)
    )


# ── 6. 恢复入口的身份 ─────────────────────────────────────────────────

RECOVERY_KIND = "topic_answer_recovery"
"""回答准备失败 / 回答失败的恢复记录 kind（复用 RecoveryInbox 与前端错误恢复）。"""


def stage_failure_message(stages: TopicStages) -> str:
    """按真实失败阶段给出人话（不把「工具成功」说成「整轮失败」）。"""
    if stages.failed_stage == "context_prepare":
        return "话题已切换，回答准备失败"
    if stages.failed_stage == "answer":
        return "话题已切换，回答生成失败"
    if stages.failed_stage == "ownership":
        return "话题已切换，本轮归属迁移失败"
    return "话题操作没有完成"


def iter_topic_tool_names() -> Iterable[str]:
    return TOPIC_TOOL_KINDS.keys()
