"""Fragment management: open fragment per topic, close on thresholds.

A fragment is the chunk boundary of the memory domain. The open fragment
is where new messages are appended; closing writes the model summary and
freezes the chunk (append-only).

封块阈值按**对话轮**计算（第三阶段 spec 第 57~59 条）：一轮 = 一条 user 消息
加上它之后的 assistant 回答；工具消息不构成用户理解中的「一轮」。
设置页写「标准（10 轮）」时，这里就必须真的封在 10 轮，而不是 10 条消息
（旧实现数消息条数，所以「10 轮」实际只有 5 轮）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_MAX_TURNS = 10
DEFAULT_MAX_TOKENS = 4096

# 记忆封块设置的键与范围：设置页文案一直写「轮」，所以键名也必须说「轮」。
FRAGMENT_TURNS_KEY = "fragment.max_turns"
# 旧键记的其实是消息条数（文案却写「轮」，语义错误）。只作为一次性迁移回退读取。
LEGACY_FRAGMENT_MESSAGES_KEY = "fragment.max_messages"
FRAGMENT_MIN_TURNS = 1
FRAGMENT_MAX_TURNS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def resolve_max_turns(store, default: int = DEFAULT_MAX_TURNS) -> int:
    """读取记忆封块阈值（单位：轮）。

    新键优先；只有旧键（历史上按消息条数计）时做一次性迁移：用旧值并写回新键，
    旧键保留不删（不丢用户数据，回退也仍然读得到）。越界的旧值收敛到合法区间。

    API 层与服务层共用这一处解析，避免「设置页读到旧值、运行时算另一套」这类
    两处实现漂移。
    """
    if store.get(FRAGMENT_TURNS_KEY) is not None:
        value = store.get_int(FRAGMENT_TURNS_KEY, default)
    elif store.get(LEGACY_FRAGMENT_MESSAGES_KEY) is not None:
        value = store.get_int(LEGACY_FRAGMENT_MESSAGES_KEY, default)
        store.set(FRAGMENT_TURNS_KEY, str(value))
    else:
        return default
    return max(FRAGMENT_MIN_TURNS, min(FRAGMENT_MAX_TURNS, value))


@dataclass(frozen=True)
class Fragment:
    id: str
    topic_id: str
    start_message_id: str | None
    end_message_id: str | None
    summary: str | None
    summary_model: str | None
    summary_version: int
    created_at: str
    closed_at: str | None
    meta: dict


class FragmentManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.conn = conn
        self.max_turns = max_turns
        self.max_tokens = max_tokens

    def get_or_create_open(self, topic_id: str) -> Fragment:
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE topic_id = ? AND closed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        if row is not None:
            return self._from_row(row)
        fragment_id = new_id("frag")
        now = _now()
        self.conn.execute(
            "INSERT INTO fragments (id, topic_id, created_at, summary_version, meta) "
            "VALUES (?, ?, ?, 0, '{}')",
            (fragment_id, topic_id, now),
        )
        return Fragment(
            id=fragment_id,
            topic_id=topic_id,
            start_message_id=None,
            end_message_id=None,
            summary=None,
            summary_model=None,
            summary_version=0,
            created_at=now,
            closed_at=None,
            meta={},
        )

    def open_fragment(self, topic_id: str) -> Fragment | None:
        """当前开放片段（只读；不存在时返回 None，不创建）。"""
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE topic_id = ? AND closed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def get(self, fragment_id: str) -> Fragment | None:
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    # -- 关系：来源、祖先路径、校验（阶段 3）-----------------------------

    def source_of(self, fragment_id: str) -> str | None:
        """这条片段的直接来源（普通延续 / 历史重新展开都记在这里）。"""
        row = self.conn.execute(
            "SELECT source_fragment_id FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
        return row["source_fragment_id"] if row is not None else None

    def ancestors(
        self, fragment_id: str, *, max_depth: int = 5
    ) -> list[tuple[str, int]]:
        """从直接来源开始的祖先链：(fragment_id, depth)，由近到远。

        三件事必须同时成立，否则历史关系会把上下文构建拖坏：

        * **深度上限**：坏数据里的长链不会无限读下去；
        * **环保护**：A→B→A 这种坏关系不能变成死循环；
        * **去重**：同一条祖先只出现一次（取最近的那次深度）。
        """
        chain: list[tuple[str, int]] = []
        seen: set[str] = {fragment_id}
        current = self.source_of(fragment_id)
        depth = 1
        while current and depth <= max_depth and current not in seen:
            chain.append((current, depth))
            seen.add(current)
            current = self.source_of(current)
            depth += 1
        return chain

    def validate_source(self, topic_id: str, source_fragment_id: str | None) -> None:
        """来源必须存在、属于同一个话题、不能自指、不能成环。"""
        if not source_fragment_id:
            return
        source = self.get(source_fragment_id)
        if source is None:
            raise ValueError(f"来源片段不存在：{source_fragment_id}")
        if source.topic_id != topic_id:
            raise ValueError(
                f"来源片段 {source_fragment_id} 不属于话题 {topic_id}（不能跨话题继承）"
            )

    def would_cycle(self, fragment_id: str, source_fragment_id: str | None) -> bool:
        """把 `fragment_id` 挂到 `source_fragment_id` 下面，会不会让祖先链成环。

        两种情况成环：来源就是自己；来源的祖先链里已经出现自己
        （等于把一段挂到它自己的后代下面）。新建片段用的是全新 id，
        不可能触发第二种 —— 这个判断真正的用途是**改挂来源**这类操作。
        """
        if not source_fragment_id:
            return False
        if source_fragment_id == fragment_id:
            return True
        return any(
            fid == fragment_id for fid, _ in self.ancestors(source_fragment_id, max_depth=64)
        )

    def create_child(
        self,
        topic_id: str,
        *,
        source_fragment_id: str | None = None,
        relation_type: str = "normal",
        boundary_reason: str | None = None,
        same_stage: bool | None = None,
    ) -> str:
        """新建一条片段并写清它的来源与分段原因（关系写入的唯一入口）。"""
        fragment_id = new_id("frag")
        self.validate_source(topic_id, source_fragment_id)
        # 新 id 不可能已经在来源的祖先链里，所以这里只需要校验来源本身；
        # 环只可能来自「改挂来源」或坏数据，前者用 would_cycle 判，后者由 ancestors 兜住。
        self.conn.execute(
            "INSERT INTO fragments "
            "(id, topic_id, created_at, summary_version, meta, source_fragment_id, "
            " relation_type, boundary_reason, same_stage, content_version) "
            "VALUES (?, ?, ?, 0, '{}', ?, ?, ?, ?, 0)",
            (
                fragment_id,
                topic_id,
                _now(),
                source_fragment_id,
                relation_type,
                boundary_reason,
                None if same_stage is None else (1 if same_stage else 0),
            ),
        )
        return fragment_id

    def close(
        self,
        fragment_id: str,
        summary: str,
        summary_model: str | None = None,
        summary_version: int = 1,
    ) -> None:
        self.conn.execute(
            "UPDATE fragments SET summary = ?, summary_model = ?, summary_version = ?, "
            "closed_at = ? WHERE id = ?",
            (summary, summary_model, summary_version, _now(), fragment_id),
        )

    def seal(
        self,
        fragment_id: str,
        *,
        reason: str | None = None,
        content_version: int | None = None,
    ) -> None:
        """**封存**片段：只固定「这一段到今天为止」这件事，不写摘要。

        摘要与索引属于派生数据（阶段 2）：它们可能失败、可以重试，
        不该把「封存」这件事一起拖慢或拖回。所以封存自己只做三件事：
        记封存时间、记分段原因、固定内容版本（派生任务据此校验迟到结果）。
        """
        if content_version is None:
            self.conn.execute(
                "UPDATE fragments SET closed_at = ?, "
                "boundary_reason = COALESCE(?, boundary_reason) WHERE id = ?",
                (_now(), reason, fragment_id),
            )
            return
        self.conn.execute(
            "UPDATE fragments SET closed_at = ?, boundary_reason = COALESCE(?, boundary_reason), "
            "content_version = ? WHERE id = ?",
            (_now(), reason, content_version, fragment_id),
        )

    def should_close(self, fragment: Fragment) -> bool:
        """Chunk-boundary policy: 完整的对话轮数达到上限才封块。

        两个条件缺一不可：

        1. 已经开始的轮数（= user 消息数）达到 `max_turns`；
        2. 当前片段不是停在半轮（最后落下的不是 user 消息）。

        第 2 条保证第 N 轮的助手回答不会因为「刚好数到 N 轮的 user 消息」
        被写进下一个片段，把一轮对话拆到两个片段里。
        """
        if self.turn_count(fragment.id) < self.max_turns:
            return False
        return not self._ends_mid_turn(fragment.id)

    def message_count(self, fragment_id: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE fragment_id = ?",
                (fragment_id,),
            ).fetchone()["c"]
        )

    def turn_count(self, fragment_id: str) -> int:
        """已开始的对话轮数：一条 user 消息开启一轮。

        工具消息与助手回答都不计入 —— 它们是这一轮的一部分，不是新的一轮。
        """
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE fragment_id = ? AND role = 'user'",
                (fragment_id,),
            ).fetchone()["c"]
        )

    def _ends_mid_turn(self, fragment_id: str) -> bool:
        """片段是否停在半轮（最后一条消息是 user）。"""
        row = self.conn.execute(
            "SELECT role FROM messages WHERE fragment_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        return row is not None and row["role"] == "user"

    def messages(self, fragment_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE fragment_id = ? ORDER BY created_at",
            (fragment_id,),
        ).fetchall()

    def _from_row(self, row: sqlite3.Row) -> Fragment:
        return Fragment(
            id=row["id"],
            topic_id=row["topic_id"],
            start_message_id=row["start_message_id"],
            end_message_id=row["end_message_id"],
            summary=row["summary"],
            summary_model=row["summary_model"],
            summary_version=row["summary_version"],
            created_at=row["created_at"],
            closed_at=row["closed_at"],
            meta=json.loads(row["meta"] or "{}"),
        )
