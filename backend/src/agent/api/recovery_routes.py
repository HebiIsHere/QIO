"""A01 + A03 的恢复端点（W1 交付）。

一行接线（`api/server.py` 由 Lead 改）：

    from agent.api.recovery_routes import build_router
    app.include_router(build_router(ctx))

工厂签名固定 `build_router(ctx) -> APIRouter`，模块级、**不 import
`api/server.py`**（避免循环）。只依赖 ctx 上的：

* `ctx.conn`：库连接（必需）；
* `ctx.instances`（或 `ctx.instance_registry`）：实例归属判定，没有就按 `ctx.conn`
  自己造一个 `InstanceRegistry`（只读用途，不 start）；
* `ctx.turns`：turn 队列（「继续」要真的派发新 turn，必需；缺了就 503）；
* `ctx.turn_journal` / `ctx.instance_id`（可选：复用既有台账与实例身份）；
* `ctx.bindings`（可选：把排队中的接续意图带给新 turn）。

端点（契约 §2.2）：

```
GET  /api/recovery/records?limit=200[&kinds=user_turn,derived_task][&classes=...]
POST /api/recovery/records/{record_id}/continue   {"expected_class","expected_status"}
POST /api/recovery/records/{record_id}/repair     {"expected_class":"orphaned_claim"}
POST /api/recovery/records/{record_id}/ignore     {"expected_class"}
POST /api/recovery/records/{record_id}/requeue    {"expected_state","expected_generation"}
```
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from agent.services.recovery import (
    RecoveryConflict,
    RecoveryDispatchError,
    RecoveryInbox,
)

__all__ = ["build_router"]


class _ContinueBody(BaseModel):
    """客户端把它看到的状态一起回传：状态变了就 409，而不是抢一条别的状态。"""

    model_config = ConfigDict(extra="ignore")

    expected_class: str | None = None
    expected_status: str | None = None
    topic_id: str | None = None


class _ExpectedClassBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_class: str | None = None


class _RequeueBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expected_state: str | None = None
    expected_generation: int | None = None


def build_router(ctx: Any) -> APIRouter:
    """按 ctx 造出恢复端点（由 Lead 在 `api/server.py` include 一行）。"""
    router = APIRouter(prefix="/api/recovery", tags=["recovery"])

    def _registry() -> Any:
        registry = getattr(ctx, "instance_registry", None)
        if registry is None:
            registry = getattr(ctx, "instances", None)
        if registry is None:
            # 没有归属表时按 ctx.conn 造一个只读判定器（不 start、不写实例行）。
            from agent.storage.instance_registry import InstanceRegistry

            registry = InstanceRegistry(ctx.conn)
        return registry

    def _submitter() -> Any:
        turns = getattr(ctx, "turns", None)
        if turns is None or not hasattr(turns, "submit"):
            return None
        bindings = getattr(ctx, "bindings", None)

        def _submit(message: str, topic_id: str | None, turn_id: str) -> Any:
            intent_id = None
            if bindings is not None:
                try:
                    pending = bindings.peek_intent()
                except Exception:  # noqa: BLE001 - 意图读取失败不影响重发
                    pending = None
                if pending is not None:
                    intent_id = getattr(pending, "intent_id", None)
            return turns.submit(message, topic_id, intent_id=intent_id, turn_id=turn_id)

        return _submit

    def _inbox() -> RecoveryInbox:
        # 每次请求现造：它只是「表 + 归属判定 + 派发通道」的组合，没有缓存状态。
        return RecoveryInbox(
            ctx.conn,
            _registry(),
            turns=getattr(ctx, "turns", None),
            instance_id=getattr(ctx, "instance_id", None),
            submitter=_submitter(),
            journal=getattr(ctx, "turn_journal", None),
        )

    @router.get("/records")
    async def list_records(
        limit: int = 200,
        kinds: str | None = None,
        classes: str | None = None,
    ) -> dict:
        """只读清单：四类历史状态分开表达，unknown 绝不被当成「死」。"""
        return _inbox().list_records(
            limit=limit,
            kinds=_split(kinds),
            classes=_split(classes),
        ).to_dict()

    @router.post("/records/{record_id}/continue")
    async def continue_record(record_id: str, body: _ContinueBody | None = Body(default=None)) -> Any:
        """接手一条「被接受但没有执行」的消息，并按原话题重新提交一个 turn。"""
        payload = body or _ContinueBody()
        try:
            result = _inbox().take_over_and_continue(
                record_id,
                expected_class=payload.expected_class,
                expected_status=payload.expected_status,
                topic_id=payload.topic_id,
            )
        except RecoveryConflict as exc:
            return _conflict(str(exc))
        except RecoveryDispatchError as exc:
            # 明确的失败（没有假 200）：关联已经写好，记录不会消失，可稍后重试。
            return JSONResponse(
                status_code=503,
                content={"ok": False, "accepted": False, "conflict": False, "reason": str(exc)},
            )
        return result

    @router.post("/records/{record_id}/repair")
    async def repair_record(record_id: str, body: _ExpectedClassBody | None = Body(default=None)) -> Any:
        """修复「抢占过但没有后继」的记录，让它重新可继续 / 可忽略。"""
        payload = body or _ExpectedClassBody()
        result = _inbox().repair_orphan(record_id, expected_class=payload.expected_class)
        if not result.get("ok"):
            return JSONResponse(status_code=409, content=result)
        return result

    @router.post("/records/{record_id}/ignore")
    async def ignore_record(record_id: str, body: _ExpectedClassBody | None = Body(default=None)) -> Any:
        """用户已知晓：不再提示，原文保留，不产生后继。"""
        payload = body or _ExpectedClassBody()
        result = _inbox().ignore(record_id, expected_class=payload.expected_class)
        if not result.get("ok"):
            return JSONResponse(status_code=409, content=result)
        return result

    @router.post("/records/{record_id}/requeue")
    async def requeue_record(record_id: str, body: _RequeueBody | None = Body(default=None)) -> Any:
        """把一条卡住的 running 派生任务交回队列（条件更新 + 代次递增）。"""
        payload = body or _RequeueBody()
        result = _inbox().requeue_derived(
            record_id,
            expected_state=payload.expected_state,
            expected_generation=payload.expected_generation,
        )
        if not result.get("ok"):
            return JSONResponse(status_code=409, content=result)
        return result

    return router


def _split(value: str | None) -> list[str] | None:
    """`kinds=user_turn,derived_task` → ["user_turn", "derived_task"]。"""
    if not value:
        return None
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return parts or None


def _conflict(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"ok": False, "conflict": True, "reason": reason},
    )
