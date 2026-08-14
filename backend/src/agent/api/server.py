"""FastAPI application: health, SSE, credentials, turns, graph, approvals."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections import deque
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import AsyncOpenAI

from agent.api.bus import EventBus
from agent.api.events import AgentEvent, EventType, make_event
from agent.adapters.probe import probe_adapter
from agent.config import Settings
from agent.graph.layout import assign_positions
from agent.services.app import AppContext

FRAGMENT_MIN_MESSAGES = 1
FRAGMENT_MAX_MESSAGES = 30
DEFAULT_FRAGMENT_MAX_MESSAGES = 10


def create_app(settings: Settings, conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI(title="QIO", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local desktop app: frontend dev origin
        allow_methods=["*"],
        allow_headers=["*"],
    )
    bus = EventBus()
    ctx = AppContext(settings, conn, bus)
    from agent.tools.approval import ApprovalService

    approvals = ctx.approvals

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "db": conn.execute("SELECT 1").fetchone()[0] == 1}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(bus.stream(), media_type="text/event-stream")

    # -- credentials -------------------------------------------------------

    @app.get("/api/credentials")
    async def list_credentials() -> dict:
        creds = ctx.credentials.list_credentials()
        for c in creds:
            c["key_id"] = c.pop("id")
        return {"credentials": creds}

    @app.post("/api/credentials")
    async def create_credential(body: dict) -> dict:
        key_id = str(body.get("key_id", "")).strip() or f"key_{uuid.uuid4().hex[:12]}"
        try:
            version = ctx.credentials.create(
                key_id=key_id,
                secret=body["secret"],
                tags=body.get("tags", []),
                endpoint=body.get("endpoint"),
                default_model=body.get("default_model"),
                scope=body.get("scope"),
                budget=body.get("budget"),
                note=body.get("note"),
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await bus.publish(
            make_event(
                EventType.CREDENTIAL_STATUS,
                {"key_id": key_id, "status": "active", "version": version},
            )
        )
        return {"ok": True, "key_id": key_id, "version": version}

    @app.post("/api/credentials/identify")
    async def identify_credential(body: dict) -> dict:
        from agent.services.identify import identify_key

        key = str(body.get("secret", "")).strip()
        if not key:
            raise HTTPException(status_code=400, detail="secret required")
        result = await identify_key(key)
        if result is None:
            return {"identified": False}
        return {"identified": True, **result}

    @app.post("/api/credentials/{key_id}/test")
    async def test_credential(key_id: str) -> dict:
        secret = ctx.credentials.get_secret(key_id)
        meta = ctx.credentials.get_metadata(key_id)
        if secret is None or meta is None:
            raise HTTPException(status_code=404, detail="credential unavailable")
        base_url = meta["endpoint"] or "https://api.openai.com/v1"
        model = meta["default_model"] or "gpt-4o-mini"
        client = AsyncOpenAI(api_key=secret, base_url=base_url)
        try:
            probe = await probe_adapter(client, model, endpoint=base_url)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"connection failed: {type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
        return {
            "key_id": key_id,
            "probe": {"mode": probe.mode.value, "detail": probe.detail},
        }

    @app.post("/api/credentials/{key_id}/revoke")
    async def revoke_credential(key_id: str) -> dict:
        try:
            ctx.credentials.revoke(key_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await bus.publish(
            make_event(EventType.CREDENTIAL_STATUS, {"key_id": key_id, "status": "revoked"})
        )
        return {"ok": True, "key_id": key_id}

    # -- settings -----------------------------------------------------------

    @app.get("/api/settings/memory")
    async def get_memory_settings() -> dict:
        return {
            "fragment_max_messages": ctx.settings_store.get_int(
                "fragment.max_messages", DEFAULT_FRAGMENT_MAX_MESSAGES
            )
        }

    @app.put("/api/settings/memory")
    async def update_memory_settings(body: dict) -> dict:
        raw = body.get("fragment_max_messages")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="fragment_max_messages must be an integer")
        if not (FRAGMENT_MIN_MESSAGES <= value <= FRAGMENT_MAX_MESSAGES):
            raise HTTPException(
                status_code=400,
                detail=f"fragment_max_messages must be in [{FRAGMENT_MIN_MESSAGES}, {FRAGMENT_MAX_MESSAGES}]",
            )
        ctx.settings_store.set("fragment.max_messages", str(value))
        return {"ok": True, "fragment_max_messages": value}

    # -- anchor -------------------------------------------------------------

    @app.post("/api/anchor")
    async def set_anchor(body: dict) -> dict:
        from agent.graph.anchors import AnchorService

        topic_id = str(body.get("topic_id") or "").strip()
        if not topic_id:
            raise HTTPException(status_code=400, detail="topic_id required")
        node = ctx.topics.nodes.get_topic(topic_id)
        if node is None:
            raise HTTPException(status_code=404, detail="topic not found")
        fragment_id = body.get("fragment_id")
        if fragment_id:
            frag = ctx.fragments.get(fragment_id)
            if frag is None or frag.topic_id != topic_id:
                raise HTTPException(status_code=400, detail="fragment not found in topic")
        AnchorService(ctx.conn).set_active(topic_id, fragment_id or None)
        return {"ok": True, "topic_id": topic_id, "fragment_id": fragment_id}

    # -- turns -------------------------------------------------------------

    @app.post("/api/turns")
    async def start_turn(body: dict) -> dict:
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")
        topic_id = body.get("topic_id")
        task = asyncio.create_task(_safe_run_turn(ctx, message, topic_id))
        return {"ok": True, "message": message, "topic_id": topic_id, "task": task.get_name()}

    # -- graph -------------------------------------------------------------

    @app.get("/api/session/context")
    async def session_context() -> dict:
        topic_id = ctx.current_topic()
        node = ctx.topics.nodes.get_topic(topic_id)
        return {
            "topic_id": topic_id,
            "topic_name": node.name if node else topic_id,
            "anchor_fragment": ctx.anchor_fragment_info(),
            "messages": ctx.session_messages(topic_id),
        }

    @app.get("/api/graph/topics")
    async def list_topics() -> dict:
        fingerprints = ctx.topics.list_with_fingerprints()
        return {
            "topics": [
                {
                    "topic_id": f.topic_id,
                    "title": f.title,
                    "keywords": f.keywords,
                    "fragment_count": f.fragment_count,
                    "last_activity": f.last_activity,
                    "summary_preview": f.summary_preview,
                }
                for f in fingerprints
            ]
        }

    @app.get("/api/graph/topics/{topic_id}")
    async def topic_detail(topic_id: str) -> dict:
        node = ctx.topics.nodes.get_topic(topic_id)
        if node is None:
            raise HTTPException(status_code=404, detail="topic not found")
        fragments = ctx.conn.execute(
            "SELECT * FROM fragments WHERE topic_id = ? ORDER BY created_at",
            (topic_id,),
        ).fetchall()
        fragment_payloads = []
        for f in fragments:
            messages = ctx.conn.execute(
                "SELECT id, role, content, content_type, created_at FROM messages "
                "WHERE fragment_id = ? ORDER BY created_at LIMIT 50",
                (f["id"],),
            ).fetchall()
            fragment_payloads.append(
                {
                    "fragment_id": f["id"],
                    "summary": f["summary"],
                    "closed_at": f["closed_at"],
                    "message_count": len(messages),
                    "messages": [dict(m) for m in messages],
                }
            )
        entities = ctx.conn.execute(
            "SELECT n.id, n.name FROM edges e JOIN nodes n ON n.id = e.dst "
            "WHERE e.src = ? AND e.type = 'mention' ORDER BY e.weight DESC",
            (topic_id,),
        ).fetchall()
        knowledge = ctx.conn.execute(
            "SELECT id, category, state, content, confidence, updated_at FROM knowledge "
            "WHERE topic_id = ? ORDER BY updated_at DESC",
            (topic_id,),
        ).fetchall()
        return {
            "topic_id": topic_id,
            "name": node.name,
            "fragments": fragment_payloads,
            "entities": [dict(e) for e in entities],
            "knowledge": [dict(k) for k in knowledge],
        }

    @app.get("/api/graph/positions")
    async def graph_positions() -> dict:
        return {"topics": assign_positions(ctx.conn)}

    # -- knowledge management --------------------------------------------

    @app.get("/api/knowledge")
    async def list_knowledge(
        category: str | None = None,
        state: str | None = None,
        q: str | None = None,
    ) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        items = ks.list_items(category=category, state=state, q=q)
        out = []
        for it in items:
            topic_name = None
            if it.topic_id:
                node = ctx.topics.nodes.get_topic(it.topic_id)
                topic_name = node.name if node is not None else None
            out.append({
                "id": it.id,
                "category": it.category,
                "state": it.state.value,
                "content": it.content,
                "confidence": it.confidence,
                "topic_id": it.topic_id,
                "topic_name": topic_name,
                "created_at": it.created_at,
                "updated_at": it.updated_at,
            })
        return {"knowledge": out}

    @app.post("/api/knowledge")
    async def create_knowledge(body: dict) -> dict:
        from agent.knowledge.lifecycle import CATEGORIES, KnowledgeService

        category = str(body.get("category") or "").strip()
        content = str(body.get("content") or "").strip()
        if category not in CATEGORIES:
            raise HTTPException(status_code=400, detail=f"unknown category: {category}")
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        topic_id = body.get("topic_id") or None
        ks = KnowledgeService(ctx.conn)
        try:
            item = ks.create(category=category, content=content, topic_id=topic_id)
            ks.submit(item.id)
            ks.verify(item.id, verified_by="user")
            active = ks.activate(item.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        node = ctx.topics.nodes.get_topic(active.topic_id) if active.topic_id else None
        return {"ok": True, "knowledge": {
            "id": active.id, "category": active.category, "state": active.state.value,
            "content": active.content, "confidence": active.confidence,
            "topic_id": active.topic_id, "topic_name": node.name if node else None,
            "created_at": active.created_at, "updated_at": active.updated_at,
        }}

    @app.post("/api/knowledge/{knowledge_id}/verify")
    async def verify_knowledge(knowledge_id: str) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        if item.state.value != "pending_review":
            raise HTTPException(status_code=400, detail="only pending_review can be verified")
        try:
            ks.verify(knowledge_id, verified_by="user")
            active = ks.activate(knowledge_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "knowledge": {"id": active.id, "state": active.state.value}}

    @app.post("/api/knowledge/{knowledge_id}/reject")
    async def reject_knowledge(knowledge_id: str) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        if item.state.value != "pending_review":
            raise HTTPException(status_code=400, detail="only pending_review can be rejected")
        draft = ks.reject(knowledge_id)
        return {"ok": True, "knowledge": {"id": draft.id, "state": draft.state.value}}

    # -- knowledge correction ----------------------------------------------

    @app.post("/api/knowledge/{knowledge_id}/revise")
    async def revise_knowledge(knowledge_id: str, body: dict) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        content = str(body.get("content") or "").strip()
        if not content:
            raise HTTPException(status_code=400, detail="content required")
        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        new_item = ks.create(
            category=item.category,
            content=content,
            node_ids=list(item.node_ids),
            supersedes_id=item.id,
            provenance={"corrected_from": item.id},
        )
        ks.submit(new_item.id)
        ks.verify(new_item.id, verified_by="user")
        ks.activate(new_item.id)
        return {"ok": True, "knowledge_id": new_item.id, "supersedes": item.id}

    # -- entity cards ---------------------------------------------------------

    @app.get("/api/entities")
    async def list_entities() -> dict:
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(ctx.conn)
        return {"entities": [svc.to_dict(c) for c in svc.list_active()]}

    @app.get("/api/entities/{entity_id}")
    async def get_entity(entity_id: str) -> dict:
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(ctx.conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        return {"entity": svc.to_dict(card)}

    @app.post("/api/entities/{entity_id}/revise")
    async def revise_entity(entity_id: str, body: dict) -> dict:
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(ctx.conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        attributes = body.get("attributes")
        aliases = body.get("aliases")
        summary = body.get("summary")
        kind = body.get("kind")
        updated = svc.revise(
            entity_id,
            attributes=attributes,
            aliases=aliases,
            summary=summary,
            kind=kind,
        )
        return {"ok": True, "entity": svc.to_dict(updated) if updated else None}

    @app.post("/api/entities/{entity_id}/revoke")
    async def revoke_entity(entity_id: str) -> dict:
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(ctx.conn)
        if not svc.revoke(entity_id):
            raise HTTPException(status_code=404, detail="entity not found or already archived")
        return {"ok": True, "entity_id": entity_id}

    @app.post("/api/entities/{entity_id}/relations")
    async def add_entity_relation(entity_id: str, body: dict) -> dict:
        from agent.entities.cards import EntityCardService

        rel_type = str(body.get("type") or "").strip()
        target = str(body.get("target") or "").strip()
        if not rel_type or not target:
            raise HTTPException(status_code=400, detail="type and target required")
        svc = EntityCardService(ctx.conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        updated = svc.add_relation(entity_id, target, rel_type)
        return {"ok": True, "entity": svc.to_dict(updated) if updated else None}

    @app.delete("/api/entities/{entity_id}/relations")
    async def remove_entity_relation(entity_id: str, body: dict) -> dict:
        from agent.entities.cards import EntityCardService

        rel_type = str(body.get("type") or "").strip()
        target = str(body.get("target") or "").strip()
        if not rel_type or not target:
            raise HTTPException(status_code=400, detail="type and target required")
        svc = EntityCardService(ctx.conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        updated = svc.remove_relation(entity_id, target, rel_type)
        return {"ok": True, "entity": svc.to_dict(updated) if updated else None}

    @app.post("/api/knowledge/{knowledge_id}/revoke")
    async def revoke_knowledge(knowledge_id: str) -> dict:
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        ks.revoke(item.id)
        return {"ok": True, "knowledge_id": item.id}

    # -- maintenance -------------------------------------------------------

    @app.post("/api/maintenance/run")
    async def run_maintenance() -> dict:
        task = asyncio.create_task(ctx.maintenance.run_once())
        return {"ok": True, "started": True, "task": task.get_name()}

    @app.get("/api/settings/maintenance")
    async def get_maintenance_settings() -> dict:
        return {
            "enabled": ctx.settings_store.get("maintenance.enabled", "true") != "false",
            "interval_hours": ctx.settings_store.get_int("maintenance.interval_hours", 24),
        }

    @app.put("/api/settings/maintenance")
    async def update_maintenance_settings(body: dict) -> dict:
        if "enabled" in body:
            ctx.settings_store.set("maintenance.enabled", "true" if body["enabled"] else "false")
        if "interval_hours" in body:
            raw = body["interval_hours"]
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="interval_hours must be an integer")
            if not (1 <= value <= 24 * 30):
                raise HTTPException(status_code=400, detail="interval_hours must be in [1, 720]")
            ctx.settings_store.set("maintenance.interval_hours", str(value))
        return await get_maintenance_settings()

    # -- approvals ---------------------------------------------------------

    @app.post("/api/approvals/{approval_id}/respond")
    async def respond_approval(approval_id: str, body: dict) -> dict:
        decision = body.get("decision", "")
        scope = body.get("scope")
        overrides = body.get("overrides")
        try:
            handled = await approvals.respond(approval_id, decision, scope, overrides)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not handled:
            raise HTTPException(status_code=404, detail="approval not found or already answered")
        return {"ok": True, "approval_id": approval_id, "decision": decision}

    # -- test-only event publish ------------------------------------------

    @app.post("/api/events/test")
    async def publish_test(event_type: str, data: dict | None = None) -> dict:
        event = make_event(EventType(event_type), data or {})
        await bus.publish(event)
        return {"id": event.id, "type": event.type.value}

    app.state.bus = bus
    app.state.approvals = approvals
    app.state.ctx = ctx
    try:
        ctx.maintenance.start()
    except RuntimeError:
        pass  # 不在事件循环内（如测试构造）时由手动 API 触发
    return app


async def _safe_run_turn(ctx: AppContext, message: str, topic_id: str | None) -> None:
    try:
        await ctx.run_turn(message, topic_id)
    except Exception as exc:  # noqa: BLE001 - background task boundary
        from agent.api.events import EventType, make_event

        await ctx.bus.publish(
            make_event(
                EventType.ERROR,
                {"code": "turn_task", "message": str(exc)[:200], "recoverable": True},
            )
        )