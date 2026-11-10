"""话题申请的执行桥（契约 §2.3，B 的所有权）。

模型需要换题时仍然调用**原有**的 `switch_topic` / `create_topic`；本模块在
`ToolRegistry.execute` 之前拦截它们，按「用户先确认」的产品行为处理：

1. 本轮用户**已经明确要求**这次切换 / 新建（`ctx.explicit_target` /
   `ctx.explicit_create_name`）→ 不弹普通申请，直接记 trace 后放行；
2. `TopicDiscussionGuard` 判定同轮重复申请 / 同一段讨论已拒绝 → 直接返回失败
   结果，让模型在原话题作答（不再打扰用户）；
3. 否则用 `ApprovalService.request(kind, payload)` 建立申请（复用既有身份 /
   600s 有效期 / 单次消费 / pending_approvals 持久化 / APPROVAL_REQUIRED 事件），
   本轮在这里等待用户决定；
4. 批准 → 校验 edits（新话题名）与位置版本（active anchor / 本轮 binding 仍是
   from_topic）→ 用（可能被改写的）参数放行原工具；工具真实成功之后
   `after_tool` 调注入的 `rebind_turn(topic_id)` 把本轮归属搬到目标话题，
   再由 loop 调 `build_system(topic_id)` 重建目标话题上下文；
5. 拒绝 / 超时 / 取消 / 过期 / stale → 工具**不执行**，记录拒绝范围（内存，
   不新增表），返回「请直接在当前话题回答、不要重复询问」的失败结果。

设计边界：
* 本模块**不**直接执行话题工具、不写 topic/fragment/anchor —— 执行仍走 registry，
  归属搬迁由 orchestrator 注入的闭包负责（唯一写入者不变）；
* 拒绝范围只按 `ctx.topic_refusal_scope` / `app.topic_application_state` 提供证据，
  跨轮不做程序强制（契约：跨轮由证据文案 + 模型判断）；
* 不新增持久化：Guard 是进程内内存状态，随 app 生命周期存在。

需要 Lead 在 execute_loop 里接线（参数已与 Lead 对齐）::

    bridge = TopicApplicationBridge(
        app, ctx, adapter=adapter, plan=plan,
        rebind_turn=lambda topic_id: self.rebind_turn_to_topic(ctx, topic_id),
        rebuild_system=lambda topic_id: self.rebuild_system_after_topic_application(
            ctx, adapter, plan, topic_id
        ),
    )
    loop = AgentLoop(..., topic_application=bridge)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from agent.tools.base import ToolResult
from agent.trace.redact import redact_text, sanitize_error_text

logger = logging.getLogger(__name__)

#: 被拦截的话题工具 → 审批 kind（契约 §2.1）。
TOPIC_TOOL_KINDS = {
    "switch_topic": "topic_switch",
    "create_topic": "topic_create",
}
TOPIC_APPLICATION_KINDS = frozenset(TOPIC_TOOL_KINDS.values())

#: 申请原因只用于展示，最多 80 字（契约 §2.1）。
REASON_LIMIT = 80
#: Guard 的内存上限（拒绝记录 / 轮次记录）：只作为证据，不无限增长。
MAX_REFUSALS = 20
MAX_TURNS = 32

REFUSAL_INSTRUCTION = (
    "用户没有同意发起这次话题变更（{what}）。现在**不要**调用 switch_topic / "
    "create_topic，也不要再次询问用户是否要换话题；请直接在当前话题「{current}」"
    "回答用户原来的问题。"
)
STALE_INSTRUCTION = (
    "话题申请已失效（{why}），本次没有执行。不要重复提交申请，请直接在当前话题"
    "「{current}」回答用户原来的问题。"
)
DUPLICATE_INSTRUCTION = (
    "本轮已经处理过同一段讨论的话题申请（{what}），不会再次弹出确认。"
    "请直接在当前话题「{current}」回答用户原来的问题，不要重复申请。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_name(value: Any) -> str:
    """话题名比较口径：去空白与常见引号（不改变真实落库名字）。"""
    text = str(value or "").strip()
    for ch in " 　\t\r\n\"'“”「」『』《》：:":
        text = text.replace(ch, "")
    return text


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
    """一次被拒绝 / 失效的话题申请（供 A 的 topic_decision 作为证据读取）。

    只记录真实发生过的事实：哪个话题方向、哪种动作、为什么没成、属于哪一轮 /
    哪条用户消息 / 哪个绑定片段。**不是**对用户语义的猜测。
    """

    topic_id: str
    topic_name: str
    action: str  # "switch" | "create"
    reason: str  # rejected / timeout / cancelled / stale / invalid_target / duplicate
    turn_id: str
    user_message_id: str | None = None
    fragment_id: str | None = None
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
            "created_at": self.created_at,
        }


class TopicDiscussionGuard:
    """话题申请的内存守卫与拒绝范围记录（`app.topic_application_state`）。

    职责（不新增表）：
    * 同轮同工具重复申请拦截：同一轮里已经决定过的申请不再弹第二次；已拒绝的
      同一段讨论直接返回失败结果（由 bridge 使用）；
    * 拒绝范围证据：`refused_attempts` 给 A 的 `build_topic_decision_state` 只读；
    * 有界：只保留最近 `MAX_TURNS` 轮 / `MAX_REFUSALS` 条拒绝记录。
    """

    def __init__(self, *, max_refusals: int = MAX_REFUSALS, max_turns: int = MAX_TURNS) -> None:
        self._decisions: dict[str, dict[str, str]] = {}
        self._turn_order: list[str] = []
        self._refusals: list[RefusedAttempt] = []
        self._max_refusals = max(1, int(max_refusals))
        self._max_turns = max(1, int(max_turns))

    # -- 同轮申请状态 ------------------------------------------------------

    def _turn_slot(self, turn_id: str) -> dict[str, str]:
        slot = self._decisions.get(turn_id)
        if slot is None:
            slot = {}
            self._decisions[turn_id] = slot
            self._turn_order.append(turn_id)
            self._prune_turns()
        return slot

    def _prune_turns(self) -> None:
        while len(self._turn_order) > self._max_turns:
            oldest = self._turn_order.pop(0)
            self._decisions.pop(oldest, None)

    def application_decision(self, turn_id: str, kind: str, key: str) -> str | None:
        """同轮同 (kind, key) 的申请决定：pending / approved / refused / None。"""
        return self._decisions.get(turn_id, {}).get(f"{kind}|{key}")

    def turn_application(self, turn_id: str) -> str | None:
        """本轮是否已经有过一次话题申请（不管目标）：返回任意一条键。"""
        slot = self._decisions.get(turn_id) or {}
        return next(iter(slot), None)

    def note_application(self, turn_id: str, kind: str, key: str) -> None:
        self._turn_slot(turn_id)[f"{kind}|{key}"] = "pending"

    def note_decision(self, turn_id: str, kind: str, key: str, decision: str) -> None:
        self._turn_slot(turn_id)[f"{kind}|{key}"] = str(decision)

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
    ) -> RefusedAttempt:
        attempt = RefusedAttempt(
            topic_id=str(topic_id or ""),
            topic_name=str(topic_name or ""),
            action=str(action or ""),
            reason=str(reason or ""),
            turn_id=str(turn_id or ""),
            user_message_id=user_message_id,
            fragment_id=fragment_id,
        )
        self._refusals.append(attempt)
        if len(self._refusals) > self._max_refusals:
            del self._refusals[: len(self._refusals) - self._max_refusals]
        return attempt

    @property
    def refused_attempts(self) -> list[dict[str, Any]]:
        """按时间顺序（旧 → 新）返回最近被拒绝的申请，供 A 渲染证据。"""
        return [item.as_dict() for item in self._refusals]

    def refusals_for(self, topic_id: str) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._refusals if item.topic_id == topic_id]

    def refusal_scope(self, topic_id: str | None = None) -> dict[str, Any] | None:
        """最近一条（指定话题的）拒绝范围；没有就是 None（不猜）。"""
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
    key: str
    from_topic: str
    target_topic_id: str | None = None
    new_name: str | None = None
    approval_id: str | None = None
    explicit: bool = False
    edits_applied: bool = False

    def what(self) -> str:
        if self.kind == "topic_switch":
            return f"切换到其他话题（{self.target_topic_id or ''}）"
        return f"新建话题「{self.new_name or ''}」"


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
    ) -> None:
        self.app = app
        self.ctx = ctx
        # adapter / plan 目前只用于让 orchestrator 的闭包拿到同一份本轮上下文；
        # bridge 自己不读它们的内容（避免复制上下文装配逻辑）。
        self.adapter = adapter
        self.plan = plan
        self.rebind_turn = rebind_turn
        self.rebuild_system = rebuild_system
        state = getattr(app, "topic_application_state", None)
        if not isinstance(state, TopicDiscussionGuard):
            state = TopicDiscussionGuard()
            try:
                setattr(app, "topic_application_state", state)
            except Exception:  # noqa: BLE001 - 只读 app 对象时退化为桥内状态
                logger.warning("failed to attach topic application state", exc_info=True)
        self.guard = state
        # call_id → 已批准的申请；工具执行成功后由 after_tool 消费。
        self._approved: dict[str, _PendingApplication] = {}
        # 最近一次为哪次调用重建了目标话题上下文（诊断 / 测试断言用）。
        self.last_rebind_topic: str | None = None

    # -- 上下文辅助 --------------------------------------------------------

    @property
    def turn_id(self) -> str:
        return str(getattr(self.ctx, "turn_id", "") or "")

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
        conn = getattr(self.app, "conn", None)
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

    # -- payload（契约 §2.1）-----------------------------------------------

    def payload(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """严格按契约 §2.1 构造审批载荷（不放原问题 / 摘要 / 预览）。"""
        arguments = dict(arguments or {})
        from_topic = self._from_topic()
        payload: dict[str, Any] = {
            "action": "switch" if kind == "topic_switch" else "create",
            "turn_id": self.turn_id,
            "from_topic_id": from_topic,
            "from_topic_name": self._from_topic_name(),
            "reason": _short_reason(arguments.get("reason")),
            "source": "model",
        }
        if kind == "topic_switch":
            target_id = str(arguments.get("topic_id") or "").strip()
            payload["target_topic_id"] = target_id
            payload["target_topic_name"] = self._node_name(self._node(target_id))
        else:
            payload["new_name"] = str(arguments.get("name") or "").strip()
        return payload

    # -- 拒绝范围 ----------------------------------------------------------

    def record_refusal(
        self,
        *,
        kind: str,
        topic_id: str,
        topic_name: str,
        reason: str,
        turn_id: str | None = None,
    ) -> RefusedAttempt:
        """记录一次没成的话题申请，并把范围挂到 `ctx.topic_refusal_scope`。"""
        attempt = self.guard.record_refusal(
            topic_id=topic_id,
            topic_name=topic_name,
            action="switch" if kind == "topic_switch" else "create",
            reason=reason,
            turn_id=str(turn_id or self.turn_id),
            user_message_id=self._user_message_id(),
            fragment_id=self._fragment_id(),
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

    # -- 明确要求（用户本轮已说清楚，不再弹普通申请）----------------------

    def explicit_requirement(self, kind: str, arguments: dict[str, Any]) -> str | None:
        """本轮用户是否已明确要求这次具体变更：返回证据名或 None。"""
        if kind == "topic_switch":
            target = str(arguments.get("topic_id") or "").strip()
            explicit = str(getattr(self.ctx, "explicit_target", None) or "").strip()
            if target and explicit and target == explicit:
                return "explicit_navigation"
            return None
        name = _norm_name(arguments.get("name"))
        explicit_name = _norm_name(getattr(self.ctx, "explicit_create_name", None))
        if name and explicit_name and name == explicit_name:
            return "explicit_creation"
        return None

    # -- 申请建立 ----------------------------------------------------------

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

        explicit = self.explicit_requirement(kind, arguments)
        if explicit:
            _trace(self.ctx, "topic_application_explicit", f"{kind}:{explicit}")
            pending = self._pending_from_arguments(kind, arguments)
            pending.explicit = True
            self._remember(call, pending)
            return None

        key = self._target_key(kind, arguments)
        if kind == "topic_switch" and key == self._from_topic():
            # 已经在目标话题里：这是一次幂等的「再切一次」，没有需要用户确认的变更。
            _trace(self.ctx, "topic_application_noop", f"{kind}:{key}")
            return None

        decision = self.guard.application_decision(self.turn_id, kind, key)
        if decision == "approved":
            # 用户本轮已批准过同一个目标：模型重试时直接执行（create 的查重 /
            # 相似冲突二次审批仍在工具内部保留，不跳过）。这条重试路径同样要
            # 在工具真实成功后搬迁归属 —— 记下待执行申请。
            _trace(self.ctx, "topic_application_reuse_approval", f"{kind}:{key}")
            self._remember(call, self._pending_from_arguments(kind, arguments))
            return None
        if decision in ("refused", "pending"):
            return self._duplicate_result(kind, arguments)
        if self.guard.turn_application(self.turn_id) is not None:
            # 同轮只处理一次话题申请：第二次（即使目标不同）不再打扰用户。
            return self._duplicate_result(kind, arguments)

        conflict = await self._request_application(call, kind, arguments)
        if conflict is not None:
            return conflict
        # 用户可能改了新建话题名（edits）：原工具必须用校正后的参数执行。
        pending = self._approved.get(str(getattr(call, "id", "") or ""))
        if pending is not None and pending.edits_applied:
            self._apply_edited_arguments(call, pending)
        return None

    def _arguments_usable(self, kind: str, arguments: dict[str, Any]) -> bool:
        if kind == "topic_switch":
            target = str(arguments.get("topic_id") or "").strip()
            return bool(target) and self._node(target) is not None
        return bool(str(arguments.get("name") or "").strip())

    def _pending_from_arguments(
        self, kind: str, arguments: dict[str, Any], *, approval_id: str | None = None
    ) -> _PendingApplication:
        """按本次调用参数构造「待执行申请」（明确要求 / 已批准重试路径共用）。"""
        return _PendingApplication(
            kind=kind,
            key=self._target_key(kind, arguments),
            from_topic=self._from_topic(),
            target_topic_id=(
                str(arguments.get("topic_id") or "").strip()
                if kind == "topic_switch"
                else None
            ),
            new_name=(
                str(arguments.get("name") or "").strip() if kind == "topic_create" else None
            ),
            approval_id=approval_id,
        )

    @staticmethod
    def _target_key(kind: str, arguments: dict[str, Any]) -> str:
        if kind == "topic_switch":
            return str(arguments.get("topic_id") or "").strip()
        return _norm_name(arguments.get("name"))

    async def _request_application(
        self, call: Any, kind: str, arguments: dict[str, Any]
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
            )

        key = self._target_key(kind, arguments)
        payload = self.payload(kind, arguments)
        self.guard.note_application(self.turn_id, kind, key)
        _trace(
            self.ctx,
            "topic_application_requested",
            f"{kind}:{key}:from={self._from_topic()}",
        )
        try:
            with _phase_cm(self.ctx, "approval_wait", f"topic_application:{kind}"):
                result = await requester(kind, payload)
        except asyncio.CancelledError:
            # 用户按了停止：审批随之结束（ApprovalService 已发 APPROVAL_RESULT）。
            self.guard.note_decision(self.turn_id, kind, key, "cancelled")
            _trace(self.ctx, "topic_application_cancelled", f"{kind}:{key}")
            return ToolResult(
                ok=False,
                content="本轮已停止，话题申请没有执行。",
                error="turn cancelled",
                category="cancelled",
                status="cancelled",
            )

        decision = str(getattr(result, "decision", "") or "")
        if decision != "approved":
            self.guard.note_decision(self.turn_id, kind, key, "refused")
            reason = decision or "rejected"
            return self._refusal_result(kind, arguments, reason=reason)

        # 已批准：校验位置版本与 edits，再决定是否放行原工具。
        approval_id = str(getattr(result, "approval_id", "") or "") or None
        pending = self._approval_to_pending(
            kind, arguments, result, approval_id=approval_id
        )
        conflict = await self.execute_approved(kind, arguments, pending, result)
        if conflict is not None:
            self.guard.note_decision(self.turn_id, kind, key, "refused")
            if approval_id:
                await self._settle_legacy(approval_id, ok=False, error=conflict.error)
            return conflict
        self.guard.note_decision(self.turn_id, kind, key, "approved")
        self._remember(call, pending)
        return None

    def _approval_to_pending(
        self,
        kind: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        approval_id: str | None,
    ) -> _PendingApplication:
        pending = _PendingApplication(
            kind=kind,
            key=self._target_key(kind, arguments),
            from_topic=self._from_topic(),
            target_topic_id=(
                str(arguments.get("topic_id") or "").strip()
                if kind == "topic_switch"
                else None
            ),
            new_name=(
                str(arguments.get("name") or "").strip() if kind == "topic_create" else None
            ),
            approval_id=approval_id,
        )
        edits = getattr(result, "edits", None)
        if kind == "topic_create" and isinstance(edits, dict):
            name = str(edits.get("name") or "").strip()
            if name:
                pending.new_name = name
                pending.edits_applied = name != str(arguments.get("name") or "").strip()
        return pending

    async def execute_approved(
        self,
        kind: str,
        arguments: dict[str, Any],
        pending: _PendingApplication,
        result: Any = None,
    ) -> ToolResult | None:
        """批准之后、原工具执行之前：校验 edits / 目标 / 位置版本。

        返回 None = 允许原工具按（可能被 edits 改写后的）参数执行；
        返回 ToolResult = 不执行（stale / 目标不存在 / 名称无效）。
        """
        # edits 名称（create 可被用户改写；空名称在后端 API 已被 400 拒绝，
        # 这里再兜一次，保证任何调用方都不会用空名称创建）。
        if kind == "topic_create":
            if not str(pending.new_name or "").strip():
                return self._refusal_result(
                    kind, arguments, reason="topic_name_required"
                )
        else:
            target = self._node(pending.target_topic_id)
            if target is None:
                return self._refusal_result(
                    kind, arguments, reason="topic_target_missing"
                )

        # 位置版本：用户等待期间可能手动导航 / 本轮绑定被收尾 —— 那都算 stale，
        # 不执行、不覆盖用户的新选择（契约 §2.2）。
        stale_why = self._stale_reason(pending.from_topic)
        if stale_why:
            return self._stale_result(kind, arguments, stale_why)
        return None

    def _apply_edited_arguments(
        self, call: Any, pending: _PendingApplication
    ) -> None:
        """把用户批准的新名称写回 call.arguments（原地改 dict，事件/历史同源）。

        ToolCall 是 frozen dataclass，但 arguments 是普通 dict：原地改它，registry
        的 TOOL_START/TOOL_END、工具历史与回填给模型的结果看到的都是校正后的值。
        """
        name = str(pending.new_name or "").strip()
        arguments = getattr(call, "arguments", None)
        if not name or not isinstance(arguments, dict):
            return
        if str(arguments.get("name") or "").strip() == name:
            return
        arguments["name"] = name
        _trace(self.ctx, "topic_application_edited", f"create:{name}")

    def _stale_reason(self, from_topic: str) -> str | None:
        if not from_topic:
            return "missing_from_topic"
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
        active = self._active_topic()
        if active is not None and active != from_topic:
            return "active_anchor_changed"
        current = str(getattr(self.ctx, "bound_topic", None) or "")
        if current and current != from_topic:
            return "turn_topic_changed"
        return None

    # -- 工具结果之后的归属搬迁 -------------------------------------------

    async def after_tool(self, call: Any, result: ToolResult) -> dict[str, Any] | None:
        """原工具执行之后：真实成功才搬迁本轮归属，并告诉 loop 重建上下文。

        返回形状（与 Lead 接线一致）：
        * {"rebind_topic_id": str} → loop 在工具结果回填后重建 system；
        * {"result": ToolResult} → 后置步骤失败，用这个诚实的失败结果替换；
        * None → 没有待处理申请（或工具失败，失败已由工具结果本身表达）。
        """
        call_id = str(getattr(call, "id", "") or "")
        pending = self._approved.pop(call_id, None) if call_id else None
        if pending is None:
            return None

        if not result.ok:
            # 工具真实失败：不搬归属；把真实结果留在审批历史里（失败状态真实）。
            await self._settle_legacy(pending.approval_id, ok=False, error=result.error)
            _trace(
                self.ctx,
                "topic_application_tool_failed",
                f"{pending.kind}:{result.error or result.content or ''}"[:300],
            )
            return None

        topic_id = self._new_topic_after(pending)
        if not topic_id:
            return await self._rebind_failure(
                pending,
                "工具报告成功，但找不到目标话题（未搬迁本轮归属）",
                code="topic_target_missing",
            )
        rebinder = self.rebind_turn
        if not callable(rebinder):
            return await self._rebind_failure(
                pending,
                "没有可用的本轮归属搬迁入口（未搬迁本轮归属）",
                code="rebind_unavailable",
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
            return await self._rebind_failure(
                pending,
                f"话题已变更，但本轮归属迁移失败：{detail[:160]}",
                code="rebind_failed",
                settle=False,
            )
        self.last_rebind_topic = topic_id
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
        return {"rebind_topic_id": topic_id}

    async def _rebind_failure(
        self,
        pending: _PendingApplication,
        message: str,
        *,
        code: str,
        settle: bool = True,
    ) -> dict[str, Any]:
        _trace_warning(self.ctx, code, message)
        if settle:
            await self._settle_legacy(pending.approval_id, ok=False, error=message)
        return {
            "result": ToolResult(
                ok=False,
                content=message + "。话题工具本身已经执行，这个事实不会回滚；"
                "请不要重复调用话题工具。",
                error=message,
                category="unknown",
                recoverable=False,
            )
        }

    def _new_topic_after(self, pending: _PendingApplication) -> str | None:
        if pending.kind == "topic_switch":
            return pending.target_topic_id or None
        # create：工具成功后 Anchor 已经落到新话题；读真实 Anchor（而不是猜名字）。
        active = self._active_topic()
        if active and active != pending.from_topic and self._node(active) is not None:
            return active
        name = str(pending.new_name or "").strip()
        conn = getattr(self.app, "conn", None)
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
        """请 orchestrator 以目标话题重新装配 system 文本（不复制原话题历史）。"""
        builder = self.rebuild_system
        if not callable(builder):
            _trace_warning(self.ctx, "topic_rebuild_unavailable", "没有上下文重建入口")
            return None
        return await builder(topic_id)

    # -- 通过审批后的内部状态 ---------------------------------------------

    def _remember(self, call: Any, pending: _PendingApplication) -> None:
        call_id = str(getattr(call, "id", "") or "") if call is not None else ""
        if not call_id:
            # 明确要求的路径也会走 after_tool，但这里必须能按 call 找到。
            call_id = f"auto:{pending.kind}:{pending.key}:{len(self._approved)}"
        self._approved[call_id] = pending

    # -- 拒绝 / 重复结果 ---------------------------------------------------

    def _duplicate_result(self, kind: str, arguments: dict[str, Any]) -> ToolResult:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        target = self._display_target(kind, arguments)
        message = DUPLICATE_INSTRUCTION.format(what=target, current=current)
        self._record_duplicate_refusal(kind, arguments)
        return ToolResult(
            ok=False,
            content=message,
            error="topic_application_duplicate",
            category="unknown",
            recoverable=False,
            facts={"topic_application": "duplicate", "kind": kind},
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
        )

    def _refusal_result(
        self,
        kind: str,
        arguments: dict[str, Any],
        *,
        reason: str,
        content: str | None = None,
    ) -> ToolResult:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        target = self._display_target(kind, arguments)
        if content is None:
            if reason in _STALE_REASONS:
                content = STALE_INSTRUCTION.format(
                    why=_reason_label(reason), current=current
                )
            else:
                content = REFUSAL_INSTRUCTION.format(what=target, current=current)
        self.record_refusal(
            kind=kind,
            topic_id=self._target_key(kind, arguments),
            topic_name=target,
            reason=reason,
        )
        return ToolResult(
            ok=False,
            content=content,
            error=f"topic_application_{reason}",
            category=_category_for_reason(reason),
            recoverable=False,
            facts={"topic_application": reason, "kind": kind},
            code=f"topic_application_{reason}",
        )

    def _stale_result(
        self, kind: str, arguments: dict[str, Any], why: str
    ) -> ToolResult:
        current = self._from_topic_name() or self._from_topic() or "当前话题"
        target = self._display_target(kind, arguments)
        self.record_refusal(
            kind=kind,
            topic_id=self._target_key(kind, arguments),
            topic_name=target,
            reason="stale",
        )
        return ToolResult(
            ok=False,
            content=STALE_INSTRUCTION.format(
                why=_reason_label(why), current=current
            ),
            error=f"topic_application_stale:{why}",
            category="unknown",
            recoverable=False,
            facts={"topic_application": "stale", "kind": kind, "why": why},
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


#: 这些原因都按「申请失效」口径回答（目标/位置已不可用，不是用户拒绝）。
_STALE_REASONS = frozenset(
    {"stale", "topic_target_missing", "topic_name_required", "duplicate"}
)

_REASON_LABELS = {
    "stale": "等待期间话题位置已改变",
    "active_anchor_changed": "等待期间已经导航到别的话题",
    "binding_changed": "本轮归属已改变",
    "binding_closed": "本轮已经收尾",
    "turn_topic_changed": "本轮话题已改变",
    "topic_target_missing": "目标话题已不存在",
    "topic_name_required": "话题名不能为空",
    "no_approval_channel": "当前没有审批通道",
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
    "get_topic_application_state",
]
