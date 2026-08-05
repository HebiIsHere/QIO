"""FastAPI application: health, SSE, credentials, turns, graph, approvals."""

from __future__ import annotations

import asyncio
import json
import sqlite3
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


def create_app(settings: Settings, conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI(title="smart-agent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local desktop app: frontend dev origin
        allow_methods=["*"],
        allow_headers=["*"],
    )
    bus = EventBus()
    ctx = AppContext(settings, conn, bus)
    from agent.tools.approval import ApprovalService

    approvals = ApprovalService(bus)

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
        try:
            version = ctx.credentials.create(
                key_id=body["key_id"],
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
                {"key_id": body["key_id"], "status": "active", "version": version},
            )
        )
        return {"ok": True, "key_id": body["key_id"], "version": version}

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

    # -- approvals ---------------------------------------------------------

    @app.post("/api/approvals/{approval_id}/respond")
    async def respond_approval(approval_id: str, body: dict) -> dict:
        decision = body.get("decision", "")
        scope = body.get("scope")
        try:
            handled = await approvals.respond(approval_id, decision, scope)
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