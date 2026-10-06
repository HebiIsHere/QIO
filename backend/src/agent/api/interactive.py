"""互动模式路由聚合（Lead 维护）。

子模块各自带 `router`（写全路径），这里只做 `include_router`；
`server.py` 只认这一个入口，避免多个子智能体同时改公共文件。
"""

from __future__ import annotations

from fastapi import APIRouter

from agent.api.interactive_intents import router as intents_router
from agent.api.interactive_store import router as store_router

router = APIRouter()
router.include_router(store_router)
router.include_router(intents_router)
