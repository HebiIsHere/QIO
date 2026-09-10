"""MemoryLifecycle: post-turn memory/knowledge lifecycle.

Owns fragment close (summary + entities + entity cards + index), knowledge
extraction (draft → verification → attach), and budget-pressure rolling
consolidation. Extracted from AppContext so the orchestrator reads as a
pipeline instead of a pile of domain details.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from agent.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

CONSOLIDATION_COOLDOWN_SECONDS = 600


class MemoryLifecycle:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fragments,
        topics,
        index_builder,
        embedding=None,
        user_root_id: Callable[[], str],
        refresh_selector: Callable[[], None],
    ) -> None:
        self.conn = conn
        self.fragments = fragments
        self.topics = topics
        self.index_builder = index_builder
        self.embedding = embedding
        self.user_root_id = user_root_id
        self.refresh_selector = refresh_selector

    # -- fragment close ---------------------------------------------------

    async def close_fragment(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> Any | None:
        from agent.memory.summary import summarize_fragment

        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return None
        messages = self.fragments.messages(fragment.id)
        summary, error = await summarize_fragment(adapter, [dict(m) for m in messages])
        if summary is None:
            self.fragments.close(fragment.id, "", summary_model=None, summary_version=0)
            return None

        # entities: lazy-create nodes + mention edges (attachment surface)
        entity_ids: list[str] = []
        from agent.graph.edges import EdgeService
        from agent.graph.nodes import NodeService

        nodes = NodeService(self.conn)
        edges = EdgeService(self.conn)
        for name in summary.entities:
            entity, created = nodes.mention(name, topic_id, force=False)
            if entity is not None:
                entity_ids.append(entity.id)
                edges.add(topic_id, entity.id, "mention")

        from agent.services.affinity import relate_shared_entities

        relate_shared_entities(self.conn, topic_id, entity_ids)

        # 实体卡提炼（失败静默降级）
        from agent.entities.cards import EntityCardService
        from agent.entities.extract import extract_entity_cards

        try:
            candidates = await extract_entity_cards(adapter, [dict(m) for m in messages])
            card_svc = EntityCardService(self.conn)
            for cand in candidates:
                card = card_svc.upsert(cand)
                try:
                    if self.embedding is not None and hasattr(
                        self.embedding, "save_entity_card_vector"
                    ):
                        self.embedding.save_entity_card_vector(
                            card.id, f"{card.name}：{card.summary or ''}"
                        )
                except Exception:
                    pass
        except Exception:
            logger.warning("entity card extraction failed", exc_info=True)

        self.fragments.close(
            fragment.id, summary.summary, summary_model=adapter.model, summary_version=1
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

        if tracer is not None:
            tracer.write("fragments_closed", fragment.id)
            tracer.write("summaries", f"{fragment.id}:{summary.title or ''}")
        await self.extract_knowledge(
            adapter, summary, topic_id, entity_ids, fragment.id, tracer=tracer
        )
        return self.fragments.get(fragment.id)

    # -- knowledge extraction --------------------------------------------

    async def extract_knowledge(
        self,
        adapter: BaseAdapter,
        summary: Any,
        topic_id: str,
        entity_ids: list[str],
        fragment_id: str,
        tracer=None,
    ) -> None:
        from agent.knowledge.lifecycle import HIGH_IMPACT_CATEGORIES, KnowledgeService
        from agent.knowledge.verify import VerificationService
        from agent.memory.summary import extract_knowledge_candidates

        extraction, error = await extract_knowledge_candidates(adapter, summary)
        if extraction is None:
            logger.info("knowledge extraction skipped: %s", error)
            return
        ks = KnowledgeService(self.conn)
        vs = VerificationService(self.conn, ks)
        for cand in extraction.candidates:
            node_ids: list[str] = []
            if cand.attach == "user":
                node_ids = [self.user_root_id()]
            elif cand.attach == "entity" and cand.entity:
                node, _ = self.topics.nodes.mention(cand.entity, topic_id, force=False)
                if node is not None:
                    node_ids = [node.id]
            else:
                node_ids = [topic_id]
            try:
                item = ks.create(
                    category=cand.category,
                    content=cand.content,
                    node_ids=node_ids,
                    provenance={"fragment_id": fragment_id},
                )
                if tracer is not None:
                    tracer.write("knowledge", item.id)
                ks.submit(item.id)
                if cand.category in HIGH_IMPACT_CATEGORIES:
                    continue  # stays draft; user confirmation required
                result = vs.review(ks.get(item.id), verified_by="system")
                if result.accepted:
                    ks.activate(item.id)
            except Exception as exc:  # extraction must not break the fragment close
                logger.warning("knowledge candidate failed: %s", exc)

    # -- budget-pressure consolidation -----------------------------------

    async def consolidate(self, topic_id: str, adapter: BaseAdapter) -> bool:
        """Rolling summary of the open fragment under budget pressure."""
        from agent.memory.summary import summarize_rolling

        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return False
        meta = dict(fragment.meta)
        last = meta.get("consolidated_at")
        if last:
            try:
                last_ts = datetime.fromisoformat(last)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last_ts).total_seconds()
                if age < CONSOLIDATION_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass
        messages = self.fragments.messages(fragment.id)
        summary, error = await summarize_rolling(
            adapter, fragment.summary, [dict(m) for m in messages]
        )
        if summary is None:
            logger.info("consolidation skipped: %s", error)
            return False
        now = datetime.now(timezone.utc).isoformat()
        meta["consolidated"] = True
        meta["consolidated_at"] = now
        meta["consolidation_count"] = int(meta.get("consolidation_count", 0)) + 1
        self.conn.execute(
            "UPDATE fragments SET summary = ?, summary_model = ?, "
            "summary_version = summary_version + 1, meta = ? WHERE id = ?",
            (summary.summary, adapter.model, json.dumps(meta, ensure_ascii=False), fragment.id),
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
        self.refresh_selector()
        return True
