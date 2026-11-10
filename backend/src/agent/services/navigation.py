"""统一 Topic 导航入口（TopicNavigationService）。

第二阶段的核心逻辑修复之一：**Anchor 只能有一个写入者**。

必须分开的概念：

* **当前话题 / Anchor**：用户当前对话真正继续发生的位置。只有本模块能改它；
* **选中话题**：用户在星球上正在浏览谁（纯前端状态，不改 Anchor）；
* **引用话题**：为了回答当前问题临时读取另一个话题（检索 / Focus，只读）；
* **待确认切换**：Predictor 觉得「可能属于另一个话题」时给出的建议，等用户点头才生效。

因此禁止这些隐式联动（第二阶段明确要求删除）：

    选中 Topic → currentTopic = selectedTopic
    Memory Retrieval 命中 Topic → currentTopic = result.topic
    Predictor 判断属于别的话题 → 直接改 Anchor

所有真正的 Topic Switch 都从这里走：进入话题 / 创建话题 / 确认切换 / 从历史继续。

两阶段接续契约（F14）：`register_continuation`（点击时只登记，返回 changed 事实，
F08 语义下重复登记不推进导航版本）→ `apply_continuation`（真正有消息要执行时才落实，
唯一创建片段的位置）。旧的 `continue_from_history` 组合入口已删除（仅测试调用、
无生产依赖）。
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
    cas_or_advance,
    current as current_nav_version,
)
from agent.storage.db import transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 工具导航登记（阶段 1 的受控例外）─────────────────────────────────────
# 阶段 1 让「本轮归属」在轮前固定，代价是工具自己换话题也被当成「后续导航」
# 排除掉了：建话题那一轮留在上一个话题里。这里把「谁换的话题」记下来 ——
# 工具换话题（create_topic / switch_topic）整轮跟着走，用户换话题（另一个
# 请求进来）一步不动。
#
# turn_id 走 ContextVar：工具在 turn 派生的 task 里执行，能读到本轮 id；
# 另一个请求的任务读不到（默认 None），所以用户导航永远登记不上。
# 回传走模块级 dict：子 task 里的写入不会传回父上下文，只能放在共享表里。
_TOOL_NAV_TURN: ContextVar[str | None] = ContextVar("qio_tool_nav_turn", default=None)
_TOOL_NAV_REBINDS: dict[str, str] = {}
# 兜底上限：turn 在 persist 之前失败时由收尾清理，这里再挡一层内存无界增长。
_TOOL_NAV_REBINDS_LIMIT = 64


def set_tool_nav_turn(turn_id: str | None) -> None:
    """标记「当前正在执行的是哪个 turn」，工具据此登记导航归属。"""
    _TOOL_NAV_TURN.set(turn_id)


def note_tool_navigation(topic_id: str) -> None:
    """工具换话题成功：登记本轮归属应跟随到该话题（同一轮后登记的覆盖先前）。"""
    turn_id = _TOOL_NAV_TURN.get()
    if not turn_id or not topic_id:
        return
    _TOOL_NAV_REBINDS[turn_id] = topic_id
    if len(_TOOL_NAV_REBINDS) > _TOOL_NAV_REBINDS_LIMIT:
        for stale in list(_TOOL_NAV_REBINDS)[:-_TOOL_NAV_REBINDS_LIMIT]:
            _TOOL_NAV_REBINDS.pop(stale, None)


def take_tool_navigation(turn_id: str) -> str | None:
    """取出并清掉本轮的导航登记。"""
    return _TOOL_NAV_REBINDS.pop(turn_id, None)


def clear_tool_navigation(turn_id: str) -> None:
    """本轮结束（失败 / 取消也算）：不留悬挂登记。"""
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
    #: 本次写入后的真实导航版本（P1-04）。置信来源：navigation_version。
    nav_version: int | None = None


def relate_topics(conn: sqlite3.Connection, a: str, b: str) -> None:
    """两个话题之间的 related 边（无向，方向归一化后权重才能累加）。"""
    if not a or not b or a == b:
        return
    src, dst = sorted([a, b])
    EdgeService(conn).add(src, dst, "related")


# ── 语义授权识别：已在本轮删除（P1-01）────────────────────────────────
# 这里曾经有 detect_explicit_navigation / detect_explicit_topic_creation：
# 用关键词 + 包含匹配在**进入主循环之前**判断「用户是否明确要求切换/新建」，
# 据此决定免确认直切或直接执行。那是程序在替主模型做自然语言语义判断，
# 会产生「否定切换被当成明确切换」「解释新建话题被当成要新建」等错误。
#
# 现在唯一的语义判断位置是本轮主循环模型：它照常调用 switch_topic /
# create_topic，并在工具参数的 intent 里声明 user_request / suggestion；
# 程序只消费这个受控通道的值（见 services/topic_operation.py）。
# 不要再在这里加回任何关键词、正则、包含匹配、相似度阈值式的「授权识别」。


class TopicNavigationService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.nodes = NodeService(conn)
        self.anchors = AnchorService(conn)
        self.fragments = FragmentManager(conn)
        self._pending_reason: str = ""

    # -- 真正改变对话位置的动作 ------------------------------------------

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
    ) -> NavigationResult:
        """进入一个话题：Anchor 移到该话题（有历史位置就恢复它）。

        `fragment_id=None` 表示「进入这个话题的最新位置」，与
        「从某段历史继续」是两个明确不同的动作（见 register_continuation /
        apply_continuation 两阶段契约）。

        `restore_position=False` 用于「用户明确选择从最新位置开始」：
        此时不恢复该话题保存过的旧位置，而是把位置直接落到最新（None）。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        if fragment_id is not None:
            fragment = self.fragments.get(fragment_id)
            if fragment is None or fragment.topic_id != topic_id:
                raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")
        elif restore_position:
            # 切回话题时恢复它保存的位置（没有 / 已失效则为 None）
            fragment_id = self.anchors.position_fragment(topic_id)
        previous = self.anchors.get_active()
        # Anchor 写入与导航版本推进在**同一个事务**里：`expected_nav_version` 非 None
        # 时它是一次授权 CAS（话题申请确认后执行）——版本已被用户的新导航改掉就抛
        # NavigationVersionConflict，事务回滚，Anchor 保持原样（不覆盖用户的新选择）。
        with transaction(self.conn):
            self.anchors.set_active(topic_id, fragment_id)
            if advance_nav_version:
                nav_version = cas_or_advance(
                    self.conn,
                    expected_nav_version,
                    source="navigation.enter_topic",
                    topic_id=topic_id,
                    fragment_id=fragment_id,
                    turn_id=turn_id,
                )
            else:
                # 例行位置细化（轮首补锚点 / 轮末把位置推进到本轮真实片段）：
                # 它不是用户或工具的**导航决策**，不推进版本（契约 §5 的推进来源清单）。
                # 否则一次正常的工具换题会被同一轮收尾再推一次，恢复入口会把自己的
                # 记录误判成「用户后来已经离开」。
                nav_version = current_nav_version(self.conn)
        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result(topic_id, fragment_id, nav_version=nav_version)

    def register_continuation(
        self,
        topic_id: str,
        fragment_id: str,
        *,
        request_id: str | None = None,
        advance_nav_version: bool = True,
        turn_id: str | None = None,
    ) -> dict:
        """登记「下一次发送要从这段历史继续」。**不创建任何片段。**

        F08：返回值带 changed 事实 —— 只有真实选择改变才推进导航版本：

        * 点击已封存的历史 A：首次登记 changed=True（改选则 +1 版本）；
        * 同 request_id 重放 / 重复点同一来源：changed=False，返回既有选择与
          相同 nav_version，**不推进**（否则会让等待中的申请 / 恢复误失效）；
        * 重放已被替换 / 取消的旧请求：不重新激活旧选择，返回 intent_id=None
          且 changed=False（当前选择保持不变）；
        * 点击当前开放片段：视为继续现有位置（opens_current=True），不登记意图，
          也不推进版本。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        source = self.fragments.get(fragment_id)
        if source is None or source.topic_id != topic_id:
            raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")
        if source.closed_at is None:
            # 来源就是当前开放片段：它就是对话的位置，普通继续，不需要接续意图
            # （没有位置变化，也就不推进导航版本）。
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
                "changed": False,
                "state": None,
            }

        # 选历史片段 / 改接续位置是一次真实的位置变化（P1-04）：登记意图与推进
        # 导航版本放在同一个事务里，失败时登记不会留下半截状态。
        with transaction(self.conn):
            registration = TurnBindingService(self.conn).register_intent_ex(
                topic_id, fragment_id, request_id=request_id
            )
            intent = registration.intent
            changed = bool(registration.changed)
            nav_version = (
                cas_or_advance(
                    self.conn,
                    None,
                    source="navigation.register_continuation",
                    topic_id=topic_id,
                    fragment_id=fragment_id,
                    turn_id=turn_id,
                )
                if changed and advance_nav_version
                else current_nav_version(self.conn)
            )
        # 重放已失效的旧请求（被替换 / 取消）：不重新激活、不覆盖当前选择。
        inactive = intent.state in (INTENT_REPLACED, INTENT_CANCELLED)
        return {
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "fragment_title": self.fragment_title(fragment_id),
            "historic": True,
            "opens_current": False,
            "intent_id": (None if inactive else intent.intent_id),
            "intent_version": (None if inactive else intent.version),
            "source_fragment_id": fragment_id,
            "nav_version": nav_version,
            "changed": changed,
            "state": intent.state,
            "inactive": inactive,
        }

    def register_and_position_continuation(
        self,
        topic_id: str,
        fragment_id: str,
        *,
        request_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict:
        """点击「从这里继续」：登记 + 有效性判断 + 需要的 Anchor 落位，**同一个事务**。

        R3（契约 §5）：路由不得只取登记结果里的 fragment_id 继续落位。三种情况必须分开：

        * **当前同一选择的幂等复用**（同 request_id 参数一致 / 重复点同一来源，
          意图仍有效）：changed=False, replay=True，**零位置写入、零版本推进**，
          回放既有选择与当前 Anchor 事实（只读）；
        * **已失效旧选择的重放**（意图被 replaced / cancelled）：inactive=True，
          零位置写入、零版本推进，路由据此返回 409；
        * **用户真正发起的新选择**（含点当前开放片段）：正常落位；真实改选推进一次
          导航版本，changed=True。

        同一 request_id 被用于不同 (topic_id, fragment_id) → 返回 conflict=True
        （路由映射 409 continuation_request_conflict），零写入、零版本推进。

        `transaction()` 可嵌套复用：登记（register_intent_ex）与 Anchor / 版本写入共享
        同一个 SQLite 事务，任何一步失败整体回滚 —— 不会留下「显示 A、实际接续 B」的
        半截状态。**点击路径不创建任何片段**：apply_continuation 仍是唯一落实位置。
        """
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        source = self.fragments.get(fragment_id)
        if source is None or source.topic_id != topic_id:
            raise FragmentNotInTopic(f"片段不属于该话题：{fragment_id}")

        bindings = TurnBindingService(self.conn)
        with transaction(self.conn):
            existing = bindings.intent_by_request(request_id) if request_id else None

            # 1) request_id 一致性（契约 §5.2.4）：同一个 id 只能对应一个选择。
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

            # 2) 点当前开放片段：普通继续（不登记意图），正常落位并推进一次版本。
            if source.closed_at is None:
                nav_version = self._write_continuation_position(
                    topic_id, fragment_id, turn_id=turn_id
                )
                return self._continuation_payload(
                    position=self._position_after(topic_id, fragment_id, nav_version),
                    intent_id=None,
                    intent_version=None,
                    source_fragment_id=fragment_id,
                    state=None,
                    changed=True,
                    replay=False,
                    inactive=False,
                    opens_current=True,
                    conflict=False,
                    code=None,
                )

            # 3) request_id 命中既有意图：按意图状态区分「幂等复用 / 失效拒绝」。
            if existing is not None:
                if existing.state == INTENT_REGISTERED:
                    # 同 request_id 重放：仍是当前选择 → 零写入回放既有事实。
                    return self._replay_payload(existing, requested_fragment=fragment_id)
                if existing.state in (INTENT_REPLACED, INTENT_CANCELLED):
                    # 旧请求不得重新激活，也不得覆盖当前选择。
                    return self._inactive_payload(existing)
                # consumed / resolved：已经落实过的旧选择。
                current = bindings.peek_intent()
                if current is None:
                    # 之后没有更新选择 → 回放既有结果（零写入），不重新激活。
                    return self._replay_payload(existing, requested_fragment=fragment_id)
                if (
                    current.topic_id == topic_id
                    and current.source_fragment_id == fragment_id
                ):
                    # 用户后来又点了同一段历史（新意图）：当前选择仍是它 → 回放当前选择。
                    return self._replay_payload(current, requested_fragment=fragment_id)
                # 之后已有别的选择：旧请求按失效拒绝，绝不覆盖新选择。
                return self._inactive_payload(existing)

            # 4) 没有 request_id 命中：重复点同一来源也算幂等复用（零写入）。
            current = bindings.peek_intent()
            if (
                current is not None
                and current.topic_id == topic_id
                and current.source_fragment_id == fragment_id
            ):
                return self._replay_payload(current, requested_fragment=fragment_id)

            # 5) 真实新选择：登记（替换旧选择）+ 落位 + 推进一次版本，同一事务。
            registration = bindings.register_intent_ex(
                topic_id, fragment_id, request_id=request_id
            )
            intent = registration.intent
            if not registration.changed:
                # 兜底：登记层认为没有改变（同选择被别处先登记）→ 按幂等回放，
                # 绝不重复推进版本。
                return self._replay_payload(intent, requested_fragment=fragment_id)
            nav_version = self._write_continuation_position(
                topic_id, fragment_id, turn_id=turn_id
            )
            return self._continuation_payload(
                position=self._position_after(topic_id, fragment_id, nav_version),
                intent_id=intent.intent_id,
                intent_version=intent.version,
                source_fragment_id=fragment_id,
                state=intent.state,
                changed=True,
                replay=False,
                inactive=False,
                opens_current=False,
                conflict=False,
                code=None,
            )

    # -- register_and_position_continuation 的内部辅助（只读/事务内）--------

    def _current_position(self) -> dict:
        """当前 Anchor 的只读事实（幂等回放 / 失效拒绝返回它，不写回）。"""
        anchor = self.anchors.get_active()
        topic_id = anchor.topic_id if anchor is not None else None
        fragment_id = anchor.fragment_id if anchor is not None else None
        return {
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "fragment_title": self.fragment_title(fragment_id),
            "historic": self.anchors.is_historic_position(topic_id) if topic_id else False,
            "nav_version": current_nav_version(self.conn),
        }

    def _position_after(self, topic_id: str, fragment_id: str, nav_version: int) -> dict:
        """刚刚写入的位置事实（真实新选择 / 点开放片段之后）。"""
        return {
            "topic_id": topic_id,
            "fragment_id": fragment_id,
            "fragment_title": self.fragment_title(fragment_id),
            "historic": self.anchors.is_historic_position(topic_id),
            "nav_version": nav_version,
        }

    def _write_continuation_position(
        self, topic_id: str, fragment_id: str, *, turn_id: str | None
    ) -> int:
        """Anchor 落位 + 导航版本推进一次（调用方负责把它放进事务）。"""
        self.anchors.set_active(topic_id, fragment_id)
        return cas_or_advance(
            self.conn,
            None,
            source="navigation.continue_from_history",
            topic_id=topic_id,
            fragment_id=fragment_id,
            turn_id=turn_id,
        )

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
    ) -> dict:
        """register_and_position_continuation 的冻结返回形状（契约 §5.2.2）。"""
        return {
            "topic_id": position["topic_id"],
            "fragment_id": position["fragment_id"],
            "fragment_title": position["fragment_title"],
            "historic": position["historic"],
            "nav_version": position["nav_version"],
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
        }

    def _replay_payload(self, intent: ContinuationIntent, *, requested_fragment: str) -> dict:
        """幂等复用：零写入，回放既有选择 + **当前 Anchor 事实**（只读）。"""
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
        """旧请求重放：意图已失效；零写入，返回当前 Anchor 事实供路由如实说明。"""
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
            code=None,
        )

    def apply_continuation(
        self,
        topic_id: str,
        intent_id: str,
        *,
        relate: bool = True,
        expected_nav_version: int | None = None,
        turn_id: str | None = None,
    ) -> NavigationResult:
        """落实接续意图：这是真正创建/复用片段的**唯一**位置（原子交接）。

        三种情况：
        1. 来源此刻是开放片段 → 普通继续，不新建；
        2. 该话题没有开放片段 → 新建 D，来源记 A；
        3. 该话题已有开放片段 C → 在同一个事务里封存 C（记录原因）再建 D，
           而不是直接 INSERT —— 这正是过去撞上
           `idx_fragments_one_open_per_topic` 的那条路径。

        重复调用（传输重试 / 工具重试）返回同一个结果，不重复创建。
        """
        bindings = TurnBindingService(self.conn)
        intent = bindings.intent_by_id(intent_id)
        if intent is None:
            raise IntentNotFound(f"接续意图不存在：{intent_id}")
        if intent.topic_id != topic_id:
            raise FragmentNotInTopic(
                f"接续意图 {intent_id} 属于话题 {intent.topic_id}，与 {topic_id} 不一致"
            )
        if intent.state in (INTENT_REPLACED, INTENT_CANCELLED):
            # F08/F14：旧请求重放不能重新激活被替换 / 取消的选择。
            raise IntentNotActive(
                f"接续意图 {intent_id} 已失效（{intent.state}），不能重新落实"
            )

        # 已落实过：复用同一个片段（重试不新建路径）
        if intent.resolved_fragment_id:
            existing = self.fragments.get(intent.resolved_fragment_id)
            if existing is not None:
                # 重试（传输重试 / 工具重试）：不重复创建，也不推进版本。
                return self._result(
                    topic_id,
                    existing.id,
                    created_fragment_id=existing.id,
                    source_fragment_id=intent.source_fragment_id,
                    nav_version=current_nav_version(self.conn),
                )

        source = self.fragments.get(intent.source_fragment_id) if intent.source_fragment_id else None
        if source is not None and source.closed_at is None:
            # 先落实位置（含 CAS），成功后才把意图标成已落实：CAS 失败时意图仍可重试。
            result = self.enter_topic(
                topic_id,
                fragment_id=source.id,
                relate=relate,
                expected_nav_version=expected_nav_version,
                turn_id=turn_id,
            )
            bindings.resolve_intent(intent_id, source.id)
            return result

        previous = self.anchors.get_active()
        open_fragment = self.fragments.open_fragment(topic_id)
        sealed_id: str | None = None
        with transaction(self.conn):
            if open_fragment is not None:
                sealed_id = open_fragment.id
                self.conn.execute(
                    "UPDATE fragments SET closed_at = ?, boundary_reason = ? WHERE id = ?",
                    (_now(), "history_continuation", sealed_id),
                )
            # 关系写入只走 FragmentManager.create_child：来源校验（同话题/自指/成环）
            # 与「来源、关系类型、分段原因」三个字段的写法只在这里定义一次。
            new_fragment_id = self.fragments.create_child(
                topic_id,
                source_fragment_id=intent.source_fragment_id,
                relation_type="history_reopen",
                boundary_reason="history_continuation",
            )
            self.anchors.set_active(topic_id, new_fragment_id)
            # 位置写入 + 导航版本 CAS 在同一个事务里：申请基于的起点变了就整体回滚
            # （不建片段、不落实意图、Anchor 不动），绝不执行旧申请。
            nav_version = cas_or_advance(
                self.conn,
                expected_nav_version,
                source="navigation.apply_continuation",
                topic_id=topic_id,
                fragment_id=new_fragment_id,
                turn_id=turn_id,
            )
            # 落实结果与片段创建在同一个事务里：不会出现「片段建了但意图没落实」
            bindings.resolve_intent(intent_id, new_fragment_id)
        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, topic_id)
        return self._result(
            topic_id,
            new_fragment_id,
            created_fragment_id=new_fragment_id,
            source_fragment_id=intent.source_fragment_id,
            sealed_fragment_id=sealed_id,
            nav_version=nav_version,
        )

    def create_topic(
        self,
        name: str,
        *,
        relate: bool = True,
        expected_nav_version: int | None = None,
        turn_id: str | None = None,
    ) -> NavigationResult:
        previous = self.anchors.get_active()
        # 建话题 + 写 Anchor + 版本 CAS 是同一个事务：申请基于的起点已经变了就整体
        # 回滚，**不会留下一个已创建但没人承认的话题**。
        with transaction(self.conn):
            node = self.nodes.create_topic(name)
            self.anchors.set_active(node.id)
            nav_version = cas_or_advance(
                self.conn,
                expected_nav_version,
                source="navigation.create_topic",
                topic_id=node.id,
                turn_id=turn_id,
            )
        if relate and previous is not None and previous.topic_id:
            relate_topics(self.conn, previous.topic_id, node.id)
        return self._result(node.id, None, nav_version=nav_version)

    # -- 待确认切换 -------------------------------------------------------

    def request_switch(self, topic_id: str, *, reason: str = "") -> dict:
        """登记「推测切换」：只写 pending，不碰 active。"""
        node = self.nodes.get_topic(topic_id)
        if node is None:
            raise TopicNotFound(f"话题不存在：{topic_id}")
        self.anchors.request_pending(topic_id)
        self._pending_reason = reason
        return {"topic_id": topic_id, "topic_name": node.name, "reason": reason}

    def pending_switch(self) -> dict | None:
        pending = self.anchors.get_pending()
        if pending is None or not pending.topic_id:
            return None
        node = self.nodes.get_topic(pending.topic_id)
        if node is None:
            return None
        return {
            "topic_id": pending.topic_id,
            "topic_name": node.name,
            "fragment_id": pending.fragment_id,
            "reason": self._pending_reason,
        }

    def confirm_switch(
        self,
        *,
        expected_nav_version: int | None = None,
        turn_id: str | None = None,
    ) -> NavigationResult | None:
        pending = self.pending_switch()
        if pending is None:
            return None
        result = self.enter_topic(
            pending["topic_id"],
            fragment_id=pending["fragment_id"],
            expected_nav_version=expected_nav_version,
            turn_id=turn_id,
        )
        self.reject_switch()
        return result

    def reject_switch(self) -> None:
        """用户说「保留当前」：待确认清空，Anchor 一动不动。"""
        self.anchors.discard_pending()
        self._pending_reason = ""

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

    def _result(
        self,
        topic_id: str,
        fragment_id: str | None,
        *,
        created_fragment_id: str | None = None,
        source_fragment_id: str | None = None,
        sealed_fragment_id: str | None = None,
        nav_version: int | None = None,
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
        )
