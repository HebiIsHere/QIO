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
import logging
import os
import secrets
import sqlite3
import uuid
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
# 记忆封块设置的键名、范围与旧键迁移：设置读写与运行时（turn_orchestrator）
# 共用同一处解析，避免两套语义漂移。
from agent.memory.fragment import (
    FRAGMENT_MAX_TURNS,
    FRAGMENT_MIN_TURNS,
    FRAGMENT_TURNS_KEY,
    resolve_max_turns,
)
from agent.services.app import SESSION_PAGE_DEFAULT_LIMIT, AppContext
from agent.services.planet import VISIBLE_CAPACITY, PlanetBrowseService

# 开发模式的 CORS 兜底：本机 dev server 任意端口。
DEV_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

# 摘要只给「一句」：详情层要的是看得懂，不是把整段摘要摊开
_SENTENCE_END = "。！？!?\n"


def _first_sentence(text: str | None) -> str | None:
    """取摘要首句；没有内容就不返回，绝不编造。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    for idx, char in enumerate(cleaned):
        if char in _SENTENCE_END:
            return cleaned[: idx + 1].strip()
    return cleaned


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


def create_app(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    close_db_on_shutdown: bool = False,
) -> FastAPI:
    bus = EventBus()
    ctx = AppContext(settings, conn, bus)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """应用生命周期：启动后台维护，关闭时按顺序收尾。

        startup 必须有 running loop 才可能真正启动后台任务 —— 所以维护调度
        放在这里，而不是 `create_app()` 那种同步构造阶段（旧代码在那里
        `try: start() except RuntimeError: pass`，等于**从未启动**还不出声）。

        shutdown 顺序：停维护 → 停 Turn → 停 TaskManager → 关 adapter/HTTP
        → （真实入口才）关 DB。DB 放在最后，避免后台任务还在写时连接先断了。
        """
        ctx.maintenance.start()
        # 阶段 2：进程重启后把「卡在 running」的派生任务放回可重试状态，
        # 并把上次没做完的补齐（幂等，不重放任何外部副作用）。
        try:
            from agent.services import derived_tasks

            recovered = derived_tasks.recover_stale(ctx.conn)
            if recovered:
                logging.getLogger(__name__).info("recovered %s stale derived tasks", recovered)
        except Exception:  # noqa: BLE001 - 恢复失败不该让应用起不来
            logging.getLogger(__name__).warning("derived task recovery failed", exc_info=True)
        try:
            yield
        finally:
            try:
                await ctx.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不能阻止退出
                logging.getLogger(__name__).warning("app context close failed", exc_info=True)
            if close_db_on_shutdown:
                try:
                    from agent.storage.db import close as close_conn

                    close_conn(conn)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).warning("closing db failed", exc_info=True)

    app = FastAPI(title="QIO", version="0.1.6", lifespan=lifespan)
    auth = SessionAuth.from_settings(settings)
    instance_id = f"qio_{uuid.uuid4().hex[:16]}"
    # 事件要能自证「来自哪个后端实例」：进程重启后 revision 从 0 重新计数，
    # 前端据此知道旧基准作废、要完整 resync（见 /api/runtime/state）。
    ctx.instance_id = instance_id
    ctx.turns.instance_id = instance_id
    app.add_middleware(
        CORSMiddleware,
        # 只信任 QIO 自己的 WebView origin；开发模式额外允许本机 dev server。
        allow_origins=list(settings.allowed_origins),
        allow_origin_regex=DEV_ORIGIN_REGEX if settings.dev_insecure else None,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-QIO-Session"],
    )
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
        """一次请求 = 一次完整的凭据重配置。

        endpoint 属于凭据的安全身份：改 endpoint 就等于换了服务提供方，
        必须重新输入 secret 并显式确认（规则在 store 里强制，服务端说了算）。

        这里刻意**只调用一次** `reconfigure`，而不是「先 update_secret 再 update_metadata」：
        后者在两步之间失败会留下「secret 已换、endpoint 还是旧的」这种混合状态。
        """
        kwargs: dict = {}
        if "tags" in body:
            kwargs["tags"] = body["tags"]
        if "default_model" in body:
            kwargs["default_model"] = body["default_model"]
        if "budget" in body:
            kwargs["budget"] = body["budget"]
        if "note" in body:
            kwargs["note"] = body["note"]
        if "endpoint" in body:
            kwargs["endpoint"] = body.get("endpoint")
        secret = str(body.get("secret") or "").strip() or None
        try:
            meta = ctx.credentials.reconfigure(
                key_id,
                **kwargs,
                secret=secret,
                confirm_reconfigure=bool(body.get("confirm_reconfigure")),
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
        from agent.memory.fragment import resolve_max_tokens

        return {
            "fragment_max_turns": resolve_max_turns(ctx.settings_store),
            # 阶段 4：长度是兜底手段（到点分块，但不表示任务完成），
            # 设置页要能看见并调整它，界面文案见前端。
            "fragment_max_tokens": resolve_max_tokens(ctx.settings_store),
        }

    @app.put("/api/settings/memory")
    async def update_memory_settings(body: dict) -> dict:
        from agent.memory.fragment import FRAGMENT_TOKENS_KEY, resolve_max_tokens

        if "fragment_max_tokens" in body:
            try:
                tokens = int(body["fragment_max_tokens"])
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="fragment_max_tokens must be an integer"
                ) from None
            if not (2_000 <= tokens <= 200_000):
                raise HTTPException(
                    status_code=400,
                    detail="fragment_max_tokens must be in [2000, 200000]",
                )
            ctx.settings_store.set(FRAGMENT_TOKENS_KEY, str(tokens))
            # 只改长度时不强制要求同时给轮数
            if "fragment_max_turns" not in body:
                return {
                    "ok": True,
                    "fragment_max_tokens": resolve_max_tokens(ctx.settings_store),
                    "fragment_max_turns": resolve_max_turns(ctx.settings_store),
                }
        raw = body.get("fragment_max_turns")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="fragment_max_turns must be an integer")
        if not (FRAGMENT_MIN_TURNS <= value <= FRAGMENT_MAX_TURNS):
            raise HTTPException(
                status_code=400,
                detail=f"fragment_max_turns must be in [{FRAGMENT_MIN_TURNS}, {FRAGMENT_MAX_TURNS}]",
            )
        ctx.settings_store.set(FRAGMENT_TURNS_KEY, str(value))
        return {
            "ok": True,
            "fragment_max_turns": value,
            "fragment_max_tokens": resolve_max_tokens(ctx.settings_store),
        }

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
                # 阶段 1：点击历史**只登记接续意图**，不创建片段。
                # 界面显示「将从所选记录继续」；真正的新片段在本轮消息执行时落实。
                intent = ctx.navigation.register_continuation(
                    topic_id, str(fragment_id), request_id=body.get("request_id")
                )
                if intent.get("opens_current"):
                    result = ctx.navigation.enter_topic(
                        topic_id, fragment_id=str(fragment_id), relate=False
                    )
                else:
                    # 位置停在所选历史处（historic 提示由 Anchor 事件广播）
                    result = ctx.navigation.enter_topic(
                        topic_id, fragment_id=str(fragment_id), relate=False
                    )
                await ctx._publish_anchor_event()
                return {
                    "ok": True,
                    "topic_id": result.topic_id,
                    "fragment_id": result.fragment_id,
                    "fragment_title": result.fragment_title,
                    "historic": result.historic,
                    "created_fragment_id": None,
                    "source_fragment_id": intent.get("source_fragment_id") or str(fragment_id),
                    "intent_id": intent.get("intent_id"),
                    "intent_version": intent.get("intent_version"),
                    "pending": bool(intent.get("intent_id")),
                }
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

    @app.post("/api/anchor/continue/cancel")
    async def cancel_continuation() -> dict:
        """取消「下一条消息从某段历史继续」的登记。

        阶段 1 的两步语义：点击历史只登记意图，真正的新片段要等消息执行时才落实。
        所以用户在发送前取消是零成本的：不发消息就不留痕迹（不产生空片段）。
        """
        pending = ctx.bindings.peek_intent()
        if pending is None:
            return {"ok": True, "cancelled": False}
        ctx.bindings.cancel_intent(pending.intent_id)
        await ctx._publish_anchor_event()
        return {"ok": True, "cancelled": True}

    @app.post("/api/turns")
    async def start_turn(body: dict) -> dict:
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message required")
        topic_id = body.get("topic_id")
        # 受理时就已经有 turn_id：前端可以立刻用它做乐观消息关联与取消，
        # 不必等 SSE 的 TURN_START（SSE 是异步状态通道，不承担请求身份）。
        # 提交这一刻捕获待落实的接续选择：之后再选别的，只影响后续提交
        # （排队中的这条消息不被追溯改向）。
        pending = ctx.bindings.peek_intent()
        turn = ctx.turns.submit(
            message, topic_id, intent_id=pending.intent_id if pending else None
        )
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

    @app.get("/api/turns/queue")
    async def turn_queue() -> dict:
        """权威的队列快照（运行中 + 排队中 + revision）。

        SSE 事件流是「增量」；一旦客户端察觉到事件流可能不完整
        （收到 RESYNC），就用这个接口重新取一次权威状态，而不是猜。
        与 SSE 的 TURN_QUEUE 同源（同一个 snapshot()），所以两者可以互相校正。
        """
        return ctx.turns.snapshot()

    @app.get("/api/runtime/state")
    async def runtime_state() -> dict:
        """RESYNC 之后要恢复的**全部**权威状态（Turn 队列之外还有别的）。

        事件流只能表达增量。任何「丢一次事件就会让界面永久停在错误状态」的东西，
        都必须能从服务端重新查出来：

        * `turn_queue`：运行中 / 排队的 Turn（含 revision，供前端做新旧比较）；
        * `approvals`：仍在等待用户决定的审批（断线错过的 APPROVAL_REQUIRED）；
        * `tasks`：仍在跑 / 仍在排队的独立任务（断线错过的 SUBAGENT_STATUS）。

        `instance_id` 与后端实例绑定：后端重启后 revision 会从头计数，
        前端据此判断「基准已经换了」，而不是把新实例的低 revision 当成旧状态。
        """
        return {
            "instance_id": instance_id,
            "revision": ctx.turns.snapshot()["revision"],
            "turn_queue": ctx.turns.snapshot(),
            "approvals": ctx.approvals.pending(),
            "tasks": ctx.task_manager.snapshot(),
            # 工具执行的权威事实（活工具 + 最近结束的工具）：
            # TOOL_END 可能丢在失真区间里，但终态本身是服务器已经知道的事实，
            # 客户端据此把「运行中」的卡片恢复成真实的 success / failed / cancelled。
            # 只有服务器也确认不了（记录已回收 / 进程重启）时才轮到 unknown。
            "tools": ctx.tool_executions(),
            # 本轮已输出的执行叙事（模型文案 + 系统生成的调用摘要）：
            # 断线期间丢失的叙事在这里补齐，客户端按 narrative_id 去重。
            "narratives": ctx.active_turn_narratives(),
        }

    @app.post("/api/turns/{turn_id}/cancel")
    async def cancel_turn_by_id(turn_id: str) -> dict:
        """按 turn_id 取消 —— 运行中或仍在排队中的都可。"""
        ok = ctx.turns.cancel(turn_id)
        return {"ok": ok, "cancelled": ok, "turn_id": turn_id}

    # -- topic switch（待确认切换） ----------------------------------------
    #
    # Predictor 可以提建议，但不能自行移动用户：真正的切换由这两个动作决定。

    @app.post("/api/topic-switch/confirm")
    async def confirm_topic_switch() -> dict:
        result = ctx.navigation.confirm_switch()
        if result is None:
            return {"ok": False, "topic_id": None}
        await ctx._publish_anchor_event()
        return {
            "ok": True,
            "topic_id": result.topic_id,
            "fragment_id": result.fragment_id,
            "fragment_title": result.fragment_title,
            "historic": result.historic,
        }

    @app.post("/api/topic-switch/reject")
    async def reject_topic_switch() -> dict:
        ctx.navigation.reject_switch()
        return {"ok": True}

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
    async def session_context(limit: int | None = None) -> dict:
        topic_id = ctx.current_topic()
        node = ctx.topics.nodes.get_topic(topic_id)
        # 首次只给最近一页（默认 200 条）：不再随历史长度线性增长
        page = ctx.session_messages_page(topic_id, limit=limit or SESSION_PAGE_DEFAULT_LIMIT)
        return {
            "topic_id": topic_id,
            "topic_name": node.name if node else topic_id,
            "anchor_fragment": ctx.anchor_fragment_info(),
            "messages": page["messages"],
            "has_more": page["has_more"],
            "next_before": page["next_before"],
        }

    @app.get("/api/session/messages")
    async def session_messages_before(
        topic_id: str | None = None, before: str | None = None, limit: int | None = None
    ) -> dict:
        """更早的一页历史（用户向上读时按需加载）。"""
        target = topic_id or ctx.current_topic()
        page = ctx.session_messages_page(
            target, limit=limit or SESSION_PAGE_DEFAULT_LIMIT, before=before
        )
        return {"topic_id": target, **page}

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
        # 目录层的补充信息：一句摘要 / 关键词 / 最近活动 / 真实消息数。
        # 全部来自已有数据，取不到就是 None 或空，不编造、不内联原文。
        latest = ctx.conn.execute(
            "SELECT summary FROM fragments WHERE topic_id = ? "
            "AND summary IS NOT NULL AND summary <> '' "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        keyword_counter: Counter[str] = Counter()
        for row in ctx.conn.execute(
            "SELECT keywords FROM memory_index WHERE topic_id = ? ORDER BY created_at DESC",
            (topic_id,),
        ).fetchall():
            try:
                indexed = json.loads(row["keywords"] or "[]")
            except ValueError:
                continue
            if isinstance(indexed, list):
                keyword_counter.update(
                    str(item).strip() for item in indexed if str(item).strip()
                )
        activity = ctx.conn.execute(
            "SELECT MAX(m.created_at) AS last_activity, COUNT(*) AS message_count "
            "FROM messages m JOIN fragments f ON f.id = m.fragment_id "
            "WHERE f.topic_id = ?",
            (topic_id,),
        ).fetchone()
        return {
            "topic_id": topic_id,
            "name": node.name,
            "summary": _first_sentence(latest["summary"]) if latest else None,
            "keywords": [kw for kw, _ in keyword_counter.most_common(12)],
            "last_activity": activity["last_activity"] if activity else None,
            "message_count": int(activity["message_count"] or 0) if activity else 0,
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

    # -- onboarding（首次引导 / 欢迎页）----------------------------------

    def _onboarding():
        from agent.services.onboarding import OnboardingService

        return OnboardingService(ctx.conn, app.version)

    @app.get("/api/onboarding/status")
    async def onboarding_status() -> dict:
        return _onboarding().status().to_dict()

    @app.post("/api/onboarding/seen")
    async def onboarding_seen() -> dict:
        """向导打开即记「本版本已展示过欢迎页」——保证每个版本只强制展开一次。"""
        return _onboarding().mark_seen().to_dict()

    @app.post("/api/onboarding/profile")
    async def onboarding_profile(body: dict) -> dict:
        try:
            return _onboarding().save_profile(
                name=str(body.get("name", "")),
                intro=str(body.get("intro", "")),
                tags=body.get("tags") or [],
                style=str(body.get("style", "")),
                goals=body.get("goals") or [],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/onboarding/complete")
    async def onboarding_complete() -> dict:
        return _onboarding().complete().to_dict()

    @app.post("/api/onboarding/hint")
    async def onboarding_hint(body: dict) -> dict:
        return _onboarding().set_hint_dismissed(bool(body.get("dismissed", False))).to_dict()

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

    @app.post("/api/knowledge/{knowledge_id}/ignore")
    async def ignore_knowledge(knowledge_id: str) -> dict:
        """用户不要这条长期知识，别再问（对话内候选卡的「忽略」）。

        与 `reject` 不同：reject 是打回草稿、条目仍留在审核列表里等人处理；
        ignore 表示用户明确不要它 —— 状态转 `revoked`，并在 provenance 里记
        `ignored_at`，之后同一条内容不再作为知识候选出现在对话里。已经忽略过的
        条目重复调用是幂等的。
        """
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        item = ks.get(knowledge_id)
        if item is None:
            raise HTTPException(status_code=404, detail="knowledge not found")
        if item.state.value == "revoked":
            return {"ok": True, "knowledge_id": item.id}
        ks.revoke(item.id)
        provenance = dict(item.provenance or {})
        now = datetime.now(timezone.utc).isoformat()
        provenance["ignored_at"] = now
        provenance["reason"] = "user_ignored"
        ctx.conn.execute(
            "UPDATE knowledge SET provenance = ?, updated_at = ? WHERE id = ?",
            (json.dumps(provenance, ensure_ascii=False), now, item.id),
        )
        ctx.conn.commit()
        # 记下「这一类刚被忽略」：同类候选在冷却期内不再弹到对话里。
        # 模型每次的措辞都不同，只按句子去重挡不住「刚说忽略又被问一遍」。
        from agent.services.app import KNOWLEDGE_IGNORE_KEY_PREFIX

        ctx.settings_store.set(f"{KNOWLEDGE_IGNORE_KEY_PREFIX}{item.category}", now)
        return {"ok": True, "knowledge_id": item.id}

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
        # 审批是「有身份、只能消费一次」的授权对象：这里必须把身份字段真的传下去，
        # 让服务层校验「这次批准是不是就是为这次请求发的」。
        turn_id = body.get("turn_id")
        session_id = body.get("session_id")
        digest = body.get("request_digest")
        try:
            handled = await approvals.respond(
                approval_id,
                decision,
                scope,
                overrides,
                turn_id=turn_id,
                session_id=session_id,
                digest=digest,
            )
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
    return app
