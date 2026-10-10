# -*- coding: utf-8 -*-
"""A04 端点：用户管理「被保护而没落的自动候选」（契约 §3.3）。

```
GET  /api/entities/candidates?include_archived=true&limit=200 -> listing（跨卡片）
GET  /api/entities/{entity_id}/candidates                     -> {"entity": {...}, "candidates": [...]}
POST /api/entities/{entity_id}/candidates/{candidate_id}/adopt
     body {"expected_revision": N} -> 200 {"ok":true,"entity":...,"adopted":{...}}
                                   -> 409 {"ok":false,"conflict":true,"current_revision":N,"reason":"..."}
POST /api/entities/{entity_id}/candidates/{candidate_id}/dismiss
     body {"expected_revision": N} -> 200 {"ok":true,"dismissed":true} | 409
```

挂载（Lead 在 `api/server.py` 里做，一行）：

```python
from agent.api.entity_pending_routes import build_router as build_entity_pending_router
app.include_router(build_entity_pending_router(ctx))
```

**必须在 `ctx = AppContext(...)` 之后立刻挂**（即所有 `@app.get("/api/entities/...")`
装饰器之前）：starlette 按注册顺序取第一个能匹配的路由，而既有的
`GET /api/entities/{entity_id}` 会把 `/api/entities/candidates` 当成 entity_id="candidates"
先吃掉。本模块只依赖 `ctx.conn`，不 import `api/server.py`（避免循环依赖）。

兜底：同一处理器另外挂在 `GET /api/entity-candidates`（没有路径参数冲突的别名）。
正常挂载顺序下用它的人不需要；它是「挂载顺序无法保证时」的安全出口，别让界面因此拿不到清单。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from agent.entities.cards import EntityCardService
from agent.entities.pending import (
    CandidateConflict,
    CandidateNotFound,
    adopt,
    dismiss,
    list_candidates,
)

# 默认每页候选数（与契约一致）。
DEFAULT_LIMIT = 200


def build_router(ctx) -> APIRouter:  # noqa: ANN001 - ctx 由 Lead 传入（AppContext）
    """A04 路由工厂：只依赖 `ctx.conn`。"""
    conn = ctx.conn
    router = APIRouter()

    def _cross_card_listing(include_archived: bool, limit: int) -> dict:
        return list_candidates(
            conn,
            include_archived=include_archived,
            limit=limit,
        ).to_dict()

    @router.get("/api/entities/candidates")
    async def list_all_candidates(
        include_archived: bool = True, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """跨卡片的待处理候选清单（只读；total/truncated 如实）。"""
        return _cross_card_listing(include_archived, limit)

    @router.get("/api/entity-candidates")
    async def list_all_candidates_alias(
        include_archived: bool = True, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """同上（别名路径）：见模块 docstring 的挂载顺序说明。"""
        return _cross_card_listing(include_archived, limit)

    @router.get("/api/entities/{entity_id}/candidates")
    async def list_entity_candidates(
        entity_id: str, include_archived: bool = True, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """单张卡片的候选（连实体一起给，界面不用再拉一次）。"""
        svc = EntityCardService(conn)
        card = svc.get(entity_id)
        if card is None:
            raise HTTPException(status_code=404, detail="entity not found")
        listing = list_candidates(
            conn,
            entity_id=entity_id,
            include_archived=include_archived,
            limit=limit,
        ).to_dict()
        return {"entity": svc.to_dict(card), **listing}

    @router.post("/api/entities/{entity_id}/candidates/{candidate_id}/adopt")
    async def adopt_candidate(
        entity_id: str, candidate_id: str, body: dict | None = None
    ) -> dict:
        payload = body or {}
        try:
            outcome = adopt(
                conn,
                entity_id,
                candidate_id,
                expected_revision=payload.get("expected_revision"),
            )
        except CandidateNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
        except CandidateConflict as exc:
            return _conflict_response(exc)
        if not outcome.ok:
            # 归档卡 / 不支持的候选类型：条件不满足，就不是成功。
            return _blocked_response(conn, entity_id, outcome)
        return outcome.to_dict()

    @router.post("/api/entities/{entity_id}/candidates/{candidate_id}/dismiss")
    async def dismiss_candidate(
        entity_id: str, candidate_id: str, body: dict | None = None
    ) -> dict:
        payload = body or {}
        try:
            outcome = dismiss(
                conn,
                entity_id,
                candidate_id,
                expected_revision=payload.get("expected_revision"),
            )
        except CandidateNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
        except CandidateConflict as exc:
            return _conflict_response(exc)
        return outcome.to_dict()

    return router


def _conflict_response(exc: CandidateConflict):
    """409：条件校验失败，什么都没改。"""
    return JSONResponse(status_code=409, content=exc.to_dict())


def _blocked_response(conn, entity_id: str, outcome):
    """采纳被挡住（归档卡 / 不支持的候选类型）→ 409 + 原因，界面据此说明。"""
    card = EntityCardService(conn).get(entity_id)
    return JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "conflict": True,
            "current_revision": card.revision if card is not None else None,
            "reason": outcome.blocked_reason,
            "detail": outcome.detail,
        },
    )
