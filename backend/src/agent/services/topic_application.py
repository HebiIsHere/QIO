"""话题申请的执行桥（契约 P1-04 / P1-05，C 的所有权）。

模型需要换题时仍然调用**原有**的 `switch_topic` / `create_topic`；本模块在
`ToolRegistry.execute` 之前拦截它们，把「身份 / 版本 / 起点快照」变成可校验的
程序事实：

1. **语义来源唯一**：本轮主循环模型在工具参数 `intent` 里声明
   `user_request` / `suggestion`（缺省 / 未知 = suggestion，安全侧）。
   * `user_request` → 校验参数后**直接执行原工具**（route=direct，不弹普通确认）；
   * `suggestion` → 建立话题申请（复用 pending_approvals 的身份 / 600s 有效期 /
     单次消费 / APPROVAL_REQUIRED 事件），用户决定前不执行。
2. **操作身份（P1-05）**：一次逻辑话题操作 = 一个稳定的 `operation_id`；
   名称 / 目标只是**带版本的参数**（`param_version`）。用户改名只更新同一操作的
   参数并推进版本，执行用该版本；`(operation_id, param_version)` 相同的合法重试
   可以再次进入既有流程（含 create_topic 工具内部的查重冲突二次审批），不会被
   「本轮已处理过」拦截；不同参数版本要求新的申请，旧批准绝不迁移到新版本。
3. **起点快照（P1-04）**：申请 payload 保存 `operation_id / turn_id /
   user_message_id / from_topic_id / from_fragment_id / base_nav_version /
   param_version`。校验三处：确认前（API respond 端点，见 tools/approval.py 的
   _topic_navigation_stale）、实际写入前（execute_approved 复核 + 原工具内 CAS，
   见 topic_tools.py）、异步恢复时（loop / 恢复入口用 navigation_version 复核）。
   失效 ≠ 拒绝：不写拒绝范围、不自动重弹、不自动跳回旧位置；返回
   `code=topic_application_stale` 的真实失败，让模型在原话题回答。
4. **幂等**：重复确认 / 网络重放 / 超时重试 / 刷新恢复至多消费一次；同版本重试
   已成功时直接返回既有结果，不重复创建。
5. **阶段**：通过注入的 `set_phase` 回调（loop.set_topic_phase）报告
   AWAITING_CONFIRMATION / EXECUTING / CONTEXT_PREPARING；只有 loop 在上下文准备
   成功后进入 ANSWERABLE。
6. **决定层 / 执行层分开（F02）**：`TopicOperation.decided` 记录用户 / 程序决定，
   `attempt` 记录原工具执行到哪一步。主动要求（direct）只写决定层；工具失败不会
   被当成「本轮已处理过」——只有 `retryable_failure / conflict_pending` 允许同版本
   再次进入原工具及既有冲突确认，成功后的重放只返回既有结果，`stale / refused /
   terminal_failure` 不重试且不续期。
7. **精确执行参数（F07）**：`execution_params`（只外层 strip）决定 param_version
   与批准身份；`normalize_topic_name` 宽松归一化只作查重证据。用户弹窗编辑确认的
   最终名称写入同一操作新版本；模型自行改名会推进版本、旧批准失效。
8. **具体冲突拒绝（F10）**：消费工具结构化结果 `topic_create_conflict_declined` →
   REFUSED + CONFLICT_DECLINED、清 pending conflict、进入原话题回答、同版本不再弹；
   超时 / 失效 / 技术错误分别表示，超时不得写成主动拒绝、失效不写拒绝证据。
9. **未完成事实（F02 给 loop）**：`operation_progress()` / `has_incomplete_operation()`
   以操作**当前终态**回答本轮能否发布正式回答；被拦截的 ToolResult.facts 同时带
   `keep_decision_open` / `operation_progress`，旧批次的 keep 事实不能拦住新版本终态。

设计边界：
* 本模块**不**直接执行话题工具、不写 topic/fragment/anchor —— 执行仍走 registry，
  归属搬迁由 orchestrator 注入的闭包负责（唯一写入者不变）；
* 拒绝范围只按 `ctx.topic_refusal_scope` / `app.topic_application_state` 提供证据，
  跨轮不做程序强制（契约：跨轮由证据文案 + 模型判断）；
* 操作登记表是进程内有界内存（TopicOperationRegistry），申请持久化与有效期仍由
  pending_approvals 负责；operation_id 稳定不延长有效期。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agent.services.navigation_version import current as current_nav_version
from agent.services.topic_operation import (
    AttemptStatus,
    ConflictStatus,
    DecisionStatus,
    IntentSource,
    OperationStatus,
    TopicOperation,
    TopicOperationRegistry,
    TopicPhase,
    dedup_key,
    execution_params,
    intent_from_arguments,
    normalize_topic_name,
    strip_outer,
)
from agent.tools.base import ToolResult
from agent.trace.redact import redact_text, sanitize_error_text

logger = logging.getLogger(__name__)

#: 被拦截的话题工具 → 审批 kind（契约 P1-01）。
TOPIC_TOOL_KINDS = {
    "switch_topic": "topic_switch",
    "create_topic": "topic_create",
}
TOPIC_APPLICATION_KINDS = frozenset(TOPIC_TOOL_KINDS.values())

#: 申请原因只用于展示，最多 80 字。
REASON_LIMIT = 80
#: Guard 的内存上限（拒绝记录 / 轮次记录）：只作为证据，不无限增长。
MAX_REFUSALS = 20
MAX_TURNS = 32
#: 同一轮里最多建立几次申请（防止模型用不断改参数的方式无限弹窗）。
MAX_APPLICATIONS_PER_TURN = 4

REFUSAL_INSTRUCTION = (
    "用户没有同意发起这次话题变更（{what}）。现在**不要**调用 switch_topic / "
    "create_topic，也不要再次询问用户是否要换话题；请直接在当前话题「{current}」"
    "回答用户原来的问题。"
)
STALE_INSTRUCTION = (
    "这次话题申请已失效（{why}），本次没有执行，也不会再执行。不要重复提交申请；"
    "请直接在当前话题「{current}」回答用户原来的问题。"
)
DUPLICATE_INSTRUCTION = (
    "本轮已经处理过同一段讨论的话题申请（{what}），不会再次弹出确认。"
    "请直接在当前话题「{current}」回答用户原来的问题，不要重复申请。"
)
DIRECT_RETRY_INSTRUCTION = (
    "这次话题操作本轮已经按用户明确要求执行过（{what}），不会重复执行。"
    "请直接基于真实结果回答用户原来的问题，不要重复调用话题工具。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_reason(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > REASON_LIMIT:
        text = text[:REASON_LIMIT]
    return text


def _trace(ctx: Any, key: str, detail: str) -> None:
    """写 trace（没有 tracer / 写失败都不影响执行；detail 先脱敏）。"""
    tracer = getattr(ctx, "trace", None)
    writer = getattr(tracer, "write", None)
    if not callable(writer):
        return
    try:
        writer(key, redact_text(str(detail))[:500])
    except Exception:  # noqa: BLE001 - 取证失败不能挡住申请流程
        logger.warning("topic application trace write failed", exc_info=True)


def _trace_warning(ctx: Any, key: str, message: str) -> None:
    tracer = getattr(ctx, "trace", None)
    writer = getattr(tracer, "warning", None)
    if not callable(writer):
        return
    try:
        writer(key, redact_text(str(message))[:500])
    except Exception:  # noqa: BLE001 - 同上
        logger.warning("topic application trace warning failed", exc_info=True)


def _phase_cm(ctx: Any, name: str, detail: str):
    """把「等待用户确认」放进取证时间轴（没有 tracer 时是空上下文）。"""
    tracer = getattr(ctx, "trace", None)
    phase = getattr(tracer, "phase", None)
    if not callable(phase):
        return nullcontext()
    return phase(name, detail)


@dataclass(frozen=True)
class RefusedAttempt:
    """一次被拒绝 / 重复的话题申请（供 A 的 topic_decision 作为证据读取）。

    只记录真实发生过的事实：哪个对象、哪种动作、为什么没成、属于哪一轮 /
    哪条用户消息 / 哪个绑定片段，以及操作身份（便于按同一讨论分段继承）。
    **不是**对用户语义的猜测。失效（stale）/ 超时（timeout）**不**写进这里
    （契约 P1-04 / F10：失效 ≠ 拒绝，超时 ≠ 主动拒绝）。
    """

    topic_id: str
    topic_name: str
    action: str  # "switch" | "create"
    #: rejected / cancelled / duplicate / conflict_declined
    reason: str
    turn_id: str
    user_message_id: str | None = None
    #: 记录时本轮绑定的片段（A 沿 same_stage / source_fragment_id 来源链判断
    #: 「同一段讨论」的范围，本模块不做语义分类、也不读 fragments）。
    fragment_id: str | None = None
    #: 操作身份（同一逻辑操作的拒绝证据可按它去重 / 关联）。
    operation_id: str | None = None
    param_version: int | None = None
    #: 具体冲突拒绝时的独立状态（F10）；普通拒绝为 None。
    conflict: str | None = None
    #: 具体查重冲突拒绝时**真正被拒绝的对象**（相似话题 id），不扩大为全局拒绝。
    similar_topic_id: str | None = None
    created_at: str = field(default_factory=_now_iso)

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic_id": self.topic_id,
            "topic_name": self.topic_name,
            "action": self.action,
            "reason": self.reason,
            "turn_id": self.turn_id,
            "user_message_id": self.user_message_id,
            "fragment_id": self.fragment_id,
            "operation_id": self.operation_id,
            "param_version": self.param_version,
            "conflict": self.conflict,
            "similar_topic_id": self.similar_topic_id,
            "created_at": self.created_at,
        }


class TopicDiscussionGuard:
    """话题申请的内存守卫与拒绝范围记录（`app.topic_application_state`）。

    职责（不新增表）：
    * 拒绝范围证据：`refused_attempts` 给 A 的 `build_topic_decision_state` 只读；
    * 有界：只保留最近 MAX_TURNS 轮 / MAX_REFUSALS 条拒绝记录。

    P1-05 之后「同轮防重」不再由这里按名称决定：它由 bridge 的
    `(operation_id, param_version)` 决定记录负责；本类只保留证据读取面。
    """

    def __init__(self, *, max_refusals: int = MAX_REFUSALS, max_turns: int = MAX_TURNS) -> None:
        self._decisions: dict[str, dict[str, str]] = {}
        self._turn_order: list[str] = []
        self._refusals: list[RefusedAttempt] = []
        self._max_refusals = max(1, int(max_refusals))
        self._max_turns = max(1, int(max_turns))

    # -- 旧接口保留（A 的只读证据 / 既有测试读 refused_attempts）----------
    def application_decision(self, turn_id: str, kind: str, key: str) -> str | None:
        return self._decisions.get(turn_id, {}).get(f"{kind}|{key}")

    def turn_application(self, turn_id: str) -> str | None:
        slot = self._decisions.get(turn_id) or {}
        return next(iter(slot), None)

    def note_application(self, turn_id: str, kind: str, key: str) -> None:
        slot = self._decisions.setdefault(turn_id, {})
        if turn_id not in self._turn_order:
            self._turn_order.append(turn_id)
        slot[f"{kind}|{key}"] = "pending"
        while len(self._turn_order) > self._max_turns:
            self._decisions.pop(self._turn_order.pop(0), None)

    def note_decision(self, turn_id: str, kind: str, key: str, decision: str) -> None:
        self._decisions.setdefault(turn_id, {})[f"{kind}|{key}"] = str(decision)

    # -- 拒绝记录（证据） --------------------------------------------------
    def record_refusal(
        self,
        *,
        topic_id: str,
        topic_name: str,
        action: str,
        reason: str,
        turn_id: str,
        user_message_id: str | None = None,
        fragment_id: str | None = None,
        operation_id: str | None = None,
        param_version: int | None = None,
        conflict: str | None = None,
        similar_topic_id: str | None = None,
    ) -> RefusedAttempt:
        attempt = RefusedAttempt(
            topic_id=str(topic_id or ""),
            topic_name=str(topic_name or ""),
            action=str(action or ""),
            reason=str(reason or ""),
            turn_id=str(turn_id or ""),
            user_message_id=user_message_id,
            fragment_id=fragment_id,
            operation_id=operation_id,
            param_version=param_version,
            conflict=conflict,
            similar_topic_id=similar_topic_id,
        )
        self._refusals.append(attempt)
        if len(self._refusals) > self._max_refusals:
            del self._refusals[: len(self._refusals) - self._max_refusals]
        return attempt

    @property
    def refused_attempts(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._refusals]

    def refusals_for(self, topic_id: str) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._refusals if item.topic_id == topic_id]

    # -- F09-record：稳定的只读读取面（A 的 topic_decision 消费）------------
    def refusal_entries(
        self, *, topic_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """全部拒绝证据（旧 -> 新，跨轮有界内存），只读。

        topic_id 非空时只返回该话题的条目（创建类拒绝的 topic_id 可能为空，
        调用方需要「按讨论」证据时用 refusals_within 传片段集合）。
        """
        items = [
            item.as_dict()
            for item in self._refusals
            if topic_id is None or item.topic_id == str(topic_id)
        ]
        if limit is not None:
            items = items[-max(0, int(limit)) :]
        return items

    def refusals_within(
        self,
        fragment_ids: Any,
        *,
        topic_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """只返回属于给定片段集合的拒绝证据（F09 容量分段继承的统一入口）。

        调用方（A）负责沿 fragments 的 same_stage / source_fragment_id 来源链
        算出「同一段讨论」的 fragment_id 集合（有界 / 防环 / 去重）；本方法只做
        集合过滤，不做语义分类、不读数据库、不修改任何状态。
        """
        wanted = {str(value) for value in (fragment_ids or ()) if str(value or "")}
        if not wanted:
            return []
        items: list[dict[str, Any]] = []
        for item in self._refusals:
            if item.fragment_id is None or str(item.fragment_id) not in wanted:
                continue
            if topic_id is not None and item.topic_id != str(topic_id):
                continue
            items.append(item.as_dict())
        if limit is not None:
            items = items[-max(0, int(limit)) :]
        return items

    def refusal_scope(self, topic_id: str | None = None) -> dict[str, Any] | None:
        for item in reversed(self._refusals):
            if topic_id is None or item.topic_id == topic_id:
                return item.as_dict()
        return None

    def reset(self) -> None:
        self._decisions.clear()
        self._turn_order.clear()
        self._refusals.clear()


@dataclass
class _PendingApplication:
    """一次「已批准、等待原工具执行」的申请（按 call_id 挂在 bridge 上）。"""

    kind: str
    operation_id: str
    param_version: int
    key: str
    from_topic: str
    base_nav_version: int | None = None
    target_topic_id: str | None = None
    new_name: str | None = None
    approval_id: str | None = None
    edits_applied: bool = False

    def what(self) -> str:
        if self.kind == "topic_switch":
            return f"切换到其他话题（{self.target_topic_id or ''}）"
        return f"新建话题「{self.new_name or ''}」"


def _operation_registry_for(app: Any) -> TopicOperationRegistry:
    """AppContext 级操作登记表（进程内、按 turn_id 隔离、跨轮不复用）。"""
    registry = getattr(app, "topic_operation_registry", None)
    if not isinstance(registry, TopicOperationRegistry):
        registry = TopicOperationRegistry()
        try:
            setattr(app, "topic_operation_registry", registry)
        except Exception:  # noqa: BLE001 - 只读 app 对象时退化为桥内状态
            logger.warning("failed to attach topic operation registry", exc_info=True)
    return registry


class TopicApplicationBridge:
    """在 registry.execute 之前建立话题申请，批准后执行原工具并衔接上下文。"""

    def __init__(
        self,
        app: Any,
        ctx: Any,
        *,
        adapter: Any = None,
        plan: Any = None,
        rebind_turn: Callable[[str], Awaitable[Any]] | None = None,
        rebuild_system: Callable[[str], Awaitable[str]] | None = None,
        set_phase: Callable[[Any], None] | None = None,
    ) -> None:
        self.app = app
        self.ctx = ctx
        # adapter / plan 只用于让 orchestrator 的闭包拿到同一份本轮上下文；
        # bridge 自己不读它们的内容（避免复制上下文装配逻辑）。
        self.adapter = adapter
        self.plan = plan
        self.rebind_turn = rebind_turn
        self.rebuild_system = rebuild_system
        self.set_phase = set_phase
        self.guard = get_topic_application_state(app)
        self.registry = _operation_registry_for(app)
        # 审批已消费、工具执行结果还没回来的申请。
        self._approved: dict[str, _PendingApplication] = {}
        # (operation_id, param_version) → 工具真实成功结果（幂等重放只返回它）
        self._results: dict[tuple[str, int], ToolResult] = {}
        # (operation_id, param_version) → 终态失败结果（拒绝 / 失效 / 不可重试失败）。
        # 同版本重放返回**既有结构化结果**，绝不重新执行、也不改成成功（F02）。
        self._terminal_results: dict[tuple[str, int], ToolResult] = {}
        # (operation_id, param_version) → approval_id（真实执行结果回写历史用）
        self._approval_ids: dict[tuple[str, int], str] = {}
        # call_id → 本次调用对应的操作（after_tool 用）与 ContextVar token
        self._call_operations: dict[str, TopicOperation] = {}
        self._bind_tokens: dict[str, Any] = {}
        self._turn_applications = 0
        self.last_rebind_topic: str | None = None
        # 最近一次真实导航结果（F05/F12：A 从 directive 读不到时才用它兜底，不猜）。
        self.last_navigation: dict[str, Any] = {}
        # 最近一次操作（loop 的恢复记录用：current_operation()）。
        self._active_operation: TopicOperation | None = None

    # -- 上下文辅助 --------------------------------------------------------

    @property
    def turn_id(self) -> str:
        return str(getattr(self.ctx, "turn_id", "") or "")

    def _conn(self) -> Any:
        return getattr(self.app, "conn", None)

    def _nav_version(self) -> int | None:
        conn = self._conn()
        if conn is None:
            return None
        try:
            return int(current_nav_version(conn))
        except Exception:  # noqa: BLE001 - 读不到版本时只做 binding 兜底校验（不猜）
            logger.warning("failed to read navigation version", exc_info=True)
            return None

    def _active_topic(self) -> str | None:
        anchors = getattr(self.app, "navigation", None)
        anchors = getattr(anchors, "anchors", None)
        getter = getattr(anchors, "get_active", None)
        if not callable(getter):
            return None
        try:
            active = getter()
        except Exception:  # noqa: BLE001 - 读不到就当不知道（不猜）
            logger.warning("failed to read active anchor", exc_info=True)
            return None
        return getattr(active, "topic_id", None) if active is not None else None

    def _node(self, topic_id: str | None):
        if not topic_id:
            return None
        topics = getattr(self.app, "topics", None)
        nodes = getattr(topics, "nodes", None)
        getter = getattr(nodes, "get_topic", None)
        if callable(getter):
            try:
                return getter(topic_id)
            except Exception:  # noqa: BLE001 - 读失败等价于不存在（调用方会如实失败）
                logger.warning("failed to read topic %s", topic_id, exc_info=True)
                return None
        conn = self._conn()
        if conn is not None:
            try:
                return conn.execute(
                    "SELECT id, name FROM nodes WHERE id = ? AND type = 'topic'",
                    (topic_id,),
                ).fetchone()
            except Exception:  # noqa: BLE001
                logger.warning("failed to query topic %s", topic_id, exc_info=True)
        return None

    @staticmethod
    def _node_name(node: Any) -> str:
        if node is None:
            return ""
        name = getattr(node, "name", None)
        if name is None and isinstance(node, dict):
            name = node.get("name")
        return str(name or "")

    def _from_topic(self) -> str:
        topic = getattr(self.ctx, "bound_topic", None) or getattr(
            self.ctx, "current_topic", None
        )
        return str(topic or self._active_topic() or "")

    def _from_topic_name(self) -> str:
        return self._node_name(self._node(self._from_topic()))

    def _user_message_id(self) -> str | None:
        return getattr(self.ctx, "user_message_id", None)

    def _fragment_id(self) -> str | None:
        return getattr(self.ctx, "bound_fragment_id", None)

    # -- 决策位置快照（F05，A 在 TurnContext 写，C 只读）--------------------

    def _decision_snapshot(self) -> dict[str, Any]:
        """读取本轮决策依据形成时冻结的真实位置。

        * 有快照（A 的 begin 写入 decision_* 任一非 None）：严格用快照，
          None 就是 None，**不**在工具返回后用当前版本补；
        * 没有快照（旧测试替身 / 子任务 / 旧路径）：退回既有「当前值」口径，
          保持旧行为不破坏。

        返回 dict：topic_id / fragment_id / nav_version / queued / available。
        """
        ctx = self.ctx
        raw = [
            getattr(ctx, "decision_nav_version", None),
            getattr(ctx, "decision_topic_id", None),
            getattr(ctx, "decision_fragment_id", None),
        ]
        available = any(value is not None for value in raw)
        if available:
            return {
                "available": True,
                "topic_id": getattr(ctx, "decision_topic_id", None),
                "fragment_id": getattr(ctx, "decision_fragment_id", None),
                "nav_version": getattr(ctx, "decision_nav_version", None),
                "queued": bool(getattr(ctx, "decision_queued", False)),
            }
        return {
            "available": False,
            "topic_id": self._from_topic() or None,
            "fragment_id": self._fragment_id(),
            "nav_version": self._nav_version(),
            "queued": False,
        }

    def note_decision_navigation(
        self,
        topic_id: str,
        fragment_id: str | None = None,
        nav_version: int | None = None,
    ) -> dict[str, Any]:
        """同轮自己成功导航后的**因果更新**（A 在 orchestrator/loop 调用）。

        只把 ctx 的决策快照写成这次操作的真实结果：不推进导航版本、不写 Anchor、
        不算外来手动导航；重复调用同值幂等。ctx 只读时返回快照而不抛异常。
        """
        snapshot = {
            "topic_id": str(topic_id or "") or None,
            "fragment_id": fragment_id,
            "nav_version": int(nav_version) if nav_version is not None else None,
        }
        for name, value in (
            ("decision_topic_id", snapshot["topic_id"]),
            ("decision_fragment_id", snapshot["fragment_id"]),
            ("decision_nav_version", snapshot["nav_version"]),
        ):
            try:
                setattr(self.ctx, name, value)
            except Exception:  # noqa: BLE001 - 只读 ctx 退化：不改变真实导航结果
                logger.warning("failed to update decision snapshot", exc_info=True)
                break
        _trace(
            self.ctx,
            "topic_application_causal_navigation",
            f"{snapshot['topic_id']}:v{snapshot['nav_version']}",
        )
        return snapshot

    # -- 阶段回调（loop.set_topic_phase 的 sync 包装）-----------------------

    def _set_phase(self, phase: TopicPhase) -> None:
        callback = self.set_phase
        if not callable(callback):
            loop = getattr(self.ctx, "loop", None)
            callback = getattr(loop, "set_topic_phase", None)
        if not callable(callback):
            return
        try:
            callback(phase)
        except TypeError:
            try:
                callback(phase.value if isinstance(phase, TopicPhase) else str(phase))
            except Exception:  # noqa: BLE001 - 阶段回调失败不改变工具真实结果
                logger.warning("topic phase callback failed", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.warning("topic phase callback failed", exc_info=True)

    # -- payload（契约 P1-04）---------------------------------------------

    def payload(
        self,
        kind: str,
        arguments: dict[str, Any],
        *,
        operation: TopicOperation | None = None,
    ) -> dict[str, Any]:
        """严格按契约 P1-04 构造审批载荷（不放原问题 / 摘要 / 预览）。"""
        arguments = dict(arguments or {})
        operation = operation or self._active_operation
        from_topic = (operation.from_topic if operation else None) or self._from_topic()
        payload: dict[str, Any] = {
            "action": "switch" if kind == "topic_switch" else "create",
            "kind": kind,
            "turn_id": self.turn_id,
            "from_topic_id": from_topic,
            "from_topic_name": self._from_topic_name(),
            "from_fragment_id": (
                operation.from_fragment if operation else self._fragment_id()
            ),
            "reason": _short_reason(arguments.get("reason")),
            "source": "model",
            "user_message_id": (
                operation.user_message_id
                if operation is not None
                else self._user_message_id()
            ),
            "intent_source": (
                operation.intent_source.value
                if operation is not None
                else IntentSource.SUGGESTION.value
            ),
        }
        if operation is not None:
            payload["operation_id"] = operation.operation_id
            payload["param_version"] = int(operation.param_version)
            payload["base_nav_version"] = operation.base_nav_version
        # F07：批准身份用**执行参数**（只做外层 strip）；查重归一化只作证据。
        params = execution_params(kind, arguments)
        payload["dedup_key"] = dedup_key(kind, arguments)
        if kind == "topic_switch":
            target_id = params["topic_id"]
            payload["target_topic_id"] = target_id
            payload["target_topic_name"] = self._node_name(self._node(target_id))
        else:
            payload["new_name"] = params["name"]
        return payload

    # -- 当前操作（loop / 恢复记录只读）------------------------------------

    def current_operation(self) -> TopicOperation | None:
        return self._active_operation

    def operation_for_approval(self, approval_id: str | None) -> TopicOperation | None:
        return self.registry.by_approval_id(approval_id)

    # -- 未完成操作事实（F02：给 loop 的发布前提一个**当前终态**查询）--------

    def operation_progress(self) -> str:
        """本轮最近一次话题操作的真实进度（只读程序事实，不按文本猜）。

        * "none"     —— 本轮还没有话题操作；
        * "pending"  —— 仍未完成（等待用户决定 / 已批准未执行 / 执行中 /
                        冲突待确认 / 可重试失败）：loop 不得发布正式回答；
        * "terminal" —— 已终结（拒绝 / 失效 / 不可重试失败）：不得借旧批次
                        的 keep_decision_open 拦住本轮回答；
        * "success"  —— 工具真实成功（上下文准备由 loop 的待重建记录负责）。

        旧批次遗留的 keep 事实因此不能单独决定新 param_version 的终态。
        """
        operation = self._active_operation
        if operation is None:
            operation = self.registry.for_turn(self.turn_id)
        if operation is None:
            return "none"
        if operation.decided in (DecisionStatus.REFUSED, DecisionStatus.STALE):
            return "terminal"
        if (
            operation.decided is DecisionStatus.PENDING
            and operation.failure_code == "approval_timeout"
        ):
            # 申请超时：本轮已终结（不重弹、不执行），但决定层没有用户拒绝。
            return "terminal"
        if operation.attempt is AttemptStatus.SUCCEEDED:
            return "success"
        if operation.attempt is AttemptStatus.TERMINAL_FAILURE:
            return "terminal"
        if operation.decided is DecisionStatus.PENDING:
            return "pending"
        if operation.decided is DecisionStatus.APPROVED:
            return "pending"
        return "none"

    def has_incomplete_operation(self) -> bool:
        """是否存在「等待用户决定 / 执行中 / 冲突待确认 / 可重试失败」的操作。"""
        return self.operation_progress() == "pending"

    def _progress_facts(self, operation: TopicOperation | None = None) -> dict[str, Any]:
        """终态事实：供 loop 在被拦截的结果上刷新本轮发布前提（F02）。"""
        self._active_operation = operation or self._active_operation
        progress = self.operation_progress()
        return {
            "keep_decision_open": progress == "pending",
            "operation_progress": progress,
        }

    # -- 拒绝范围 ----------------------------------------------------------

    def record_refusal(
        self,
        *,
        kind: str,
        topic_id: str,
        topic_name: str,
        reason: str,
        turn_id: str | None = None,
        operation: TopicOperation | None = None,
        conflict: ConflictStatus | str | None = None,
        similar_topic_id: str | None = None,
    ) -> RefusedAttempt:
        """记录一次**用户拒绝 / 重复 / 取消**的话题申请（失效 / 超时不记录）。"""
        operation = operation or self._active_operation
        conflict_value: str | None = None
        if isinstance(conflict, ConflictStatus):
            conflict_value = conflict.value
        elif conflict is not None:
            conflict_value = str(conflict)
        attempt = self.guard.record_refusal(
            topic_id=topic_id,
            topic_name=topic_name,
            action="switch" if kind == "topic_switch" else "create",
            reason=reason,
            turn_id=str(turn_id or self.turn_id),
            user_message_id=self._user_message_id(),
            fragment_id=self._fragment_id(),
            operation_id=(
                operation.operation_id if operation is not None else None
            ),
            param_version=(
                int(operation.param_version) if operation is not None else None
            ),
            conflict=conflict_value,
            similar_topic_id=similar_topic_id,
        )
        scope = attempt.as_dict()
        try:
            setattr(self.ctx, "topic_refusal_scope", scope)
        except Exception:  # noqa: BLE001 - ctx 是 dataclass，正常可写；只读对象退化
            logger.warning("failed to set topic_refusal_scope", exc_info=True)
        _trace(
            self.ctx,
            "topic_application_refused",
            f"{attempt.action}:{attempt.topic_id or attempt.topic_name}:{attempt.reason}",
        )
        return attempt

    # -- 入口 --------------------------------------------------------------

    async def before_tool(self, call: Any) -> ToolResult | None:
        """registry.execute 之前的拦截点。

        None = 照常执行（参数可能已被批准时的 edits 校正）；非 None = 本次调用
        已经处理完（工具绝不执行），直接作为该 call 的最终结果。
        """
        name = str(getattr(call, "name", "") or "")
        kind = TOPIC_TOOL_KINDS.get(name)
        if kind is None:
            return None
        arguments = dict(getattr(call, "arguments", None) or {})
        if not self._arguments_usable(kind, arguments):
            # 参数本身不成立（缺 topic_id / 空名称）：交给原工具如实报参数错误，
            # 不为一个注定失败的操作打扰用户。
            _trace(self.ctx, "topic_application_skipped", f"{kind}:invalid_arguments")
            return None

        intent = intent_from_arguments(arguments)
        operation = self._operation_for_call(
            kind, arguments, intent, call_id=str(getattr(call, "id", "") or "")
        )
        self._active_operation = operation
        # 恢复记录 / loop 从操作身份读取 user_message_id（契约模块字段），
        # 未完成步骤重试都复用同一条原问题，不新建用户消息。
        if getattr(operation, "user_message_id", None) is None:
            operation.user_message_id = self._user_message_id()

        key = (operation.operation_id, int(operation.param_version))
        cached = self._results.get(key)
        if cached is not None and cached.ok:
            # 已成功过：重复确认 / 重放 / 工具重试只返回既有目标与结果，不再执行。
            _trace(self.ctx, "topic_application_idempotent", f"{kind}:{key[0]}:v{key[1]}")
            return cached

        # F02：失效 / 拒绝 / 不可重试失败 = 终态：同版本重放返回**既有结构化结果**，
        # 既不重新执行，也不会被改成成功。
        terminal = self._terminal_results.get(key)
        if terminal is not None and not operation.can_retry():
            if operation.decided is DecisionStatus.REFUSED:
                self._record_duplicate_refusal(kind, arguments)
            _trace(
                self.ctx,
                "topic_application_terminal_replay",
                f"{kind}:{key[0]}:v{key[1]}:{operation.retry_block_reason()}",
            )
            return terminal

        if intent is IntentSource.USER_REQUEST:
            return self._route_direct(call, operation, kind, arguments)
        return await self._route_application(call, operation, kind, arguments)

    # -- 操作身份 ----------------------------------------------------------

    def _operation_for_call(
        self, kind: str, arguments: dict[str, Any], intent: IntentSource, *, call_id: str
    ) -> TopicOperation:
        snapshot = self._decision_snapshot()
        existing = self.registry.for_turn(self.turn_id, kind=kind)
        if existing is not None:
            # 同一轮同一 kind 是同一逻辑操作：**执行参数**实质变化才推进参数版本。
            changed = existing.update_params(arguments)
            existing.remember_call(call_id)
            if changed:
                # F05：参数版本推进 = 新决定。起点快照必须换成**当前决策快照**
                # （同轮自己成功导航后的因果更新由 A 通过 note_decision_navigation
                # 写回 ctx），否则新版本会拿旧 base_nav_version 去做 CAS，被错误判失效。
                # 同一 param_version 的合法重试不刷新：保持原起点校验语义。
                if snapshot["available"]:
                    existing.base_nav_version = snapshot["nav_version"]
                    if snapshot["topic_id"]:
                        existing.from_topic = snapshot["topic_id"]
                    existing.from_fragment = snapshot["fragment_id"]
                elif existing.base_nav_version is None:
                    # 无快照的旧路径：保持原「首次补当前值」语义。
                    existing.base_nav_version = snapshot["nav_version"]
            if existing.base_nav_version is None and not changed:
                existing.base_nav_version = snapshot["nav_version"]
            if existing.user_message_id is None:
                existing.user_message_id = self._user_message_id()
            return existing
        return self.registry.begin(
            turn_id=self.turn_id,
            kind=kind,
            route=("direct" if intent is IntentSource.USER_REQUEST else "application"),
            intent_source=intent,
            arguments=arguments,
            from_topic=(snapshot["topic_id"] or self._from_topic()),
            from_fragment=snapshot["fragment_id"],
            base_nav_version=snapshot["nav_version"],
            user_message_id=self._user_message_id(),
        )

    # -- 直接执行（user_request）------------------------------------------

    def _route_direct(
        self, call: Any, operation: TopicOperation, kind: str, arguments: dict[str, Any]
    ) -> ToolResult | None:
        """用户明确要求：直接进原工具。

        F02：**「直接要求」只是决定层，不等于执行成功**。工具失败（含查重冲突 /
        可重试失败）后，同 operation + 同 param_version 必须允许再次进入原工具；
        只有真实成功才返回既有结果，拒绝 / 失效 / 不可重试失败返回终态结果。
        """
        if operation.decided is DecisionStatus.STALE:
            return self._stale_result(
                kind, arguments, operation.failure_code or "nav_version_changed",
                operation=operation,
            )
        if operation.attempt is AttemptStatus.EXECUTING:
            # 同一批里的重复调用：第一次还在执行，不能重复执行第二次。
            return self._executing_result(kind, arguments)
        if operation.can_retry():
            # 冲突待确认 / 可重试失败：同版本合法重试，再次进入原工具。
            operation.route = "direct"
            operation.intent_source = IntentSource.USER_REQUEST
            operation.begin_attempt()
            if operation.base_nav_version is None:
                operation.base_nav_version = self._decision_snapshot()["nav_version"]
            self._remember(call, self._pending_from_operation(operation, kind, arguments))
            self._bind(call, operation)
            self._set_phase(TopicPhase.EXECUTING)
            _trace(
                self.ctx,
                "topic_application_direct_retry",
                f"{kind}:{operation.operation_id}:{operation.retry_block_reason()}",
            )
            return None
        if not operation.decided is DecisionStatus.NONE:
            # pending / refused / 终态失败：同一版本不能「改口」绕过已发生的决定。
            return self._duplicate_result(kind, arguments, direct=True)

        operation.route = "direct"
        operation.intent_source = IntentSource.USER_REQUEST
        operation.decide(DecisionStatus.APPROVED)
        operation.begin_attempt()
        snapshot = self._decision_snapshot()
        if operation.base_nav_version is None:
            operation.base_nav_version = snapshot["nav_version"]
        if not operation.from_topic:
            operation.from_topic = snapshot["topic_id"] or self._from_topic()
        if operation.from_fragment is None:
            operation.from_fragment = snapshot["fragment_id"]
        self._remember(call, self._pending_from_operation(operation, kind, arguments))
        self._bind(call, operation)
        self._set_phase(TopicPhase.EXECUTING)
        _trace(self.ctx, "topic_application_direct", f"{kind}:{operation.operation_id}")
        return None

    # -- 申请（suggestion）-------------------------------------------------

    async def _route_application(
        self, call: Any, operation: TopicOperation, kind: str, arguments: dict[str, Any]
    ) -> ToolResult | None:
        """QIO 建议：先建立申请，用户批准后才执行原工具（F02 状态机入口）。"""
        if operation.decided is DecisionStatus.STALE:
            return self._stale_result(
                kind, arguments, operation.failure_code or "nav_version_changed",
                operation=operation,
            )
        if operation.attempt is AttemptStatus.EXECUTING:
            return self._executing_result(kind, arguments)
        if operation.can_retry():
            # 同 operation + 同 param_version 的合法重试：已批准（含冲突待确认 /
            # 可重试失败）直接再次进入原工具，不重复发起普通申请。
            operation.route = "application"
            operation.begin_attempt()
            _trace(
                self.ctx,
                "topic_application_reuse_approval",
                f"{kind}:{operation.operation_id}:{operation.retry_block_reason()}",
            )
            self._remember(
                call,
                self._pending_from_operation(
                    operation, kind, arguments, approval_id=self._approval_ids.get(
                        (operation.operation_id, int(operation.param_version))
                    )
                ),
            )
            self._bind(call, operation)
            self._set_phase(TopicPhase.EXECUTING)
            return None
        if operation.decided is DecisionStatus.PENDING:
            # 申请等待中 / 申请超时后同版本重放：不再次弹窗（有效期不续期）。
            return self._terminal_results.get(
                (operation.operation_id, int(operation.param_version))
            ) or self._duplicate_result(kind, arguments)
        if operation.decided is DecisionStatus.REFUSED or operation.attempt is (
            AttemptStatus.TERMINAL_FAILURE
        ):
            return self._terminal_results.get(
                (operation.operation_id, int(operation.param_version))
            ) or self._duplicate_result(kind, arguments)
        if self._turn_applications >= MAX_APPLICATIONS_PER_TURN:
            _trace_warning(
                self.ctx,
                "topic_application_capped",
                f"本轮申请次数超过上限 {MAX_APPLICATIONS_PER_TURN}",
            )
            return self._duplicate_result(kind, arguments)

        # 新申请：起点快照（导航版本 / 话题 / 片段）只在这里取一次（F05）。
        snapshot = self._decision_snapshot()
        operation.route = "application"
        operation.intent_source = IntentSource.SUGGESTION
        if operation.base_nav_version is None:
            operation.base_nav_version = snapshot["nav_version"]
        if not operation.from_topic:
            operation.from_topic = snapshot["topic_id"] or self._from_topic()
        if operation.from_fragment is None:
            operation.from_fragment = snapshot["fragment_id"]
        return await self._request_application(call, operation, kind, arguments)

    async def _request_application(
        self, call: Any, operation: TopicOperation, kind: str, arguments: dict[str, Any]
    ) -> ToolResult | None:
        """建立并等待一次申请；返回 None = 已批准可执行，否则是最终结果。"""
        approvals = getattr(self.app, "approvals", None)
        requester = getattr(approvals, "request", None)
        if not callable(requester):
            # 没有审批通道（子任务 / 单元测试）：话题变更不做，也不假装做了。
            _trace_warning(self.ctx, "topic_application_unavailable", "没有审批通道")
            return self._refusal_result(
                kind,
                arguments,
                reason="no_approval_channel",
                content=self._no_channel_content(kind, arguments),
                record_evidence=False,
                operation=operation,
            )

        key = (operation.operation_id, int(operation.param_version))
        payload = self.payload(kind, arguments, operation=operation)
        self._turn_applications += 1
        operation.decide(DecisionStatus.PENDING)
        self.guard.note_application(self.turn_id, kind, self._target_key(kind, arguments))
        _trace(
            self.ctx,
            "topic_application_requested",
            f"{kind}:{operation.operation_id}:v{operation.param_version}"
            f":base={operation.base_nav_version}",
        )
        # 等待用户决定期间不得发布正式正文（阶段门 P1-02）。
        self._set_phase(TopicPhase.AWAITING_CONFIRMATION)
        try:
            with _phase_cm(self.ctx, "approval_wait", f"topic_application:{kind}"):
                result = await requester(kind, payload)
        except asyncio.CancelledError:
            # 本轮被用户停止：不是对话题申请的主动拒绝（不写拒绝证据）。
            operation.decide(DecisionStatus.REFUSED)
            operation.fail_attempt(AttemptStatus.TERMINAL_FAILURE, code="cancelled")
            self.guard.note_decision(
                self.turn_id, kind, self._target_key(kind, arguments), "cancelled"
            )
            _trace(self.ctx, "topic_application_cancelled", f"{kind}:{operation.operation_id}")
            cancelled_result = ToolResult(
                ok=False,
                content="本轮已停止，话题申请没有执行。",
                error="turn cancelled",
                category="cancelled",
                status="cancelled",
                code="topic_application_cancelled",
                facts={
                    "topic_application": "cancelled",
                    "kind": kind,
                    **self._progress_facts(operation),
                },
            )
            self._terminal_results[key] = cancelled_result
            return cancelled_result

        decision = str(getattr(result, "decision", "") or "")
        if decision == "stale":
            # 确认前校验（respond 端点）已发现起点改变：失效 ≠ 拒绝。
            operation.decide(DecisionStatus.STALE)
            operation.fail_attempt(
                AttemptStatus.TERMINAL_FAILURE, code="nav_version_changed"
            )
            why = self._stale_why_for(operation)
            _trace(
                self.ctx,
                "topic_application_stale",
                f"{kind}:{operation.operation_id}:{why}",
            )
            stale_result = self._stale_result(kind, arguments, why, operation=operation)
            self._terminal_results[key] = stale_result
            return stale_result

        if decision == "timeout":
            # 超时 ≠ 主动拒绝（F10）：不写拒绝证据、不改成拒绝状态；
            # 本轮不再重复弹窗（稳定操作 ID 不延长有效期），在原话题回答。
            operation.decide(DecisionStatus.PENDING)
            operation.failure_code = "approval_timeout"
            _trace(
                self.ctx,
                "topic_application_timeout",
                f"{kind}:{operation.operation_id}",
            )
            timeout_result = ToolResult(
                ok=False,
                content=(
                    "等待用户确认超时，这次话题操作没有执行（也不是用户拒绝）。"
                    "本轮不会再次弹出同一个申请；请直接在当前话题回答用户原来的问题。"
                ),
                error="topic application confirmation timed out",
                category="timeout",
                recoverable=True,
                facts={
                    "topic_application": "timeout",
                    "kind": kind,
                    "operation_id": operation.operation_id,
                    "param_version": operation.param_version,
                    **self._progress_facts(operation),
                },
                code="topic_application_timeout",
            )
            self._terminal_results[key] = timeout_result
            return timeout_result

        if decision != "approved":
            # 用户明确拒绝（rejected 等）：拒绝证据只覆盖本次具体对象。
            operation.decide(DecisionStatus.REFUSED)
            operation.fail_attempt(AttemptStatus.TERMINAL_FAILURE, code="rejected")
            self.guard.note_decision(
                self.turn_id, kind, self._target_key(kind, arguments), "refused"
            )
            reason = decision or "rejected"
            refused_result = self._refusal_result(
                kind, arguments, reason=reason, operation=operation
            )
            self._terminal_results[key] = refused_result
            return refused_result

        # 已批准：把用户就地修改（edits）写进**同一操作**的新参数版本（F07）。
        # 用户刚确认的最终名称就是执行名称；模型后来自行改成别的名称会推进版本、
        # 需要新申请，不能借用这次批准。
        approval_id = str(getattr(result, "approval_id", "") or "") or None
        effective = dict(arguments)
        edits = getattr(result, "edits", None)
        edited_name: str | None = None
        if kind == "topic_create" and isinstance(edits, dict):
            name = strip_outer(edits.get("name"))
            if name:
                effective["name"] = name
                edited_name = name
        operation.update_params(effective)
        operation.decide(DecisionStatus.APPROVED)
        operation.begin_attempt()
        new_key = (operation.operation_id, int(operation.param_version))
        pending = self._pending_from_operation(
            operation, kind, effective, approval_id=approval_id
        )
        if edited_name:
            pending.edits_applied = edited_name != strip_outer(arguments.get("name"))

        conflict = await self.execute_approved(
            kind, effective, pending, result, operation=operation
        )
        if conflict is not None:
            code = str(getattr(conflict, "code", "") or "")
            stale = code == "topic_application_stale" or code in (
                "topic_application_topic_target_missing",
                "topic_application_topic_name_required",
            )
            if stale:
                operation.decide(DecisionStatus.STALE)
                operation.fail_attempt(
                    AttemptStatus.TERMINAL_FAILURE, code=code or "stale"
                )
            else:
                operation.decide(DecisionStatus.REFUSED)
                operation.fail_attempt(
                    AttemptStatus.TERMINAL_FAILURE, code=code or "refused"
                )
            self._terminal_results[new_key] = conflict
            if approval_id:
                await self._settle_legacy(approval_id, ok=False, error=conflict.error)
            return conflict

        if approval_id:
            self._approval_ids[new_key] = approval_id
            self.registry.bind_approval(operation, approval_id)
        self.guard.note_decision(
            self.turn_id, kind, self._target_key(kind, effective), "approved"
        )
        self._remember(call, pending)
        self._bind(call, operation)
        if pending.edits_applied:
            self._apply_edited_arguments(call, pending)
        self._set_phase(TopicPhase.EXECUTING)
        return None

    # -- 批准之后、工具执行之前 -------------------------------------------

    async def execute_approved(
        self,
        kind: str,
        arguments: dict[str, Any],
        pending: _PendingApplication,
        result: Any = None,
        *,
        operation: TopicOperation | None = None,
    ) -> ToolResult | None:
        """批准之后、原工具执行之前：校验 edits / 目标 / 起点快照。

        返回 None = 允许原工具按（可能被 edits 改写后的）参数执行；
        返回 ToolResult = 不执行（stale / 目标不存在 / 名称无效）。
        """
        if kind == "topic_create":
            if not str(pending.new_name or "").strip():
                return self._refusal_result(kind, arguments, reason="topic_name_required")
        else:
            target = self._node(pending.target_topic_id)
            if target is None:
                return self._refusal_result(kind, arguments, reason="topic_target_missing")

        # 起点快照：用户等待期间可能手动导航 / 换片段 / 本轮绑定被收尾 ——
        # 那都算失效，不执行、不覆盖用户的新选择（契约 P1-04）。
        stale_why = self._stale_reason(operation, pending)
        if stale_why:
            return self._stale_result(kind, arguments, stale_why, operation=operation)
        return None

    def _stale_why_for(self, operation: TopicOperation | None) -> str:
        if operation is not None and operation.base_nav_version is not None:
            actual = self._nav_version()
            if actual is not None and int(actual) != int(operation.base_nav_version):
                return "nav_version_changed"
        return "application_invalidated"

    def _stale_reason(
        self,
        operation: TopicOperation | None,
        pending: _PendingApplication,
    ) -> str | None:
        # 1) 导航版本 CAS 快照（同话题换片段 / 离开再返回都会让版本变化）。
        if operation is not None and operation.base_nav_version is not None:
            actual = self._nav_version()
            if actual is not None and int(actual) != int(operation.base_nav_version):
                return "nav_version_changed"
        from_topic = pending.from_topic or (operation.from_topic if operation else "") or ""
        if not from_topic:
            return "missing_from_topic"
        # 2) 本轮绑定仍是申请建立时的话题，且没有被收尾。
        binding = None
        bindings = getattr(self.app, "bindings", None)
        getter = getattr(bindings, "binding_for", None)
        if callable(getter) and self.turn_id:
            try:
                binding = getter(self.turn_id)
            except Exception:  # noqa: BLE001
                logger.warning("failed to read binding for stale check", exc_info=True)
        if binding is not None:
            if str(getattr(binding, "topic_id", "")) != from_topic:
                return "binding_changed"
            if str(getattr(binding, "write_state", "")) != "open":
                return "binding_closed"
        # 3) 真实 Anchor 没被另一个请求改走。
        active = self._active_topic()
        if active is not None and active != from_topic:
            return "active_anchor_changed"
        current = str(getattr(self.ctx, "bound_topic", None) or "")
        if current and current != from_topic:
            return "turn_topic_changed"
        return None

    def _apply_edited_arguments(self, call: Any, pending: _PendingApplication) -> None:
        """把用户批准的新名称写回 call.arguments（原地改 dict，事件/历史同源）。"""
        name = strip_outer(pending.new_name)
        arguments = getattr(call, "arguments", None)
        if not name or not isinstance(arguments, dict):
            return
        if strip_outer(arguments.get("name")) == name:
            return
        arguments["name"] = name
        _trace(self.ctx, "topic_application_edited", f"create:{name}")

    # -- 工具结果之后的归属搬迁 -------------------------------------------

    async def after_tool(self, call: Any, result: ToolResult) -> dict[str, Any] | None:
        """原工具执行之后：真实成功才搬迁本轮归属，并告诉 loop 重建上下文。

        返回形状（与 Lead / loop 接线一致）：
        * rebind_topic_id / target_topic_id / operation_id / param_version /
          operation_status / conflict / stages / keep_decision_open；
        * keep_decision_open=True：操作尚未终结（查重冲突待用户确认），loop 不得
          把本轮推进到 ANSWERABLE，否则同版本合法重试会被当成迟到调用丢弃；
        * {"result": ToolResult}：后置步骤失败，用这个诚实的失败结果替换；
        * None：没有待处理申请（或工具失败，失败已由工具结果本身表达）。
        """
        call_id = str(getattr(call, "id", "") or "")
        operation = self._call_operations.pop(call_id, None) if call_id else None
        self._unbind(call_id)
        pending = self._approved.pop(call_id, None) if call_id else None
        if pending is None:
            return None
        if operation is None:
            operation = self.registry.by_operation_id(pending.operation_id)
        self._active_operation = operation

        if not result.ok:
            return await self._after_tool_failure(pending, operation, result)

        return await self._after_tool_success(pending, operation, result)

    async def _after_tool_failure(
        self,
        pending: _PendingApplication,
        operation: TopicOperation | None,
        result: ToolResult,
    ) -> dict[str, Any]:
        code = str(getattr(result, "code", "") or "")
        facts = getattr(result, "facts", None)
        facts = facts if isinstance(facts, dict) else {}
        if not code:
            code = str(facts.get("code") or "")
        key = (pending.operation_id, int(pending.param_version))
        failure_text = redact_text(str(result.error or result.content or ""))[:200]
        _trace(
            self.ctx,
            "topic_application_tool_failed",
            f"{pending.kind}:{code or result.error or result.content or ''}"[:300],
        )
        if operation is not None:
            operation.stages.tool_success = False
            operation.stages.failure = failure_text

        # 冲突类失败不是终态：审批历史保持「已批准（等待用户处理冲突）」，
        # 由同版本重试后的真实执行结果收口，不能写成用户拒绝或执行失败。
        pending_codes = {
            "topic_create_conflict",
            "topic_create_conflict_timeout",
            "topic_create_conflict_failed",
        }
        if code not in pending_codes:
            await self._settle_legacy(pending.approval_id, ok=False, error=result.error)

        if code == "nav_version_conflict":
            # 实际写入前的 CAS 失败：起点已经变了，绝不执行；失效 ≠ 拒绝。
            if operation is not None:
                operation.decide(DecisionStatus.STALE)
                operation.conflict = ConflictStatus.NONE
                operation.fail_attempt(AttemptStatus.TERMINAL_FAILURE, code=code)
                operation.stages.failed_stage = "navigation"
            self._terminal_results[key] = result
            self._set_phase(TopicPhase.ANSWERABLE)
            return self._directive(
                operation, pending, keep_decision_open=False, target=None
            )
        if code == "topic_create_conflict":
            # 查重冲突独立状态：冲突 ≠ 创建成功 ≠ 整轮结束（同版本可重试确认）。
            if operation is not None:
                operation.conflict = ConflictStatus.CONFLICT_PENDING
                operation.fail_attempt(
                    AttemptStatus.CONFLICT_PENDING,
                    conflict=ConflictStatus.CONFLICT_PENDING,
                    code=code,
                )
                operation.stages.failed_stage = "conflict"
            return self._directive(
                operation, pending, keep_decision_open=True, target=None
            )
        if code in ("topic_create_conflict_timeout", "topic_create_conflict_failed"):
            # 超时 / 技术错误 ≠ 主动拒绝（F10）：不写拒绝证据；同版本可再次进入
            # 具体冲突确认（F02），所以保持决策开放而不是收口成正式回答。
            if operation is not None:
                operation.conflict = ConflictStatus.CONFLICT_PENDING
                operation.fail_attempt(
                    AttemptStatus.RETRYABLE_FAILURE,
                    conflict=ConflictStatus.CONFLICT_PENDING,
                    code=code,
                )
                operation.stages.failed_stage = "conflict"
            return self._directive(
                operation, pending, keep_decision_open=True, target=None
            )
        if code == "topic_create_conflict_declined":
            # F10：具体冲突的拒绝进入统一终态 —— REFUSED + CONFLICT_DECLINED、
            # 清待处理冲突、进入原话题回答；证据只覆盖**这个具体对象**。
            if operation is not None:
                operation.decide(DecisionStatus.REFUSED)
                operation.fail_attempt(
                    AttemptStatus.TERMINAL_FAILURE,
                    conflict=ConflictStatus.CONFLICT_DECLINED,
                    code=code,
                )
                operation.stages.failed_stage = "conflict_declined"
            self.record_refusal(
                kind=pending.kind,
                topic_id=str(facts.get("similar_topic_id") or ""),
                topic_name=str(pending.new_name or facts.get("target_name") or ""),
                reason="conflict_declined",
                operation=operation,
                conflict=ConflictStatus.CONFLICT_DECLINED,
                similar_topic_id=(
                    str(facts.get("similar_topic_id")) if facts.get("similar_topic_id") else None
                ),
            )
            self._terminal_results[key] = result
            self._set_phase(TopicPhase.ANSWERABLE)
            return self._directive(
                operation, pending, keep_decision_open=False, target=None
            )
        if code == "topic_create_conflict_stale":
            # 冲突确认期间的失效：失效 ≠ 拒绝，不写拒绝证据、不重弹。
            if operation is not None:
                operation.decide(DecisionStatus.STALE)
                operation.conflict = ConflictStatus.NONE
                operation.fail_attempt(AttemptStatus.TERMINAL_FAILURE, code=code)
                operation.stages.failed_stage = "conflict"
            self._terminal_results[key] = result
            self._set_phase(TopicPhase.ANSWERABLE)
            return self._directive(
                operation, pending, keep_decision_open=False, target=None
            )
        if code == "topic_create_conflict_cancelled":
            # 本轮被用户停止：不是对这次话题申请的主动拒绝。
            if operation is not None:
                operation.decide(DecisionStatus.REFUSED)
                operation.conflict = ConflictStatus.CONFLICT_PENDING
                operation.fail_attempt(AttemptStatus.TERMINAL_FAILURE, code=code)
                operation.stages.failed_stage = "conflict"
            self._terminal_results[key] = result
            return self._directive(
                operation, pending, keep_decision_open=False, target=None
            )
        # 普通工具失败：工具已经执行并失败，不可重试 —— 在原话题回答（不搬归属）。
        if operation is not None:
            operation.decide(DecisionStatus.APPROVED)
            operation.fail_attempt(AttemptStatus.TERMINAL_FAILURE, code=code or "tool_failure")
            operation.stages.failed_stage = "tool"
        self._terminal_results[key] = result
        self._set_phase(TopicPhase.ANSWERABLE)
        return self._directive(operation, pending, keep_decision_open=False, target=None)

    async def _after_tool_success(
        self,
        pending: _PendingApplication,
        operation: TopicOperation | None,
        result: ToolResult,
    ) -> dict[str, Any]:
        facts = getattr(result, "facts", None)
        facts = facts if isinstance(facts, dict) else {}
        key = (pending.operation_id, int(pending.param_version))
        if operation is not None:
            operation.stages.tool_success = True
            operation.stages.failed_stage = ""
            operation.stages.failure = ""
        topic_id = self._new_topic_after(pending)
        if not topic_id:
            if operation is not None:
                # 工具真实成功（不可回滚），但本轮归属没搬过去：这是归属失败。
                operation.fail_attempt(
                    AttemptStatus.SUCCEEDED, code="topic_target_missing"
                )
                operation.stages.failed_stage = "ownership"
                operation.stages.failure = "工具报告成功，但找不到目标话题"
            return await self._rebind_failure(
                pending,
                "工具报告成功，但找不到目标话题（未搬迁本轮归属）",
                code="topic_target_missing",
                operation=operation,
            )
        if operation is not None:
            operation.stages.navigation_applied = True
        rebinder = self.rebind_turn
        if not callable(rebinder):
            if operation is not None:
                operation.fail_attempt(
                    AttemptStatus.SUCCEEDED, code="rebind_unavailable"
                )
                operation.stages.failed_stage = "ownership"
                operation.stages.failure = "没有可用的本轮归属搬迁入口"
            return await self._rebind_failure(
                pending,
                "没有可用的本轮归属搬迁入口（未搬迁本轮归属）",
                code="rebind_unavailable",
                operation=operation,
            )
        try:
            await rebinder(topic_id)
        except Exception as exc:  # noqa: BLE001 - 转成诚实失败结果，不假成功
            detail = sanitize_error_text(str(exc))
            await self._settle_legacy(
                pending.approval_id,
                ok=True,
                summary=f"话题已变更，但本轮归属迁移失败：{detail[:160]}",
            )
            if operation is not None:
                # 工具真实成功（不可回滚），归属迁移失败：两层状态分开记录。
                operation.fail_attempt(
                    AttemptStatus.SUCCEEDED, code="rebind_failed"
                )
                operation.stages.failed_stage = "ownership"
                operation.stages.failure = detail[:200]
            return await self._rebind_failure(
                pending,
                f"话题已变更，但本轮归属迁移失败：{detail[:160]}",
                code="rebind_failed",
                settle=False,
                operation=operation,
            )
        self.last_rebind_topic = topic_id
        if operation is not None:
            operation.succeed_attempt()
            operation.stages.turn_ownership = True
            operation.target_topic_id = topic_id
            if pending.kind == "topic_create":
                operation.new_name = pending.new_name
        # F05/F12：真实导航结果（写入后的版本 / 目标 / 片段）随 directive 回传，
        # A 用它调用 note_decision_navigation；读不到就 None，绝不猜一个版本。
        nav_version = facts.get("nav_version")
        fragment_id = facts.get("fragment_id")
        if nav_version is None:
            nav_version = self._nav_version()
        self.last_navigation = {
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "nav_version": nav_version,
        }
        self._results[key] = result
        _trace(
            self.ctx,
            "topic_application_rebind",
            f"{pending.kind}:{pending.from_topic}->{topic_id}",
        )
        await self._settle_legacy(
            pending.approval_id,
            ok=True,
            summary=f"已执行：{pending.what()}",
        )
        # 话题已换、上下文未装配：只有 loop 的 build_system 成功后才 ANSWERABLE。
        self._set_phase(TopicPhase.CONTEXT_PREPARING)
        directive = self._directive(
            operation,
            pending,
            keep_decision_open=False,
            target=topic_id,
            rebind_topic_id=topic_id,
        )
        directive["nav_version"] = nav_version
        directive["topic_id"] = topic_id
        directive["fragment_id"] = fragment_id
        return directive

    async def _rebind_failure(
        self,
        pending: _PendingApplication,
        message: str,
        *,
        code: str,
        settle: bool = True,
        operation: TopicOperation | None = None,
    ) -> dict[str, Any]:
        _trace_warning(self.ctx, code, message)
        if settle:
            await self._settle_legacy(pending.approval_id, ok=False, error=message)
        directive = self._directive(
            operation,
            pending,
            keep_decision_open=False,
            target=None,
            result=ToolResult(
                ok=False,
                content=message + "。话题工具本身已经执行，这个事实不会回滚；"
                "请不要重复调用话题工具。",
                error=message,
                category="unknown",
                recoverable=False,
                code=code,
                facts={
                    "topic_application": "ownership_failed",
                    "failed_stage": "ownership",
                    "ownership_failed": True,
                    "operation_id": pending.operation_id,
                    "param_version": int(pending.param_version),
                },
            ),
        )
        # F06：结构化事实 —— 工具/导航成功但归属没搬过去。A 据此停止本轮、
        # 进入可恢复失败（不按错误文本猜）。
        directive["ownership_failed"] = True
        directive["failed_stage"] = "ownership"
        if operation is not None:
            operation.stages.failed_stage = "ownership"
            operation.stages.failure = operation.stages.failure or redact_text(message)[:200]
        return directive

    def _directive(
        self,
        operation: TopicOperation | None,
        pending: _PendingApplication,
        *,
        keep_decision_open: bool,
        target: str | None,
        rebind_topic_id: str | None = None,
        result: ToolResult | None = None,
    ) -> dict[str, Any]:
        stages = operation.stages.as_dict() if operation is not None else {}
        directive: dict[str, Any] = {
            "operation_id": pending.operation_id,
            "param_version": int(pending.param_version),
            "target_topic_id": target,
            "operation_status": (
                operation.status.value
                if operation is not None
                else OperationStatus.FAILED.value
            ),
            "conflict": (
                operation.conflict.value
                if operation is not None
                else ConflictStatus.NONE.value
            ),
            "stages": stages,
            "keep_decision_open": bool(keep_decision_open),
        }
        if rebind_topic_id:
            directive["rebind_topic_id"] = rebind_topic_id
        if operation is not None:
            directive["decided"] = operation.decided.value
            directive["attempt"] = operation.attempt.value
        if result is not None:
            directive["result"] = result
        return directive

    def _new_topic_after(self, pending: _PendingApplication) -> str | None:
        if pending.kind == "topic_switch":
            return pending.target_topic_id or None
        # create：工具成功后 Anchor 已经落到新话题；读真实 Anchor（而不是猜名字）。
        active = self._active_topic()
        if active and active != pending.from_topic and self._node(active) is not None:
            return active
        name = str(pending.new_name or "").strip()
        conn = self._conn()
        if conn is not None and name:
            try:
                row = conn.execute(
                    "SELECT id FROM nodes WHERE type = 'topic' AND name = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
            except Exception:  # noqa: BLE001
                logger.warning("failed to find created topic by name", exc_info=True)
                row = None
            if row is not None:
                value = row["id"] if not isinstance(row, dict) else row.get("id")
                if value:
                    return str(value)
        return None

    async def _settle_legacy(
        self,
        approval_id: str | None,
        *,
        ok: bool,
        error: str | None = None,
        summary: str | None = None,
    ) -> None:
        """把旧审批的真实执行结果写回历史/事件（工具执行边界口径一致）。"""
        if not approval_id:
            return
        approvals = getattr(self.app, "approvals", None)
        recorder = getattr(approvals, "record_legacy_execution", None)
        if not callable(recorder):
            return
        try:
            await recorder(
                approval_id,
                ok=bool(ok),
                error=redact_text(str(error)) if error else None,
                summary=redact_text(str(summary)) if summary else None,
            )
        except Exception:  # noqa: BLE001 - 历史收口失败不能改变工具结果
            logger.warning("failed to settle topic approval execution", exc_info=True)

    # -- loop 用的上下文重建入口 -------------------------------------------

    async def build_system(self, topic_id: str) -> str | None:
        """请 orchestrator 以目标话题重新装配 system 文本（不复制原话题历史）。

        失败必须 raise（Lead 冻结）：不能 warn 后返回旧 system，那会把「目标话题
        上下文没就绪」伪装成已就绪；由 loop 转成 RECOVERABLE_FAILURE + 恢复入口。
        """
        builder = self.rebuild_system
        if not callable(builder):
            raise RuntimeError("没有可用的目标话题上下文重建入口")
        return await builder(topic_id)

    # -- 通过审批后的内部状态 ---------------------------------------------

    def _bind(self, call: Any, operation: TopicOperation) -> None:
        call_id = str(getattr(call, "id", "") or "")
        if not call_id:
            return
        self._call_operations[call_id] = operation
        from agent.services.topic_operation import bind_operation

        try:
            self._bind_tokens[call_id] = bind_operation(operation)
        except Exception:  # noqa: BLE001 - ContextVar 绑定失败：工具仍按无绑定执行
            logger.warning("failed to bind topic operation context", exc_info=True)
        operation.remember_call(call_id)

    def _unbind(self, call_id: str) -> None:
        token = self._bind_tokens.pop(call_id, None)
        if token is None:
            return
        from agent.services.topic_operation import unbind_operation

        unbind_operation(token)

    def _remember(self, call: Any, pending: _PendingApplication) -> None:
        call_id = str(getattr(call, "id", "") or "") if call is not None else ""
        if not call_id:
            call_id = f"auto:{pending.kind}:{pending.operation_id}:{len(self._approved)}"
        self._approved[call_id] = pending

    def _pending_from_operation(
        self,
        operation: TopicOperation,
        kind: str,
        arguments: dict[str, Any],
        *,
        approval_id: str | None = None,
    ) -> _PendingApplication:
        """按操作身份 + 本次**执行参数**构造「待执行申请」（F07）。"""
        params = execution_params(kind, arguments)
        return _PendingApplication(
            kind=kind,
            operation_id=operation.operation_id,
            param_version=int(operation.param_version),
            key=self._target_key(kind, arguments),
            from_topic=operation.from_topic or self._from_topic(),
            base_nav_version=operation.base_nav_version,
            target_topic_id=(params["topic_id"] if kind == "topic_switch" else None),
            new_name=(params["name"] if kind == "topic_create" else None),
            approval_id=approval_id,
        )

    @staticmethod
    def _target_key(kind: str, arguments: dict[str, Any]) -> str:
        if kind == "topic_switch":
            return str(arguments.get("topic_id") or "").strip()
        return normalize_topic_name(arguments.get("name"))

    def _arguments_usable(self, kind: str, arguments: dict[str, Any]) -> bool:
        if kind == "topic_switch":
            target = str(arguments.get("topic_id") or "").strip()
            return bool(target) and self._node(target) is not None
        return bool(str(arguments.get("name") or "").strip())

    # -- 拒绝 / 重复 / 失效结果 -------------------------------------------

    def _duplicate_result(
        self, kind: str, arguments: dict[str, Any], *, direct: bool = False
    ) -> ToolResult:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        target = self._display_target(kind, arguments)
        template = DIRECT_RETRY_INSTRUCTION if direct else DUPLICATE_INSTRUCTION
        message = template.format(what=target, current=current)
        self._record_duplicate_refusal(kind, arguments)
        return ToolResult(
            ok=False,
            content=message,
            error="topic_application_duplicate",
            category="unknown",
            recoverable=False,
            facts={
                "topic_application": "duplicate",
                "kind": kind,
                **self._progress_facts(),
            },
            code="topic_application_duplicate",
        )

    def _record_duplicate_refusal(self, kind: str, arguments: dict[str, Any]) -> None:
        topic_id = self._target_key(kind, arguments)
        name = self._display_target(kind, arguments)
        _trace(self.ctx, "topic_application_duplicate", f"{kind}:{topic_id}")
        self.record_refusal(
            kind=kind,
            topic_id=topic_id,
            topic_name=name,
            reason="duplicate",
            operation=self._active_operation,
        )

    def _refusal_result(
        self,
        kind: str,
        arguments: dict[str, Any],
        *,
        reason: str,
        content: str | None = None,
        record_evidence: bool = True,
        operation: TopicOperation | None = None,
    ) -> ToolResult:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        target = self._display_target(kind, arguments)
        stale_like = reason in _STALE_REASONS
        if content is None:
            if stale_like:
                content = STALE_INSTRUCTION.format(
                    why=_reason_label(reason), current=current
                )
            else:
                content = REFUSAL_INSTRUCTION.format(what=target, current=current)
        if not stale_like and record_evidence:
            # 失效 ≠ 拒绝：目标/名称失效不写拒绝范围，避免污染「用户拒绝过」的证据。
            self.record_refusal(
                kind=kind,
                topic_id=self._target_key(kind, arguments),
                topic_name=target,
                reason=reason,
                operation=operation,
            )
        return ToolResult(
            ok=False,
            content=content,
            error=f"topic_application_{reason}",
            category=_category_for_reason(reason),
            recoverable=False,
            facts={
                "topic_application": reason,
                "kind": kind,
                "operation_id": (
                    (operation or self._active_operation).operation_id
                    if (operation or self._active_operation) is not None
                    else None
                ),
                **self._progress_facts(operation),
            },
            code=f"topic_application_{reason}",
        )

    def _executing_result(
        self, kind: str, arguments: dict[str, Any]
    ) -> ToolResult:
        """同一版本正在执行（同批重复调用）：不重复执行，也不伪造结果。"""
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        return ToolResult(
            ok=False,
            content=(
                "这次话题操作正在执行中，不能重复执行；请等结果返回后基于真实结果回答。"
                f"当前话题「{current}」。"
            ),
            error="topic application executing",
            category="unknown",
            recoverable=True,
            facts={
                "topic_application": "executing",
                "kind": kind,
                **self._progress_facts(),
            },
            code="topic_application_executing",
        )

    def _stale_result(
        self,
        kind: str,
        arguments: dict[str, Any],
        why: str,
        *,
        operation: TopicOperation | None = None,
    ) -> ToolResult:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        actual = self._nav_version()
        base = operation.base_nav_version if operation is not None else None
        _trace(
            self.ctx,
            "topic_application_stale",
            f"{kind}:{why}:base={base}:actual={actual}",
        )
        return ToolResult(
            ok=False,
            content=STALE_INSTRUCTION.format(why=_reason_label(why), current=current),
            error=f"topic_application_stale:{why}",
            category="unknown",
            recoverable=False,
            facts={
                "topic_application": "stale",
                "kind": kind,
                "why": why,
                "operation_id": operation.operation_id if operation is not None else None,
                "param_version": operation.param_version if operation is not None else None,
                "base_nav_version": base,
                "current_nav_version": actual,
                **self._progress_facts(operation),
            },
            code="topic_application_stale",
        )

    def _no_channel_content(self, kind: str, arguments: dict[str, Any]) -> str:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        return (
            "当前没有可用的审批通道，话题变更没有执行。"
            f"请直接在当前话题「{current}」回答用户原来的问题。"
        )

    def _display_target(self, kind: str, arguments: dict[str, Any]) -> str:
        if kind == "topic_switch":
            topic_id = str(arguments.get("topic_id") or "").strip()
            return self._node_name(self._node(topic_id)) or topic_id or "目标话题"
        return str(arguments.get("name") or "").strip() or "新话题"


#: 这些原因都按「申请失效 / 不可执行」口径回答（不是用户拒绝）。
_STALE_REASONS = frozenset(
    {"stale", "topic_target_missing", "topic_name_required", "duplicate", "nav_version_conflict"}
)

_REASON_LABELS = {
    "stale": "等待期间话题位置已改变",
    "nav_version_changed": "等待期间话题位置已改变（导航版本已变化）",
    "application_invalidated": "申请已失效",
    "active_anchor_changed": "等待期间已经导航到别的话题",
    "binding_changed": "本轮归属已改变",
    "binding_closed": "本轮已经收尾",
    "turn_topic_changed": "本轮话题已改变",
    "missing_from_topic": "不知道申请是从哪个话题发起的",
    "topic_target_missing": "目标话题已不存在",
    "topic_name_required": "话题名不能为空",
    "no_approval_channel": "当前没有审批通道",
    "duplicate": "本轮已经处理过同一段讨论的申请",
}


def _reason_label(reason: str) -> str:
    return _REASON_LABELS.get(str(reason or ""), "申请已失效")


def _category_for_reason(reason: str) -> str:
    """反馈层类别（见 core/tool_feedback.py 的 ErrorCategory）：不虚构「代码异常」。"""
    reason = str(reason or "")
    if reason == "timeout":
        return "timeout"
    if reason == "cancelled":
        return "cancelled"
    if reason in ("rejected", "no_approval_channel"):
        return "permission"
    return "unknown"


def get_topic_application_state(app: Any) -> TopicDiscussionGuard:
    """读取（必要时创建）app 上的话题申请内存状态（A 的 topic_decision 用）。"""
    state = getattr(app, "topic_application_state", None)
    if not isinstance(state, TopicDiscussionGuard):
        state = TopicDiscussionGuard()
        try:
            setattr(app, "topic_application_state", state)
        except Exception:  # noqa: BLE001 - 只读 app 时退回新对象（不写回）
            logger.warning("failed to attach topic application state", exc_info=True)
    return state


__all__ = [
    "TopicApplicationBridge",
    "TopicDiscussionGuard",
    "RefusedAttempt",
    "TOPIC_TOOL_KINDS",
    "TOPIC_APPLICATION_KINDS",
    "MAX_APPLICATIONS_PER_TURN",
    "get_topic_application_state",
]
