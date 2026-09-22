"""轮次绑定与接续意图的持久状态（阶段 1）。

在这之前，「这一轮写入哪个 Fragment」是在写入那一刻临时看 Anchor 决定的：

- 轮询期间用户改了导航，`persist` 会把**已经提交**的用户消息搬到别的话题；
- 用户「从某段历史继续」时，目标话题若已有开放片段，插入新片段会撞上
  `idx_fragments_one_open_per_topic`。

这份服务把归属变成一条持久记录：轮前确定 → 写入照做 → 收尾更新。
它只做数据库读写（没有 await），事务边界短，可被传输层重试安全地重复调用。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.storage.db import transaction

# 接续意图的状态：
# registered —— 用户已选择，尚未有消息执行到它（待落实）
# resolved   —— 已落实到一个具体 Fragment，但首轮还没成功推进
# consumed   —— 首轮成功推进（界面提示可以撤下，来源关系保留）
# replaced   —— 用户在发送前改选了别的历史
# cancelled  —— 用户取消（不发消息就不留痕迹）
INTENT_REGISTERED = "registered"
INTENT_RESOLVED = "resolved"
INTENT_CONSUMED = "consumed"
INTENT_REPLACED = "replaced"
INTENT_CANCELLED = "cancelled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_intent_id() -> str:
    return f"intent_{uuid.uuid4().hex[:12]}"


class BindingConflict(ValueError):
    """同一个 turn 试图写入不同的归属；同一轮只能有一个 Topic/Fragment。"""


class IntentNotFound(ValueError):
    """接续意图不存在。"""


@dataclass(frozen=True)
class TurnBinding:
    turn_id: str
    topic_id: str
    fragment_id: str | None
    intent_id: str | None
    intent_version: int | None
    write_state: str
    status: str | None


@dataclass(frozen=True)
class ContinuationIntent:
    intent_id: str
    topic_id: str
    source_fragment_id: str | None
    version: int
    state: str
    resolved_fragment_id: str | None
    request_id: str | None


def _binding_from_row(row: sqlite3.Row) -> TurnBinding:
    return TurnBinding(
        turn_id=str(row["turn_id"]),
        topic_id=str(row["topic_id"]),
        fragment_id=row["fragment_id"],
        intent_id=row["intent_id"],
        intent_version=row["intent_version"],
        write_state=str(row["write_state"]),
        status=row["status"],
    )


def _intent_from_row(row: sqlite3.Row) -> ContinuationIntent:
    return ContinuationIntent(
        intent_id=str(row["intent_id"]),
        topic_id=str(row["topic_id"]),
        source_fragment_id=row["source_fragment_id"],
        version=int(row["version"]),
        state=str(row["state"]),
        resolved_fragment_id=row["resolved_fragment_id"],
        request_id=row["request_id"],
    )


class TurnBindingService:
    """一轮对话的归属 + 用户的历史接续意图。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 轮次绑定 ---------------------------------------------------------

    def binding_for(self, turn_id: str) -> TurnBinding | None:
        row = self.conn.execute(
            "SELECT * FROM turn_bindings WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return _binding_from_row(row) if row is not None else None

    def record_binding(
        self,
        turn_id: str,
        topic_id: str,
        *,
        fragment_id: str | None = None,
        intent_id: str | None = None,
        intent_version: int | None = None,
        system: bool = False,
    ) -> TurnBinding:
        """写入本轮归属。同一个 turn 重复写同名值算重试（幂等），改值算冲突。"""
        existing = self.binding_for(turn_id)
        if existing is not None:
            # 懒创建补全：绑定先落在「还没有开放片段」的话题上（fragment_id=None），
            # 随后第一条消息创建了片段 —— 这时把具体片段补进绑定是**细化**，不是改归属。
            if existing.fragment_id is None and fragment_id is not None:
                self.conn.execute(
                    "UPDATE turn_bindings SET fragment_id = ?, updated_at = ? WHERE turn_id = ?",
                    (fragment_id, _now(), turn_id),
                )
                return self.binding_for(turn_id)  # type: ignore[return-value]
            if (
                existing.topic_id != topic_id
                or existing.fragment_id != fragment_id
                or existing.intent_id != intent_id
            ):
                raise BindingConflict(
                    f"turn {turn_id} 已绑定 {existing.topic_id}/{existing.fragment_id}，"
                    f"不能改成 {topic_id}/{fragment_id}"
                )
            self.conn.execute(
                "UPDATE turn_bindings SET updated_at = ? WHERE turn_id = ?", (_now(), turn_id)
            )
            return self.binding_for(turn_id)  # type: ignore[return-value]

        now = _now()
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO turn_bindings "
                    "(turn_id, topic_id, fragment_id, intent_id, intent_version, "
                    " write_state, status, system, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'open', NULL, ?, ?, ?)",
                    (
                        turn_id,
                        topic_id,
                        fragment_id,
                        intent_id,
                        intent_version,
                        1 if system else 0,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            # 并发/重试：另一条路径先写成功了。只有值完全一致才算幂等。
            existing = self.binding_for(turn_id)
            if existing is None or existing.topic_id != topic_id or existing.fragment_id != fragment_id:
                raise BindingConflict(f"turn {turn_id} 的绑定写入竞争失败") from None
            return existing
        return self.binding_for(turn_id)  # type: ignore[return-value]

    def mark_write_closed(self, turn_id: str) -> None:
        """写入结束：本轮不会再追加内容（可以据此释放片段占用）。"""
        self.conn.execute(
            "UPDATE turn_bindings SET write_state = 'closed', updated_at = ? WHERE turn_id = ?",
            (_now(), turn_id),
        )

    def rebind_topic_from_tool_nav(self, turn_id: str, topic_id: str) -> TurnBinding:
        """受控例外：本轮自己的工具换了话题，归属跟着走。

        这是 `record_binding` 唯一允许改话题的入口，因此条件卡得很死：

        * 只能改**还没收尾**的轮次（`write_state='open'`）—— 收尾后再改就是把
          已归档的轮次搬走，正是阶段 1 要禁止的事；
        * 只把话题改成新值、片段清空 —— 具体片段随后由 `record_binding` 的
          「懒创建补全」分支补上，不在这里猜；
        * 调用方必须先确认 Anchor 仍停在 `topic_id`（用户后续导航优先），
          否则用 `record_binding` 该报 `BindingConflict` 还是照报。
        """
        existing = self.binding_for(turn_id)
        if existing is None:
            raise BindingConflict(f"turn {turn_id} 还没有绑定，不能做工具导航改向")
        if existing.write_state != "open":
            raise BindingConflict(
                f"turn {turn_id} 已 {existing.write_state}，不能再改归属"
            )
        if existing.topic_id == topic_id:
            return existing
        self.conn.execute(
            "UPDATE turn_bindings SET topic_id = ?, fragment_id = NULL, updated_at = ? "
            "WHERE turn_id = ?",
            (topic_id, _now(), turn_id),
        )
        return self.binding_for(turn_id)  # type: ignore[return-value]

    def mark_status(self, turn_id: str, status: str) -> None:
        """记录终态（completed/failed/cancelled/unavailable），供恢复时判断。"""
        self.conn.execute(
            "UPDATE turn_bindings SET status = ?, updated_at = ? WHERE turn_id = ?",
            (status, _now(), turn_id),
        )

    def fragment_write_busy(self, fragment_id: str) -> bool:
        """该片段上是否还有「未结束写入」的轮次（封存前必须为 False）。"""
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM turn_bindings "
            "WHERE fragment_id = ? AND write_state = 'open'",
            (fragment_id,),
        ).fetchone()
        return bool(row["c"])

    # -- 接续意图 ---------------------------------------------------------

    def intent_by_id(self, intent_id: str) -> ContinuationIntent | None:
        row = self.conn.execute(
            "SELECT * FROM continuation_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return _intent_from_row(row) if row is not None else None

    def intent_by_request(self, request_id: str) -> ContinuationIntent | None:
        row = self.conn.execute(
            "SELECT * FROM continuation_intents WHERE request_id = ?", (request_id,)
        ).fetchone()
        return _intent_from_row(row) if row is not None else None

    def peek_intent(self) -> ContinuationIntent | None:
        """当前待落实的接续选择（没有就 None）。"""
        row = self.conn.execute(
            "SELECT * FROM continuation_intents WHERE state = ? "
            "ORDER BY version DESC, created_at DESC LIMIT 1",
            (INTENT_REGISTERED,),
        ).fetchone()
        return _intent_from_row(row) if row is not None else None

    def register_intent(
        self, topic_id: str, source_fragment_id: str | None, *, request_id: str | None = None
    ) -> ContinuationIntent:
        """登记「下一次发送要从这段历史继续」。

        - 同一个 request_id 重发：返回既有意图（传输重试不制造第二条路径）；
        - 重复点击同一段历史：返回既有意图，版本不涨（避免无意义的历史）；
        - 发送前改选另一段历史：旧的登记态标 replaced，新意图版本 +1。
        """
        if request_id:
            existing = self.intent_by_request(request_id)
            if existing is not None:
                return existing
        current = self.peek_intent()
        if current is not None and current.topic_id == topic_id and current.source_fragment_id == source_fragment_id:
            return current

        now = _now()
        version = (current.version + 1) if current is not None else 1
        intent_id = new_intent_id()
        with transaction(self.conn):
            if current is not None:
                self.conn.execute(
                    "UPDATE continuation_intents SET state = ?, updated_at = ? WHERE intent_id = ?",
                    (INTENT_REPLACED, now, current.intent_id),
                )
            self.conn.execute(
                "INSERT INTO continuation_intents "
                "(intent_id, topic_id, source_fragment_id, version, state, "
                " resolved_fragment_id, request_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (intent_id, topic_id, source_fragment_id, version, INTENT_REGISTERED, request_id, now, now),
            )
        return self.intent_by_id(intent_id)  # type: ignore[return-value]

    def resolve_intent(self, intent_id: str, fragment_id: str) -> ContinuationIntent:
        """落实：这条意图真正对应到某个 Fragment（首轮还没成功）。"""
        intent = self.intent_by_id(intent_id)
        if intent is None:
            raise IntentNotFound(f"接续意图不存在：{intent_id}")
        if intent.state in (INTENT_RESOLVED, INTENT_CONSUMED):
            if intent.resolved_fragment_id != fragment_id:
                raise BindingConflict(
                    f"意图 {intent_id} 已落实到 {intent.resolved_fragment_id}，不能改成 {fragment_id}"
                )
            return intent
        if intent.state == INTENT_CANCELLED:
            raise BindingConflict(f"意图 {intent_id} 已取消，不能再落实")
        self.conn.execute(
            "UPDATE continuation_intents SET state = ?, resolved_fragment_id = ?, updated_at = ? "
            "WHERE intent_id = ?",
            (INTENT_RESOLVED, fragment_id, _now(), intent_id),
        )
        return self.intent_by_id(intent_id)  # type: ignore[return-value]

    def consume_intent(self, intent_id: str) -> ContinuationIntent:
        """首轮成功推进：提示可以撤下（来源关系永久保留）。"""
        intent = self.intent_by_id(intent_id)
        if intent is None:
            raise IntentNotFound(f"接续意图不存在：{intent_id}")
        if intent.state == INTENT_CONSUMED:
            return intent
        self.conn.execute(
            "UPDATE continuation_intents SET state = ?, updated_at = ? WHERE intent_id = ?",
            (INTENT_CONSUMED, _now(), intent_id),
        )
        return self.intent_by_id(intent_id)  # type: ignore[return-value]

    def cancel_intent(self, intent_id: str) -> ContinuationIntent:
        """用户取消选择：不发消息就不留痕迹。"""
        intent = self.intent_by_id(intent_id)
        if intent is None:
            raise IntentNotFound(f"接续意图不存在：{intent_id}")
        if intent.state == INTENT_CANCELLED:
            return intent
        self.conn.execute(
            "UPDATE continuation_intents SET state = ?, updated_at = ? WHERE intent_id = ?",
            (INTENT_CANCELLED, _now(), intent_id),
        )
        return self.intent_by_id(intent_id)  # type: ignore[return-value]
