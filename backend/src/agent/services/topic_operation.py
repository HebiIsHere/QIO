"""话题操作身份、阶段与事实（本轮 P1 修复的共享契约，唯一权威定义）。

本模块只放**结构化事实**，不做任何自然语言理解：
* 语义判断（「用户是否主动要求」「是否值得拆题」）由本轮主循环模型在
  `switch_topic` / `create_topic` 的 `intent` 参数里声明，程序只消费这个
  受控通道的值，不再用关键词 / 正则 / 相似度阈值反推用户意图；
* 身份、版本、阶段、事实由本模块定义，所有调用方（loop / orchestrator /
  topic_application / topic_tools / api）只读这里的枚举与数据类，不各自造一套。

三层事实分开记录，任何一层失败都不允许被另一层的成功覆盖：

1. `TopicOperation`：一次逻辑话题操作的**身份**（operation_id 稳定，参数带版本）；
2. `TopicStages`：这一步真实走到了哪里（工具成功 / 导航落实 / 归属搬迁 /
   上下文就绪 / 回答完成）；
3. `TopicPhase`：本轮输出边界（只有 ANSWERABLE 允许发布正式回答）。
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


# ── 3. 操作身份 ─────────────────────────────────────────────────────────

def new_operation_id(turn_id: str, kind: str) -> str:
    """一次逻辑话题操作的稳定身份（与名称无关；名称只是带版本的参数）。"""
    suffix = uuid.uuid4().hex[:12]
    return f"topop-{str(turn_id or 'turn')[:24]}-{str(kind or 'topic')[:16]}-{suffix}"


def normalize_topic_name(value: Any) -> str:
    """名称比较口径（只用于查重 / 幂等键，不改变真实落库名字）。"""
    text = str(value or "").strip()
    for ch in " 　\t\r\n\"'“”「」『』《》：:":
        text = text.replace(ch, "")
    return text


def canonical_params(kind: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """逻辑参数的规范形：决定「参数版本」是否变化。

    只保留真正影响执行结果的字段（reason 等展示字段不参与版本）。
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    if kind == "topic_switch":
        return {"topic_id": str(arguments.get("topic_id") or "").strip()}
    return {"name": normalize_topic_name(arguments.get("name"))}


def param_digest(kind: str, arguments: dict[str, Any] | None) -> str:
    params = canonical_params(kind, arguments)
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
    status: OperationStatus = OperationStatus.PENDING
    conflict: ConflictStatus = ConflictStatus.NONE
    stages: "TopicStages" = field(default_factory=lambda: TopicStages())

    # -- 参数版本 ------------------------------------------------------
    def update_params(self, arguments: dict[str, Any] | None) -> bool:
        """用新参数更新同一操作；参数实质变化才推进 param_version。"""
        params = canonical_params(self.kind, arguments)
        if params == self.params:
            return False
        self.params = params
        self.param_version += 1
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
            "conflict": self.conflict.value,
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
            params=canonical_params(kind, arguments),
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
