# -*- coding: utf-8 -*-
"""内部事件总线：emit / waterfall / parallel / serial 四种分发（异步）。

与 SSE EventBus（agent/api/bus.py，面向前端）分离：本总线是后端内部协作
机制，监听器通过 on() 注册、返回 disposer 可逆卸载。
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

Handler = Callable[..., Any]


async def _maybe_await(result: Any) -> Any:
    """统一适配同步/异步 handler：awaitable 则等待，否则原样返回。"""
    if inspect.isawaitable(result):
        return await result
    return result


class InternalEventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Handler]] = {}

    def on(self, event: str, handler: Handler) -> Callable[[], None]:
        """注册监听器；返回可逆卸载函数。"""
        self._listeners.setdefault(event, []).append(handler)

        def dispose() -> None:
            listeners = self._listeners.get(event)
            if listeners and handler in listeners:
                listeners.remove(handler)

        return dispose

    # -- emit：观察者，按注册顺序调用，不等待返回值 -------------------------
    async def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        for handler in list(self._listeners.get(event, [])):
            await _maybe_await(handler(*args, **kwargs))

    # -- waterfall：顺序中间件，有返回值，可短路 ----------------------------
    async def waterfall(self, event: str, value: Any, *args: Any) -> Any:
        handlers = list(self._listeners.get(event, []))

        async def dispatch(index: int, current: Any) -> Any:
            if index >= len(handlers):
                return current
            delegated = False
            box: dict[str, Any] = {}

            async def next_value(override: Any = None) -> Any:
                nonlocal delegated
                delegated = True
                result = await dispatch(index + 1, current if override is None else override)
                box["v"] = result
                return result

            outcome = await _maybe_await(handlers[index](current, *args, next=next_value))
            if delegated:
                return box.get("v", current)
            return outcome  # 未调 next → 短路

        return await dispatch(0, value)

    # -- parallel：并行等待，不返回 -----------------------------------------
    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> None:
        handlers = list(self._listeners.get(event, []))

        async def _run(handler: Handler) -> None:
            await _maybe_await(handler(*args, **kwargs))

        if handlers:
            await asyncio.gather(*(_run(h) for h in handlers))

    # -- serial：顺序等待，值依次传递 ----------------------------------------
    async def serial(self, event: str, value: Any, *args: Any) -> Any:
        current = value
        for handler in list(self._listeners.get(event, [])):
            current = await _maybe_await(handler(current, *args))
        return current
