"""Application context: wires storage, credentials, adapters, tools, loop.

Built once per process; the HTTP layer pulls what it needs from it.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from openai import AsyncOpenAI

from agent.adapters.base import AdapterMode, BaseAdapter
from agent.adapters.native import NativeAdapter
from agent.adapters.probe import ProbeCache, probe_adapter
from agent.adapters.text import TextAdapter
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.policy import CredentialPolicy, CredentialRef
from agent.credentials.store import CredentialStore
from agent.graph.topics import TopicService
from agent.memory.fragment import FragmentManager
from agent.memory.index import IndexBuilder
from agent.memory.ingest import MemoryWriter
from agent.selector.selector import Selector
from agent.services.injection import BudgetConfig, InjectionAssembler, InjectionBudget
from agent.services.retrieval import Retriever
from agent.storage.migrate import apply_migrations
from agent.tools.builtin import EchoTool, NowTool
from agent.tools.memory_search import MemorySearchTool
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAIN_LOOP_TAG = "main-loop"
FORCE_CONTINUE_DEFAULT = False


class AppContext:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, bus: EventBus) -> None:
        self.settings = settings
        self.conn = conn
        self.bus = bus
        self.credentials = CredentialStore(conn)
        self.policy = CredentialPolicy(self.credentials)
        self.probe_cache = ProbeCache()
        self.fragments = FragmentManager(conn)
        self.memory = MemoryWriter(conn, self.fragments)
        self.index_builder = IndexBuilder(conn)
        self.topics = TopicService(conn)
        self.selector = Selector()
        self.retriever = Retriever(self.selector, self.topics)
        self.registry = ToolRegistry()
        self.registry.register(EchoTool())
        self.registry.register(NowTool())
        self.registry.register(MemorySearchTool(self.retriever))
        self._refresh_selector()

    # -- adapter ----------------------------------------------------------

    def resolve_main_ref(self) -> CredentialRef | None:
        refs = self.policy.resolve("main-loop", [MAIN_LOOP_TAG])
        return refs[0] if refs else None

    async def build_adapter(self) -> BaseAdapter | None:
        ref = self.resolve_main_ref()
        if ref is None:
            return None
        secret = self.credentials.get_secret(ref.key_id)
        if secret is None:
            return None
        base_url = ref.endpoint or "https://api.openai.com/v1"
        client = AsyncOpenAI(api_key=secret, base_url=base_url)
        probe = await probe_adapter(
            client, ref.default_model or "gpt-4o-mini",
            endpoint=base_url, cache=self.probe_cache,
        )
        if probe.mode == AdapterMode.NATIVE:
            return NativeAdapter(client, ref.default_model or "gpt-4o-mini", endpoint=base_url)
        if probe.mode == AdapterMode.TEXT:
            return TextAdapter(client, ref.default_model or "gpt-4o-mini", endpoint=base_url)
        return None

    # -- selector refresh ------------------------------------------------

    def _refresh_selector(self) -> None:
        rows = self.conn.execute(
            "SELECT mi.id AS index_id, mi.fragment_id, mi.topic_id, mi.entity_ids, "
            "mi.keywords, mi.title, mi.token_estimate, mi.created_at, f.summary "
            "FROM memory_index mi LEFT JOIN fragments f ON f.id = mi.fragment_id"
        ).fetchall()
        docs = []
        titles: dict[str, str] = {}
        tokens: dict[str, int] = {}
        for row in rows:
            import json

            doc_id = row["index_id"]
            text = " ".join(
                [row["summary"] or "", row["title"] or ""]
            ).strip() or doc_id
            docs.append(
                {
                    "doc_id": doc_id,
                    "text": text,
                    "topic_id": row["topic_id"],
                    "entity_ids": json.loads(row["entity_ids"] or "[]"),
                    "keywords": json.loads(row["keywords"] or "[]"),
                    "created_at": row["created_at"],
                }
            )
            titles[doc_id] = row["title"] or ""
            tokens[doc_id] = row["token_estimate"] or 0
        from agent.selector.base import IndexedDoc

        self.selector.load(
            [IndexedDoc(**d) for d in docs], titles=titles, token_estimates=tokens
        )

    # -- injection --------------------------------------------------------

    def build_injection(self, query: str, anchor_topic_id: str | None = None) -> str:
        from agent.knowledge.inject import InjectionSource

        budget = InjectionBudget(
            BudgetConfig(
                context_window=1_000_000,
                memory_strength=self.settings.memory_strength
                if hasattr(self.settings, "memory_strength")
                else 0.5,
            )
        )
        assembler = InjectionAssembler(
            budget,
            self.retriever,
            knowledge_source=InjectionSource(self.conn),
        )
        payload = assembler.build(query, anchor_topic_id=anchor_topic_id, top_k=6)
        return payload.text

    # -- turn -------------------------------------------------------------

    async def run_turn(self, message: str, topic_id: str | None = None) -> dict:
        from agent.core.loop import AgentLoop

        adapter = await self.build_adapter()
        if adapter is None:
            await self.bus.publish(
                make_warning("no main-loop credential configured; add one in Settings")
            )
            return {"ok": False, "reason": "no_credential"}

        # write user message into memory domain first
        message_id, closed = self.memory.append_message(
            topic_id=topic_id or self._ensure_default_topic(),
            role="user",
            content=message,
            content_type="text",
            model=adapter.model,
        )
        injection = self.build_injection(message, anchor_topic_id=topic_id)
        prompt = message
        if injection:
            prompt = f"{injection}\n\n用户消息：{message}"

        loop = AgentLoop(adapter, self.registry, self.bus)
        try:
            result = await loop.run(prompt)
        except Exception as exc:
            logger.exception("turn failed")
            await self.bus.publish(
                make_error("turn_failed", str(exc)[:200], recoverable=True)
            )
            return {"ok": False, "reason": "turn_failed"}
        # record assistant message + tool messages are recorded by loop? (v1: record final)
        topic = topic_id or self._default_topic_id
        self.memory.append_message(
            topic_id=topic,
            role="assistant",
            content=result.final_content or "",
            content_type="text",
            model=adapter.model,
        )
        # chunk close + summary + index (best effort; summarizer needs the model)
        fragment = self.fragments.get_or_create_open(topic)
        if self.fragments.should_close(fragment):
            closed = await self._close_fragment(topic, adapter)
            if closed is not None:
                self._refresh_selector()
        return {"ok": True, "turn": result.__dict__}

    async def _close_fragment(self, topic_id: str, adapter: BaseAdapter) -> Any | None:
        from agent.memory.summary import summarize_fragment

        def summarizer(fragment, messages):
            return None  # replaced below with async call

        # async close with real summarizer
        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return None
        messages = self.fragments.messages(fragment.id)
        summary, error = await summarize_fragment(
            adapter, [dict(m) for m in messages]
        )
        if summary is not None:
            self.fragments.close(
                fragment.id,
                summary.summary,
                summary_model=adapter.model,
                summary_version=1,
            )
            self.index_builder.build(
                fragment_id=fragment.id,
                topic_id=topic_id,
                title=summary.title,
                summary_text=summary.summary,
                entities=summary.entities,
                keywords=summary.keywords,
                message_texts=[m["content"] for m in messages],
            )
            return self.fragments.get(fragment.id)
        self.fragments.close(fragment.id, "", summary_model=None, summary_version=0)
        return None

    # -- topics -----------------------------------------------------------

    def current_topic(self) -> str:
        """Current anchor topic (active cursor), falling back to the default topic."""
        row = self.conn.execute(
            "SELECT topic_id FROM cursor WHERE anchor_type = 'active'"
        ).fetchone()
        if row is not None and row["topic_id"]:
            return row["topic_id"]
        return self._ensure_default_topic()

    def session_messages(self, topic_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, role, content, content_type, created_at FROM messages "
            "WHERE fragment_id IN (SELECT id FROM fragments WHERE topic_id = ?) "
            "ORDER BY created_at",
            (topic_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _ensure_default_topic(self) -> str:
        if getattr(self, "_default_topic_id", None):
            return self._default_topic_id
        topics = self.topics.nodes.list_topics()
        if topics:
            self._default_topic_id = topics[0].id
        else:
            node = self.topics.nodes.create_topic("默认话题")
            self._default_topic_id = node.id
        return self._default_topic_id


def make_warning(message: str):
    from agent.api.events import EventType, make_event

    return make_event(EventType.WARNING, {"code": "app", "message": message})


def make_error(code: str, message: str, recoverable: bool):
    from agent.api.events import EventType, make_event

    return make_event(EventType.ERROR, {"code": code, "message": message, "recoverable": recoverable})