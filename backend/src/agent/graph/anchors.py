"""Anchor service: current topic + fragment position.

Anchor 语义（2026-09-12 定稿，见 docs/architecture.md「Anchor 生命周期」）：

* **Topic**：长期讨论主题；**Fragment**：记忆域的分块单位；
* **Anchor**：当前用户在某个 Topic 中明确关注 / 恢复到的历史位置，
  不是「这个 Topic 最新片段」的别名，也不是每次检索的副作用；
* 一个 Topic 一个位置（active 行即当前话题的位置；离开话题时旧位置写入 history）；
* 只有「用户明确选择」（Planet 从这里开始 / POST /api/anchor）与
  「Agent 显式 continue」才改变位置；memory_search 永远只读；
* 一轮成功结束后由 TurnOrchestrator 把位置推进到本轮真实片段（历史选择就此消费），
  失败 / 取消不推进。

pending 行（request_pending/confirm_pending/discard_pending）是历史遗留的
防抖入口，当前没有任何调用方；保留 API 与 schema（CHECK 约束含 pending），
新代码不要用它表达「待消费的用户选择」——那由 active 行本身承担。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Anchor:
    topic_id: str | None
    fragment_id: str | None
    anchor_type: str  # active / pending / history
    updated_at: str


class AnchorService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_active(self) -> Anchor | None:
        return self._get_type("active")

    def get_pending(self) -> Anchor | None:
        return self._get_type("pending")

    def set_active(self, topic_id: str, fragment_id: str | None = None) -> Anchor:
        """Move the current active anchor into history, then set a new one."""
        current = self.get_active()
        now = _now()
        if current is not None and current.topic_id != topic_id:
            # 历史行只写「仍然有效」的片段：损坏/跨话题的 fragment_id 落 NULL，
            # 否则 FK 会直接抛错，一个坏锚点能把之后每次切换都打断。
            self._insert(
                "history",
                current.topic_id,
                self._sanitized_fragment(current.topic_id, current.fragment_id),
                now,
            )
        self._upsert_singleton("active", topic_id, fragment_id, now)
        return self.get_active()  # type: ignore[return-value]

    def request_pending(self, topic_id: str, fragment_id: str | None = None) -> Anchor:
        """Debounce entry: candidate anchor before switching."""
        now = _now()
        self._upsert_singleton("pending", topic_id, fragment_id, now)
        pending = self.get_pending()
        assert pending is not None
        return pending

    def confirm_pending(self) -> Anchor | None:
        """pending -> active (the switch is confirmed)."""
        pending = self.get_pending()
        if pending is None or pending.topic_id is None:
            return None
        self.set_active(pending.topic_id, pending.fragment_id)
        self.conn.execute("DELETE FROM cursor WHERE anchor_type = 'pending'")
        return self.get_active()

    def discard_pending(self) -> None:
        """The pending switch did not settle; keep the current anchor."""
        self.conn.execute("DELETE FROM cursor WHERE anchor_type = 'pending'")

    def get_position(self, topic_id: str) -> Anchor | None:
        """Most recent known position for a topic (active or history)."""
        active = self.get_active()
        if active is not None and active.topic_id == topic_id:
            return active
        row = self.conn.execute(
            "SELECT * FROM cursor WHERE anchor_type = 'history' AND topic_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        return self._from_row(row) if row else None

    def position_fragment(self, topic_id: str) -> str | None:
        """该话题已保存且**仍然有效**的片段位置（无效/跨话题 → None）。

        切回一个已有历史位置的话题时必须走这里：片段可能已被归档、清理，
        或者数据损坏（记录指向别的 topic 的片段）。任何一种情况都只能降级为
        None，绝不能把错误片段注入别的话题。
        """
        position = self.get_position(topic_id)
        fragment_id = position.fragment_id if position is not None else None
        if not fragment_id:
            return None
        row = self.conn.execute(
            "SELECT topic_id FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
        if row is None or row["topic_id"] != topic_id:
            return None
        return fragment_id

    def restore_position(self, topic_id: str) -> Anchor:
        """切回话题：恢复该话题保存的位置（没有/无效则为 None）。"""
        return self.set_active(topic_id, self.position_fragment(topic_id))

    def open_fragment_id(self, topic_id: str) -> str | None:
        """该话题当前开放（仍在写入）的片段；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT id FROM fragments WHERE topic_id = ? AND closed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        return row["id"] if row is not None else None

    def focus_fragment(self, topic_id: str) -> str | None:
        """本轮真正需要额外 Focus 的片段：位置有效、且**不是**当前开放片段。

        当前开放片段已经通过短期转录进入上下文，再注入一份就是重复噪声；
        只有用户指向历史片段（从这里开始 / Agent 显式 continue 到历史片段）时，
        才需要把那段历史显式抬进本轮 Focus。
        """
        fragment_id = self.position_fragment(topic_id)
        if fragment_id is None:
            return None
        if fragment_id == self.open_fragment_id(topic_id):
            return None
        return fragment_id

    def is_historic_position(self, topic_id: str) -> bool:
        """当前位置是否是一个「历史位置」（用于前端只显示真实的历史选择）。"""
        return self.focus_fragment(topic_id) is not None

    def history(self, topic_id: str) -> list[Anchor]:
        rows = self.conn.execute(
            "SELECT * FROM cursor WHERE anchor_type = 'history' AND topic_id = ? "
            "ORDER BY updated_at DESC",
            (topic_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    # -- internals --------------------------------------------------------

    def _get_type(self, anchor_type: str) -> Anchor | None:
        row = self.conn.execute(
            "SELECT * FROM cursor WHERE anchor_type = ?", (anchor_type,)
        ).fetchone()
        return self._from_row(row) if row else None

    def _sanitized_fragment(self, topic_id: str | None, fragment_id: str | None) -> str | None:
        """片段不存在 / 不属于该话题时返回 None（用于写历史行前清洗）。"""
        if not topic_id or not fragment_id:
            return None
        row = self.conn.execute(
            "SELECT topic_id FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
        if row is None or row["topic_id"] != topic_id:
            return None
        return fragment_id

    def _upsert_singleton(self, anchor_type: str, topic_id: str | None, fragment_id: str | None, now: str) -> None:
        existing = self._get_type(anchor_type)
        if existing is not None:
            self.conn.execute(
                "UPDATE cursor SET topic_id = ?, fragment_id = ?, updated_at = ? "
                "WHERE anchor_type = ?",
                (topic_id, fragment_id, now, anchor_type),
            )
        else:
            self.conn.execute(
                "INSERT INTO cursor (id, topic_id, fragment_id, anchor_type, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (anchor_type, topic_id, fragment_id, anchor_type, now),
            )

    def _insert(self, anchor_type: str, topic_id: str | None, fragment_id: str | None, now: str) -> None:
        self.conn.execute(
            "INSERT INTO cursor (id, topic_id, fragment_id, anchor_type, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", topic_id, fragment_id, anchor_type, now),
        )

    def _from_row(self, row: sqlite3.Row) -> Anchor:
        return Anchor(
            topic_id=row["topic_id"],
            fragment_id=row["fragment_id"],
            anchor_type=row["anchor_type"],
            updated_at=row["updated_at"],
        )
