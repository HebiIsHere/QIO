"""Memory writer: append-only transcript ingestion.

Discipline: the main model is the only writer of summaries; this module
only records what happened (append-only) and triggers chunk closing when
the fragment thresholds are hit. The summarizer callback is injected so
callers control the (async) model call.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from agent.memory.fragment import Fragment, FragmentManager, new_id


class BindingMismatch(ValueError):
    """写入的归属与轮次绑定的归属不一致。

    出现它说明调用方想写的 Fragment 不是这一轮绑定的那一个 —— 这种时候
    绝不能「就近写进当前开放片段」：那正是把已完成轮次的消息搬走的做法。
    """


class MemoryWriter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fragments: FragmentManager,
        bindings: object | None = None,
    ) -> None:
        self.conn = conn
        self.fragments = fragments
        # 轮次绑定服务（agent.services.binding.TurnBindingService）。
        # 用对象注入而不是直接 import：memory/ 不反过来依赖 services/。
        self.bindings = bindings

    def _resolve_fragment(
        self,
        *,
        topic_id: str,
        fragment_id: str | None,
        turn_id: str | None,
    ) -> Fragment:
        """确定这条消息该写进哪个 Fragment。

        优先级：轮次绑定 > 调用方显式传入 > 该话题当前开放片段。
        （没有轮次绑定的写入是系统通知一类，沿用旧行为。）
        """
        binding = None
        if turn_id and self.bindings is not None:
            binding = self.bindings.binding_for(turn_id)
        if binding is not None:
            if binding.topic_id != topic_id:
                raise BindingMismatch(
                    f"turn {turn_id} 绑定在话题 {binding.topic_id}，不能写入 {topic_id}"
                )
            if fragment_id is not None and binding.fragment_id not in (None, fragment_id):
                raise BindingMismatch(
                    f"turn {turn_id} 绑定片段 {binding.fragment_id}，与传入的 {fragment_id} 不一致"
                )
            fragment_id = binding.fragment_id if binding.fragment_id else fragment_id

        if fragment_id is None:
            return self.fragments.get_or_create_open(topic_id)

        fragment = self.fragments.get(fragment_id)
        if fragment is None:
            raise BindingMismatch(f"片段不存在：{fragment_id}")
        if fragment.topic_id != topic_id:
            raise BindingMismatch(f"片段 {fragment_id} 不属于话题 {topic_id}")
        if fragment.closed_at is not None:
            raise BindingMismatch(
                f"片段 {fragment_id} 已封存，不能继续追加内容（这一轮绑定的是它）"
            )
        return fragment

    def append_message(
        self,
        *,
        topic_id: str,
        role: str,
        content: str,
        content_type: str = "text",
        model: str | None = None,
        raw: dict | None = None,
        fragment_id: str | None = None,
        turn_id: str | None = None,
    ) -> tuple[str, Fragment | None]:
        """Append one message; returns (message_id, closed_fragment).

        closed_fragment is non-None when this append crossed the chunk
        threshold and the fragment was closed. Closing itself does not
        summarize — the caller invokes the summarizer via
        close_open_fragment().
        """
        fragment = self._resolve_fragment(
            topic_id=topic_id, fragment_id=fragment_id, turn_id=turn_id
        )
        message_id = new_id("msg")
        now = self._now()
        self.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, "
            "model, raw, created_at, storage_tier, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'hot', ?)",
            (
                message_id,
                fragment.id,
                role,
                content,
                content_type,
                model,
                json.dumps(raw or {}, ensure_ascii=False),
                now,
                turn_id,
            ),
        )
        if fragment.start_message_id is None:
            self.conn.execute(
                "UPDATE fragments SET start_message_id = ? WHERE id = ?",
                (message_id, fragment.id),
            )
        self.conn.execute(
            "UPDATE fragments SET end_message_id = ? WHERE id = ?",
            (message_id, fragment.id),
        )
        closed: Fragment | None = None
        if self.fragments.should_close(fragment):
            closed = self.fragments.get(fragment.id)
        return message_id, closed

    def close_open_fragment(
        self,
        topic_id: str,
        summarizer: Callable[[Fragment, list[sqlite3.Row]], str | None],
        *,
        summary_model: str | None = None,
    ) -> Fragment | None:
        """Close the open fragment for a topic using the injected summarizer.

        summarizer returns the summary text, or None on failure (degraded:
        the fragment is closed with an empty summary; the index falls back
        to direct citation of the raw text).
        """
        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return None  # nothing to summarize
        messages = self.fragments.messages(fragment.id)
        summary = summarizer(fragment, messages)
        if summary:
            self.fragments.close(fragment.id, summary, summary_model=summary_model)
        else:
            self.fragments.close(fragment.id, "", summary_model=None, summary_version=0)
        return self.fragments.get(fragment.id)

    def _now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()
