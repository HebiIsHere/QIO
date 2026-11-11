"""统一 Topic 导航入口（TopicNavigationService）。

R16 契约 §4：所有生产位置写入必须且只能经
services/position_state.py::PositionStateService.apply()。本模块每个公开写入口都只是
它的薄包装：保留既有函数名/返回形状，内部不再各自写 Anchor/pending/两类版本。
旧调用方没有命令身份时合成 anonymous internal 身份（UUID scope，§14.7 兼容路径）；
生产新路径（HTTP/工具/orchestrator）必须传真实身份 request:/operation:/internal:。

Anchor 是用户当前对话真正继续发生的位置，只有统一入口能改它；选中话题是纯前端状态；
引用话题只读；待确认切换是历史遗留 API（生产无调用方）。

两阶段接续（F14）：register_continuation（点击/工具只登记）→ apply_continuation
（真正有消息执行时落实，唯一创建片段的位置）。
"""

from __future__ import annotations

import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.graph.anchors import AnchorService
from agent.graph.edges import EdgeService
from agent.graph.nodes import NodeService
from agent.memory.fragment import FragmentManager, new_id
from agent.services.binding import (
    INTENT_CANCELLED,
    INTENT_REGISTERED,
    INTENT_REPLACED,
    ContinuationIntent,
    IntentNotActive,
    IntentNotFound,
    TurnBindingService,
)
from agent.services.navigation_version import (
    NavigationVersionConflict,
    current as current_nav_version,
)
from agent.services.position_state import (
    KIND_APPLY_CONTINUATION,
    KIND_AUTO_SEGMENT,
    KIND_CANCEL_CONTINUATION,
    KIND_CREATE_TOPIC,
    KIND_ENTER_TOPIC,
    KIND_INTERNAL_REFINE,
    KIND_OPEN_FRAGMENT,
    KIND_SELECT_HISTORY,
    CommandResult,
    NavCommandIdentity,
    PositionCommandConflict,
    PositionCommandRejected,
    PositionStateService,
    PositionTransition,
)
from agent.storage.db import transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 工具导航登记（阶段 1 的受控例外）─────────────────────────────────────
_TOOL_NAV_TURN: ContextVar[str | None] = ContextVar("qio_tool_nav_turn", default=None)
_TOOL_NAV_REBINDS: dict[str, str] = {}
_TOOL_NAV_REBINDS_LIMIT = 64


def set_tool_nav_turn(turn_id: str | None) -> None:
    _TOOL_NAV_TURN.set(turn_id)


def note_tool_navigation(topic_id: str) -> None:
    turn_id = _TOOL_NAV_TURN.get()
    if not turn_id or not topic_id:
        return
    _TOOL_NAV_REBINDS[turn_id] = topic_id
    if len(_TOOL_NAV_REBINDS) > _TOOL_NAV_REBINDS_LIMIT:
        for stale in list(_TOOL_NAV_REBINDS)[:-_TOOL_NAV_REBINDS_LIMIT]:
            _TOOL_NAV_REBINDS.pop(stale, None)


def take_tool_navigation(turn_id: str) -> str | None:
    return _TOOL_NAV_REBINDS.pop(turn_id, None)


def clear_tool_navigation(turn_id: str) -> None:
    _TOOL_NAV_REBINDS.pop(turn_id, None)


class TopicNotFound(ValueError):
    """目标话题不存在。"""


class FragmentNotInTopic(ValueError):
    """片段不存在，或者不属于这个话题（绝不能把别的历史注入进来）。"""


@dataclass(frozen=True)
class NavigationResult:
    topic_id: str
    fragment_id: str | None
    fragment_title: str | None
    historic: bool
    created_fragment_id: str | None = None
    source_fragment_id: str | None = None
    sealed_fragment_id: str | None = None
    nav_version: int | None = None
    # R16（§14.7）：统一入口命令结果事实；全部有默认值，旧调用方不破。
    changed: bool = False
    replay: bool = False
    command_id: str | None = None
    position: dict | None = None
    position_version: int | None = None


def relate_topics(conn: sqlite3.Connection, a: str, b: str) -> None:
    if not a or not b or a == b:
        return
    src, dst = sorted([a, b])
    EdgeService(conn).add(src, dst, "related")


# ── 语义授权识别：已在本轮删除（P1-01）────────────────────────────────
# 唯一语义判断位置是本轮主循环模型（工具参数的 intent）。不要再在这里加回任何
# 关键词、正则、包含匹配、相似度阈值式的「授权识别」。


class TopicNavigationService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.nodes = NodeService(conn)
        self.anchors = AnchorService(conn)
        self.fragments = FragmentManager(conn)
        self.bindings = TurnBindingService(conn)
        self.positions = PositionStateService(conn)

    # -- 统一入口辅助 -----------------------------------------------------
    def _identity_for(
        self,
        kind: str,
        *,
        identity: NavCommandIdentity | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        param_version: int | None = None,
    ) -> NavCommandIdentity:
        if identity is not None:
            return identity
        if request_id:
            return NavCommandIdentity.request(str(request_id))
        if operation_id is not None and param_version is not None:
            return NavCommandIdentity.operation(str(operation_id), int(param_version))
        return NavCommandIdentity.anonymous(kind)

    @staticmethod
    def _initiator_for(identity: NavCommandIdentity, default: str = "user") -> str:
        if identity.kind == "operation":
            return "tool"
        if identity.kind == "request":
            return default
        return "internal"

    @staticmethod
    def _raise_if_rejected(
        result: CommandResult,
        *,
        expected_nav_version: int | None = None,
        source: str = "",
    ) -> None:
        if result.status == "conflict":
            raise PositionCommandConflict(result)
        if result.status in ("rejected", "superseded"):
            expected = result.result.get("expected_nav_version", expected_nav_version)
            actual = result.result.get("actual_nav_version")
            if result.code in ("navigation_command_stale", "position_state_stale") and (
                expected is not None and actual is not None
            ):
                raise NavigationVersionConflict(int(expected), int(actual), source=source)
            raise PositionCommandRejected(result)

    def _result_from_command(
        self,
        result: CommandResult,
        *,
        topic_id: str,
        fragment_id: str | None,
        created_fragment_id: str | None = None,
        source_fragment_id: str | None = None,
        sealed_fragment_id: str | None = None,
    ) -> NavigationResult:
        position = dict(result.position or {})
        historic = (
            bool(position.get("historic"))
            if position.get("topic_id") == topic_id
            else self.anchors.is_historic_position(topic_id)
        )
        return NavigationResult(
            topic_id=topic_id,
            fragment_id=fragment_id,
            fragment_title=self.fragment_title(fragment_id),
            historic=historic,
            created_fragment_id=created_fragment_id,
            source_fragment_id=source_fragment_id,
            sealed_fragment_id=sealed_fragment_id,
            nav_version=int(result.nav_version),
            changed=bool(result.changed),
            replay=bool(result.replay),
            command_id=result.command_id,
            position=position,
            position_version=int(result.position_version),
        )

    def _current_position(self) -> dict:
        position = self.positions.read()
        return {
            "topic_id": position.get("topic_id"),
            "fragment_id": position.get("fragment_id"),
            "fragment_title": position.get("fragment_title"),
            "historic": bool(position.get("historic")),
            "nav_version": int(position.get("nav_version") or 0),
            "position_version": int(position.get("position_version") or 0),
        }

    def _registered_payload(
        self, payload: dict, topic_id: str, fragment_id: str
    ) -> dict:
        """工具登记路径的顶层字段描述**请求登记的历史来源**（不是当前 Anchor）。

        register_continuation 既有语义：topic_id/fragment_id = 用户选中的来源，
        historic=True；当前位置事实在 position wire 里如实给出。
        """
        payload["topic_id"] = topic_id
        payload["fragment_id"] = fragment_id
        payload["fragment_title"] = self.fragment_title(fragment_id)
        payload["source_fragment_id"] = fragment_id
        payload["historic"] = True
        return payload

    # -- 进入话题 / 内部细化 ---------------------------------------------

    def enter_topic(
        self,
        topic_id: str,
        *,
        fragment_id: str | None = None,
        relate: bool = False,
        restore_position: bool = True,
        expected_nav_version: int | None = None,
        turn_id: str | None = None,
        advance_nav_version: bool = True,
        identity: NavCommandIdentity | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        param_version: int | None = None,
        expected_position_version: int | None = None,
        internal_scope: str | None = None,
        phase: str | None = None,
    ) -> NavigationResult:
        """进入话题：fragment_id=None + restore_position=True 恢复保存位置；
        restore_position=False 表示用户明确选择从最新位置开始（存储 NULL）。
        advance_nav_version=False 映射 internal_refine（不推进 nav_version，
        必须带 expected_position_version 做内部前提校验）。"""
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        if fragment_id is not None:
            fragment = self.fragments.get(fragment_id)
            if fragment is None or fragment.topic_id != topic_id:
                raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")
        elif restore_position:
            fragment_id = self.anchors.position_fragment(topic_id)
        previous = self.anchors.get_active()

        if advance_nav_version:
            kind = KIND_ENTER_TOPIC
            source = "navigation.enter_topic"
            resolved_identity = self._identity_for(
                kind,
                identity=identity,
                request_id=request_id,
                operation_id=operation_id,
                param_version=param_version,
            )
            initiator = self._initiator_for(resolved_identity, default="user")
            base_nav_version = expected_nav_version
        else:
            kind = KIND_INTERNAL_REFINE
            source = "turn.advance_anchor"
            scope = internal_scope or (
                f"{turn_id or 'legacy'}:{phase or 'post_turn'}:{fragment_id or topic_id}"
            )
            if identity is not None:
                resolved_identity = identity
            elif expected_position_version is None:
                resolved_identity = NavCommandIdentity.anonymous(kind)
            else:
                resolved_identity = NavCommandIdentity.internal(kind, scope)
            initiator = "internal"
            base_nav_version = None

        result = self.positions.apply(
            PositionTransition(
                kind=kind,
                source=source,
                identity=resolved_identity,
                initiator=initiator,
                target_topic_id=topic_id,
                target_fragment_id=fragment_id,
                base_nav_version=base_nav_version,
                expected_position_version=expected_position_version,
                turn_id=turn_id,
            )
        )
        self._raise_if_rejected(
            result, expected_nav_version=expected_nav_version, source=source
        )
        if (
            relate
            and result.changed
            and previous is not None
            and previous.topic_id
            and previous.topic_id != topic_id
        ):
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result_from_command(
            result, topic_id=topic_id, fragment_id=fragment_id
        )

    # -- 接续：工具登记路径 -----------------------------------------------

    def register_continuation(
        self,
        topic_id: str,
        fragment_id: str,
        *,
        request_id: str | None = None,
        advance_nav_version: bool = True,
        turn_id: str | None = None,
        identity: NavCommandIdentity | None = None,
        operation_id: str | None = None,
        param_version: int | None = None,
    ) -> dict:
        """登记「下一次发送要从这段历史继续」：只登记 pending，不创建片段、不搬 Anchor。

        F08：changed=False 的重复登记零位置写入、零版本推进；重放已替换/取消的旧请求
        不重新激活（intent_id=None、inactive=True）。工具登记路径不广播位置事件。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        source = self.fragments.get(fragment_id)
        if source is None or source.topic_id != topic_id:
            raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")
        if source.closed_at is None:
            position = self._current_position()
            return {
                "topic_id": topic_id,
                "fragment_id": fragment_id,
                "fragment_title": self.fragment_title(fragment_id),
                "historic": False,
                "opens_current": True,
                "intent_id": None,
                "intent_version": None,
                "source_fragment_id": fragment_id,
                "nav_version": current_nav_version(self.conn),
                "position_version": position["position_version"],
                "changed": False,
                "replay": False,
                "inactive": False,
                "conflict": False,
                "code": None,
                "state": None,
                "command_id": None,
                "position": self.positions.read(),
            }

        resolved_identity = self._identity_for(
            KIND_SELECT_HISTORY,
            identity=identity,
            request_id=request_id,
            operation_id=operation_id,
            param_version=param_version,
        )
        result = self.positions.apply(
            PositionTransition(
                kind=KIND_SELECT_HISTORY,
                source="navigation.register_continuation",
                identity=resolved_identity,
                initiator=self._initiator_for(resolved_identity, default="internal"),
                pending="register",
                pending_topic_id=topic_id,
                pending_fragment_id=fragment_id,
                pending_request_id=request_id,
                turn_id=turn_id,
            )
        )
        if result.status == "conflict":
            return self._registered_payload(
                self._continuation_payload(
                    position=self._current_position(),
                    intent_id=None,
                    intent_version=None,
                    source_fragment_id=fragment_id,
                    state=None,
                    changed=False,
                    replay=False,
                    inactive=False,
                    opens_current=False,
                    conflict=True,
                    code="navigation_command_conflict",
                    command_id=result.command_id,
                    position_version=result.position_version,
                ),
                topic_id,
                fragment_id,
            )
        if result.status in ("rejected", "superseded"):
            return self._registered_payload(
                self._continuation_payload(
                    position=self._current_position(),
                    intent_id=None,
                    intent_version=None,
                    source_fragment_id=fragment_id,
                    state=None,
                    changed=False,
                    replay=False,
                    inactive=True,
                    opens_current=False,
                    conflict=False,
                    code=result.code,
                    command_id=result.command_id,
                    position_version=result.position_version,
                ),
                topic_id,
                fragment_id,
            )

        intent_id = result.result.get("intent_id")
        intent_version = result.result.get("intent_version")
        state = result.result.get("state")
        # 重放命令时命令行存的是存储时结果；旧接口语义要看**当前**意图状态
        #（已替换/取消的旧 request 必须返回 inactive，不能返回可落实的 intent_id）。
        if request_id:
            current = self.bindings.intent_by_request(str(request_id))
            if current is not None:
                state = current.state
                intent_id = current.intent_id
                intent_version = current.version
            else:
                state = state or INTENT_REGISTERED
        inactive = state in (INTENT_REPLACED, INTENT_CANCELLED)
        return self._registered_payload(
            self._continuation_payload(
                position=self._position_after_result(result, topic_id, fragment_id),
                intent_id=None if inactive else intent_id,
                intent_version=None if inactive else intent_version,
                source_fragment_id=fragment_id,
                state=state,
                changed=bool(result.changed),
                replay=bool(result.replay),
                inactive=inactive,
                opens_current=False,
                conflict=False,
                code="continuation_request_inactive" if inactive else None,
                wire_position=result.position,
                nav_version=result.nav_version,
                command_id=result.command_id,
                position_version=result.position_version,
            ),
            topic_id,
            fragment_id,
        )

    # -- 接续：点击路径（登记 + 落位）-------------------------------------

    def register_and_position_continuation(
        self,
        topic_id: str,
        fragment_id: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
        identity: NavCommandIdentity | None = None,
        expected_nav_version: int | None = None,
        operation_id: str | None = None,
        param_version: int | None = None,
    ) -> dict:
        """点击「从这里继续」：登记 + 有效性判断 + 需要时的 Anchor 落位（同一事务）。

        历史片段 = select_history（Anchor 落历史片段并广播）；当前开放片段 =
        open_fragment（取消不再适用的旧 registered）。幂等重放/失效请求零写入。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        source = self.fragments.get(fragment_id)
        if source is None or source.topic_id != topic_id:
            raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")

        existing = self.bindings.intent_by_request(request_id) if request_id else None
        if existing is not None and (
            existing.topic_id != topic_id
            or existing.source_fragment_id != fragment_id
        ):
            return self._continuation_payload(
                position=self._current_position(),
                intent_id=None,
                intent_version=None,
                source_fragment_id=fragment_id,
                state=existing.state,
                changed=False,
                replay=False,
                inactive=False,
                opens_current=False,
                conflict=True,
                code="continuation_request_conflict",
            )

        resolved_identity = self._identity_for(
            KIND_SELECT_HISTORY,
            identity=identity,
            request_id=request_id,
            operation_id=operation_id,
            param_version=param_version,
        )

        if source.closed_at is None:
            result = self.positions.apply(
                PositionTransition(
                    kind=KIND_OPEN_FRAGMENT,
                    source="navigation.continue_from_history",
                    identity=resolved_identity,
                    initiator=self._initiator_for(resolved_identity, default="user"),
                    target_topic_id=topic_id,
                    target_fragment_id=fragment_id,
                    base_nav_version=expected_nav_version,
                    pending="cancel",
                    turn_id=turn_id,
                )
            )
            self._raise_if_rejected(
                result,
                expected_nav_version=expected_nav_version,
                source="navigation.continue_from_history",
            )
            return self._continuation_payload(
                position=self._position_after_result(result, topic_id, fragment_id),
                intent_id=None,
                intent_version=None,
                source_fragment_id=fragment_id,
                state=None,
                changed=bool(result.changed),
                replay=bool(result.replay),
                inactive=False,
                opens_current=True,
                conflict=False,
                code=None,
                wire_position=result.position,
                nav_version=result.nav_version,
                command_id=result.command_id,
                position_version=result.position_version,
            )

        if existing is not None:
            if existing.state == INTENT_REGISTERED:
                return self._replay_payload(existing, requested_fragment=fragment_id)
            if existing.state in (INTENT_REPLACED, INTENT_CANCELLED):
                return self._inactive_payload(existing)
            current = self.bindings.peek_intent()
            if current is None:
                return self._replay_payload(existing, requested_fragment=fragment_id)
            if current.topic_id == topic_id and current.source_fragment_id == fragment_id:
                return self._replay_payload(current, requested_fragment=fragment_id)
            return self._inactive_payload(existing)

        # 4) 没有 request_id 命中：重复点同一来源也算幂等复用（零写入）。
        # 只有**没有显式请求身份**的旧路径才适用这条快捷判定；带 request_id 的新用户
        # 点击是真实命令：即使 pending 来源相同，Anchor 移动仍须按 §14.5 推进 nav。
        if request_id is None:
            current = self.bindings.peek_intent()
            if (
                current is not None
                and current.topic_id == topic_id
                and current.source_fragment_id == fragment_id
            ):
                return self._replay_payload(current, requested_fragment=fragment_id)

        result = self.positions.apply(
            PositionTransition(
                kind=KIND_SELECT_HISTORY,
                source="navigation.continue_from_history",
                identity=resolved_identity,
                initiator=self._initiator_for(resolved_identity, default="user"),
                target_topic_id=topic_id,
                target_fragment_id=fragment_id,
                base_nav_version=expected_nav_version,
                pending="register",
                pending_topic_id=topic_id,
                pending_fragment_id=fragment_id,
                pending_request_id=request_id,
                turn_id=turn_id,
            )
        )
        self._raise_if_rejected(
            result,
            expected_nav_version=expected_nav_version,
            source="navigation.continue_from_history",
        )
        return self._continuation_payload(
            position=self._position_after_result(result, topic_id, fragment_id),
            intent_id=result.result.get("intent_id"),
            intent_version=result.result.get("intent_version"),
            source_fragment_id=fragment_id,
            state=result.result.get("state"),
            changed=bool(result.changed),
            replay=bool(result.replay),
            inactive=False,
            opens_current=False,
            conflict=False,
            code=None,
            wire_position=result.position,
            nav_version=result.nav_version,
            command_id=result.command_id,
            position_version=result.position_version,
        )

    # -- 落实接续（唯一创建片段的位置）------------------------------------

    def apply_continuation(
        self,
        topic_id: str,
        intent_id: str,
        *,
        relate: bool = True,
        expected_nav_version: int | None = None,
        turn_id: str | None = None,
        identity: NavCommandIdentity | None = None,
        expected_position_version: int | None = None,
    ) -> NavigationResult:
        """落实接续意图：真正创建/复用片段的唯一位置（原子交接）。

        R16：apply_continuation 转换（nav_version 不推进；position_version 在
        片段/Anchor/意图状态真实变化时 +1）。身份用 turn+intent 确定性 scope；
        片段创建与统一入口写入同一事务，CAS 失败整体回滚。
        """
        intent = self.bindings.intent_by_id(intent_id)
        if intent is None:
            raise IntentNotFound(f"接续意图不存在：{intent_id}")
        if intent.topic_id != topic_id:
            raise FragmentNotInTopic(
                f"接续意图 {intent_id} 属于话题 {intent.topic_id}，与 {topic_id} 不一致"
            )
        if expected_nav_version is not None:
            actual_nav = int(current_nav_version(self.conn))
            if actual_nav != int(expected_nav_version):
                raise NavigationVersionConflict(
                    int(expected_nav_version),
                    actual_nav,
                    source="navigation.apply_continuation",
                )
        if intent.state in (INTENT_REPLACED, INTENT_CANCELLED):
            raise IntentNotActive(
                f"接续意图 {intent_id} 已失效（{intent.state}），不能重新落实"
            )
        if intent.resolved_fragment_id:
            existing = self.fragments.get(intent.resolved_fragment_id)
            if existing is not None:
                return self._result(
                    topic_id,
                    existing.id,
                    created_fragment_id=existing.id,
                    source_fragment_id=intent.source_fragment_id,
                    nav_version=current_nav_version(self.conn),
                    changed=False,
                    replay=True,
                    position=self.positions.read(),
                )

        source = (
            self.fragments.get(intent.source_fragment_id)
            if intent.source_fragment_id
            else None
        )
        previous = self.anchors.get_active()
        resolved_identity = identity or NavCommandIdentity.internal(
            KIND_APPLY_CONTINUATION, f"{turn_id or 'legacy'}:{intent_id}"
        )
        pos_now = self.positions.current_position_version()
        expected_pos = (
            int(expected_position_version)
            if expected_position_version is not None
            else int(pos_now)
        )

        if source is not None and source.closed_at is None:
            result = self.positions.apply(
                PositionTransition(
                    kind=KIND_APPLY_CONTINUATION,
                    source="navigation.apply_continuation",
                    identity=resolved_identity,
                    initiator="internal",
                    target_topic_id=topic_id,
                    target_fragment_id=source.id,
                    base_nav_version=expected_nav_version,
                    expected_position_version=expected_pos,
                    pending="resolve",
                    pending_intent_id=intent_id,
                    turn_id=turn_id,
                )
            )
            self._raise_if_rejected(
                result,
                expected_nav_version=expected_nav_version,
                source="navigation.apply_continuation",
            )
            if relate and previous is not None and previous.topic_id:
                relate_topics(self.conn, previous.topic_id, topic_id)
            return self._result_from_command(
                result,
                topic_id=topic_id,
                fragment_id=source.id,
                source_fragment_id=intent.source_fragment_id,
            )

        open_fragment = self.fragments.open_fragment(topic_id)
        sealed_id: str | None = None
        transition = PositionTransition(
            kind=KIND_APPLY_CONTINUATION,
            source="navigation.apply_continuation",
            identity=resolved_identity,
            initiator="internal",
            base_nav_version=expected_nav_version,
            expected_position_version=expected_pos,
            pending="resolve",
            pending_intent_id=intent_id,
            turn_id=turn_id,
        )
        try:
            with transaction(self.conn):
                if open_fragment is not None:
                    sealed_id = open_fragment.id
                    self.conn.execute(
                        "UPDATE fragments SET closed_at = ?, boundary_reason = ? WHERE id = ?",
                        (_now(), "history_continuation", sealed_id),
                    )
                new_fragment_id = self.fragments.create_child(
                    topic_id,
                    source_fragment_id=intent.source_fragment_id,
                    relation_type="history_reopen",
                    boundary_reason="history_continuation",
                )
                transition.target_topic_id = topic_id
                transition.target_fragment_id = new_fragment_id
                transition.causal = (
                    {"sealed_fragment_id": sealed_id} if sealed_id else {}
                )
                result = self.positions.apply(transition)
                if result.status not in ("applied", "replay"):
                    raise PositionCommandRejected(result)
        except PositionCommandRejected as rejected:
            self.positions.record_rejection(transition, rejected.result)
            self._raise_if_rejected(
                rejected.result,
                expected_nav_version=expected_nav_version,
                source="navigation.apply_continuation",
            )
            raise  # pragma: no cover - _raise_if_rejected 必抛

        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result_from_command(
            result,
            topic_id=topic_id,
            fragment_id=new_fragment_id,
            created_fragment_id=new_fragment_id,
            source_fragment_id=intent.source_fragment_id,
            sealed_fragment_id=sealed_id,
        )

    # -- 新建话题 ---------------------------------------------------------

    def create_topic(
        self,
        name: str,
        *,
        relate: bool = True,
        expected_nav_version: int | None = None,
        turn_id: str | None = None,
        identity: NavCommandIdentity | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
        param_version: int | None = None,
    ) -> NavigationResult:
        """建话题 + 写 Anchor + 版本推进同一事务（统一入口内建 node）。"""
        previous = self.anchors.get_active()
        resolved_identity = self._identity_for(
            KIND_CREATE_TOPIC,
            identity=identity,
            request_id=request_id,
            operation_id=operation_id,
            param_version=param_version,
        )
        result = self.positions.apply(
            PositionTransition(
                kind=KIND_CREATE_TOPIC,
                source="navigation.create_topic",
                identity=resolved_identity,
                initiator=self._initiator_for(resolved_identity, default="user"),
                new_topic_name=str(name),
                base_nav_version=expected_nav_version,
                turn_id=turn_id,
            )
        )
        self._raise_if_rejected(
            result,
            expected_nav_version=expected_nav_version,
            source="navigation.create_topic",
        )
        topic_id = str(
            result.result.get("new_topic_id")
            or result.result.get("target_topic_id")
            or ""
        )
        if relate and result.changed and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result_from_command(result, topic_id=topic_id, fragment_id=None)

    # -- 取消待落实接续 ---------------------------------------------------

    def cancel_continuation(
        self,
        *,
        request_id: str | None = None,
        identity: NavCommandIdentity | None = None,
        turn_id: str | None = None,
        pending_intent_id: str | None = None,
        pending_intent_version: int | None = None,
    ) -> dict:
        """取消未落实接续（§14.9：nav_version 不推进；pending 真实变化时 position +1）。

        R17 §2.2：生产入口（HTTP）必须携带取消目标 pending_intent_id（+可选版本），
        统一入口在事务内先校验目标再取消；pending_intent_id=None 的旧调用方保持
        「取消当前 registered」行为。拒绝（目标不存在/失效/版本不一致）时本包装
        只返回结果事实，由调用方按既有形状映射（HTTP 层给 409）。
        """
        pending = self.bindings.peek_intent()
        intent_id = (
            pending_intent_id or (pending.intent_id if pending is not None else None)
        )
        if intent_id is None:
            position = self.positions.read()
            return {
                "ok": True,
                "cancelled": False,
                "cancelled_intent_id": None,
                "intent_version": None,
                "changed": False,
                "replay": False,
                "nav_version": int(position.get("nav_version") or 0),
                "position_version": int(position.get("position_version") or 0),
                "position": position,
                "command_id": None,
            }
        resolved_identity = self._identity_for(
            KIND_CANCEL_CONTINUATION, identity=identity, request_id=request_id
        )
        result = self.positions.apply(
            PositionTransition(
                kind=KIND_CANCEL_CONTINUATION,
                source="navigation.cancel_continuation",
                identity=resolved_identity,
                initiator=self._initiator_for(resolved_identity, default="user"),
                pending="cancel",
                pending_intent_id=intent_id,
                pending_intent_version=pending_intent_version,
                turn_id=turn_id,
            )
        )
        if result.status == "conflict":
            raise PositionCommandConflict(result)
        return {
            "ok": result.status in ("applied", "replay"),
            "cancelled": bool(result.result.get("cancelled")),
            "cancelled_intent_id": result.result.get("cancelled_intent_id"),
            "intent_version": result.result.get("intent_version"),
            "changed": bool(result.changed),
            "replay": bool(result.replay),
            "inactive": bool(result.inactive),
            "code": result.code,
            "cancelled_intent_id": result.result.get("cancelled_intent_id"),
            "nav_version": int(result.nav_version),
            "position_version": int(result.position_version),
            "position": result.position,
            "command_id": result.command_id,
        }

    # -- orchestrator 内部推进 -------------------------------------------

    def apply_auto_segment(
        self,
        topic_id: str,
        sealed_fragment_id: str,
        *,
        turn_id: str | None = None,
        expected_position_version: int | None = None,
        source: str = "capacity.segment",
    ) -> CommandResult:
        """容量分段封存后的 auto_segment 转换：position_version 一律 +1，nav 不推进，
        pending 不动（不得清掉旧申请）。调用方必须已经真实封存该片段。"""
        scope = f"{turn_id or 'legacy'}:{sealed_fragment_id}"
        expected = (
            int(expected_position_version)
            if expected_position_version is not None
            else self.positions.current_position_version()
        )
        return self.positions.apply(
            PositionTransition(
                kind=KIND_AUTO_SEGMENT,
                source=source,
                identity=NavCommandIdentity.internal(KIND_AUTO_SEGMENT, scope),
                initiator="internal",
                expected_position_version=expected,
                causal={"sealed_fragment_id": sealed_fragment_id},
                turn_id=turn_id,
            )
        )

    def apply_internal_refine(
        self,
        topic_id: str,
        fragment_id: str | None,
        *,
        turn_id: str | None = None,
        phase: str = "post_turn",
        expected_position_version: int | None = None,
        source: str = "turn.advance_anchor",
        internal_scope: str | None = None,
        causal: dict | None = None,
    ) -> CommandResult:
        """内部片段细化（轮首补锚点 / 轮末推进 / 边界细化）：nav 不推进，
        身份带 phase，必须携带 expected_position_version。"""
        scope = internal_scope or (
            f"{turn_id or 'legacy'}:{phase}:{fragment_id or topic_id}"
        )
        expected = (
            int(expected_position_version)
            if expected_position_version is not None
            else self.positions.current_position_version()
        )
        return self.positions.apply(
            PositionTransition(
                kind=KIND_INTERNAL_REFINE,
                source=source,
                identity=NavCommandIdentity.internal(KIND_INTERNAL_REFINE, scope),
                initiator="internal",
                target_topic_id=topic_id,
                target_fragment_id=fragment_id,
                expected_position_version=expected,
                turn_id=turn_id,
                causal=dict(causal or {}),
            )
        )

    # -- 旧「待确认切换」原语：R16 已删除（契约 §4/§9）--------------------
    # request_switch / pending_switch / confirm_switch / reject_switch 曾把「程序推测的
    # 切换建议」写进 cursor 的 pending 行。唯一生产入口（/api/topic-switch/*）已 410 退役，
    # 待落实的用户选择统一用 continuation_intents 表达并由统一位置入口写入，
    # 因此这里不再保留第二套业务写入方法（底层 AnchorService 的 pending 辅助保留，
    # 但业务层不得调用；生产测试 test_topic_navigation 有架构守卫）。

    # -- 只读辅助 ---------------------------------------------------------

    def fragment_title(self, fragment_id: str | None) -> str | None:
        if not fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            return str(row["title"])
        fragment = self.fragments.get(fragment_id)
        if fragment is not None and fragment.summary:
            return fragment.summary.strip().splitlines()[0][:40]
        return None

    def _position_after_result(
        self, result: CommandResult, topic_id: str, fragment_id: str | None
    ) -> dict:
        position = dict(result.position or {})
        if position.get("topic_id") != topic_id:
            # 工具登记路径不搬 Anchor：位置事实如实取当前 Anchor，目标由目标字段表达。
            return {
                "topic_id": position.get("topic_id"),
                "fragment_id": position.get("fragment_id"),
                "fragment_title": position.get("fragment_title"),
                "historic": bool(position.get("historic")),
                "nav_version": int(result.nav_version),
                "position_version": int(result.position_version),
            }
        return {
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "fragment_title": self.fragment_title(fragment_id),
            "historic": bool(position.get("historic")),
            "nav_version": int(result.nav_version),
            "position_version": int(result.position_version),
        }

    def _continuation_payload(
        self,
        *,
        position: dict,
        intent_id: str | None,
        intent_version: int | None,
        source_fragment_id: str | None,
        state: str | None,
        changed: bool,
        replay: bool,
        inactive: bool,
        opens_current: bool,
        conflict: bool,
        code: str | None,
        nav_version: int | None = None,
        command_id: str | None = None,
        position_version: int | None = None,
        wire_position: dict | None = None,
    ) -> dict:
        wire = wire_position if wire_position is not None else self.positions.read()
        return {
            "topic_id": position["topic_id"],
            "fragment_id": position["fragment_id"],
            "fragment_title": position["fragment_title"],
            "historic": position["historic"],
            "nav_version": (
                int(nav_version)
                if nav_version is not None
                else int(position.get("nav_version") or 0)
            ),
            "position_version": (
                int(position_version)
                if position_version is not None
                else int(position.get("position_version") or 0)
            ),
            "intent_id": intent_id,
            "intent_version": intent_version,
            "source_fragment_id": source_fragment_id,
            "state": state,
            "changed": bool(changed),
            "replay": bool(replay),
            "inactive": bool(inactive),
            "opens_current": bool(opens_current),
            "conflict": bool(conflict),
            "code": code,
            "command_id": command_id,
            "position": wire,
        }

    def _replay_payload(
        self, intent: ContinuationIntent, *, requested_fragment: str
    ) -> dict:
        return self._continuation_payload(
            position=self._current_position(),
            intent_id=intent.intent_id,
            intent_version=intent.version,
            source_fragment_id=intent.source_fragment_id or requested_fragment,
            state=intent.state,
            changed=False,
            replay=True,
            inactive=False,
            opens_current=False,
            conflict=False,
            code=None,
        )

    def _inactive_payload(self, intent: ContinuationIntent) -> dict:
        return self._continuation_payload(
            position=self._current_position(),
            intent_id=None,
            intent_version=None,
            source_fragment_id=intent.source_fragment_id,
            state=intent.state,
            changed=False,
            replay=False,
            inactive=True,
            opens_current=False,
            conflict=False,
            code="continuation_request_inactive",
        )

    def _result(
        self,
        topic_id: str,
        fragment_id: str | None,
        *,
        created_fragment_id: str | None = None,
        source_fragment_id: str | None = None,
        sealed_fragment_id: str | None = None,
        nav_version: int | None = None,
        changed: bool = False,
        replay: bool = False,
        command_id: str | None = None,
        position: dict | None = None,
        position_version: int | None = None,
    ) -> NavigationResult:
        return NavigationResult(
            topic_id=topic_id,
            fragment_id=fragment_id,
            fragment_title=self.fragment_title(fragment_id),
            historic=self.anchors.is_historic_position(topic_id),
            created_fragment_id=created_fragment_id,
            source_fragment_id=source_fragment_id,
            sealed_fragment_id=sealed_fragment_id,
            nav_version=nav_version,
            changed=changed,
            replay=replay,
            command_id=command_id,
            position=position,
            position_version=position_version,
        )
