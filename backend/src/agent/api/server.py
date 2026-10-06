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
import contextlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from agent.api.auth import TICKET_SCOPE_EVENTS, SessionAuth
from agent.api.bus import EventBus
from agent.api.events import AgentEvent, EventType, make_event
from agent.config import Settings
from agent.credentials.providers import (
    CUSTOM_PRESET,
    MODEL_SUGGESTION_NOTE,
    find_preset,
    list_presets,
    preset_for_endpoint,
)
from agent.credentials.store import USABLE_VERIFY_STATES
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
# 附件路由的错误类型与可绑状态：模块级导入（无循环依赖）
from agent.services.attachments import (
    TEMP_SUFFIX,
    STATE_CANCELLED as DISK_CANCELLED,
    STATE_FAILED as DISK_FAILED,
    AttachmentContentError,
    AttachmentError,
    DiskOutcome,
    UploadAborted,
    UploadTooLarge,
    rejected_failure_message,
)
# 上传作业：接收端 / 工作线程 / 收尾共享同一份终态（round 4 问题三）
from agent.services.attachment_upload import (
    SETTLE_SECONDS as UPLOAD_SETTLE_SECONDS,
    # 桥接队列深度搬进了作业模块；名字继续在这里可用（容量口径的唯一来源，验收用例读它）
    UPLOAD_QUEUE_DEPTH,
    UploadJob,
    UploadJobEnded,
    abort_jobs_for,
    active_jobs as upload_active_jobs,
    run_upload_worker,
)
from agent.services.planet import VISIBLE_CAPACITY, PlanetBrowseService
from agent.trace.redact import redact_text
from agent.storage.db_identity import (
    accept_current,
    check_enabled,
    check_integrity,
    connection_db_path,
    disabled_report,
)

# 开发模式的 CORS 兜底：本机 dev server 任意端口。
DEV_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


#: GET /content 只把「确定安全、可内联查看」的类型如实告诉浏览器（并始终带 nosniff）。
#: HTML / SVG / XML 这类会执行脚本或带外链的类型**不内联**：一律 application/octet-stream。
INLINE_SAFE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".ogg": "audio/ogg; codecs=opus",
}



# 摘要只给「一句」：详情层要的是看得懂，不是把整段摘要摊开
_SENTENCE_END = "。！？!?\n"


def _normalized_endpoint(value: str | None) -> str:
    """地址比较用的归一化形式：忽略大小写与结尾斜杠。"""
    return (value or "").strip().rstrip("/").lower()


def _same_endpoint(left: str | None, right: str | None) -> bool:
    """两个地址是不是同一个（空地址永不相等：不知道属于哪里就别乱发）。"""
    normalized = _normalized_endpoint(left)
    return bool(normalized) and normalized == _normalized_endpoint(right)


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
    provenance = dict(item.provenance or {})
    return {
        "id": item.id,
        "category": item.category,
        "state": item.state.value,
        "content": item.content,
        "confidence": item.confidence,
        "topic_id": item.topic_id,
        "topic_name": topic_name,
        # 「看得懂」三件套：从哪来、管多大范围、什么时候结束的
        "source": _knowledge_source_label(provenance),
        "scope": _knowledge_scope(ctx, item),
        "ended": bool(provenance.get("ended_at")),
        "ended_at": provenance.get("ended_at"),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


_KNOWLEDGE_SOURCE_LABELS = {"onboarding": "引导", "dream_correct": "后台整理"}


def _knowledge_source_label(provenance: dict) -> str:
    """把内部的来源标记翻译成人话（用户不需要看到 fragment_id 这类东西）。"""
    if provenance.get("corrected_from"):
        return "你的修正"
    label = _KNOWLEDGE_SOURCE_LABELS.get(str(provenance.get("source") or ""))
    if label:
        return label
    if provenance.get("fragment_id"):
        return "对话"
    return "未记录"


def _topic_ended(ctx, topic_id: str) -> bool:
    """话题是否已结束（标记存在 nodes.meta.ended_at）。"""
    node = ctx.topics.nodes.get_topic(topic_id)
    return bool(node is not None and node.meta.get("ended_at"))


def _knowledge_scope(ctx, item) -> str:
    """这条知识作用在哪里：全局（你）/ 某个话题 / 某张实体卡 / 未指定。"""
    if item.topic_id:
        node = ctx.topics.nodes.get_topic(item.topic_id)
        return f"话题：{node.name}" if node is not None else "话题"
    for node_id in item.node_ids or []:
        node = ctx.topics.nodes.get(node_id)
        if node is None:
            continue
        if node.type == "user":
            return "全局（你）"
        if node.type == "entity":
            return f"实体：{node.name}"
        if node.type == "topic":
            return f"话题：{node.name}"
    return "未指定"


def _dev_task_row(workspaces, task) -> dict:
    """开发任务列表的一行（列表接口与放弃接口**共用同一形状**）。

    状态一律来自工作区本身；已放弃的任务照样在列表里（`abandoned: true`），
    只是界面的「未完成」视图会把它过滤掉。
    """
    status = workspaces.status(task.id)
    return {
        "id": task.id,
        "request": task.request[:200],
        "phase": status.get("phase"),
        "submitted": bool(status.get("submitted")),
        "test_passed": status.get("last_test_passed"),
        # 证据是否对应当前内容：false 就是「改过，结论不算数了」
        "test_evidence_current": bool(status.get("test_evidence_current")),
        "updated_at": status.get("last_test_at") or status.get("created_at"),
        # 有没有「在某个环境里跑它的测试」的授权（范围另见 /api/dev/authorizations）
        "authorized": bool(status.get("test_authorized")),
        # 放弃开发（不可逆终态）：界面按 !submitted && !abandoned 过滤未完成列表
        "abandoned": bool(status.get("abandoned")),
        "abandoned_at": status.get("abandoned_at"),
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
        # 附件：重启收敛 —— 上次没完成准备的标成 failed（可重试）、副本丢了标 missing、
        # 清掉自己留下的 .part 临时文件。不猜状态，只写文件世界的事实。
        try:
            recovered = ctx.attachments.reconcile()
            if recovered.get("recovered_prepared") or recovered.get("temp_files_removed"):
                logging.getLogger(__name__).info("attachments reconciled: %s", recovered)
        except Exception:  # noqa: BLE001 - 收敛失败不该让应用起不来
            logging.getLogger(__name__).warning("attachment reconcile failed", exc_info=True)
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

    app = FastAPI(title="QIO", version="0.1.14", lifespan=lifespan)
    auth = SessionAuth.from_settings(settings)
    instance_id = f"qio_{uuid.uuid4().hex[:16]}"
    # 事件要能自证「来自哪个后端实例」：进程重启后 revision 从 0 重新计数，
    # 前端据此知道旧基准作废、要完整 resync（见 /api/runtime/state）。
    ctx.instance_id = instance_id
    ctx.turns.instance_id = instance_id
    # 附件服务：登记 / 副本 / 引用 / 可用性检查的唯一入口（见 services/attachments.py）。
    # 注册放在 create_app（而不是 AppContext.__init__）：附件相关的文件都归本模块所有，
    # 不改 A 名下的 services/app.py。
    from agent.trace.redact import redact_text
    from agent.services.attachments import AttachmentService

    attachments = AttachmentService(conn, settings.data_dir)
    ctx.attachments = attachments
    ctx.services.register("attachments", attachments)
    from agent.tools.attachment_tools import ReadAttachmentTool

    ctx.registry.register(
        ReadAttachmentTool(
            attachments,
            # 工具执行时处于 single-flight 的 active turn：这就是本轮的真实 turn_id
            active_turn_id=lambda: (ctx.turns.active.turn_id if ctx.turns.active else None),
        )
    )
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
        """backend 身份确认：前端/壳用它验证连上的是本实例，而不是别的进程。

        顺带带上数据库身份自检结果（`db`）：数据目录被外部软件"影子替换"时，
        界面要能立刻告诉用户"你看到的不是原来那个数据库"，而不是让记录凭空消失。
        """
        return {
            "instance_id": instance_id,
            "pid": os.getpid(),
            "auth_required": auth.enabled,
            "version": app.version,
            "db": _db_integrity_payload(),
        }

    def _db_integrity_payload() -> dict:
        path = connection_db_path(ctx.conn) or str(settings.db_path)
        if not check_enabled():
            return disabled_report(path).as_payload()
        return check_integrity(ctx.conn, path).as_payload()

    @app.post("/api/db-integrity/accept")
    async def accept_db_integrity() -> dict:
        """用户确认"以当前数据库为准"：重新记基线，告警随之消失。"""
        path = connection_db_path(ctx.conn) or str(settings.db_path)
        report = accept_current(ctx.conn, path)
        return {"ok": True, "db": report.as_payload()}

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
        default = ctx.credentials.effective_default()
        return {
            "credentials": creds,
            # 「当前默认使用」由后端算：显式默认项优先，没有就按既有排序回落。
            # 界面据此显示「当前默认」标记，而不是自己猜一个。
            "default_key_id": default["id"] if default else None,
        }

    @app.get("/api/credentials/providers")
    async def list_providers() -> dict:
        """厂商预设：界面的唯一来源（名称 / 协议 / 地址 / 建议模型 / 类别）。

        这**不识别 Key**：识别属于本地前缀提示（返回 key_hint 字段），
        任何验证都只在用户选定地址上进行。
        """
        providers = [preset.to_dict() for preset in list_presets()]
        providers.append(CUSTOM_PRESET.to_dict())
        return {"providers": providers, "model_note": MODEL_SUGGESTION_NOTE}

    def _credential_payload(meta: dict) -> dict:
        payload = dict(meta)
        payload["key_id"] = payload.pop("id")
        preset = preset_for_endpoint(payload.get("endpoint"))
        payload["provider_id"] = preset.id if preset else None
        payload["provider_name"] = preset.name if preset else None
        return payload

    def _verify_failure_message(result) -> str:
        return result.message

    async def _run_verification(key_id: str) -> dict:
        """对一条**已存在**的凭据做一次真实调用验证，并按结果更新状态。

        - 通过 → verify_state=verified，并在没有可用默认项时把它设为默认；
        - 明确被拒（Key 无效 / 没有该模型权限 / 额度不足）→ verify_state=failed；
        - 只是网络/超时/限流这类临时故障 → **保持原状态**（不能因为一次断网
          就把一把本来可用的钥匙判死），但如实把这次失败告诉用户。
        """
        from agent.services.verify import TRANSIENT_REASONS, verify_model

        secret = ctx.credentials.get_secret(key_id)
        meta = ctx.credentials.get_metadata(key_id)
        if secret is None or meta is None:
            return {
                "ok": False,
                "state": "failed",
                "reason_code": "credential_unavailable",
                "message": "这条凭据当前不可用（已停用或密钥不在本机）",
                "detail": "",
                "mode": None,
                "state_kept": False,
            }
        previous = str(meta.get("verify_state") or "unverified")
        result = await verify_model(
            secret=secret,
            endpoint=meta.get("endpoint"),
            model=meta.get("default_model"),
            kind=meta.get("kind"),
        )
        payload = result.to_dict()
        if result.ok:
            ctx.credentials.set_verified(key_id, True)
            ctx.credentials.promote_default()
            payload["state_kept"] = False
        elif result.reason_code in TRANSIENT_REASONS and previous in ("verified", "legacy"):
            payload.update(state=previous, state_kept=True)
            payload["message"] = f"{_verify_failure_message(result)}（这条凭据的状态保持不变）"
        else:
            ctx.credentials.set_verified(key_id, False, result.reason_code)
            payload["state_kept"] = False
        updated = ctx.credentials.get_metadata(key_id)
        if updated is not None:
            payload["credential"] = _credential_payload(updated)
        await bus.publish(
            make_event(
                EventType.CREDENTIAL_STATUS,
                {"key_id": key_id, "status": payload["state"], "verified": result.ok},
            )
        )
        return payload

    @app.post("/api/credentials")
    async def create_credential(body: dict) -> dict:
        """保存 = 校验 + 安全写入 + 一次自动可用性验证。

        「保存成功」与「验证通过」是两件事：写入失败什么都不留（报错），
        写入成功但验证没过则如实返回「已保存，尚未通过验证」。
        """
        secret = str(body.get("secret") or "").strip()
        if not secret:
            raise HTTPException(status_code=400, detail="请填写 API Key")

        # 用途：新建时**缺省** = 主对话；用户显式提交空用途时是他自己的选择，
        # 这里返回看得懂的错误，不静默覆盖成主对话。
        if "tags" not in body:
            tags = ["main-loop"]
        else:
            raw_tags = body.get("tags")
            tags = (
                [str(t).strip() for t in raw_tags if str(t).strip()]
                if isinstance(raw_tags, list)
                else []
            )
            if not tags:
                raise HTTPException(
                    status_code=400,
                    detail="请至少选择一种用途（例如「主对话」）；只有这样配置才能被任务选中",
                )

        preset = find_preset(str(body.get("provider") or "").strip()) or preset_for_endpoint(
            body.get("endpoint")
        )
        endpoint = body.get("endpoint")
        if endpoint is None:
            endpoint = preset.base_url if preset else None
        endpoint = str(endpoint or "").strip() or None
        if not endpoint:
            raise HTTPException(status_code=400, detail="请填写服务地址")
        kind = str(body.get("kind") or (preset.kind if preset else "")).strip() or None
        model = body.get("default_model")
        if model is None:
            model = preset.suggested_model if preset else ""
        model = str(model or "").strip()
        if not model:
            # 没有可靠默认模型的服务（聚合/自定义）：明确要求用户补一个，
            # 不替他从模型列表里挑第一条，也不回退到别家的模型。
            raise HTTPException(status_code=400, detail="请选择或填写模型名称")
        note = str(body.get("note") or "").strip() or (preset.name if preset else None)

        def _budget(value):
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="用量上限要填数字（token）") from exc

        # 内部标识不要求用户输入。带 client_request_id 时由它推导：
        # 「点了保存但结果没回来，用户又点了一次」不会留下两条凭据。
        request_id = str(body.get("client_request_id") or "").strip()
        key_id = str(body.get("key_id") or "").strip()
        if not key_id:
            key_id = (
                f"key_{uuid.uuid5(uuid.NAMESPACE_URL, request_id).hex[:12]}"
                if request_id
                else f"key_{uuid.uuid4().hex[:12]}"
            )
        existing = ctx.credentials.get_metadata(key_id)
        if existing is not None:
            return {
                "ok": True,
                "saved": True,
                "idempotent": True,
                "key_id": key_id,
                "version": existing["version"],
                "credential": _credential_payload(existing),
                "verify": {
                    "ok": existing.get("verify_state") == "verified",
                    "state": existing.get("verify_state"),
                    "reason_code": None,
                    "message": "这次保存之前已经写入过了，没有重复创建",
                    "detail": "",
                    "mode": None,
                    "state_kept": True,
                },
            }
        try:
            version = ctx.credentials.create(
                key_id=key_id,
                secret=secret,
                tags=tags,
                endpoint=endpoint,
                default_model=model,
                budget=_budget(body.get("budget")),
                note=note,
                kind=kind,
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 安全存储不可用等系统级失败
            raise HTTPException(
                status_code=500,
                detail="保存失败：系统的安全凭据库在写入时出错，密钥没有保存",
            ) from exc
        await bus.publish(
            make_event(
                EventType.CREDENTIAL_STATUS,
                {"key_id": key_id, "status": "active", "version": version},
            )
        )
        verify = await _run_verification(key_id)
        meta = ctx.credentials.get_metadata(key_id) or {}
        return {
            "ok": True,
            "saved": True,
            "idempotent": False,
            "key_id": key_id,
            "version": version,
            "credential": _credential_payload(meta),
            "verify": verify,
        }

    @app.post("/api/credentials/verify-draft")
    async def verify_draft(body: dict) -> dict:
        """保存前验证一份草稿（换钥、自定义服务用）。

        只请求 body 里给的 endpoint，绝不把 Key 发给别家。密钥不落库、不记日志。
        """
        from agent.services.verify import verify_model

        secret = str(body.get("secret") or "").strip()
        if not secret:
            raise HTTPException(status_code=400, detail="请填写 API Key")
        result = await verify_model(
            secret=secret,
            endpoint=str(body.get("endpoint") or "").strip() or None,
            model=str(body.get("default_model") or "").strip() or None,
            kind=str(body.get("kind") or "").strip() or None,
        )
        return {"ok": result.ok, "verify": result.to_dict()}

    @app.get("/api/credentials/models")
    async def list_credential_models(
        endpoint: str, secret: str | None = None, kind: str | None = None, key_id: str | None = None
    ) -> dict:
        """模型候选列表（只是便利，不是验证）。

        `key_id` 用于已保存的凭据（不把密钥交给前端）；`secret` 用于还没落库的草稿。
        已保存的 Key 只回答**它自己那个地址**：地址变了就等于换了发送目标，
        在用户重新填 Key 并确认之前不能把它发出去。
        """
        from agent.services.verify import list_models

        token = secret
        if token is None and key_id:
            meta = ctx.credentials.get_metadata(key_id) or {}
            if not _same_endpoint(meta.get("endpoint"), endpoint):
                return {
                    "models": [],
                    "note": "服务地址和这条凭据保存时不一致，不会用已保存的 Key 去取模型列表；"
                    "填好新的 Key 并保存后即可取回",
                }
            token = ctx.credentials.get_secret(key_id)
        if not token:
            return {"models": [], "note": "没有可用的密钥，无法获取模型列表；可以手动填写模型名称"}
        models = await list_models(secret=token, endpoint=endpoint, kind=kind)
        return {"models": models}

    @app.post("/api/credentials/{key_id}/verify")
    async def verify_credential(key_id: str) -> dict:
        """重试验证：操作的是同一条记录，不会重复创建凭据。"""
        if ctx.credentials.get_metadata(key_id) is None:
            raise HTTPException(status_code=404, detail="credential not found")
        result = await _run_verification(key_id)
        return {"ok": True, "key_id": key_id, "verify": result}

    @app.post("/api/credentials/{key_id}/default")
    async def set_default_credential(key_id: str) -> dict:
        """把某条主对话凭据设为默认。

        只改「用哪一条」，不是授权：停用、撤销、预算用尽、没有 main-loop 用途、
        还没通过验证的凭据都不能被设为默认。
        """
        meta = ctx.credentials.get_metadata(key_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="credential not found")
        if "main-loop" not in (meta.get("tags") or []):
            raise HTTPException(status_code=400, detail="这条凭据没有「主对话」用途，不能设为默认")
        if meta.get("status") != "active" or not meta.get("enabled", True):
            raise HTTPException(status_code=400, detail="请先启用这条凭据，再设为默认")
        if (meta.get("verify_state") or "unverified") not in USABLE_VERIFY_STATES:
            raise HTTPException(status_code=400, detail="请先完成验证，再把这条凭据设为默认")
        budget_left = ctx.credentials.budget_left(key_id)
        if budget_left is not None and budget_left <= 0:
            raise HTTPException(status_code=400, detail="这条凭据的用量上限已经用完，不能设为默认")
        updated = ctx.credentials.set_default(key_id)
        await bus.publish(
            make_event(EventType.CREDENTIAL_STATUS, {"key_id": key_id, "status": "default"})
        )
        return {"ok": True, "credential": _credential_payload(updated)}

    @app.post("/api/credentials/{key_id}/test")
    async def test_credential(key_id: str) -> dict:
        """（保留的旧入口）测试连接 = 重试验证，走与正式对话一致的协议。"""
        if ctx.credentials.get_metadata(key_id) is None:
            raise HTTPException(status_code=404, detail="credential not found")
        result = await _run_verification(key_id)
        return {
            "key_id": key_id,
            "verify": result,
            "probe": {"mode": result.get("mode") or "", "detail": result.get("detail") or ""},
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
        if "kind" in body:
            kwargs["kind"] = body.get("kind")
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
        # 影响「还能不能通」的字段变了（钥匙 / 地址 / 协议 / 模型），就重新验证一次：
        # 验证结果必须跟着配置走，不能沿用旧模型、旧地址的结论。
        verify = None
        if secret is not None or {"endpoint", "kind", "default_model"} & set(body):
            verify = await _run_verification(key_id)
            meta = ctx.credentials.get_metadata(key_id) or meta
        await bus.publish(
            make_event(
                EventType.CREDENTIAL_STATUS,
                {
                    "key_id": key_id,
                    "status": "active",
                    "version": meta.get("version"),
                },
            )
        )
        return {"ok": True, "credential": _credential_payload(meta), "verify": verify}

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
            # 新根目录同样要就位：否则用户填了一个还不存在的目录，之后每个相对
            # 路径的文件调用都会以「系统找不到指定的路径」结束。
            ctx.computer.ensure_root()
        if "permission_mode" in body:
            mode = str(body["permission_mode"])
            if mode not in PERMISSION_MODES:
                raise HTTPException(status_code=400, detail="invalid permission_mode")
            store.set("computer.permission_mode", mode)
        return await get_computer_settings()

    @app.get("/api/settings/tools")
    async def get_tool_history_settings() -> dict:
        """工具调用历史的设置：是否保存输出全文、输出保留多少天、整条记录保留多少天。

        `record_retention_days = 0` 表示**永久保留整条记录** —— 这是默认值，
        与历史行为一致（以前只按天清输出全文，参数/错误等永久保留）。
        `record_count` 让界面能说清「清空会删掉多少条」，而不是让用户盲删。
        """
        from agent.storage.tool_records import (
            DEFAULT_RECORD_RETENTION_DAYS,
            DEFAULT_RETENTION_DAYS,
            count_records,
        )

        store = ctx.settings_store
        days = store.get_int("tools.output_retention_days", DEFAULT_RETENTION_DAYS)
        record_days = store.get_int(
            "tools.record_retention_days", DEFAULT_RECORD_RETENTION_DAYS
        )
        return {
            "record_outputs": store.get_bool("tools.record_outputs", True),
            "output_retention_days": max(0, days),
            "record_retention_days": max(0, record_days),
            "record_count": count_records(ctx.conn),
        }

    @app.put("/api/settings/tools")
    async def update_tool_history_settings(body: dict) -> dict:
        from agent.storage.tool_records import MAX_RETENTION_DAYS

        store = ctx.settings_store
        if "record_outputs" in body:
            store.set("tools.record_outputs", "1" if body.get("record_outputs") else "0")
        if "output_retention_days" in body:
            try:
                days = int(body["output_retention_days"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="invalid output_retention_days")
            # 超出范围按边界收敛（与搜索设置的既有做法一致）
            store.set(
                "tools.output_retention_days", str(max(0, min(days, MAX_RETENTION_DAYS)))
            )
        if "record_retention_days" in body:
            try:
                record_days = int(body["record_retention_days"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="invalid record_retention_days")
            # 0 = 永久保留（默认，与既有行为一致）
            store.set(
                "tools.record_retention_days",
                str(max(0, min(record_days, MAX_RETENTION_DAYS))),
            )
        payload = await get_tool_history_settings()
        # 保存即生效：把天数调小要马上清掉过期内容；purged 是这次清掉的条数
        payload["purged"] = ctx.prune_tool_outputs()
        payload["records_purged"] = ctx.prune_tool_records()
        return payload

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

    # -- attachments -------------------------------------------------------

    async def _prepare_attachment_in_background(attachment_id: str) -> None:
        """后台准备：**工作线程只做文件 I/O**，数据库动作全部回到事件循环线程。

        为什么必须这么绕（2026-10-06 CI py3.12/windows 真事故）：AttachmentService 与
        整个应用共用同一个 sqlite 连接（storage/db.py 用 check_same_thread=False）。
        以前这里把整个 run_prepare 丢进 asyncio.to_thread，工作线程于是既读又写那个
        共享连接，与事件循环线程并发使用同一个连接对象 —— 结果是
        sqlite3.InterfaceError，以及「刚 POST 成功、紧接着 GET 404」的幻影状态。
        本机（Windows + py3.11）反复全绿只是时序运气。
        """
        att = attachments.get(attachment_id, check=False)
        if att is None:
            return
        outcome = await asyncio.to_thread(attachments.copy_to_disk, att)
        attachments.apply_outcome(attachment_id, outcome)

    def _note_background_failure(task: asyncio.Task) -> None:
        """后台任务的异常必须被取走：否则日志里只剩 'Task exception was never retrieved'。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logging.getLogger(__name__).warning(
                "附件后台准备失败：%s", redact_text(str(exc)), exc_info=exc
            )

    def _schedule_prepare(attachment_id: str) -> None:
        task = asyncio.create_task(_prepare_attachment_in_background(attachment_id))
        task.add_done_callback(_note_background_failure)

    @app.post("/api/attachments")
    async def create_attachment(body: dict) -> dict:
        """登记一个本地文件：**按服务端 stat 出来的真实大小**决定存副本还是记引用。

        ≤ 100_000_000 字节 → 存独立副本（后台复制，先写 .part 再改名提交）；
        >  100_000_000 字节 → 只记路径 + 元数据（历史保留的是位置，不保证内容仍在）。
        复制在后台线程里做，这里立刻返回登记事实（state=prepared），
        前端按 GET /api/attachments/{id} 跟到 ready / failed / changed。
        """
        source_path = str(body.get("source_path") or "")
        raw_name = body.get("name")
        topic_id = body.get("topic_id")
        try:
            att = attachments.prepare(
                source_path,
                name=str(raw_name) if raw_name else None,
                size=body.get("size"),
                topic_id=str(topic_id) if topic_id else None,
            )
        except AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _schedule_prepare(att.id)
        return {"ok": True, "attachment": attachments.payload(att, check=False)}

    def _upload_limit_detail() -> str:
        from agent.services.attachments import human_size

        limit = attachments.max_upload_bytes
        return (
            f"浏览器上传只用于 <= {human_size(limit)} 的文件；这个文件更大，"
            "请用桌面端拖入或选择本地路径"
            f"（大于 {human_size(limit)} 的文件只记位置，不复制内容）"
        )

    def _converge_upload(attachment_id: str, reason: str, *, state: str = DISK_FAILED) -> None:
        """失败/取消的收尾（**事件循环线程**）：行还在就如实转 failed/cancelled，绝不提交 ready。

        工作线程只做文件 I/O、不再落库，所以这里是上传状态的唯一出口。行已经被用户删掉时
        什么都不做（apply_outcome 会自己处理「行不在」的情况并清掉可能已提交的副本）。
        """
        attachments.apply_outcome(attachment_id, DiskOutcome(state=state, error=reason))

    def _purge_uncommitted_copy(att) -> None:
        """取消竞态清理：工作线程可能已经把正式副本提交到位（os.replace 之后才被丢弃），

        而它的结果已经落不到库里 —— 按**可预测的副本路径**（QIO 自己的管理目录）清掉它，
        避免留下无人认领的副本。用户原文件永远不在此列。
        """
        with contextlib.suppress(Exception):
            path = attachments.copy_path(att)
            if attachments.is_managed_path(path):
                Path(path).unlink(missing_ok=True)
                Path(str(path) + TEMP_SUFFIX).unlink(missing_ok=True)

    async def _settle_upload_worker(
        worker: asyncio.Task, job: UploadJob, *, attachment_id: str
    ) -> DiskOutcome | None:
        """请求被取消（服务关闭 / 客户端离开）：解除工作线程阻塞读、有界等它收尾，返回磁盘结果。

        收尾纪律：**不留临时文件、不留孤儿副本**。
        * 先置服务侧取消标志（工作线程提交前的最后一道闸会看到它，不再 os.replace）；
        * 再置作业终态（解除它在 queue.get 上的阻塞）；
        * 工作线程如果已经提交了正式副本（竞态），这里把它当作孤儿清掉 —— 行不会落成 ready。
        """
        attachments.cancel(attachment_id)
        job.abort("上传被取消（服务关闭或请求中断）；没有保存任何副本")
        outcome: DiskOutcome | None = None
        with contextlib.suppress(BaseException):
            outcome = await asyncio.wait_for(
                asyncio.shield(worker), timeout=UPLOAD_SETTLE_SECONDS
            )
        if outcome is not None and outcome.stored_path:
            if attachments.is_managed_path(outcome.stored_path):
                try:
                    Path(outcome.stored_path).unlink(missing_ok=True)
                except OSError as exc:  # noqa: BLE001 - 清理失败不能掩盖取消
                    logging.getLogger(__name__).warning(
                        "清理被取消上传的副本失败：%s", redact_text(str(exc))
                    )
        return outcome

    @app.post("/api/attachments/upload")
    async def upload_attachment(request: Request) -> dict:
        """浏览器回退：请求体就是**原始字节**（不引入 multipart 依赖）。

        头：X-QIO-Name（URL 编码的 UTF-8 文件名）、X-QIO-Topic-Id（可选）。
        没有真实路径：只存副本；超过阈值的字节明确拒绝，不偷偷存一个大副本。

        有界接收（审计问题 6）：**不把整包读进内存**，没有 Content-Length 时照样强制上限
        （每收一块累加校验，超限立刻中止并清理）。写临时文件 + 算 sha256 在工作线程
        （纯文件 I/O），登记与落状态在事件循环线程 —— 同一个 sqlite 连接永不被两个线程碰，
        见 services/attachments.py 顶部的线程纪律。
        """
        limit = attachments.max_upload_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(status_code=413, detail=_upload_limit_detail())
        raw_name = request.headers.get("x-qio-name") or "attachment"
        try:
            from urllib.parse import unquote

            name = unquote(raw_name)
        except Exception:  # noqa: BLE001 - 头里的名字解不出来就退回原名
            name = raw_name
        topic_id = request.headers.get("x-qio-topic-id") or None
        # 先登记一行（prepared）：字节由工作线程落盘，状态由事件循环线程落库
        att = attachments.begin_upload(
            name=name, topic_id=str(topic_id) if topic_id else None
        )
        # 一次上传 = 一个作业：队列 + 终态 + 工作线程句柄，接收端/工作线程/取消清理共享它。
        # 这样「工作线程死了」不再表现为「队列永远等不到空位」，取消也能解除工作线程的阻塞读。
        job = UploadJob(
            label=att.id,
            loop=asyncio.get_running_loop(),
            depth=UPLOAD_QUEUE_DEPTH,
            cancel_requested=lambda: attachments.is_cancel_requested(att.id),
        )
        worker = asyncio.create_task(
            asyncio.to_thread(run_upload_worker, attachments, att, job, max_bytes=limit)
        )
        received = 0
        too_large = False
        read_error: BaseException | None = None
        ended: UploadJobEnded | None = None
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > limit:
                    too_large = True
                    break
                # 每次排队前先看终态；队列满时同时等空位与终态（谁先到谁解除等待）
                await job.put(chunk)
        except UploadJobEnded as exc:
            # 工作线程已经结束（写盘失败 / 被取消）：立即停止接收，按它的结果准确反馈
            ended = exc
        except asyncio.CancelledError:
            # 服务关闭 / 请求被取消：先让工作线程看到终态（解除阻塞读），再等它收尾
            await _settle_upload_worker(worker, job, attachment_id=att.id)
            _converge_upload(att.id, "上传被取消（服务关闭或请求中断）；没有保存任何副本")
            _purge_uncommitted_copy(att)
            job.close()
            raise
        except Exception as exc:  # noqa: BLE001 - 客户端断开/协议错误：按中止处理
            read_error = exc
        finally:
            if read_error is not None:
                job.abort(f"上传被中断：{redact_text(str(read_error))}；没有保存任何副本")
            with contextlib.suppress(UploadJobEnded):
                await job.close_input(abort=bool(too_large or read_error))
        try:
            try:
                outcome = await worker
            except (UploadTooLarge, UploadAborted) as exc:
                # 超限/中止都不留行、不留文件：这不是「失败的附件」，是被拒绝的上传
                if too_large or isinstance(exc, UploadTooLarge):
                    attachments.delete(att.id)
                    raise HTTPException(status_code=413, detail=_upload_limit_detail()) from exc
                if read_error is not None:
                    attachments.delete(att.id)
                    raise HTTPException(
                        status_code=400, detail=f"上传被中断：{redact_text(str(read_error))}"
                    ) from exc
                # 取消（用户 DELETE / 服务关闭）：行若还在，如实转 cancelled；不提交 ready
                _converge_upload(att.id, str(exc), state=DISK_CANCELLED)
                if attachments.get(att.id, check=False) is None or ended is not None:
                    raise HTTPException(status_code=404, detail="上传期间附件已被移除") from exc
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except AttachmentError as exc:
                attachments.delete(att.id)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except asyncio.CancelledError:
                # 等结果时被取消（服务关闭 / 客户端离开）：工作线程可能刚好把副本提交到位，
                # 而它的结果已经无法落库 —— 先解除阻塞、再把这份无人认领的副本清掉。
                await _settle_upload_worker(worker, job, attachment_id=att.id)
                _converge_upload(att.id, "上传被取消（服务关闭或请求中断）；没有保存任何副本")
                _purge_uncommitted_copy(att)
                raise
            if too_large:
                attachments.delete(att.id)
                raise HTTPException(status_code=413, detail=_upload_limit_detail())
            applied = attachments.apply_outcome(att.id, outcome)
            if applied is None:
                raise HTTPException(status_code=404, detail="上传期间附件已被移除")
            if outcome.state == DISK_FAILED:
                # 真实写盘失败（建目录 / 打开 / 写入途中 / 权限）：不装作成功 ——
                # 行如实转 failed（带人话原因，可重试），HTTP 报服务端失败。
                raise HTTPException(
                    status_code=500,
                    detail=outcome.error or "上传失败：没有保存副本",
                )
            if outcome.state == DISK_CANCELLED:
                raise HTTPException(status_code=409, detail=outcome.error or "上传已取消")
            return {"ok": True, "attachment": attachments.payload(applied, check=False)}
        finally:
            job.close()

    @app.get("/api/attachments")
    async def list_attachments(
        topic_id: str | None = None,
        turn_id: str | None = None,
        unbound: bool = False,
    ) -> dict:
        items = attachments.list(
            topic_id=topic_id, turn_id=turn_id, unbound=bool(unbound), limit=100
        )
        return {"ok": True, "attachments": [attachments.payload(a) for a in items]}

    @app.get("/api/attachments/{attachment_id}")
    async def get_attachment(attachment_id: str) -> dict:
        """元数据 + 可用性/变化检查：missing（不在原位）/ changed（内容与登记时不同）。"""
        att = attachments.get(attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="没有这个附件")
        return {"ok": True, "attachment": attachments.payload(att)}

    @app.get("/api/attachments/{attachment_id}/content")
    async def attachment_content(attachment_id: str) -> FileResponse:
        """打开/下载一个附件：**只读 QIO 自己管理的副本**。

        * 只认 kind=copy 且 state=ready 的副本，路径由 id 从数据库取，
          **绝不接受调用方给的任意路径** —— 这是「打开历史附件」与「任意文件读取」的分界线；
        * 沿用 /api/* 的会话令牌认证（session_guard 中间件），没有裸链接；
        * 文件名只用 QIO 清洗过的 original_name（safe_name 落盘名，不含路径），
          并带 X-Content-Type-Options: nosniff；HTML/SVG 这类会执行脚本的类型不内联。
        """
        try:
            path, name = attachments.content_target(attachment_id)
        except AttachmentContentError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        media_type = INLINE_SAFE_SUFFIXES.get(Path(name).suffix.lower(), "application/octet-stream")
        return FileResponse(
            path,
            media_type=media_type,
            filename=name,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @app.post("/api/attachments/{attachment_id}/relocate")
    async def relocate_attachment(attachment_id: str, body: dict) -> dict:
        """文件被移动/改名之后重新指定位置；副本按新来源重做。

        重新校验（真实大小 → copy / reference、状态、位置）后把准备交给后台：
        工作线程只做文件 I/O，状态回事件循环线程落库（审计问题 6）。
        响应是**受理事实**（prepared / missing / failed）：不要当成功，
        按 GET /api/attachments/{id} 跟到 ready / failed / changed。
        """
        try:
            att = attachments.plan_relocate(attachment_id, str(body.get("source_path") or ""))
        except AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if att.state == "prepared":
            _schedule_prepare(att.id)
        return {"ok": True, "attachment": attachments.payload(att, check=False)}

    @app.post("/api/attachments/{attachment_id}/retry")
    async def retry_attachment(attachment_id: str) -> dict:
        """失败/取消/变化之后重试：同一行重做副本，不新建附件。"""
        att = attachments.get(attachment_id, check=False)
        if att is None:
            raise HTTPException(status_code=404, detail="没有这个附件")
        _schedule_prepare(att.id)
        return {"ok": True, "attachment": attachments.payload(att, check=False)}

    @app.delete("/api/attachments/{attachment_id}")
    async def delete_attachment(attachment_id: str) -> dict:
        """移除附件：**只删 QIO 自己管理的副本，绝不动用户原文件**。

        正在复制时调用它 = 取消：复制线程在分块之间看到标志就停下并清掉临时文件。
        """
        # 先中止正在进行的上传作业：删除附件 = 取消这次上传。
        # 不能只靠服务侧的取消事件 —— services/attachments.py 的 delete() 置位后会把事件从
        # _cancel 里清掉，事后轮询的取块循环就看不到取消了（CI py3.12 的取消用例红在这里）。
        abort_jobs_for(
            attachment_id, "附件已被移除，上传取消；没有保存任何副本"
        )
        result = attachments.delete(attachment_id)
        if not result["removed"]:
            raise HTTPException(status_code=404, detail="没有这个附件")
        return {"ok": True, **result}

    # -- 轮次绑定：B 的冻结回执 + 受理前预检（round4 §1.2） -------------------

    def _attachment_failure(
        rejected: list[tuple[str, str]],
        *,
        message: str | None = None,
    ) -> HTTPException:
        """结构化失败（409）：受理前拒绝、不入队、不消费 resend claim。

        detail 的形状是前端契约（stores/session.ts 读 detail.rejected；
        message 用 B 的 rejected_failure_message 生成一句话），不要改。
        """
        rows = [(str(item), str(reason)) for item, reason in (rejected or [])]
        return HTTPException(
            status_code=409,
            detail={
                "code": "attachment_binding_failed",
                "message": message or rejected_failure_message(rows),
                "rejected": [{"id": item, "reason": reason} for item, reason in rows],
                "bound_attachment_ids": [],
            },
        )

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
        # 附件（契约 §1.4）：attachment_ids 的**存在性**即语义 ——
        # 字段出现（含空列表）= 显式，只绑列出的这些，[] 表示这一轮没有附件；
        # 字段缺失才走旧客户端兜底（把本话题下还没绑定任何轮次的附件绑上来）。
        # 以前写成 body.get("attachment_ids") or []：显式空列表被压成 falsy 落进兜底分支，
        # 用户清空附件后发纯文字，遗留附件仍被绑进这一轮（审计问题 3）。
        explicit_ids: list[str] | None = None
        if "attachment_ids" in body:
            raw_ids = body.get("attachment_ids")
            if not isinstance(raw_ids, list):
                raise HTTPException(status_code=400, detail="attachment_ids must be a list")
            explicit_ids = [str(item) for item in raw_ids]
        # 重试复用（round4 §1.2）：重试时必须带上原轮 turn_id，B 据此克隆可复用的附件；
        # 严格语义「rejected 非空 → 不入队」：**submit 之前**用 B 的只读预检挡一次
        # （判据与 bind_for_turn 共用同一份 _reject_reason，两处规则不会漂移）。
        retry_raw = body.get("retry_of_turn_id")
        retry_of = (
            str(retry_raw).strip()
            if isinstance(retry_raw, str) and retry_raw.strip()
            else None
        )
        precheck = attachments.precheck_for_turn(
            attachment_ids=explicit_ids, topic_id=topic_id, retry_of_turn_id=retry_of
        )
        if precheck:
            raise _attachment_failure(precheck)
        turn = ctx.turns.submit(
            message, topic_id, intent_id=pending.intent_id if pending else None
        )
        outcome = attachments.bind_for_turn(
            turn_id=turn.turn_id,
            message_id=None,
            attachment_ids=explicit_ids,
            topic_id=topic_id,
            retry_of_turn_id=retry_of,
        )
        if outcome.rejected:
            # 预检之后的竞态（刚被删 / 被别的轮次抢走）：撤销刚提交的这一轮，不入队。
            ctx.turns.cancel(turn.turn_id)
            raise _attachment_failure(outcome.rejected)
        return {
            "ok": True,
            "accepted": True,
            "turn_id": turn.turn_id,
            "status": turn.status,
            "message": message,
            "topic_id": topic_id,
            # 实际绑定回执（§1.2）：前端以它为准更新界面，未绑定不得显示为「已带上」
            **outcome.as_receipt(),
            "attachments": [attachments.payload(a, check=False) for a in outcome],
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

    @app.get("/api/dev/tasks")
    async def dev_tasks() -> dict:
        """开发任务列表（状态来自工作区本身，不是界面的记忆）。

        「有未完成的任务」入口用它：刷新、重启、断线之后任务都还在，
        不会再出现「模型说要继续开发，界面上却找不到那个任务」。

        已放弃的任务**仍然出现在这里**（带 `abandoned: true`）：这是事实清单；
        「未完成」是界面按 `!submitted && !abandoned` 过滤出来的视图。
        """
        return {
            "tasks": [
                _dev_task_row(ctx.dev_workspaces, task)
                for task in ctx.dev_workspaces.list_tasks()
            ]
        }

    @app.post("/api/dev/tasks/{task_id}/abandon")
    async def abandon_dev_task(task_id: str) -> dict:
        """放弃一项没做完的开发任务（终态，幂等）。

        语义边界（见 _ABANDON-CONTRACT.md 第 1 章）：放弃**不**删记录、不删工作区
        文件、不删已注册工具；它只结束这项开发并收回该任务的执行授权。

        **顺序**（契约 v2 第 C 章，不许调换）：
        1. 先把放弃终态**可靠落盘**（`abandon()` 内部是事务式的：先严格收回长期授权，
           再严格写 `state.json` 并回读校验）；
        2. 只有落盘成功，才去作废这个任务的未决审批。

        为什么不是「先作废审批再标放弃」：作废是**不可逆**的（等待方立刻收到 cancelled），
        如果先作废、随后保存失败，就会出现「确认已经作废、任务却还在」的部分完成状态，
        用户看到的是「失败、请重试」，而他刚点掉的确认已经回不来了。反过来的失败代价小得多：
        任务仍是未完成、卡片还在、重试即可收敛。

        保存失败（`persist_failed`）时**一个审批都不作废**、任务行原样返回（仍是未放弃），
        界面保留条目、就地显示原因、允许重试；这保证不会出现「内存已放弃、磁盘未放弃」
        却报成功的状态。

        被拒绝（正在执行 / 已提交）时**零状态改动**：不标放弃、不收回授权、
        不作废审批、不停任何东西。这个接口**绝不**调用 turn 取消 —— 那会误停用户
        别的任务；没有「只停止这一个任务」的能力就如实说明（can_stop=false）。
        """
        readiness = ctx.dev_workspaces.abandon_readiness(task_id)
        if readiness["status"] == "not_found":
            raise HTTPException(status_code=404, detail=readiness["message"])
        if not readiness["allowed"]:
            # 拒绝路径：读一次当前状态行即可，一个字段都不改。
            task = ctx.dev_workspaces.task(task_id)
            return {
                "ok": False,
                "status": readiness["status"],
                "message": readiness["message"],
                "revoked": False,
                "invalidated_approvals": 0,
                "can_stop": bool(readiness["can_stop"]),
                "persisted": False,
                "task": _dev_task_row(ctx.dev_workspaces, task) if task else None,
            }

        # 1) 先把终态可靠落盘（失败则内存与磁盘一致地保持「未放弃」）
        result = ctx.dev_workspaces.abandon(task_id)
        task = ctx.dev_workspaces.task(task_id)
        if not result.get("ok"):
            return {
                "ok": False,
                "status": str(result.get("status") or "persist_failed"),
                "message": str(result.get("message") or "这次没能放弃：状态没能保存，请重试。"),
                # 如实反映**已经落盘**的那部分（例如长期授权已收回）
                "revoked": bool(result.get("revoked")),
                "invalidated_approvals": 0,
                "can_stop": bool(readiness["can_stop"]),
                "persisted": bool(result.get("persisted", False)),
                "task": _dev_task_row(ctx.dev_workspaces, task) if task else None,
            }

        # 2) 落盘成功之后才作废未决审批（test_execution / tool_create / credential_grant）。
        #    已经放弃过（幂等重复）也要走这一步：上次可能在作废之前就中断了，重试要能收敛。
        invalidated = ctx.approvals.invalidate_for_task(task_id)
        return {
            "ok": True,
            "status": str(result.get("status") or "abandoned"),
            "message": str(result.get("message") or "已放弃这个开发任务。"),
            "revoked": bool(result.get("revoked")),
            "invalidated_approvals": int(invalidated),
            "can_stop": bool(readiness["can_stop"]),
            "persisted": True,
            "task": _dev_task_row(ctx.dev_workspaces, task) if task else None,
        }

    @app.get("/api/dev/authorizations")
    async def dev_authorizations() -> dict:
        """当前的执行授权**范围**：用户要看得到自己同意过什么。

        授权不是一句「已允许」：它绑在（能力策略指纹 + 执行环境）上，范围包括
        能力、目录、网络与凭据引用。这里把它们如实列出来。
        """
        rows = ctx.dev_workspaces.authorizations()
        for row in rows:
            definition = ctx.dev_workspaces.read_definition(row["task_id"])
            row["tool_name"] = getattr(definition, "name", None)
        return {"authorizations": rows}

    @app.post("/api/dev/authorizations/{task_id}/revoke")
    async def revoke_dev_authorization(task_id: str) -> dict:
        """收回某个开发任务的执行授权：下一次测试会重新征求确认。"""
        return {"revoked": ctx.dev_workspaces.revoke_test_authorization(task_id)}

    @app.get("/api/runtime/state")
    async def runtime_state() -> dict:
        """RESYNC 之后要恢复的**全部**权威状态（Turn 队列之外还有别的）。

        事件流只能表达增量。任何「丢一次事件就会让界面永久停在错误状态」的东西，
        都必须能从服务端重新查出来：

        * `turn_queue`：运行中 / 排队的 Turn（含 revision，供前端做新旧比较）；
        * `approvals`：仍在等待用户决定的审批（断线错过的 APPROVAL_REQUIRED）；
        * `interrupted_turns`：上一个进程结束时**已经被接受、但没有执行完**的
          用户消息（排队中就退出、或执行到一半退出）。它们不会被自动重放，
          但也不能静默消失 —— 界面据此如实告诉用户，并提供「重发 / 知道了」。
        * `tasks`：仍在跑 / 仍在排队的独立任务（断线错过的 SUBAGENT_STATUS）。

        `instance_id` 与后端实例绑定：后端重启后 revision 会从头计数，
        前端据此判断「基准已经换了」，而不是把新实例的低 revision 当成旧状态。
        """
        return {
            "instance_id": instance_id,
            "revision": ctx.turns.snapshot()["revision"],
            "turn_queue": ctx.turns.snapshot(),
            "approvals": ctx.approvals.pending(),
            # 上一次进程结束时仍没人回答的审批：不恢复等待，只说清「那次操作没有执行」。
            "interrupted_approvals": ctx.approvals.interrupted(),
            # 上一次进程结束时没有被执行完的用户消息（见 storage/turn_journal.py）。
            "interrupted_turns": ctx.turn_journal.unfinished(),
            "tasks": ctx.task_manager.snapshot(),
            # 工具执行的权威事实（活工具 + 最近结束的工具）：
            # TOOL_END 可能丢在失真区间里，但终态本身是服务器已经知道的事实，
            # 客户端据此把「运行中」的卡片恢复成真实的 success / failed / cancelled。
            # 只有服务器也确认不了（记录已回收 / 进程重启）时才轮到 unknown。
            "tools": ctx.tool_executions(),
            # 本轮已输出的执行叙事（模型文案 + 系统生成的调用摘要）：
            # 断线期间丢失的叙事在这里补齐，客户端按 narrative_id 去重。
            "narratives": ctx.active_turn_narratives(),
            # 结束事实（R4 S6）：RESYNC 时把「当前相关轮次」（运行 / 排队 / 刚取消 /
            # 上一个进程留下的未完成轮）的事实一并给前端，按 turn_id 合并。
            "turn_facts": _turn_facts_for(_current_turn_ids()),
        }

    @app.post("/api/turns/{turn_id}/cancel")
    async def cancel_turn_by_id(turn_id: str) -> dict:
        """按 turn_id 取消 —— 运行中或仍在排队中的都可。"""
        ok = ctx.turns.cancel(turn_id)
        return {"ok": ok, "cancelled": ok, "turn_id": turn_id}

    # -- 未执行的用户消息（重启恢复）--------------------------------------
    # 语义：只留痕 + 用户决定，**不自动重放**。reason/time 都如实给，
    # 前端不需要也不可能「猜」出这条消息到底执行过没有。

    @app.post("/api/turns/{turn_id}/resend")
    async def resend_turn(turn_id: str) -> dict:
        """把一条「被接受但没有执行」的消息按原话题重新提交。

        一次性：先用带条件的 UPDATE 抢占（`claim`），抢不到就 409 ——
        所以同一条不可能被重发两次，已经完成的 turn 也不可能被重发。
        """
        record = ctx.turn_journal.recoverable(turn_id)
        if record is None:
            raise HTTPException(
                status_code=409,
                detail="这一条不在「未执行」状态（可能已经执行完成或已经被处理过），不能重发",
            )
        # 附件（§1.2）：重发按**原来那一轮**的清单重新归属（retry_of_turn_id=原轮），
        # 所以原轮附件可以克隆复用。预检必须在 claim **之前**：被拒绝的重发不消费 claim。
        retry_ids = attachments.retry_attachment_ids(turn_id)
        precheck = attachments.precheck_for_turn(
            attachment_ids=retry_ids,
            topic_id=record["topic_id"],
            retry_of_turn_id=turn_id,
        )
        if precheck:
            raise _attachment_failure(precheck)
        if not ctx.turn_journal.claim(turn_id):
            raise HTTPException(status_code=409, detail="这一条已经被处理过了")
        pending = ctx.bindings.peek_intent()
        try:
            turn = ctx.turns.submit(
                record["message"],
                record["topic_id"],
                intent_id=pending.intent_id if pending else None,
            )
            outcome = attachments.bind_for_turn(
                turn_id=turn.turn_id,
                message_id=None,
                attachment_ids=retry_ids,
                topic_id=record["topic_id"],
                retry_of_turn_id=turn_id,
            )
        except Exception:
            ctx.turn_journal.release_claim(turn_id)  # 提交失败 → 退回去，用户还能再试
            raise
        if outcome.rejected:
            # 预检之后的竞态：撤销刚提交的这一轮、退回 claim，结构化失败（不静默丢附件）。
            ctx.turns.cancel(turn.turn_id)
            ctx.turn_journal.release_claim(turn_id)
            raise _attachment_failure(outcome.rejected)
        ctx.turn_journal.mark_recovered(turn_id, new_turn_id=turn.turn_id)
        return {
            "ok": True,
            "recovered_turn_id": turn_id,
            "turn_id": turn.turn_id,
            "status": turn.status,
            # 重发同样要带回执：重试复用的克隆是**新 id**，前端以回执为准
            **outcome.as_receipt(),
            "attachments": [attachments.payload(a, check=False) for a in outcome],
        }

    @app.post("/api/turns/{turn_id}/dismiss")
    async def dismiss_turn(turn_id: str) -> dict:
        """用户选择「知道了」：不再提示，但记录与消息原文仍然保留（不删用户数据）。"""
        ok = ctx.turn_journal.dismiss(turn_id)
        if not ok:
            raise HTTPException(status_code=409, detail="这一条不在「未执行」状态")
        return {"ok": True, "dismissed": turn_id}

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

    def _current_turn_ids() -> list[str]:
        """「当前相关轮次」：运行中 + 排队中 + 刚取消 + 上个进程留下的未完成轮。

        只按权威来源取 id（queue 快照 / 台账），不扫全表。
        """
        snapshot = ctx.turns.snapshot()
        ids: list[str] = []
        running = snapshot.get("running") or {}
        if running.get("turn_id"):
            ids.append(str(running["turn_id"]))
        ids.extend(str(item.get("turn_id") or "") for item in snapshot.get("queued") or [])
        ids.extend(str(item.get("turn_id") or "") for item in snapshot.get("cancelled") or [])
        ids.extend(str(row.get("turn_id") or "") for row in ctx.turn_journal.unfinished())
        return ids

    def _turn_facts_for(turn_ids) -> list[dict]:
        """这一页 / 当前相关轮次的结束事实（R4 S6）：**一次批量查询**，绝不 N+1。

        * 只给台账里**确实有事实**的轮次：旧记录（迁移前三列全 NULL）不出现 ——
          不伪造成 none / 假原因；没有任何事实时就是空数组。
        * 顺序 = 调用方给的顺序（前端按 turn_id 合并，顺序只影响可读性）。
        * 台账读不出来只记日志：历史接口照常返回（不能因为旁路台账挂掉）。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for item in turn_ids:
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                wanted.append(value)
        if not wanted:
            return []
        try:
            rows = ctx.turn_journal.facts(wanted)
        except Exception as exc:  # noqa: BLE001 - 台账读不出来不能影响历史接口
            logger.warning("结束事实读台账失败（历史照常返回）：%s", redact_text(str(exc)))
            return []
        out: list[dict] = []
        for turn_id in wanted:
            row = (rows or {}).get(turn_id)
            if not row:
                continue
            actions = [str(a) for a in (row.get("actions") or [])]
            if not any(
                (row.get("reason_code"), row.get("reason"), row.get("stopped_by"), actions)
            ):
                continue  # 旧记录：没有事实就不带这一条（不给假原因）
            out.append(
                {
                    "turn_id": str(row.get("turn_id") or turn_id),
                    "status": row.get("status"),
                    "reason_code": row.get("reason_code"),
                    "reason": row.get("reason"),
                    "stopped_by": row.get("stopped_by"),
                    "actions": actions,
                }
            )
        return out

    def _history_page_with_attachments(page: dict) -> dict:
        """给一页历史消息补上附件（问题 5：刷新 / 重进历史后附件行必须还在）。

        * 形状与实时发送路径**完全一致**：就是 attachments.payload()，前端 session.ts
          用同一个 toAttachmentRef 收敛，不需要第二套解析；
        * 整页一次批量查询（services/attachments.payloads_for_messages），不做 N+1；
        * 状态是**现在的事实**（payload(check=True)）：missing / changed / failed 在重新
          打开历史时如实呈现，而不是发送时写死的旧状态（契约 §1.6）；
        * 没有附件的消息不带这个字段（与实时路径 ...(refs.length ? {attachments} : {}) 一致），
          分页字段与 before 游标原样不动。
        """
        messages = page.get("messages") or []
        enriched = messages
        by_message = attachments.payloads_for_messages(messages)
        if by_message:
            enriched = []
            for message in messages:
                items = by_message.get(str(message.get("id") or ""))
                if items:
                    updated = dict(message)
                    updated["attachments"] = items
                    enriched.append(updated)
                else:
                    enriched.append(message)
        # 结束事实（R4 S6）：刷新 / 换设备后失败轮仍要说得出为什么、还有哪些操作。
        # 只查这一页涉及的轮次（一次批量），旧记录没有事实就不出现。
        return {
            **page,
            "messages": enriched,
            "turn_facts": _turn_facts_for(
                str(message.get("turn_id") or "") for message in messages
            ),
        }

    @app.get("/api/session/context")
    async def session_context(limit: int | None = None) -> dict:
        topic_id = ctx.current_topic()
        node = ctx.topics.nodes.get_topic(topic_id)
        # 首次只给最近一页（默认 200 条）：不再随历史长度线性增长
        page = _history_page_with_attachments(
            ctx.session_messages_page(topic_id, limit=limit or SESSION_PAGE_DEFAULT_LIMIT)
        )
        return {
            "topic_id": topic_id,
            "topic_name": node.name if node else topic_id,
            "anchor_fragment": ctx.anchor_fragment_info(),
            "messages": page["messages"],
            # 这一页涉及的轮次结束事实（R4 S6：失败轮的原因 / 可用操作，刷新后仍在）
            "turn_facts": page["turn_facts"],
            # 这一页涉及的工具调用（预览；全文走 /api/tool-records/{id}）
            "tool_records": page["tool_records"],
            "has_more": page["has_more"],
            "next_before": page["next_before"],
        }

    @app.get("/api/session/messages")
    async def session_messages_before(
        topic_id: str | None = None, before: str | None = None, limit: int | None = None
    ) -> dict:
        """更早的一页历史（用户向上读时按需加载）。

        每条消息同样带上 attachments（问题 5）：翻页翻到的历史附件也要能打开 / 重新定位。
        """
        target = topic_id or ctx.current_topic()
        page = _history_page_with_attachments(
            ctx.session_messages_page(
                target, limit=limit or SESSION_PAGE_DEFAULT_LIMIT, before=before
            )
        )
        return {"topic_id": target, **page}

    @app.get("/api/tool-records/{record_id}")
    async def tool_record(record_id: str) -> dict:
        """按 id 取一次工具调用的全文（参数 + 输出）。

        工具调用历史只服务用户复盘：不进上下文、不进摘要、不进检索索引。
        记录不存在返回 404；输出被保留期清掉时记录仍在（output_missing=1）。
        """
        from agent.storage.tool_records import get_record

        record = get_record(ctx.conn, record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="tool record not found")
        return record

    @app.delete("/api/tool-records/{record_id}")
    async def delete_tool_record(record_id: str) -> dict:
        """删掉一条工具调用历史（用户主动，不可撤销）。

        只删 `tool_records`（用户能回看的完整历史）；`tool_calls` / `turn_traces`
        是审计记录，是否记录由 `trace.enabled` 决定 —— 见 storage/tool_records.py。
        """
        from agent.storage.tool_records import delete_record

        if not delete_record(ctx.conn, record_id):
            raise HTTPException(status_code=404, detail="tool record not found")
        return {"ok": True, "deleted": record_id}

    @app.delete("/api/tool-records")
    async def clear_tool_records(
        topic_id: str | None = None, older_than_days: int | None = None
    ) -> dict:
        """清空工具调用历史（默认全部；也可只清某个话题 / 只清 N 天前的）。

        审计表不在这个动作的范围里：用户删的是「自己能回看的完整记录」。
        """
        from agent.storage.tool_records import delete_records

        deleted = delete_records(
            ctx.conn, topic_id=topic_id, older_than_days=older_than_days
        )
        return {"ok": True, "deleted": deleted, "scope": "tool_records"}

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
                    "ended": _topic_ended(ctx, f.topic_id),
                }
                for f in fingerprints
            ]
        }

    @app.post("/api/graph/topics/{topic_id}/end")
    async def end_topic(topic_id: str, body: dict | None = None) -> dict:
        """标记话题已结束：离开星球主视图，进「已结束」分组，记忆仍可搜到。"""
        reason = str((body or {}).get("reason") or "user_confirmed")
        try:
            node = ctx.topics.nodes.mark_topic_ended(topic_id, reason=reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="topic not found") from exc
        return {"ok": True, "topic_id": node.id, "ended": True}

    @app.post("/api/graph/topics/{topic_id}/resume")
    async def resume_topic(topic_id: str) -> dict:
        try:
            node = ctx.topics.nodes.resume_topic(topic_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="topic not found") from exc
        return {"ok": True, "topic_id": node.id, "ended": False}

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
            # 已结束分组：主视图放不下的「旧话题」在这里，搜索与记忆检索仍然可达。
            "ended_topics": [
                {
                    "topic_id": t.topic_id,
                    "title": t.title,
                    "fragment_count": t.fragment_count,
                    "last_activity": t.last_activity,
                    "summary_preview": t.summary_preview,
                    "visual_seed": t.visual_seed,
                }
                for t in ctx.planet.ended_overview()
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

        return OnboardingService(ctx.conn, app.version, main_credential=ctx.resolve_main_ref)

    @app.get("/api/onboarding/status")
    async def onboarding_status() -> dict:
        return _onboarding().status().to_dict()

    @app.post("/api/onboarding/seen")
    async def onboarding_seen() -> dict:
        """向导打开即记「本版本已展示过欢迎页」——保证每个版本只强制展开一次。"""
        return _onboarding().mark_seen().to_dict()

    @app.post("/api/onboarding/profile")
    async def onboarding_profile(body: dict) -> dict:
        """（v1 兼容入口）逐步落库；v2 的核对清单走 /api/onboarding/submit。"""
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

    @app.post("/api/onboarding/submit")
    async def onboarding_submit(body: dict) -> dict:
        """核对清单确认后的一次性写入：用户填的直接生效，模型推测的等确认。"""
        try:
            return _onboarding().submit(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/onboarding/followups")
    async def onboarding_followups(body: dict) -> dict:
        """按用户自己的描述现问一到两个追问；没有可用模型时返回空列表。"""
        from agent.services.followups import suggest_follow_ups

        description = str(body.get("description") or "").strip()
        if not description:
            return {"questions": []}
        adapter = await ctx.build_adapter()
        if adapter is None:
            return {"questions": []}
        questions, _error = await suggest_follow_ups(adapter, description)
        return {"questions": questions}

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

    @app.post("/api/knowledge/{knowledge_id}/end")
    async def end_knowledge(knowledge_id: str, body: dict | None = None) -> dict:
        """标记「已结束」：不再是当前状态，但仍可被相关对话参考到（权重降低）。"""
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        reason = str((body or {}).get("reason") or "user_confirmed")
        try:
            item = ks.mark_ended(knowledge_id, reason=reason)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="knowledge not found") from exc
        return {"ok": True, "knowledge": _knowledge_payload(ctx, item)}

    @app.post("/api/knowledge/{knowledge_id}/resume")
    async def resume_knowledge(knowledge_id: str) -> dict:
        """撤销「已结束」：重新当作当前状态。"""
        from agent.knowledge.lifecycle import KnowledgeService

        ks = KnowledgeService(ctx.conn)
        try:
            item = ks.resume(knowledge_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="knowledge not found") from exc
        return {"ok": True, "knowledge": _knowledge_payload(ctx, item)}

    @app.post("/api/knowledge/{knowledge_id}/scope")
    async def set_knowledge_scope(knowledge_id: str, body: dict) -> dict:
        """改适用范围：全局（你）/ 只在某个话题里生效。归属管理在知识页，不在引导里。"""
        from agent.knowledge.lifecycle import KnowledgeService

        scope_type = str(body.get("type") or "global")
        ks = KnowledgeService(ctx.conn)
        if scope_type == "global":
            user_node = ctx.topics.nodes.get_or_create_user_root()
            node_ids, topic_id = [user_node.id], None
        elif scope_type == "topic":
            topic_id = str(body.get("topic_id") or "")
            node = ctx.topics.nodes.get_topic(topic_id)
            if node is None:
                raise HTTPException(status_code=404, detail="topic not found")
            node_ids = [topic_id]
        else:
            raise HTTPException(status_code=400, detail="invalid scope type")
        try:
            item = ks.set_scope(knowledge_id, node_ids=node_ids, topic_id=topic_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="knowledge not found") from exc
        return {"ok": True, "knowledge": _knowledge_payload(ctx, item)}

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
