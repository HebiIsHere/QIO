"""FastAPI application: health, SSE, credentials, turns, graph, approvals.

安全边界（本机 API）：

* 所有 `/api/*`（health 除外）都要求 QIO 会话令牌（Bearer / X-QIO-Session）；
  令牌由桌面壳生成，只活在这个进程里，不持久化、不进日志。
* CORS 只信任 QIO 自己的 WebView origin（开发模式额外允许本机 dev server）。
* Host 必须是回环地址（防 DNS rebinding）。
* SSE 用一次性、短 TTL、scope=events 的 ticket 认证，主令牌不进 URL。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import uuid
from collections import deque
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI

from agent.api.auth import TICKET_SCOPE_EVENTS, SessionAuth
from agent.api.bus import EventBus
from agent.api.events import AgentEvent, EventType, make_event
from agent.adapters.probe import probe_adapter
from agent.config import Settings
from agent.graph.layout import assign_positions
from agent.services.app import AppContext
from agent.services.planet import VISIBLE_CAPACITY, PlanetBrowseService

FRAGMENT_MIN_MESSAGES = 1
FRAGMENT_MAX_MESSAGES = 30
DEFAULT_FRAGMENT_MAX_MESSAGES = 10

# 开发模式的 CORS 兜底：本机 dev server 任意端口。
DEV_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


def _knowledge_payload(ctx, item) -> dict:
    """知识条目的结构化载荷（列表与新建共用）。"""
    topic_name = None
    if item.topic_id:
        node = ctx.topics.nodes.get_topic(item.topic_id)
        topic_name = node.name if node is not None else None
    return {
        "id": item.id,
        "category": item.category,
        "state": item.state.value,
        "content": item.content,
        "confidence": item.confidence,
        "topic_id": item.topic_id,
        "topic_name": topic_name,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def create_app(settings: Settings, conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI(title="QIO", version="0.1.0")
    auth = SessionAuth.from_settings(settings)
    instance_id = f"qio_{uuid.uuid4().hex[:16]}"
    app.add_middleware(
        CORSMiddleware,
        # 只信任 QIO 自己的 WebView origin；开发模式额外允许本机 dev server。
        allow_origins=list(settings.allowed_origins),
        allow_origin_regex=DEV_ORIGIN_REGEX if settings.dev_insecure else None,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-QIO-Session"],
    )
    bus = EventBus()
    ctx = AppContext(settings, conn, bus)
    from agent.tools.approval import ApprovalService

    approvals = ctx.approvals

    @app.middleware("http")
    async def session_guard(request: Request, call_next):
        """本机 API 的应用级身份认证（在 CORS 之前拦截）。"""
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        headers = {k.lower(): v for k, v in request.headers.items()}
        # ticket 只对 SSE 入口有效，且是一次性的
        ticket = request.query_params.get("ticket") if path == "/api/events" else None
        allowed, reason = auth.check_request(
            path=path, method=request.method, headers=headers, ticket=ticket
        )
        if not allowed:
            status = 403 if reason in ("origin_rejected", "host_not_loopback") else 401
            return JSONResponse({"detail": "unauthorized", "reason": reason}, status_code=status)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "db": conn.execute("SELECT 1").fetchone()[0] == 1}

    @app.get("/api/instance")
    async def instance_info() -> dict:
        """backend 身份确认：前端/壳用它验证连上的是本实例，而不是别的进程。"""
        return {
            "instance_id": instance_id,
            "pid": os.getpid(),
            "auth_required": auth.enabled,
            "version": app.version,
        }

    @app.post("/api/events/ticket")
    async def create_events_ticket() -> dict:
        """EventSource 无法带 header：换一张一次性、短生命周期、scope=events 的票。"""
        if not auth.enabled:
            return {"ticket": "", "expires_in": 0, "auth_required": False}
        return {
            "ticket": auth.issue_ticket(TICKET_SCOPE_EVENTS),
            "expires_in": auth.ticket_ttl,
            "auth_required": True,
        }

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        header_cursor = request.headers.get("last-event-id")
        cursor = header_cursor or request.query_params.get("last_event_id") or None
        return StreamingResponse(
            bus.stream(cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

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

    def _credential_payload(meta: dict) -> dict:
        payload = dict(meta)
        payload["key_id"] = payload.pop("id")
        return payload

    @app.patch("/api/credentials/{key_id}")
    async def update_credential_meta(key_id: str, body: dict) -> dict:
        kwargs: dict = {}
        if "tags" in body:
            kwargs["tags"] = body["tags"]
        if "default_model" in body:
            kwargs["default_model"] = body["default_model"]
        if "budget" in body:
            kwargs["budget"] = body["budget"]
        if "note" in body:
            kwargs["note"] = body["note"]

        # endpoint 属于凭据的安全身份，不是普通元数据：改了 endpoint 就等于换了
        # 服务提供方，绝不能继续复用 keyring 里的旧 Key 静默发出去。
        # 必须重新输入 secret（rotation）+ 显式确认重新配置。
        endpoint_changed = False
        if "endpoint" in body:
            new_endpoint = str(body.get("endpoint") or "").strip()
            current = ctx.credentials.get_metadata(key_id)
            if current is None:
                raise HTTPException(status_code=404, detail="credential not found")
            if new_endpoint != (current.get("endpoint") or ""):
                from agent.credentials.store import validate_endpoint

                try:
                    validate_endpoint(new_endpoint)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                secret = str(body.get("secret") or "").strip()
                if not secret or not body.get("confirm_reconfigure"):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "changing endpoint is a credential reconfiguration: "
                            "re-enter the secret and pass confirm_reconfigure=true"
                        ),
                    )
                endpoint_changed = True
                ctx.credentials.update_secret(key_id, secret)
                kwargs["endpoint"] = new_endpoint
        try:
            meta = ctx.credentials.update_metadata(
                key_id,
                **kwargs,
                secret=body.get("secret") if endpoint_changed else None,
                confirm_reconfigure=bool(endpoint_changed),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "credential": _credential_payload(meta)}

    @app.post("/api/credentials/{key_id}/enable")
    async def enable_credential(key_id: str) -> dict:
        try:
            meta = ctx.credentials.set_enabled(key_id, True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await bus.publish(
            make_event(EventType.CREDENTIAL_STATUS, {"key_id": key_id, "status": "active"})
        )
        return {"ok": True, "credential": _credential_payload(meta)}

    @app.post("/api/credentials/{key_id}/disable")
    async def disable_credential(key_id: str) -> dict:
        try:
            meta = ctx.credentials.set_enabled(key_id, False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await bus.publish(
            make_event(EventType.CREDENTIAL_STATUS, {"key_id": key_id, "status": "paused"})
        )
        return {"ok": True, "credential": _credential_payload(meta)}

    @app.get("/api/credentials/{key_id}/audit")
    async def credential_audit(key_id: str) -> dict:
        logs = ctx.credentials.audit_log(key_id)
        return {"ok": True, "audit": logs}

    @app.delete("/api/credentials/{key_id}")
    async def delete_credential(key_id: str) -> dict:
        try:
            ctx.credentials.delete(key_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await bus.publish(
            make_event(EventType.CREDENTIAL_STATUS, {"key_id": key_id, "status": "deleted"})
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

    # -- UI 偏好：打字机输出速度（三档：25 / 50 / 75 字符每秒） ----------------

    TYPEWRITER_SPEEDS = (25, 50, 75)
    DEFAULT_TYPEWRITER_CPS = 50

    @app.get("/api/settings/ui")
    async def get_ui_settings() -> dict:
        return {
            "typewriter_cps": ctx.settings_store.get_int(
                "ui.typewriter_cps", DEFAULT_TYPEWRITER_CPS
            )
        }

    @app.put("/api/settings/ui")
    async def update_ui_settings(body: dict) -> dict:
        raw = body.get("typewriter_cps")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="typewriter_cps must be an integer")
        if value not in TYPEWRITER_SPEEDS:
            raise HTTPException(
                status_code=400,
                detail=f"typewriter_cps must be one of {list(TYPEWRITER_SPEEDS)}",
            )
        ctx.settings_store.set("ui.typewriter_cps", str(value))
        return {"ok": True, "typewriter_cps": value}

    # -- 对话深度：迭代上限 / 输出 token 预算 -------------------------------

    LOOP_MAX_ITERATIONS_LIMIT = 1000
    LOOP_DEFAULT_ITERATIONS = 128
    LOOP_DEFAULT_OUTPUT_TOKENS = 51200

    @app.get("/api/settings/loop")
    async def get_loop_settings() -> dict:
        store = ctx.settings_store
        return {
            "max_iterations": store.get_int("loop.max_iterations", LOOP_DEFAULT_ITERATIONS),
            "output_token_budget": store.get_int(
                "loop.output_token_budget", LOOP_DEFAULT_OUTPUT_TOKENS
            ),
        }

    @app.put("/api/settings/loop")
    async def update_loop_settings(body: dict) -> dict:
        store = ctx.settings_store
        if "max_iterations" in body:
            try:
                v = int(body["max_iterations"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="max_iterations must be an integer")
            if not (1 <= v <= LOOP_MAX_ITERATIONS_LIMIT):
                raise HTTPException(
                    status_code=400,
                    detail=f"max_iterations must be in [1, {LOOP_MAX_ITERATIONS_LIMIT}]",
                )
            store.set("loop.max_iterations", str(v))
        if "output_token_budget" in body:
            try:
                v = int(body["output_token_budget"])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="output_token_budget must be an integer"
                )
            if v < 0:
                raise HTTPException(
                    status_code=400, detail="output_token_budget must be >= 0"
                )
            store.set("loop.output_token_budget", str(v))
        return await get_loop_settings()

    @app.get("/api/settings/search")
    async def get_search_settings() -> dict:
        store = ctx.settings_store
        bocha_key = store.get("search.bocha_api_key", "") or ""
        return {
            "searxng_url": store.get("search.searxng_url", "") or "",
            "bocha_has_key": bool(bocha_key),
            # 免密钥通道（Exa / Parallel 免费 MCP + DuckDuckGo HTML）默认开启
            "keyless_fallback": store.get_bool("search.keyless_fallback", True),
            "top_k_default": store.get_int("search.top_k_default", 5),
            "max_fetch_chars": store.get_int("search.max_fetch_chars", 15000),
        }

    @app.put("/api/settings/search")
    async def update_search_settings(body: dict) -> dict:
        store = ctx.settings_store
        if "searxng_url" in body:
            store.set("search.searxng_url", str(body.get("searxng_url") or ""))
        if "bocha_api_key" in body:
            store.set("search.bocha_api_key", str(body.get("bocha_api_key") or ""))
        if "keyless_fallback" in body:
            store.set("search.keyless_fallback", "1" if body.get("keyless_fallback") else "0")
        if "top_k_default" in body:
            try:
                v = int(body["top_k_default"])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="top_k_default must be an integer"
                )
            store.set("search.top_k_default", str(max(1, min(v, 20))))
        if "max_fetch_chars" in body:
            try:
                v = int(body["max_fetch_chars"])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="max_fetch_chars must be an integer",
                )
            store.set("search.max_fetch_chars", str(max(1000, min(v, 40000))))
        # 保存即生效：把设置套用到运行中的 SearchService（改 SearXNG/博查/免密钥开关
        # 不需要重启后端）
        ctx.apply_search_settings()
        return await get_search_settings()

    # -- computer control settings -----------------------------------------

    PERMISSION_MODES = ("default", "plan", "accept-edits", "bypass")

    @app.get("/api/settings/computer")
    async def get_computer_settings() -> dict:
        store = ctx.settings_store
        mode = store.get("computer.permission_mode", "default") or "default"
        return {
            "root_dir": store.get("computer.root_dir", "") or "",
            "permission_mode": mode if mode in PERMISSION_MODES else "default",
        }

    @app.put("/api/settings/computer")
    async def update_computer_settings(body: dict) -> dict:
        store = ctx.settings_store
        if "root_dir" in body:
            store.set("computer.root_dir", str(body.get("root_dir") or ""))
        if "permission_mode" in body:
            mode = str(body["permission_mode"])
            if mode not in PERMISSION_MODES:
                raise HTTPException(status_code=400, detail="invalid permission_mode")
            store.set("computer.permission_mode", mode)
        return await get_computer_settings()

    # -- anchor -------------------------------------------------------------

    @app.post("/api/anchor")
    async def set_anchor(body: dict) -> dict:
        from agent.services.navigation import FragmentNotInTopic, TopicNotFound

        topic_id = str(body.get("topic_id") or "").strip()
        if not topic_id:
            raise HTTPException(status_code=400, detail="topic_id required")
        fragment_id = body.get("fragment_id")
        # 两个明确不同的动作：进入话题（最新位置）与从历史继续（新建接续片段）。
        # 具体规则都在 TopicNavigationService 里，路由不自己写 anchor。
        try:
            if body.get("continue_from_history"):
                if not fragment_id:
                    raise HTTPException(status_code=400, detail="fragment_id required to continue")
                result = ctx.navigation.continue_from_history(topic_id, str(fragment_id))
            else:
                # 用户明确点「进入这个话题」＝从最新位置继续，不恢复旧位置。
                result = ctx.navigation.enter_topic(
                    topic_id,
                    fragment_id=str(fragment_id) if fragment_id else None,
                    restore_position=False,
                )
        except TopicNotFound:
            raise HTTPException(status_code=404, detail="topic not found") from None
        except FragmentNotInTopic:
            raise HTTPException(status_code=400, detail="fragment not found in topic") from None
        # 广播权威锚点：标题 / historic 只能有一个来源（后端），
        # 前端不用摘要自己拼标题，也不会在位置推进后继续显示旧提示。
        await ctx._publish_anchor_event()
        return {
            "ok": True,
            "topic_id": result.topic_id,
            "fragment_id": result.fragment_id,
            "fragment_title": result.fragment_title,
            "historic": result.historic,
            "created_fragment_id": result.created_fragment_id,
            "source_fragment_id": result.source_fragment_id,
        }

    # -- turns -------------------------------------------------------------

    @app.post("/api/turns")
    async def start_turn(body: dict) -> dict:
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")
        topic_id = body.get("topic_id")
        # 受理时就已经有 turn_id：前端可以立刻用它做乐观消息关联与取消，
        # 不必等 SSE 的 TURN_START（SSE 是异步状态通道，不承担请求身份）。
        turn = ctx.turns.submit(message, topic_id)
        return {
            "ok": True,
            "accepted": True,
            "turn_id": turn.turn_id,
            "status": turn.status,
            "message": message,
            "topic_id": topic_id,
        }

    @app.post("/api/turns/cancel")
    async def cancel_turn() -> dict:
        """取消当前 active 主 turn（single-flight：唯一目标）。"""
        cancelled = ctx.turns.cancel_active()
        active = ctx.turns.active
        return {
            "ok": cancelled,
            "cancelled": cancelled,
            "turn_id": active.turn_id if active is not None else None,
        }

    @app.post("/api/turns/{turn_id}/cancel")
    async def cancel_turn_by_id(turn_id: str) -> dict:
        """按 turn_id 取消 —— 运行中或仍在排队中的都可。"""
        ok = ctx.turns.cancel(turn_id)
        return {"ok": ok, "cancelled": ok, "turn_id": turn_id}

    # -- agent trace (read-only debug) -------------------------------------

    @app.get("/api/traces")
    async def list_traces(limit: int = 50, offset: int = 0) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        return {
            "traces": ctx.trace_store.list(limit=limit, offset=offset),
            "total": ctx.trace_store.count(),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/traces/{turn_id}")
    async def get_trace(turn_id: str) -> dict:
        trace = ctx.trace_store.get(turn_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return trace

    @app.get("/api/settings/trace")
    async def get_trace_settings() -> dict:
        return {"enabled": ctx.trace_store.enabled}

    @app.put("/api/settings/trace")
    async def update_trace_settings(body: dict) -> dict:
        enabled = bool(body.get("enabled", True))
        ctx.settings_store.set("trace.enabled", "true" if enabled else "false")
        ctx.trace_store.enabled = enabled
        return {"enabled": enabled}

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
        # 第二层（Topic Detail）：只给目录与计数，不内联任何 Message 原文。
        # 原文属于第三层，由 /api/fragments/{id}/messages 按需分页读取。
        fragment_rows = ctx.conn.execute(
            "SELECT f.id AS fragment_id, f.summary, f.closed_at, f.created_at, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.fragment_id = f.id) AS message_count "
            "FROM fragments f WHERE f.topic_id = ? ORDER BY f.created_at",
            (topic_id,),
        ).fetchall()
        fragment_payloads = [
            {
                "fragment_id": row["fragment_id"],
                "summary": row["summary"],
                "closed_at": row["closed_at"],
                "created_at": row["created_at"],
                "message_count": int(row["message_count"] or 0),
            }
            for row in fragment_rows
        ]
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

    # -- planet（长期话题的浏览景观） --------------------------------------
    #
    # 三层接口的第一层与第三层：
    #   GET  /api/planet/overview          有哪些话题可以展示（轻量，无原文）
    #   POST /api/planet/browse            接下来该展示哪一批（游标可前进可后退）
    #   GET  /api/fragments/{id}/messages  真正展开某段历史时才按页取原文
    # 星球不返回「所有话题 × 所有片段 × 所有消息」，也不在前端洗牌。

    @app.get("/api/planet/overview")
    async def planet_overview() -> dict:
        topics = ctx.planet.overview()
        return {
            "topics": [
                {
                    "topic_id": t.topic_id,
                    "title": t.title,
                    "fragment_count": t.fragment_count,
                    "last_activity": t.last_activity,
                    "summary_preview": t.summary_preview,
                    "visual_seed": t.visual_seed,
                }
                for t in topics
            ],
            "total": len(topics),
            "visible_capacity": VISIBLE_CAPACITY,
        }

    @app.post("/api/planet/browse")
    async def planet_browse(body: dict) -> dict:
        direction = str(body.get("direction") or "forward")
        if direction not in ("forward", "backward"):
            raise HTTPException(status_code=400, detail="invalid direction")
        raw_count = body.get("count", VISIBLE_CAPACITY)
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid count") from None
        exclude = body.get("exclude") or []
        if not isinstance(exclude, list):
            raise HTTPException(status_code=400, detail="invalid exclude")
        cursor = body.get("cursor")
        raw_seed = body.get("seed")
        try:
            seed = int(raw_seed) if raw_seed is not None else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid seed") from None
        return PlanetBrowseService(ctx.conn).browse(
            cursor=str(cursor) if cursor else None,
            direction=direction,
            count=count,
            exclude=[str(item) for item in exclude],
            current_topic_id=body.get("current_topic_id") or None,
            seed=seed,
        )

    @app.get("/api/fragments/{fragment_id}/messages")
    async def fragment_messages(fragment_id: str, offset: int = 0, limit: int = 50) -> dict:
        fragment = ctx.fragments.get(fragment_id)
        if fragment is None:
            raise HTTPException(status_code=404, detail="fragment not found")
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        rows = ctx.conn.execute(
            "SELECT id, role, content, content_type, created_at FROM messages "
            "WHERE fragment_id = ? ORDER BY created_at, id LIMIT ? OFFSET ?",
            (fragment_id, limit, offset),
        ).fetchall()
        return {
            "fragment_id": fragment_id,
            "topic_id": fragment.topic_id,
            "messages": [dict(row) for row in rows],
            "total": ctx.fragments.message_count(fragment_id),
            "offset": offset,
            "limit": limit,
        }

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
            out.append(_knowledge_payload(ctx, it))
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
        return {"ok": True, "knowledge": _knowledge_payload(ctx, active)}

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
    # 只在开发模式注册（生产构建里这条路由根本不存在），且同样要求会话认证。
    if settings.dev_insecure or settings.test_events:

        @app.post("/api/events/test")
        async def publish_test(event_type: str, data: dict | None = None) -> dict:
            event = make_event(EventType(event_type), data or {})
            await bus.publish(event)
            return {"id": event.id, "type": event.type.value}

    app.state.bus = bus
    app.state.auth = auth
    app.state.instance_id = instance_id
    app.state.approvals = approvals
    app.state.ctx = ctx
    try:
        ctx.maintenance.start()
    except RuntimeError:
        pass  # 不在事件循环内（如测试构造）时由手动 API 触发
    return app
