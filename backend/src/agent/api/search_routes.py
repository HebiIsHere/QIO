"""联网搜索设置：通道状态 + 真实连接检查（设置页改版 §9）。

为什么单独一个路由工厂
----------------------
``api/server.py`` 已经很长了（四千多行），搜索状态/连接检查是**设置页改版新增**的一组
只读/低频接口。路由放在本文件里、由 server.py 挂一行 ``register_search_routes(app, ctx)``，
既不动既有接口，也避免把新的判断逻辑塞进那个长文件。

两条口径（都不是文案问题）
--------------------------
1. **「已配置」≠「检查通过」**。``GET /api/settings/search/status`` 里
   ``configured`` 是本地判断（有没有填 Key / 地址、通道是否免密钥），
   ``usable`` 只来自一次真实检查；没检查过就是 ``null`` + ``check_state="unchecked"``，
   界面必须显示「未检查」，不得画成「已验证可用」。
2. **检查必须是真的**。``POST /api/settings/search/check`` 真的发一次极小请求（带短超时），
   并把断网 / 超时 / 限流 / 凭据无效 / 通道反爬分开报；不存在「定时任务成功过所以显示可用」。
   一次检查就是一次调用，结果只由这次调用决定。

安全边界：响应里**没有**任何密钥原文，博查 Key 只以 ``bocha_has_key`` 布尔出现；
搜索凭据与模型凭据分开，这里不读 ``credentials`` 表（博查 Key 不是模型 Key）。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def register_search_routes(app: Any, ctx: Any) -> None:
    from agent.services.search import CHANNEL_SPECS

    channel_ids = [spec["id"] for spec in CHANNEL_SPECS]

    def _service():
        service = getattr(ctx, "search_service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="搜索服务尚未初始化，请稍后重试")
        return service

    def _params() -> dict[str, Any]:
        """参数与范围：范围只有 services/settings_service.py 一处权威，这里只读出来。"""
        from agent.services.settings_service import (
            SEARCH_MAX_FETCH_CHARS_MAX,
            SEARCH_MAX_FETCH_CHARS_MIN,
            SEARCH_TOP_K_MAX,
            SEARCH_TOP_K_MIN,
        )

        store = ctx.settings_store
        return {
            "top_k_default": store.get_int("search.top_k_default", 5),
            "top_k_min": SEARCH_TOP_K_MIN,
            "top_k_max": SEARCH_TOP_K_MAX,
            "max_fetch_chars": store.get_int("search.max_fetch_chars", 15000),
            "max_fetch_chars_min": SEARCH_MAX_FETCH_CHARS_MIN,
            "max_fetch_chars_max": SEARCH_MAX_FETCH_CHARS_MAX,
        }

    def _payload(wanted: list[str] | None = None) -> dict[str, Any]:
        service = _service()
        store = ctx.settings_store
        return {
            "channels": service.channel_status(wanted),
            # 真实回退顺序（从上到下尝试）：关闭免密钥通道只会让 exa/parallel/
            # duckduckgo 从这条链里消失，不会改动其余通道的相对顺序。
            "chain": service.chain_ids(),
            "keyless_fallback": store.get_bool("search.keyless_fallback", True),
            "bocha_has_key": bool(str(store.get("search.bocha_api_key", "") or "").strip()),
            "searxng_configured": bool(
                str(store.get("search.searxng_url", "") or "").strip()
            ),
            "params": _params(),
        }

    @app.get("/api/settings/search/status")
    async def search_status() -> dict:
        """各搜索通道：是否已配置 + 是否**真的**检查通过（未检查就是 null）。"""
        return _payload()

    @app.post("/api/settings/search/check")
    async def search_check(body: dict | None = None) -> dict:
        """对指定通道做一次真实、低影响的连接检查。

        ``body.channels`` 省略 = 检查全部通道。只接受已知的通道 id（不猜、不模糊匹配）。
        """
        body = body or {}
        raw = body.get("channels")
        wanted: list[str] | None
        if raw is None:
            wanted = None
        elif isinstance(raw, list):
            wanted = [str(item or "").strip() for item in raw]
            wanted = [item for item in wanted if item]
            unknown = [item for item in wanted if item not in channel_ids]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail="未知的搜索通道：" + "、".join(unknown),
                )
            if not wanted:
                wanted = None
        else:
            raise HTTPException(
                status_code=400, detail="channels 必须是通道 id 数组（省略则检查全部）"
            )
        service = _service()
        await service.check_channels(wanted)
        # 返回**全部**通道的最新状态（与 GET 同形状）：界面刷新一整张卡片，
        # 未检查的通道仍然是 usable=null，不会因为一次检查就都变成「可用」。
        payload = _payload()
        payload["checked_channels"] = wanted or list(channel_ids)
        return payload
