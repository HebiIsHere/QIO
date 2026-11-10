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
    INTENT_REPLACED,
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
