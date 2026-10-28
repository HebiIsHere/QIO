"""模型目录 / 用途分配 / 默认模型 / 按模型验证的 HTTP 路由。

契约见 _STG-COMMON.md §6.3。返回形状固定为 camelCase 以外的小写下划线键
（与基线 credentials 接口一致）；**任何响应都不含密钥**：这里只读元数据与模型记录，
密钥只在 verify / discover 内部读到即用，不回包、不进日志。

挂载方式（由 server.py 调用一次，见报告）：

    from agent.api.model_routes import register_model_routes

    register_model_routes(app, ctx)

安全边界（与 services/model_catalog.py 同一口径）：
* 只有 POST /api/models/discover 会向服务商发认证请求，且只发给这条连接**已保存**的地址；
* GET 一律只读本地事实，不联网；
* 验证结果按 (credential_id, model_id, 配置指纹) 记录，迟到结果只留痕。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def _store(ctx: Any) -> Any:
    from agent.credentials.models import ModelCatalogStore

    return ModelCatalogStore(ctx.conn, ctx.credentials)


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_or(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def register_model_routes(app: Any, ctx: Any) -> None:
    """把模型目录相关路由挂到一个已创建的 FastAPI app 上。"""

    @app.get("/api/models/catalog")
    async def models_catalog(refresh: int = 0) -> dict:
        """目录快照：连接 → 模型 → 用途分配 → 默认模型（**只读，不联网**）。

        refresh 参数保留给界面（契约 §6.3），但这里刻意**不**在 GET 里发起认证请求：
        GET 可能被自动重试，带网络副作用的 GET 会让「刷新」变成不可控的重复探测。
        真正的刷新走 POST /api/models/discover（用户显式动作）。
        """
        result = _store(ctx).catalog()
        result["refresh_requested"] = bool(refresh)
        return result

    @app.get("/api/models/catalog/public")
    async def models_catalog_public() -> dict:
        """公共模型目录说明（哪些服务商的 /models 是公共目录）。

        只返回静态事实，不发任何请求：公共目录不是权限证明。
        """
        from agent.services.model_catalog import public_catalog_info

        return public_catalog_info()

    @app.post("/api/models/discover")
    async def models_discover(body: dict) -> dict:
        """获取一条已保存连接的模型列表（唯一会发认证请求的入口）。"""
        from agent.services import model_catalog as discovery

        credential_id = str(body.get("credential_id") or "").strip()
        if not credential_id:
            raise HTTPException(status_code=400, detail="请先选择要获取模型的连接")
        try:
            return await discovery.discover(
                ctx,
                credential_id,
                page=_int_or(body.get("page"), 1),
                page_size=_int_or(body.get("page_size"), discovery.DISCOVERY_PAGE_SIZE_DEFAULT),
                refresh=_bool_or(body.get("refresh"), False),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/models/usage")
    async def models_set_usage(body: dict) -> dict:
        """分配用途：给 (credential_id, model_id) 或恢复跟随默认（两者都为空）。"""
        usage_key = str(body.get("usage_key") or "").strip()
        if not usage_key:
            raise HTTPException(status_code=400, detail="缺少用途标识")
        store = _store(ctx)
        try:
            assignment = store.set_usage(
                usage_key,
                body.get("credential_id"),
                body.get("model_id"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        usages = {item["usage_key"]: item for item in store.list_usages()}
        return {
            "ok": True,
            "usage": assignment,
            "resolved": usages.get(str(assignment["usage_key"]), {}).get("resolved"),
            "usages": list(usages.values()),
        }

    @app.post("/api/models/default")
    async def models_set_default(body: dict) -> dict:
        """设置默认模型（日常对话用哪条连接的哪个模型）。"""
        credential_id = str(body.get("credential_id") or "").strip()
        model_id = str(body.get("model_id") or "").strip()
        if not credential_id or not model_id:
            raise HTTPException(status_code=400, detail="请选择默认模型所在的连接与模型")
        store = _store(ctx)
        try:
            default = store.set_default(credential_id, model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        from agent.credentials.usages import find_usage

        main_chat = find_usage("main-chat")
        resolved = store.describe_usage(main_chat, default=default) if main_chat else None
        return {"ok": True, "default": default, "main_chat": resolved}

    @app.post("/api/models/verify")
    async def models_verify(body: dict) -> dict:
        """对一个模型做一次真实调用验证（不改变 Key 级验证状态）。"""
        from agent.services.model_catalog import verify_catalog_model

        credential_id = str(body.get("credential_id") or "").strip()
        model_id = str(body.get("model_id") or "").strip()
        if not credential_id or not model_id:
            raise HTTPException(status_code=400, detail="请选择要验证的连接与模型")
        try:
            return await verify_catalog_model(ctx, credential_id, model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/models/usage-resolve")
    async def models_usage_resolve(usage_key: str) -> dict:
        """一个用途现在实际会打到哪条连接的哪个模型（调试 / 界面展示用）。"""
        from agent.services.model_catalog import resolve_usage

        try:
            return resolve_usage(ctx, usage_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
