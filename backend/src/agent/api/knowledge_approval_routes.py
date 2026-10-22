"""知识保存申请的 HTTP 路由（D 提供；server.py 挂载）。

冻结接口（只追加、可合并）：

    POST /api/knowledge/{knowledge_id}/approval
        body {
          "decision": "approve" | "approve_modified" | "reject" | "ignore",
          "content": str?,              # 仅 approve_modified：用户确认后的内容
          "expected_version": int?,     # 用户看到的那一版（CAS）
          "expected_content": str?,     # 用户看到的内容（CAS）
          "expected_updated_at": str?   # 用户看到的更新时间（CAS）
        }
        -> {
          "ok": true, "decision": "...", "already_handled": bool,
          "knowledge": {...}, "supersedes"?: str, "message"?: str
        }

    错误一律结构化：
      400 -> {"ok": false, "code": "knowledge_approval_invalid", "error": ...}
      404 -> {"ok": false, "code": "knowledge_not_found", "error": ...}
      409 -> {"ok": false, "code": "version_changed" | "content_changed" |
              "already_handled" | "superseded" | "invalid_state",
              "error": ..., "current": {...}}
      500 -> {"ok": false, "code": "knowledge_approval_failed", "error": ...}

设计边界：

* **知识保存申请只走这个接口**（knowledge id，无 approval_id）。工具/权限/凭据
  申请（approval_id）继续走 `POST /api/approvals/{approval_id}/respond`，两边不串线；
  后台维护的 `kind=high_impact_knowledge` 审批带着 approval_id，属于后一类。
* 处理逻辑全部在 `agent.services.knowledge_approval`（单事务），本模块只做参数
  校验 / 错误映射 / 「不保存」的同类冷却记录，不写第二套业务语义。
* 挂载（api/server.py，一行）：

      from agent.api.knowledge_approval_routes import register_knowledge_approval_routes
      register_knowledge_approval_routes(app, ctx)

  必须在 create_app 的路由定义区末尾、`return app` 之前调用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from agent.services.knowledge_approval import (
    DECISION_APPROVE_MODIFIED,
    DECISION_IGNORE,
    KnowledgeApprovalConflict,
    KnowledgeApprovalError,
    KnowledgeApprovalInvalid,
    KnowledgeApprovalNotFound,
    dispatch_knowledge_decision,
)
from agent.trace.redact import redact_text


def _expected_version(body: dict[str, Any]) -> int | None:
    raw = body.get("expected_version")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise KnowledgeApprovalInvalid("expected_version 必须是整数") from None


def _error_response(exc: KnowledgeApprovalError) -> JSONResponse:
    """结构化错误响应；任何错误文本先过 redact（不得出现密钥原文）。"""
    payload = exc.to_dict()
    payload["error"] = redact_text(str(payload.get("error") or "")) or "知识保存申请处理失败"
    return JSONResponse(status_code=exc.status_code, content=payload)


def build_router(ctx: Any) -> APIRouter:  # noqa: ANN001 - ctx 由 Lead 传入（AppContext）
    """知识保存申请路由工厂：只依赖 ctx.conn / ctx.settings_store。"""
    router = APIRouter()

    @router.post("/api/knowledge/{knowledge_id}/approval")
    async def knowledge_approval(knowledge_id: str, body: dict | None = None) -> Any:
        payload = body or {}
        decision = str(payload.get("decision") or "").strip()
        content = payload.get("content")
        if content is not None and not isinstance(content, str):
            return _error_response(
                KnowledgeApprovalInvalid("content 必须是字符串；这次没有写入任何内容。")
            )
        try:
            result = dispatch_knowledge_decision(
                ctx.conn,
                knowledge_id,
                decision,
                content=content,
                expected_version=_expected_version(payload),
                expected_content=payload.get("expected_content"),
                expected_updated_at=payload.get("expected_updated_at"),
            )
        except KnowledgeApprovalError as exc:
            return _error_response(exc)
        # 「不保存」与既有 /api/knowledge/{id}/ignore 保持同一长期表态：
        # 记录同类冷却时间（下次同类候选在冷却期内不再弹到对话里）。
        # 数据库写入已在事务里完成；这里是内存/设置层的补充，失败不影响已保存结果。
        if result.get("decision") == DECISION_IGNORE:
            try:
                from agent.services.app import KNOWLEDGE_IGNORE_KEY_PREFIX

                category = str((result.get("knowledge") or {}).get("category") or "")
                if category:
                    ctx.settings_store.set(
                        f"{KNOWLEDGE_IGNORE_KEY_PREFIX}{category}",
                        datetime.now(timezone.utc).isoformat(),
                    )
            except Exception:  # noqa: BLE001 - 冷却记录失败不改变「没有保存」这个事实
                pass
        return result

    return router


def register_knowledge_approval_routes(app: Any, ctx: Any) -> None:  # noqa: ANN001
    """把知识保存申请路由挂到 FastAPI app 上（server.py 调用一行即可）。"""
    app.include_router(build_router(ctx))


__all__ = ["build_router", "register_knowledge_approval_routes"]
